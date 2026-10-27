package com.wattracker.android.screens.calendar

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.wattracker.android.cloud.CalendarMonth
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
 * UI state for the Calendar screen: one month of the plan, and the day the
 * rider tapped.
 */
data class CalendarUiState(
    val isStarting: Boolean = true,
    val deviceState: CloudSession.DeviceState = CloudSession.DeviceState.unpaired,
    val isPaired: Boolean = false,
    val isLoading: Boolean = false,
    val isRefreshing: Boolean = false,
    val isWaking: Boolean = false,
    val throttledRetrySeconds: Int? = null,
    val error: String? = null,
    val month: CalendarMonth = CalendarMonth.current(),
    /** The month on screen; null is the honest "nothing to show" state, never a blank grid. */
    val data: CalendarData? = null,
    /**
     * The month on screen was read successfully and its own days held nothing.
     *
     * The screen needs this to say which of two very different empties it is
     * looking at: "nothing planned or recorded in September" (the rider can
     * step to another month) versus "your backend has published no calendar
     * at all". Without it both read the same sentence, and the first one lied.
     */
    val monthIsEmpty: Boolean = false,
    /**
     * That empty came out of the cache rather than out of a server answer.
     *
     * The cloud's read seam serves the cache when its network step fails, so a
     * month with no cached days read while offline is an empty the app
     * *inferred*, and the screen has to say so with the same "Offline" badge a
     * cached grid gets. Without it "nothing planned in January 2027" looks like
     * something the backend told us.
     */
    val monthIsEmptyFromCache: Boolean = false,
    val selectedDate: String? = null,
) {
    val selectedDay: CalendarDayEntry?
        get() = selectedDate?.let { date -> data?.entry(date) }
}

/**
 * One month of plan, rides and races against the active backend.
 *
 * Mirrors the iOS `CalendarModel` and the dashboard/activities view models:
 * cache paints before the network, a changed identity clears the previous
 * rider's month and supersedes the load in flight, and a same-identity
 * resume joins that load rather than restarting it.
 *
 * Two reads ride along with the calendar itself, exactly as iOS runs them:
 * the activities collection supplies the summaries of rides linked to
 * completed workouts (the calendar objects deliberately omit them), and the
 * dashboard supplies the FTP that turns a published %FTP target into watts.
 * Neither may hide a usable calendar -- a failed ride read is a thinner day,
 * not a broken screen -- but both must propagate a `notPaired` or a
 * `deviceRemoved`, because a calendar that arrives just before a revocation
 * must never keep presenting the departed rider's data.
 */
