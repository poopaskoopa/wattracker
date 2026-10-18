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
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.async
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
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
    /** A waking cloud refused the read: the screen shows the waking notice. */
    val isWaking: Boolean = false,
    /** A rate-limited read: the screen shows the retry notice. */
    val throttledRetrySeconds: Int? = null,
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
 * One instance per screen scope: in the two-pane layout it serves
 * whichever ride the list has selected, and in the pushed screen it serves
 * that screen's ride, so a ride's detail and streams never outlive the
 * pane that is showing them.
 *
 * Mirrors the iOS `ActivityDetailScreen`: the detail and the streams go
 * out in parallel and fail independently (a detail error does not hide a
 * chart, and a streams 404 is the rider's "No recorded streams", not a
 * failure). The ride's streams arrive downsampled to 1,500 points as
 * published -- this view model renders what arrives and never requests raw
 * or resamples.
 */
class RideDetailViewModel(
    private val readModel: ReadSession,
    private val coroutineScope: CoroutineScope? = null,
) : ViewModel() {

    private val scope: CoroutineScope
        get() = coroutineScope ?: viewModelScope

    private val _uiState = MutableStateFlow(RideDetailUiState())
    val uiState: StateFlow<RideDetailUiState> = _uiState.asStateFlow()

    private var loadJob: Job? = null
    /** The ride the in-flight (or last) load ran for. */
    private var loadedId: Int? = null

    /**
     * Load [activityId]. A load of the same ride while one is in flight is
     * ignored; a different ride supersedes the walk in flight, because its
     * result belongs to the ride that was just left.
     */
    fun load(activityId: Int) {
        if (loadedId == activityId && loadJob?.isActive == true) return
        loadJob?.cancel()
        loadedId = activityId
        _uiState.update {
            it.copy(
                isLoading = true,
                summary = null,
                detail = null,
                streams = null,
                noStreams = false,
                isWaking = false,
                throttledRetrySeconds = null,
                detailError = null,
                streamsError = null,
            )
        }
        loadJob = scope.launch {
            try {
                doLoad(activityId)
            } finally {
                // A superseded load (the ride was left) must not clear the
                // flag the new load just set.
                if (loadedId == activityId) {
                    _uiState.update {
                        if (it.isLoading) it.copy(isLoading = false) else it
                    }
                }
            }
        }
    }

    /** Re-run the read for the ride currently loaded (a resume). */
    fun reload() {
        val id = loadedId ?: return
        if (loadJob?.isActive == true) return // join the walk in flight
        load(id)
    }

    /**
     * Drop the ride data while the pane is not visible: a 1,500-point
     * stream per channel is large, the session's bounded cache still holds
     * it, and an off-screen pane keeps it for nothing.
     */
    fun release() {
        _uiState.update { it.copy(summary = null, detail = null, streams = null) }
    }

    private suspend fun doLoad(activityId: Int) {
        val deviceState = readModel.deviceState
        val isPaired = readModel.isPaired
        _uiState.update { it.copy(isStarting = false, deviceState = deviceState, isPaired = isPaired) }
        if (!isPaired || deviceState == CloudSession.DeviceState.removed) {
            return
        }

        // The list's summary, if the cached list has it: the header and
        // metric grid paint before the network answers.
        val cachedSummary = cachedSummary(activityId)
        if (cachedSummary != null) {
            _uiState.update { it.copy(summary = cachedSummary) }
        }

        // Both reads in flight at once, each failing on its own.
        val detailDeferred = scope.async {
            readResult { readModel.activityDetail(activityId) }
        }
        val streamsDeferred = scope.async {
            readResult { readModel.activityStreams(activityId) }
        }
        val detailResult = detailDeferred.await()
        val streamsResult = streamsDeferred.await()

        // A superseded load must not write its result over the new ride's.
        currentCoroutineContext().ensureActive()

        detailResult.fold(
            onSuccess = { detail ->
                _uiState.update { it.copy(detail = detail) }
            },
            onFailure = { e ->
                applyFailure(e, forStreams = false)
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
                when {
                    isNotFound(e) ->
                        _uiState.update { it.copy(streams = null, noStreams = true) }
                    else ->
                        applyFailure(e, forStreams = true)
                }
            },
        )

        currentCoroutineContext().ensureActive()
        _uiState.update { it.copy(isLoading = false) }
    }

    /**
     * A failure on either read: a gone credential clears the ride's data
     * (the screen shows the removed/unpaired state, not an error over the
     * previous data), a waking cloud shows the waking notice, a throttle
     * the retry notice, and everything else its message on the side that
     * failed.
     */
    private fun applyFailure(e: Throwable, forStreams: Boolean) {
        when {
            e is CloudSession.Failure.DeviceRemoved ||
                e is LocalClientException.Unauthorized ||
                e is CloudSession.Failure.NotPaired -> {
                val removed = e is CloudSession.Failure.DeviceRemoved ||
                    e is LocalClientException.Unauthorized
                _uiState.update {
                    it.copy(
                        deviceState = if (removed) {
                            CloudSession.DeviceState.removed
                        } else {
                            CloudSession.DeviceState.unpaired
                        },
                        isPaired = false,
                        summary = null,
                        detail = null,
                        streams = null,
                        noStreams = false,
                        isWaking = false,
                        isLoading = false,
                        detailError = null,
                        streamsError = null,
                    )
                }
            }

            e is CloudSession.Failure.Waking ->
                _uiState.update { it.copy(isWaking = true) }

            else -> {
                // The local backend's 429/503 carries `Retry-After` on its
                // own exception; it earns the same notice the cloud's does.
                val throttled = (e as? CloudSession.Failure.Throttled)?.retryAfter?.toInt()
                    ?: (e as? LocalClientException.Http)
                        ?.takeIf { it.status == 429 || it.status == 503 }
                        ?.retryAfter?.toInt()
                if (throttled != null) {
                    _uiState.update { it.copy(throttledRetrySeconds = throttled) }
                } else {
                    val message = e.message ?: e::class.simpleName
                    _uiState.update {
                        if (forStreams) it.copy(streamsError = message) else it.copy(detailError = message)
                    }
                }
            }
        }
    }

    private fun isNotFound(e: Throwable): Boolean =
        (e is CloudSession.Failure.Server && e.status == 404) ||
            (e is LocalClientException.Http && e.status == 404)

    /** [runCatching], except a cancellation keeps propagating. */
    private suspend fun <T> readResult(block: suspend () -> T): Result<T> = try {
        Result.success(block())
    } catch (e: CancellationException) {
        throw e
    } catch (e: Exception) {
        Result.failure(e)
    }

    private suspend fun cachedSummary(activityId: Int): ActivitySummary? {
        val snapshot = try {
            readModel.cached(CloudRoute.Activities)
        } catch (e: CancellationException) {
            throw e
        } catch (_: Exception) {
            return null
        }
        return snapshot?.items
            ?.mapNotNull { (it.payload as? CloudPayload.Activity)?.value }
            ?.firstOrNull { it.id?.toInt() == activityId }
    }
}
