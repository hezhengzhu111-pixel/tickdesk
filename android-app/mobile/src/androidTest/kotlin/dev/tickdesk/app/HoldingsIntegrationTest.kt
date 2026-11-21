package dev.tickdesk.app

import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import androidx.test.platform.app.InstrumentationRegistry
import dev.tickdesk.app.data.RelayClient
import dev.tickdesk.app.data.RoomWorkbenchStore
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
import java.math.BigDecimal
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate
import java.util.Base64
import java.util.UUID

/** Real device transport with separate Room and server databases; never replaces the saved pairing. */
class HoldingsIntegrationTest {
    @Test fun simpleHoldingImportSurvivesReconnectAndCommitsOnlyValidRows() = runBlocking(Dispatchers.IO) {
        val args = InstrumentationRegistry.getArguments()
        assumeTrue(args.getString("holdingsIntegration") == "true")
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
        val database = "holdings-isolated-${UUID.randomUUID()}.db"
        val activity = launchResumedMainActivity()
        try {
            RoomWorkbenchStore.open(context, database).use { store ->
                var repo = WorkbenchRepository(store)
                repo.selectPair(config.pairId)
                var socket = async { RelayClient(repo, client) { }.run(config) }
                suspend fun waitFor(label: String, predicate: (WorkbenchProjection) -> Boolean): WorkbenchProjection =
                    try { withTimeout(30_000) { repo.state.first(predicate) } }
                    catch (_: TimeoutCancellationException) {
                        error("$label timed out: connection=${repo.state.value.connection}, import=${repo.state.value.holdingImport}")
                    }
                suspend fun command(name: String, data: JsonObject = buildJsonObject {}): IntentView {
                    val id = repo.enqueue(name, data)
                    return waitFor(name) { s -> s.intents.any { it.id == id && it.stage in setOf(DeliveryStage.APPLIED, DeliveryStage.REJECTED) } }
                        .intents.single { it.id == id }.also { assertEquals(it.errorCode, DeliveryStage.APPLIED, it.stage) }
                }
                suspend fun adjust(symbol: String, name: String, qty: Int, cost: String) = command("adjust_position", buildJsonObject {
                    put("symbol", symbol); put("name", name); put("qty", qty); put("availableQty", qty); put("cost", cost)
                    put("basePositionRevision", repo.state.value.meta!!.number("positionRevision")!!)
                    put("asOf", System.currentTimeMillis()); put("reason", "isolated holdings test")
                    putJsonArray("resolvedExecutionIds") {}; put("pendingLots", JsonNull)
                    if (qty == 0) put("confirmZero", true)
                })
                suspend fun completed(id: String, expected: HoldingImportStatus): WorkbenchProjection {
                    val state = waitFor("terminal screenshot import") { it.holdingImport?.id == id && it.holdingImport?.terminal == true }
                    assertEquals(state.holdingImport.toString(), expected, state.holdingImport!!.status)
                    return state
                }
                try {
                    waitFor("initial sync") { it.connection.canWrite }
                    for (query in listOf("浦发", "6000", "新易盛", "300502")) {
                        val result = command("search_symbols", buildJsonObject { put("query", query) })
                        assertTrue("No suggestions for $query", result.result!!.array("items")!!.isNotEmpty())
                    }
                    val realVision = args.getString("realVision") == "true"
                    adjust("600000.SH", "浦发银行", 200, "9.50")
                    if (!realVision) {
                        adjust("601318.SH", "中国平安", 35, "12.50")
                        assertEquals(35L, repo.state.value.records.getValue("positions").single { it.text("symbol") == "601318.SH" }.number("qty"))
                        adjust("600000.SH", "浦发银行", 225, "9.50")
                        adjust("601318.SH", "中国平安", 0, "12.50")
                        assertEquals(0L, repo.state.value.records.getValue("positions").single { it.text("symbol") == "601318.SH" }.number("qty"))
                    }
                    waitFor("manual holding sync") { it.records["positions"]?.any { p -> p.text("symbol") == "600000.SH" && p.text("name") == "浦发银行" } == true }
                    val original = repo.state.value.records.getValue("positions").single { it.text("symbol") == "600000.SH" }
                    val notice = repo.state.value.meta!!.obj("visionNotice")!!.text("noticeVersion")!!
                    val id = repo.startHoldingImport("valid.png", "image/png", if (realVision) holdingImage() else png(0xff00ff00.toInt()), notice)
                    // Recreate the repository during the upload using the same isolated durable store.
                    socket.cancelAndJoin()
                    repo = WorkbenchRepository(store)
                    repo.selectPair(config.pairId)
                    socket = async { RelayClient(repo, client) { }.run(config) }
                    val completed = completed(id, HoldingImportStatus.SUCCESS)
                    assertFalse("A successful import must not be presented as retryable", completed.holdingImport!!.canRetry)
                    val positions = completed.records.getValue("positions")
                    assertEquals(original, positions.single { it.text("symbol") == "600000.SH" })
                    val imported = positions.single { it.text("symbol") == "300502.SZ" }
                    assertEquals("新易盛", imported.text("name")); assertEquals(100L, imported.number("qty"))
                    assertEquals(80L, imported.number("availableQty")); assertEquals(0, BigDecimal("10").compareTo(BigDecimal(imported.text("cost")!!)))
                    val stableRevision = completed.meta!!.number("positionRevision")
                    val failures = if (realVision) emptyList() else listOf(
                        0xffff0000.toInt() to HoldingImportStatus.INVALID, // Invalid quantity
                        0xffffffff.toInt() to HoldingImportStatus.INVALID, // Non-holding screenshot
                        0xff0000ff.toInt() to HoldingImportStatus.FAILED, // Upstream error
                        0xff00ffff.toInt() to HoldingImportStatus.INVALID, // Name and code do not match
                        0xffff00ff.toInt() to HoldingImportStatus.INVALID, // Duplicate holdings
                        0xff204060.toInt() to HoldingImportStatus.INVALID, // Low-confidence cost
                    )
                    for ((color, expected) in failures) {
                        val next = repo.startHoldingImport("failure.png", "image/png", png(color), notice)
                        val terminal = completed(next, expected)
                        assertEquals(stableRevision, terminal.meta!!.number("positionRevision"))
                        assertEquals(positions, terminal.records.getValue("positions"))
                    }
                    if (!realVision) {
                        val cancel = repo.startHoldingImport("cancel.png", "image/png", png(0xffffa500.toInt()), notice)
                        waitFor("authoritative pending batch") { projection ->
                            projection.records["imports"]?.any { it.text("importId") == cancel && it.text("status") == "pending" } == true
                        }
                        assertEquals("cancelled", command("cancel_import", buildJsonObject { put("importId", cancel) }).result!!.text("status"))
                        assertEquals("IMPORT_CANCELLED", completed(cancel, HoldingImportStatus.FAILED).holdingImport!!.errorCode)
                        assertEquals(positions, repo.state.value.records.getValue("positions"))

                        val cancelled = repo.startHoldingImport("revoked.png", "image/png", png(0xffffff00.toInt()), notice)
                        waitFor("recognition before revoke") { it.holdingImport?.id == cancelled && it.holdingImport?.status == HoldingImportStatus.RECOGNIZING }
                        command("accept_vision_notice", buildJsonObject { put("noticeVersion", notice); put("accepted", false) })
                        val stopped = waitFor("revocation terminates auto import") {
                            it.holdingImport?.id == cancelled && it.holdingImport?.status == HoldingImportStatus.FAILED
                        }
                        assertEquals(stableRevision, stopped.meta!!.number("positionRevision"))
                        delay(2500) // Let the deliberately late fixture response reach the durable commit gate.
                        assertEquals(positions, repo.state.value.records.getValue("positions"))

                        val bad = repo.startHoldingImport("two-rows.png", "image/png", png(0xff8000ff.toInt()), notice)
                        val badResult = completed(bad, HoldingImportStatus.INVALID)
                        val failedWorkflow = badResult.holdingImport!!
                        assertTrue("Confirmed invalid result with a saved original must be retryable", failedWorkflow.canRetry)
                        assertEquals(stableRevision, badResult.meta!!.number("positionRevision"))
                        assertEquals(positions, badResult.records.getValue("positions"))
                        val retry = repo.retryHoldingImport(notice)
                        val retried = completed(retry, HoldingImportStatus.SUCCESS)
                        val successfulWorkflow = retried.holdingImport!!
                        assertEquals(failedWorkflow.transferId, successfulWorkflow.transferId)
                        assertFalse(successfulWorkflow.canRetry)
                        val changed = retried.records.getValue("positions").associateBy { it.text("symbol") }
                        assertEquals(original, changed["600000.SH"])
                        assertEquals(120L, changed["300502.SZ"]?.number("qty"))
                        assertEquals(50L, changed["000001.SZ"]?.number("qty"))
                        assertEquals(JsonNull, changed["000001.SZ"]?.get("availableQty"))
                        val importBatch = retried.records.getValue("imports").single { it.text("importId") == retry }
                        assertEquals(2, importBatch.array("rows")!!.size)
                        assertEquals("1464", importBatch.array("recognizedRows")!!.first().jsonObject.obj("raw")?.text("marketValue"))
                        try {
                            repo.retryHoldingImport(notice)
                            fail("A confirmed screenshot must not be re-executed")
                        } catch (_: ProtocolException) {
                            // The previous business result was already applied.
                        }

                        command("pause")
                        assertEquals(true, repo.state.value.meta!!.obj("analysis")!!.flag("paused"))
                        command("set_profile", buildJsonObject { put("profile", "aggressive") })
                        assertEquals("aggressive", repo.state.value.meta!!.obj("analysis")!!.text("profile"))
                        val setting = repo.state.value.meta!!.obj("settings")!!
                        command("set_settings", buildJsonObject {
                            put("baseSettingsRevision", setting.number("settingsRevision")!!)
                            put("reminderModes", setting.obj("reminderModes")!!)
                            put("nightStart", "22:15"); put("nightEnd", "06:35")
                            put("criticalAtNight", true); put("pushEnabled", setting.flag("pushEnabled")!!)
                        })
                        val resume = command("resume")
                        assertEquals(false, repo.state.value.meta!!.obj("analysis")!!.flag("paused"))
                        assertEquals("rejected", resume.result!!.obj("scan")!!.text("status"))
                        val focus = buildJsonObject { putJsonArray("symbols") { add(JsonPrimitive("300502.SZ")) } }
                        assertEquals("300502.SZ", command("watch", focus).result!!.array("addedSymbols")!!.single().jsonPrimitive.content)
                        assertEquals("300502.SZ", command("watch", focus).result!!.array("unchangedSymbols")!!.single().jsonPrimitive.content)
                        assertEquals(1, repo.state.value.records.getValue("watchlist").size)
                        assertEquals("300502.SZ", command("unwatch", focus).result!!.array("removedSymbols")!!.single().jsonPrimitive.content)
                        assertTrue(repo.state.value.records.getValue("watchlist").isEmpty())
                        socket.cancelAndJoin()
                        repo = WorkbenchRepository(store)
                        repo.selectPair(config.pairId)
                        socket = async { RelayClient(repo, client) { }.run(config) }
                        val reopened = waitFor("settings after restart") { it.connection.canWrite }
                        assertEquals("22:15", reopened.meta!!.obj("settings")!!.text("nightStart"))
                        assertEquals("06:35", reopened.meta!!.obj("settings")!!.text("nightEnd"))
                        assertEquals(true, reopened.meta!!.obj("settings")!!.flag("criticalAtNight"))
                        assertEquals("aggressive", reopened.meta!!.obj("analysis")!!.text("profile"))
                        assertEquals(false, reopened.meta!!.obj("analysis")!!.flag("paused"))
                        assertTrue(reopened.records.getValue("watchlist").isEmpty())
                        println("HOLDINGS_FIXTURE_PASS: two consecutive adds, edit, clear; cancelled late OCR; atomic multi-row retry and retained original; night setup, profile, pause/resume and watch/unwatch survive reconnect")
                    }
                    println("HOLDINGS_PASS: name/code search; manual name; reconnect; auto commit; unmentioned preserved; realVision=$realVision; failureCases=${failures.size}")
                } finally { socket.cancelAndJoin() }
            }
        } finally {
            context.deleteDatabase(database)
            onTestMainThread { activity.finish() }
            client.connectionPool.evictAll()
        }
    }

