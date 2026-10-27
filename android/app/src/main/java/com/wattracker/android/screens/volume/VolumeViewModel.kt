package com.wattracker.android.screens.volume

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
 * UI state for the Volume screen: the weekly totals and the window and
 * metric the rider picked.
 */
data class VolumeUiState(
    val isStarting: Boolean = true,
    val deviceState: CloudSession.DeviceState = CloudSession.DeviceState.unpaired,
    val isPaired: Boolean = false,
    val isLoading: Boolean = false,
    val isRefreshing: Boolean = false,
    val isWaking: Boolean = false,
    val throttledRetrySeconds: Int? = null,
    val error: String? = null,
    /** The weeks read, dense-filled; null is the honest "nothing to show" state. */
    val data: VolumeData? = null,
    val selectedMetric: VolumeMetric = VolumeMetric.Hours,
    val selectedRange: VolumeRange = VolumeRange.Last12,
)

/**
 * The weekly volume, against the active backend.
 *
 * Mirrors the iOS `VolumeScreenModel` and the other screen view models:
 * cache paints before the network, a changed identity (pair, sign-out,
 * removal) clears the previous rider's weeks and supersedes the load in
 * flight, and a same-identity resume joins the walk in flight. Metric and
 * range are views over data already read -- selecting one re-renders and
 * never re-reads, which is why the chart's windows cost no requests.
 */
class VolumeViewModel(
    private val readModel: ReadSession,
    private val coroutineScope: CoroutineScope? = null,
) : ViewModel() {

    private val scope: CoroutineScope
        get() = coroutineScope ?: viewModelScope

    private val _uiState = MutableStateFlow(VolumeUiState())
    val uiState: StateFlow<VolumeUiState> = _uiState.asStateFlow()

    private var loadJob: Job? = null
    /** The identity the in-flight load started under; `null` when there is none. */
    private var loadIdentity: Int? = null
    /** The identity whose weeks are on screen; a re-pair clears the previous rider's. */
    private var shownIdentity: Int? = null

    fun selectMetric(metric: VolumeMetric) {
        _uiState.update { it.copy(selectedMetric = metric) }
    }

    fun selectRange(range: VolumeRange) {
        _uiState.update { it.copy(selectedRange = range) }
    }

    /**
     * Reload from the active backend, re-reading the pairing state first.
     * A changed identity cancels the load in flight; a same-identity resume
     * joins it -- the same rule the activities list runs on.
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

    /** The rider pulled to refresh: lift a waking gate, then re-read. */
    fun pullToRefresh() {
        loadJob?.cancel()
        loadIdentity = readModel.identity
        _uiState.update { it.copy(isRefreshing = true) }
        loadJob = scope.launch {
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

        if (!isPaired || deviceState == CloudSession.DeviceState.removed) {
            shownIdentity = null
            _uiState.update {
                it.copy(
                    isLoading = false,
                    isRefreshing = false,
                    isWaking = false,
                    throttledRetrySeconds = null,
                    error = null,
                    data = null,
                )
            }
            return
        }

        if (shownIdentity != identity) {
            shownIdentity = null
            _uiState.update { it.copy(data = null) }
        }

        // 1. The cache first, so the network is not on the first-paint path.
        val cached = cachedOrNull(CloudRoute.Volume)
        if (cached != null) {
            checkActive()
            if (readModel.identity == identity) {
                shownIdentity = identity
                apply(cached)
            }
        }

        // 2. Always the network: it refreshes the weeks and it is how a
        // revocation is discovered.
        _uiState.update {
            it.copy(isLoading = true, error = null, throttledRetrySeconds = null, isWaking = false)
        }

        val fresh = try {
            readModel.load(CloudRoute.Volume)
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            checkActive()
            showFailure(e)
            return
        }
        checkActive()
        shownIdentity = identity
        apply(fresh)
        _uiState.update { it.copy(isLoading = false, isRefreshing = false, error = null) }
    }

    /**
     * Paint one snapshot. A snapshot with no weeks at all is the "no volume
     * data yet" state, not an empty chart: the rider must be able to tell
     * "nothing published" from "no weeks in this window".
     */
    private fun apply(snapshot: CloudSnapshot) {
        val data = VolumeData(snapshot)
        _uiState.update { it.copy(data = if (data.weeks.isEmpty()) null else data) }
    }

    private suspend fun cachedOrNull(route: CloudRoute) = try {
        readModel.cached(route)
    } catch (e: CancellationException) {
        throw e
    } catch (_: Exception) {
        null
    }

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
                    data = null,
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
                    data = null,
                    error = null,
                )
            }

            else -> {
                // An ordinary failure (offline, throttled, waking) leaves the
                // cached weeks on screen when there are any.
                val waking = e is CloudSession.Failure.Waking
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
                        // Same convention as the other two screens: the
                        // failure carries its own rider-facing sentence.
                        error = if (waking || retrySec != null) null else e.message,
                    )
                }
            }
        }
    }
}
