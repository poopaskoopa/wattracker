package com.wattracker.android.screens.volume

import com.wattracker.android.cloud.CloudItem
import com.wattracker.android.cloud.CloudKind
import com.wattracker.android.cloud.CloudPayload
import com.wattracker.android.cloud.CloudRoute
import com.wattracker.android.cloud.CloudSession
import com.wattracker.android.cloud.CloudSnapshot
import com.wattracker.android.cloud.LocalClientException
import com.wattracker.android.cloud.ReadSession
import com.wattracker.android.cloud.VolumeWeek
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Before
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class VolumeViewModelTest {

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
        private val cachedSnapshot: CloudSnapshot? = null,
        private val loadHandler: suspend (CloudRoute) -> CloudSnapshot = { route ->
            CloudSnapshot(route, 1, emptyList(), CloudSnapshot.Source.network, 2000L)
        },
    ) : ReadSession {
        var loadCalls = 0
        var retryCalls = 0

        override suspend fun cached(route: CloudRoute): CloudSnapshot? =
            if (route == CloudRoute.Volume) cachedSnapshot else null

        override suspend fun load(route: CloudRoute): CloudSnapshot {
            loadCalls += 1
            return loadHandler(route)
        }

        override suspend fun activityDetail(activityId: Int) = throw NotImplementedError()
        override suspend fun activityStreams(activityId: Int) = throw NotImplementedError()
        override suspend fun retryNowIfWaking() {
            retryCalls += 1
        }
    }

    private fun weekItem(weekStart: String, hours: Double = 2.0): CloudItem = CloudItem(
        id = "volume-week-$weekStart",
        kind = CloudKind.VolumeWeek,
        revision = 1,
        payload = CloudPayload.VolumeWeek(VolumeWeek(weekStart, hours, 60.0, 30.0, 500.0)),
    )

    private fun volumeSnapshot(
        weeks: List<CloudItem>,
        source: CloudSnapshot.Source = CloudSnapshot.Source.network,
    ) = CloudSnapshot(CloudRoute.Volume, 1, weeks, source, 2000L)

    @Test
    fun cachePaintsBeforeTheNetwork() = runTest(testDispatcher) {
        val session = FakeReadSession(
            cachedSnapshot = volumeSnapshot(listOf(weekItem("2026-05-04")), source = CloudSnapshot.Source.cache),
            loadHandler = { volumeSnapshot(listOf(weekItem("2026-05-04"), weekItem("2026-05-11"))) },
        )
        val vm = VolumeViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()
        val state = vm.uiState.value
        assertEquals(2, state.data?.weeks?.size)
        assertEquals(CloudSnapshot.Source.network, state.data?.source)
    }

    @Test
    fun anOrdinaryFailureKeepsTheCachedWeeks() = runTest(testDispatcher) {
        val session = FakeReadSession(
            cachedSnapshot = volumeSnapshot(listOf(weekItem("2026-05-04")), source = CloudSnapshot.Source.cache),
            loadHandler = { throw CloudSession.Failure.Offline() },
        )
        val vm = VolumeViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()
        val state = vm.uiState.value
        assertNotNull(state.data)
        assertEquals(CloudSnapshot.Source.cache, state.data?.source)
        assertEquals("No connection", state.error)
    }

    @Test
    fun aSnapshotWithNoWeeksIsTheNoDataStateNotAnEmptyChart() = runTest(testDispatcher) {
        val session = FakeReadSession(loadHandler = { volumeSnapshot(emptyList()) })
        val vm = VolumeViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()
        assertNull(vm.uiState.value.data)
        assertNull(vm.uiState.value.error)
    }

    @Test
    fun removedClearsTheWeeks() = runTest(testDispatcher) {
        val session = FakeReadSession(
            cachedSnapshot = volumeSnapshot(listOf(weekItem("2026-05-04")), source = CloudSnapshot.Source.cache),
            loadHandler = { throw CloudSession.Failure.DeviceRemoved() },
        )
        val vm = VolumeViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()
        val state = vm.uiState.value
        assertEquals(CloudSession.DeviceState.removed, state.deviceState)
        assertNull(state.data)
    }

    @Test
    fun unpairedAsksForNothing() = runTest(testDispatcher) {
        val session = FakeReadSession(
            deviceState = CloudSession.DeviceState.unpaired,
            isPaired = false,
            loadHandler = { throw AssertionError("an unpaired device must not be asked to read") },
        )
        val vm = VolumeViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()
        assertEquals(0, session.loadCalls)
    }

    @Test
    fun selectionsSurviveARefresh() = runTest(testDispatcher) {
        val session = FakeReadSession(loadHandler = { volumeSnapshot(listOf(weekItem("2026-05-04"))) })
        val vm = VolumeViewModel(session, coroutineScope = backgroundScope)
        vm.selectMetric(VolumeMetric.Tss)
        vm.selectRange(VolumeRange.YearToDate)
        vm.refresh()
        assertEquals(VolumeMetric.Tss, vm.uiState.value.selectedMetric)
        assertEquals(VolumeRange.YearToDate, vm.uiState.value.selectedRange)
    }

    @Test
    fun pullToRefreshLiftsTheWakingGateOnlyWhenUserInitiated() = runTest(testDispatcher) {
        val session = FakeReadSession(mayBeWaking = true)
        val vm = VolumeViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()
        assertEquals(0, session.retryCalls)
        vm.pullToRefresh()
        assertEquals(1, session.retryCalls)
    }

    @Test
    fun aLocalThrottleEarnsTheRetryNotice() = runTest(testDispatcher) {
        val session = FakeReadSession(
            loadHandler = { throw LocalClientException.Http(status = 503, path = "/api/volume", retryAfter = 30.0) },
        )
        val vm = VolumeViewModel(session, coroutineScope = backgroundScope)
        vm.refresh()
        val state = vm.uiState.value
        assertEquals(30, state.throttledRetrySeconds)
        assertNull(state.error)
    }
}