    private fun png(color: Int): ByteArray {
        val bitmap = Bitmap.createBitmap(64, 64, Bitmap.Config.ARGB_8888)
        return try {
            bitmap.eraseColor(color)
            ByteArrayOutputStream().use { output -> check(bitmap.compress(Bitmap.CompressFormat.PNG, 100, output)); output.toByteArray() }
        } finally { bitmap.recycle() }
    }

    private fun holdingImage(): ByteArray {
        val bitmap = Bitmap.createBitmap(1000, 400, Bitmap.Config.ARGB_8888)
        return try {
            val canvas = Canvas(bitmap)
            canvas.drawColor(Color.WHITE)
            val paint = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.BLACK; textSize = 32f }
            canvas.drawText("持仓明细", 35f, 55f, paint)
            canvas.drawText("测试用合成图片", 35f, 110f, paint)
            val columns = listOf(35f, 230f, 480f, 650f, 820f)
            listOf("证券名称", "证券代码", "持仓数量", "可用数量", "成本价").forEachIndexed { i, value ->
                canvas.drawText(value, columns[i], 200f, paint)
            }
            listOf("新易盛", "300502.SZ", "100", "80", "10.00").forEachIndexed { i, value ->
                canvas.drawText(value, columns[i], 275f, paint)
            }
            ByteArrayOutputStream().use { output -> check(bitmap.compress(Bitmap.CompressFormat.PNG, 100, output)); output.toByteArray() }
        } finally { bitmap.recycle() }
    }
}
