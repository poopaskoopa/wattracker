package com.wattracker.android.cloud

import java.io.IOException
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min
import kotlin.math.pow
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext

/** A route's objects as the app should render them right now. */
data class CloudSnapshot(
    val route: CloudRoute,
    val revision: Int,
    val items: List<CloudItem>,
    val source: Source,
    /** Epoch millis when these objects were read (or last stored, for `.cache`). */
    val asOf: Long,
) {
    enum class Source {
        network,

        /** The network attempt failed or was refused; this is last-known data. */
        cache,
    }
}

/**
 * The token lifecycle, the offline cache, and the one place that decides this
 * device is gone.
 *
 * The iOS version is an `actor`; the interesting states are all concurrent
 * ones: five screens asking for data at launch, a token that expires between
 * two of them, a refresh that must happen once for all five. Kotlin's answer
 * is a [Mutex] for the state plus a shared [CompletableDeferred] for the one
 * in-flight refresh: the mutex is held only while reading or writing state,
 * and every network call and every blocking store access (Room,
 * EncryptedSharedPreferences) runs with it released -- the network on
 * `Dispatchers.IO` in the client, the stores via [onIo] -- so concurrent
 * callers overlap instead of queuing behind a page walk.
 *
 * The disk writes are generation-stamped: the cache refuses a write from an
 * identity older than one already committed, so a read that is still in flight
 * cannot put a wiped device's objects back after a removal or re-pairing.
 */
