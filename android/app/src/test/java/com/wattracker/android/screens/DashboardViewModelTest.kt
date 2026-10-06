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
import com.wattracker.android.cloud.LocalClientException
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
        override var identity: Int = 1,
        override var lastSuccess: Long? = 1000L,
        override var mayBeWaking: Boolean = false,
        private val cachedSnapshots: Map<CloudRoute, CloudSnapshot> = emptyMap(),
        private val bumpIdentityOnCachedRead: Boolean = false,
        private val loadHandler: suspend (CloudRoute) -> CloudSnapshot = { route ->
            CloudSnapshot(route, 1, emptyList(), CloudSnapshot.Source.network, 2000L)
        },
        private val retryNowIfWakingHandler: suspend () -> Unit = {},
    ) : ReadSession {
        /** Every `load` call, in order, so a test can count network walks. */
        val loads = mutableListOf<CloudRoute>()

        override suspend fun cached(route: CloudRoute): CloudSnapshot? {
            // A concurrent sign-out landing mid-read: the identity changes
            // while the rows are being read, before the wipe reaches the disk.
            if (bumpIdentityOnCachedRead) identity += 1
            return cachedSnapshots[route]
        }
        override suspend fun load(route: CloudRoute): CloudSnapshot {
            loads.add(route)
            return loadHandler(route)
        }
        override suspend fun activityDetail(activityId: Int): ActivityDetail = throw NotImplementedError()
        override suspend fun activityStreams(activityId: Int): ActivityStreams = throw NotImplementedError()

        override suspend fun retryNowIfWaking() = retryNowIfWakingHandler()
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
    fun aChangedIdentitySupersedesTheLoadInFlight() = runTest(testDispatcher) {
        // CloudSession.load serves the cache when its network step is
        // cancelled, so a superseded load can still return normally. Its
        // result must not overwrite the newer one. A pairing change is what
        // supersedes: the identity differs, so the in-flight load is cancelled
        // and restarted, as before.
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
        session.identity += 1 // the rider paired a different device in Settings
        vm.refresh() // cancels it and loads 300 W

        assertEquals(300.0, vm.uiState.value.dashboardData?.currentFTP!!, 1e-9)
        staleReturns.complete(Unit) // the superseded load now returns 200 W
        assertEquals(300.0, vm.uiState.value.dashboardData?.currentFTP!!, 1e-9)
    }

    @Test
    fun aSameIdentityResumeJoinsTheWalkInFlight() = runTest(testDispatcher) {
        // A rotation, a tab switch, or a return from Settings that changed
        // nothing resumes the screen while the walk is still in flight under
        // the same identity. The in-flight load must finish and paint, not
        // restart the fifteen-page walk from page one.
        val network = CompletableDeferred<Unit>()
        var dashboardLoads = 0
        val session = FakeReadSession(
            loadHandler = { route ->
                if (route == CloudRoute.Dashboard) {
                    dashboardLoads += 1
                    network.await() // park on the walk
                    trainingSnapshot(250.0)
                } else {
                    CloudSnapshot(route, 1, emptyList(), CloudSnapshot.Source.network, 2000L)
                }
            },
        )
        val vm = DashboardViewModel(session, coroutineScope = backgroundScope)
        vm.refresh() // starts the walk, parks on `network`
        vm.refresh() // ON_RESUME under the same identity: join
        vm.refresh() // and join again
        assertEquals("the walk is not restarted", 1, dashboardLoads)
        network.complete(Unit)
        assertEquals(250.0, vm.uiState.value.dashboardData?.currentFTP!!, 1e-9)
    }

    @Test
    fun aSignOutDuringTheCachedReadIsNotPainted() = runTest(testDispatcher) {
        // The sign-out's disk wipe is still in flight when the cache-first
        // read lands: the rows are the old identity's. The identity changed,
        // so the cached data must not paint. The network step is parked on a
        // gate, so the assertion is on the screen *while* the load is in
        // flight -- not just the final state, which the sign-out clears either
        // way.
        val network = CompletableDeferred<Unit>()
        val cachedItem = CloudItem(
            id = "ts-1",
            kind = CloudKind.TrainingState,
            revision = 1,
            payload = CloudPayload.TrainingState(
                TrainingState(ftp = 100.0, cp = null, wprime = null, ctl = 50.0, atl = 55.0, tsb = -5.0, decoupling = null),
            ),
        )
        val session = FakeReadSession(
            cachedSnapshots = mapOf(
                CloudRoute.Dashboard to CloudSnapshot(
                    CloudRoute.Dashboard, 1, listOf(cachedItem), CloudSnapshot.Source.cache, 1000L,
                ),
            ),
            bumpIdentityOnCachedRead = true,
            loadHandler = {
                network.await()
                throw CloudSession.Failure.NotPaired()
            },
        )
        val vm = DashboardViewModel(session, coroutineScope = backgroundScope)
        vm.refresh() // runs to the network step, which parks on `network`

        // The old identity's cache did not paint: with the load parked, the
        // screen shows no data, not the cached 100 W.
        assertTrue(vm.uiState.value.isLoading)
        assertNull("the stale cache must not paint", vm.uiState.value.dashboardData)

        network.complete(Unit)
        assertFalse(vm.uiState.value.isPaired)
        assertNull(vm.uiState.value.dashboardData)
    }

    @Test
    fun aLocal429WithRetryAfterShowsRateLimitMessage() = runTest(testDispatcher) {
        // The local backend's 429 carries `Retry-After` on its own exception;
        // it earns the same rate-limit notice the cloud's throttle does.
        val session = FakeReadSession(
            loadHandler = { throw LocalClientException.Http(429, "/api/state", 30.0) },
        )
        val vm = DashboardViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()
        val state = vm.uiState.value
        assertFalse(state.isLoading)
        assertEquals(30, state.throttledRetrySeconds)
        assertNull(state.error)
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

    @Test
    fun aWakingFailureShowsTheWakingNoticeWithNoError() = runTest(testDispatcher) {
        // A scaled-to-zero server the refresh rides out and still cannot reach
        // is "waking," not a generic failure: it shows the waking notice, not a
        // red error card, and its (hardcoded) message does not duplicate it.
        val session = FakeReadSession(
            loadHandler = { throw CloudSession.Failure.Waking() },
        )
        val vm = DashboardViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()
        val state = vm.uiState.value
        assertFalse(state.isLoading)
        assertTrue(state.isWaking)
        assertNull(state.error)
        assertNull(state.dashboardData)
        assertTrue(state.isPaired) // waking is not a pairing failure
    }

    @Test
    fun aRePairClearsThePreviousIdentitysTiles() = runTest(testDispatcher) {
        // The rider re-paired a different device in Settings: the tiles on
        // screen are the previous rider's. The new identity's load must clear
        // them -- even when it fails (here, the new cloud is waking) -- instead
        // of leaving the old data up under "Syncing."
        var failing = false
        val session = FakeReadSession(
            loadHandler = { route ->
                if (route == CloudRoute.Dashboard) {
                    if (failing) throw CloudSession.Failure.Waking() else trainingSnapshot(250.0)
                } else {
                    CloudSnapshot(route, 1, emptyList(), CloudSnapshot.Source.network, 2000L)
                }
            },
        )
        val vm = DashboardViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()
        assertNotNull(vm.uiState.value.dashboardData)

        // The rider re-pairs a different device; its first load fails waking.
        failing = true
        session.identity += 1
        vm.refresh()

        val state = vm.uiState.value
        assertNull("the previous rider's tiles are cleared", state.dashboardData)
        assertTrue(state.isWaking)
    }

    @Test
    fun aPullToRefreshLiftsTheWakingGateAndRerunsTheWalk() = runTest(testDispatcher) {
        // The rider pulls to refresh: the gate a waking server set is lifted
        // (`retryNowIfWaking`), and the walk re-runs from scratch -- a manual
        // refresh is never joined.
        val network = CompletableDeferred<Unit>()
        var lifts = 0
        var dashboardLoads = 0
        val session = FakeReadSession(
            mayBeWaking = true,
            loadHandler = { route ->
                if (route == CloudRoute.Dashboard) {
                    dashboardLoads += 1
                    network.await()
                    trainingSnapshot(250.0)
                } else {
                    CloudSnapshot(route, 1, emptyList(), CloudSnapshot.Source.network, 2000L)
                }
            },
            retryNowIfWakingHandler = { lifts += 1 },
        )
        val vm = DashboardViewModel(session, coroutineScope = backgroundScope)
        vm.pullToRefresh()
        assertEquals("the pull lifted the waking gate", 1, lifts)
        assertEquals("the walk re-ran", 1, dashboardLoads)
        assertTrue(vm.uiState.value.isRefreshing)

        network.complete(Unit)
        assertFalse(vm.uiState.value.isRefreshing)
        assertEquals(250.0, vm.uiState.value.dashboardData?.currentFTP!!, 1e-9)
    }
}
