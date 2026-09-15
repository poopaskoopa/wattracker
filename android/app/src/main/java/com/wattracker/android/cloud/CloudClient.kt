package com.wattracker.android.cloud

import com.wattracker.android.json.JsonValue
import com.wattracker.android.json.toJson
import java.net.URLEncoder
import java.security.SecureRandom
import java.util.Base64

/**
 * The transport result the [CloudSession] reasons about.
 *
 * Every decision in the state machine -- refresh, clock-skew refusal, a strike
 * toward removal, a backoff, a retry -- reads the status plus the two
 * response facts that carry meaning: `Retry-After` and the server `Date`. A
 * sealed result keeps those beside the parsed body instead of encoding them
 * in an exception message.
 */
sealed class CloudApiResult<out T> {
    abstract val status: Int
    abstract val retryAfterSeconds: Double?
    abstract val serverDateMillis: Long?

    data class Success<T>(
        val value: T,
        override val status: Int,
        override val retryAfterSeconds: Double?,
        override val serverDateMillis: Long?,
    ) : CloudApiResult<T>()

    data class Failure(
        override val status: Int,
        val body: ByteArray,
        override val retryAfterSeconds: Double?,
        override val serverDateMillis: Long?,
    ) : CloudApiResult<Nothing>()
}

/** What a refresh produced, and what the server's clock said while doing it. */
data class RefreshOutcome(
    val readerContext: String,
    val expiresIn: Double,
    val serverDateMillis: Long?,
) {
    override fun toString(): String =
        "RefreshOutcome(readerContext=<redacted>, expiresIn=$expiresIn, serverDateMillis=$serverDateMillis)"
}

/**
 * One request each, typed, with nothing remembered between them.
 *
 * The Kotlin port of `CloudClient.swift`. Pair once with a code the rider's
 * desktop minted, trade the durable device credential for a short-lived reader
 * context, then read a collection. Every signed request goes through
 * [CanonicalRequest], which is the whole point.
 *
 * This layer holds no state -- no token, no cache, no retry, no idea what time
 * the last request failed at. All of that is [CloudSession]; keeping the two
 * apart is what lets the lifecycle be tested against scripted responses
 * without a network.
 *
 * Nothing here logs. The refresh body carries a bearer token and the writer
 * requests carry a credential; a `println` of a response is a leak, so the
 * only failure surface is the [CloudApiResult] status.
 */
