package com.wattracker.android.cloud

import com.wattracker.android.json.JsonValue
import com.wattracker.android.json.asBoolean
import com.wattracker.android.json.asDouble
import com.wattracker.android.json.asString
import com.wattracker.android.json.opt
import com.wattracker.android.json.optArray
import com.wattracker.android.json.optBoolean
import com.wattracker.android.json.optDouble
import com.wattracker.android.json.optInt
import com.wattracker.android.json.optString

/**
 * The read plane's wire format, as types instead of dictionary lookups.
 *
 * Every object the desktop publishes is `{id, kind, revision, data}` plus an
 * optional `deleted` marker, and `kind` decides how `data` reads. That is the
 * whole envelope; `wattracker/cloud/models.py:CloudObject.wire` is the other
 * half of it.
 *
 * Two decoding rules are load-bearing rather than stylistic:
 *
 * 1. **An unknown kind decodes, it does not throw.** The `since=` protocol
 *    resends nothing: once the client acknowledges a checkpoint, an object it
 *    dropped is gone until the desktop happens to republish it. A single
 *    unmodelled kind that failed the whole page would take the modelled objects
 *    on that page with it.
 * 2. **A tombstone carries no payload.** The server sends `data: {}` for a
 *    deleted object, so decoding it as its kind would fail on every required
 *    field. `deleted` is read first and the payload is skipped.
 *
 * Every numeric field is a `Double?` and every string optional, because
 * `_safe_data` in `snapshot.py` turns a non-finite float into `null` and
 * several of these values are genuinely absent for a rider with no FTP, no HR
 * data or no weight history. A model that demanded them would decode a real,
 * correct snapshot as a failure.
 */

// MARK: - Kind

/** The ten published object kinds, plus an escape hatch for kinds not modelled. */
sealed class CloudKind {
    abstract val wire: String

    data object Profile : CloudKind() { override val wire = "profile" }
    data object TrainingState : CloudKind() { override val wire = "training_state" }
    data object FtpHistory : CloudKind() { override val wire = "ftp_history" }
    data object LoadPoint : CloudKind() { override val wire = "load_point" }
    data object Curve : CloudKind() { override val wire = "curve" }
    data object VolumeWeek : CloudKind() { override val wire = "volume_week" }
    data object CalendarDay : CloudKind() { override val wire = "calendar_day" }
    data object Activity : CloudKind() { override val wire = "activity" }
    data object ActivityDetail : CloudKind() { override val wire = "activity_detail" }
    data object Stream : CloudKind() { override val wire = "stream" }

    /** A kind this build does not model. Carried, never discarded. */
    data class Other(override val wire: String) : CloudKind()

    companion object {
        fun fromWire(wire: String): CloudKind = when (wire) {
            "profile" -> Profile
            "training_state" -> TrainingState
            "ftp_history" -> FtpHistory
            "load_point" -> LoadPoint
            "curve" -> Curve
            "volume_week" -> VolumeWeek
            "calendar_day" -> CalendarDay
            "activity" -> Activity
            "activity_detail" -> ActivityDetail
            "stream" -> Stream
            else -> Other(wire)
        }
    }
}

// MARK: - Item + payload

/** One published object. */
data class CloudItem(
    val id: String,
    val kind: CloudKind,
    val revision: Int,
    val deleted: Boolean = false,
    val payload: CloudPayload,
) {
    companion object {
        fun fromJson(v: JsonValue): CloudItem {
            val id = v.optString("id") ?: throw CloudDecodeException("item has no id")
            val kindWire = v.optString("kind") ?: throw CloudDecodeException("item has no kind")
            val revision = v.optInt("revision") ?: throw CloudDecodeException("item has no revision")
            val deleted = v.optBoolean("deleted") ?: false
            val payload = if (deleted) {
                // `data` is `{}` on a tombstone. Reading it as the kind's
                // payload would fail on the first non-optional field and take
                // the whole page down with it.
                CloudPayload.Tombstone
            } else {
                CloudPayload.fromJson(kindWire, v.opt("data") ?: JsonValue.Object(emptyMap()))
            }
            return CloudItem(id, CloudKind.fromWire(kindWire), revision, deleted, payload)
        }
    }

    fun toJson(): JsonValue {
        val fields = LinkedHashMap<String, JsonValue>()
        fields["id"] = JsonValue.String(id)
        fields["kind"] = JsonValue.String(kind.wire)
        fields["revision"] = JsonValue.Number(revision.toDouble())
        if (deleted) fields["deleted"] = JsonValue.Bool(value = true)
        fields["data"] = payload.toJson()
        return JsonValue.Object(fields)
    }
}

