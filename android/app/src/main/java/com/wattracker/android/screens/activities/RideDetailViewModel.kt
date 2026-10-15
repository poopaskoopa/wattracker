package com.wattracker.android.screens.activities

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.wattracker.android.cloud.ActivityDetail
import com.wattracker.android.cloud.ActivityStreams
import com.wattracker.android.cloud.ActivitySummary
import com.wattracker.android.cloud.CloudPayload
import com.wattracker.android.cloud.CloudRoute
import com.wattracker.android.cloud.CloudSession
import com.wattracker.android.cloud.LocalClientException
import com.wattracker.android.cloud.ReadSession
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.async
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/**
 * UI state for one ride's detail: the summary, the zone times, and the
 * bounded streams that feed the "Ride graphs" chart.
 */
data class RideDetailUiState(
    val isStarting: Boolean = true,
    val deviceState: CloudSession.DeviceState = CloudSession.DeviceState.unpaired,
    val isPaired: Boolean = false,
    val isLoading: Boolean = true,
    /** The list's summary for this ride, shown as soon as the cached list can supply it. */
    val summary: ActivitySummary? = null,
    val detail: ActivityDetail? = null,
    val streams: ActivityStreams? = null,
    /** The ride has no plottable streams: a 404, or an empty payload. */
    val noStreams: Boolean = false,
    val detailError: String? = null,
    val streamsError: String? = null,
) {
    /** The summary to render: the loaded detail, else the list's summary. */
    val shownSummary: ActivitySummary?
        get() = detail?.let {
            ActivitySummary(
                id = it.id,
                startTime = it.startTime,
                durationS = it.durationS,
                distanceM = it.distanceM,
                avgPower = it.avgPower,
                avgHr = it.avgHr,
                np = it.np,
                intensityFactor = it.intensityFactor,
                tss = it.tss,
                rpe = it.rpe,
            )
        } ?: summary
}

/**
 * Loads one ride's detail and streams on demand.
 *
 * Mirrors the iOS `ActivityDetailScreen`: the two reads go out in parallel
 * and fail independently (a detail error does not hide a chart, and a
 * streams 404 is the rider's "No recorded streams", not a failure). The
 * ride's streams arrive downsampled to 1,500 points as published -- this
 * view model renders what arrives and never requests raw or resamples.
 */
class RideDetailViewModel(
    private val readModel: ReadSession,
    private val activityId: Int,
    private val coroutineScope: CoroutineScope? = null,
) : ViewModel() {

    private val scope: CoroutineScope
        get() = coroutineScope ?: viewModelScope

    private val _uiState = MutableStateFlow(RideDetailUiState())
    val uiState: StateFlow<RideDetailUiState> = _uiState.asStateFlow()

    private var loading = false

    /** Load this ride; a second call while one is in flight is ignored. */
    fun load() {
        if (loading) return
        loading = true
        _uiState.update {
            it.copy(
                isLoading = true,
                detail = null,
                streams = null,
                noStreams = false,
                detailError = null,
                streamsError = null,
            )
        }
        scope.launch {
            val deviceState = readModel.deviceState
            val isPaired = readModel.isPaired
            _uiState.update {
                it.copy(isStarting = false, deviceState = deviceState, isPaired = isPaired)
            }
            if (!isPaired || deviceState == CloudSession.DeviceState.removed) {
                _uiState.update { it.copy(isLoading = false) }
                loading = false
                return@launch
            }

            // The list's summary, if the cached list has it: the header and
            // metric grid paint before the network answers.
            val cachedSummary = cachedSummary()
            if (cachedSummary != null) {
                _uiState.update { it.copy(summary = it.summary ?: cachedSummary) }
            }

            // Both reads in flight at once, each failing on its own.
            val detailResult = async { runCatching { readModel.activityDetail(activityId) } }.await()
            val streamsResult = async { runCatching { readModel.activityStreams(activityId) } }.await()

            detailResult.fold(
                onSuccess = { detail ->
                    _uiState.update { it.copy(detail = detail) }
                },
                onFailure = { e ->
                    _uiState.update { it.copy(detailError = e.message ?: e::class.simpleName) }
                },
            )

            streamsResult.fold(
                onSuccess = { streams ->
                    // A payload with no plottable channel says "no streams" the
                    // way a 404 does (a ride without any recorded channel).
                    if (StreamSeries.all(streams).isEmpty()) {
                        _uiState.update { it.copy(streams = null, noStreams = true) }
                    } else {
                        _uiState.update { it.copy(streams = streams) }
                    }
                },
                onFailure = { e ->
                    if (isNotFound(e)) {
                        _uiState.update { it.copy(streams = null, noStreams = true) }
                    } else {
                        _uiState.update { it.copy(streamsError = e.message ?: e::class.simpleName) }
                    }
                },
            )

            _uiState.update { it.copy(isLoading = false) }
            loading = false
        }
    }

    private fun isNotFound(e: Throwable): Boolean =
        (e is CloudSession.Failure.Server && e.status == 404) ||
            (e is LocalClientException.Http && e.status == 404)

    private suspend fun cachedSummary(): ActivitySummary? {
        val snapshot = try {
            readModel.cached(CloudRoute.Activities)
        } catch (_: Exception) {
            return null
        }
        return snapshot?.items
            ?.mapNotNull { (it.payload as? CloudPayload.Activity)?.value }
            ?.firstOrNull { it.id?.toInt() == activityId }
    }
}
