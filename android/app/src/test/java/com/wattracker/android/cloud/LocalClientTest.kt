package com.wattracker.android.cloud

import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.InetAddress
import java.net.ServerSocket

class LocalClientTest {

    private class FakeLocalTransport : LocalTransport {
        val requests = mutableListOf<LocalRequest>()
        var sessionTicketResponse = LocalResponse(
            status = 200,
            body = "{\"ticket\": \"test-ticket-123\"}".toByteArray(Charsets.UTF_8),
            url = "http://10.0.2.2:8000/api/connector/session",
        )
        var redeemResponse = LocalResponse(
            status = 303,
            body = ByteArray(0),
            url = "http://10.0.2.2:8000/connector/session?token=test-ticket-123",
            headers = mapOf(
                "Set-Cookie" to listOf("session=test-cookie-val; Path=/; HttpOnly"),
                "Location" to listOf("/"),
            ),
        )
        var apiStateResponse = LocalResponse(
            status = 200,
            body = "{\"ctl\": 50.0, \"atl\": 60.0, \"tsb\": -10.0, \"ftp\": 250}".toByteArray(Charsets.UTF_8),
            url = "http://10.0.2.2:8000/api/state",
        )

        override suspend fun send(request: LocalRequest): LocalResponse {
            requests.add(request)
            return when {
                request.url.contains("/api/connector/session") -> sessionTicketResponse
                request.url.contains("/connector/session") -> redeemResponse
                request.url.contains("/api/state") -> apiStateResponse
                else -> LocalResponse(status = 200, body = "{}".toByteArray(Charsets.UTF_8), url = request.url)
            }
        }
    }

    @Test
    fun validateServerUrlRejectsHttpInRelease() {
        try {
            LocalClient.validateServerUrl("http://example.com:8000", isDebug = false)
            fail("Expected InsecureOrInvalidBaseUrl")
        } catch (_: LocalClientException.InsecureOrInvalidBaseUrl) {
            // expected
        }
    }

    @Test
    fun validateServerUrlRejectsHttpLocalhostInRelease() {
        try {
            LocalClient.validateServerUrl("http://localhost:8000", isDebug = false)
            fail("Expected InsecureOrInvalidBaseUrl in release")
        } catch (_: LocalClientException.InsecureOrInvalidBaseUrl) {
            // expected
        }
    }

    @Test
    fun validateServerUrlRejectsInvalidPathOrQueryOrUser() {
        try {
            LocalClient.validateServerUrl("https://example.com:8000/subpath", isDebug = false)
            fail("Expected InsecureOrInvalidBaseUrl for path")
        } catch (_: LocalClientException.InsecureOrInvalidBaseUrl) {}

        try {
            LocalClient.validateServerUrl("https://example.com:8000?query=1", isDebug = false)
            fail("Expected InsecureOrInvalidBaseUrl for query")
        } catch (_: LocalClientException.InsecureOrInvalidBaseUrl) {}

        try {
            LocalClient.validateServerUrl("https://user:pass@example.com:8000", isDebug = false)
            fail("Expected InsecureOrInvalidBaseUrl for userinfo")
        } catch (_: LocalClientException.InsecureOrInvalidBaseUrl) {}
    }

    @Test
    fun validateServerUrlAllowsHttpLoopbackInDebug() {
        val url1 = LocalClient.validateServerUrl("http://localhost:8000", isDebug = true)
        assertEquals("http://localhost:8000", url1)

        val url2 = LocalClient.validateServerUrl("http://10.0.2.2:8000/", isDebug = true)
        assertEquals("http://10.0.2.2:8000", url2)
    }

    @Test
    fun validateServerUrlAllowsHttpsAnyHost() {
        val url = LocalClient.validateServerUrl("https://mywattracker.local:8443", isDebug = false)
        assertEquals("https://mywattracker.local:8443", url)
    }

    @Test
    fun authenticateMintsTicketAndRedeemsCookie() = runTest {
        val transport = FakeLocalTransport()
        val creds = LocalCredentials("http://10.0.2.2:8000", "secret-connector-token")
        val client = LocalClient(credentialsProvider = { creds }, transport = transport, isDebug = true)

        val cookie = client.authenticate()

        assertEquals("session=test-cookie-val", cookie)
        assertEquals(2, transport.requests.size)

        // Request 1: POST /api/connector/session with Bearer token
        val req1 = transport.requests[0]
        assertEquals("POST", req1.method)
        assertEquals("Bearer secret-connector-token", req1.headers["Authorization"])

        // Request 2: GET /connector/session?token=test-ticket-123
        val req2 = transport.requests[1]
        assertEquals("GET", req2.method)
        assertTrue(req2.url.contains("token=test-ticket-123"))
    }

