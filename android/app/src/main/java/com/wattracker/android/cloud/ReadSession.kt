package com.wattracker.android.cloud

/**
 * The backend-neutral read seam the screens consume.
 *
 * Two backends implement it: [CloudSession] (the cloud, with its pairing,
 * signing, refresh and revocation) and [LocalClient] (the rider's on-device
 * data -- a placeholder seam for now, not yet wired to a store). The screens
 * depend on this interface, so they cannot tell which backend is serving
 * them, which is what the seam is for.
 */
interface ReadSession {

    /** The cloud's standing for this device; the shell's pairing UI reads it. */
    val deviceState: CloudSession.DeviceState

    /** When the last successful read landed, or null. */
    val lastSuccess: Long?

    /** Whether the cloud backend is paired. */
    val isPaired: Boolean

    /** Last-known data, no network attempt. Suspend so the store read is off-thread. */
    suspend fun cached(route: CloudRoute): CloudSnapshot?

    /** A route's objects, reconciled where possible; last-known data on failure. */
    suspend fun load(route: CloudRoute): CloudSnapshot

    suspend fun activityDetail(activityId: Int): ActivityDetail

    suspend fun activityStreams(activityId: Int): ActivityStreams
}
