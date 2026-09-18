package com.wattracker.android.cloud

import com.wattracker.android.json.JsonValue
import com.wattracker.android.json.optArray
import com.wattracker.android.json.optInt
import com.wattracker.android.json.optString
import com.wattracker.android.json.toJson
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The Kotlin half of the shared cloud-object shape fixture.
 *
 * `tests/vectors/cloud_objects_v1.json` pins the shape of all ten published
 * object kinds; the Python and Swift suites read the same file. Drift between
 * the app's models and the publisher fails a test in all three languages.
 */
class CloudObjectsTest {

    private val fixture: JsonValue = TestVectors.parse("cloud_objects_v1.json")

    private fun decodeCollection(text: String): CollectionResponse =
        CollectionResponse.fromJson(JsonValue.parse(text))

    @Test
    fun everyPublishedKindDecodesFromTheShapeTheServerSends() {
        assertEquals(1, fixture.optInt("version"))
        val kinds = fixture.optArray("kinds")!!.map { (it as JsonValue.String).value }
        assertEquals(kinds, kinds.sorted())
        assertEquals(10, kinds.size)
        assertEquals(kinds.size, kinds.toSet().size)

        val items = fixture.optArray("items")!!.map { CloudItem.fromJson(it) }
        assertEquals(kinds.size, items.size)
        assertEquals(kinds.toSet(), items.map { it.kind.wire }.toSet())
        for (kind in kinds) {
            assertEquals(kind, 1, items.count { it.kind.wire == kind })
        }
        for (item in items) {
            if (item.kind is CloudKind.Other) {
                throw AssertionError("published kind decoded as .other: ${item.kind.wire}")
            }
            assertTrue(item.revision > 0)
            assertFalse(item.deleted)
        }

        val profile = (items.first { it.kind == CloudKind.Profile }.payload) as CloudPayload.Profile
        assertEquals(248.0, profile.value.resolvedFTP!!, 1e-9)
        assertEquals(2, profile.value.power?.zones?.size)
        assertNull(profile.value.power?.zones?.get(1)?.max)
        assertEquals(false, profile.value.heartRate?.available)

        val stream = (items.first { it.kind == CloudKind.Stream }.payload) as CloudPayload.Stream
        val power = stream.value.streams.power!!
        assertEquals(3, power.size)
        assertNull(power[1]) // a recording gap stays a gap
        assertNull(stream.value.streams.cadence) // an unrecorded channel is absent, not empty
    }

    @Test
    fun everyFixtureItemSurvivesDecodeEncodeDecode() {
        // Every item in the published fixture must round-trip through the app's
        // own serializer, so drift from the publisher fails here, not in
        // production. On a mismatch, name the kind -- do not work around it.
        val items = fixture.optArray("items")!!
        for (itemJson in items) {
            val decoded = CloudItem.fromJson(itemJson)
            val redecoded = CloudItem.fromJson(JsonValue.parse(decoded.toJson().toJson()))
            if (redecoded != decoded) {
                throw AssertionError(
                    "round-trip mismatch for kind '${decoded.kind.wire}' (id '${decoded.id}'):\n" +
                        "  decoded:   ${decoded.toJson().toJson()}\n" +
                        "  redecoded: ${redecoded.toJson().toJson()}",
                )
            }
        }
    }

    @Test
    fun bothProfilePublishersAreUnderstood() {
        val response = decodeCollection(
            """{"items":[{"id":"profile","kind":"profile","revision":1,"data":{"ftp_watts":211.4}}]}""",
        )
        val profile = response.items[0].payload as CloudPayload.Profile
        assertEquals(211.4, profile.value.resolvedFTP!!, 1e-9)
        assertNull(response.revision) // a non-mobile route carries no checkpoint
    }

    @Test
    fun anUnknownKindSurvivesADecodeAndEncodeUnchanged() {
        val text = """{"id":"gadget-1","kind":"gadget","revision":6,"data":{"nested":{"list":[1,2,3],"flag":true,"nothing":null},"name":"x"}}"""
        val item = CloudItem.fromJson(JsonValue.parse(text))
        assertEquals(CloudKind.Other("gadget"), item.kind)
        val payload = item.payload
        assertTrue(payload is CloudPayload.Other)
        assertEquals("x", (payload as CloudPayload.Other).value.optString("name"))
        // Re-encode to text and decode again; the value must survive byte-wise.
        val round = CloudItem.fromJson(JsonValue.parse(item.toJson().toJson()))
        assertEquals(item, round)
    }

