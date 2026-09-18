package com.wattracker.android.cloud

import com.wattracker.android.json.JsonValue
import com.wattracker.android.json.toJson
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.delay
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.IOException
import java.security.KeyPairGenerator
import java.security.spec.ECGenParameterSpec

/**
 * The state machine against scripted responses, no network.
 *
 * These are the tests #194 lives or dies on: that a single 404 (a deployment
 * mid-restart) does not unpair a working device, that two do, that a skewed
 * clock is never mistaken for revocation, that offline reads fall back to
 * last-known data instead of an error, and that five readers share one
 * refresh. The session is a set of suspend functions, so each case runs in
 * [runTest].
 */
class CloudSessionTest {

    private val signingNamespace = "ab".repeat(32)

    private val paired = PairedDevice(
        credentialId = "cred-1",
        signingNamespace = signingNamespace,
        subscriptionKey = "sub-1",
        capabilities = listOf("read"),
    )

    private lateinit var signer: DeviceSigner
    private var nowMillis = 1_700_000_000_000L

    private lateinit var store: InMemoryDeviceCredentialStore
    private lateinit var cache: InMemorySnapshotCache
    private lateinit var transport: FakeTransport
    private lateinit var removalGate: InMemoryRemovalGate
    private lateinit var session: CloudSession

    @Before
    fun setUp() {
        val keyPair = KeyPairGenerator.getInstance("EC").apply {
            initialize(ECGenParameterSpec("secp256r1"))
        }.generateKeyPair()
        signer = KeyPairSigner(keyPair, hardwareBacked = false)
    }

    private fun makeSession(prePaired: PairedDevice? = null, responder: suspend (CloudRequest) -> CloudResponse) {
        // The session reads the store exactly once, in init -- so the credential
        // must already be there for a paired session, the way a prior launch
        // would have left it.
        val credentialStore = InMemoryDeviceCredentialStore()
        if (prePaired != null) credentialStore.save(prePaired)
        store = credentialStore
        cache = InMemorySnapshotCache({ nowMillis })
        transport = FakeTransport(responder)
        removalGate = InMemoryRemovalGate()
        val client = CloudClient("http", "host", signer, transport) { nowMillis / 1000 }
        session = CloudSession(
            client = client,
            credentials = credentialStore,
            cache = cache,
            removalGate = removalGate,
            clock = { nowMillis },
            random = { 0.5 },
        )
    }

    private fun makePairedSession(responder: suspend (CloudRequest) -> CloudResponse) {
        makeSession(prePaired = paired, responder = responder)
    }

    // MARK: - Pairing

    @Test
    fun pairingMintsATokenAndClearsAnyPriorScope() = runTest {
        makeSession { request ->
            if (request.url.endsWith("/devices/pair")) {
                CloudResponse(200, pairingJson().toByteArray(), null, nowMillis)
            } else {
                throw AssertionError("unexpected request ${request.url}")
            }
        }
        // A prior launch stored it under its own fresh identity (a restart
        // starts at generation 0); a seed above the session's identity would
        // make the session's own later writes refused, not a prior launch.
        cache.store(listOf(profileItem(9)), 9, CloudRoute.Dashboard, full = true, generation = 0)
        val device = session.pair("THE-CODE", "bike phone")
        assertEquals("cred-1", device.credentialId)
        assertEquals(CloudSession.DeviceState.paired, session.deviceState)
        // A new identity starts from nothing: the prior scope's checkpoint is gone.
        assertNull(cache.load(CloudRoute.Dashboard))
    }

    @Test
    fun pairingClearsPendingRemovalGate() = runTest {
        makeSession { request ->
            if (request.url.endsWith("/devices/pair")) {
                CloudResponse(200, pairingJson().toByteArray(), null, nowMillis)
            } else {
                throw AssertionError("unexpected request ${request.url}")
            }
        }
        removalGate.markPending()
        assertTrue(removalGate.isPending())
        session.pair("THE-CODE", "bike phone")
        assertFalse(removalGate.isPending())
    }

