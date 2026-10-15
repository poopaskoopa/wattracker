package com.wattracker.android.screens.activities

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.wattracker.android.cloud.CloudRoute
import com.wattracker.android.cloud.CloudSession
import com.wattracker.android.cloud.CloudSnapshot
import com.wattracker.android.cloud.LocalClientException
import com.wattracker.android.cloud.ReadSession
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/**
 * UI state for the Activities screen: the ride list and the selected ride.
 */
data class ActivitiesUiState(
    val isStarting: Boolean = true,
    val deviceState: CloudSession.DeviceState = CloudSession.DeviceState.unpaired,
    val isPaired: Boolean = false,
    val isLoading: Boolean = false,
    val isRefreshing: Boolean = false,
    val isWaking: Boolean = false,
    val throttledRetrySeconds: Int? = null,
    val error: String? = null,
    val rides: List<RideSummary> = emptyList(),
    /** Where the rides came from, for the "Offline" badge; null before the first paint. */
    val source: CloudSnapshot.Source? = null,
    /** Epoch millis of the snapshot on screen, for the "updated" line. */
    val asOf: Long = 0L,
    val selectedId: String? = null,
) {
    val selectedRide: RideSummary?
        get() = rides.firstOrNull { it.id == selectedId }
}

/**
 * The ride list, newest first, against the active backend.
 *
 * Mirrors the dashboard's view model and the iOS `ActivitiesModel`: it does
 * not load on construction (the screen refreshes on `ON_RESUME`, which also
 * arrives for the first load), the cache paints before the network, a
 * changed identity (pair, sign-out, removal) clears the previous rider's
 * rides and supersedes a load in flight, and a same-identity resume joins
 * the walk in flight rather than restarting it from page one.
 */
