package com.wattracker.android.cloud

import com.wattracker.android.BuildConfig
import com.wattracker.android.json.JsonValue
import com.wattracker.android.json.opt
import com.wattracker.android.json.optArray
import com.wattracker.android.json.optString
import java.net.URI
import java.util.Calendar
import java.util.TimeZone
import kotlinx.coroutines.CancellationException

/**
 * Credentials for connecting to a local desktop server.
 */
data class LocalCredentials(
    val serverUrl: String,
    val token: String,
    val username: String? = null,
    val pairedAtMillis: Long = System.currentTimeMillis(),
) {
    override fun toString(): String =
        "LocalCredentials(serverUrl=$serverUrl, token=<redacted>, username=$username, pairedAtMillis=$pairedAtMillis)"

    companion object
}

/**
 * Failures that can occur when communicating with the local desktop backend.
 */
sealed class LocalClientException(message: String) : Exception(message) {
    class InsecureOrInvalidBaseUrl(reason: String = "The desktop address must use HTTPS") : LocalClientException(reason)
    class MissingToken : LocalClientException("The desktop token is missing")
    class Unauthorized : LocalClientException("The local device token was refused or has been revoked")
    data class UnexpectedLanding(val expected: String, val actualPath: String) :
        LocalClientException("Expected $expected, landed on $actualPath")
    data class Http(val status: Int, val path: String, val retryAfter: Double? = null) :
        LocalClientException("HTTP $status from $path")
    data class MalformedResponse(val path: String) :
        LocalClientException("Malformed response from $path")
}

private data class ActiveSession(
    val cookie: String,
    val originUrl: String,
    val token: String,
)

/**
 * Client for the rider's local desktop server reached via the connector protocol over HTTPS.
 *
 * Note: Readers map local server JSON endpoints to domain models for Step 3/4. Full fixture
 * shape pinning is done in Step 4.
 */