class CloudDecodeException(message: String) : Exception(message)

/** `data`, read as whatever `kind` says it is. */
sealed class CloudPayload {
    data class Profile(val value: RiderProfile) : CloudPayload()
    // Where a wrapper shares its model's name the parameter type must be the
    // fully-qualified top-level model, or it self-references the wrapper.
    data class TrainingState(val value: com.wattracker.android.cloud.TrainingState) : CloudPayload()
    data class FtpHistory(val value: FtpHistoryPoint) : CloudPayload()
    data class LoadPoint(val value: com.wattracker.android.cloud.LoadPoint) : CloudPayload()
    data class Curve(val value: PowerCurve) : CloudPayload()
    data class VolumeWeek(val value: com.wattracker.android.cloud.VolumeWeek) : CloudPayload()
    data class CalendarDay(val value: com.wattracker.android.cloud.CalendarDay) : CloudPayload()
    data class Activity(val value: ActivitySummary) : CloudPayload()
    data class ActivityDetail(val value: com.wattracker.android.cloud.ActivityDetail) : CloudPayload()
    data class Stream(val value: ActivityStreams) : CloudPayload()
    data class Other(val value: JsonValue) : CloudPayload()
    data object Tombstone : CloudPayload()

    companion object {
        fun fromJson(kindWire: String, data: JsonValue): CloudPayload = when (CloudKind.fromWire(kindWire)) {
            CloudKind.Profile -> Profile(RiderProfile.fromJson(data))
            // The payload wrappers named after their models (TrainingState,
            // LoadPoint, ...) shadow the top-level data models inside this
            // scope, so the model's companion is referenced by full name.
            CloudKind.TrainingState -> TrainingState(com.wattracker.android.cloud.TrainingState.fromJson(data))
            CloudKind.FtpHistory -> FtpHistory(FtpHistoryPoint.fromJson(data))
            CloudKind.LoadPoint -> LoadPoint(com.wattracker.android.cloud.LoadPoint.fromJson(data))
            CloudKind.Curve -> Curve(PowerCurve.fromJson(data))
            CloudKind.VolumeWeek -> VolumeWeek(com.wattracker.android.cloud.VolumeWeek.fromJson(data))
            CloudKind.CalendarDay -> CalendarDay(com.wattracker.android.cloud.CalendarDay.fromJson(data))
            CloudKind.Activity -> Activity(ActivitySummary.fromJson(data))
            CloudKind.ActivityDetail -> ActivityDetail(com.wattracker.android.cloud.ActivityDetail.fromJson(data))
            CloudKind.Stream -> Stream(ActivityStreams.fromJson(data))
            is CloudKind.Other -> Other(data)
        }
    }

    fun toJson(): JsonValue = when (this) {
        is Profile -> value.toJson()
        is TrainingState -> value.toJson()
        is FtpHistory -> value.toJson()
        is LoadPoint -> value.toJson()
        is Curve -> value.toJson()
        is VolumeWeek -> value.toJson()
        is CalendarDay -> value.toJson()
        is Activity -> value.toJson()
        is ActivityDetail -> value.toJson()
        is Stream -> value.toJson()
        is Other -> value
        is Tombstone -> JsonValue.Object(emptyMap())
    }
}

// MARK: - The published kinds

