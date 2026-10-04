package com.wattracker.android.screens.dashboard

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.wattracker.android.cloud.CloudRoute
import com.wattracker.android.cloud.CloudSession
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
 * UI state for the Dashboard Screen.
 */
data class DashboardUiState(
    val isStarting: Boolean = true,
    val deviceState: CloudSession.DeviceState = CloudSession.DeviceState.unpaired,
    val isPaired: Boolean = false,
    val isLoading: Boolean = false,
    val isWaking: Boolean = false,
    val throttledRetrySeconds: Int? = null,
    val error: String? = null,
    val selectedWindow: LoadWindow = LoadWindow.SIX_WEEKS,
    val dashboardData: DashboardData? = null,
)

/**
 * ViewModel for the Dashboard screen.
 *
 * It does not load on construction. The screen calls [refresh] on every
 * `ON_RESUME`, and a lifecycle observer receives `ON_RESUME` as soon as it is
 * added, so that is the first load too. A load in `init` as well would start
 * two full walks of the dashboard route on every cold start.
 */
class DashboardViewModel(
    private val readModel: ReadSession,
    private val coroutineScope: CoroutineScope? = null,
) : ViewModel() {

    private val scope: CoroutineScope
        get() = coroutineScope ?: viewModelScope

    private val _uiState = MutableStateFlow(DashboardUiState())
    val uiState: StateFlow<DashboardUiState> = _uiState.asStateFlow()

    private var loadJob: Job? = null

    fun setWindow(window: LoadWindow) {
        _uiState.update { it.copy(selectedWindow = window) }
    }

    /**
     * Reload from the active backend, re-reading the pairing state first.
     *
     * A refresh cancels the one in flight rather than joining it: the rider
     * may have paired, signed out or re-paired in Settings since it started,
     * and only the newest load has read that. The cancelled load never writes
     * state again (see [checkActive]).
     */
    fun refresh() {
        loadJob?.cancel()
        loadJob = scope.launch { load() }
    }

    private suspend fun load() {
        val deviceState = readModel.deviceState
        val isPaired = readModel.isPaired
        _uiState.update {
            it.copy(isStarting = false, deviceState = deviceState, isPaired = isPaired)
        }

        if (!isPaired || deviceState == CloudSession.DeviceState.removed) {
            _uiState.update { it.copy(isLoading = false, dashboardData = null, error = null) }
            return
        }

        // 1. The cache first, so the network is not on the first-paint path.
        val cachedDashboard = cachedOrNull(CloudRoute.Dashboard)
        if (cachedDashboard != null) {
            var data = DashboardData.fromSnapshot(cachedDashboard)
            if (data.activities.isEmpty()) {
                val cachedActivities = cachedOrNull(CloudRoute.Activities)
                if (cachedActivities != null) {
                    data = data.copy(activities = DashboardData.activitiesFrom(cachedActivities))
                }
            }
            checkActive()
            _uiState.update { it.copy(dashboardData = data) }
        }

        // 2. Always the network: it refreshes the numbers and it is how a
        // revocation is discovered.
        _uiState.update {
            it.copy(isLoading = true, error = null, throttledRetrySeconds = null, isWaking = false)
        }

        val freshDashboard = try {
            readModel.load(CloudRoute.Dashboard)
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            checkActive()
            showFailure(e)
            return
        }
        checkActive()
        val fresh = DashboardData.fromSnapshot(freshDashboard)

        // The cloud dashboard route publishes profile, training_state,
        // load_point and curve only; recent rides come from the Activities
        // route. Whether to fetch them is decided on the fresh snapshot alone:
        // the rides kept on screen below must never suppress the fetch, or a
        // newly published ride would never reach the strip.
        val needsActivities = fresh.activities.isEmpty()
        val shown = if (needsActivities) {
            // Keep what is on screen until the new list arrives, so the strip
            // does not blink out and back during the fetch.
            fresh.copy(activities = _uiState.value.dashboardData?.activities.orEmpty())
        } else {
            fresh
        }
        _uiState.update { it.copy(isLoading = false, dashboardData = shown, error = null) }

        if (!needsActivities) return
        val activities = try {
            readModel.load(CloudRoute.Activities)
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            checkActive()
            // An ordinary failure leaves the kept rides in place; a removal or
            // sign-out found here clears the screen like one found above.
            if (isLifecycleFailure(e)) showFailure(e)
            return
        }
        checkActive()
        _uiState.update {
            it.copy(dashboardData = it.dashboardData?.copy(activities = DashboardData.activitiesFrom(activities)))
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

    private fun isLifecycleFailure(e: Exception): Boolean =
        e is CloudSession.Failure.DeviceRemoved ||
            e is CloudSession.Failure.NotPaired ||
            e is LocalClientException.Unauthorized

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
                    isWaking = false,
                    deviceState = CloudSession.DeviceState.removed,
                    isPaired = false,
                    dashboardData = null,
                    error = null,
                )
            }
            notPaired -> _uiState.update {
                it.copy(
                    isLoading = false,
                    isWaking = false,
                    deviceState = CloudSession.DeviceState.unpaired,
                    isPaired = false,
                    dashboardData = null,
                    error = null,
                )
            }
            else -> {
                val waking = e is CloudSession.Failure.Waking
                val retrySec = (e as? CloudSession.Failure.Throttled)?.retryAfter?.toInt()
                _uiState.update {
                    it.copy(
                        isLoading = false,
                        isWaking = waking,
                        throttledRetrySeconds = retrySec,
                        error = if (retrySec == null) e.message else null,
                    )
                }
            }
        }
    }
}