    @Test
    fun pairingWithZeroExpiresInUsesDefaultLifetime() = runTest {
        makeSession { request ->
            if (request.url.endsWith("/devices/pair")) {
                CloudResponse(200, pairingJson(expiresIn = 0.0).toByteArray(), null, nowMillis)
            } else {
                throw AssertionError("unexpected request ${request.url}")
            }
        }
        session.pair("THE-CODE", "bike phone")
        assertEquals(CloudSession.DeviceState.paired, session.deviceState)
    }

    // MARK: - Reads

    @Test
    fun aColdStartReadsThroughARefresh() = runTest {
        makePairedSession { request ->
            when {
                request.url.contains("/context/refresh") ->
                    CloudResponse(200, refreshJson("ctx-1", 300.0).toByteArray(), null, nowMillis)
                request.url.contains("/context/dashboard") ->
                    CloudResponse(200, collectionJson(listOf(profileItem(1)), 1, null).toByteArray(), null, null)
                else -> throw AssertionError("unexpected ${request.url}")
            }
        }
        val snapshot = session.load(CloudRoute.Dashboard)
        assertEquals(CloudSnapshot.Source.network, snapshot.source)
        assertEquals(1, snapshot.revision)
        assertEquals(1, snapshot.items.size)
        // The read stored the reconciled collection as the new checkpoint.
        assertEquals(1, cache.load(CloudRoute.Dashboard)?.revision)
    }

    @Test
    fun aFailedReadServesLastKnownData() = runTest {
        makePairedSession { request ->
            when {
                request.url.contains("/context/refresh") ->
                    CloudResponse(200, refreshJson("ctx-1", 300.0).toByteArray(), null, nowMillis)
                request.url.contains("/context/dashboard") ->
                    CloudResponse(500, ByteArray(0), null, null)
                else -> throw AssertionError("unexpected ${request.url}")
            }
        }
        // A prior launch stored it under its own fresh identity (a restart
        // starts at generation 0).
        cache.store(listOf(profileItem(7)), 7, CloudRoute.Dashboard, full = true, generation = 0)
        val snapshot = session.load(CloudRoute.Dashboard)
        assertEquals(CloudSnapshot.Source.cache, snapshot.source)
        assertEquals(7, snapshot.revision)
    }

    @Test
    fun aFailedReadWithNoCacheThrows() = runTest {
        makePairedSession { request ->
            if (request.url.contains("/context/refresh")) {
                CloudResponse(200, refreshJson("ctx-1", 300.0).toByteArray(), null, nowMillis)
            } else {
                CloudResponse(500, ByteArray(0), null, null)
            }
        }
        expectFailure<CloudSession.Failure.Server> { session.load(CloudRoute.Dashboard) }
    }

    @Test
    fun aTransportErrorIsOfflineAndServesTheCache() = runTest {
        makePairedSession { request ->
            if (request.url.contains("/context/refresh")) {
                throw IOException("no route to host")
            }
            CloudResponse(200, ByteArray(0), null, nowMillis)
        }
        // A prior launch stored it under its own fresh identity (a restart
        // starts at generation 0).
        cache.store(listOf(profileItem(4)), 4, CloudRoute.Curve, full = true, generation = 0)
        val snapshot = session.load(CloudRoute.Curve)
        assertEquals(CloudSnapshot.Source.cache, snapshot.source)
        // And it did not move the device toward removal.
        assertEquals(CloudSession.DeviceState.paired, session.deviceState)
    }

    @Test
    fun aRead404IsRetriedOnceAgainstAFreshContext() = runTest {
        var firstCollectionServed = false
        var refreshCount = 0
        makePairedSession { request ->
            when {
                request.url.contains("/context/refresh") -> {
                    refreshCount += 1
                    CloudResponse(200, refreshJson("ctx-$refreshCount", 300.0).toByteArray(), null, nowMillis)
                }
                request.url.contains("/context/volume") ->
                    if (!firstCollectionServed) {
                        firstCollectionServed = true
                        CloudResponse(404, ByteArray(0), null, nowMillis)
                    } else {
                        CloudResponse(200, collectionJson(listOf(profileItem(2)), 2, null).toByteArray(), null, null)
                    }
                else -> throw AssertionError("unexpected ${request.url}")
            }
        }
        val snapshot = session.load(CloudRoute.Volume)
        assertEquals(CloudSnapshot.Source.network, snapshot.source)
        assertEquals(1, snapshot.items.size)
        // Two refreshes: the cold-start mint, then the forced refresh the read
        // 404 demanded. That second refresh is the retry against a fresh context.
        assertEquals(2, refreshCount)
    }