/** `profile`. Two publishers emit this kind and they do not agree. */
data class RiderProfile(
    val displayName: String?,
    val ftp: Double?,
    val ftpWatts: Double?,
    val power: MetricState?,
    val heartRate: MetricState?,
    val weightKg: Double?,
    val weightDate: String?,
    val weightSource: String?,
) {
    /** The rider's FTP whichever publisher wrote it, or null when unpublished. */
    val resolvedFTP: Double? get() = ftp ?: ftpWatts ?: power?.value

    companion object {
        fun fromJson(v: JsonValue): RiderProfile = RiderProfile(
            displayName = v.optString("display_name"),
            ftp = v.optDouble("ftp"),
            ftpWatts = v.optDouble("ftp_watts"),
            power = (v.opt("power") as? JsonValue.Object)?.let { MetricState.fromJson(it) },
            heartRate = (v.opt("heart_rate") as? JsonValue.Object)?.let { MetricState.fromJson(it) },
            weightKg = v.optDouble("weight_kg"),
            weightDate = v.optString("weight_date"),
            weightSource = v.optString("weight_source"),
        )
    }

    fun toJson(): JsonValue = jsonObj(
        "display_name" to displayName, "ftp" to ftp, "ftp_watts" to ftpWatts,
        "power" to power?.toJson(), "heart_rate" to heartRate?.toJson(),
        "weight_kg" to weightKg, "weight_date" to weightDate, "weight_source" to weightSource,
    )
}

/** The `{available, value, source, zones}` block shared by power and HR. */
data class MetricState(
    val available: Boolean,
    val value: Double?,
    val source: String?,
    val zones: List<Zone>?,
) {
    companion object {
        fun fromJson(v: JsonValue): MetricState = MetricState(
            available = v.optBoolean("available") ?: false,
            value = v.optDouble("value"),
            source = v.optString("source"),
            zones = v.optArray("zones")?.map { Zone.fromJson(it) },
        )
    }

    fun toJson(): JsonValue = jsonObj(
        "available" to available, "value" to value, "source" to source,
        "zones" to zones?.let { list -> jsonList(list.map { it.toJson() }) },
    )
}

data class Zone(
    val label: String?,
    val name: String?,
    val pct: Double?,
    val min: Double?,
    /** Nil in the open-ended top zone, which is a value, not a gap. */
    val max: Double?,
    val range: String?,
) {
    companion object {
        fun fromJson(v: JsonValue): Zone = Zone(
            label = v.optString("label"), name = v.optString("name"),
            pct = v.optDouble("pct"), min = v.optDouble("min"),
            max = v.optDouble("max"), range = v.optString("range"),
        )
    }

    fun toJson(): JsonValue = jsonObj(
        "label" to label, "name" to name, "pct" to pct,
        "min" to min, "max" to max, "range" to range,
    )
}

/** `training_state`: the numbers the dashboard's header reads. */
data class TrainingState(
    val ftp: Double?,
    val cp: Double?,
    val wprime: Double?,
    val ctl: Double?,
    val atl: Double?,
    val tsb: Double?,
    val decoupling: Double?,
) {
    companion object {
        fun fromJson(v: JsonValue): TrainingState = TrainingState(
            ftp = v.optDouble("ftp"), cp = v.optDouble("cp"),
            wprime = v.optDouble("wprime"), ctl = v.optDouble("ctl"),
            atl = v.optDouble("atl"), tsb = v.optDouble("tsb"),
            decoupling = v.optDouble("decoupling"),
        )
    }

    fun toJson(): JsonValue = jsonObj(
        "ftp" to ftp, "cp" to cp, "wprime" to wprime, "ctl" to ctl,
        "atl" to atl, "tsb" to tsb, "decoupling" to decoupling,
    )
}

/** `ftp_history`: one dated FTP. */
data class FtpHistoryPoint(val date: String?, val ftpWatts: Double?, val source: String?) {
    companion object {
        fun fromJson(v: JsonValue): FtpHistoryPoint = FtpHistoryPoint(
            date = v.optString("date"), ftpWatts = v.optDouble("ftp_watts"),
            source = v.optString("source"),
        )
    }