    @Test
    fun warmStartReadCarriesCookieHeader() = runTest {
        val transport = FakeLocalTransport()
        val creds = LocalCredentials("http://10.0.2.2:8000", "token")
        val client = LocalClient(credentialsProvider = { creds }, transport = transport, isDebug = true)

        client.setSessionCookieForTesting("session=test-cookie-val")
        client.activityDetail(123)

        val lastReq = transport.requests.last()
        assertEquals("session=test-cookie-val", lastReq.headers["Cookie"])
    }

    @Test
    fun coldStartReadMintsTicketRedeemsCookieAndLoadsData() = runTest {
        val transport = FakeLocalTransport()
        val creds = LocalCredentials("http://10.0.2.2:8000", "secret-connector-token")
        val client = LocalClient(credentialsProvider = { creds }, transport = transport, isDebug = true)

        // Process starts with no cookie set. First data read automatically mints ticket, redeems cookie, and loads profile.
        val snapshot = client.load(CloudRoute.Profile)

        assertNotNull(snapshot)
        assertEquals(3, transport.requests.size)

        // Request 1: Mint
        assertEquals("POST", transport.requests[0].method)
        assertEquals("Bearer secret-connector-token", transport.requests[0].headers["Authorization"])

        // Request 2: Redeem
        assertEquals("GET", transport.requests[1].method)
        assertTrue(transport.requests[1].url.contains("token=test-ticket-123"))

        // Request 3: GET /api/state carrying minted Cookie
        assertEquals("GET", transport.requests[2].method)
        assertEquals("session=test-cookie-val", transport.requests[2].headers["Cookie"])
    }

    @Test
    fun coldStartWithMint401CallsOnRevokedAndClearsState() = runTest {
        var revokedCalled = false
        val transport = object : LocalTransport {
            override suspend fun send(request: LocalRequest): LocalResponse {
                // Cold start mint returns 401 Unauthorized
                return LocalResponse(status = 401, body = ByteArray(0), url = request.url)
            }
        }
        var creds: LocalCredentials? = LocalCredentials("http://10.0.2.2:8000", "revoked-token")
        val client = LocalClient(
            credentialsProvider = { creds },
            onRevoked = {
                revokedCalled = true
                creds = null
            },
            transport = transport,
            isDebug = true,
        )

        try {
            client.load(CloudRoute.Profile)
            fail("Expected Unauthorized exception on cold start with revoked token")
        } catch (_: LocalClientException.Unauthorized) {
            assertTrue(revokedCalled)
            assertNull(client.cached(CloudRoute.Profile))
            assertFalse(client.isPaired)
            assertEquals(CloudSession.DeviceState.removed, client.deviceState)
        }
    }

    @Test
    fun initialPairingWithBadTokenDoesNotCallOnRevoked() = runTest {
        var revokedCalled = false
        val transport = object : LocalTransport {
            override suspend fun send(request: LocalRequest): LocalResponse {
                return LocalResponse(status = 401, body = ByteArray(0), url = request.url)
            }
        }
        val client = LocalClient(
            credentialsProvider = { null },
            onRevoked = { revokedCalled = true },
            transport = transport,
            isDebug = true,
        )

        try {
            client.authenticate(LocalCredentials("http://10.0.2.2:8000", "bad-candidate-token"))
            fail("Expected Unauthorized on bad candidate token")
        } catch (_: LocalClientException.Unauthorized) {
            // Pairing attempt with bad candidate token must NOT call onRevoked
            assertFalse(revokedCalled)
        }
    }

