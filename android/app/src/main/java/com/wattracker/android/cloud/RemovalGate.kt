package com.wattracker.android.cloud

import android.content.Context
import android.content.SharedPreferences

/**
 * A tiny durable flag for one failure: a device removal whose credential wipe
 * did not complete (a `commit()` that threw, e.g. the disk was full). The cache
 * was already wiped -- the rider's data is gone -- but the credential survived,
 * so the next launch would otherwise come back "paired". Recording the removal
 * here lets the next start finish it instead of shipping a stale paired device.
 */
interface RemovalGate {
    /** Record that a removal left the credential on disk and must be retried. */
    fun markPending()

    /** True (and cleared) if a prior start left an unfinished removal. */
    fun takePending(): Boolean
}

/** In-process [RemovalGate]; the tests use it, and it is the reference. */
class InMemoryRemovalGate : RemovalGate {
    private val lock = Any()
    private var pending = false

    override fun markPending() {
        synchronized(lock) { pending = true }
    }

    override fun takePending(): Boolean = synchronized(lock) {
        if (pending) {
            pending = false
            true
        } else {
            false
        }
    }
}

/** The durable [RemovalGate] in a small SharedPreferences. */
class SharedPreferencesRemovalGate(context: Context) : RemovalGate {
    private val prefs: SharedPreferences =
        context.applicationContext.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    override fun markPending() {
        prefs.edit().putBoolean(KEY, true).apply()
    }

    override fun takePending(): Boolean {
        if (!prefs.getBoolean(KEY, false)) return false
        prefs.edit().remove(KEY).apply()
        return true
    }

    private companion object {
        const val FILE = "wattracker_removal"
        const val KEY = "pending"
    }
}
