package com.wattracker.android.screens.calendar

import com.wattracker.android.cloud.ActivitySummary
import com.wattracker.android.cloud.CalendarDay
import com.wattracker.android.cloud.CalendarMonth
import com.wattracker.android.cloud.CloudItem
import com.wattracker.android.cloud.CloudKind
import com.wattracker.android.cloud.CloudPayload
import com.wattracker.android.cloud.CloudRoute
import com.wattracker.android.cloud.CloudSnapshot
import com.wattracker.android.cloud.RiderProfile
import com.wattracker.android.cloud.TrainingState
import com.wattracker.android.json.JsonValue
import com.wattracker.android.json.opt
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class CalendarDataTest {

    private fun dayItem(
        id: String,
        date: String?,
        workouts: List<JsonValue> = emptyList(),
        activities: List<JsonValue> = emptyList(),
        race: JsonValue? = null,
        ooto: Boolean? = null,
        phase: String? = null,
        part: Int? = null,
        parts: Int? = null,
        deleted: Boolean = false,
    ): CloudItem = CloudItem(
        id = id,
        kind = CloudKind.CalendarDay,
        revision = 1,
        deleted = deleted,
        payload = if (deleted) CloudPayload.Tombstone else CloudPayload.CalendarDay(
            CalendarDay(
                date = date,
                ooto = ooto,
                phase = phase,
                race = race,
                workouts = workouts,
                activities = activities,
                part = part,
                parts = parts,
            ),
        ),
    )

    private fun workout(id: Double, name: String, completedActivityId: Double? = null, missed: Boolean = false): JsonValue {
        val fields = LinkedHashMap<String, JsonValue>()
        fields["id"] = JsonValue.Number(id)
        fields["name"] = JsonValue.String(name)
        if (completedActivityId != null) fields["completed_activity_id"] = JsonValue.Number(completedActivityId)
        if (missed) fields["missed"] = JsonValue.Bool(true)
        return JsonValue.Object(fields)
    }

    private fun activityRow(id: Double, startTime: String): JsonValue = JsonValue.Object(
        linkedMapOf(
            "id" to JsonValue.Number(id),
            "start_time" to JsonValue.String(startTime),
            "duration_s" to JsonValue.Number(3600.0),
        ),
    )

    private fun calendarSnapshot(items: List<CloudItem>): CloudSnapshot =
        CloudSnapshot(CloudRoute.Calendar, 1, items, CloudSnapshot.Source.network, 2000L)

    @Test
    fun splitDaysMergeOnTheirDate() {
        // A day too large for one object arrives as parts; the rider's day
        // must show the union, not the part that happened to arrive last.
        val data = CalendarData(
            calendarSnapshot(
                listOf(
                    dayItem("calendar-day-2026-05-04-part-1", "2026-05-04", workouts = listOf(workout(1.0, "Waake")), part = 1, parts = 2),
                    dayItem("calendar-day-2026-05-04-part-2", "2026-05-04", workouts = listOf(workout(2.0, "Intervals")), activities = listOf(activityRow(9.0, "2026-05-04T07:00:00")), part = 2, parts = 2),
                ),
            ),
        )
        assertEquals(1, data.days.size)
        val day = data.days.single()
        assertEquals(2, day.workoutCount)
        assertEquals(1, day.activityCount)
        assertTrue(day.hasContent)
    }

    @Test
    fun tombstonesAndUnreadableDatesAreAbsent() {
        val data = CalendarData(
            calendarSnapshot(
                listOf(
                    dayItem("calendar-day-2026-05-05", "2026-05-05", deleted = true),
                    dayItem("calendar-day-garbage", "2026-13-99", workouts = listOf(workout(1.0, "Ghost"))),
                    dayItem("calendar-day-2026-05-06", "2026-05-06", workouts = listOf(workout(2.0, "Real"))),
                ),
            ),
        )
        assertEquals(1, data.days.size)
        assertEquals("2026-05-06", data.days.single().dateISO)
        assertNull(data.entry("2026-05-05"))
    }

    @Test
    fun linkedRidesComeFromTheActivitiesCollectionExactlyOnce() {
        val dayWithLink = dayItem(
            "calendar-day-2026-05-11",
            "2026-05-11",
            workouts = listOf(workout(3.0, "Endurance", completedActivityId = 7.0)),
        )
        val summary = ActivitySummary(
            id = 7.0, startTime = "2026-05-11T06:30:00", durationS = 5400.0, distanceM = 61000.0,
            avgPower = 190.0, avgHr = 138.0, np = 205.0, intensityFactor = 0.82, tss = 74.0, rpe = 5.0,
        )
        val activitiesSnapshot = CloudSnapshot(
            CloudRoute.Activities, 1,
            listOf(CloudItem("activity-7", CloudKind.Activity, 1, payload = CloudPayload.Activity(summary))),
            CloudSnapshot.Source.network, 2000L,
        )

        val data = CalendarData(calendarSnapshot(listOf(dayWithLink)), activitiesSnapshot)
        val day = data.days.single()
        assertEquals(1, day.activityCount)
        val row = day.activities.single()
        assertEquals(7, CalendarData.activityID(row))
        assertEquals("2026-05-11T06:30:00", (row.opt("start_time") as? JsonValue.String)?.value)

        // And when the day already carries the row (the unlinked set the
        // desktop publishes), the join adds no duplicate.
        val dayCarryingIt = dayItem(
            "calendar-day-2026-05-11",
            "2026-05-11",
            workouts = listOf(workout(3.0, "Endurance", completedActivityId = 7.0)),
            activities = listOf(activityRow(7.0, "2026-05-11T06:30:00")),
        )
        val second = CalendarData(calendarSnapshot(listOf(dayCarryingIt)), activitiesSnapshot)
        assertEquals(1, second.days.single().activityCount)
    }

    @Test
    fun aJsonNullRaceIsNoRace() {
        val withNull = CalendarData(calendarSnapshot(listOf(dayItem("a", "2026-05-12", race = JsonValue.Null))))
        assertFalse(withNull.days.single().hasContent)
        assertNull(withNull.days.single().race)

        val withRace = CalendarData(
            calendarSnapshot(
                listOf(dayItem("b", "2026-05-13", race = JsonValue.Object(linkedMapOf("name" to JsonValue.String("Rennterweg"))))),
            ),
        )
        assertTrue(withRace.days.single().hasContent)
        assertEquals("Rennterweg", CalendarJSON.raceTitle(withRace.days.single().race!!))
    }

    @Test
    fun phaseAloneIsContextNotContent() {
        // A plan window stamps its phase on every day it covers. If that
        // counted as content, the accent border would light up the whole
        // month and stop meaning anything -- iOS's rule, ported.
        val data = CalendarData(
            calendarSnapshot(
                listOf(dayItem("p", "2026-05-20", phase = "build")),
            ),
        )
        val day = data.days.single()
        assertEquals("build", day.phase)
        assertFalse(day.hasContent)
        assertFalse("the month stays a no-data month", data.hasContent)
    }

    @Test
    fun ootoAndPhaseAccumulateAcrossParts() {
        val data = CalendarData(
            calendarSnapshot(
                listOf(
                    dayItem("p1", "2026-05-14", ooto = false, part = 1, parts = 2),
                    dayItem("p2", "2026-05-14", ooto = true, phase = "build", part = 2, parts = 2),
                ),
            ),
        )
        val day = data.days.single()
        assertTrue(day.ooto)
        assertEquals("build", day.phase)
    }

    @Test
    fun currentFtpComesFromTheDashboardInItsPrecedenceOrder() {
        val training = CloudItem(
            "training-state", CloudKind.TrainingState, 1,
            payload = CloudPayload.TrainingState(TrainingState(ftp = 250.0, cp = null, wprime = null, ctl = null, atl = null, tsb = null, decoupling = null)),
        )
        val profile = CloudItem(
            "rider-profile", CloudKind.Profile, 1,
            payload = CloudPayload.Profile(RiderProfile("R", ftp = null, ftpWatts = 240.0, power = null, heartRate = null, weightKg = null, weightDate = null, weightSource = null)),
        )
        val zeroTraining = CloudItem(
            "training-state", CloudKind.TrainingState, 1,
            payload = CloudPayload.TrainingState(TrainingState(ftp = 0.0, cp = null, wprime = null, ctl = null, atl = null, tsb = null, decoupling = null)),
        )
        val dashboard = CloudSnapshot(CloudRoute.Dashboard, 1, listOf(training, profile), CloudSnapshot.Source.network, 0L)
        assertEquals(250.0, CalendarData.currentFTP(dashboard))

        val onlyProfile = CloudSnapshot(CloudRoute.Dashboard, 1, listOf(profile), CloudSnapshot.Source.network, 0L)
        assertEquals(240.0, CalendarData.currentFTP(onlyProfile))

        // A zero FTP is no FTP: it must not silence the profile's.
        val zeroAndProfile = CloudSnapshot(CloudRoute.Dashboard, 1, listOf(zeroTraining, profile), CloudSnapshot.Source.network, 0L)
        assertEquals(240.0, CalendarData.currentFTP(zeroAndProfile))

        assertNull(CalendarData.currentFTP(null))
    }

    @Test
    fun gridSnapshotAddsCachedAdjacentDaysWithoutDuplicatingItsOwn() {
        val month = CalendarMonth(2026, 9)
        val outside = month.gridDates().first { !it.isInMonth }   // 2026-08-31
        val inside = month.gridDates().first { it.isInMonth }     // 2026-09-01
        val primary = calendarSnapshot(
            listOf(dayItem("calendar-day-${inside.isoDate}", inside.isoDate, workouts = listOf(workout(1.0, "A")))),
        )
        val adjacent = calendarSnapshot(
            listOf(
                dayItem("calendar-day-${outside.isoDate}", outside.isoDate, workouts = listOf(workout(2.0, "B"))),
                // A duplicate of a date the primary already carries: never added twice.
                dayItem("dupe", inside.isoDate, workouts = listOf(workout(3.0, "C"))),
            ),
        )
        val merged = CalendarData.gridSnapshot(primary, month, listOf(adjacent, null))
        val data = CalendarData(merged)
        val insideDay = data.entry(inside.isoDate)!!
        assertEquals(1, insideDay.workoutCount)
        assertNotNull(data.entry(outside.isoDate))
    }

    @Test
    fun monthContentDecidesItselfNotItsEdgeCells() {
        // A month whose own days are empty but whose cached Monday lead-in is
        // not is still "no calendar data yet" for this month.
        val month = CalendarMonth(2026, 9)
        val outside = month.gridDates().first { !it.isInMonth }
        val snapshot = calendarSnapshot(
            listOf(dayItem("calendar-day-2026-09-02", "2026-09-02"), dayItem("calendar-day-2026-09-03", "2026-09-03")),
        )
        val grid = CalendarData.gridSnapshot(
            snapshot, month,
            listOf(calendarSnapshot(listOf(dayItem("lead", outside.isoDate, workouts = listOf(workout(1.0, "W"))))), null),
        )
        assertFalse("the month itself has nothing", CalendarData(snapshot).hasContent)
        assertTrue("the grid does span into the neighbour", CalendarData(grid).days.any { it.dateISO == outside.isoDate })
    }

    @Test
    fun workoutStatusReadsTheFlagsInOrder() {
        val completed = workout(1.0, "A", completedActivityId = 5.0)
        assertEquals(WorkoutStatus.Completed, CalendarJSON.workoutStatus(completed))
        val missed = workout(2.0, "B", missed = true)
        assertEquals(WorkoutStatus.Missed, CalendarJSON.workoutStatus(missed))
        val planned = workout(3.0, "C")
        assertEquals(WorkoutStatus.Planned, CalendarJSON.workoutStatus(planned))
        val adapted = JsonValue.Object(linkedMapOf("name" to JsonValue.String("D"), "adapted" to JsonValue.Bool(true)))
        assertEquals(WorkoutStatus.Adapted, CalendarJSON.workoutStatus(adapted))
        assertEquals(1, CalendarJSON.missedCount(listOf(missed, planned)))
    }

    @Test
    fun desktopFormattingVoices() {
        assertEquals("1h 30m", CalendarJSON.durationString(5400.0))
        assertEquals("45m", CalendarJSON.durationString(2700.0))
        assertEquals("61 km", CalendarJSON.distanceString(61000.0))
        assertEquals("61.5 km", CalendarJSON.distanceString(61500.0))
        assertEquals("81", CalendarJSON.numberString(81.0))
        assertEquals("74.5", CalendarJSON.numberString(74.5))
        assertEquals("\u2014", CalendarJSON.numberString(Double.NaN))
        // A date-only value reads as a day; a value that is not one passes
        // through rather than becoming a guess.
        assertEquals("Tuesday, Sep 1, 2026", CalendarJSON.displayDate("2026-09-01"))
        assertEquals("not a date", CalendarJSON.displayDate("not a date"))
    }
}
