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
 * The data source is chosen by the rider, never inferred: the cloud source
 * delegates to the cloud (which reports an unpaired or removed device rather
 * than hiding it), and local data is served only when the local source is
 * explicitly selected. There is no silent fallback from a removed/unpaired
 * cloud to an empty local source -- that would leave the screens showing
 * fresh-looking blanks instead of the pairing or removed UI.
 */
class ReadModel(
    private val cloud: CloudSession,
    private val local: LocalClient,
    private val source: () -> DataSource = { DataSource.Cloud },
) : ReadSession {

    val cloudSession: CloudSession
        get() = cloud

    val localClient: LocalClient
        get() = local

    val activeDataSource: DataSource
        get() = source()

    private fun active(): ReadSession = when (source()) {
        DataSource.Local -> local
        DataSource.Cloud -> cloud
    }

    override val deviceState: CloudSession.DeviceState
        get() = active().deviceState

    override val isPaired: Boolean
        get() = active().isPaired

    override val lastSuccess: Long?
        get() = active().lastSuccess

    override suspend fun cached(route: CloudRoute): CloudSnapshot? = active().cached(route)

    override suspend fun load(route: CloudRoute): CloudSnapshot = active().load(route)

    override suspend fun activityDetail(activityId: Int): ActivityDetail = active().activityDetail(activityId)

    override suspend fun activityStreams(activityId: Int): ActivityStreams = active().activityStreams(activityId)
}