class CloudSession(
    private val client: CloudClient,
    private val credentials: DeviceCredentialStore,
    private val cache: SnapshotCache,
    private val removalGate: RemovalGate = InMemoryRemovalGate(),
    private val clock: () -> Long = { System.currentTimeMillis() },
    private val random: () -> Double = { Math.random() },
) : ReadSession {

    // MARK: - State

    enum class DeviceState { unpaired, paired, removed }

    /**
     * A failure the state machine surfaced. These are classes, not singletons:
     * one shared instance across every throw and thread would share a stack
     * trace and a suppressed-exception list, and these cross a lot of callers.
     */
    sealed class Failure(message: String) : Exception(message) {
        class NotPaired : Failure("This device is not paired yet")
        class DeviceRemoved : Failure("This device was removed")
        class Offline : Failure("No connection")
        class Throttled(val retryAfter: Double) :
            Failure("The server asked for ${retryAfter.toInt()}s before the next try")
        class ClockSkew(val seconds: Double) :
            Failure("This device's clock is ${seconds.toInt()}s off the server's")
        class Server(val status: Int) : Failure("HTTP $status")
    }

    /** A reader context and the two facts about it that decide when it is replaced. */
    private data class ReaderToken(val value: String, val generation: Int, val expiresAt: Long)

    // Read by the non-suspend status getters without locking. @Volatile so a
    // getter never blocks behind a network call and sees a coherent value.
    @Volatile private var device: PairedDevice? = null
    @Volatile private var state: DeviceState
    @Volatile private var token: ReaderToken? = null
    @Volatile private var lastSuccessfulRead: Long? = null

    // Everything else is touched only inside the mutex.
    private val mutex = Mutex()
    private var lifecycleGeneration = 0
    private var mintCount = 0
    private var nextAttemptAllowedAt: Long? = null
    private var consecutiveFailures = 0
    private var consecutiveRejections = 0
    private val activityObjects = HashMap<String, CloudItem>()
    private var pendingRefresh: CompletableDeferred<ReaderToken>? = null

    init {
        // An unfinished removal from a prior start -- a credential wipe that
        // failed, leaving the device paired with an empty cache -- is completed
        // now. Runs on the construction thread, which the app keeps off main.
        if (removalGate.takePending()) {
            try {
                credentials.clear()
            } catch (_: Exception) {
                removalGate.markPending()
            }
            cache.removeAll(lifecycleGeneration.toLong())
        }
        val stored = credentials.load()
        device = stored
        state = if (stored == null) DeviceState.unpaired else DeviceState.paired
    }

    // MARK: - What the app asks

    override val deviceState: DeviceState
        get() = state

    override val lastSuccess: Long?
        get() = lastSuccessfulRead

    override val isPaired: Boolean
        get() = device != null

    /**
     * Last-known data, with no network attempt at all.
     *
     * This is what a cold start renders first: the screens have something real
     * on them before the first request is built, and [load] then replaces it
     * with the reconciled result. It reads one small store and cannot fail --
     * an unreadable cache is simply nothing. Suspend so the Room read happens
     * off the caller's (possibly main) thread.
     */
    override suspend fun cached(route: CloudRoute): CloudSnapshot? {
        val stored = onIo { cache.load(route) } ?: return null
        return CloudSnapshot(route, stored.revision, stored.items, CloudSnapshot.Source.cache, stored.storedAt)
    }

    /**
     * Redeem a pairing code.
     *
     * A successful pairing is a new identity, so it starts from nothing: the
     * previous device's cached objects are not this one's, their revisions
     * mean nothing against the new credential's scope, and a `removed` state
     * left over from the credential being replaced would be wrong from the
     * first request onward. The generation commits *before* the disk is
     * touched, so a read that captured the old identity is refused by the
     * cache's epoch; the cache is wiped and the new credential written
     * durably *before* the in-memory state flips, so a crash in between is a
     * cold start rather than a new credential bound to the old checkpoint.
     */
    suspend fun pair(code: String, label: String? = null): PairedDevice {
        val beforeGeneration = mutex.withLock { lifecycleGeneration }
        val result = try {
            client.pair(code, label)
        } catch (e: Failure) {
            throw e
        } catch (e: IOException) {
            throw Failure.Offline()
        }
        val pairing = when (result) {
            is CloudApiResult.Failure -> throw Failure.Server(result.status)
            is CloudApiResult.Success -> result.value
        }
        val newGeneration = mutex.withLock {
            if (lifecycleGeneration != beforeGeneration) throw lifecycleFailure()
            lifecycleGeneration += 1
            lifecycleGeneration
        }
        onIo { cache.removeAll(newGeneration.toLong()) }
        onIo { credentials.save(pairing.device) }
        return mutex.withLock {
            if (lifecycleGeneration != newGeneration) throw lifecycleFailure()
            device = pairing.device
            state = DeviceState.paired
            mintCount += 1
            token = ReaderToken(
                value = pairing.initialReaderContext,
                generation = mintCount,
                expiresAt = now() + (min(pairing.expiresIn ?: defaultContextLifetime, maximumContextLifetime) * 1000).toLong(),
            )
            consecutiveFailures = 0
            consecutiveRejections = 0
            nextAttemptAllowedAt = null
            activityObjects.clear()
            pairing.device
        }
    }

    /** Forget this device locally: credential, token and cache. */
    suspend fun signOut() {
        val newGeneration = mutex.withLock { signOutState() }
        removeDisk(newGeneration)
    }

    /** Revoke this device on the server and forget it locally. */
    suspend fun removeDevice() {
        val (active, generation) = mutex.withLock { activeDevice() to lifecycleGeneration }
        val result = try {
            client.revoke(active.credentialId, active)
        } catch (e: Failure) {
            throw e
        } catch (e: IOException) {
            throw Failure.Offline()
        }
        when (result) {
            is CloudApiResult.Failure -> throw Failure.Server(result.status)
            is CloudApiResult.Success -> {
                val newGeneration = mutex.withLock {
                    if (lifecycleGeneration != generation) throw lifecycleFailure()
                    signOutState()
                }
                removeDisk(newGeneration)
            }
        }
    }

    /**
     * A route's objects, reconciled with the server where that is possible.
     *
     * Never throws when there is a cache to serve: airplane mode, a 429, a
     * token that cannot be refreshed and a server that is simply down all
     * produce last-known data marked `.cache` rather than an error. The one
     * exceptions are a removed or unpaired device, where continuing to show
     * the rider's data is the outcome being prevented.
     */
    override suspend fun load(route: CloudRoute): CloudSnapshot {
        val (readDevice, readGeneration) = mutex.withLock { activeDevice() to lifecycleGeneration }
        val cached = onIo { cache.load(route) }
        val failure: Exception? = try {
            val snapshot = reconcile(route, cached, readDevice, readGeneration)
            return mutex.withLock {
                validate(readDevice, readGeneration)
                lastSuccessfulRead = now()
                snapshot
            }
        } catch (e: Failure.DeviceRemoved) {
            throw e
        } catch (e: Failure.NotPaired) {
            throw e
        } catch (e: Exception) {
            e
        }
        return mutex.withLock {
            validate(readDevice, readGeneration)
            val stored = cached ?: throw failure!!
            CloudSnapshot(route, stored.revision, stored.items, CloudSnapshot.Source.cache, stored.storedAt)
        }
    }

    /** One ride's detail, fetched on demand and remembered for the session. */
    override suspend fun activityDetail(activityId: Int): ActivityDetail {
        val item = activityObject("activity-detail-$activityId") { dev, ctx ->
            client.activityDetail(activityId, ctx, dev)
        }
        return (item.payload as? CloudPayload.ActivityDetail)?.value ?: throw Failure.Server(0)
    }

    /** The bounded, downsampled streams, fetched only after detail opens. */
    override suspend fun activityStreams(activityId: Int): ActivityStreams {
        val item = activityObject("stream-$activityId") { dev, ctx ->
            client.activityStreams(activityId, ctx, dev)
        }
        return (item.payload as? CloudPayload.Stream)?.value ?: throw Failure.Server(0)
    }

    // MARK: - The token

    /** Under the mutex. */
    private fun activeDevice(): PairedDevice {
        if (state == DeviceState.removed) throw Failure.DeviceRemoved()
        if (state != DeviceState.paired) throw Failure.NotPaired()
        return device ?: throw Failure.NotPaired()
    }

    /**
     * A usable reader context, refreshing if one is needed.
     *
     * `after` is how a caller says "the token I just used was rejected". It
     * forces a token minted later than that one, so a refresh that produced the
     * dead token cannot be mistaken for the fix.
     */
    private suspend fun context(after: Int?): ReaderToken {
        while (true) {
            // Fast path and refresh acquisition, under the lock. A caller on the
            // fast path returns here; a caller that starts or joins a refresh
            // gets a decision and does the network with the lock released.
            val decision = mutex.withLock {
                if (state == DeviceState.removed) throw Failure.DeviceRemoved()
                if (device == null) throw Failure.NotPaired()
                val current = token
                if (current != null && isUsable(current, now()) && (after?.let { current.generation > it } ?: true)) {
                    return current
                }
                val allowed = nextAttemptAllowedAt
                if (allowed != null && allowed > now()) throw Failure.Throttled((allowed - now()) / 1000.0)
                val running = pendingRefresh
                if (running != null && running.isActive) {
                    RefreshDecision.Join(running)
                } else {
                    val started = CompletableDeferred<ReaderToken>()
                    pendingRefresh = started
                    RefreshDecision.Start(started)
                }
            }
            when (decision) {
                is RefreshDecision.Start -> {
                    try {
                        val fresh = performRefresh()
                        decision.deferred.complete(fresh)
                        return fresh
                    } catch (e: CancellationException) {
                        // The starting coroutine was cancelled (its screen
                        // left). Do not hand that cancellation to the waiters:
                        // publish an abort so they retry, and rethrow it only
                        // for ourselves.
                        decision.deferred.completeExceptionally(RefreshAborted())
                        throw e
                    } catch (e: Throwable) {
                        decision.deferred.completeExceptionally(e)
                        throw e
                    } finally {
                        mutex.withLock { if (pendingRefresh === decision.deferred) pendingRefresh = null }
                    }
                }
                is RefreshDecision.Join -> {
                    // Wait with the lock released, then loop and re-check.
                    try {
                        decision.deferred.await()
                    } catch (e: RefreshAborted) {
                        // The starter bailed (cancelled); loop and re-check,
                        // which starts a fresh refresh for the remaining callers.
                    } catch (e: Failure) {
                        throw e
                    }
                }
            }
        }
    }

    private sealed class RefreshDecision {
        class Start(val deferred: CompletableDeferred<ReaderToken>) : RefreshDecision()
        class Join(val deferred: CompletableDeferred<ReaderToken>) : RefreshDecision()
    }

    /** The in-flight refresh's starter was cancelled; waiters retry, not cancel. */
    private class RefreshAborted : Exception("the in-flight refresh was aborted")

    private fun isUsable(token: ReaderToken, now: Long): Boolean {
        val remaining = (token.expiresAt - now) / 1000.0
        return remaining > max(refreshAhead, minimumUsableLifetime)
    }

    /** One signed refresh, and everything that follows from how it went. */
    private suspend fun performRefresh(): ReaderToken {
        val (refreshDevice, refreshGeneration) = mutex.withLock {
            (device ?: throw Failure.NotPaired()) to lifecycleGeneration
        }
        val result = try {
            client.refreshReaderContext(refreshDevice)
        } catch (e: Failure) {
            throw e
        } catch (e: IOException) {
            // A transport error is the network, not the credential: it must
            // never move this device toward `removed`.
            mutex.withLock { noteFailure(retryAfter = null) }
            throw Failure.Offline()
        }
        // Apply under the lock; the removal's disk wipe happens after, off it,
        // so the lock is never held across the credential/cache writes.
        var removedAt: Int? = null
        try {
            return mutex.withLock {
                if (!isCurrent(refreshDevice, refreshGeneration)) throw lifecycleFailure()
                when (result) {
                    is CloudApiResult.Success -> {
                        consecutiveFailures = 0
                        consecutiveRejections = 0
                        nextAttemptAllowedAt = null
                        mintCount += 1
                        lastSuccessfulRead = now()
                        val fresh = ReaderToken(
                            value = result.value.readerContext,
                            generation = mintCount,
                            expiresAt = now() + (min(result.value.expiresIn, maximumContextLifetime) * 1000).toLong(),
                        )
                        token = fresh
                        fresh
                    }
                    is CloudApiResult.Failure -> {
                        val refusal = refusal(result)
                        if (refusal is Failure.DeviceRemoved) {
                            removedAt = markRemovedState()
                            throw refusal
                        }
                        throw refusal
                    }
                }
            }
        } catch (e: Failure) {
            removedAt?.let { removeDisk(it) }
            throw e
        }
    }

    /**
     * What a refused refresh means: the sharpest question this client has to
     * answer. The read plane answers 404 to every authentication failure
     * deliberately, so this client can never be *told* it was revoked -- it
     * can only observe that a correctly signed request stopped being accepted.
     *
     * So `removed` is inferred, and inferred conservatively:
     *
     * - Only 404 counts (a 403 is a quota refusal, a 401 a gateway).
     * - Twice, with a backoff between, so a deployment mid-restart is absorbed.
     * - Never while the clock is suspect -- including when that cannot be
     *   checked (a 404 with no readable `Date`).
     *
     * This decides the removal and flips the in-memory state; the disk wipe is
     * done by the caller (off the lock) via [removeDisk].
     */
    private fun refusal(result: CloudApiResult.Failure): Failure {
        val status = result.status
        val retryAfter = result.retryAfterSeconds
        val serverDate = result.serverDateMillis
        if (status == 404) {
            if (serverDate == null) {
                noteFailure(retryAfter = null)
                return Failure.Server(status)
            }
            val skew = serverDate / 1000.0 - now() / 1000.0
            if (abs(skew) > clockSkewTolerance) {
                noteFailure(retryAfter = null)
                return Failure.ClockSkew(seconds = skew)
            }
            consecutiveRejections += 1
            if (consecutiveRejections >= rejectionsBeforeRemoval) {
                // The state flip and the disk wipe are done by the caller,
                // which owns the (off-lock) [removeDisk]; this only decides.
                return Failure.DeviceRemoved()
            }
            noteFailure(retryAfter = null)
            return Failure.Server(status)
        }
        noteFailure(retryAfter = retryAfter)
        if (status == 429 || status == 503 || status == 403) {
            return Failure.Throttled(retryAfter ?: pendingDelay())
        }
        return Failure.Server(status)
    }

    /**
     * The durable half of a removal, run off the state lock. The rider's
     * cached data goes first -- so a failed credential wipe still leaves
     * nothing readable -- then the credential. A credential wipe that fails is
     * recorded so the next start finishes it.
     */
    private suspend fun removeDisk(generation: Int) {
        onIo { cache.removeAll(generation.toLong()) }
        try {
            onIo { credentials.clear() }
        } catch (e: Exception) {
            removalGate.markPending()
            throw e
        }
    }

    /** Under the mutex; returns the new identity. The disk is wiped by the caller. */
    private fun markRemovedState(): Int {
        lifecycleGeneration += 1
        state = DeviceState.removed
        token = null
        device = null
        lastSuccessfulRead = null
        consecutiveFailures = 0
        consecutiveRejections = 0
        nextAttemptAllowedAt = null
        activityObjects.clear()
        return lifecycleGeneration
    }

    /** Under the mutex; returns the new identity. The disk is wiped by the caller. */
    private fun signOutState(): Int {
        lifecycleGeneration += 1
        device = null
        token = null
        state = DeviceState.unpaired
        lastSuccessfulRead = null
        consecutiveFailures = 0
        consecutiveRejections = 0
        nextAttemptAllowedAt = null
        activityObjects.clear()
        return lifecycleGeneration
    }

    /**
     * Push the next attempt out. A server `Retry-After` is taken exactly
     * (clamped so a malformed value cannot overflow the arithmetic); otherwise
     * it is exponential with equal jitter (the floor is half the ceiling, not
     * one second -- full jitter's near-zero tail made two-strike removal a coin
     * flip).
     */
    private fun noteFailure(retryAfter: Double?) {
        consecutiveFailures += 1
        val raw = if (retryAfter != null) {
            retryAfter
        } else {
            val exponent = min(consecutiveFailures, 8)
            val ceiling = min(maximumBackoff, baseBackoff * 2.0.pow(exponent - 1))
            ceiling / 2 + random() * (ceiling / 2)
        }
        val delay = raw.coerceIn(0.0, maximumBackoffCeiling)
        nextAttemptAllowedAt = now() + (delay * 1000).toLong()
    }

    private fun pendingDelay(): Double {
        val allowed = nextAttemptAllowedAt ?: return baseBackoff
        return max((allowed - now()) / 1000.0, 0.0)
    }

    private fun isCurrent(device: PairedDevice, lifecycleGeneration: Int): Boolean =
        state == DeviceState.paired && this.device == device && this.lifecycleGeneration == lifecycleGeneration

    private fun lifecycleFailure(): Failure =
        if (state == DeviceState.removed) Failure.DeviceRemoved() else Failure.NotPaired()

    /** Under the mutex. */
    private fun validate(readDevice: PairedDevice, lifecycleGeneration: Int) {
        if (state == DeviceState.removed) throw Failure.DeviceRemoved()
        if (state != DeviceState.paired || this.device != readDevice || this.lifecycleGeneration != lifecycleGeneration) {
            throw Failure.NotPaired()
        }
    }

    // MARK: - Activity-owned objects

    private suspend fun activityObject(
        objectId: String,
        read: suspend (PairedDevice, String) -> CloudApiResult<CloudItem>,
    ): CloudItem {
        val (device, generation) = mutex.withLock { activeDevice() to lifecycleGeneration }
        mutex.withLock {
            activityObjects[objectId]?.let { return it }
            nextAttemptAllowedAt?.let { allowed ->
                if (allowed > now()) throw Failure.Throttled((allowed - now()) / 1000.0)
            }
        }
        val attempt = context(after = null)
        val result = try {
            read(device, attempt.value)
        } catch (e: Failure) {
            throw e
        } catch (e: IOException) {
            throw Failure.Offline()
        }
        return when (result) {
            is CloudApiResult.Success -> mutex.withLock {
                validate(device, generation)
                activityObjects[objectId] = result.value
                lastSuccessfulRead = now()
                result.value
            }
            is CloudApiResult.Failure -> {
                val status = result.status
                if (status == 429 || status == 503) {
                    val delay = mutex.withLock {
                        noteFailure(retryAfter = result.retryAfterSeconds)
                        result.retryAfterSeconds ?: pendingDelay()
                    }
                    throw Failure.Throttled(delay)
                }
                if (status != 404) throw Failure.Server(status)
                val renewed = context(after = attempt.generation)
                val retry = try {
                    read(device, renewed.value)
                } catch (e: Failure) {
                    throw e
                } catch (e: IOException) {
                    throw Failure.Offline()
                }
                when (retry) {
                    is CloudApiResult.Success -> mutex.withLock {
                        validate(device, generation)
                        activityObjects[objectId] = retry.value
                        lastSuccessfulRead = now()
                        retry.value
                    }
                    is CloudApiResult.Failure -> throw Failure.Server(retry.status)
                }
            }
        }
    }

    // MARK: - Reading a collection

    private suspend fun reconcile(
        route: CloudRoute,
        cached: CachedCollection?,
        device: PairedDevice,
        lifecycleGeneration: Int,
    ): CloudSnapshot {
        mutex.withLock {
            nextAttemptAllowedAt?.let { allowed ->
                if (allowed > now()) throw Failure.Throttled((allowed - now()) / 1000.0)
            }
        }
        // A delta is asked for only where the route serves one AND there is
        // something to be a delta from.
        val since = if (route.servesDeltas) cached?.revision else null
        var cursor: String? = null
        val received = ArrayList<CloudItem>()
        var revision = since ?: 0
        var pages = 0
        do {
            val response = page(route, device, since, cursor)
            mutex.withLock { validate(device, lifecycleGeneration) }
            received.addAll(response.items)
            response.revision?.let { revision = max(revision, it) }
            cursor = response.nextCursor
            pages += 1
        } while (cursor != null && pages < maximumPages)

        val complete = cursor == null
        val merged = merge(received, if (since == null) null else cached)
        val now = now()
        if (complete) {
            mutex.withLock { validate(device, lifecycleGeneration) }
            // The write carries the identity this read ran under, so the cache
            // refuses it if a removal or re-pairing committed in the meantime.
            onIo { cache.store(received, revision, route, full = since == null, generation = lifecycleGeneration.toLong()) }
        }
        return CloudSnapshot(
            route = route,
            revision = if (complete) revision else (cached?.revision ?: revision),
            items = merged,
            source = CloudSnapshot.Source.network,
            asOf = now,
        )
    }

    /**
     * One page, with exactly one forced refresh if the reader context was the
     * problem. This path deliberately never counts toward revocation: only the
     * signed refresh, which proves possession of the device key, is allowed to
     * decide that this device is gone.
     */
    private suspend fun page(
        route: CloudRoute,
        device: PairedDevice,
        since: Int?,
        cursor: String?,
    ): CollectionResponse {
        val attempt = context(after = null)
        val result = try {
            client.collection(route, attempt.value, device, since, cursor)
        } catch (e: Failure) {
            throw e
        } catch (e: IOException) {
            throw Failure.Offline()
        }
        return when (result) {
            is CloudApiResult.Success -> result.value
            is CloudApiResult.Failure -> {
                val status = result.status
                if (status == 429 || status == 503) {
                    val delay = mutex.withLock {
                        noteFailure(retryAfter = result.retryAfterSeconds)
                        result.retryAfterSeconds ?: pendingDelay()
                    }
                    throw Failure.Throttled(delay)
                }
                if (status != 404) throw Failure.Server(status)
                val renewed = context(after = attempt.generation)
                val retry = try {
                    client.collection(route, renewed.value, device, since, cursor)
                } catch (e: Failure) {
                    throw e
                } catch (e: IOException) {
                    throw Failure.Offline()
                }
                when (retry) {
                    is CloudApiResult.Success -> retry.value
                    is CloudApiResult.Failure -> throw Failure.Server(retry.status)
                }
            }
        }
    }

    /**
     * Run a blocking store access (Room, EncryptedSharedPreferences) off the
     * caller's thread. The session is suspend end to end, so a screen on the
     * main thread never blocks a database or crypto call; only the network
     * also does this (in the transport), and the state mutex is never held
     * across it.
     */
    private suspend fun <T> onIo(block: () -> T): T = withContext(Dispatchers.IO) { block() }

    private fun now(): Long = clock()

    companion object {
        const val defaultContextLifetime: Double = 300.0
        const val refreshAhead: Double = 60.0
        const val minimumUsableLifetime: Double = 15.0
        const val maximumContextLifetime: Double = 300.0
        const val clockSkewTolerance: Double = 240.0
        const val baseBackoff: Double = 30.0
        const val maximumBackoff: Double = 300.0
        /** Upper bound on any backoff, so `delay * 1000` can never overflow. */
        const val maximumBackoffCeiling: Double = 86_400.0
        const val rejectionsBeforeRemoval: Int = 2
        const val maximumPages: Int = 50
    }
}
