package dev.tickdesk.app

import androidx.test.platform.app.InstrumentationRegistry
import dev.tickdesk.app.data.RelayClient
import dev.tickdesk.app.data.RoomWorkbenchStore
import dev.tickdesk.core.*
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.*
import okhttp3.CertificatePinner
import okhttp3.OkHttpClient
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.tls.HandshakeCertificates
import okhttp3.tls.HeldCertificate
import org.junit.Assert.*
import org.junit.Test
import java.net.InetAddress
import java.security.SecureRandom
import java.util.Base64
import java.util.UUID
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

class RelayClientTest {
    private val context = InstrumentationRegistry.getInstrumentation().targetContext
    private val leaf = HeldCertificate.Builder().commonName("local test").addSubjectAlternativeName("127.0.0.1").build()
    private val serverCerts = HandshakeCertificates.Builder().heldCertificate(leaf).build()
    private val clientCerts = HandshakeCertificates.Builder().addTrustedCertificate(leaf.certificate).build()
    private val client = OkHttpClient.Builder().sslSocketFactory(clientCerts.sslSocketFactory(), clientCerts.trustManager).build()
    private val token = Base64.getUrlEncoder().withoutPadding().encodeToString(ByteArray(32).apply { SecureRandom().nextBytes(this) })

    @Test fun sendsHelloAndBootstrapSyncOverPinnedTls() = runBlocking {
        val sync = AtomicReference<BusinessMessage>()
        val testName = "tls-test-${UUID.randomUUID()}.db"
        try {
            MockWebServer().use { server ->
                server.useHttps(serverCerts.sslSocketFactory(), false)
                server.enqueue(MockResponse().setHeader("Sec-WebSocket-Protocol", "tickdesk.v2")
                    .withWebSocketUpgrade(object : WebSocketListener() {
                        override fun onMessage(webSocket: WebSocket, text: String) {
                            val frame = Protocol.frame(text)
                            if (frame.type == FrameType.HELLO) {
                                webSocket.send("""{"v":2,"type":"welcome","pairId":"test_pair","relayBootId":"test_boot","connectionId":"test_session","role":"app","heartbeatIntervalMs":15000,"heartbeatTimeoutMs":45000}""")
                                webSocket.send("""{"v":2,"type":"presence","peer":{"online":true,"incarnation":"local","serverEpoch":"epoch"}}""")
                            } else {
                                sync.set(frame.message)
                                webSocket.close(4401, "Test closes after bootstrap")
                            }
                        }
                    }))
                server.start(InetAddress.getByName("127.0.0.1"), 8443)
                RoomWorkbenchStore.open(context, testName).use { store ->
                    val repo = WorkbenchRepository(store)
                    repo.selectPair("test_pair")
                    val end = withTimeout(12_000) { RelayClient(repo, client).run(config(CertificatePinner.pin(leaf.certificate))) }
                    assertEquals("CREDENTIAL_REVOKED", end.reason)
                    assertFalse(end.retry)
                    assertEquals("sync", sync.get()?.command)
                    assertNull(sync.get()?.epoch)
                    val request = server.takeRequest(1, TimeUnit.SECONDS)!!
                    assertEquals("/relay/v2/connect", request.path)
                    assertTrue(request.getHeader("Authorization") == "Bearer $token")
                    assertEquals("tickdesk.v2", request.getHeader("Sec-WebSocket-Protocol"))
                }
            }
        } finally {
            context.deleteDatabase(testName)
        }
        Unit
    }

    @Test fun mismatchedPinStopsBeforeBearerIsSent() = runBlocking {
        val testName = "bad-pin-${UUID.randomUUID()}.db"
        try {
            MockWebServer().use { server ->
                server.useHttps(serverCerts.sslSocketFactory(), false)
                server.start(InetAddress.getByName("127.0.0.1"), 8443)
                RoomWorkbenchStore.open(context, testName).use { store ->
                    val repo = WorkbenchRepository(store)
                    repo.selectPair("test_pair")
                    val other = HeldCertificate.Builder().build()
                    val end = withTimeout(12_000) { RelayClient(repo, client).run(config(CertificatePinner.pin(other.certificate))) }
                    assertEquals("TLS_VALIDATION", end.reason)
                    assertFalse(end.retry)
                    assertEquals(0, server.requestCount)
                }
            }
        } finally {
            context.deleteDatabase(testName)
        }
        Unit
    }

    private fun config(pin: String) = Protocol.pairing(buildJsonObject {
        put("protocol", 2); put("relayUrl", "wss://127.0.0.1:8443/relay/v2/connect")
        put("pairId", "test_pair"); put("appToken", token); put("pins", JsonArray(listOf(JsonPrimitive(pin))))
    }.toString())
}
