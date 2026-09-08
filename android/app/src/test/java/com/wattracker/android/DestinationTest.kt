package com.wattracker.android

import com.wattracker.android.shell.Destination
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class DestinationTest {

    @Test
    fun `fromRoute round-trips all destinations`() {
        for (destination in Destination.entries) {
            assertEquals(destination, Destination.fromRoute(destination.route))
        }
    }

    @Test
    fun `fromRoute returns null for garbage`() {
        assertNull(Destination.fromRoute("nonexistent"))
        assertNull(Destination.fromRoute(""))
        assertNull(Destination.fromRoute(null))
    }

    @Test
    fun `routes are lowercase enum names`() {
        assertEquals("dashboard", Destination.Dashboard.route)
        assertEquals("activities", Destination.Activities.route)
        assertEquals("calendar", Destination.Calendar.route)
        assertEquals("volume", Destination.Volume.route)
        assertEquals("settings", Destination.Settings.route)
    }
}
