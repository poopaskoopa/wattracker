package com.wattracker.android.cloud

import java.io.IOException
import javax.net.ssl.SSLException
import kotlin.math.abs
import kotlin.math.ceil

/**
 * Maps errors during cloud pairing to user-facing failure messages.
 *
 * Guaranteed security property: wrong, expired, and already-used codes produce
 * the exact same sentence so the client does not act as an oracle. Conditions
 * earn distinct messages only if they are provably independent of the code's value
 * (e.g., offline connection, server throttled 429/503, clock skew).
 */
object PairingFailureMessage {

    const val CODE_REFUSED =
        "That code did not work. Ask the desktop app for a new one and type it in again."

    const val OFFLINE =
        "No connection. Check that this device is on the network, then try again."

    fun text(error: Throwable): String {
        return when (error) {
            is CloudSession.Failure -> textForCloudSessionFailure(error)
            is CredentialStoreException ->
                "Could not save pairing on this device. Restart the app and try again."
            is IOException -> OFFLINE
            else -> CODE_REFUSED
        }
    }

    private fun textForCloudSessionFailure(failure: CloudSession.Failure): String = when (failure) {
        is CloudSession.Failure.Offline -> OFFLINE
        is CloudSession.Failure.Throttled -> busy(failure.retryAfter)
        is CloudSession.Failure.ClockSkew ->
            "This device's clock is ${abs(failure.seconds).toInt()}s away from the server's. " +
                "Set the date and time automatically, then try again."
        is CloudSession.Failure.Server -> busyOrRefused(failure.status, null)
        is CloudSession.Failure.NotPaired, is CloudSession.Failure.DeviceRemoved -> CODE_REFUSED
    }

    private fun busyOrRefused(status: Int, retryAfter: Double?): String {
        return if (status == 429 || status == 503) {
            busy(retryAfter)
        } else {
            CODE_REFUSED
        }
    }

    private fun busy(retryAfter: Double?): String {
        return if (retryAfter != null && retryAfter > 0) {
            "The server is busy. Try again in ${ceil(retryAfter).toInt()}s."
        } else {
            "The server is busy. Wait a moment and try again."
        }
    }
}

/**
 * Maps errors during local desktop pairing to clear, actionable messages.
 */
object LocalPairingFailureMessage {

    fun text(error: Throwable): String {
        return when (error) {
            is LocalClientException.InsecureOrInvalidBaseUrl ->
                error.message ?: "Enter an HTTPS desktop address (e.g. https://192.168.1.10:8000)."
            is LocalClientException.MissingToken ->
                "Enter the connector token from the desktop web settings."
            is LocalClientException.Unauthorized ->
                "The desktop token was refused or has been revoked. Check the token in web settings and try again."
            is LocalClientException.UnexpectedLanding, is LocalClientException.MalformedResponse ->
                "The desktop's reply could not be read. This app may need updating."
            is LocalClientException.Http -> {
                if (error.status == 429 || error.status == 503) {
                    "The desktop server is busy (HTTP ${error.status}). Try again in a moment."
                } else {
                    "HTTP ${error.status} from desktop server."
                }
            }
            is SSLException ->
                "The desktop's HTTPS certificate is not trusted by this device."
            is IOException ->
                "No connection. Check that this device is on the same network as the desktop server."
            is CredentialStoreException ->
                "Could not save local credentials on this device. Restart the app and try again."
            else ->
                error.message ?: "Could not connect to the local desktop server."
        }
    }
}

/**
 * Result of attempting to remove a cloud device.
 */
sealed class RemoveDeviceResult {
    object ServerRevoked : RemoveDeviceResult()
    object LocalFallback : RemoveDeviceResult()
    data class Failed(val message: String) : RemoveDeviceResult()
}

object RemoveDeviceFailureMessage {

    fun text(error: Throwable): String {
        return when (error) {
            is CloudSession.Failure.Offline, is IOException ->
                "No connection. Check your network connection and try again."
            is CloudSession.Failure.Throttled ->
                "The server is busy. Try again in a moment."
            is CloudSession.Failure.Server ->
                if (error.status == 429 || error.status == 503) {
                    "The server is busy (HTTP ${error.status}). Try again in a moment."
                } else {
                    "Could not remove device (HTTP ${error.status})."
                }
            else ->
                error.message ?: "Could not remove device."
        }
    }
}
