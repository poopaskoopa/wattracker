package com.wattracker.android.screens

import com.wattracker.android.cloud.CloudItem
import com.wattracker.android.cloud.CloudKind
import com.wattracker.android.cloud.CloudPayload
import com.wattracker.android.cloud.CloudRoute
import com.wattracker.android.cloud.CloudSnapshot
import com.wattracker.android.cloud.CurvePoint
import com.wattracker.android.cloud.LoadPoint
import com.wattracker.android.cloud.PowerCurve
import com.wattracker.android.cloud.RiderProfile
import com.wattracker.android.cloud.TrainingState
import com.wattracker.android.screens.dashboard.DashboardData
import com.wattracker.android.screens.dashboard.LoadWindow
import com.wattracker.android.screens.dashboard.loadAxisRange
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class DashboardDataTest {

    @Test
    fun formatNumberFormatsDecimalsAndIntegers() {
        assertEquals("250 W", DashboardData.formatNumber(250.0, " W"))
        assertEquals("250.5 W", DashboardData.formatNumber(250.5, " W"))
        assertEquals("—", DashboardData.formatNumber(null))
        assertEquals("—", DashboardData.formatNumber(Double.NaN))
    }

    @Test
    fun fromSnapshotExtractsProfileAndTrainingState() {
        val items = listOf(
            CloudItem(
                id = "profile",
                kind = CloudKind.Profile,
                revision = 1,
                payload = CloudPayload.Profile(
                    RiderProfile(
                        displayName = "rider",
                        ftp = 250.0,
                        ftpWatts = null,
                        power = null,
                        heartRate = null,
                        weightKg = 70.0,
                        weightDate = "2024-05-01",
                        weightSource = "garmin",
                    ),
                ),
            ),
            CloudItem(
                id = "training-state",
                kind = CloudKind.TrainingState,
                revision = 1,
                payload = CloudPayload.TrainingState(
                    TrainingState(
                        ftp = 255.0,
                        cp = 260.0,
                        wprime = 15000.0,
                        ctl = 60.0,
                        atl = 70.0,
                        tsb = -10.0,
                        decoupling = null,
                    ),
                ),
            ),
            CloudItem(
                id = "curve",
                kind = CloudKind.Curve,
                revision = 1,
                payload = CloudPayload.Curve(
                    PowerCurve(
                        measured = listOf(CurvePoint(t = 60.0, power = 400.0)),
                        allTime = emptyList(),
                        lastRide = null,
                        model = emptyList(),
                        cp = 260.0,
                        wprime = 15000.0,
                    ),
                ),
            ),
            CloudItem(
                id = "load-1",
                kind = CloudKind.LoadPoint,
                revision = 1,
                payload = CloudPayload.LoadPoint(
                    LoadPoint(date = "2024-05-01", tss = 50.0, ctl = 58.0, atl = 65.0, tsb = -7.0),
                ),
            ),
        )

        val snapshot = CloudSnapshot(
            route = CloudRoute.Dashboard,
            revision = 1,
            items = items,
            source = CloudSnapshot.Source.network,
            asOf = 1000L,
        )

        val data = DashboardData.fromSnapshot(snapshot)

        assertTrue(data.hasAnyData)
        assertEquals(255.0, data.currentFTP!!, 1e-9)
        assertEquals(260.0, data.currentCP!!, 1e-9)
        assertEquals(15000.0, data.currentWPrime!!, 1e-9)
        assertEquals(70.0, data.weightKg!!, 1e-9)
        assertEquals(60.0, data.currentCTL!!, 1e-9)
        assertEquals(70.0, data.currentATL!!, 1e-9)
        assertEquals(-10.0, data.currentTSB!!, 1e-9)
        assertEquals(1, data.loadPoints.size)
    }

    @Test
    fun latestLoadPointSelectsNewestDatedRecord() {
        val items = listOf(
            CloudItem(
                id = "load-1",
                kind = CloudKind.LoadPoint,
                revision = 1,
                payload = CloudPayload.LoadPoint(
                    LoadPoint(date = "2024-05-01", tss = 50.0, ctl = 50.0, atl = 50.0, tsb = 0.0),
                ),
            ),
            CloudItem(
                id = "load-2",
                kind = CloudKind.LoadPoint,
                revision = 1,
                payload = CloudPayload.LoadPoint(
                    LoadPoint(date = "2024-05-10", tss = 60.0, ctl = 55.0, atl = 60.0, tsb = -5.0),
                ),
            ),
            CloudItem(
                id = "load-3",
                kind = CloudKind.LoadPoint,
                revision = 1,
                payload = CloudPayload.LoadPoint(
                    LoadPoint(date = "2024-05-05", tss = 55.0, ctl = 52.0, atl = 55.0, tsb = -3.0),
                ),
            ),
        )

        val snapshot = CloudSnapshot(
            route = CloudRoute.Dashboard,
            revision = 1,
            items = items,
            source = CloudSnapshot.Source.network,
            asOf = 1000L,
        )

        val data = DashboardData.fromSnapshot(snapshot)
        val latest = data.latestLoadPoint

        assertNotNull(latest)
        assertEquals("2024-05-10", latest?.date)
        assertEquals(55.0, latest?.ctl!!, 1e-9)
    }

    @Test
    fun pointsForWindowFiltersRelativeToNewestPoint() {
        val items = listOf(
            CloudItem(
                id = "load-1",
                kind = CloudKind.LoadPoint,
                revision = 1,
                payload = CloudPayload.LoadPoint(
                    LoadPoint(date = "2024-01-01", tss = 50.0, ctl = 50.0, atl = 50.0, tsb = 0.0),
                ),
            ),
            CloudItem(
                id = "load-2",
                kind = CloudKind.LoadPoint,
                revision = 1,
                payload = CloudPayload.LoadPoint(
                    LoadPoint(date = "2024-05-01", tss = 60.0, ctl = 55.0, atl = 60.0, tsb = -5.0),
                ),
            ),
            CloudItem(
                id = "load-3",
                kind = CloudKind.LoadPoint,
                revision = 1,
                payload = CloudPayload.LoadPoint(
                    LoadPoint(date = "2024-05-15", tss = 65.0, ctl = 58.0, atl = 62.0, tsb = -4.0),
                ),
            ),
        )

        val snapshot = CloudSnapshot(
            route = CloudRoute.Dashboard,
            revision = 1,
            items = items,
            source = CloudSnapshot.Source.network,
            asOf = 1000L,
        )

        val data = DashboardData.fromSnapshot(snapshot)
        val pointsSixWeeks = data.points(LoadWindow.SIX_WEEKS)

        // 2024-05-01 and 2024-05-15 are within 42 days of 2024-05-15; 2024-01-01 is not.
        assertEquals(2, pointsSixWeeks.size)
        assertEquals("2024-05-01", pointsSixWeeks[0].date)
        assertEquals("2024-05-15", pointsSixWeeks[1].date)
    }

    @Test
    fun loadAxisIncludesTssSoBarsStayInsideTheChart() {
        // A hard day's TSS is far above CTL/ATL; before the bars shared the
        // axis they were drawn past the top of the canvas.
        val axis = loadAxisRange(
            listOf(
                LoadPoint(date = "2024-05-01", tss = 250.0, ctl = 60.0, atl = 70.0, tsb = -20.0),
                LoadPoint(date = "2024-05-02", tss = null, ctl = 61.0, atl = 65.0, tsb = -4.0),
            ),
        )
        assertEquals(-20.0, axis!!.start, 1e-9)
        assertEquals(250.0, axis.endInclusive, 1e-9)
    }

    @Test
    fun loadAxisAlwaysContainsZeroAndIsNullWhenNothingPlots() {
        val axis = loadAxisRange(listOf(LoadPoint(date = "2024-05-01", tss = null, ctl = 40.0, atl = 45.0, tsb = null)))
        assertEquals(0.0, axis!!.start, 1e-9)
        assertEquals(45.0, axis.endInclusive, 1e-9)
        assertNull(loadAxisRange(listOf(LoadPoint(date = "2024-05-01", tss = null, ctl = null, atl = null, tsb = Double.NaN))))
    }

    @Test
    fun hasAnyDataReturnsFalseForEmptySnapshot() {
        val snapshot = CloudSnapshot(
            route = CloudRoute.Dashboard,
            revision = 1,
            items = emptyList(),
            source = CloudSnapshot.Source.network,
            asOf = 1000L,
        )

        val data = DashboardData.fromSnapshot(snapshot)
        assertFalse(data.hasAnyData)
        assertNull(data.currentFTP)
        assertNull(data.latestLoadPoint)
    }
}