    @Test
    fun fiveConcurrentReadersShareOneRefresh() = runTest {
        var refreshCount = 0
        makePairedSession { request ->
            when {
                request.url.contains("/context/refresh") -> {
                    refreshCount += 1
                    delay(10) // hold the refresh in flight so the others join it
                    CloudResponse(200, refreshJson("ctx-1", 300.0).toByteArray(), null, nowMillis)
                }
                request.url.contains("/context/dashboard") ->
                    CloudResponse(200, collectionJson(listOf(profileItem(1)), 1, null).toByteArray(), null, null)
                else -> throw AssertionError("unexpected ${request.url}")
            }
        }
        val jobs = (1..5).map { async { session.load(CloudRoute.Dashboard) } }
        val results = jobs.awaitAll()
        assertEquals(5, results.size)
        assertTrue(results.all { it.source == CloudSnapshot.Source.network })
        // One refresh served all five; the rest awaited the same deferred.
        assertEquals(1, refreshCount)
    }

    // MARK: - Removal: the sharpest decision

    @Test
    fun aSingle404DoesNotRemoveTheDevice() = runTest {
        makePairedSession { request ->
            if (request.url.contains("/context/refresh")) {
                // An in-sync 404: the server clock matches, so this is a real
                // rejection, not skew.
                CloudResponse(404, ByteArray(0), null, nowMillis)
            } else {
                CloudResponse(200, ByteArray(0), null, nowMillis)
            }
        }
        expectFailure<CloudSession.Failure.Server> { session.load(CloudRoute.Dashboard) }
        assertEquals(CloudSession.DeviceState.paired, session.deviceState)
        assertNotNull(store.load())
    }

    @Test
    fun twoInSync404sRemoveTheDeviceAndWipeEverything() = runTest {
        makePairedSession { request ->
            if (request.url.contains("/context/refresh")) {
                CloudResponse(404, ByteArray(0), null, nowMillis)
            } else {
                CloudResponse(200, ByteArray(0), null, nowMillis)
            }
        }
        // First strike.
        expectFailure<CloudSession.Failure.Server> { session.load(CloudRoute.Dashboard) }
        assertEquals(CloudSession.DeviceState.paired, session.deviceState)
        // Cross the backoff gate, then the second strike.
        nowMillis += 120_000
        expectFailure<CloudSession.Failure.DeviceRemoved> { session.load(CloudRoute.Dashboard) }
        assertEquals(CloudSession.DeviceState.removed, session.deviceState)
        assertNull(store.load())
        assertNull(cache.load(CloudRoute.Dashboard))
    }

    @Test
    fun removeDeviceWipesTheCredentialAndTheCache() = runTest {
        makePairedSession { request ->
            when {
                request.url.contains("/revoke") ->
                    CloudResponse(200, """{"revoked":true}""".toByteArray(), null, nowMillis)
                else -> throw AssertionError("unexpected ${request.url}")
            }
        }
        // A little rider data, so the wipe is observable on the cache. A prior
        // launch stored it under its own fresh identity (a restart starts at
        // generation 0).
        cache.store(listOf(profileItem(3)), 3, CloudRoute.Dashboard, full = true, generation = 0)
        session.removeDevice()
        assertEquals(CloudSession.DeviceState.unpaired, session.deviceState)
        // The disk is wiped, not just memory: the credential and the rider's
        // data are both gone, so the next launch is not paired again.
        assertNull(store.load())
        assertNull(cache.load(CloudRoute.Dashboard))
    }