    fun toJson(): JsonValue =
        jsonObj("date" to date, "ftp_watts" to ftpWatts, "source" to source)
}

/** `load_point`: one day of the CTL/ATL/TSB series. */
data class LoadPoint(
    val date: String?,
    val tss: Double?,
    val ctl: Double?,
    val atl: Double?,
    val tsb: Double?,
) {
    companion object {
        fun fromJson(v: JsonValue): LoadPoint = LoadPoint(
            date = v.optString("date"), tss = v.optDouble("tss"),
            ctl = v.optDouble("ctl"), atl = v.optDouble("atl"), tsb = v.optDouble("tsb"),
        )
    }

    fun toJson(): JsonValue =
        jsonObj("date" to date, "tss" to tss, "ctl" to ctl, "atl" to atl, "tsb" to tsb)
}

/** `curve`: mean-maximal power, measured and modelled. */
data class PowerCurve(
    val measured: List<CurvePoint>?,
    val allTime: List<CurvePoint>?,
    val lastRide: List<CurvePoint>?,
    val model: List<CurvePoint>?,
    val cp: Double?,
    val wprime: Double?,
) {
    companion object {
        fun fromJson(v: JsonValue): PowerCurve = PowerCurve(
            measured = points(v, "measured"),
            allTime = points(v, "all_time"),
            lastRide = points(v, "last_ride"),
            model = points(v, "model"),
            cp = v.optDouble("cp"),
            wprime = v.optDouble("wprime"),
        )

        private fun points(v: JsonValue, key: String): List<CurvePoint>? =
            v.optArray(key)?.map { CurvePoint.fromJson(it) }
    }

    fun toJson(): JsonValue = jsonObj(
        "measured" to measured?.let { list -> jsonList(list.map { it.toJson() }) },
        "all_time" to allTime?.let { list -> jsonList(list.map { it.toJson() }) },
        "last_ride" to lastRide?.let { list -> jsonList(list.map { it.toJson() }) },
        "model" to model?.let { list -> jsonList(list.map { it.toJson() }) },
        "cp" to cp, "wprime" to wprime,
    )
}

data class CurvePoint(
    /** Duration in seconds. */
    val t: Double?,
    val power: Double?,
) {
    companion object {
        fun fromJson(v: JsonValue): CurvePoint =
            CurvePoint(t = v.optDouble("t"), power = v.optDouble("power"))
    }

    fun toJson(): JsonValue = jsonObj("t" to t, "power" to power)
}

/** `volume_week`: one Monday-anchored week. */
data class VolumeWeek(
    val weekStart: String?,
    val hours: Double?,
    val tss: Double?,
    val distanceKm: Double?,
    val calories: Double?,
) {
    companion object {
        fun fromJson(v: JsonValue): VolumeWeek = VolumeWeek(
            weekStart = v.optString("week_start"), hours = v.optDouble("hours"),
            tss = v.optDouble("tss"), distanceKm = v.optDouble("distance_km"),
            calories = v.optDouble("calories"),
        )
    }

    fun toJson(): JsonValue = jsonObj(
        "week_start" to weekStart, "hours" to hours, "tss" to tss,
        "distance_km" to distanceKm, "calories" to calories,
    )
}

/**
 * `calendar_day`.
 *
 * `workouts` and `activities` are the desktop's own row shapes, and `race` is a
 * race row. They stay as [JsonValue]: the calendar screen's schema is the
 * desktop's, and a mirror here would turn one added column into a decode
 * failure for a whole day. `part`/`parts` appear only where a day was too large
 * for one object and had to be split.
 */