    @Test
    fun mintOKThenRetriedRead303DoesNotCallOnRevoked() = runTest {
        var revokedCalled = false
        var mintCount = 0
        val transport = object : LocalTransport {
            override suspend fun send(request: LocalRequest): LocalResponse {
                if (request.url.contains("/api/connector/session")) {
                    mintCount++
                    return LocalResponse(status = 200, body = "{\"ticket\": \"test-ticket\"}".toByteArray(Charsets.UTF_8), url = request.url)
                }
                if (request.url.contains("/connector/session")) {
                    return LocalResponse(status = 303, body = ByteArray(0), url = request.url, headers = mapOf("Set-Cookie" to listOf("session=fresh-cookie"), "Location" to listOf("/")))
                }
                // All data reads return 303 redirect to /login
                return LocalResponse(status = 303, body = ByteArray(0), url = request.url, headers = mapOf("Location" to listOf("/login")))
            }
        }
        val creds = LocalCredentials("http://10.0.2.2:8000", "valid-token")
        val client = LocalClient(
            credentialsProvider = { creds },
            onRevoked = { revokedCalled = true },
            transport = transport,
            isDebug = true,
        )

        client.setSessionCookieForTesting("session=old-expired-cookie")

        try {
            client.load(CloudRoute.Profile)
            fail("Expected Unauthorized exception on redirect")
        } catch (_: LocalClientException.Unauthorized) {
            // Re-mint succeeded (mintCount == 1), but retried read redirected.
            // Valid token was NOT wiped because re-mint succeeded!
            assertEquals(1, mintCount)
            assertFalse(revokedCalled)
            assertTrue(client.isPaired)
        }
    }

    @Test
    fun authenticateHandles401Unauthorized() = runTest {
        val transport = FakeLocalTransport().apply {
            sessionTicketResponse = LocalResponse(status = 401, body = ByteArray(0), url = "http://10.0.2.2:8000/api/connector/session")
        }
        val creds = LocalCredentials("http://10.0.2.2:8000", "invalid-token")
        val client = LocalClient(credentialsProvider = { creds }, transport = transport, isDebug = true)

        try {
            client.authenticate()
            fail("Expected Unauthorized")
        } catch (_: LocalClientException.Unauthorized) {
            // expected
        }
    }

    @Test
    fun unexpectedLandingSanitizesTokenQuery() = runTest {
        val transport = FakeLocalTransport().apply {
            redeemResponse = LocalResponse(
                status = 303,
                body = ByteArray(0),
                url = "http://10.0.2.2:8000/connector/session?token=secret-ticket-value",
                headers = mapOf(
                    "Set-Cookie" to listOf("session=test"),
                    "Location" to listOf("/login?token=secret-ticket-value"),
                ),
            )
        }
        val creds = LocalCredentials("http://10.0.2.2:8000", "token")
        val client = LocalClient(credentialsProvider = { creds }, transport = transport, isDebug = true)

        try {
            client.authenticate()
            fail("Expected UnexpectedLanding")
        } catch (e: LocalClientException.UnexpectedLanding) {
            assertFalse(e.message?.contains("secret-ticket-value") == true)
        }
    }

    @Test
    fun loadDashboardFetchesStateAndBuildsSnapshot() = runTest {
        val transport = FakeLocalTransport()
        val creds = LocalCredentials("http://10.0.2.2:8000", "secret-connector-token")
        val client = LocalClient(credentialsProvider = { creds }, transport = transport, isDebug = true)

        val snapshot = client.load(CloudRoute.Dashboard)

        assertEquals(CloudRoute.Dashboard, snapshot.route)
        assertNotNull(snapshot)
        assertTrue(snapshot.items.isNotEmpty())
    }

    @Test
    fun loadDashboardPropagatesFailureWhenAllEndpointsFail() = runTest {
        val transport = object : LocalTransport {
            override suspend fun send(request: LocalRequest): LocalResponse {
                if (request.url.contains("/api/connector/session")) {
                    return LocalResponse(status = 200, body = "{\"ticket\": \"test-ticket\"}".toByteArray(Charsets.UTF_8), url = request.url)
                }
                if (request.url.contains("/connector/session")) {
                    return LocalResponse(status = 303, body = ByteArray(0), url = request.url, headers = mapOf("Set-Cookie" to listOf("session=cookie"), "Location" to listOf("/")))
                }
                return LocalResponse(status = 500, body = ByteArray(0), url = request.url)
            }
        }
        val creds = LocalCredentials("http://10.0.2.2:8000", "token")
        val client = LocalClient(credentialsProvider = { creds }, transport = transport, isDebug = true)

        try {
            client.load(CloudRoute.Dashboard)
            fail("Expected exception when all endpoints fail")
        } catch (_: LocalClientException.Http) {
            assertNull(client.lastSuccess)
        }
    }