    @Test
    fun anUnknownKindDoesNotTakeThePageDownWithIt() {
        val response = decodeCollection(
            """{"items":[
              {"id":"gadget-1","kind":"gadget","revision":6,"data":{"whatever":1}},
              {"id":"profile","kind":"profile","revision":6,"data":{"ftp":250}}
            ],"revision":6,"next_cursor":null}""",
        )
        assertEquals(2, response.items.size)
        assertEquals(CloudKind.Profile, response.items[1].kind)
    }

    @Test
    fun aTombstoneCarriesNoPayloadAndIsNotMistakenForAnObject() {
        val response = decodeCollection(
            """{"items":[{"id":"training-state","kind":"training_state","revision":9,
               "data":{},"deleted":true}],"revision":9,"next_cursor":null}""",
        )
        val item = response.items[0]
        assertTrue(item.deleted)
        assertEquals(CloudPayload.Tombstone, item.payload)
        assertEquals(CloudKind.TrainingState, item.kind)
    }

    @Test
    fun theRoutesThatServeDeltasAreExactlyTheMobileOnes() {
        assertEquals(
            setOf(CloudRoute.Dashboard, CloudRoute.Volume, CloudRoute.Curve, CloudRoute.Activities),
            CloudRoute.entries.filter { it.servesDeltas }.toSet(),
        )
        assertEquals("/api/v1/context/dashboard", CloudRoute.Dashboard.path)
    }

    @Test
    fun aModelRoundTripsThroughItsOwnSerializer() {
        // Each fixture is a data value constructed directly, not a parsed JSON
        // string. The assertion is the load-bearing one: encode the data value,
        // re-decode it, and demand it comes back equal to what we built.
        val items = listOf(
            profileItem(),
            trainingStateItem(),
            ftpHistoryItem(),
            loadPointItem(),
            curveItem(),
            volumeWeekItem(),
            calendarDayItem(),
            activityItem(),
            activityDetailItem(),
            streamItem(),
        )
        for (item in items) {
            assertEquals(
                item,
                CloudItem.fromJson(JsonValue.parse(item.toJson().toJson())),
            )
        }
    }

    private fun profileItem() = CloudItem(
        id = "rider-1",
        kind = CloudKind.Profile,
        revision = 12,
        deleted = false,
        payload = CloudPayload.Profile(
            RiderProfile(
                displayName = "Takazumi",
                ftp = null, // one publisher writes it, one doesn't; both legal
                ftpWatts = 250.0,
                power = MetricState(
                    available = true,
                    value = 250.0,
                    source = "zwap8",
                    // Top zone is open-ended: max is a value (null), not a gap.
                    zones = listOf(
                        Zone(label = "2", name = "Sweet Spot", pct = 88.0, min = 91.0, max = 104.0, range = "91-104%"),
                        Zone(label = "5", name = "Neuromuscular", pct = 106.0, min = 106.0, max = null, range = null),
                    ),
                ),
                heartRate = MetricState(available = false, value = null, source = null, zones = null),
                weightKg = 72.5,
                weightDate = "2024-05-01",
                weightSource = "battledragon",
            ),
        ),
    )

    private fun trainingStateItem() = CloudItem(
        id = "training-state",
        kind = CloudKind.TrainingState,
        revision = 20,
        deleted = false,
        payload = CloudPayload.TrainingState(
            TrainingState(
                ftp = 250.0, cp = 300.5, wprime = 12000.0,
                ctl = 45.0, atl = 50.0, tsb = -5.0,
                decoupling = null, // genuinely absent for a fresh rider
            ),
        ),
    )

    private fun ftpHistoryItem() = CloudItem(
        id = "ftp-2024-04-01",
        kind = CloudKind.FtpHistory,
        revision = 7,
        deleted = false,
        payload = CloudPayload.FtpHistory(
            FtpHistoryPoint(date = "2024-04-01", ftpWatts = 245.0, source = "test"),
        ),
    )

