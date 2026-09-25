package com.wattracker.android.cloud

import java.io.IOException
import javax.net.ssl.SSLException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class PairingFailureMessageTest {

    @Test
    fun wrongOrExpiredOrServerErrorsCollapseToSameCodeRefusedSentence() {
        val msg404 = PairingFailureMessage.text(CloudSession.Failure.Server(404))
        val msg400 = PairingFailureMessage.text(CloudSession.Failure.Server(400))
        val msg403 = PairingFailureMessage.text(CloudSession.Failure.Server(403))
        val msg410 = PairingFailureMessage.text(CloudSession.Failure.Server(410))
        val msg500 = PairingFailureMessage.text(CloudSession.Failure.Server(500))

        assertEquals(PairingFailureMessage.CODE_REFUSED, msg404)
        assertEquals(PairingFailureMessage.CODE_REFUSED, msg400)
        assertEquals(PairingFailureMessage.CODE_REFUSED, msg403)
        assertEquals(PairingFailureMessage.CODE_REFUSED, msg410)
        assertEquals(PairingFailureMessage.CODE_REFUSED, msg500)
    }

    @Test
    fun cloudServer429And503RenderBusy() {
        val msg429 = PairingFailureMessage.text(CloudSession.Failure.Server(429))
        val msg503 = PairingFailureMessage.text(CloudSession.Failure.Server(503))

        assertTrue(msg429.contains("busy"))
        assertTrue(msg503.contains("busy"))
    }

    @Test
    fun throttledRoundsUpFractionalSeconds() {
        val msg = PairingFailureMessage.text(CloudSession.Failure.Throttled(0.3))
        assertTrue(msg.contains("1s"))
    }

    @Test
    fun offlineConnectionRendersOfflineMessage() {
        val msg = PairingFailureMessage.text(IOException("Connection failed"))
        assertEquals(PairingFailureMessage.OFFLINE, msg)
    }

    @Test
    fun clockSkewRendersClockSkewMessage() {
        val msg = PairingFailureMessage.text(CloudSession.Failure.ClockSkew(300.0))
        assertTrue(msg.contains("300s"))
    }

    @Test
    fun localInsecureUrlRendersHttpsNotice() {
        val msg = LocalPairingFailureMessage.text(LocalClientException.InsecureOrInvalidBaseUrl())
        assertTrue(msg.contains("HTTPS"))
    }

    @Test
    fun localUnauthorizedRendersRefusedNotice() {
        val msg = LocalPairingFailureMessage.text(LocalClientException.Unauthorized())
        assertTrue(msg.contains("refused"))
    }

    @Test
    fun localSslExceptionRendersCertificateNotice() {
        val msg = LocalPairingFailureMessage.text(SSLException("Untrusted certificate"))
        assertTrue(msg.contains("certificate"))
    }

    @Test
    fun removeDeviceFailureMessageFormatsOffline() {
        val msg = RemoveDeviceFailureMessage.text(IOException("Network error"))
        assertTrue(msg.contains("connection"))
    }
}
