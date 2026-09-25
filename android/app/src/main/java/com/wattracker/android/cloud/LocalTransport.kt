package com.wattracker.android.cloud

import java.io.ByteArrayOutputStream
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

data class LocalRequest(
    val method: String,
    val url: String,
    val headers: Map<String, String> = emptyMap(),
    val body: ByteArray = ByteArray(0),
)

data class LocalResponse(
    val status: Int,
    val body: ByteArray,
    val url: String,
    val headers: Map<String, List<String>> = emptyMap(),
) {
    val location: String?
        get() = header("Location")

    val setCookie: String?
        get() = header("Set-Cookie")

    val retryAfterSeconds: Double?
        get() = header("Retry-After")?.toDoubleOrNull()

    private fun header(name: String): String? {
        return headers.entries.firstOrNull { it.key.equals(name, ignoreCase = true) }?.value?.firstOrNull()
    }
}

interface LocalTransport {
    suspend fun send(request: LocalRequest): LocalResponse
}

class HttpLocalTransport(
    private val connectTimeoutMs: Int = 10_000,
    private val readTimeoutMs: Int = 20_000,
) : LocalTransport {

    override suspend fun send(request: LocalRequest): LocalResponse = withContext(Dispatchers.IO) {
        val conn = (URL(request.url).openConnection() as HttpURLConnection).apply {
            requestMethod = request.method
            connectTimeout = connectTimeoutMs
            readTimeout = readTimeoutMs
            instanceFollowRedirects = false
            useCaches = false
            request.headers.forEach { (key, value) -> setRequestProperty(key, value) }
        }

        try {
            if (request.body.isNotEmpty()) {
                conn.doOutput = true
                conn.outputStream.use { it.write(request.body) }
            }

            val status = conn.responseCode
            val stream = if (status in 200..399) conn.inputStream else conn.errorStream
            val body = stream?.use { input ->
                val buffer = ByteArrayOutputStream()
                val chunk = ByteArray(8192)
                var bytesRead: Int
                while (input.read(chunk).also { bytesRead = it } != -1) {
                    buffer.write(chunk, 0, bytesRead)
                }
                buffer.toByteArray()
            } ?: ByteArray(0)

            val finalUrl = conn.url.toString()
            val responseHeaders = conn.headerFields.filterKeys { it != null }

            LocalResponse(
                status = status,
                body = body,
                url = finalUrl,
                headers = responseHeaders,
            )
        } catch (e: Exception) {
            if (e is IOException) throw e
            throw IOException("Local transport error: ${e.message}", e)
        } finally {
            conn.disconnect()
        }
    }
}