class LocalClient(
    private val credentialsProvider: () -> LocalCredentials? = { null },
    private val onRevoked: () -> Unit = {},
    private val transport: LocalTransport = HttpLocalTransport(),
    private val isDebug: Boolean = BuildConfig.DEBUG,
    private val clock: () -> Long = { System.currentTimeMillis() },
) : ReadSession {

    @Volatile private var activeSession: ActiveSession? = null
    @Volatile private var isRevoked: Boolean = false
    @Volatile private var lastSuccessfulRead: Long? = null
    @Volatile private var cachedState: CloudSnapshot? = null

    val credentials: LocalCredentials?
        get() = credentialsProvider()

    override val deviceState: CloudSession.DeviceState
        get() = when {
            isRevoked -> CloudSession.DeviceState.removed
            credentials != null -> CloudSession.DeviceState.paired
            else -> CloudSession.DeviceState.unpaired
        }

    override val isPaired: Boolean
        get() = credentials != null

    override val lastSuccess: Long?
        get() = lastSuccessfulRead

    fun reset() {
        activeSession = null
        cachedState = null
        lastSuccessfulRead = null
    }

    fun markRemoved() {
        isRevoked = true
        reset()
    }

    internal fun setSessionCookieForTesting(cookie: String?, originUrl: String? = null) {
        activeSession = if (cookie != null) {
            val cred = credentials
            val url = originUrl ?: cred?.let { runCatching { validateServerUrl(it.serverUrl, isDebug) }.getOrNull() } ?: ""
            val tok = cred?.token ?: ""
            ActiveSession(cookie, url, tok)
        } else null
    }

    override suspend fun cached(route: CloudRoute): CloudSnapshot? {
        return cachedState?.takeIf { it.route == route }
    }

    /**
     * Authenticate with the local server using the connector token.
     * Mints a single-use ticket via POST /api/connector/session, redeems it via
     * GET /connector/session?token=..., and saves the session cookie in memory.
     *
     * Side-effect free with respect to stored credentials: does not call [onRevoked].
     * Revocation handling is performed by the caller ([getJson]) during read operations.
     */
    suspend fun authenticate(cred: LocalCredentials = requireCredentials()): String {
        val validUrl = validateServerUrl(cred.serverUrl, isDebug)
        val token = cred.token.trim()
        if (token.isEmpty()) throw LocalClientException.MissingToken()

        // Step 1: Mint a session ticket
        val pairReq = LocalRequest(
            method = "POST",
            url = "$validUrl/api/connector/session",
            headers = mapOf(
                "Authorization" to "Bearer $token",
                "Accept" to "application/json",
            ),
        )
        val pairResp = transport.send(pairReq)
        if (pairResp.status == 401) {
            throw LocalClientException.Unauthorized()
        }
        if (pairResp.status !in 200..299) {
            throw LocalClientException.Http(pairResp.status, "/api/connector/session", pairResp.retryAfterSeconds)
        }

        val ticketJson = runCatching { JsonValue.parse(pairResp.body.toString(Charsets.UTF_8)) }.getOrNull()
            as? JsonValue.Object ?: throw LocalClientException.MalformedResponse("/api/connector/session")
        val ticket = ticketJson.optString("ticket")
            ?: throw LocalClientException.MalformedResponse("/api/connector/session")

        // Step 2: Redeem ticket for session cookie
        val redeemUrl = "$validUrl/connector/session?token=${URI(null, null, ticket, null).rawSchemeSpecificPart}"
        val redeemReq = LocalRequest(
            method = "GET",
            url = redeemUrl,
        )
        val redeemResp = transport.send(redeemReq)
        if (redeemResp.status == 401) {
            throw LocalClientException.Unauthorized()
        }
        if (redeemResp.status != 303) {
            throw LocalClientException.Http(redeemResp.status, "/connector/session", redeemResp.retryAfterSeconds)
        }

        val setCookieHeader = redeemResp.setCookie
        val locationHeader = redeemResp.location
        val cleanLandingPath = sanitizePath(locationHeader ?: redeemResp.url)
        if (setCookieHeader.isNullOrBlank() || !isExpectedRootLanding(locationHeader, validUrl)) {
            throw LocalClientException.UnexpectedLanding("/", cleanLandingPath)
        }

        val cookieValue = setCookieHeader.substringBefore(";").trim()
        activeSession = ActiveSession(cookieValue, validUrl, token)
        isRevoked = false

        return cookieValue
    }

    override suspend fun load(route: CloudRoute): CloudSnapshot {
        val cred = requireCredentials()
        val validUrl = validateServerUrl(cred.serverUrl, isDebug)

        val items = when (route) {
            CloudRoute.Dashboard -> loadDashboard(cred, validUrl)
            CloudRoute.Volume -> loadVolume(cred, validUrl)
            CloudRoute.Curve -> loadCurve(cred, validUrl)
            CloudRoute.Profile -> loadProfile(cred, validUrl)
            CloudRoute.Activities -> loadActivities(cred, validUrl)
            CloudRoute.Calendar -> loadCalendar(cred, validUrl)
            CloudRoute.Races -> loadRaces(cred, validUrl)
        }

        lastSuccessfulRead = clock()
        val snapshot = CloudSnapshot(
            route = route,
            revision = 1,
            items = items,
            source = CloudSnapshot.Source.network,
            asOf = clock(),
        )
        cachedState = snapshot
        return snapshot
    }

    override suspend fun activityDetail(activityId: Int): ActivityDetail {
        val cred = requireCredentials()
        val validUrl = validateServerUrl(cred.serverUrl, isDebug)
        val json = getJson(cred, validUrl, "/api/activity/$activityId")
        return ActivityDetail.fromJson(json)
    }

    override suspend fun activityStreams(activityId: Int): ActivityStreams {
        val cred = requireCredentials()
        val validUrl = validateServerUrl(cred.serverUrl, isDebug)
        val json = getJson(cred, validUrl, "/api/activity/$activityId")
        val streamsObj = (json.opt("streams") as? JsonValue.Object)
            ?: ((json.opt("data") as? JsonValue.Object)?.opt("streams") as? JsonValue.Object)
            ?: (json as? JsonValue.Object)

        val timeMin = (streamsObj?.opt("t") as? JsonValue.Array)?.values?.map { (it as? JsonValue.Number)?.value }
        val timeSec = timeMin?.map { min -> min?.let { it * 60.0 } }
        val power = (streamsObj?.opt("power") as? JsonValue.Array)?.values?.map { (it as? JsonValue.Number)?.value }
        val hr = (streamsObj?.opt("heartrate") as? JsonValue.Array)?.values?.map { (it as? JsonValue.Number)?.value }
        val cad = (streamsObj?.opt("cadence") as? JsonValue.Array)?.values?.map { (it as? JsonValue.Number)?.value }
        val alt = (streamsObj?.opt("altitude") as? JsonValue.Array)?.values?.map { (it as? JsonValue.Number)?.value }

        return ActivityStreams(
            streams = ActivityStreams.Channels(
                time = timeSec,
                power = power,
                heartrate = hr,
                cadence = cad,
                altitude = alt,
            ),
        )
    }

    private suspend fun loadDashboard(cred: LocalCredentials, baseUrl: String): List<CloudItem> {
        val items = mutableListOf<CloudItem>()
        var successCount = 0
        var lastError: Throwable? = null

        try {
            val stateJson = getJson(cred, baseUrl, "/api/state")
            val trainingState = TrainingState.fromJson(stateJson)
            items.add(CloudItem("training-state", CloudKind.TrainingState, 1, deleted = false, payload = CloudPayload.TrainingState(trainingState)))
            successCount++
        } catch (e: Exception) {
            if (e is CancellationException) throw e
            lastError = e
        }

        try {
            val loadJson = getJson(cred, baseUrl, "/api/load", mapOf("months" to "3"))
            if (loadJson is JsonValue.Array) {
                loadJson.values.forEachIndexed { idx, elem ->
                    if (elem is JsonValue.Object) {
                        val lp = LoadPoint.fromJson(elem)
                        items.add(CloudItem("load-point-$idx", CloudKind.LoadPoint, 1, deleted = false, payload = CloudPayload.LoadPoint(lp)))
                    }
                }
            }
            successCount++
        } catch (e: Exception) {
            if (e is CancellationException) throw e
            lastError = e
        }

        try {
            val curveJson = getJson(cred, baseUrl, "/api/curve")
            val curve = PowerCurve.fromJson(curveJson)
            items.add(CloudItem("power-curve", CloudKind.Curve, 1, deleted = false, payload = CloudPayload.Curve(curve)))
            successCount++
        } catch (e: Exception) {
            if (e is CancellationException) throw e
            lastError = e
        }

        try {
            val activitiesJson = getJson(cred, baseUrl, "/api/activities")
            if (activitiesJson is JsonValue.Array) {
                activitiesJson.values.take(5).forEachIndexed { idx, elem ->
                    if (elem is JsonValue.Object) {
                        val summary = ActivitySummary.fromJson(elem)
                        items.add(CloudItem("activity-${summary.id ?: idx}", CloudKind.Activity, 1, deleted = false, payload = CloudPayload.Activity(summary)))
                    }
                }
            }
            successCount++
        } catch (e: Exception) {
            if (e is CancellationException) throw e
            lastError = e
        }

        val err = lastError
        if (successCount == 0 && err != null) {
            throw err
        }
        return items
    }

    private suspend fun loadVolume(cred: LocalCredentials, baseUrl: String): List<CloudItem> {
        val json = getJson(cred, baseUrl, "/api/volume")
        val weeksArr = json.optArray("weeks") ?: (json as? JsonValue.Array)?.values ?: return emptyList()
        return weeksArr.mapIndexedNotNull { idx, elem ->
            (elem as? JsonValue.Object)?.let { obj ->
                val vw = VolumeWeek.fromJson(obj)
                CloudItem("volume-week-$idx", CloudKind.VolumeWeek, 1, deleted = false, payload = CloudPayload.VolumeWeek(vw))
            }
        }
    }

    private suspend fun loadCurve(cred: LocalCredentials, baseUrl: String): List<CloudItem> {
        val json = getJson(cred, baseUrl, "/api/curve")
        val curve = PowerCurve.fromJson(json)
        return listOf(CloudItem("power-curve", CloudKind.Curve, 1, deleted = false, payload = CloudPayload.Curve(curve)))
    }

    private suspend fun loadProfile(cred: LocalCredentials, baseUrl: String): List<CloudItem> {
        val json = getJson(cred, baseUrl, "/api/state")
        val profile = RiderProfile.fromJson(json)
        return listOf(CloudItem("rider-profile", CloudKind.Profile, 1, deleted = false, payload = CloudPayload.Profile(profile)))
    }

    private suspend fun loadActivities(cred: LocalCredentials, baseUrl: String): List<CloudItem> {
        val json = getJson(cred, baseUrl, "/api/activities")
        val itemsArr = (json as? JsonValue.Array)?.values ?: json.optArray("activities") ?: return emptyList()
        return itemsArr.mapIndexedNotNull { idx, elem ->
            (elem as? JsonValue.Object)?.let { obj ->
                val summary = ActivitySummary.fromJson(obj)
                CloudItem("activity-${summary.id ?: idx}", CloudKind.Activity, 1, deleted = false, payload = CloudPayload.Activity(summary))
            }
        }
    }

    private suspend fun loadCalendar(cred: LocalCredentials, baseUrl: String): List<CloudItem> {
        val cal = Calendar.getInstance(TimeZone.getDefault())
        val year = cal.get(Calendar.YEAR)
        val month = cal.get(Calendar.MONTH) + 1
        val json = getJson(cred, baseUrl, "/api/calendar", mapOf("year" to year.toString(), "month" to month.toString()))
        val weeksArr = json.optArray("weeks") ?: return emptyList()
        val days = mutableListOf<CloudItem>()
        var dayIdx = 0
        weeksArr.forEach { weekElem ->
            (weekElem as? JsonValue.Array)?.values?.forEach { dayElem ->
                (dayElem as? JsonValue.Object)?.let { dayObj ->
                    val cd = CalendarDay.fromJson(dayObj)
                    days.add(CloudItem("calendar-day-${cd.date ?: dayIdx++}", CloudKind.CalendarDay, 1, deleted = false, payload = CloudPayload.CalendarDay(cd)))
                }
            }
        }
        return days
    }

    private suspend fun loadRaces(cred: LocalCredentials, baseUrl: String): List<CloudItem> {
        val calDays = loadCalendar(cred, baseUrl)
        return calDays.filter { item ->
            val cd = (item.payload as? CloudPayload.CalendarDay)?.value
            cd?.race != null
        }
    }

    private suspend fun getJson(
        cred: LocalCredentials,
        baseUrl: String,
        path: String,
        params: Map<String, String> = emptyMap(),
    ): JsonValue {
        val currentSession = activeSession
        val cookie = try {
            if (currentSession != null && currentSession.originUrl == baseUrl && currentSession.token == cred.token) {
                currentSession.cookie
            } else {
                authenticate(cred)
            }
        } catch (e: LocalClientException.Unauthorized) {
            handleRevocation(cred, baseUrl)
            throw e
        }
        return try {
            sendJsonWithCookie(baseUrl, path, cookie, params)
        } catch (_: LocalClientException.Unauthorized) {
            // Re-authenticate once on 401 or session redirect to /login
            activeSession = null
            val newCookie = try {
                authenticate(cred)
            } catch (e: LocalClientException.Unauthorized) {
                handleRevocation(cred, baseUrl)
                throw e
            }
            sendJsonWithCookie(baseUrl, path, newCookie, params)
        }
    }

    private fun handleRevocation(attemptedCred: LocalCredentials?, attemptedBaseUrl: String) {
        val currentCred = credentials
        if (currentCred != null && attemptedCred != null) {
            val currentUrl = runCatching { validateServerUrl(currentCred.serverUrl, isDebug) }.getOrNull()
            if (currentUrl != attemptedBaseUrl || currentCred.token != attemptedCred.token) {
                // Credentials changed while this request was in flight. Do NOT wipe new credentials.
                return
            }
        }
        markRemoved()
        onRevoked()
    }

    private suspend fun sendJsonWithCookie(
        baseUrl: String,
        path: String,
        cookie: String,
        params: Map<String, String> = emptyMap(),
    ): JsonValue {
        val queryStr = if (params.isNotEmpty()) {
            "?" + params.entries.joinToString("&") { (k, v) ->
                "${URI(null, null, k, null).rawSchemeSpecificPart}=${URI(null, null, v, null).rawSchemeSpecificPart}"
            }
        } else ""
        val req = LocalRequest(
            method = "GET",
            url = "$baseUrl$path$queryStr",
            headers = mapOf(
                "Cookie" to cookie,
                "Accept" to "application/json",
            ),
        )
        val resp = transport.send(req)

        // Detect session expiration / redirect to login (/login or /welcome)
        val loc = resp.location
        if (loc != null && (loc.contains("/login") || loc.contains("/welcome"))) {
            activeSession = null
            throw LocalClientException.Unauthorized()
        }

        if (resp.status == 401) {
            activeSession = null
            throw LocalClientException.Unauthorized()
        }
        if (resp.status !in 200..299) {
            throw LocalClientException.Http(resp.status, path, resp.retryAfterSeconds)
        }

        val bodyStr = resp.body.toString(Charsets.UTF_8)
        return runCatching { JsonValue.parse(bodyStr) }.getOrElse {
            throw LocalClientException.MalformedResponse(path)
        }
    }

    private fun requireCredentials(): LocalCredentials {
        return credentials ?: throw LocalClientException.MissingToken()
    }

    private fun isExpectedRootLanding(location: String?, baseUrl: String): Boolean {
        if (location.isNullOrBlank()) return false
        val trimmed = location.trim()
        if (trimmed == "/" || trimmed == "") return true
        return try {
            val landing = URI(baseUrl).resolve(trimmed)
            val baseUri = URI(baseUrl)
            landing.scheme.equals(baseUri.scheme, ignoreCase = true) &&
                landing.host.equals(baseUri.host, ignoreCase = true) &&
                (landing.port == baseUri.port || (landing.port == -1 && baseUri.port == -1)) &&
                (landing.path.isNullOrEmpty() || landing.path == "/")
        } catch (_: Exception) {
            false
        }
    }

    private fun sanitizePath(urlOrPath: String): String {
        return try {
            val uri = URI(urlOrPath)
            uri.path ?: "/"
        } catch (_: Exception) {
            urlOrPath.substringBefore("?")
        }
    }

    companion object {
        fun validateServerUrl(inputUrl: String, isDebug: Boolean): String {
            val trimmed = inputUrl.trim().removeSuffix("/")
            if (trimmed.isEmpty()) {
                throw LocalClientException.InsecureOrInvalidBaseUrl("Enter a desktop server URL.")
            }

            val uri = runCatching { URI(trimmed) }.getOrNull()
                ?: throw LocalClientException.InsecureOrInvalidBaseUrl("Invalid server URL format.")

            val scheme = uri.scheme?.lowercase()
                ?: throw LocalClientException.InsecureOrInvalidBaseUrl("URL must include scheme (e.g. https://).")
            val host = uri.host
                ?: throw LocalClientException.InsecureOrInvalidBaseUrl("Invalid hostname in URL.")
            if (host.isEmpty()) {
                throw LocalClientException.InsecureOrInvalidBaseUrl("Invalid hostname in URL.")
            }

            if (uri.userInfo != null || uri.query != null || uri.fragment != null) {
                throw LocalClientException.InsecureOrInvalidBaseUrl("Desktop URL must not contain query or authentication parameters.")
            }
            if (uri.path != null && uri.path.isNotEmpty() && uri.path != "/") {
                throw LocalClientException.InsecureOrInvalidBaseUrl("Desktop URL path must be root (/). See README.md:540-549 for reverse proxy setup.")
            }

            if (!isDebug) {
                if (scheme != "https") {
                    throw LocalClientException.InsecureOrInvalidBaseUrl("The desktop address must use HTTPS. See README.md:540-549 for reverse proxy setup.")
                }
            } else {
                if (scheme != "https") {
                    val isLoopback = host.equals("localhost", ignoreCase = true) ||
                        host == "127.0.0.1" ||
                        host == "10.0.2.2"
                    if (scheme != "http" || !isLoopback) {
                        throw LocalClientException.InsecureOrInvalidBaseUrl("The desktop address must use HTTPS unless connecting to local debug host.")
                    }
                }
            }

            val portPart = if (uri.port != -1) ":${uri.port}" else ""
            return "$scheme://$host$portPart"
        }
    }
}
