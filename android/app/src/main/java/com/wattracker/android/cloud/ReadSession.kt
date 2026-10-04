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

    /**
     * The backend's current identity: it changes when the credential behind
     * the data is replaced -- pairing, sign-out, removal. A load started
     * under an older identity is stale even while it is still running.
     */
    val identity: Int

    /** Last-known data, no network attempt. Suspend so the store read is off-thread. */
    suspend fun cached(route: CloudRoute): CloudSnapshot?

    /** A route's objects, reconciled where possible; last-known data on failure. */
    suspend fun load(route: CloudRoute): CloudSnapshot

    suspend fun activityDetail(activityId: Int): ActivityDetail

    suspend fun activityStreams(activityId: Int): ActivityStreams

    /**
     * Whether this backend can be asleep when asked, so a slow first answer
     * is the server waking rather than something wrong. Only the cloud read
     * app scales to zero; the rider's desktop is either up or it is not.
     */
    val mayBeWaking: Boolean

    /**
     * The rider asked (pull to refresh): lift a backoff that a waking server
     * caused, so the next read goes out now. A backoff the server asked for
     * with 429/503 is not the rider's to shorten and stays in force.
     */
    suspend fun retryNowIfWaking()
}