    @Test
    fun aClockSkewIsNeverBankedTowardRemoval() = runTest {
        // One session, one set of counters. The responder starts skewed and then
        // flips in-sync: if a skewed 404 were (wrongly) banked, the first
        // in-sync 404 would already be the second strike and remove the device.
        var skewed = true
        makePairedSession { request ->
            if (request.url.contains("/context/refresh")) {
                CloudResponse(404, ByteArray(0), null, nowMillis + if (skewed) 5 * 60 * 1000 else 0)
            } else {
                CloudResponse(200, ByteArray(0), null, nowMillis)
            }
        }
        // Five minutes of skew: refused as clock skew, not counted.
        expectFailure<CloudSession.Failure.ClockSkew> { session.load(CloudRoute.Dashboard) }
        assertEquals(CloudSession.DeviceState.paired, session.deviceState)

        // In sync now. The first in-sync 404 must be a *Server* strike, not
        // removal -- proof the skewed sample was not banked.
        skewed = false
        nowMillis += 120_000
        expectFailure<CloudSession.Failure.Server> { session.load(CloudRoute.Dashboard) }
        assertEquals(CloudSession.DeviceState.paired, session.deviceState)

        // Only the second in-sync 404 removes.
        nowMillis += 120_000
        expectFailure<CloudSession.Failure.DeviceRemoved> { session.load(CloudRoute.Dashboard) }
        assertEquals(CloudSession.DeviceState.removed, session.deviceState)
    }

    @Test
    fun a404WithNoDateIsNeverBankedTowardRemoval() = runTest {
        // A 404 that arrives with no readable server Date cannot rule out skew,
        // so it is thrown away rather than banked -- and takes a working
        // device through the whole test without ever removing it.
        makePairedSession { request ->
            if (request.url.contains("/context/refresh")) {
                CloudResponse(404, ByteArray(0), null, null)
            } else {
                CloudResponse(200, ByteArray(0), null, nowMillis)
            }
        }
        repeat(5) {
            // A big jump each pass: the backoff grows with every failure, so a
            // fixed small advance would start landing on the throttled side.
            nowMillis += 600_000
            expectFailure<CloudSession.Failure.Server> { session.load(CloudRoute.Dashboard) }
        }
        assertEquals(CloudSession.DeviceState.paired, session.deviceState)
        assertNotNull(store.load())
    }

    // MARK: - Throttling

    @Test
    fun aRetryAfterIsHonouredOverAComputedBackoff() = runTest {
        makePairedSession { request ->
            if (request.url.contains("/context/refresh")) {
                CloudResponse(429, ByteArray(0), retryAfterSeconds = 120.0, nowMillis)
            } else {
                CloudResponse(200, ByteArray(0), null, nowMillis)
            }
        }
        val thrown = expectFailure<CloudSession.Failure.Throttled> { session.load(CloudRoute.Dashboard) }
        assertEquals(120.0, thrown.retryAfter, 1.0)
        // Inside the window, the very next attempt is refused without a request.
        nowMillis += 30_000
        val throttled = expectFailure<CloudSession.Failure.Throttled> { session.load(CloudRoute.Dashboard) }
        assertTrue(throttled.retryAfter <= 90.0)
    }

    @Test
    fun aRetryAfterOnOneRouteGatesAnotherRoute() = runTest {
        makePairedSession { request ->
            when {
                request.url.contains("/context/refresh") ->
                    CloudResponse(200, refreshJson("ctx-1", 300.0).toByteArray(), null, nowMillis)
                // A 429 on the read plane, not the refresh: this is the 429
                // branch in page(), the one a refresh-429 never exercises.
                request.url.contains("/context/dashboard") ->
                    CloudResponse(429, ByteArray(0), retryAfterSeconds = 120.0, nowMillis)
                else -> CloudResponse(200, ByteArray(0), null, nowMillis)
            }
        }
        // The Dashboard read hits the 429 and records the device-wide backoff.
        expectFailure<CloudSession.Failure.Throttled> { session.load(CloudRoute.Dashboard) }
        // The backoff is per-device, so a different route is gated the same way
        // -- and gated *before* it sends anything.
        nowMillis += 30_000
        expectFailure<CloudSession.Failure.Throttled> { session.load(CloudRoute.Curve) }
        assertTrue(transport.requests.none { it.url.contains("/context/curve") })
        assertEquals(CloudSession.DeviceState.paired, session.deviceState)
    }

