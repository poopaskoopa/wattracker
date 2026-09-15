package com.wattracker.android.cloud

import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.URL
import java.text.SimpleDateFormat
import java.util.Locale
import java.util.TimeZone
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * A fully-specified HTTP request and the raw response the transport returns.
 *
 * The seam that makes [CloudSession] runnable on a JVM with no Android in it:
 * the session talks to a [CloudTransport] and a fake in tests, and
 * [HttpUrlCloudTransport] in production. Everything the session's decisions
 * depend on -- status, body, `Retry-After`, the server `Date` -- is on
 * [CloudResponse], so the state machine needs no network of its own.
 */
data class CloudRequest(
    val method: String,
    val url: String,
    val headers: Map<String, String>,
    val body: ByteArray,
) {
    // The headers carry a bearer reader context and the subscription key. The
    // generated toString must never print them: this type can end up in a
    // crash report or an assertion message.
    override fun toString(): String =
        "CloudRequest(method=$method, url=$url, headers={${headerSummary()}}, body=${body.size}B)"

    private fun headerSummary(): String =
        headers.entries.joinToString(", ") { (name, value) ->
            "$name=${if (name in SENSITIVE) "<redacted>" else value}"
        }

    companion object {
        private val SENSITIVE = setOf("Authorization", "Ocp-Apim-Subscription-Key")
    }
}

data class CloudResponse(
    val status: Int,
    val body: ByteArray,
    val retryAfterSeconds: Double?,
    val serverDateMillis: Long?,
) {
    val isSuccess: Boolean
        get() = status in 200..299
}

interface CloudTransport {
    suspend fun send(request: CloudRequest): CloudResponse
}

/**
 * Production transport over `HttpURLConnection`.
 *
 * `java.net.http.HttpClient` does not exist on Android, so this is the whole
 * networking story: a connection, a few fixed headers, a body, and the
 * response. Errors are not exceptions -- a 401/404/429/5xx is a `CloudResponse`
 * with that status, because the state machine must read those to decide
 * between a refresh, a clock-skew refusal, a removal, and a backoff.
 *
 * Redirects are turned off and treated as a failure: this API never
 * redirects, and `HttpURLConnection`'s following drops only `Authorization` --
 * not the `Ocp-Apim-Subscription-Key` or the `X-Device-*` / `X-Writer-*`
 * headers -- so a 3xx to another host would hand over credentials it should
 * not have.
 */
class HttpUrlCloudTransport(
    private val clock: () -> Long = { System.currentTimeMillis() / 1000L },
    private val nowMillis: () -> Long = { System.currentTimeMillis() },
) : CloudTransport {

    override suspend fun send(request: CloudRequest): CloudResponse = withContext(Dispatchers.IO) {
        val connection = URL(request.url).openConnection() as HttpURLConnection
        // `setReadTimeout` bounds one blocking read, not the request: a server
        // that drips a byte at a time, each just inside the per-read window,
        // would otherwise hold the request open forever. The deadline below is
        // the bound on the whole thing, checked after the headers and between
        // body chunks; a single blocking read can overshoot it by at most
        // READ_TIMEOUT_MS.
        val deadline = nowMillis() + REQUEST_DEADLINE_MS
        try {
            // The API never redirects; a 3xx is a failure the caller sees as-is.
            connection.instanceFollowRedirects = false
            connection.requestMethod = request.method
            connection.connectTimeout = CONNECT_TIMEOUT_MS
            connection.readTimeout = READ_TIMEOUT_MS
            connection.doOutput = request.body.isNotEmpty()
            request.headers.forEach { (name, value) ->
                connection.setRequestProperty(name, value)
            }
            if (request.body.isNotEmpty()) {
                connection.outputStream.use { it.write(request.body) }
            }

            val status = connection.responseCode
            if (nowMillis() > deadline) throw IOException("the request exceeded the overall deadline")
            val stream = if (status in 200..299) connection.inputStream else connection.errorStream
            val body = stream?.use { readCapped(it, MAX_RESPONSE_BYTES, deadline, nowMillis) } ?: ByteArray(0)
            val retryAfter = connection.getHeaderField("Retry-After")
                ?.let { parseRetryAfter(it, clock()) }
            val serverDate = parseHttpDate(connection.getHeaderField("Date"))
            CloudResponse(status, body, retryAfter, serverDate)
        } finally {
            connection.disconnect()
        }
    }

    companion object {
        private const val CONNECT_TIMEOUT_MS = 15_000
        private const val READ_TIMEOUT_MS = 30_000

        /** Headers to last byte: the whole request may not take longer than this. */
        internal const val REQUEST_DEADLINE_MS = 60_000
    }
}

/**
 * A body past this cap is not this API; it is truncated and then fails to
 * decode, which the client surfaces as a failure rather than a success.
 *
 * A few megabytes, not fifty: the cap becomes bytes, then a String, then a
 * JSON tree in memory, and the largest real payload on this API is a
 * 1500-point stream page -- tens of kilobytes. Four MiB is two orders of
 * magnitude past that, and it is the same order as the iOS cache's
 * per-route cap.
 */
internal const val MAX_RESPONSE_BYTES = 4 * 1024 * 1024

/**
 * Read a response body up to a hard cap, closing the stream, so a runaway
 * body cannot hold the socket or exhaust memory. [deadline] is the request's
 * overall limit (epoch millis): it is checked after every chunk, so a
 * trickling server is cut off instead of held open -- at most one read
 * timeout past the deadline, because a single blocking read is bounded only
 * by the connection's read timeout. Top-level (not a member) so the cap and
 * the deadline are testable without an HTTP connection.
 */
internal fun readCapped(
    stream: InputStream,
    cap: Int = MAX_RESPONSE_BYTES,
    deadline: Long = Long.MAX_VALUE,
    now: () -> Long = { System.currentTimeMillis() },
): ByteArray {
    val out = ByteArrayOutputStream()
    val chunk = ByteArray(8192)
    while (out.size() < cap) {
        // Read at most the remaining allowance, so a full chunk never pushes
        // the result past the cap.
        val read = stream.read(chunk, 0, minOf(chunk.size, cap - out.size()))
        if (read == -1) break
        out.write(chunk, 0, read)
        if (now() > deadline) throw IOException("the response exceeded the overall deadline")
    }
    return out.toByteArray()
}

/**
 * `Retry-After` is a delta-seconds or an HTTP-date. The session clamps and
 * prefers this over a computed backoff, so a value the server did not
 * intend must not be invented here.
 */
internal fun parseRetryAfter(value: String, nowSeconds: Long): Double? {
    val trimmed = value.trim()
    trimmed.toLongOrNull()?.let { return it.coerceAtLeast(0).toDouble() }
    val dateMillis = parseHttpDate(trimmed)
    if (dateMillis != null && dateMillis > 0L) {
        return (dateMillis / 1000.0 - nowSeconds).coerceAtLeast(0.0)
    }
    return null
}

/**
 * Parse an HTTP-date (RFC 1123 `Wed, 21 Oct 2015 07:28:00 GMT`), the format
 * `server.py`'s `Date` header uses. Returns null rather than guessing on a
 * value it cannot parse -- a wrong server clock would corrupt the skew
 * decision more than its absence.
 */
internal fun parseHttpDate(value: String?): Long? {
    if (value == null) return null
    val format = SimpleDateFormat("EEE, dd MMM yyyy HH:mm:ss zzz", Locale.US).apply {
        timeZone = TimeZone.getTimeZone("GMT")
        isLenient = false
    }
    return try {
        format.parse(value.trim())?.time
    } catch (e: Exception) {
        null
    }
}