data class CalendarDay(
    val date: String?,
    val ooto: Boolean?,
    val phase: String?,
    val race: JsonValue?,
    val workouts: List<JsonValue>?,
    val activities: List<JsonValue>?,
    val part: Int?,
    val parts: Int?,
) {
    companion object {
        fun fromJson(v: JsonValue): CalendarDay = CalendarDay(
            date = v.optString("date"),
            ooto = v.optBoolean("ooto"),
            phase = v.optString("phase"),
            race = v.opt("race"),
            workouts = v.optArray("workouts"),
            activities = v.optArray("activities"),
            part = v.optInt("part"),
            parts = v.optInt("parts"),
        )
    }

    fun toJson(): JsonValue {
        val fields = LinkedHashMap<String, JsonValue>()
        fields["date"] = jsonScalar(date)
        fields["ooto"] = if (ooto != null) JsonValue.Bool(ooto) else JsonValue.Null
        if (phase != null) fields["phase"] = JsonValue.String(phase)
        fields["race"] = race ?: JsonValue.Null
        if (workouts != null) fields["workouts"] = jsonList(workouts)
        if (activities != null) fields["activities"] = jsonList(activities)
        if (part != null) fields["part"] = JsonValue.Number(part.toDouble())
        if (parts != null) fields["parts"] = JsonValue.Number(parts.toDouble())
        return JsonValue.Object(fields)
    }
}

/** `activity`: the summary row, which is what a list renders. */
data class ActivitySummary(
    val id: Double?,
    val startTime: String?,
    val durationS: Double?,
    val distanceM: Double?,
    val avgPower: Double?,
    val avgHr: Double?,
    val np: Double?,
    /** `if_` on the wire: `if` is a Python keyword and the column kept it. */
    val intensityFactor: Double?,
    val tss: Double?,
    val rpe: Double?,
) {
    companion object {
        fun fromJson(v: JsonValue): ActivitySummary = ActivitySummary(
            id = v.optDouble("id"), startTime = v.optString("start_time"),
            durationS = v.optDouble("duration_s"), distanceM = v.optDouble("distance_m"),
            avgPower = v.optDouble("avg_power"), avgHr = v.optDouble("avg_hr"),
            np = v.optDouble("np"), intensityFactor = v.optDouble("if_"),
            tss = v.optDouble("tss"), rpe = v.optDouble("rpe"),
        )
    }

    fun toJson(): JsonValue = jsonObj(
        "id" to id, "start_time" to startTime, "duration_s" to durationS,
        "distance_m" to distanceM, "avg_power" to avgPower, "avg_hr" to avgHr,
        "np" to np, "if_" to intensityFactor, "tss" to tss, "rpe" to rpe,
    )
}

/** `activity_detail`: the summary plus what only one ride's page needs. */
data class ActivityDetail(
    val id: Double?,
    val startTime: String?,
    val durationS: Double?,
    val distanceM: Double?,
    val avgPower: Double?,
    val avgHr: Double?,
    val np: Double?,
    val intensityFactor: Double?,
    val tss: Double?,
    val rpe: Double?,
    val weightKg: Double?,
    val weightSource: String?,
    val weightDate: String?,
    /** The zone summary block, kept as JSON for the same reason a calendar day is. */
    val zones: JsonValue?,
) {
    companion object {
        fun fromJson(v: JsonValue): ActivityDetail = ActivityDetail(
            id = v.optDouble("id"), startTime = v.optString("start_time"),
            durationS = v.optDouble("duration_s"), distanceM = v.optDouble("distance_m"),
            avgPower = v.optDouble("avg_power"), avgHr = v.optDouble("avg_hr"),
            np = v.optDouble("np"), intensityFactor = v.optDouble("if_"),
            tss = v.optDouble("tss"), rpe = v.optDouble("rpe"),
            weightKg = v.optDouble("weight_kg"), weightSource = v.optString("weight_source"),
            weightDate = v.optString("weight_date"), zones = v.opt("zones"),
        )
    }

    fun toJson(): JsonValue {
        val fields = LinkedHashMap<String, JsonValue>()
        fields["id"] = jsonScalar(id)
        fields["start_time"] = jsonScalar(startTime)
        fields["duration_s"] = jsonScalar(durationS)
        fields["distance_m"] = jsonScalar(distanceM)
        fields["avg_power"] = jsonScalar(avgPower)
        fields["avg_hr"] = jsonScalar(avgHr)
        fields["np"] = jsonScalar(np)
        fields["if_"] = jsonScalar(intensityFactor)
        fields["tss"] = jsonScalar(tss)
        fields["rpe"] = jsonScalar(rpe)
        fields["weight_kg"] = jsonScalar(weightKg)
        fields["weight_source"] = jsonScalar(weightSource)
        fields["weight_date"] = jsonScalar(weightDate)
        fields["zones"] = zones ?: JsonValue.Null
        return JsonValue.Object(fields)
    }
}

