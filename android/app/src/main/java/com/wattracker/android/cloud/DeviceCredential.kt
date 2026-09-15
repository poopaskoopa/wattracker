package com.wattracker.android.cloud

import com.wattracker.android.json.JsonValue
import com.wattracker.android.json.asString
import com.wattracker.android.json.optArray
import com.wattracker.android.json.optString
import com.wattracker.android.json.toJson

/**
 * A cloud device binding: what the pairing response gave back, minus the
 * `reader_context` (a short-lived secret stored separately).
 *
 * This is the durable half of a pairing. The other half -- a signing key that
 * cannot be exported -- lives in the [DeviceKeyStore], never here. The two are
 * bound by the server, which is what makes [PairingResult] a pair and not two
 * independent blobs.
 */
data class PairedDevice(
    val credentialId: String,
    val signingNamespace: String,
    val subscriptionKey: String,
    val capabilities: List<String> = emptyList(),
    val label: String? = null,
) {
    fun toJson(): String {
        val fields = LinkedHashMap<String, JsonValue>()
        fields["credential_id"] = JsonValue.String(credentialId)
        fields["signing_namespace"] = JsonValue.String(signingNamespace)
        fields["subscription_key"] = JsonValue.String(subscriptionKey)
        fields["capabilities"] = JsonValue.Array(capabilities.map { JsonValue.String(it) })
        if (label != null) fields["label"] = JsonValue.String(label)
        return JsonValue.Object(fields).toJson()
    }

    override fun toString(): String =
        "PairedDevice(credentialId=$credentialId, signingNamespace=$signingNamespace, " +
            "subscriptionKey=<redacted>, capabilities=$capabilities, label=$label)"

    companion object {
        fun fromJson(text: String): PairedDevice {
            val v = JsonValue.parse(text)
            return PairedDevice(
                credentialId = v.optString("credential_id")
                    ?: throw CloudDecodeException("no credential_id"),
                signingNamespace = v.optString("signing_namespace")
                    ?: throw CloudDecodeException("no signing_namespace"),
                subscriptionKey = v.optString("subscription_key")
                    ?: throw CloudDecodeException("no subscription_key"),
                capabilities = v.optArray("capabilities")?.mapNotNull { it.asString() } ?: emptyList(),
                label = v.optString("label"),
            )
        }
    }
}

/** The result of a successful pairing: a device and its first reader context. */
data class PairingResult(
    val device: PairedDevice,
    val initialReaderContext: String,
    val expiresIn: Double?,
) {
    override fun toString(): String =
        "PairingResult(device=$device, initialReaderContext=<redacted>, expiresIn=$expiresIn)"
}

/**
 * A local-backend credential: the rider's server URL and the connector token.
 *
 * The token is the bearer secret; it is what `LocalClient` exchanges for a
 * session, and it is what must never leave the encrypted store or reach a
 * log. The server URL is rider-entered (no hostname is baked in).
 */
data class LocalCredential(
    val serverUrl: String,
    val token: String,
    val label: String? = null,
) {
    fun toJson(): String {
        val fields = LinkedHashMap<String, JsonValue>()
        fields["server_url"] = JsonValue.String(serverUrl)
        fields["token"] = JsonValue.String(token)
        if (label != null) fields["label"] = JsonValue.String(label)
        return JsonValue.Object(fields).toJson()
    }

    override fun toString(): String =
        "LocalCredential(serverUrl=$serverUrl, token=<redacted>, label=$label)"

    companion object {
        fun fromJson(text: String): LocalCredential {
            val v = JsonValue.parse(text)
            return LocalCredential(
                serverUrl = v.optString("server_url")
                    ?: throw CloudDecodeException("no server_url"),
                token = v.optString("token")
                    ?: throw CloudDecodeException("no token"),
                label = v.optString("label"),
            )
        }
    }
}
