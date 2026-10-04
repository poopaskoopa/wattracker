package com.wattracker.android.screens

import com.wattracker.android.cloud.ActivityDetail
import com.wattracker.android.cloud.ActivityStreams
import com.wattracker.android.cloud.ActivitySummary
import com.wattracker.android.cloud.CloudItem
import com.wattracker.android.cloud.CloudKind
import com.wattracker.android.cloud.CloudPayload
import com.wattracker.android.cloud.CloudRoute
import com.wattracker.android.cloud.CloudSession
import com.wattracker.android.cloud.CloudSnapshot
import com.wattracker.android.cloud.ReadSession
import com.wattracker.android.cloud.TrainingState
import com.wattracker.android.screens.dashboard.DashboardViewModel
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import kotlinx.coroutines.withContext
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class DashboardViewModelTest {

    private val testDispatcher = UnconfinedTestDispatcher()

    @Before
    fun setUp() {
        Dispatchers.setMain(testDispatcher)
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    private class FakeReadSession(
        override var deviceState: CloudSession.DeviceState = CloudSession.DeviceState.paired,
        override var isPaired: Boolean = true,
        override var lastSuccess: Long? = 1000L,
        private val cachedSnapshots: Map<CloudRoute, CloudSnapshot> = emptyMap(),
        private val loadHandler: suspend (CloudRoute) -> CloudSnapshot = { route ->
            CloudSnapshot(route, 1, emptyList(), CloudSnapshot.Source.network, 2000L)
        },
    ) : ReadSession {
        /** Every `load` call, in order, so a test can count network walks. */
        val loads = mutableListOf<CloudRoute>()

        override suspend fun cached(route: CloudRoute): CloudSnapshot? = cachedSnapshots[route]
        override suspend fun load(route: CloudRoute): CloudSnapshot {
            loads.add(route)
            return loadHandler(route)
        }
        override suspend fun activityDetail(activityId: Int): ActivityDetail = throw NotImplementedError()
        override suspend fun activityStreams(activityId: Int): ActivityStreams = throw NotImplementedError()

        override val mayBeWaking: Boolean
            get() = false

        override suspend fun retryNowIfWaking() {
            // No gate in the fake; nothing to lift.
        }
    }

    @Test
    fun loadsDataAndRendersSuccessfully() = runTest(testDispatcher) {
        val trainingStateItem = CloudItem(
            id = "ts-1",
            kind = CloudKind.TrainingState,
            revision = 1,
            payload = CloudPayload.TrainingState(
                TrainingState(ftp = 250.0, cp = 260.0, wprime = 15000.0, ctl = 60.0, atl = 70.0, tsb = -10.0, decoupling = null),
            ),
        )
        val dashboardSnapshot = CloudSnapshot(
            route = CloudRoute.Dashboard,
            revision = 1,
            items = listOf(trainingStateItem),
            source = CloudSnapshot.Source.network,
            asOf = 2000L,
        )

        val session = FakeReadSession(
            loadHandler = { route ->
                if (route == CloudRoute.Dashboard) dashboardSnapshot else CloudSnapshot(route, 1, emptyList(), CloudSnapshot.Source.network, 2000L)
            },
        )

        val vm = DashboardViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()

        val state = vm.uiState.value
        assertFalse(state.isStarting)
        assertTrue(state.isPaired)
        assertFalse(state.isLoading)
        assertNotNull(state.dashboardData)
        assertEquals(250.0, state.dashboardData?.currentFTP!!, 1e-9)
    }

    @Test
    fun cacheShownFirstThenRefreshedFromNetwork() = runTest(testDispatcher) {
        val cachedItem = CloudItem(
            id = "ts-1",
            kind = CloudKind.TrainingState,
            revision = 1,
            payload = CloudPayload.TrainingState(
                TrainingState(ftp = 240.0, cp = 250.0, wprime = 14000.0, ctl = 50.0, atl = 55.0, tsb = -5.0, decoupling = null),
            ),
        )
        val cachedSnapshot = CloudSnapshot(
            route = CloudRoute.Dashboard,
            revision = 1,
            items = listOf(cachedItem),
            source = CloudSnapshot.Source.cache,
            asOf = 1000L,
        )

        val freshItem = CloudItem(
            id = "ts-1",
            kind = CloudKind.TrainingState,
            revision = 2,
            payload = CloudPayload.TrainingState(
                TrainingState(ftp = 250.0, cp = 260.0, wprime = 15000.0, ctl = 60.0, atl = 70.0, tsb = -10.0, decoupling = null),
            ),
        )
        val freshSnapshot = CloudSnapshot(
            route = CloudRoute.Dashboard,
            revision = 2,
            items = listOf(freshItem),
            source = CloudSnapshot.Source.network,
            asOf = 2000L,
        )

        val session = FakeReadSession(
            cachedSnapshots = mapOf(CloudRoute.Dashboard to cachedSnapshot),
            loadHandler = { route ->
                if (route == CloudRoute.Dashboard) freshSnapshot else CloudSnapshot(route, 1, emptyList(), CloudSnapshot.Source.network, 2000L)
            },
        )

        val vm = DashboardViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()

        val state = vm.uiState.value
        assertFalse(state.isStarting)
        assertTrue(state.isPaired)
        assertNotNull(state.dashboardData)
        assertEquals(250.0, state.dashboardData?.currentFTP!!, 1e-9)
        assertEquals(CloudSnapshot.Source.network, state.dashboardData?.source)
    }

    @Test
    fun recentRidesSortedNewestFirst() = runTest(testDispatcher) {
        val oldActivity = CloudItem(
            id = "activity-999",
            kind = CloudKind.Activity,
            revision = 1,
            payload = CloudPayload.Activity(
                ActivitySummary(
                    id = 999.0, startTime = "2024-05-01T10:00:00", durationS = 3600.0,
                    distanceM = 20000.0, avgPower = 180.0, avgHr = 130.0, np = 190.0,
                    intensityFactor = 0.75, tss = 40.0, rpe = null,
                ),
            ),
        )
        val newActivity = CloudItem(
            id = "activity-100",
            kind = CloudKind.Activity,
            revision = 1,
            payload = CloudPayload.Activity(
                ActivitySummary(
                    id = 100.0, startTime = "2024-05-10T10:00:00", durationS = 3600.0,
                    distanceM = 25000.0, avgPower = 200.0, avgHr = 140.0, np = 210.0,
                    intensityFactor = 0.85, tss = 55.0, rpe = null,
                ),
            ),
        )

        val activitiesSnapshot = CloudSnapshot(
            route = CloudRoute.Activities,
            revision = 1,
            // Out of order: older activity first in list
            items = listOf(oldActivity, newActivity),
            source = CloudSnapshot.Source.network,
            asOf = 2000L,
        )

        val session = FakeReadSession(
            loadHandler = { route ->
                if (route == CloudRoute.Activities) activitiesSnapshot else CloudSnapshot(route, 1, emptyList(), CloudSnapshot.Source.network, 2000L)
            },
        )

        val vm = DashboardViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()

        val state = vm.uiState.value
        val acts = state.dashboardData?.activities
        assertNotNull(acts)
        assertEquals(2, acts?.size)
        // Newest ride first!
        assertEquals(100.0, acts?.get(0)?.id!!, 1e-9)
        assertEquals("2024-05-10T10:00:00", acts?.get(0)?.startTime)
        assertEquals(999.0, acts?.get(1)?.id!!, 1e-9)
        assertEquals("2024-05-01T10:00:00", acts?.get(1)?.startTime)
    }

    @Test
    fun notPairedFailureTransitionsToUnpairedState() = runTest(testDispatcher) {
        val session = FakeReadSession(
            loadHandler = { throw CloudSession.Failure.NotPaired() },
        )

        val vm = DashboardViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()

        val state = vm.uiState.value
        assertFalse(state.isStarting)
        assertEquals(CloudSession.DeviceState.unpaired, state.deviceState)
        assertFalse(state.isPaired)
        assertNull(state.dashboardData)
    }

    @Test
    fun deviceAlreadyRemovedAtStartRendersRemovedCard() = runTest(testDispatcher) {
        val session = FakeReadSession(
            deviceState = CloudSession.DeviceState.removed,
            isPaired = false,
        )

        val vm = DashboardViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()

        val state = vm.uiState.value
        assertFalse(state.isStarting)
        assertEquals(CloudSession.DeviceState.removed, state.deviceState)
        assertFalse(state.isPaired)
        assertNull(state.dashboardData)
    }

    @Test
    fun revocationExceptionTransitionToRemovedState() = runTest(testDispatcher) {
        val session = FakeReadSession(
            loadHandler = { throw CloudSession.Failure.DeviceRemoved() },
        )

        val vm = DashboardViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()

        val state = vm.uiState.value
        assertFalse(state.isStarting)
        assertEquals(CloudSession.DeviceState.removed, state.deviceState)
        assertFalse(state.isPaired)
        assertNull(state.dashboardData)
    }

    private fun activityItem(id: Int, startTime: String) = CloudItem(
        id = "activity-$id",
        kind = CloudKind.Activity,
        revision = 1,
        payload = CloudPayload.Activity(
            ActivitySummary(
                id = id.toDouble(), startTime = startTime, durationS = 3600.0,
                distanceM = 20000.0, avgPower = 180.0, avgHr = 130.0, np = 190.0,
                intensityFactor = 0.75, tss = 40.0, rpe = null,
            ),
        ),
    )

    private fun trainingSnapshot(ftp: Double) = CloudSnapshot(
        route = CloudRoute.Dashboard,
        revision = 1,
        items = listOf(
            CloudItem(
                id = "training-state",
                kind = CloudKind.TrainingState,
                revision = 1,
                payload = CloudPayload.TrainingState(
                    TrainingState(ftp = ftp, cp = null, wprime = null, ctl = 60.0, atl = 70.0, tsb = -10.0, decoupling = null),
                ),
            ),
        ),
        source = CloudSnapshot.Source.network,
        asOf = 2000L,
    )

    @Test
    fun constructionDoesNotLoad() = runTest(testDispatcher) {
        // The screen's ON_RESUME is the first load; a load in init as well
        // would run two full dashboard walks on every cold start.
        val session = FakeReadSession()

        val vm = DashboardViewModel(session, coroutineScope = backgroundScope)

        assertTrue(session.loads.isEmpty())
        assertTrue(vm.uiState.value.isStarting)
    }

    @Test
    fun everyRefreshRefetchesActivitiesSoANewRideAppears() = runTest(testDispatcher) {
        // The cloud dashboard route carries no activities, so the strip comes
        // from the Activities route. Rides already on screen must not stop a
        // later refresh from fetching it again.
        var published = listOf(activityItem(1, "2024-05-01T10:00:00"))
        val session = FakeReadSession(
            loadHandler = { route ->
                when (route) {
                    CloudRoute.Dashboard -> trainingSnapshot(250.0)
                    CloudRoute.Activities ->
                        CloudSnapshot(route, 1, published, CloudSnapshot.Source.network, 2000L)
                    else -> CloudSnapshot(route, 1, emptyList(), CloudSnapshot.Source.network, 2000L)
                }
            },
        )
        val vm = DashboardViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()
        assertEquals(listOf(1.0), vm.uiState.value.dashboardData?.activities?.map { it.id })

        published = published + activityItem(2, "2024-05-10T10:00:00")
        vm.refresh()

        assertEquals(2, session.loads.count { it == CloudRoute.Activities })
        assertEquals(listOf(2.0, 1.0), vm.uiState.value.dashboardData?.activities?.map { it.id })
    }

    @Test
    fun ridesStayOnScreenWhileTheActivitiesFetchRuns() = runTest(testDispatcher) {
        val gate = CompletableDeferred<CloudSnapshot>()
        var activityLoads = 0
        val session = FakeReadSession(
            loadHandler = { route ->
                when (route) {
                    CloudRoute.Dashboard -> trainingSnapshot(250.0)
                    CloudRoute.Activities -> {
                        activityLoads += 1
                        if (activityLoads == 1) {
                            CloudSnapshot(route, 1, listOf(activityItem(1, "2024-05-01T10:00:00")), CloudSnapshot.Source.network, 2000L)
                        } else {
                            gate.await()
                        }
                    }
                    else -> CloudSnapshot(route, 1, emptyList(), CloudSnapshot.Source.network, 2000L)
                }
            },
        )
        val vm = DashboardViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()

        vm.refresh() // parks on the second Activities fetch

        assertFalse(vm.uiState.value.isLoading)
        assertEquals(listOf(1.0), vm.uiState.value.dashboardData?.activities?.map { it.id })
        gate.complete(CloudSnapshot(CloudRoute.Activities, 2, emptyList(), CloudSnapshot.Source.network, 3000L))
        // A withdrawn ride disappears: the new list replaces the kept one.
        assertEquals(emptyList<Double>(), vm.uiState.value.dashboardData?.activities?.map { it.id })
    }

    @Test
    fun refreshAfterPairingInSettingsLoadsTheDashboard() = runTest(testDispatcher) {
        val session = FakeReadSession(
            deviceState = CloudSession.DeviceState.unpaired,
            isPaired = false,
            loadHandler = { route ->
                if (route == CloudRoute.Dashboard) trainingSnapshot(250.0) else CloudSnapshot(route, 1, emptyList(), CloudSnapshot.Source.network, 2000L)
            },
        )
        val vm = DashboardViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()
        assertFalse(vm.uiState.value.isPaired)

        // The rider pairs in Settings and comes back: ON_RESUME refreshes.
        session.deviceState = CloudSession.DeviceState.paired
        session.isPaired = true
        vm.refresh()

        assertTrue(vm.uiState.value.isPaired)
        assertEquals(250.0, vm.uiState.value.dashboardData?.currentFTP!!, 1e-9)
    }

    @Test
    fun refreshAfterSignOutInSettingsClearsTheDashboard() = runTest(testDispatcher) {
        val session = FakeReadSession(
            loadHandler = { route ->
                if (route == CloudRoute.Dashboard) trainingSnapshot(250.0) else CloudSnapshot(route, 1, emptyList(), CloudSnapshot.Source.network, 2000L)
            },
        )
        val vm = DashboardViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()
        assertNotNull(vm.uiState.value.dashboardData)

        session.deviceState = CloudSession.DeviceState.unpaired
        session.isPaired = false
        vm.refresh()

        assertFalse(vm.uiState.value.isPaired)
        assertNull(vm.uiState.value.dashboardData)
    }

    @Test
    fun aNewRefreshSupersedesTheOneInFlight() = runTest(testDispatcher) {
        // CloudSession.load serves the cache when its network step is
        // cancelled, so a superseded load can still return normally. Its
        // result must not overwrite the newer one.
        val network = CompletableDeferred<Unit>()
        val staleReturns = CompletableDeferred<Unit>()
        var dashboardLoads = 0
        val session = FakeReadSession(
            loadHandler = { route ->
                if (route == CloudRoute.Dashboard) {
                    dashboardLoads += 1
                    if (dashboardLoads == 1) {
                        try {
                            network.await()
                        } catch (_: CancellationException) {
                            // Swallow the cancellation, as CloudSession.load
                            // does, and return only after the newer load has
                            // finished.
                            withContext(NonCancellable) { staleReturns.await() }
                        }
                        trainingSnapshot(200.0)
                    } else {
                        trainingSnapshot(300.0)
                    }
                } else {
                    CloudSnapshot(route, 1, emptyList(), CloudSnapshot.Source.network, 2000L)
                }
            },
        )
        val vm = DashboardViewModel(session, coroutineScope = backgroundScope)
        vm.refresh() // parks on `network`
        vm.refresh() // cancels it and loads 300 W

        assertEquals(300.0, vm.uiState.value.dashboardData?.currentFTP!!, 1e-9)
        staleReturns.complete(Unit) // the superseded load now returns 200 W
        assertEquals(300.0, vm.uiState.value.dashboardData?.currentFTP!!, 1e-9)
    }

    @Test
    fun throttledExceptionShowsRateLimitMessage() = runTest(testDispatcher) {
        val session = FakeReadSession(
            loadHandler = { throw CloudSession.Failure.Throttled(30.0) },
        )

        val vm = DashboardViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()

        val state = vm.uiState.value
        assertFalse(state.isLoading)
        assertEquals(30, state.throttledRetrySeconds)
        assertNull(state.error)
    }
}
