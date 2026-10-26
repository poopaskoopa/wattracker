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
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import kotlinx.coroutines.Dispatchers
import org.junit.After
import org.junit.Assert.assertEquals
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

        override suspend fun cached(route: CloudRoute): CloudSnapshot? = cached(route, null)
        override suspend fun cached(route: CloudRoute, month: CalendarMonth?): CloudSnapshot? =
            cachedSnapshots[route to month] ?: cachedSnapshots[route to null]
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

    @Test
    fun emptyMonthIsTheNoDataStateNotABlankGrid() = runTest(testDispatcher) {
        val session = FakeReadSession(
            loadHandler = { route, _ -> CloudSnapshot(route, 1, emptyList(), CloudSnapshot.Source.network, 2000L) },
        )
        val vm = CalendarViewModel(session, coroutineScope = backgroundScope)
        vm.moveMonth(monthsFromTodayTo(may))
        val state = vm.uiState.value
        assertNull("an empty month shows no grid", state.data)
        assertNull(state.error)
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
