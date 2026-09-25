package com.wattracker.android.cloud

/**
 * The on-device read backend seam.
 *
 * The local backend -- the rider's desktop server reached over HTTPS through
 * the connector (plan §2.3) -- is **not implemented yet**. This stub keeps the
 * [ReadSession] seam and the data-source toggle honest: selecting it returns a
 * clear [ReadUnavailable] rather than silently serving an empty `local_payloads`
 * table that nothing writes. When the connector lands, its implementation
 * replaces this body; [ReadModel] already routes to it only when the local
 * source is explicitly selected.
 */
class LocalClient : ReadSession {

    override val deviceState: CloudSession.DeviceState
        get() = CloudSession.DeviceState.unpaired

    override val isPaired: Boolean
        get() = false

    override val lastSuccess: Long?
        get() = null

    override suspend fun cached(route: CloudRoute): CloudSnapshot? = null

    override suspend fun load(route: CloudRoute): CloudSnapshot =
        throw ReadUnavailable("The local backend is not implemented yet")

    override suspend fun activityDetail(activityId: Int): ActivityDetail =
        throw ReadUnavailable("The local backend is not implemented yet")

    override suspend fun activityStreams(activityId: Int): ActivityStreams =
        throw ReadUnavailable("The local backend is not implemented yet")
}

/** A read this backend cannot serve -- distinct from a transport failure. */
class ReadUnavailable(message: String) : Exception(message)