    // MARK: - Multi-page walk

    @Test
    fun aPageWalkStopsAtTheEndAndStoresTheCheckpoint() = runTest {
        makePairedSession { request ->
            when {
                request.url.contains("/context/refresh") ->
                    CloudResponse(200, refreshJson("ctx-1", 300.0).toByteArray(), null, nowMillis)
                request.url.contains("cursor=c1") ->
                    CloudResponse(200, collectionJson(listOf(objectItem("a-2", 2)), 2, null).toByteArray(), null, null)
                request.url.contains("/context/activities") ->
                    CloudResponse(200, collectionJson(listOf(objectItem("a-1", 1)), 1, "c1").toByteArray(), null, null)
                else -> throw AssertionError("unexpected ${request.url}")
            }
        }
        val snapshot = session.load(CloudRoute.Activities)
        assertEquals(CloudSnapshot.Source.network, snapshot.source)
        assertEquals(2, snapshot.revision)
        assertEquals(2, snapshot.items.size)
        assertEquals(2, cache.load(CloudRoute.Activities)?.revision)
    }

    @Test
    fun aWalkCutOffAtThePageLimitDoesNotAdvanceTheCheckpoint() = runTest {
        // Every page carries a next cursor: the walk hits the page limit.
        makePairedSession { request ->
            when {
                request.url.contains("/context/refresh") ->
                    CloudResponse(200, refreshJson("ctx-1", 300.0).toByteArray(), null, nowMillis)
                else ->
                    CloudResponse(200, collectionJson(listOf(objectItem("x", 1)), 1, "next").toByteArray(), null, null)
            }
        }
        val snapshot = session.load(CloudRoute.Activities)
        assertEquals(CloudSnapshot.Source.network, snapshot.source)
        // A truncated walk must not write a checkpoint it did not earn.
        assertNull(cache.load(CloudRoute.Activities))
    }

    @Test
    fun aCollectionReadSendsTheBoundedPageLimit() = runTest {
        // The server's default page (100 objects at up to 512 KiB each)
        // can out-run the transport cap, and a truncated page fails on
        // every read, not just the first; the client bounds the page
        // itself.
        makePairedSession { request ->
            when {
                request.url.contains("/context/refresh") ->
                    CloudResponse(200, refreshJson("ctx-1", 300.0).toByteArray(), null, nowMillis)
                else ->
                    CloudResponse(200, collectionJson(listOf(profileItem(1)), 1, null).toByteArray(), null, null)
            }
        }
        session.load(CloudRoute.Dashboard)
        val request = transport.requests.first { it.url.contains("/context/dashboard") }
        assertTrue(
            "the page limit keeps a worst-case page under the transport cap",
            request.url.contains("limit=${CloudClient.COLLECTION_PAGE_LIMIT}"),
        )
    }

    @Test
    fun anUnpaginatedCollectionReadDoesNotSendALimit() = runTest {
        makePairedSession { request ->
            when {
                request.url.contains("/context/refresh") ->
                    CloudResponse(200, refreshJson("ctx-1", 300.0).toByteArray(), null, nowMillis)
                else ->
                    CloudResponse(200, collectionJson(listOf(profileItem(1)), 1, null).toByteArray(), null, null)
            }
        }
        session.load(CloudRoute.Calendar)
        val request = transport.requests.first { it.url.contains("/context/calendar") }
        assertFalse(
            "unpaginated routes must not send limit query parameter",
            request.url.contains("limit="),
        )
    }

    // MARK: - Restart: the cache must not out-veto a new process

