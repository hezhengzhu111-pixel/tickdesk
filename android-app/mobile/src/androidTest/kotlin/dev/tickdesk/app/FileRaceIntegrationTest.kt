package dev.tickdesk.app

import android.graphics.Bitmap
import android.os.Build
import android.view.accessibility.AccessibilityNodeInfo
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.Column
import androidx.compose.ui.Modifier
import androidx.test.platform.app.InstrumentationRegistry
import dev.tickdesk.app.data.RelayClient
import dev.tickdesk.app.data.RoomWorkbenchStore
import dev.tickdesk.app.ui.HoldingImportPanel
import dev.tickdesk.app.ui.TickDeskTheme
import dev.tickdesk.core.*
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.first
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

/** Isolated Android Room + WSS check. The host restarts both processes between phases. */
class FileRaceIntegrationTest {
    @Test fun cancellationAndDeletionKeepConfirmedHoldingAndReportAfterRestart(): Unit = runBlocking(Dispatchers.IO) {
        val args = InstrumentationRegistry.getArguments()
        val phase = args.getString("fileRacePhase")
        assumeTrue(BuildConfig.MOCK_DEMO && phase in setOf("before", "after"))
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val certificate = Base64.getDecoder().decode(requireNotNull(args.getString("caDer"))).inputStream().use {
            CertificateFactory.getInstance("X.509").generateCertificate(it) as X509Certificate
        }
        val tls = HandshakeCertificates.Builder().addTrustedCertificate(certificate).build()
        val client = OkHttpClient.Builder().sslSocketFactory(tls.sslSocketFactory(), tls.trustManager).build()
        val config = Protocol.pairing(buildJsonObject {
            put("protocol", 2); put("pairId", args.getString("pairId")); put("appToken", args.getString("appBearer"))
            put("relayUrl", "wss://127.0.0.1:8443/relay/v2/connect")
            putJsonArray("pins") { add(CertificatePinner.pin(certificate)) }
        }.toString())
        val database = "file-race-${config.pairId}.db"
        try {
            RoomWorkbenchStore.open(context, database).use { store ->
                val repo = WorkbenchRepository(store)
                repo.selectPair(config.pairId)
                val deleteAck = CompletableDeferred<BusinessMessage>()
                val relay = RelayClient(repo, client) {
                    if (it.kind == "ack" && it.command == "delete_file" &&
                        it.data.obj("result")?.obj("file")?.text("status") == "deleted") deleteAck.complete(it)
                }
                val socket = async { relay.run(config) }
                suspend fun await(label: String, predicate: (WorkbenchProjection) -> Boolean): WorkbenchProjection =
                    withTimeoutOrNull(25_000) { repo.state.first(predicate) } ?: error(
                        "$label timed out. Connection=${repo.state.value.connection}, workflow=${repo.state.value.holdingImport?.status}",
                    )
                suspend fun receipt(id: String, name: String) = await(name) { snap -> snap.intents.any {
                        it.id == id && it.stage in setOf(DeliveryStage.APPLIED, DeliveryStage.REJECTED)
                    } }.intents.single { it.id == id }
                suspend fun command(name: String, input: JsonObject = buildJsonObject {}, error: String? = null): IntentView {
                    val result = receipt(repo.enqueue(name, input), name)
                    assertEquals(result.errorCode, if (error == null) DeliveryStage.APPLIED else DeliveryStage.REJECTED, result.stage)
                    assertEquals(error, result.errorCode)
                    return result
                }
                suspend fun original(fileId: String, expectedPng: ByteArray) {
                    val transfer = repo.startDownload(fileId)
                    val downloaded = await("download original") { snap ->
                        snap.transfers.any { it.id == transfer && !it.needsDelivery }
                    }.transfers.single { it.id == transfer }
                    assertEquals(downloaded.errorCode, FileTransferStatus.COMPLETE, downloaded.status)
                    assertArrayEquals(expectedPng, repo.readDownloaded(transfer))
                    await("acknowledge original chunks") { !it.hasPendingReceipts }
                    relay.flushWrites()
                }
                suspend fun listed() = command("list_files").result!!.array("items")!!.map { it.jsonObject }
                    .associateBy { it.text("name") }

                try {
                    await("paired snapshot") { it.connection.canWrite }
                    val baselinePng = png(0xff00ff00.toInt())
                    val baselineName = "race-baseline.png"
                    val racingName = "race-inflight.png"
                    if (phase == "before") {
                        val notice = repo.state.value.meta!!.obj("visionNotice")!!.text("noticeVersion")!!
                        val baseline = repo.startHoldingImport(baselineName, "image/png", baselinePng, notice)
                        val first = await("confirmed initial holding") { it.holdingImport?.id == baseline &&
                            it.holdingImport?.status == HoldingImportStatus.SUCCESS }
                        val initial = first.records.getValue("positions").single()
                        assertEquals("300502.SZ", initial.text("symbol"))
                        assertEquals(100L, initial.number("qty"))
                        val positive = listed().getValue(baselineName)
                        assertEquals("available", positive.text("status"))
                        assertEquals(CanonicalJson.sha256(baselinePng), positive.text("sha256"))
                        original(positive.text("fileId")!!, baselinePng)

                        val report = command("chat", buildJsonObject { put("text", "/分析 300502.SZ") })
                        val saved = command("get_report", buildJsonObject { put("reportId", report.id) }).result!!
                        assertTrue("Report must cite the actual stored holding", saved.array("sources")!!.isNotEmpty())

                        val race = repo.startHoldingImport(racingName, "image/png", png(0xff804000.toInt()), notice)
                        await("vision has a pending lease") { state -> state.records["imports"]?.any {
                            it.text("importId") == race && it.text("status") == "pending"
                        } == true }
                        val racy = listed().getValue(racingName)
                        val deletion = buildJsonObject {
                            put("fileId", racy.text("fileId")!!); put("baseVersion", racy.number("version")!!)
                            put("confirm", true)
                        }
                        command("delete_file", deletion, "FILE_BUSY")
                        assertEquals("available", listed().getValue(racingName).text("status"))
                        val cancelId = repo.enqueue("cancel_import", buildJsonObject { put("importId", race) })
                        val deleteId = repo.enqueue("delete_file", deletion)
                        val cancelled = receipt(cancelId, "cancel racing vision")
                        assertEquals("cancelled", cancelled.result!!.text("status"))
                        val competing = receipt(deleteId, "concurrent deletion")
                        val removed = if (competing.errorCode == null) competing.result!!.obj("file")!!
                            else {
                                assertEquals("FILE_BUSY", competing.errorCode)
                                command("delete_file", deletion).result!!.obj("file")!!
                            }
                        assertEquals("deleted", removed.text("status"))
                        assertEquals(racy.text("fileId"), removed.text("fileId"))
                        val ack = withTimeout(15_000) { deleteAck.await() }
                        val current = await("updated linked import after deleting original") { state -> state.records["imports"]?.any {
                            it.text("importId") == race && it.text("status") == "cancelled" && it.text("fileStatus") == "deleted"
                        } == true }
                        assertTrue(current.sequence!! >= ack.sequence!!)
                        assertFalse("Confirmed deletion must remove the retry action", current.holdingImport!!.canRetry)
                        assertEquals(100L, repo.state.value.records.getValue("positions").single().number("qty"))
                    } else {
                        val files = listed()
                        val baseline = files.getValue(baselineName)
                        val deleted = files.getValue(racingName)
                        assertEquals("available", baseline.text("status"))
                        assertEquals("deleted", deleted.text("status"))
                        original(baseline.text("fileId")!!, baselinePng)
                        val blocked = repo.startDownload(deleted.text("fileId")!!)
                        val failedRead = await("tombstone keeps bytes unavailable") { state ->
                            state.transfers.any { it.id == blocked && !it.needsDelivery }
                        }.transfers.single { it.id == blocked }
                        assertEquals("FILE_DELETED", failedRead.errorCode)
                        assertNull(repo.readDownloaded(blocked))
                        val current = repo.state.value
                        val holding = current.records.getValue("positions").single()
                        assertEquals(100L, holding.number("qty"))
                        assertEquals(80L, holding.number("availableQty"))
                        assertTrue(current.records.getValue("executions").isEmpty())
                        val imports = current.records.getValue("imports").associateBy { it.text("fileId") }
                        assertEquals("confirmed", imports.getValue(baseline.text("fileId")).text("status"))
                        assertEquals("cancelled", imports.getValue(deleted.text("fileId")).text("status"))
                        assertEquals("deleted", imports.getValue(deleted.text("fileId")).text("fileStatus"))
                        val reports = command("list_reports").result!!.array("items")!!
                        val saved = command("get_report", buildJsonObject {
                            put("reportId", reports.single().jsonObject.text("reportId")!!)
                        }).result!!
                        assertTrue(saved.array("sources")!!.isNotEmpty())
                        assertTrue(saved.obj("header")!!.array("symbols")!!.contains(JsonPrimitive("300502.SZ")))
                        assertFalse("Deleted original cannot be used to retry", current.holdingImport!!.canRetry)
                        try {
                            repo.retryHoldingImport(current.meta!!.obj("visionNotice")!!.text("noticeVersion")!!)
                            fail("The retry command must reject an unavailable original")
                        } catch (_: ProtocolException) {
                            // The prior workflow is already terminal and its file was deleted.
                        }
                        assertDeletedOriginalShown(current)
                    }
                } finally {
                    socket.cancelAndJoin()
                }
            }
        } finally {
            if (phase == "after") context.deleteDatabase(database)
            client.connectionPool.evictAll()
        }
    }