    @Test
    fun sessionRedirectToLoginWithSuccessfulReAuthLoadsData() = runTest {
        var mintCount = 0
        val lastCookieCarried = mutableListOf<String?>()
        val transport = object : LocalTransport {
            override suspend fun send(request: LocalRequest): LocalResponse {
                if (request.url.contains("/api/connector/session")) {
                    mintCount++
                    return LocalResponse(status = 200, body = "{\"ticket\": \"fresh-ticket-$mintCount\"}".toByteArray(Charsets.UTF_8), url = request.url)
                }
                if (request.url.contains("/connector/session")) {
                    return LocalResponse(status = 303, body = ByteArray(0), url = request.url, headers = mapOf("Set-Cookie" to listOf("session=fresh-cookie-$mintCount"), "Location" to listOf("/")))
                }
                val cookie = request.headers["Cookie"]
                lastCookieCarried.add(cookie)
                if (cookie == "session=old-expired-cookie") {
                    // Session expired redirect to /login
                    return LocalResponse(status = 303, body = ByteArray(0), url = request.url, headers = mapOf("Location" to listOf("/login")))
                }
                // Fresh cookie returns state JSON
                return LocalResponse(status = 200, body = "{\"ctl\": 60.0, \"atl\": 70.0, \"tsb\": -10.0, \"ftp\": 260}".toByteArray(Charsets.UTF_8), url = request.url)
            }
        }

        val creds = LocalCredentials("http://10.0.2.2:8000", "valid-token")
        val client = LocalClient(credentialsProvider = { creds }, transport = transport, isDebug = true)

        // Establish initial session
        val initialCookie = client.authenticate()
        assertEquals("session=fresh-cookie-1", initialCookie)

        // Manually simulate an expired session cookie
        client.setSessionCookieForTesting("session=old-expired-cookie")

        // Load profile -> triggers 303 redirect, re-authenticates with fresh-cookie-2, and returns data
        val snapshot = client.load(CloudRoute.Profile)
        assertNotNull(snapshot)
        assertEquals(1, snapshot.items.size)
        assertEquals(2, mintCount)
        assertEquals("session=fresh-cookie-2", lastCookieCarried.last())
    }

    @Test
    fun sessionRedirectToLoginWithFailedReAuthTriggersOnRevokedAndResetsState() = runTest {
        var revokedCalled = false
        val transport = object : LocalTransport {
            override suspend fun send(request: LocalRequest): LocalResponse {
                if (request.url.contains("/api/connector/session")) {
                    // Token revoked on re-mint attempt
                    return LocalResponse(status = 401, body = ByteArray(0), url = request.url)
                }
                if (request.headers["Cookie"] == "session=old-expired-cookie") {
                    return LocalResponse(status = 303, body = ByteArray(0), url = request.url, headers = mapOf("Location" to listOf("/login")))
                }
                return LocalResponse(status = 200, body = "{}".toByteArray(Charsets.UTF_8), url = request.url)
            }
        }
        var creds: LocalCredentials? = LocalCredentials("http://10.0.2.2:8000", "revoked-token")
        val client = LocalClient(
            credentialsProvider = { creds },
            onRevoked = {
                revokedCalled = true
                creds = null
            },
            transport = transport,
            isDebug = true,
        )

        // Establish initial expired session with cached data
        client.setSessionCookieForTesting("session=old-expired-cookie")

        try {
            client.activityDetail(123)
            fail("Expected Unauthorized exception when session redirects to /login and token is revoked")
        } catch (_: LocalClientException.Unauthorized) {
            assertTrue(revokedCalled)
            assertNull(client.cached(CloudRoute.Dashboard))
            assertFalse(client.isPaired)
            assertEquals(CloudSession.DeviceState.removed, client.deviceState)
        }
    }