/**
 * `stream`: the downsampled per-second channels for one ride.
 *
 * The server downsamples to 1,500 points before publishing, so this is bounded
 * by construction. A channel is absent when the ride did not record it, and an
 * element is null where the recording had a gap.
 */
data class ActivityStreams(val streams: Channels) {
    data class Channels(
        val time: List<Double?>?,
        val power: List<Double?>?,
        val heartrate: List<Double?>?,
        val cadence: List<Double?>?,
        val altitude: List<Double?>?,
    ) {
        companion object {
            fun fromJson(v: JsonValue): Channels = Channels(
                time = nums(v, "time"), power = nums(v, "power"),
                heartrate = nums(v, "heartrate"), cadence = nums(v, "cadence"),
                altitude = nums(v, "altitude"),
            )

            private fun nums(v: JsonValue, key: String): List<Double?>? =
                v.optArray(key)?.map { it.asDouble() }
        }

        fun toJson(): JsonValue = jsonObj(
            "time" to time?.let { list -> jsonNullableDoubles(list) },
            "power" to power?.let { list -> jsonNullableDoubles(list) },
            "heartrate" to heartrate?.let { list -> jsonNullableDoubles(list) },
            "cadence" to cadence?.let { list -> jsonNullableDoubles(list) },
            "altitude" to altitude?.let { list -> jsonNullableDoubles(list) },
        )
    }

    companion object {
        fun fromJson(v: JsonValue): ActivityStreams {
            val channels = (v.opt("streams") as? JsonValue.Object)
                ?: throw CloudDecodeException("stream has no streams block")
            return ActivityStreams(Channels.fromJson(channels))
        }
    }

    fun toJson(): JsonValue = jsonObj("streams" to streams.toJson())
}

// MARK: - Responses

/** What every collection route returns. */
data class CollectionResponse(
    val items: List<CloudItem>,
    val revision: Int?,
    val nextCursor: String?,
) {
    companion object {
        fun fromJson(v: JsonValue): CollectionResponse {
            val items = v.optArray("items")
                ?: throw CloudDecodeException("collection has no items")
            return CollectionResponse(
                items = items.map { CloudItem.fromJson(it) },
                revision = v.optInt("revision"),
                nextCursor = v.optString("next_cursor"),
            )
        }
    }
}

/** `POST /api/v1/context/refresh`. */
data class RefreshResponse(
    val readerContext: String,
    val expiresIn: Double?,
    val capabilities: List<String>?,
) {
    override fun toString(): String =
        "RefreshResponse(readerContext=<redacted>, expiresIn=$expiresIn, capabilities=$capabilities)"

    companion object {
        fun fromJson(v: JsonValue): RefreshResponse {
            val context = v.optString("reader_context")
                ?: throw CloudDecodeException("refresh has no reader_context")
            return RefreshResponse(
                readerContext = context,
                expiresIn = v.optDouble("expires_in"),
                capabilities = v.optArray("capabilities")
                    ?.map { it.asString() }?.filterNotNull(),
            )
        }
    }
}