    private suspend fun assertDeletedOriginalShown(current: WorkbenchProjection) {
        val activity = launchResumedMainActivity()
        val automation = InstrumentationRegistry.getInstrumentation().uiAutomation
        fun text(node: AccessibilityNodeInfo): Sequence<String> = sequence {
            if (node.isVisibleToUser) node.text?.toString()?.let { yield(it) }
            for (index in 0 until node.childCount) node.getChild(index)?.let { yieldAll(text(it)) }
        }
        try {
            onTestMainThread {
                activity.setContent { TickDeskTheme {
                    Column(Modifier.fillMaxSize().safeDrawingPadding()) {
                        HoldingImportPanel(current, busy = false, imageError = null)
                    }
                } }
            }
            val shown = withTimeout(6_000) {
                var screen = emptyList<String>()
                do {
                    delay(100)
                    if (Build.VERSION.SDK_INT >= 34) automation.clearCache()
                    screen = automation.rootInActiveWindow?.let { text(it).toList() }.orEmpty()
                } while (screen.none { "本次原图已删除" in it })
                screen
            }
            assertTrue(shown.any { "工作台已取消本次识别" in it })
            assertTrue(shown.none { "重新识别此图" in it })
        } finally {
            onTestMainThread { activity.finish() }
        }
    }

    private fun png(color: Int): ByteArray {
        val bitmap = Bitmap.createBitmap(64, 64, Bitmap.Config.ARGB_8888)
        return try {
            bitmap.eraseColor(color)
            ByteArrayOutputStream().use { output ->
                check(bitmap.compress(Bitmap.CompressFormat.PNG, 100, output))
                output.toByteArray()
            }
        } finally { bitmap.recycle() }
    }
}