    @Test
    fun httpLocalTransportDoesNotFollowRedirects() = runTest {
        val serverSocket = ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"))
        val port = serverSocket.localPort
        val thread = Thread {
            try {
                val socket = serverSocket.accept()
                val reader = BufferedReader(InputStreamReader(socket.getInputStream()))
                while (reader.readLine()?.isEmpty() == false) {}
                val out = socket.getOutputStream()
                out.write("HTTP/1.1 307 Temporary Redirect\r\nLocation: http://127.0.0.1:9999/other\r\nContent-Length: 0\r\n\r\n".toByteArray(Charsets.UTF_8))
                out.flush()
                socket.close()
            } catch (_: Exception) {}
        }
        thread.start()

        try {
            val transport = HttpLocalTransport()
            val response = transport.send(LocalRequest("GET", "http://127.0.0.1:$port/redirect"))
            assertEquals(307, response.status)
            assertEquals("http://127.0.0.1:9999/other", response.location)
        } finally {
            serverSocket.close()
            thread.join()
        }
    }

    @Test
    fun authenticateRejectsRedirectToExternalOrigin() = runTest {
        val transport = FakeLocalTransport().apply {
            redeemResponse = LocalResponse(
                status = 303,
                body = ByteArray(0),
                url = "http://10.0.2.2:8000/connector/session?token=test-ticket-123",
                headers = mapOf(
                    "Set-Cookie" to listOf("session=test-cookie"),
                    "Location" to listOf("https://evil.com/"),
                ),
            )
        }
        val creds = LocalCredentials("http://10.0.2.2:8000", "token")
        val client = LocalClient(credentialsProvider = { creds }, transport = transport, isDebug = true)

        try {
            client.authenticate()
            fail("Expected UnexpectedLanding for external origin")
        } catch (_: LocalClientException.UnexpectedLanding) {
            // expected
        }
    }

    @Test
    fun revokedLocalClientSurfacesDeviceStateRemoved() = runTest {
        val creds = LocalCredentials("http://10.0.2.2:8000", "token")
        val client = LocalClient(credentialsProvider = { creds }, isDebug = true)

        assertEquals(CloudSession.DeviceState.paired, client.deviceState)
        client.markRemoved()
        assertEquals(CloudSession.DeviceState.removed, client.deviceState)
    }

    @Test
    fun rePairingDuringInFlightReadDoesNotSendNewServerCookieToOldServer() = runTest {
        var revokedCalled = false
        var currentCreds: LocalCredentials? = LocalCredentials("http://10.0.2.2:8000", "server-a-token")
        val requests = mutableListOf<LocalRequest>()

        val transport = object : LocalTransport {
            override suspend fun send(request: LocalRequest): LocalResponse {
                requests.add(request)
                if (request.url.contains("10.0.2.2:8000")) {
                    // Simulate re-pairing happening while read on Server A is in flight
                    currentCreds = LocalCredentials("http://10.0.2.2:9000", "server-b-token")
                    return LocalResponse(status = 401, body = ByteArray(0), url = request.url)
                }
                if (request.url.contains("10.0.2.2:9000/api/connector/session")) {
                    return LocalResponse(status = 200, body = "{\"ticket\": \"b-ticket\"}".toByteArray(Charsets.UTF_8), url = request.url)
                }
                if (request.url.contains("10.0.2.2:9000/connector/session")) {
                    return LocalResponse(status = 303, body = ByteArray(0), url = request.url, headers = mapOf("Set-Cookie" to listOf("session=server-b-cookie"), "Location" to listOf("/")))
                }
                return LocalResponse(status = 200, body = "{}".toByteArray(Charsets.UTF_8), url = request.url)
            }
        }

        val client = LocalClient(
            credentialsProvider = { currentCreds },
            onRevoked = { revokedCalled = true },
            transport = transport,
            isDebug = true,
        )

        // Authenticate against Server A
        client.setSessionCookieForTesting("session=server-a-cookie", "http://10.0.2.2:8000")

        // In-flight read on Server A is triggered
        try {
            client.load(CloudRoute.Profile)
            fail("Expected Unauthorized for Server A read")
        } catch (_: LocalClientException.Unauthorized) {
            // Must NOT wipe Server B's credentials!
            assertFalse(revokedCalled)
            assertEquals(CloudSession.DeviceState.paired, client.deviceState)
            assertEquals("server-b-token", client.credentials?.token)
        }

        // None of the requests sent to Server A contained Server B's cookie
        val serverARequests = requests.filter { it.url.contains("10.0.2.2:8000") }
        assertTrue(serverARequests.none { it.headers["Cookie"] == "session=server-b-cookie" })
    }
}