    private fun loadPointItem() = CloudItem(
        id = "load-2024-05-01",
        kind = CloudKind.LoadPoint,
        revision = 14,
        deleted = false,
        payload = CloudPayload.LoadPoint(
            LoadPoint(date = "2024-05-01", tss = 120.5, ctl = 45.0, atl = 48.0, tsb = null),
        ),
    )

    private fun curveItem() = CloudItem(
        id = "curve",
        kind = CloudKind.Curve,
        revision = 4,
        deleted = false,
        payload = CloudPayload.Curve(
            PowerCurve(
                measured = listOf(CurvePoint(t = 60.0, power = 410.0), CurvePoint(t = 120.0, power = 380.0)),
                allTime = listOf(CurvePoint(t = 60.0, power = 415.0)),
                lastRide = null, // a list field that is entirely absent
                model = emptyList(), // present but empty: distinct from null
                cp = 268.0,
                wprime = null,
            ),
        ),
    )

    private fun volumeWeekItem() = CloudItem(
        id = "week-2024-04-29",
        kind = CloudKind.VolumeWeek,
        revision = 3,
        deleted = false,
        payload = CloudPayload.VolumeWeek(
            VolumeWeek(weekStart = "2024-04-29", hours = 10.5, tss = 1200.0, distanceKm = 250.0, calories = null),
        ),
    )

    private fun calendarDayItem() = CloudItem(
        id = "day-2024-05-01",
        kind = CloudKind.CalendarDay,
        revision = 9,
        deleted = false,
        payload = CloudPayload.CalendarDay(
            CalendarDay(
                date = "2024-05-01",
                ooto = true,
                phase = "sweet_spot",
                // `race`/`workouts`/`zones` survive only as non-null JSON here:
                // the encoder writes `race ?: Null` and the decoder returns the
                // JsonValue, so a Kotlin null would not survive an exact ==.
                race = JsonValue.Object(mapOf("name" to JsonValue.String("Local Criterium"))),
                workouts = listOf(JsonValue.Object(mapOf("type" to JsonValue.String("interval")))),
                activities = emptyList(), // present but empty
                part = 1,
                parts = 2,
            ),
        ),
    )

    private fun activityItem() = CloudItem(
        id = "activity-1001",
        kind = CloudKind.Activity,
        revision = 31,
        deleted = false,
        payload = CloudPayload.Activity(
            ActivitySummary(
                id = 1001.0, startTime = "2024-05-01T10:00:00", durationS = 5400.0,
                distanceM = 40000.0, avgPower = 220.0, avgHr = 150.0, np = 235.0,
                intensityFactor = 0.94, tss = 100.0, rpe = null,
            ),
        ),
    )

    private fun activityDetailItem() = CloudItem(
        id = "activity_detail-1001",
        kind = CloudKind.ActivityDetail,
        revision = 32,
        deleted = false,
        payload = CloudPayload.ActivityDetail(
            ActivityDetail(
                id = 1001.0, startTime = "2024-05-01T10:00:00", durationS = 5400.0,
                distanceM = 40000.0, avgPower = 220.0, avgHr = 150.0, np = 235.0,
                intensityFactor = 0.94, tss = 100.0, rpe = 7.0,
                weightKg = 72.5, weightSource = "garmin", weightDate = "2024-05-01",
                // Same race/zones null-collision as CalendarDay; keep it non-null.
                zones = JsonValue.Object(mapOf("z2_seconds" to JsonValue.Number(3600.0))),
            ),
        ),
    )

    private fun streamItem() = CloudItem(
        id = "stream-1001",
        kind = CloudKind.Stream,
        revision = 33,
        deleted = false,
        payload = CloudPayload.Stream(
            ActivityStreams(
                ActivityStreams.Channels(
                    time = listOf(0.0, 1.0, 2.0),
                    // A null element is a recording gap, not an absent channel:
                    // it must survive the encode/decode as null, in place.
                    power = listOf(210.0, null, 260.0),
                    heartrate = listOf(145.0, 148.0, 150.0),
                    cadence = null, // an unrecorded channel is absent, not empty
                    altitude = null,
                ),
            ),
        ),
    )
}