class CloudClient(
    baseScheme: String,
    baseAuthority: String,
    private val signer: DeviceSigner,
    private val transport: CloudTransport,
    private val clock: () -> Long = { System.currentTimeMillis() / 1000L },
) {

    private val baseUrl: String = "$baseScheme://${baseAuthority.trimEnd('/')}"

    private val secureRandom = SecureRandom()

    // MARK: - POST /api/v1/devices/pair

    /**
     * Redeem a single-use pairing code for a durable device credential. The
     * code is the authorization: this request carries no signature, because
     * there is not yet a credential to sign with. What it does carry is the
     * public half of the key every later request is signed with.
     */
    suspend fun pair(code: String, label: String?): CloudApiResult<PairingResult> {
        val body = pairBody(code, label)
        val response = transport.send(
            CloudRequest(
                method = "POST",
                url = endpoint("/api/v1/devices/pair", emptyList()),
                headers = mapOf("Content-Type" to "application/json", "Accept" to "application/json"),
                body = body.toByteArray(Charsets.UTF_8),
            ),
        )
        return decode(response) { json ->
            val payload = PairingResponse.fromJson(json)
            // We offered one algorithm; a response naming a different one means
            // the server would sign for a key it does not actually hold.
            if (payload.deviceSignatureAlgorithm != SIGNATURE_ALGORITHM) {
                throw CloudDecodeException(
                    "pair returned an unsupported signature algorithm: ${payload.deviceSignatureAlgorithm}"
                )
            }
            PairingResult(
                device = PairedDevice(
                    credentialId = payload.deviceCredential,
                    signingNamespace = payload.signingNamespace,
                    subscriptionKey = payload.deviceSubscriptionKey,
                    capabilities = payload.deviceCapabilities ?: listOf("read"),
                    label = label,
                ),
                initialReaderContext = payload.readerContext,
                expiresIn = payload.expiresIn,
            )
        }
    }

    // MARK: - POST /api/v1/context/refresh

    /**
     * Trade the device credential for a fresh reader context.
     *
     * The refresh envelope is fixed by the server and every canonical request
     * is byte-exact. Refresh carries: no body, the idempotency key
     * `context-refresh`, and an EMPTY revision string that still contributes a
     * zero length to the framing. The `serverDate` is what the clock-skew
     * refusal is built from.
     */
    suspend fun refreshReaderContext(device: PairedDevice): CloudApiResult<RefreshOutcome> {
        val path = "/api/v1/context/refresh"
        val timestamp = clock().toString()
        val nonce = freshNonce()
        val canonical = canonical(
            method = "POST",
            path = path,
            device = device,
            timestamp = timestamp,
            nonce = nonce,
            idempotencyKey = "context-refresh",
            revision = "",
        )
        val response = transport.send(
            CloudRequest(
                method = "POST",
                url = endpoint(path, emptyList()),
                headers = mapOf(
                    "X-Device-Credential" to device.credentialId,
                    "X-Device-Timestamp" to timestamp,
                    "X-Device-Nonce" to nonce,
                    "X-Device-Signature" to signer.signature(over = canonical).toHex(),
                    "Ocp-Apim-Subscription-Key" to device.subscriptionKey,
                    "Accept" to "application/json",
                ),
                body = EMPTY,
            ),
        )
        return decode(response) { json ->
            val payload = RefreshResponse.fromJson(json)
            // A missing or non-positive `expires_in` would otherwise mint a token
            // that is already expired and force a refresh on every single call.
            RefreshOutcome(
                readerContext = payload.readerContext,
                expiresIn = payload.expiresIn?.takeIf { it > 0.0 } ?: CloudSession.defaultContextLifetime,
                serverDateMillis = response.serverDateMillis,
            )
        }
    }

    // MARK: - GET /api/v1/context/*

    /**
     * One page of a collection. `since` and `cursor` are accepted only where
     * the route serves deltas; the caller checks [CloudRoute.servesDeltas].
     *
     * Reads carry the bearer reader context and the subscription key, and no
     * per-read signature -- the reader context IS the authorization.
     */
    suspend fun collection(
        route: CloudRoute,
        readerContext: String,
        device: PairedDevice,
        since: Int? = null,
        cursor: String? = null,
    ): CloudApiResult<CollectionResponse> {
        val params = buildList {
            if (since != null) add("since" to since.toString())
            if (cursor != null) add("cursor" to cursor)
        }
        val response = transport.send(
            CloudRequest(
                method = "GET",
                url = endpoint(route.path, params),
                headers = mapOf(
                    "Authorization" to "Bearer $readerContext",
                    "Ocp-Apim-Subscription-Key" to device.subscriptionKey,
                    "Accept" to "application/json",
                ),
                body = EMPTY,
            ),
        )
        return decode(response) { json -> CollectionResponse.fromJson(json) }
    }

    /** One activity-owned object, fetched after the rider opens a ride. */
    suspend fun activityDetail(
        activityId: Int,
        readerContext: String,
        device: PairedDevice,
    ): CloudApiResult<CloudItem> =
        activityObject("activity-detail-$activityId", CloudKind.ActivityDetail, readerContext, device)

    suspend fun activityStreams(
        activityId: Int,
        readerContext: String,
        device: PairedDevice,
    ): CloudApiResult<CloudItem> =
        activityObject("stream-$activityId", CloudKind.Stream, readerContext, device)

    private suspend fun activityObject(
        objectId: String,
        expectedKind: CloudKind,
        readerContext: String,
        device: PairedDevice,
    ): CloudApiResult<CloudItem> {
        val path = "/api/v1/context/activities/$objectId"
        val response = transport.send(
            CloudRequest(
                method = "GET",
                url = endpoint(path, emptyList()),
                headers = mapOf(
                    "Authorization" to "Bearer $readerContext",
                    "Ocp-Apim-Subscription-Key" to device.subscriptionKey,
                    "Accept" to "application/json",
                ),
                body = EMPTY,
            ),
        )
        return decode(response) { json ->
            val item = CloudItem.fromJson(json)
            if (item.id != objectId || item.kind != expectedKind) {
                throw CloudDecodeException("$path returned the wrong object")
            }
            item
        }
    }

    // MARK: - Signed device administration (X-Writer-*)

    /** List this credential's devices. Uses the writer scheme, not the reader one. */
    suspend fun devices(device: PairedDevice): CloudApiResult<List<CloudDevice>> {
        val path = "/api/v1/devices"
        val (timestamp, nonce, signature) = signedWriter("GET", path, device, "device-list")
        val response = transport.send(
            CloudRequest(
                method = "GET",
                url = endpoint(path, emptyList()),
                headers = writerHeaders(device, timestamp, nonce, "device-list", signature),
                body = EMPTY,
            ),
        )
        return decode(response) { json -> DeviceListResponse.fromJson(json).devices }
    }

    /**
     * Revoke a device by credential id. A different signed route, not a read.
     * [credentialId] names the target -- it may be a sibling this writer may
     * revoke, which the server allows -- and is what goes into the URL and the
     * signed path.
     */
    suspend fun revoke(credentialId: String, device: PairedDevice): CloudApiResult<Boolean> {
        val path = "/api/v1/devices/$credentialId/revoke"
        val (timestamp, nonce, signature) = signedWriter("POST", path, device, "device-revoke")
        val response = transport.send(
            CloudRequest(
                method = "POST",
                url = endpoint(path, emptyList()),
                headers = writerHeaders(device, timestamp, nonce, "device-revoke", signature),
                body = EMPTY,
            ),
        )
        return decode(response) { json -> DeviceRevokeResponse.fromJson(json).revoked }
    }

    private fun signedWriter(
        method: String,
        path: String,
        device: PairedDevice,
        idempotencyKey: String,
    ): Triple<String, String, String> {
        // The writer scheme signs a fixed revision of "0" -- unlike refresh,
        // whose revision is empty -- and echoes the idempotency key and
        // revision in their own headers.
        val timestamp = clock().toString()
        val nonce = freshNonce()
        val canonical = canonical(
            method = method,
            path = path,
            device = device,
            timestamp = timestamp,
            nonce = nonce,
            idempotencyKey = idempotencyKey,
            revision = "0",
        )
        return Triple(timestamp, nonce, signer.signature(over = canonical).toHex())
    }

    private fun writerHeaders(
        device: PairedDevice,
        timestamp: String,
        nonce: String,
        idempotencyKey: String,
        signature: String,
    ): Map<String, String> = mapOf(
        "X-Writer-Credential" to device.credentialId,
        "X-Writer-Timestamp" to timestamp,
        "X-Writer-Nonce" to nonce,
        "X-Writer-Idempotency-Key" to idempotencyKey,
        "X-Writer-Revision" to "0",
        "X-Writer-Signature" to signature,
        "Ocp-Apim-Subscription-Key" to device.subscriptionKey,
        "Accept" to "application/json",
    )

    // MARK: - Plumbing

    /** The canonical framing for one signed request. */
    private fun canonical(
        method: String,
        path: String,
        device: PairedDevice,
        timestamp: String,
        nonce: String,
        idempotencyKey: String,
        revision: String,
    ): ByteArray = CanonicalRequest.bytes(
        method = method,
        path = path,
        namespace = device.signingNamespace,
        timestamp = timestamp,
        nonce = nonce,
        bodyDigest = CanonicalRequest.digestBody(EMPTY),
        idempotencyKey = idempotencyKey,
        revision = revision,
    )

    /**
     * Join the base URL to an absolute request path.
     *
     * The path goes into the URL and (for signed requests) into the canonical
     * request, and those must be the same characters. The query is percent-
     * encoded separately because the server signs `request.url.path` and never
     * the query.
     */
    private fun endpoint(path: String, params: List<Pair<String, String>>): String {
        val base = "$baseUrl$path"
        if (params.isEmpty()) return base
        val query = params.joinToString("&") { (k, v) ->
            "${URLEncoder.encode(k, "UTF-8")}=${URLEncoder.encode(v, "UTF-8")}"
        }
        return "$base?$query"
    }

    private fun pairBody(code: String, label: String?): String {
        val fields = LinkedHashMap<String, JsonValue>()
        fields["code"] = JsonValue.String(code)
        fields["public_key"] = JsonValue.String(signer.publicKeyX963.toHex())
        fields["signature_algorithm"] = JsonValue.String(SIGNATURE_ALGORITHM)
        if (label != null) fields["label"] = JsonValue.String(label)
        return JsonValue.Object(fields).toJson()
    }

    /**
     * A nonce the replay guard has not seen. Freshness comes from this, not
     * from the signature: the server keys its replay guard on (namespace,
     * credential, nonce) and never on signature bytes, which is what makes
     * accepting a malleable signature safe.
     */
    private fun freshNonce(): String {
        val bytes = ByteArray(24)
        secureRandom.nextBytes(bytes)
        return Base64.getUrlEncoder().withoutPadding().encodeToString(bytes)
    }

    private fun <T> decode(response: CloudResponse, parse: (JsonValue) -> T): CloudApiResult<T> {
        if (!response.isSuccess) {
            return CloudApiResult.Failure(
                response.status, response.body, response.retryAfterSeconds, response.serverDateMillis,
            )
        }
        return try {
            val json = JsonValue.parse(String(response.body, Charsets.UTF_8))
            CloudApiResult.Success(parse(json), response.status, response.retryAfterSeconds, response.serverDateMillis)
        } catch (e: Exception) {
            // A 200 with an unreadable body is a protocol drift, surfaced as a
            // failure so the state machine can react; the body is not quoted
            // because on this API it is the rider's data and, on refresh, a
            // bearer token.
            CloudApiResult.Failure(MALFORMED_STATUS, response.body, response.retryAfterSeconds, response.serverDateMillis)
        }
    }

    companion object {
        private val EMPTY = ByteArray(0)
        private const val MALFORMED_STATUS = 0
        private const val SIGNATURE_ALGORITHM = "ecdsa-p256-sha256"
    }
}