/** `POST /api/v1/devices/pair`. */
data class PairingResponse(
    val deviceCredential: String,
    val deviceSubscriptionKey: String,
    val deviceSignatureAlgorithm: String,
    val deviceCapabilities: List<String>?,
    val signingNamespace: String,
    val readerContext: String,
    val expiresIn: Double?,
) {
    override fun toString(): String =
        "PairingResponse(deviceCredential=$deviceCredential, deviceSubscriptionKey=<redacted>, " +
            "deviceSignatureAlgorithm=$deviceSignatureAlgorithm, deviceCapabilities=$deviceCapabilities, " +
            "signingNamespace=$signingNamespace, readerContext=<redacted>, expiresIn=$expiresIn)"

    companion object {
        fun fromJson(v: JsonValue): PairingResponse = PairingResponse(
            deviceCredential = v.optString("device_credential")
                ?: throw CloudDecodeException("pair has no device_credential"),
            deviceSubscriptionKey = v.optString("device_subscription_key")
                ?: throw CloudDecodeException("pair has no device_subscription_key"),
            deviceSignatureAlgorithm = v.optString("device_signature_algorithm")
                ?: throw CloudDecodeException("pair has no device_signature_algorithm"),
            deviceCapabilities = v.optArray("device_capabilities")
                ?.map { it.asString() }?.filterNotNull(),
            signingNamespace = v.optString("signing_namespace")
                ?: throw CloudDecodeException("pair has no signing_namespace"),
            readerContext = v.optString("reader_context")
                ?: throw CloudDecodeException("pair has no reader_context"),
            expiresIn = v.optDouble("expires_in"),
        )
    }
}

data class CloudDevice(
    val credentialId: String,
    val label: String?,
    val capabilities: List<String>,
    val createdAt: Double?,
    val lastSeenAt: Double?,
    val revoked: Boolean,
    val isSelf: Boolean,
) {
    companion object {
        fun fromJson(v: JsonValue): CloudDevice {
            val capabilities = v.optArray("capabilities")
                ?.map { it.asString() }?.filterNotNull() ?: emptyList()
            return CloudDevice(
                credentialId = v.optString("credential_id")
                    ?: throw CloudDecodeException("device has no credential_id"),
                label = v.optString("label"),
                capabilities = capabilities,
                createdAt = v.optDouble("created_at"),
                lastSeenAt = v.optDouble("last_seen_at"),
                revoked = v.optBoolean("revoked") ?: false,
                isSelf = v.optBoolean("self") ?: false,
            )
        }
    }
}

data class DeviceListResponse(val devices: List<CloudDevice>) {
    companion object {
        fun fromJson(v: JsonValue): DeviceListResponse {
            val devices = v.optArray("devices")
                ?: throw CloudDecodeException("device list has no devices")
            return DeviceListResponse(devices.map { CloudDevice.fromJson(it) })
        }
    }
}

data class DeviceRevokeResponse(val revoked: Boolean) {
    companion object {
        fun fromJson(v: JsonValue): DeviceRevokeResponse =
            DeviceRevokeResponse(revoked = v.optBoolean("revoked") ?: false)
    }
}

// MARK: - JSON builders

private fun jsonScalar(value: Any?): JsonValue = when (value) {
    null -> JsonValue.Null
    is Boolean -> JsonValue.Bool(value)
    is Double -> JsonValue.Number(value)
    is String -> JsonValue.String(value)
    else -> JsonValue.Null
}

private fun jsonList(values: List<JsonValue>): JsonValue = JsonValue.Array(values)

private fun jsonNullableDoubles(values: List<Double?>): JsonValue = JsonValue.Array(
    values.map { if (it == null) JsonValue.Null else JsonValue.Number(it) },
)

/** Build a JSON object from `(key, value)` pairs, omitting null values. */
private fun jsonObj(vararg pairs: Pair<String, Any?>): JsonValue {
    val fields = LinkedHashMap<String, JsonValue>()
    for ((key, value) in pairs) {
        if (value != null) fields[key] = toJsonValue(value)
    }
    return JsonValue.Object(fields)
}

private fun toJsonValue(value: Any?): JsonValue = when (value) {
    null -> JsonValue.Null
    is JsonValue -> value
    is Boolean -> JsonValue.Bool(value)
    is Double -> JsonValue.Number(value)
    is String -> JsonValue.String(value)
    else -> JsonValue.Null
}
