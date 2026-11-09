package com.wattracker.android.cloud

/** Which backend serves the screens. */
enum class DataSource {
    Cloud,
    Local,
}

/**
 * The read seam the screens actually hold.
 *
 * `deviceState`, `isPaired` and `lastSuccess` reflect the active data source.
 * A launch resolves the rider's [BackendSelection] to one [DataSource] and the
 * source is never inferred from a failure: the cloud reports an unpaired or
 * removed device rather than hiding it, and local data is served only when the
 * resolved source is local. There is no silent fallback from a removed or
 * unpaired cloud to an empty local source -- that would leave the screens
 * showing fresh-looking blanks instead of the pairing or removed UI.
 *
 * [cloud] is null when the cloud could not be built at all, which is a
 * different thing from the cloud being unpaired and is reported by
 * `WatTrackerApp.cloudStartFailure`. [AbsentCloud] keeps that difference off
 * the read path: the screens keep asking one seam, and the seam says unpaired.
 */
class ReadModel(
    private val cloud: CloudSession?,
    private val local: LocalClient,
    private val source: () -> DataSource = { DataSource.Cloud },
) : ReadSession {

    /** The cloud backend, or null when this launch could not build one. */
    val cloudSession: CloudSession?
        get() = cloud

    val localClient: LocalClient
        get() = local

    val activeDataSource: DataSource
        get() = source()

    private fun active(): ReadSession = when (source()) {
        DataSource.Local -> local
        DataSource.Cloud -> cloud ?: AbsentCloud
    }

    override val deviceState: CloudSession.DeviceState
        get() = active().deviceState

    override val isPaired: Boolean
        get() = active().isPaired

    override val identity: Int
        get() = active().identity

    override val lastSuccess: Long?
        get() = active().lastSuccess

    override suspend fun cached(route: CloudRoute): CloudSnapshot? = active().cached(route)

    override suspend fun load(route: CloudRoute): CloudSnapshot = active().load(route)

    // The month overloads go to whichever backend is active: only the local
    // adapter gives the month meaning (the cloud feed spans every month and
    // its default ignores it), but which adapter that is is exactly what the
    // seam is here to hide from the screens.
    override suspend fun cached(route: CloudRoute, month: CalendarMonth?): CloudSnapshot? = active().cached(route, month)

    override suspend fun load(route: CloudRoute, month: CalendarMonth?): CloudSnapshot = active().load(route, month)

    override suspend fun activityDetail(activityId: Int): ActivityDetail = active().activityDetail(activityId)

    override suspend fun activityStreams(activityId: Int): ActivityStreams = active().activityStreams(activityId)

    override val mayBeWaking: Boolean
        get() = active().mayBeWaking

    override suspend fun retryNowIfWaking() = active().retryNowIfWaking()
}

/**
 * The cloud for a launch that could not construct one.
 *
 * It answers exactly as an unpaired [CloudSession] does -- no cache, a
 * `NotPaired` on every read -- because that is the honest read-side answer:
 * there is no cloud to read. *Why* there is none is not a read result, so it
 * stays out of the seam; the app-level gate (A1) reads
 * `WatTrackerApp.cloudStartFailure` to tell "not paired yet" from "this device
 * cannot be paired", which is the difference between a pairing screen and a
 * refusal.
 */
private object AbsentCloud : ReadSession {

    override val deviceState: CloudSession.DeviceState
        get() = CloudSession.DeviceState.unpaired

    override val isPaired: Boolean
        get() = false

    override val identity: Int
        get() = 0

    override val lastSuccess: Long?
        get() = null

    override suspend fun cached(route: CloudRoute): CloudSnapshot? = null

    override suspend fun load(route: CloudRoute): CloudSnapshot = throw CloudSession.Failure.NotPaired()

    override suspend fun activityDetail(activityId: Int): ActivityDetail = throw CloudSession.Failure.NotPaired()

    override suspend fun activityStreams(activityId: Int): ActivityStreams = throw CloudSession.Failure.NotPaired()

    /** Nothing is asleep when there is no client to wake it. */
    override val mayBeWaking: Boolean
        get() = false

    override suspend fun retryNowIfWaking() = Unit
}
