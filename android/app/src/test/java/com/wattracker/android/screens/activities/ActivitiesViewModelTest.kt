package com.wattracker.android.screens.activities

import com.wattracker.android.cloud.ActivityDetail
import com.wattracker.android.cloud.ActivityStreams
import com.wattracker.android.cloud.ActivitySummary
import com.wattracker.android.cloud.CloudItem
import com.wattracker.android.cloud.CloudKind
import com.wattracker.android.cloud.CloudPayload
import com.wattracker.android.cloud.CloudRoute
import com.wattracker.android.cloud.CloudSession
import com.wattracker.android.cloud.CloudSnapshot
import com.wattracker.android.cloud.LocalClientException
import com.wattracker.android.cloud.ReadSession
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class ActivitiesViewModelTest {

    private val testDispatcher = UnconfinedTestDispatcher()

    @Before
    fun setUp() {
        kotlinx.coroutines.Dispatchers.setMain(testDispatcher)
    }

    @After
    fun tearDown() {
        kotlinx.coroutines.Dispatchers.resetMain()
    }

    private class FakeReadSession(
        override var deviceState: CloudSession.DeviceState = CloudSession.DeviceState.paired,
        override var isPaired: Boolean = true,
        override var identity: Int = 1,
        override var lastSuccess: Long? = 1000L,
        override var mayBeWaking: Boolean = false,
        private val cachedSnapshots: Map<CloudRoute, CloudSnapshot> = emptyMap(),
        private val loadHandler: suspend (CloudRoute) -> CloudSnapshot = { route ->
            CloudSnapshot(route, 1, emptyList(), CloudSnapshot.Source.network, 2000L)
        },
        private val retryNowIfWakingHandler: suspend () -> Unit = {},
    ) : ReadSession {
        val loads = mutableListOf<CloudRoute>()
        var retryCalls = 0

        override suspend fun cached(route: CloudRoute): CloudSnapshot? = cachedSnapshots[route]
        override suspend fun load(route: CloudRoute): CloudSnapshot {
            loads.add(route)
            return loadHandler(route)
        }
        override suspend fun activityDetail(activityId: Int): ActivityDetail = throw NotImplementedError()
        override suspend fun activityStreams(activityId: Int): ActivityStreams = throw NotImplementedError()
        override suspend fun retryNowIfWaking() {
            retryCalls += 1
            retryNowIfWakingHandler()
        }
    }

    private fun activityItem(id: Int, startTime: String, revision: Int = 1, deleted: Boolean = false): CloudItem =
        CloudItem(
            id = "activity-$id",
            kind = CloudKind.Activity,
            revision = revision,
            deleted = deleted,
            payload = if (deleted) {
                CloudPayload.Tombstone
            } else {
                CloudPayload.Activity(
                    ActivitySummary(
                        id = id.toDouble(),
                        startTime = startTime,
                        durationS = 3600.0,
                        distanceM = 32100.5,
                        avgPower = 211.0,
                        avgHr = 142.0,
                        np = 225.0,
                        intensityFactor = 0.9,
                        tss = 81.0,
                        rpe = 6.0,
                    ),
                )
            },
        )

    private fun activitiesSnapshot(
        items: List<CloudItem>,
        source: CloudSnapshot.Source = CloudSnapshot.Source.network,
        asOf: Long = 2000L,
        revision: Int = 1,
    ) = CloudSnapshot(CloudRoute.Activities, revision, items, source, asOf)

    @Test
    fun loadsRidesNewestFirst() = runTest(testDispatcher) {
        val session = FakeReadSession(
            loadHandler = { route ->
                if (route == CloudRoute.Activities) {
                    activitiesSnapshot(listOf(activityItem(999, "2024-05-01T10:00:00"), activityItem(100, "2024-05-10T10:00:00")))
                } else {
                    CloudSnapshot(route, 1, emptyList(), CloudSnapshot.Source.network, 2000L)
                }
            },
        )
        val vm = ActivitiesViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()

        val rides = vm.uiState.value.rides
        assertEquals(2, rides.size)
        assertEquals(100, rides[0].activityId)
        assertEquals(999, rides[1].activityId)
        assertEquals(rides[0].id, vm.uiState.value.selectedId)
    }

    @Test
    fun tombstonesAreAbsentNotGreyed() = runTest(testDispatcher) {
        val session = FakeReadSession(
            loadHandler = { route ->
                if (route == CloudRoute.Activities) {
                    activitiesSnapshot(
                        listOf(activityItem(1, "2024-05-10T10:00:00"), activityItem(2, "2024-05-09T10:00:00", deleted = true)),
                    )
                } else {
                    CloudSnapshot(route, 1, emptyList(), CloudSnapshot.Source.network, 2000L)
                }
            },
        )
        val vm = ActivitiesViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()

        assertEquals(1, vm.uiState.value.rides.size)
        assertEquals(1, vm.uiState.value.rides[0].activityId)
    }

    @Test
    fun cachePaintsBeforeTheNetwork() = runTest(testDispatcher) {
        val session = FakeReadSession(
            cachedSnapshots = mapOf(
                CloudRoute.Activities to activitiesSnapshot(listOf(activityItem(1, "2024-05-09T10:00:00")), source = CloudSnapshot.Source.cache, asOf = 1000L),
            ),
            loadHandler = { route ->
                if (route == CloudRoute.Activities) {
                    activitiesSnapshot(listOf(activityItem(2, "2024-05-10T10:00:00"), activityItem(1, "2024-05-09T10:00:00")))
                } else {
                    CloudSnapshot(route, 1, emptyList(), CloudSnapshot.Source.network, 2000L)
                }
            },
        )
        val vm = ActivitiesViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()

        val state = vm.uiState.value
        assertEquals(CloudSnapshot.Source.network, state.source)
        assertEquals(2, state.rides.size)
        assertEquals(2000L, state.asOf)
        assertEquals(2, state.rides[0].activityId)
    }

    @Test
    fun anOrdinaryFailureKeepsTheCachedRides() = runTest(testDispatcher) {
        val session = FakeReadSession(
            cachedSnapshots = mapOf(
                CloudRoute.Activities to activitiesSnapshot(listOf(activityItem(1, "2024-05-09T10:00:00")), source = CloudSnapshot.Source.cache, asOf = 1000L),
            ),
            loadHandler = { throw CloudSession.Failure.Offline() },
        )
        val vm = ActivitiesViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()

        val state = vm.uiState.value
        assertEquals(1, state.rides.size)
        assertEquals(CloudSnapshot.Source.cache, state.source)
        assertNotNull("the failure is surfaced, the rides are not", state.error)
    }

    @Test
    fun unpairedClearsTheRenderedRideList() = runTest(testDispatcher) {
        val paired = FakeReadSession(
            loadHandler = { route ->
                if (route == CloudRoute.Activities) {
                    activitiesSnapshot(listOf(activityItem(1, "2024-05-09T10:00:00")))
                } else {
                    CloudSnapshot(route, 1, emptyList(), CloudSnapshot.Source.network, 2000L)
                }
            },
        )
        val vm = ActivitiesViewModel(paired, coroutineScope = backgroundScope)
        vm.refresh()
        assertEquals(1, vm.uiState.value.rides.size)

        // A sign-out in Settings: a signed-out rider's rides must not stay
        // rendered under an error line.
        val unpaired = FakeReadSession(
            deviceState = CloudSession.DeviceState.unpaired,
            isPaired = false,
            identity = 2,
            loadHandler = { throw AssertionError("an unpaired device must not be asked to read") },
        )
        val vm2 = ActivitiesViewModel(unpaired, coroutineScope = backgroundScope)
        vm2.refresh()

        val state = vm2.uiState.value
        assertEquals(CloudSession.DeviceState.unpaired, state.deviceState)
        assertTrue(state.rides.isEmpty())
        assertNull(state.selectedId)
        assertEquals(0, unpaired.loads.size)
    }

    @Test
    fun removedClearsTheRenderedRideList() = runTest(testDispatcher) {
        val session = FakeReadSession(
            loadHandler = { throw CloudSession.Failure.DeviceRemoved() },
        )
        val vm = ActivitiesViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()

        val state = vm.uiState.value
        assertEquals(CloudSession.DeviceState.removed, state.deviceState)
        assertTrue(state.rides.isEmpty())
        assertNull(state.selectedId)
    }

    @Test
    fun aSameIdentityResumeJoinsTheLoadInFlight() = runTest(testDispatcher) {
        var started = false
        val gate = CompletableGate()
        val session = FakeReadSession(
            loadHandler = { route ->
                if (route == CloudRoute.Activities) {
                    if (!started) {
                        started = true
                        gate.await()
                    }
                    activitiesSnapshot(listOf(activityItem(1, "2024-05-09T10:00:00")))
                } else {
                    CloudSnapshot(route, 1, emptyList(), CloudSnapshot.Source.network, 2000L)
                }
            },
        )
        val vm = ActivitiesViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()
        // A rotation or tab switch under the same identity: no restart.
        vm.refresh()
        assertEquals("the walk is not restarted", 1, session.loads.count { it == CloudRoute.Activities })
        gate.open()
        assertEquals(1, vm.uiState.value.rides.size)
    }

    @Test
    fun aChangedIdentitySupersedesTheLoadInFlight() = runTest(testDispatcher) {
        val gate = CompletableGate()
        var first = true
        val session = FakeReadSession(
            loadHandler = { route ->
                if (route == CloudRoute.Activities) {
                    if (first) {
                        first = false
                        gate.await()
                    }
                    activitiesSnapshot(listOf(activityItem(1, "2024-05-09T10:00:00")))
                } else {
                    CloudSnapshot(route, 1, emptyList(), CloudSnapshot.Source.network, 2000L)
                }
            },
        )
        val vm = ActivitiesViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()
        // A re-pair in Settings while the walk is in flight: the old load is
        // cancelled and the previous rider's rides never paint.
        session.identity = 2
        vm.refresh()
        assertEquals("the old walk is cancelled and a new one starts", 2, session.loads.count { it == CloudRoute.Activities })
        gate.open()
        assertEquals(1, vm.uiState.value.rides.size)
    }

    @Test
    fun pullToRefreshLiftsTheWakingGateOnlyWhenUserInitiated() = runTest(testDispatcher) {
        val session = FakeReadSession(mayBeWaking = true)
        val vm = ActivitiesViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()
        assertEquals(0, session.retryCalls)
        vm.pullToRefresh()
        assertEquals(1, session.retryCalls)
    }

    @Test
    fun aLocalThrottleEarnsTheRetryNotice() = runTest(testDispatcher) {
        val session = FakeReadSession(
            loadHandler = { throw LocalClientException.Http(status = 429, path = "/api/activities", retryAfter = 120.0) },
        )
        val vm = ActivitiesViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()

        val state = vm.uiState.value
        assertEquals(120, state.throttledRetrySeconds)
        assertNull("the notice is the string resource's, not a duplicated message", state.error)
    }

    /** A gate the test parks a coroutine on and opens from the test body. */
    private class CompletableGate {
        private val signal = kotlinx.coroutines.CompletableDeferred<Unit>()
        suspend fun await() = signal.await()
        fun open() = signal.complete(Unit)
    }
}