class ActivitiesViewModel(
    private val readModel: ReadSession,
    private val coroutineScope: CoroutineScope? = null,
) : ViewModel() {

    private val scope: CoroutineScope
        get() = coroutineScope ?: viewModelScope

    private val _uiState = MutableStateFlow(ActivitiesUiState())
    val uiState: StateFlow<ActivitiesUiState> = _uiState.asStateFlow()

    private var loadJob: Job? = null
    /** The identity the in-flight load started under; `null` when there is none. */
    private var loadIdentity: Int? = null
    /** The identity whose rides are on screen; a re-pair clears the previous rider's list. */
    private var shownIdentity: Int? = null

    /** Select a ride (the two-pane layout taps rows; the list pane navigates). */
    fun select(id: String) {
        _uiState.update { it.copy(selectedId = id) }
    }

    /**
     * Reload from the active backend, re-reading the pairing state first.
     *
     * A refresh under a changed identity cancels the load in flight: the
     * rider may have paired, signed out or re-paired in Settings since it
     * started, and only the newest load has read that. A resume under the
     * same identity -- a rotation, a tab switch, a return from Settings that
     * changed nothing -- joins the walk in flight instead of restarting it.
     */
    fun refresh() {
        val identity = readModel.identity
        val inFlight = loadJob
        if (inFlight?.isActive == true && loadIdentity == identity) {
            return
        }
        inFlight?.cancel()
        loadIdentity = identity
        loadJob = scope.launch { load() }
    }

    /**
     * The rider pulled to refresh: lift a gate a waking server set, then
     * restart the walk -- the rider asked for fresh data, so joining an
     * in-flight one would not answer it.
     */
    fun pullToRefresh() {
        loadJob?.cancel()
        loadIdentity = readModel.identity
        _uiState.update { it.copy(isRefreshing = true) }
        loadJob = scope.launch {
            // The desktop never scales to zero; only the cloud can be waking,
            // and only a cold-start gate may be lifted.
            if (readModel.mayBeWaking) readModel.retryNowIfWaking()
            load()
        }
    }

    private suspend fun load() {
        val deviceState = readModel.deviceState
        val isPaired = readModel.isPaired
        val identity = readModel.identity
        _uiState.update {
            it.copy(isStarting = false, deviceState = deviceState, isPaired = isPaired)
        }

        // A signed-out or removed rider leaves nothing of their rides behind:
        // keeping the last snapshot would render the departed rider's list.
        if (!isPaired || deviceState == CloudSession.DeviceState.removed) {
            shownIdentity = null
            _uiState.update {
                it.copy(
                    isLoading = false,
                    isRefreshing = false,
                    isWaking = false,
                    throttledRetrySeconds = null,
                    error = null,
                    rides = emptyList(),
                    source = null,
                    asOf = 0L,
                    selectedId = null,
                )
            }
            return
        }

        // A re-pair in Settings replaces the identity: the rides on screen
        // are the previous rider's. Clear them now, so a first load that
        // fails does not leave the old list up under "Syncing".
        if (shownIdentity != identity) {
            shownIdentity = null
            _uiState.update { it.copy(rides = emptyList(), source = null, asOf = 0L, selectedId = null) }
        }

        // 1. The cache first, so the network is not on the first-paint path.
        val cached = cachedOrNull(CloudRoute.Activities)
        if (cached != null) {
            val rides = ridesFrom(cached)
            checkActive()
            // The identity may have changed since the top of this load -- a
            // sign-out or re-pair whose disk wipe has not finished. Do not
            // paint the departed identity's rows; the network step below
            // finds the change and clears the screen.
            if (readModel.identity == identity) {
                shownIdentity = identity
                _uiState.update { it.copy(rides = rides, source = cached.source, asOf = cached.asOf) }
                syncSelection(rides)
            }
        }

        // 2. Always the network: it refreshes the list and it is how a
        // revocation is discovered.
        _uiState.update {
            it.copy(isLoading = true, error = null, throttledRetrySeconds = null, isWaking = false)
        }

        val fresh = try {
            readModel.load(CloudRoute.Activities)
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            checkActive()
            showFailure(e)
            return
        }
        checkActive()
        val rides = ridesFrom(fresh)
        shownIdentity = identity
        _uiState.update {
            it.copy(
                isLoading = false,
                isRefreshing = false,
                rides = rides,
                source = fresh.source,
                asOf = fresh.asOf,
                error = null,
            )
        }
        syncSelection(rides)
    }

    /** The rides to show from a snapshot, newest first, tombstones absent. */
    private fun ridesFrom(snapshot: CloudSnapshot): List<RideSummary> =
        snapshot.items
            .mapNotNull { RideSummary.from(it) }
            .sortedWith(RideSummary.newestFirst)

    /**
     * Keep the selection when it still exists, otherwise select the newest
     * ride -- the iOS `.onChange(of: rides)` rule.
     */
    private fun syncSelection(rides: List<RideSummary>) {
        _uiState.update { state ->
            val stillThere = state.selectedId != null && rides.any { it.id == state.selectedId }
            state.copy(selectedId = if (stillThere) state.selectedId else rides.firstOrNull()?.id)
        }
    }

    private suspend fun cachedOrNull(route: CloudRoute) = try {
        readModel.cached(route)
    } catch (e: CancellationException) {
        throw e
    } catch (_: Exception) {
        null
    }

    /**
     * Stop here if this load was superseded. `CloudSession.load` serves the
     * cache when its network step fails, and that includes being cancelled,
     * so a cancelled load can still return normally; this is what keeps its
     * stale result off the screen.
     */
    private suspend fun checkActive() = currentCoroutineContext().ensureActive()

    private fun showFailure(e: Exception) {
        val state = readModel.deviceState
        val removed = e is CloudSession.Failure.DeviceRemoved ||
            e is LocalClientException.Unauthorized ||
            state == CloudSession.DeviceState.removed
        val notPaired = e is CloudSession.Failure.NotPaired ||
            state == CloudSession.DeviceState.unpaired
        when {
            removed -> _uiState.update {
                it.copy(
                    isLoading = false,
                    isRefreshing = false,
                    isWaking = false,
                    deviceState = CloudSession.DeviceState.removed,
                    isPaired = false,
                    rides = emptyList(),
                    source = null,
                    asOf = 0L,
                    selectedId = null,
                    error = null,
                )
            }

            notPaired -> _uiState.update {
                it.copy(
                    isLoading = false,
                    isRefreshing = false,
                    isWaking = false,
                    deviceState = CloudSession.DeviceState.unpaired,
                    isPaired = false,
                    rides = emptyList(),
                    source = null,
                    asOf = 0L,
                    selectedId = null,
                    error = null,
                )
            }

            else -> {
                // An ordinary failure (offline, throttled, waking) leaves the
                // cached rides on screen when there are any; only the screen
                // chrome changes.
                val waking = e is CloudSession.Failure.Waking
                // The local backend's 429/503 carries `Retry-After` on its own
                // exception; it earns the same rate-limit notice the cloud's
                // throttle does.
                val localThrottled = (e as? LocalClientException.Http)
                    ?.takeIf { it.status == 429 || it.status == 503 }
                val retrySec = (e as? CloudSession.Failure.Throttled)?.retryAfter?.toInt()
                    ?: localThrottled?.retryAfter?.toInt()
                _uiState.update {
                    it.copy(
                        isLoading = false,
                        isRefreshing = false,
                        isWaking = waking,
                        throttledRetrySeconds = retrySec,
                        // A Waking failure has its own localized notice in the
                        // screen; leave error null and let the screen show it.
                        error = if (waking || retrySec != null) null else e.message,
                    )
                }
            }
        }
    }
}
