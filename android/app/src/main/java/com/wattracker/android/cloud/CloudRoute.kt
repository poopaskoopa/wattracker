package com.wattracker.android.cloud

/**
 * The read-plane routes, and which of them serve deltas.
 *
 * `api.py` marks `dashboard`, `volume` and `curve` `mobile=True`; only those
 * three (plus `activities`, which pages the same way) read `since=`, return a
 * `revision`, page with a signed `cursor`, and include tombstones. The rest
 * answer `{"items": [...]}` and nothing else, so there is no checkpoint to
 * cache against and asking for one would be asking a question the route does
 * not answer. That is a fact about the route, kept here, rather than something
 * inferred from a response -- an absent `revision` also describes a truncated
 * body.
 */
enum class CloudRoute(private val rawValue: String) {
    Dashboard("dashboard"),
    Volume("volume"),
    Curve("curve"),
    Profile("profile"),
    Activities("activities"),
    Calendar("calendar"),
    Races("races"),
    ;

    /** The absolute path the route lives at. */
    val path: String
        get() = "/api/v1/context/$rawValue"

    /** Whether this route serves deltas (`since`, `revision`, `cursor`). */
    val servesDeltas: Boolean
        get() = this == Dashboard || this == Volume || this == Curve || this == Activities

    /** The cache key: a fixed alphabet, never a rider- or server-chosen value. */
    val cacheKey: String
        get() = rawValue
}
