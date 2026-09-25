package com.wattracker.android.cloud

import android.content.Context
import android.content.SharedPreferences

/**
 * A tiny durable flag for one failure: a device removal whose credential wipe
 * did not complete (a `commit()` that threw, e.g., the disk was full). The
 * cache was already wiped -- the rider's data is gone -- but the credential
 * survived, so the next launch would otherwise come back "paired". Recording
 * the removal here lets the next start finish it instead of shipping a stale
 * paired device.
 *
 * The flag's whole value is its durability, so both writes are `commit()`,
 * never `apply()`: an `apply()` that dies before it hits disk trades a lost
 * credential for a lost sign-out -- the very failure the gate exists to
 * catch. And [clearPending] is the caller's to call, and only after the
 * retried wipe succeeds: a flag cleared before the wipe, with the process
 * dying in between, leaves neither the flag nor the wipe.
 *
 * A write the backing store refuses is thrown, not ignored: `commit()`
 * reports a failed durable write with `false` rather than an exception, and
 * an unrecorded failure is exactly the state this gate exists to catch.
 */
interface RemovalGate {
    /** Record that a removal left the credential on disk and must be retried. Durable; throws if the record cannot be written. */
    fun markPending()

    /** True if a prior start left an unfinished removal. Read-only; it does not consume the flag. */
    fun isPending(): Boolean

    /** Clear the flag, durably. Call it only after the retried wipe succeeded; throws if the clear cannot be written. */
    fun clearPending()
}

/** In-process [RemovalGate]; the tests use it, and it is the reference. */
class InMemoryRemovalGate : RemovalGate {
    private val lock = Any()
    private var pending = false

    override fun markPending() {
        synchronized(lock) { pending = true }
    }

    override fun isPending(): Boolean = synchronized(lock) { pending }

    override fun clearPending() {
        synchronized(lock) { pending = false }
    }
}

/** The durable [RemovalGate] in a small SharedPreferences. */
class SharedPreferencesRemovalGate(context: Context) : RemovalGate {
    private val prefs: SharedPreferences =
        context.applicationContext.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    override fun markPending() {
        // `commit()`, not `apply()`: this flag is the only record that a
        // sign-out was left half-finished, and a write that can be lost on
        // disk is not a record. commit() signals a refused write with false
        // rather than throwing; surface it.
        if (!prefs.edit().putBoolean(KEY, true).commit()) {
            throw IllegalStateException("could not persist the removal flag")
        }
    }

    override fun isPending(): Boolean = prefs.getBoolean(KEY, false)

    override fun clearPending() {
        // `commit()`: the clear must be on disk before the process can die
        // with the wipe done and the flag still set -- that is harmless but
        // not what this call promises. A refused write means the clear did
        // not happen, so it throws and the caller re-records the flag.
        if (!prefs.edit().remove(KEY).commit()) {
            throw IllegalStateException("could not clear the removal flag")
        }
    }

    private companion object {
        const val FILE = "wattracker_removal"
        const val KEY = "pending"
    }
}
