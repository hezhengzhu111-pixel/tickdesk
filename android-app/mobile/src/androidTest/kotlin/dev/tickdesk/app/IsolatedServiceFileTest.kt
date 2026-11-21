package dev.tickdesk.app

import android.graphics.Bitmap
import androidx.test.platform.app.InstrumentationRegistry
import dev.tickdesk.app.data.RelayClient
import dev.tickdesk.app.data.RoomWorkbenchStore
import dev.tickdesk.core.*
import kotlinx.coroutines.async
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.selects.select
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.*
import okhttp3.CertificatePinner
import okhttp3.OkHttpClient
import okhttp3.tls.HandshakeCertificates
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.ByteArrayOutputStream
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate
import java.util.Base64
import java.util.UUID
import kotlin.random.Random

class IsolatedServiceFileTest {
    @Test fun realServiceRoundTripVerifiesOriginalThenDeletesByVersion() = runBlocking {
        val args = InstrumentationRegistry.getArguments()
        assumeTrue("Opt-in isolated relay and local service", args.getString("isolatedFileTest") == "true")
        val cert = Base64.getDecoder().decode(checkNotNull(args.getString("caDer"))).inputStream().use {
            CertificateFactory.getInstance("X.509").generateCertificate(it) as X509Certificate
        }
        val tls = HandshakeCertificates.Builder().addTrustedCertificate(cert).build()
        val client = OkHttpClient.Builder().sslSocketFactory(tls.sslSocketFactory(), tls.trustManager).build()
        val config = Protocol.pairing(buildJsonObject {
            put("protocol", 2)
            put("pairId", checkNotNull(args.getString("pairId")))
            put("appToken", checkNotNull(args.getString("appBearer")))
            put("relayUrl", "wss://127.0.0.1:8443/relay/v2/connect")
            put("pins", JsonArray(listOf(JsonPrimitive(CertificatePinner.pin(cert)))))
        }.toString())
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val name = "isolated-file-${UUID.randomUUID()}.db"
        try {
            RoomWorkbenchStore.open(context, name).use { store ->
                val repository = WorkbenchRepository(store)
                val relay = RelayClient(repository, client)
                repository.selectPair(config.pairId)
                val socket = async { relay.run(config) }
                try {
                    suspend fun await(predicate: (WorkbenchProjection) -> Boolean): WorkbenchProjection {
                        val state = async { repository.state.first(predicate) }
                        return try {
                            withTimeout(20_000) {
                                select {
                                    state.onAwait { it }
                                    socket.onAwait { end -> error("Isolated relay ended: ${end.reason}") }
                                }
                            }
                        } finally {
                            state.cancelAndJoin()
                        }
                    }
                    await { it.connection.canWrite }
                    val png = samplePng()
                    assertTrue(png.size > Protocol.CHUNK_SIZE)
                    val upload = repository.startUpload("isolated-roundtrip.png", "image/png", png)
                    val saved = await { state -> state.transfers.any { it.id == upload && !it.needsDelivery } }
                        .transfers.single { it.id == upload }
                    assertEquals(saved.errorCode, FileTransferStatus.COMPLETE, saved.status)
                    assertEquals(CanonicalJson.sha256(png), saved.sha256)
                    val fileId = checkNotNull(saved.fileId)
                    val download = repository.startDownload(fileId)
                    val loaded = await { state -> state.transfers.any { it.id == download && !it.needsDelivery } }
                        .transfers.single { it.id == download }
                    assertEquals(loaded.errorCode, FileTransferStatus.COMPLETE, loaded.status)
                    assertArrayEquals(png, repository.readDownloaded(download))
                    await { !it.hasPendingReceipts }
                    relay.flushWrites()
                    val delete = repository.enqueue("delete_file", buildJsonObject {
                        put("fileId", fileId); put("baseVersion", checkNotNull(saved.fileVersion)); put("confirm", true)
                    })
                    val tombstone = await { state ->
                        state.intents.any { it.id == delete && (it.result != null || it.stage == DeliveryStage.REJECTED) }
                    }.intents.single { it.id == delete }
                    assertEquals(tombstone.errorCode, "deleted", tombstone.result?.obj("file")?.text("status"))
                } finally {
                    socket.cancelAndJoin()
                }
            }
        } finally {
            context.deleteDatabase(name)
        }
        Unit
    }

    private fun samplePng(): ByteArray {
        val bitmap = Bitmap.createBitmap(128, 128, Bitmap.Config.ARGB_8888)
        val random = Random(4817)
        try {
            val pixels = IntArray(128 * 128) { random.nextInt(0x1000000) or 0xff000000.toInt() }
            bitmap.setPixels(pixels, 0, 128, 0, 0, 128, 128)
            return ByteArrayOutputStream().use { out ->
                check(bitmap.compress(Bitmap.CompressFormat.PNG, 100, out))
                out.toByteArray()
            }
        } finally {
            bitmap.recycle()
        }
    }
}