class CalendarViewModel(
    private val readModel: ReadSession,
    private val coroutineScope: CoroutineScope? = null,
) : ViewModel() {

    private val scope: CoroutineScope
        get() = coroutineScope ?: viewModelScope

    private val _uiState = MutableStateFlow(CalendarUiState())
    val uiState: StateFlow<CalendarUiState> = _uiState.asStateFlow()

    private var loadJob: Job? = null
    /** The identity the in-flight load started under; `null` when there is none. */
    private var loadIdentity: Int? = null
    /** The month the in-flight load is reading; a move supersedes it. */
    private var loadMonth: CalendarMonth? = null
    /** The identity whose data is on screen; a re-pair clears the previous rider's month. */
    private var shownIdentity: Int? = null
    /** Bumped by every new request so a superseded load stops writing. */
    private var requestGeneration = 0

    /** The FTP from the last dashboard read; %FTP targets are watts in this number. */
    private var currentFTP: Double? = null

    /** Select a day (the detail pane or sheet follows); null dismisses it. */
    fun selectDay(dateISO: String?) {
        _uiState.update { it.copy(selectedDate = dateISO) }
    }

    /**
     * Reload the month on screen, re-reading the pairing state first.
     *
     * A refresh under a changed identity cancels the load in flight: the
     * rider may have paired, signed out or re-paired in Settings since it
     * started. A resume under the same identity and month -- a rotation, a
     * tab switch -- joins the load in flight instead of restarting it.
     */
    fun refresh() {
        val identity = readModel.identity
        val month = _uiState.value.month
        val inFlight = loadJob
        if (inFlight?.isActive == true && loadIdentity == identity && loadMonth == month) {
            return
        }
        inFlight?.cancel()
        loadIdentity = identity
        loadMonth = month
        val generation = ++requestGeneration
        loadJob = scope.launch { load(generation, month, refreshDashboard = true) }
    }

    /**
     * The rider pulled to refresh: lift a gate a waking server set, then
     * re-read -- the rider asked for fresh data, so joining an in-flight
     * load would not answer it.
     */
    fun pullToRefresh() {
        loadJob?.cancel()
        loadIdentity = readModel.identity
        val month = _uiState.value.month
        loadMonth = month
        _uiState.update { it.copy(isRefreshing = true) }
        val generation = ++requestGeneration
        loadJob = scope.launch {
            // The desktop never scales to zero; only the cloud can be waking,
            // and only a cold-start gate may be lifted.
            if (readModel.mayBeWaking) readModel.retryNowIfWaking()
            load(generation, month, refreshDashboard = true)
        }
    }

    /**
     * The month the rider asked for. The header flips to the new month
     * right away, the way iOS does, and the month's own cache paints under
     * it while the network read runs; the load in flight -- the month just
     * left -- is superseded and its answer is dropped.
     *
     * The grid that was up goes with the old month, and the spinner takes
     * its place until the new month's cache paints. Leaving the previous
     * month's rows up is the blank grid that looks synced: the header says
     * June, the cells are May's apart from the overlap week, and the
     * "updated" stamp is May's. iOS keeps those rows here; Android is
     * deliberately the more literal of the two about what is on screen.
     */
    fun moveMonth(offset: Int) {
        val target = _uiState.value.month.addingMonths(offset)
        if (offset == 0) return
        _uiState.update {
            it.copy(
                month = target,
                selectedDate = null,
                data = null,
                monthIsEmpty = false,
                monthIsEmptyFromCache = false,
                // A month move is always a read, so the spinner owns the body
                // immediately rather than for one frame the "nothing yet" card.
                isLoading = true,
                error = null,
            )
        }
        loadJob?.cancel()
        loadIdentity = readModel.identity
        loadMonth = target
        val generation = ++requestGeneration
        loadJob = scope.launch { load(generation, target, refreshDashboard = false) }
    }

    private suspend fun load(generation: Int, month: CalendarMonth, refreshDashboard: Boolean) {
        val deviceState = readModel.deviceState
        val isPaired = readModel.isPaired
        val identity = readModel.identity
        _uiState.update {
            it.copy(isStarting = false, deviceState = deviceState, isPaired = isPaired)
        }

        // A signed-out or removed rider leaves nothing of their calendar behind.
        if (!isPaired || deviceState == CloudSession.DeviceState.removed) {
            shownIdentity = null
            currentFTP = null
            _uiState.update {
                it.copy(
                    isLoading = false,
                    isRefreshing = false,
                    isWaking = false,
                    throttledRetrySeconds = null,
                    error = null,
                    data = null,
                    monthIsEmpty = false,
                    monthIsEmptyFromCache = false,
                    selectedDate = null,
                )
            }
            return
        }

        // A re-pair replaces the identity: the month on screen is the
        // previous rider's. Clear it now, so a first load that fails does
        // not leave the old grid up under "Syncing".
        if (shownIdentity != identity) {
            shownIdentity = null
            currentFTP = null
            _uiState.update {
                it.copy(data = null, monthIsEmpty = false, monthIsEmptyFromCache = false, selectedDate = null)
            }
        }

        // 1. The month's cache first, so the network is not on the first-paint path.
        // The cached dashboard rides along: iOS's `start` reads FTP from it too,
        // which is what keeps a cold start that never finishes its dashboard
        // read -- the rider paged months, each move superseding the last load --
        // from painting every later month's targets as "% FTP" only.
        val cachedDashboard = cachedOrNull(CloudRoute.Dashboard, null)
        var dashboard = cachedDashboard
        val cached = cachedOrNull(CloudRoute.Calendar, month)
        if (cached != null && stillCurrent(generation, month)) {
            val ftp = CalendarData.currentFTP(cachedDashboard) ?: currentFTP
            apply(generation, month, cached, cachedOrNull(CloudRoute.Activities, null), ftp)
            checkActive()
        }

        // 2. Always the network: it refreshes the month and it is how a
        // revocation is discovered.
        _uiState.update {
            it.copy(isLoading = true, error = null, throttledRetrySeconds = null, isWaking = false)
        }

        val fresh = try {
            readModel.load(CloudRoute.Calendar, month)
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            checkActive()
            showFailure(e)
            return
        }
        if (!stillCurrent(generation, month)) return

        // The linked-ride summaries. A missing activities collection must not
        // hide the calendar, but a terminal session state must not be eaten.
        // Re-read per month, as iOS's `moveMonth` does: the summaries are what
        // turn a completed workout into the ride row under it, and the desktop
        // answer is one rider's history, not a paging problem.
        var activities = cachedOrNull(CloudRoute.Activities, null)
        try {
            activities = readModel.load(CloudRoute.Activities, null)
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            if (isTerminal(e)) {
                checkActive()
                showFailure(e)
                return
            }
        }
        if (!stillCurrent(generation, month)) return

        if (refreshDashboard) {
            try {
                dashboard = readModel.load(CloudRoute.Dashboard, null)
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                if (isTerminal(e)) {
                    checkActive()
                    showFailure(e)
                    return
                }
                // A dashboard outage must not hide a usable calendar; `dashboard`
                // stays the cached one and the last FTP keeps the watts.
            }
        }
        if (!stillCurrent(generation, month)) return

        shownIdentity = identity
        apply(
            generation,
            month,
            fresh,
            activities,
            // iOS's `moveMonth` re-uses the FTP it already has; a month move
            // never re-reads the dashboard, so the cached one (or the last
            // read) is what keeps a month's steps in watts.
            CalendarData.currentFTP(dashboard) ?: currentFTP,
        )
        checkActive()
    }

    /**
     * Build the month from its snapshot, plus the adjacent-month days
     * already cached that fall in the grid's leading and trailing week --
     * those cells are real dates of the neighbouring months, and whatever is
     * already known about them is shown too.
     */
    private suspend fun apply(
        generation: Int,
        month: CalendarMonth,
        snapshot: CloudSnapshot,
        activities: CloudSnapshot?,
        ftp: Double?,
    ) {
        val grid = CalendarData.gridSnapshot(
            snapshot,
            month,
            adjacent = listOf(
                cachedOrNull(CloudRoute.Calendar, month.addingMonths(-1)),
                cachedOrNull(CloudRoute.Calendar, month.addingMonths(1)),
            ),
        )
        if (!stillCurrent(generation, month)) return
        // The FTP the screen is painting with is now the FTP the screen has:
        // iOS's `apply` stores what it was given, cache path included, and a
        // month move that skips the dashboard read depends on that.
        currentFTP = ftp
        val data = CalendarData(grid, activities, ftp)
        // Whether the month has data at all is decided by the month's own days:
        // adjacent-month edge cells are context, not content, and the cloud's
        // one feed spans every month -- judging the month over that snapshot
        // would call an empty September "has content" because October rode.
        if (data.hasContentIn(month)) {
            _uiState.update {
                it.copy(isLoading = false, isRefreshing = false, data = data, monthIsEmpty = false, error = null)
            }
        } else {
            _uiState.update {
                it.copy(
                    isLoading = false,
                    isRefreshing = false,
                    data = null,
                    monthIsEmpty = true,
                    // A cache-served snapshot is the cloud's answer when its
                    // network step failed, so this empty was inferred from what
                    // was already known: the badge belongs here too.
                    monthIsEmptyFromCache = snapshot.source == CloudSnapshot.Source.cache,
                    error = null,
                    selectedDate = null,
                )
            }
        }
    }

    private suspend fun cachedOrNull(route: CloudRoute, month: CalendarMonth?) = try {
        readModel.cached(route, month)
    } catch (e: CancellationException) {
        throw e
    } catch (_: Exception) {
        null
    }

    private fun isTerminal(e: Exception): Boolean =
        e is CloudSession.Failure.NotPaired || e is CloudSession.Failure.DeviceRemoved ||
            readModel.deviceState != CloudSession.DeviceState.paired

    /**
     * Stop here if this load was superseded -- by a newer request, or by
     * being cancelled. `CloudSession.load` serves the cache when its network
     * step fails, and that includes being cancelled, so a cancelled load can
     * still return normally; this is what keeps the month just left off the
     * screen.
     */
    private suspend fun checkActive() = currentCoroutineContext().ensureActive()

    private fun stillCurrent(generation: Int, month: CalendarMonth): Boolean =
        generation == requestGeneration && _uiState.value.month == month

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
                    selectedDate = null,
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
                    selectedDate = null,
                    error = null,
                )
            }

            else -> {
                // An ordinary failure (offline, throttled, waking) leaves the
                // cached month on screen when there is one; only the chrome
                // changes.
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
                        // The rider-facing sentences are the same convention
                        // the other screens use: each failure carries its own
                        // ("No connection", "HTTP 500 from /api/calendar"),
                        // as iOS does through `failure.description`.
                        error = if (waking || retrySec != null) null else e.message,
                    )
                }
            }
        }
    }
}