    @Test
    fun aRestartedSessionWritesToThePersistentCache() = runTest {
        // The "database": rows that survive a process restart, shared by the
        // two cache instances. Each cache keeps its own in-memory identity
        // gate -- the shape of the Room cache, whose gate is process-local
        // while the rows are durable. This pins the contract a restarted
        // session must meet against that shape: the write lands, the
        // checkpoint advances, and the read asks a delta from the old
        // checkpoint. It does not run RoomSnapshotCache itself -- a JVM test
        // has no Context to open a file-backed Room database, and the
        // dependency rule bars Robolectric -- so it is the contract half of
        // the restart fix; the gate half is GenerationGateTest, and the
        // Room class is verified on device.
        val disk = HashMap<CloudRoute, CachedCollection>()
        val sinceSeen = mutableListOf<Int?>()
        val client = CloudClient(
            "http",
            "host",
            signer,
            transport = FakeTransport { request ->
                when {
                    request.url.contains("/devices/pair") ->
                        CloudResponse(200, pairingJson().toByteArray(), null, nowMillis)
                    request.url.contains("/context/refresh") ->
                        CloudResponse(200, refreshJson("ctx", 300.0).toByteArray(), null, nowMillis)
                    request.url.contains("/context/dashboard") -> {
                        // One query parameter at a time: the read also
                        // carries the bounded page limit.
                        val since = request.url.substringAfter("since=", missingDelimiterValue = "")
                            .substringBefore("&")
                            .takeIf { it.isNotEmpty() }?.toIntOrNull()
                        sinceSeen += since
                        if (since == null) {
                            // The first launch's full read.
                            CloudResponse(200, collectionJson(listOf(profileItem(5)), 5, null).toByteArray(), null, null)
                        } else {
                            // The restarted launch's delta from the stored checkpoint.
                            CloudResponse(200, collectionJson(listOf(profileItem(8)), 8, null).toByteArray(), null, null)
                        }
                    }
                    else -> throw AssertionError("unexpected ${request.url}")
                }
            },
            clock = { nowMillis / 1000 },
        )

        // First launch: pair, read, store.
        val firstStore = InMemoryDeviceCredentialStore()
        val first = CloudSession(
            client = client,
            credentials = firstStore,
            cache = InMemorySnapshotCache({ nowMillis }, disk),
            clock = { nowMillis },
            random = { 0.5 },
        )
        first.pair("THE-CODE", "bike phone")
        val firstRead = first.load(CloudRoute.Dashboard)
        assertEquals(CloudSnapshot.Source.network, firstRead.source)
        assertEquals(5, disk[CloudRoute.Dashboard]?.revision)

        // Restart: the credential and the rows are durable; the identity is
        // not. A fresh session starts a fresh generation against the same
        // rows -- and it must be able to write to them.
        val secondStore = InMemoryDeviceCredentialStore().apply { save(firstStore.load()!!) }
        val second = CloudSession(
            client = client,
            credentials = secondStore,
            cache = InMemorySnapshotCache({ nowMillis }, disk),
            clock = { nowMillis },
            random = { 0.5 },
        )
        val secondRead = second.load(CloudRoute.Dashboard)
        assertEquals(CloudSnapshot.Source.network, secondRead.source)
        // The write landed and the checkpoint advanced past the first launch's.
        assertEquals(8, secondRead.revision)
        assertEquals(8, disk[CloudRoute.Dashboard]?.revision)
        // And the second read asked for a delta from the first launch's
        // checkpoint, not a full refetch.
        assertEquals(listOf<Int?>(null, 5), sinceSeen)
    }

    // MARK: - The removal gate

    @Test
    fun anUnfinishedRemovalIsCompletedOnTheNextStart() {
        // A prior launch's removal wiped the cache, then the credential wipe
        // failed and left the gate pending with the credential still on disk.
        val gate = InMemoryRemovalGate()
        gate.markPending()
        val credentialStore = InMemoryDeviceCredentialStore().apply { save(paired) }
        val disk = HashMap<CloudRoute, CachedCollection>()
        val cache = InMemorySnapshotCache({ nowMillis }, disk)
        cache.store(listOf(profileItem(4)), 4, CloudRoute.Dashboard, full = true, generation = 0)

        // Constructing the session completes the removal and clears the flag.
        val client = noNetworkClient()
        val session = CloudSession(
            client = client,
            credentials = credentialStore,
            cache = cache,
            removalGate = gate,
            clock = { nowMillis },
            random = { 0.5 },
        )
        assertEquals(CloudSession.DeviceState.unpaired, session.deviceState)
        assertFalse(gate.isPending())
        assertNull(credentialStore.load())
        assertNull(cache.load(CloudRoute.Dashboard))
    }

