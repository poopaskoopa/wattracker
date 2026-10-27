package com.wattracker.android.screens.calendar

import com.wattracker.android.cloud.CalendarDay
import com.wattracker.android.cloud.CalendarMonth
import com.wattracker.android.cloud.CloudItem
import com.wattracker.android.cloud.CloudKind
import com.wattracker.android.cloud.CloudPayload
import com.wattracker.android.cloud.CloudRoute
import com.wattracker.android.cloud.CloudSession
import com.wattracker.android.cloud.CloudSnapshot
import com.wattracker.android.cloud.LocalClientException
import com.wattracker.android.cloud.ReadSession
import com.wattracker.android.cloud.TrainingState
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import kotlinx.coroutines.Dispatchers
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class CalendarViewModelTest {

    private val testDispatcher = UnconfinedTestDispatcher()
    private val may = CalendarMonth(2026, 5)

    @Before
    fun setUp() {
        Dispatchers.setMain(testDispatcher)
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    /**
     * A month-keyed seam double: calendar reads carry the month, everything
     * else ignores it -- the same contract both real adapters present.
     */
    private class FakeReadSession(
        override var deviceState: CloudSession.DeviceState = CloudSession.DeviceState.paired,
        override var isPaired: Boolean = true,
        override var identity: Int = 1,
        override var lastSuccess: Long? = 1000L,
        override var mayBeWaking: Boolean = false,
        private val cachedSnapshots: Map<Pair<CloudRoute, CalendarMonth?>, CloudSnapshot> = emptyMap(),
        private val loadHandler: suspend (CloudRoute, CalendarMonth?) -> CloudSnapshot = { route, _ ->
            CloudSnapshot(route, 1, emptyList(), CloudSnapshot.Source.network, 2000L)
        },
    ) : ReadSession {
        val loads = mutableListOf<Pair<CloudRoute, CalendarMonth?>>()
        var retryCalls = 0
        /** Flip off to model a cache that no longer holds anything for a route. */
        var cacheServes = true

        override suspend fun cached(route: CloudRoute): CloudSnapshot? = cached(route, null)
        override suspend fun cached(route: CloudRoute, month: CalendarMonth?): CloudSnapshot? {
            if (!cacheServes) return null
            return cachedSnapshots[route to month] ?: cachedSnapshots[route to null]
        }
        override suspend fun load(route: CloudRoute): CloudSnapshot = load(route, null)
        override suspend fun load(route: CloudRoute, month: CalendarMonth?): CloudSnapshot {
            loads.add(route to month)
            return loadHandler(route, month)
        }
        override suspend fun activityDetail(activityId: Int) = throw NotImplementedError()
        override suspend fun activityStreams(activityId: Int) = throw NotImplementedError()
        override suspend fun retryNowIfWaking() {
            retryCalls += 1
        }
    }

    private fun dayItem(date: String, workoutCount: Int = 1): CloudItem = CloudItem(
        id = "calendar-day-$date",
        kind = CloudKind.CalendarDay,
        revision = 1,
        payload = CloudPayload.CalendarDay(
            CalendarDay(
                date = date,
                ooto = null,
                phase = null,
                race = null,
                workouts = (1..workoutCount).map {
                    CloudItemFixture.workoutRow(it.toDouble())
                },
                activities = null,
                part = null,
                parts = null,
            ),
        ),
    )

    private fun calendarSnapshot(
        items: List<CloudItem>,
        source: CloudSnapshot.Source = CloudSnapshot.Source.network,
        asOf: Long = 2000L,
    ) = CloudSnapshot(CloudRoute.Calendar, 1, items, source, asOf)

    @Test
    fun loadsTheMonthOnScreenWithCacheFirst() = runTest(testDispatcher) {
        val session = FakeReadSession(
            cachedSnapshots = mapOf(
                (CloudRoute.Calendar to may) to calendarSnapshot(
                    listOf(dayItem("2026-05-04")), source = CloudSnapshot.Source.cache, asOf = 1000L,
                ),
            ),
            loadHandler = { route, month ->
                when (route) {
                    CloudRoute.Calendar -> calendarSnapshot(
                        if (month == may) listOf(dayItem("2026-05-04"), dayItem("2026-05-05")) else emptyList(),
                    )
                    else -> CloudSnapshot(route, 1, emptyList(), CloudSnapshot.Source.network, 2000L)
                }
            },
        )
        val vm = CalendarViewModel(session, coroutineScope = backgroundScope)
        // The screen starts on the device's month; the fake serves May, so
        // the test drives to May and reads what paints.
        vm.moveMonth(monthsFromTodayTo(may))
        assertEquals(2, vm.uiState.value.data?.days?.size)
        assertEquals(CloudSnapshot.Source.network, vm.uiState.value.data?.source)
        assertTrue(session.loads.any { it == (CloudRoute.Calendar to may) })
    }

    /**
     * An empty month is its own state, and -- the part the first version of
     * this test did not check -- a state the rider can leave. The month
     * chevrons used to live inside the grid, so a month the backend answered
     * empty had no way out except changing backend or killing the app.
     */
    @Test
    fun anEmptyMonthSaysSoAndStaysNavigable() = runTest(testDispatcher) {
        val session = FakeReadSession(
            loadHandler = { route, month ->
                when {
                    route != CloudRoute.Calendar -> CloudSnapshot(route, 1, emptyList(), CloudSnapshot.Source.network, 2000L)
                    month == may -> calendarSnapshot(emptyList())
                    else -> calendarSnapshot(listOf(dayItem(month!!.isoDate(1))))
                }
            },
        )
        val vm = CalendarViewModel(session, coroutineScope = backgroundScope)
        vm.moveMonth(monthsFromTodayTo(may))
        val empty = vm.uiState.value
        assertNull("an empty month shows no grid", empty.data)
        assertNull(empty.error)
        // The screen needs to know this was a read that came back empty, not a
        // backend that has published nothing at all: the sentence it prints
        // differs, and the stepper has to be painted above it.
        assertTrue("the state says 'nothing this month'", empty.monthIsEmpty)
        assertEquals(may, empty.month)

        vm.moveMonth(1)
        val moved = vm.uiState.value
        assertEquals(CalendarMonth(2026, 6), moved.month)
        assertNotNull("the rider is not trapped in the empty month", moved.data)
        assertFalse(moved.monthIsEmpty)
    }

    @Test
    fun aCloudFeedSpanningTheYearStillCallsAnEmptyMonthEmpty() = runTest(testDispatcher) {
        // The cloud answers every month out of one whole-feed snapshot, so the
        // month-level decision cannot be asked of that snapshot -- it holds
        // October as much as May. Judged over the whole feed, May painted a
        // blank grid and the honest "nothing this month" state never appeared
        // on the cloud at all, only on the local backend.
        val feed = calendarSnapshot(listOf(dayItem("2026-06-10")))
        val session = FakeReadSession(
            loadHandler = { route, _ ->
                if (route == CloudRoute.Calendar) feed
                else CloudSnapshot(route, 1, emptyList(), CloudSnapshot.Source.network, 2000L)
            },
        )
        val vm = CalendarViewModel(session, coroutineScope = backgroundScope)
        vm.moveMonth(monthsFromTodayTo(may))
        assertTrue("May is empty even though the feed is not", vm.uiState.value.monthIsEmpty)
        assertNull(vm.uiState.value.data)
        vm.moveMonth(1)
        assertNotNull("June comes out of the same feed", vm.uiState.value.data)
        assertFalse(vm.uiState.value.monthIsEmpty)
    }

    @Test
    fun theCachedFtpCarriesIntoEveryMonthThatNeverReadsTheDashboard() = runTest(testDispatcher) {
        // The cold-start page. Each month move supersedes the load before it,
        // so on a cold start the first load's dashboard read may never finish,
        // and a month move does not read the dashboard at all. The FTP has to
        // come from the cached dashboard and then be *kept*: if it is only a
        // local variable in the cache paint, every month after the first shows
        // "% FTP" targets where the rider should see watts.
        val dashboard = CloudSnapshot(
            CloudRoute.Dashboard, 1,
            listOf(
                CloudItem(
                    "training-state", CloudKind.TrainingState, 1,
                    payload = CloudPayload.TrainingState(
                        TrainingState(ftp = 250.0, cp = null, wprime = null, ctl = null, atl = null, tsb = null, decoupling = null),
                    ),
                ),
            ),
            CloudSnapshot.Source.cache, 1000L,
        )
        val session = FakeReadSession(
            cachedSnapshots = mapOf((CloudRoute.Dashboard to null) to dashboard),
            loadHandler = { route, month ->
                when (route) {
                    CloudRoute.Calendar -> calendarSnapshot(listOf(dayItem(month!!.isoDate(1))))
                    else -> CloudSnapshot(route, 1, emptyList(), CloudSnapshot.Source.network, 2000L)
                }
            },
        )
        val vm = CalendarViewModel(session, coroutineScope = backgroundScope)
        vm.moveMonth(monthsFromTodayTo(may))
        assertEquals(250.0, vm.uiState.value.data?.currentFTP)
        // And then the FTP has to be *kept*, not re-derived: a later month may
        // find nothing cached for the dashboard, and a month move never re-reads
        // it. iOS's `apply` stores the FTP it was given and its `moveMonth`
        // passes that field; the screen's watts depend on both halves.
        session.cacheServes = false
        vm.moveMonth(1)
        assertEquals(250.0, vm.uiState.value.data?.currentFTP)
        assertEquals(
            "neither month move asked the dashboard anything",
            0,
            session.loads.count { it.first == CloudRoute.Dashboard },
        )
    }

    @Test
    fun movingToAMonthThatCannotBeReadLeavesNoGridUnderTheNewHeader() = runTest(testDispatcher) {
        // May reads; June cannot, and was never cached. Keeping May's rows up
        // under a June header is the blank grid that looks synced: the cells
        // are the wrong month apart from the overlap week, and the "updated"
        // stamp is May's. The honest answer is the failure, with no grid.
        val session = FakeReadSession(
            loadHandler = { route, month ->
                when {
                    route != CloudRoute.Calendar -> CloudSnapshot(route, 1, emptyList(), CloudSnapshot.Source.network, 2000L)
                    month == may -> calendarSnapshot(listOf(dayItem("2026-05-04")))
                    else -> throw CloudSession.Failure.Offline()
                }
            },
        )
        val vm = CalendarViewModel(session, coroutineScope = backgroundScope)
        vm.moveMonth(monthsFromTodayTo(may))
        assertNotNull(vm.uiState.value.data)
        vm.moveMonth(1)
        val state = vm.uiState.value
        assertNull("June's body is not May's rows", state.data)
        assertNotNull("and the failure says why it is empty", state.error)
        assertFalse("an unread month is not reported as an empty one", state.monthIsEmpty)
    }

    @Test
    fun anOrdinaryFailureKeepsTheCachedMonth() = runTest(testDispatcher) {
        val session = FakeReadSession(
            cachedSnapshots = mapOf(
                (CloudRoute.Calendar to may) to calendarSnapshot(
                    listOf(dayItem("2026-05-04")), source = CloudSnapshot.Source.cache, asOf = 1000L,
                ),
            ),
            loadHandler = { _, month ->
                if (month == may) throw CloudSession.Failure.Offline()
                CloudSnapshot(CloudRoute.Calendar, 1, emptyList(), CloudSnapshot.Source.network, 2000L)
            },
        )
        val vm = CalendarViewModel(session, coroutineScope = backgroundScope)
        vm.moveMonth(monthsFromTodayTo(may))
        val state = vm.uiState.value
        assertNotNull("the month the cache had stays up", state.data)
        assertNotNull("and the failure is surfaced", state.error)
        assertEquals(CloudSnapshot.Source.cache, state.data?.source)
    }

    @Test
    fun removedClearsTheMonth() = runTest(testDispatcher) {
        val session = FakeReadSession(
            loadHandler = { _, _ -> throw CloudSession.Failure.DeviceRemoved() },
        )
        val vm = CalendarViewModel(session, coroutineScope = backgroundScope)
        vm.moveMonth(monthsFromTodayTo(may))
        val state = vm.uiState.value
        assertEquals(CloudSession.DeviceState.removed, state.deviceState)
        assertNull(state.data)
        assertNull(state.selectedDate)
    }

    @Test
    fun unpairedAsksForNothing() = runTest(testDispatcher) {
        val session = FakeReadSession(
            deviceState = CloudSession.DeviceState.unpaired,
            isPaired = false,
            loadHandler = { _, _ -> throw AssertionError("an unpaired device must not be asked to read") },
        )
        val vm = CalendarViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()
        assertEquals(0, session.loads.size)
        assertEquals(CloudSession.DeviceState.unpaired, vm.uiState.value.deviceState)
    }

    @Test
    fun aTerminalAnswerFromTheRideReadEndsTheScreenToo() = runTest(testDispatcher) {
        // The calendar itself answered; the activities read discovered the
        // device was gone. The month must not stay up under a revocation.
        val session = FakeReadSession(
            deviceState = CloudSession.DeviceState.paired,
            loadHandler = { route, _ ->
                if (route == CloudRoute.Calendar) {
                    calendarSnapshot(listOf(dayItem("2026-05-04")))
                } else {
                    throw CloudSession.Failure.DeviceRemoved()
                }
            },
        )
        val vm = CalendarViewModel(session, coroutineScope = backgroundScope)
        vm.moveMonth(monthsFromTodayTo(may))
        assertEquals(CloudSession.DeviceState.removed, vm.uiState.value.deviceState)
        assertNull(vm.uiState.value.data)
    }

    @Test
    fun aNonTerminalRideReadFailureLeavesTheCalendarUp() = runTest(testDispatcher) {
        val session = FakeReadSession(
            loadHandler = { route, _ ->
                if (route == CloudRoute.Calendar) {
                    calendarSnapshot(listOf(dayItem("2026-05-04")))
                } else {
                    throw CloudSession.Failure.Offline()
                }
            },
        )
        val vm = CalendarViewModel(session, coroutineScope = backgroundScope)
        vm.moveMonth(monthsFromTodayTo(may))
        assertNotNull(vm.uiState.value.data)
    }

    @Test
    fun movingMonthsDropsTheAnswerOfTheMonthJustLeft() = runTest(testDispatcher) {
        // Same shape as the activities list's supersession test: the first
        // read is parked on a gate, the rider moves a month, and the parked
        // answer -- for the month just left -- must never paint. The fake
        // answers a calendar read with the month it was asked for, so a May
        // answer landing after the June one would be visible.
        val gate = CompletableDeferred<Unit>()
        var gated = false
        val session = FakeReadSession(
            loadHandler = { route, month ->
                if (route == CloudRoute.Calendar) {
                    if (!gated) {
                        gated = true
                        gate.await()
                    }
                    calendarSnapshot(listOf(dayItem(month!!.isoDate(1))))
                } else {
                    CloudSnapshot(route, 1, emptyList(), CloudSnapshot.Source.network, 2000L)
                }
            },
        )
        val vm = CalendarViewModel(session, coroutineScope = backgroundScope)
        vm.moveMonth(monthsFromTodayTo(CalendarMonth(2026, 5)))
        vm.moveMonth(1)
        gate.complete(Unit)
        val state = vm.uiState.value
        assertEquals(CalendarMonth(2026, 6), state.month)
        assertTrue(
            "the painted days belong to the month on screen",
            state.data?.days?.all { it.dateISO.startsWith("2026-06") } == true,
        )
    }

    @Test
    fun pullToRefreshLiftsTheWakingGateOnlyWhenUserInitiated() = runTest(testDispatcher) {
        val session = FakeReadSession(mayBeWaking = true)
        val vm = CalendarViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()
        assertEquals(0, session.retryCalls)
        vm.pullToRefresh()
        assertEquals(1, session.retryCalls)
    }

    @Test
    fun aLocalThrottleEarnsTheRetryNotice() = runTest(testDispatcher) {
        val session = FakeReadSession(
            loadHandler = { _, _ -> throw LocalClientException.Http(status = 429, path = "/api/calendar", retryAfter = 120.0) },
        )
        val vm = CalendarViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()
        val state = vm.uiState.value
        assertEquals(120, state.throttledRetrySeconds)
        assertNull("the notice is the string resource's, not a duplicated message", state.error)
    }

    private fun monthsFromTodayTo(month: CalendarMonth): Int {
        val today = CalendarMonth.current()
        return (month.year - today.year) * 12 + (month.month - today.month)
    }

    /** A tiny fixture builder for workout rows inside a `calendar_day`. */
    private object CloudItemFixture {
        fun workoutRow(id: Double): com.wattracker.android.json.JsonValue =
            com.wattracker.android.json.JsonValue.Object(
                linkedMapOf(
                    "id" to com.wattracker.android.json.JsonValue.Number(id),
                    "name" to com.wattracker.android.json.JsonValue.String("Planned $id"),
                ),
            )
    }
}