    @Test
    fun aFailedRemovalRetryKeepsTheGatePending() {
        // The wipe fails again: the flag stays set so the start after that
        // retries, and the stale credential is not silently forgotten.
        val gate = InMemoryRemovalGate()
        gate.markPending()
        val credentialStore = object : DeviceCredentialStore {
            override fun load(): PairedDevice? = paired
            override fun save(device: PairedDevice) {}
            override fun clear() {
                throw IOException("disk full")
            }
        }
        val session = CloudSession(
            client = noNetworkClient(),
            credentials = credentialStore,
            cache = InMemorySnapshotCache(),
            removalGate = gate,
            clock = { nowMillis },
            random = { 0.5 },
        )
        assertTrue(gate.isPending())
        assertEquals(CloudSession.DeviceState.paired, session.deviceState)
    }

    /** A client that must never touch the network; these tests run init only. */
    private fun noNetworkClient(): CloudClient = CloudClient(
        "http",
        "host",
        signer,
        transport = FakeTransport { throw AssertionError("no network expected") },
        clock = { nowMillis / 1000 },
    )

    // MARK: - Secret hygiene

    @Test
    fun bearerSecretsDoNotAppearInToString() {
        val device = PairedDevice("cred-1", signingNamespace, "SECRET-SUB", listOf("read"), "bike")
        assertFalse(device.toString().contains("SECRET-SUB"))
        val local = LocalCredential("https://host", "SECRET-TOKEN")
        assertFalse(local.toString().contains("SECRET-TOKEN"))
        val pairing = PairingResult(device, "SECRET-CTX", 300.0)
        assertFalse(pairing.toString().contains("SECRET-CTX"))
        val refresh = RefreshOutcome("SECRET-CTX", 300.0, nowMillis)
        assertFalse(refresh.toString().contains("SECRET-CTX"))
    }

    // MARK: - JSON builders

    private fun pairingJson(expiresIn: Double = 300.0): String =
        """{"device_credential":"cred-1","device_subscription_key":"sub-1","device_signature_algorithm":"ecdsa-p256-sha256","device_capabilities":["read"],"signing_namespace":"$signingNamespace","reader_context":"ctx-1","expires_in":$expiresIn}"""

    private fun refreshJson(context: String, expiresIn: Double): String =
        """{"reader_context":"$context","expires_in":$expiresIn}"""

    private fun collectionJson(items: List<CloudItem>, revision: Int, nextCursor: String?): String {
        val itemsJson = items.joinToString(",") { it.toJson().toJson() }
        val cursorJson = nextCursor?.let { "\"$it\"" } ?: "null"
        return """{"items":[$itemsJson],"revision":$revision,"next_cursor":$cursorJson}"""
    }

    private fun profileItem(revision: Int): CloudItem = objectItem("profile", revision)

    private fun objectItem(id: String, revision: Int): CloudItem = CloudItem.fromJson(
        JsonValue.parse("""{"id":"$id","kind":"profile","revision":$revision,"data":{"ftp":250}}"""),
    )
}

/**
 * A suspend-aware `assertThrows`: [block] must fail with a [T]. JUnit's
 * `assertThrows` takes a non-suspend thunk, which cannot call the session's
 * suspend methods.
 */
private suspend inline fun <reified T : Throwable> expectFailure(crossinline block: suspend () -> Unit): T {
    try {
        block()
    } catch (e: Throwable) {
        if (e is T) return e
        throw e
    }
    throw AssertionError("expected ${T::class.java.simpleName}, but the call completed")
}

/** A transport that records every request and answers from a scripted function. */
private class FakeTransport(private val responder: suspend (CloudRequest) -> CloudResponse) : CloudTransport {
    val requests = mutableListOf<CloudRequest>()

    override suspend fun send(request: CloudRequest): CloudResponse {
        requests += request
        return responder(request)
    }
}
