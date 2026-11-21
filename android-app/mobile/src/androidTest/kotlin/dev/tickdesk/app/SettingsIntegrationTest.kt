package dev.tickdesk.app

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.AccessibilityServiceInfo
import android.app.KeyguardManager
import android.app.NotificationManager
import android.graphics.Bitmap
import android.graphics.Rect
import android.os.Build
import android.os.Bundle
import android.os.ParcelFileDescriptor
import android.os.SystemClock
import android.view.MotionEvent
import android.view.accessibility.AccessibilityNodeInfo
import androidx.test.platform.app.InstrumentationRegistry
import dev.tickdesk.app.data.RelayClient
import dev.tickdesk.app.ui.objects
import dev.tickdesk.app.ui.stringList
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
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate
import java.time.ZoneId
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import java.util.Base64
import java.io.File

/** Runs the normal Activity and ViewModel on the separately installed demo package. */
class SettingsIntegrationTest {
    @Test fun settingsAndWatchUseRealScreenActionsAndAuthoritativeSnapshots(): Unit = runBlocking(Dispatchers.IO) {
        withDemo("settingsIntegration") { settingsAndWatch() }
    }

    @Test fun chartPeriodsRefreshAndPageThroughRealScreenActions(): Unit = runBlocking(Dispatchers.IO) {
        withDemo("chartIntegration") { chartPeriods() }
    }

    @Test fun openReportAndFollowEvidenceInRealUi(): Unit = runBlocking(Dispatchers.IO) {
        withDemo("reportIntegration") { reportEvidence() }
    }

    @Test fun archivedReportShowsQuantitativeSummaryFromWorkbench(): Unit = runBlocking(Dispatchers.IO) {
        withDemo("archivedQuantSummaryIntegration") { archivedQuantSummary() }
    }

    @Test fun openCatalogueRefreshesWhenManualAnalysisPublishesWithoutNotification(): Unit = runBlocking(Dispatchers.IO) {
        withDemo("manualPublicationIntegration") { manualReportPublication() }
    }

    @Test fun repeatCompleteWorkspaceRoundWithinOnePairing(): Unit = runBlocking(Dispatchers.IO) {
        withDemo("twoRoundIntegration") { twoWorkspaceRounds() }
    }

    @Test fun settingsReadAndRefreshMarketHistoryCoverage(): Unit = runBlocking(Dispatchers.IO) {
        withDemo("historyCoverageIntegration") { historyCoverage() }
    }

    @Test fun openRedactedRiskReportFromSecureLockscreen(): Unit = runBlocking(Dispatchers.IO) {
        withDemo("lockscreenReportIntegration") { lockscreenReport() }
    }

    @Test fun openOversizedReplyWithoutSendingAgain(): Unit = runBlocking(Dispatchers.IO) {
        withDemo("fullReplyIntegration") { fullReply() }
    }

    @Test fun submitAnalysisBeforeExternalProcessStop(): Unit = runBlocking(Dispatchers.IO) {
        withDemo("analysisBeforeProcessStop") { submitBeforeProcessStop() }
    }

    @Test fun readSameAnalysisAfterExternalProcessStop(): Unit = runBlocking(Dispatchers.IO) {
        withDemo("analysisAfterProcessStop") { readAfterProcessStop() }
    }

    @Test fun restoredWorkbenchShowsReadOnlyRecoveryAndWaitsForEvidence(): Unit = runBlocking(Dispatchers.IO) {
        withDemo("restoredWorkbenchIntegration", recovering = true) { restoredWorkbench() }
    }

    @Test fun portfolioRendersSnapshotQuotesFromWorkbench(): Unit = runBlocking(Dispatchers.IO) {
        withDemo("snapshotPortfolioIntegration") { snapshotPortfolio() }
    }

    private suspend fun CoroutineScope.withDemo(flag: String, recovering: Boolean = false, scenario: suspend DemoUi.() -> Unit) {
        val args = InstrumentationRegistry.getArguments()
        assumeTrue(BuildConfig.MOCK_DEMO && args.getString(flag) == "true")
        val activity = launchResumedMainActivity()
        val runtime = (activity.application as TickDeskApplication).runtime
        assertNull("The demo must have no saved pairing before this test", runtime.pairedScope())
        val repo = runtime.repository
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
        repo.selectPair(config.pairId)
        val reconnect = flag == "fullReplyIntegration" && args.getString("fullReplyResume") == "true"
        val socket = async {
            val relay = RelayClient(repo, client) { }
            do {
                val end = relay.run(config)
                if (!reconnect || !end.retry) break
                repo.disconnect(end.reason)
                delay(600)
            } while (isActive)
        }
        try {
            val ui = DemoUi(activity, repo)
            ui.confirmed("initial sync") {
                if (recovering) it.connection.synced && it.connection.localOnline && it.serviceRecoveryActive
                else it.connection.canWrite
            }
            ui.scenario()
        } finally {
            socket.cancelAndJoin()
            repo.selectPair(null)
            onTestMainThread { activity.finish() }
            client.connectionPool.evictAll()
        }
    }

    private class DemoUi(val activity: MainActivity, val repo: WorkbenchRepository) {
        val automation = InstrumentationRegistry.getInstrumentation().uiAutomation
        fun nodes(root: AccessibilityNodeInfo): Sequence<AccessibilityNodeInfo> = sequence {
            yield(root)
            for (index in 0 until root.childCount) root.getChild(index)?.let { yieldAll(nodes(it)) }
        }
        fun visible(): List<AccessibilityNodeInfo> {
            if (Build.VERSION.SDK_INT >= 34) automation.clearCache()
            return automation.rootInActiveWindow?.let { nodes(it).filter { n -> n.isVisibleToUser }.toList() }.orEmpty()
        }
        fun matches(node: AccessibilityNodeInfo, label: String, partial: Boolean = false) =
            listOfNotNull(node.text?.toString(), node.contentDescription?.toString()).any {
                if (partial) it.contains(label) else it == label
            }
        fun viewport() = visible().filter(AccessibilityNodeInfo::isScrollable).map { Rect().also(it::getBoundsInScreen) }.maxByOrNull {
            it.height().toLong() * it.width()
        }
        suspend fun scroll(forward: Boolean) {
            val rect = viewport() ?: return
            val x = rect.left + rect.width() * .43f
            val start = rect.top + rect.height() * if (forward) .72f else .48f
            val distance = rect.height() * if (forward) .24f else -.24f
            val at = SystemClock.uptimeMillis()
            fun swipe(action: Int, y: Float) {
                val event = MotionEvent.obtain(at, SystemClock.uptimeMillis(), action, x, y, 0)
                try { automation.injectInputEvent(event, true) } finally { event.recycle() }
            }
            swipe(MotionEvent.ACTION_DOWN, start)
            for (step in 1..5) {
                delay(38)
                swipe(MotionEvent.ACTION_MOVE, start - distance * step / 5)
            }
            delay(125)
            swipe(MotionEvent.ACTION_UP, start - distance)
        }
        suspend fun find(label: String, seek: Boolean = false, partial: Boolean = false,
                         predicate: (AccessibilityNodeInfo) -> Boolean = { true }): AccessibilityNodeInfo =
            withTimeoutOrNull(15_000) {
                var surface = visible()
                var found = surface.firstOrNull { matches(it, label, partial) && predicate(it) }
                var forward = true
                while (found == null) {
                    if (seek) scroll(forward)
                    delay(230)
                    val current = visible()
                    if (surface.mapNotNull { it.text?.toString() } == current.mapNotNull { it.text?.toString() }) forward = !forward
                    surface = current
                    found = surface.firstOrNull { matches(it, label, partial) && predicate(it) }
                }
                found
            } ?: error("Missing $label. Visible labels: ${visible().mapNotNull { it.text }.take(35)}")
        suspend fun tap(node: AccessibilityNodeInfo): Boolean {
            val target = generateSequence(node) { it.parent }.take(4).firstOrNull { it.isEnabled && it.isClickable } ?: node
            if (target.isClickable && target.isEnabled && target.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
                delay(140)
                return true
            }
            val bounds = Rect().also(target::getBoundsInScreen)
            if (!target.isEnabled || bounds.isEmpty) return false
            val at = SystemClock.uptimeMillis()
            fun touch(action: Int): Boolean {
                val touch = MotionEvent.obtain(at, SystemClock.uptimeMillis(), action, bounds.exactCenterX(), bounds.exactCenterY(), 0)
                return try { automation.injectInputEvent(touch, true) } finally { touch.recycle() }
            }
            val down = touch(MotionEvent.ACTION_DOWN)
            delay(60)
            val up = touch(MotionEvent.ACTION_UP)
            delay(140)
            return down && up
        }
        suspend fun click(label: String, seek: Boolean = false) {
            assertTrue("Tap $label", tap(find(label, seek)))
        }
        suspend fun tab(label: String) {
            val matches = visible().filter { matches(it, label) }
            val bottom = matches.maxByOrNull { Rect().also(it::getBoundsInScreen).bottom } ?: find(label)
            assertTrue("Tap tab $label", tap(bottom))
        }
        suspend fun field(from: String, to: String, seek: Boolean = false) {
            fun editor(label: AccessibilityNodeInfo) = generateSequence(label) { it.parent }.firstOrNull { node ->
                node.isEditable || node.actionList.any { it.id == AccessibilityNodeInfo.ACTION_SET_TEXT }
            }
            val node = editor(find(from, seek) { editor(it) != null })!!
            assertTrue("Edit $from", node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, Bundle().apply {
                putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, to)
            }))
        }
        suspend fun confirmed(label: String, test: (WorkbenchProjection) -> Boolean) =
            withTimeoutOrNull(12_000) { repo.state.first(test) } ?: error(
                "Waiting for $label. Connection ${repo.state.value.connection}. Recent commands " +
                    repo.state.value.intents.takeLast(8).map { "${it.command}:${it.stage}:${it.errorCode}" },
            )

        fun capture(directory: String, name: String) {
            val screenshot = automation.takeScreenshot() ?: error("Could not record $name")
            try {
                val target = File(File(activity.filesDir, directory).apply { mkdirs() }, "$name.png")
                target.outputStream().use { assertTrue(screenshot.compress(Bitmap.CompressFormat.PNG, 100, it)) }
            } finally {
                screenshot.recycle()
            }
        }

        suspend fun archivedQuantSummary() {
            val args = InstrumentationRegistry.getArguments()
            val id = requireNotNull(args.getString("archivedReportId"))
            val ready = File(activity.filesDir, "archived-quant-summary.ready")
            val cached = File(activity.filesDir, "archived-quant-summary.cached")
            val upgrade = args.getString("cachedSummaryUpgrade") == "true"
            try {
                withTimeout(12_000) { while (!ready.exists()) delay(100) }
                tab("对话")
                assertTrue(tap(find("分析报告", partial = true) { it.isClickable || it.parent?.isClickable == true }))
                val summary = "研究观察 · 入场待核实 4 项 · 退出触发 4 项 · 评分待核实 4 项"
                if (upgrade) click("刷新报告目录")
                confirmed("archived summary from the real report directory") { projection ->
                    val items = if (upgrade) projection.intents.lastOrNull { it.command == "list_reports" }
                        ?.result?.objects("items") else projection.reportReads.catalogues[ReportCatalogueQuery()]?.objects("items")
                    items?.any { it.text("reportId") == id && (upgrade || it.text("summary") == summary) } == true
                }
                find("报告摘要")
                find(if (upgrade) "量化结论不可用" else summary, partial = true)
                click("查看报告")
                val detail = confirmed("original report detail") { projection ->
                    val received = projection.intents.lastOrNull { it.command == "get_report" && it.data.text("reportId") == id }
                        ?.result?.obj("header")?.text("summary")
                    if (upgrade) received?.contains("量化结论不可用") == true else received == summary
                }.intents.last { it.command == "get_report" && it.data.text("reportId") == id }.result!!
                assertEquals(18, detail.objects("sources").size)
                find("报告详情")
                find("报告摘要")
                if (upgrade) {
                    find("量化结论不可用", partial = true)
                    capture("archived-quant-cache", "detail-before")
                    cached.createNewFile()
                    val refreshed = confirmed("new subscribed catalogue after local restart") { projection ->
                        projection.reportReads.catalogues[ReportCatalogueQuery()]?.objects("items")
                            ?.any { it.text("reportId") == id && it.text("summary") == summary } == true
                    }
                    assertEquals(detail, refreshed.intents.last { it.command == "get_report" && it.data.text("reportId") == id }.result)
                    find(summary, partial = true)
                    capture("archived-quant-cache", "detail-after")
                    click("返回")
                    find(summary, partial = true)
                    capture("archived-quant-cache", "catalogue-after")
                }
                find(summary, partial = true)
            } finally {
                ready.delete()
                cached.delete()
            }
        }

        suspend fun snapshotPortfolio() {
            val arguments = InstrumentationRegistry.getArguments()
            val expectedPrices = mapOf("300502.SZ" to "164.45", "510300.SH" to "4.045")
            val expectedTimes = mapOf("300502.SZ" to arguments.getString("stockSnapshotAt").toString(),
                "510300.SH" to arguments.getString("fundSnapshotAt").toString())
            val state = confirmed("both captured snapshot prices from the workbench") { projection ->
                val positions = projection.records["positions"].orEmpty().associateBy { it.text("symbol") }
                positions.keys == expectedPrices.keys && expectedPrices.all { (symbol, price) ->
                    positions.getValue(symbol).text("lastPrice") == price
                }
            }
            for (position in state.records.getValue("positions")) {
                assertEquals(expectedTimes[position.text("symbol")], position["priceAsOf"]?.jsonPrimitive?.content)
                assertEquals(JsonNull, position["marketValue"])
            }
            tab("持仓")
            find("参考持仓市值（CNY）")
            find("¥ 53,380.00")
            find("实时估值 0 / 2")
            capture("intraday-reference-valuation", "workbench-total")
            find("4,045", seek = true)
            find("参考市值", seek = true)
            capture("intraday-reference-valuation", "workbench-etf")
            click("持仓估值明细", seek = true)
            find("参考持仓市值（CNY）")
            find("¥ 53,380.00")
            find("实时估值 0 / 2")
            capture("intraday-reference-valuation", "workbench-details")
        }

        suspend fun restoredWorkbench() {
            val args = InstrumentationRegistry.getArguments()
            val state = repo.state.value
            assertEquals(requireNotNull(args.getString("restoredEpoch")), state.epoch)
            assertNotEquals(requireNotNull(args.getString("originalEpoch")), state.epoch)
            assertFalse(state.connection.canWrite)
            assertEquals(1, state.records["positions"]?.size)
            tab("设置")
            find("工作台处于恢复模式，普通业务保持只读。", seek = true)
            capture("restore-487", "settings-read-only")
            click("打开恢复对账", seek = true)
            find("恢复对账")
            find("证据和差异", seek = true)
            find("核对版本", seek = true, partial = true)
            find("未关闭差异 0 · 证据待封存", seek = true)
            capture("restore-487", "real-recovery")
            val confirm = find("确认恢复", seek = true)
            assertFalse("Recovery cannot be confirmed before evidence is sealed",
                generateSequence(confirm) { it.parent }.take(4).any { it.isEnabled && it.isClickable })
            if (args.getString("originalReviewsResolved") != "true") return

            click("收集、上传并冻结手机证据", seek = true)
            find("手机证据已冻结 · 0 条", seek = true)
            click("封存证据清单", seek = true)
            find("封存已验证证据")
            click("确认")
            find("未关闭差异 0 · 证据已封存", seek = true)
            click("确认恢复", seek = true)
            find("确认完成服务恢复")
            click("确认")
            val restored = confirmed("fresh normal snapshot after confirmation") {
                it.connection.synced && !it.serviceRecoveryActive && it.epoch == state.epoch
            }
            if (restored.requiresReview) {
                assertTrue(automation.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK))
                click("核对完成，重新开放写入", seek = true)
            }
            val writable = confirmed("normal capability after fresh snapshot") { it.connection.canWrite }
            assertEquals(state.epoch, writable.epoch)
            val ledgerFields = setOf("symbol", "name", "qty", "availableQty", "cost", "positionRevision", "asOf",
                "pendingLots", "reconciliationRequired")
            fun ledgerHoldings(snapshot: WorkbenchProjection) = snapshot.records.getValue("positions")
                .map { position -> position.filterKeys { it in ledgerFields } }
            assertEquals(ledgerHoldings(state), ledgerHoldings(writable))
            tab("持仓")
            capture("restore-487", "restored-workspace")
        }

        suspend fun fullReply() {
            val epoch = repo.state.value.epoch
            confirmed("full reply capability") { state ->
                "get_reply_page" in state.reads["get_status"]?.obj("protocol")?.stringList("supportedCommands").orEmpty()
            }
            adjustReportFixtureHolding("300502.SZ", 100)
            tab("对话")
            field("输入消息", "/分析 300502.SZ")
            click("发送消息")
            val excerpt = confirmed("oversized chat excerpt") { state ->
                state.replies.any { it.objects("sources").firstOrNull()?.text("ref")?.startsWith("local:chat-output:") == true }
            }.replies.single()
            val id = requireNotNull(excerpt.text("requestId"))
            find("回复超过传输上限", seek = true, partial = true)
            assertTrue(repo.state.value.intents.none { it.command == "get_reply_page" })
            field("输入消息", "保留未发送草稿")
            click("查看全文", seek = true)
            find("全文")
            find("完整回复起点🐾", partial = true)
            capture("full-reply", "start")

            val pages = repo.state.value.intents.filter { it.command == "get_reply_page" && it.stage == DeliveryStage.APPLIED }
            assertTrue(pages.size > 1)
            assertTrue(pages.all { it.data.text("requestId") == id && it.data.text("serverEpoch") == epoch })
            if (InstrumentationRegistry.getArguments().getString("fullReplyResume") == "true") {
                assertEquals("Resume at the first unread offset without loading page zero again", 1,
                    pages.count { it.result?.number("offset") == 0L })
            }
            val sequence = ReplyPageSequence(id)
            val full = pages.distinctBy { it.result?.number("offset") }.sortedBy { it.result?.number("offset") }
                .mapNotNull { sequence.append(requireNotNull(it.result)) }.single()
            assertTrue(sequence.loadedChars > 24_000)
            assertTrue("The original ending must be included", "完整回复末尾📎" in full.pages.joinToString(""))
            suspend fun advanceReader() {
                val list = visible().firstOrNull { it.isScrollable && it.actionList.any { action ->
                    action.id == AccessibilityNodeInfo.ACTION_SCROLL_FORWARD
                } }
                if (list?.performAction(AccessibilityNodeInfo.ACTION_SCROLL_FORWARD) != true) scroll(forward = true)
                delay(180)
            }
            withTimeout(38_000) {
                while (visible().none { matches(it, "完整回复末尾📎", partial = true) }) advanceReader()
            }
            repeat(3) { advanceReader() }
            capture("full-reply", "end")
            assertTrue("Use the actual Android Back action", automation.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK))
            find("保留未发送草稿")

            click("分析报告", seek = true)
            click("查看报告", seek = true)
            find("报告详情")
            confirmed("same report is available as an excerpt") { state ->
                state.intents.any { it.command == "get_report" && it.stage == DeliveryStage.APPLIED &&
                    it.result?.obj("header")?.text("reportId") == id &&
                    it.result?.objects("sources")?.firstOrNull()?.text("ref")?.startsWith("local:chat-output:") == true }
            }
            click("查看全文", seek = true)
            find("完整回复起点🐾", partial = true)
            capture("full-reply", "report")
            assertEquals(listOf(id), repo.state.value.intents.filter { it.command == "chat" }.map { it.id })
        }

        suspend fun submitBeforeProcessStop() {
            tab("对话")
            field("输入消息", "/分析 300502.SZ")
            click("发送消息")
            val pending = confirmed("one accepted analysis") { state ->
                state.intents.any { it.command == "chat" && it.stage == DeliveryStage.ACCEPTED }
            }
            find("已受理 · 等待结果", seek = true, partial = true)
            val request = pending.intents.single { it.command == "chat" }.id
            assertTrue(pending.replies.isEmpty())
            capture("analysis-process-stop", "pending-before-stop")
            File(activity.filesDir, "analysis-before-stop.ready").writeText("$request\n${pending.epoch}")
            awaitCancellation()
        }

        suspend fun readAfterProcessStop() {
            val (id, epoch) = File(activity.filesDir, "analysis-before-stop.ready").readLines()
            val arguments = InstrumentationRegistry.getArguments()
            val down = arguments.getString("analysisCompletesWhileStopped") == "true"
            tab("对话")
            val resumed = repo.state.value
            assertEquals(epoch, resumed.epoch)
            val original = resumed.intents.single { it.command == "chat" }
            assertEquals(id, original.id)
            assertEquals("/分析 300502.SZ", original.data.text("text"))
            if (down) {
                find("回复超过传输上限", partial = true)
                capture("analysis-process-stop", "reply-present-at-relaunch")
            } else {
                find("/分析 300502.SZ", seek = true)
                find("已受理 · 等待结果", seek = true, partial = true)
                assertTrue(resumed.replies.none { it.text("requestId") == id })
                capture("analysis-process-stop", "pending-after-relaunch")
            }
            File(activity.filesDir, "analysis-after-relaunch.ready").writeText(id)

            fun replyIn(state: WorkbenchProjection) = state.replies.firstOrNull { it.text("requestId") == id }
                ?: state.messages.firstOrNull { it.text("requestId") == id && it.text("role") == "assistant" }?.obj("reply")
            val completed = confirmed("original analysis completed after Android process stop") { replyIn(it) != null }
            val replied = requireNotNull(replyIn(completed))
            assertEquals(replied.text("errorCode"), "completed", replied.text("status"))
            click("分析报告", seek = true)
            click("查看报告", seek = true)
            find("报告详情")
            val detail = confirmed("same archived report with original evidence") { state ->
                state.intents.any { it.command == "get_report" && it.stage == DeliveryStage.APPLIED &&
                    it.result?.obj("header")?.text("reportId") == id }
            }.intents.last { it.command == "get_report" && it.stage == DeliveryStage.APPLIED }.result!!
            assertTrue(detail.objects("sources").any { it.text("ref")?.startsWith("local:chat-output:") == true })
            find("研究观察 · 入场", seek = true, partial = true)
            capture("analysis-process-stop", "archived-quant-summary")
            click("查看全文", seek = true)
            find("全文")
            find("复原分析正文开始", partial = true)
            val pages = confirmed("original reply pages fetched to the end") { state ->
                state.intents.any { it.command == "get_reply_page" && it.stage == DeliveryStage.APPLIED &&
                    it.result?.get("nextOffset") == JsonNull }
            }.intents.filter { it.command == "get_reply_page" && it.stage == DeliveryStage.APPLIED }
            assertTrue(pages.size > 1)
            assertTrue(pages.all { it.data.text("requestId") == id && it.data.text("serverEpoch") == epoch })
            val sequence = ReplyPageSequence(id)
            val full = pages.distinctBy { it.result?.number("offset") }.sortedBy { it.result?.number("offset") }
                .mapNotNull { sequence.append(requireNotNull(it.result)) }.single()
            assertTrue(sequence.loadedChars > 24_000)
            val ending = "进程恢复后的报告全文末尾"
            assertTrue(ending in full.pages.joinToString(""))
            suspend fun advanceReader() {
                val list = visible().firstOrNull { it.isScrollable }
                if (list?.performAction(AccessibilityNodeInfo.ACTION_SCROLL_FORWARD) != true) scroll(forward = true)
                delay(180)
            }
            withTimeout(40_000) {
                while (visible().none { matches(it, ending, partial = true) }) advanceReader()
            }
            repeat(8) { advanceReader() }
            capture("analysis-process-stop", "full-report-end")
            assertEquals(listOf(id), repo.state.value.intents.filter { it.command == "chat" }.map { it.id })
            File(activity.filesDir, "analysis-process-stop/readback.json").writeText(buildJsonObject {
                put("requestId", id)
                put("characters", sequence.loadedChars)
                put("sha256", pages.first().result?.text("sha256"))
                put("pageCount", pages.distinctBy { it.result?.number("offset") }.size)
                put("summary", detail.obj("header")?.text("summary"))
            }.toString())
        }

        suspend fun lockscreenReport() {
            val settings = InstrumentationRegistry.getArguments()
            val pin = requireNotNull(settings.getString("lockscreenPin"))
            require(pin.matches(Regex("[0-9]{4}")))
            val keyguard = activity.getSystemService(KeyguardManager::class.java)
            val notifications = activity.getSystemService(NotificationManager::class.java)
            val background = (activity.application as TickDeskApplication).runtime.backgroundReminders
            fun shell(command: String) = ParcelFileDescriptor.AutoCloseInputStream(automation.executeShellCommand(command))
                .use { it.readBytes().decodeToString().trim() }
            val directory = File(activity.filesDir, "lockscreen-report").apply { mkdirs() }
            val ready = File(directory, "ready")
            val headline = "工作台有新分析报告"
            val privateLabel = "打开核对结论、证据与有效期。"
            automation.serviceInfo = automation.serviceInfo.apply { flags = flags or AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS }
            assertEquals("1", shell("getprop ro.kernel.qemu"))
            assertEquals("1", shell("settings get secure lock_screen_show_notifications"))
            assertEquals("0", shell("settings get secure lock_screen_allow_private_notifications"))
            assertTrue(keyguard.isDeviceSecure)
            assertTrue(notifications.areNotificationsEnabled())
            assertFalse(ready.exists())
            assertNull("The isolated demo should start with reminders opted out", background.state.value.enabledPair)
            confirmed("report subscription") { snapshot ->
                snapshot.reads["list_reports"] != null
            }
            notifications.cancelAll()
            try {
                onTestMainThread { background.setEnabled(true) }
                withTimeout(5_000) { background.state.first { it.running } }
                assertTrue(automation.performGlobalAction(AccessibilityService.GLOBAL_ACTION_HOME))
                shell("input keyevent KEYCODE_SLEEP")
                delay(650)
                shell("input keyevent KEYCODE_WAKEUP")
                withTimeout(5_000) { while (!keyguard.isDeviceLocked) delay(100) }
                assertTrue("Background reminders must be running under the secure keyguard", background.state.value.running)
                ready.writeText("secure keyguard shown")
                val first = confirmed("current risk report checked before Android post") { snapshot ->
                    snapshot.intents.any { it.command == "get_report" && it.stage == DeliveryStage.APPLIED &&
                        it.result?.obj("header")?.text("stance") == "restricted" }
                }.intents.last { it.command == "get_report" && it.stage == DeliveryStage.APPLIED }
                val report = requireNotNull(first.result?.obj("header")?.text("reportId"))
                withTimeout(5_000) { while (notifications.activeNotifications.none { it.tag?.endsWith(":$report") == true }) delay(100) }
                capture("lockscreen-report", "keyguard")
                assertTrue("Report posted while the secure keyguard is still locked", keyguard.isDeviceLocked)
                val notice = withTimeoutOrNull(2_500) {
                    var title = visible().firstOrNull { matches(it, headline) }
                    while (title == null) {
                        delay(160)
                        title = visible().firstOrNull { matches(it, headline) }
                    }
                    title
                } ?: run {
                    assertTrue(automation.performGlobalAction(AccessibilityService.GLOBAL_ACTION_NOTIFICATIONS))
                    delay(650)
                    val grouped = visible().firstOrNull { matches(it, "TickDesk 联调") }
                    val summary = generateSequence(grouped) { it.parent }.take(5).firstNotNullOfOrNull { row ->
                        nodes(row).firstOrNull { it.isVisibleToUser && it.isClickable &&
                            it.viewIdResourceName?.endsWith(":id/expand_button") == true }
                    }
                    summary?.let { assertTrue("Expand the grouped TickDesk notifications", tap(it)) }
                    capture("lockscreen-report", "expanded")
                    find(headline)
                }
                assertEquals("com.android.systemui", notice.packageName?.toString())
                assertTrue(keyguard.isDeviceLocked)
                find("解锁后查看分析报告。", partial = true)
                assertTrue("The private body is redacted", visible().none { matches(it, privateLabel) || matches(it, "可卖依据待复核。") })
                capture("lockscreen-report", "redacted")
                assertTrue("Tap the locked Android notification row", tap(notice))
                delay(800)
                assertTrue("Opening the alert requires authentication", keyguard.isDeviceLocked)
                capture("lockscreen-report", "authentication")
                shell("input keyevent ${pin.map { "KEYCODE_$it" }.joinToString(" ")} KEYCODE_ENTER")
                withTimeout(6_000) { while (keyguard.isDeviceLocked) delay(100) }
                val reopened = confirmed("same risk report revalidated by the lockscreen click") { snapshot ->
                    snapshot.intents.any { it.id != first.id && it.command == "get_report" &&
                        it.data.text("reportId") == report && it.stage == DeliveryStage.APPLIED &&
                        it.result?.obj("header")?.text("state") == "current" }
                }.intents.last { it.command == "get_report" && it.stage == DeliveryStage.APPLIED }
                assertEquals(report, reopened.result?.obj("header")?.text("reportId"))
                find("报告详情")
                find("可卖依据待复核。", seek = true, partial = true)
                capture("lockscreen-report", "destination")
            } finally {
                ready.delete()
                onTestMainThread { background.setEnabled(false) }
                notifications.cancelAll()
            }
        }
        suspend fun settingsAndWatch() {
            tab("设置")
            find("配对与连接")
            find("盘中扫描")
            find("扫描档案")
            find("提醒设置")
            find("工作台服务状态")
            assertTrue("The settings overview must stay free of technical details", visible().none {
                matches(it, "公钥指纹", partial = true) || matches(it, "全市场历史覆盖", partial = true) ||
                    matches(it, "分级强提醒策略", partial = true)
            })
            capture("compact-settings", "overview")

            click("配对与连接")
            find("公钥指纹", seek = true, partial = true)
            find("重新连接与同步", seek = true)
            find("导入配对配置", seek = true)
            capture("compact-settings", "connection")
            assertTrue(automation.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK))

            click("工作台服务状态", seek = true)
            find("AI 分析", seek = true)
            find("行情与数据", seek = true)
            click("查看诊断信息", seek = true)
            find("运行诊断日志", seek = true)
            capture("compact-settings", "services")
            click("查看", seek = true)
            val logs = confirmed("service logs") { it.reads["get_logs"] != null }.reads.getValue("get_logs").objects("items")
            find(if (logs.isEmpty()) "没有可显示的日志。" else "导出本页日志", seek = true)
            assertTrue(automation.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK))

            click("盘中扫描", seek = true)
            click("暂停盘中量化扫描", seek = true)
            click("确认")
            confirmed("pause") { it.meta?.obj("analysis")?.flag("paused") == true }
            find("盘中量化扫描已暂停")
            click("恢复盘中量化扫描", seek = true)
            click("确认")
            confirmed("resume") { state -> state.meta?.obj("analysis")?.flag("paused") == false &&
                state.intents.any { it.command == "resume" && it.stage == DeliveryStage.APPLIED && it.result?.obj("scan")?.text("status") == "rejected" } }
            find("分析运行中")

            assertTrue(automation.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK))
            delay(450)
            visible().firstOrNull { matches(it, "返回顶部") }?.let { top ->
                assertTrue(tap(top))
                delay(550)
            }
            click("扫描档案", seek = true)
            val profile = find("激进动量", seek = true)
            assertTrue("Tap profile", tap(profile))
            try {
                confirmed("profile") { it.meta?.obj("analysis")?.text("profile") == "aggressive" }
            } catch (failure: IllegalStateException) {
                automation.takeScreenshot()?.let { image ->
                    try {
                        File(activity.filesDir, "test-profile-unconfirmed.png").outputStream().use {
                            image.compress(Bitmap.CompressFormat.PNG, 100, it)
                        }
                    } finally { image.recycle() }
                }
                val semantics = generateSequence(profile) { it.parent }.take(4).map { node ->
                    "${node.className} ${node.isEnabled} ${node.isClickable} ${Rect().also(node::getBoundsInScreen)}"
                }.toList()
                throw IllegalStateException("${failure.message}. Profile semantics $semantics", failure)
            }
            assertTrue(automation.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK))
            find("激进动量", seek = true)
            click("后台提醒", seek = true)
            find("查看后台连接通知通道", seek = true)
            assertTrue(automation.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK))
            click("提醒设置", seek = true)
            find("提醒设置")
            find("打开 Android 通知设置", seek = true)
            click("查看提醒规则", seek = true)
            find("分级提醒", seek = true)
            click("收起提醒规则", seek = true)
            val settings = repo.state.value.meta!!.obj("settings")!!
            field(settings.text("nightStart")!!, "22:15", seek = true)
            field(settings.text("nightEnd")!!, "06:35", seek = true)
            if (settings.flag("criticalAtNight") != true) click("夜间允许严重提醒", seek = true)
            click("提交提醒设置", seek = true)
            confirmed("night rules") { it.meta?.obj("settings")?.text("nightStart") == "22:15" &&
                it.meta?.obj("settings")?.text("nightEnd") == "06:35" &&
                it.meta?.obj("settings")?.flag("criticalAtNight") == true }
            find("最后确认 · 22:15–06:35 · 推送关闭", seek = true)
            capture("compact-settings", "reminders")
            assertTrue(automation.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK))
            find("22:15–06:35", seek = true, partial = true)

            tab("选股")
            click("搜索证券 / 添加关注", seek = true)
            val query = find("证券代码或名称")
            val editor = generateSequence(query) { it.parent }.firstOrNull { it.isEditable } ?: query
            assertTrue(editor.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, Bundle().apply {
                putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, "新易盛")
            }))
            click("查询证券", seek = true)
            find("新易盛", seek = true)
            click("加入关注", seek = true)
            confirmed("watch") { it.records["watchlist"]?.singleOrNull()?.text("symbol") == "300502.SZ" }
            click("取消关注", seek = true)
            confirmed("unwatch") { it.records["watchlist"]?.isEmpty() == true &&
                it.intents.any { item -> item.command == "unwatch" && item.stage == DeliveryStage.APPLIED } }
            find("加入关注")
        }

        suspend fun chartPeriods() {
            val first = "300502.SZ"
            val second = "600000.SH"
            val pair = repo.state.value.pairId!!
            repo.requestAuxiliary("get_status", pair)
            confirmed("market capabilities") { "get_quote_status" in it.reads["get_status"]?.obj("protocol")?.stringList("supportedCommands").orEmpty() }

            fun requestKey(row: IntentView, symbol: String, period: String, adjustment: String, before: String?) =
                row.command == "get_chart" && row.data.text("symbol") == symbol &&
                    (row.data.text("timeframe") ?: "1d") == period && row.data.text("adjustment") == adjustment &&
                    row.data.text("beforeDate") == before

            suspend fun chart(symbol: String, period: String, adjustment: String, before: String? = null, after: String? = null): JsonObject {
                val completed = confirmed("$symbol $period $adjustment before $before") { projection ->
                    projection.intents.lastOrNull { requestKey(it, symbol, period, adjustment, before) }
                        ?.let { it.id != after && it.stage == DeliveryStage.APPLIED && it.result?.objects("candles")?.isNotEmpty() == true } == true
                }.intents.last { requestKey(it, symbol, period, adjustment, before) }
                return completed.result!!.also { data ->
                    assertEquals(symbol, data.text("symbol"))
                    assertEquals(adjustment, data.text("adjustment"))
                    assertEquals(period, data.text("timeframe") ?: "1d")
                    assertNotNull(data.obj("source"))
                    assertNotNull(data.number("asOf"))
                }
            }

            suspend fun plot(data: JsonObject, label: String) =
                find("${data.objects("candles").size} 根$label", seek = true, partial = true)

            suspend fun selectedCandle(candle: JsonObject) {
                find("所选 ${candle.text("date")}")
                for ((key, label) in listOf("open" to "开", "high" to "高", "low" to "低", "close" to "收")) {
                    find("$label ${candle.text(key)}")
                }
            }

            suspend fun scrub(data: JsonObject, label: String, from: Int, to: Int) {
                val candleCount = data.objects("candles").size
                val chartBounds = Rect().also(plot(data, label)::getBoundsInScreen)
                assertTrue("The chart must be visible for the swipe", chartBounds.width() > 100 && chartBounds.height() > 70)
                val fromX = chartBounds.left + chartBounds.width() * (from + 0.5f) / candleCount
                val toX = chartBounds.left + chartBounds.width() * (to + 0.5f) / candleCount
                val at = SystemClock.uptimeMillis()
                fun touch(action: Int, x: Float) {
                    val event = MotionEvent.obtain(at, SystemClock.uptimeMillis(), action, x, chartBounds.exactCenterY(), 0)
                    try { assertTrue(automation.injectInputEvent(event, true)) } finally { event.recycle() }
                }
                touch(MotionEvent.ACTION_DOWN, fromX)
                for (step in 1..10) {
                    delay(30)
                    touch(MotionEvent.ACTION_MOVE, fromX + (toX - fromX) * step / 10)
                }
                delay(110)
                touch(MotionEvent.ACTION_UP, toX)
                selectedCandle(data.objects("candles")[to])
            }

            suspend fun top() {
                val jump = visible().firstOrNull { matches(it, "返回顶部") }
                if (jump != null) tap(jump) else repeat(3) { scroll(false) }
                delay(450)
            }

            tab("选股")
            click("搜索证券 / 添加关注", seek = true)
            field("证券代码或名称", "新易盛")
            click("查询证券", seek = true)
            find("新易盛", seek = true)
            click("加入关注", seek = true)
            confirmed("first watched") { it.records["watchlist"]?.singleOrNull()?.text("symbol") == first }
            click("查看 K 线", seek = true)
            find("日线 · 只读行情")
            val daily = chart(first, "1d", "none")
            val quote = confirmed("$first quote") { state -> state.intents.any {
                it.command == "get_quote_status" && it.stage == DeliveryStage.APPLIED &&
                    it.result?.objects("items")?.singleOrNull()?.text("symbol") == first
            } }.intents.last { it.command == "get_quote_status" && it.stage == DeliveryStage.APPLIED }.result!!
            val sessionLabel = when (quote.text("session")) {
                "closed" -> "休市"
                "open" -> "交易时段"
                else -> "交易时段待核实"
            }
            find(sessionLabel, seek = true, partial = true)
            find("数据源限流", seek = true, partial = true)
            find("快照时间", seek = true, partial = true)
            find("上次参考价 12.25", seek = true)
            capture("chart-integration", "quote")
            plot(daily, "日线")
            val dailyBars = daily.objects("candles")
            scrub(daily, "日线", dailyBars.lastIndex, 0)
            capture("chart-integration", "drag-early-day")
            scrub(daily, "日线", 0, dailyBars.lastIndex)
            capture("chart-integration", "drag-latest-day")

            top()
            click("周线")
            val weekly = chart(first, "1w", "none")
            plot(weekly, "周线")
            assertTrue("Need more than one week page", weekly.flag("hasMore") == true)
            val before = weekly.text("nextBeforeDate")!!
            top()
            click("更早周线", seek = true)
            val older = chart(first, "1w", "none", before)
            assertTrue(older.objects("candles").all { it.text("date")!! < before })
            plot(older, "周线")
            val olderBars = older.objects("candles")
            selectedCandle(olderBars.last())
            scrub(older, "周线", olderBars.lastIndex, olderBars.size / 2)
            capture("chart-integration", "older-week")
            top()
            click("返回最新", seek = true)
            plot(chart(first, "1w", "none"), "周线")
            top()
            click("前复权")
            val forward = chart(first, "1w", "forward")
            assertNotEquals(weekly.objects("candles").map { it.text("close") }, forward.objects("candles").map { it.text("close") })
            plot(forward, "周线")
            top()
            click("月线")
            val monthly = chart(first, "1mo", "forward")
            plot(monthly, "月线")
            selectedCandle(monthly.objects("candles").last())
            capture("chart-integration", "forward-month")
            assertNotEquals(weekly.objects("candles").map { it.text("date") }, monthly.objects("candles").map { it.text("date") })
            val requestId = repo.state.value.intents.last { requestKey(it, first, "1mo", "forward", null) }.id
            top()
            click("刷新本页", seek = true)
            plot(chart(first, "1mo", "forward", after = requestId), "月线")

            click("返回")
            click("取消关注", seek = true)
            confirmed("first unwatched") { it.records["watchlist"]?.isEmpty() == true }
            top()
            field("新易盛", second, seek = true)
            click("查询证券", seek = true)
            find("浦发银行", seek = true)
            click("查看 K 线", seek = true)
            find("日线 · 只读行情")
            var attempt = confirmed("second chart preparing") { projection ->
                projection.intents.lastOrNull { requestKey(it, second, "1d", "none", null) }?.errorCode == "CHART_PREPARING"
            }.intents.last { requestKey(it, second, "1d", "none", null) }
            find("工作台正在准备行情。稍后可刷新本页。", seek = true)
            capture("chart-integration", "preparing-second-symbol")
            withTimeout(25_000) {
                do {
                    val lastId = attempt.id
                    top()
                    click("刷新本页", seek = true)
                    attempt = confirmed("second chart after refresh") { projection ->
                        projection.intents.lastOrNull { requestKey(it, second, "1d", "none", null) }
                            ?.let { it.id != lastId && it.stage in setOf(DeliveryStage.APPLIED, DeliveryStage.REJECTED) } == true
                    }.intents.last { requestKey(it, second, "1d", "none", null) }
                    if (attempt.errorCode == "CHART_PREPARING") delay(700)
                } while (attempt.errorCode == "CHART_PREPARING")
            }
            assertNull(attempt.errorCode)
            val secondDaily = chart(second, "1d", "none")
            plot(secondDaily, "日线")
            selectedCandle(secondDaily.objects("candles").last())
            assertNotEquals(daily.objects("candles").last().text("close"), secondDaily.objects("candles").last().text("close"))
            click("返回")
            find("查询证券", seek = true)
        }

        private suspend fun adjustReportFixtureHolding(symbol: String, qty: Long) {
            repo.enqueue("adjust_position", buildJsonObject {
                put("symbol", symbol); put("name", "新易盛"); put("qty", qty); put("availableQty", qty); put("cost", "10.00")
                put("basePositionRevision", repo.state.value.meta!!.number("positionRevision")!!)
                put("asOf", System.currentTimeMillis()); put("reason", "隔离报告台账更新")
                putJsonArray("resolvedExecutionIds") {}; put("pendingLots", JsonNull)
            })
            confirmed("holding $qty") { it.records["positions"]?.singleOrNull()?.number("qty") == qty }
        }

        suspend fun manualReportPublication() {
            val manager = activity.getSystemService(NotificationManager::class.java)
            assertTrue("Notification permission must be enabled to prove a silent publication", manager.areNotificationsEnabled())
            val baseline = manager.activeNotifications.filter { it.notification.channelId == "tickdesk_analysis_report" }.map { it.key }.toSet()
            val ready = File(activity.filesDir, "report-publication/catalogue-ready")
            assertFalse(ready.exists())
            adjustReportFixtureHolding("300502.SZ", 100)
            tab("对话")
            click("分析报告")
            find("此范围暂无分析报告。")
            capture("report-publication", "empty")
            val originalRead = repo.state.value.intents.last { it.command == "list_reports" && it.stage == DeliveryStage.APPLIED }

            click("返回")
            field("输入消息", "/分析 300502.SZ")
            click("发送消息")
            val id = confirmed("manual analysis accepted independently of the catalogue") { state ->
                state.intents.any { it.command == "chat" && it.stage == DeliveryStage.ACCEPTED }
            }.intents.single { it.command == "chat" }.id
            click("分析报告")
            find("此范围暂无分析报告。")
            assertTrue(repo.state.value.replies.none { it.text("requestId") == id })
            assertEquals(originalRead.id, repo.state.value.intents.last { it.command == "list_reports" }.id)
            capture("report-publication", "awaiting")

            try {
                ready.writeText("Catalogue is visible before the model response")
                val replied = confirmed("manual reply saved") { state -> state.replies.any { it.text("requestId") == id } }
                    .replies.single { it.text("requestId") == id }
                assertEquals(replied.text("errorCode"), "completed", replied.text("status"))
                val catalogue = confirmed("automatic directory refresh after the manual report publication") { state ->
                    state.intents.any { it.command == "list_reports" && it.id != originalRead.id && it.stage == DeliveryStage.APPLIED &&
                        it.result?.objects("items").orEmpty().any { header -> header.text("reportId") == id } }
                }
                val event = requireNotNull(catalogue.reads["analysis_report"])
                assertEquals(id, event.obj("header")?.text("reportId"))
                assertEquals("manual", event.obj("header")?.text("origin"))
                assertEquals(JsonNull, event["notifyUntil"])
                assertTrue(catalogue.reportNotices.isEmpty())
                assertTrue("No automatic report-detail query should have run", catalogue.intents.none { it.command == "get_report" })
                find("目录发布已核对隔离持仓。", seek = true, partial = true)
                capture("report-publication", "updated")
                click("查看报告", seek = true)
                find("报告详情")
                val detail = confirmed("catalogue opens the same report with body and source") { state ->
                    state.intents.any { it.command == "get_report" && it.stage == DeliveryStage.APPLIED &&
                        it.result?.obj("header")?.text("reportId") == id }
                }.intents.single { it.command == "get_report" && it.stage == DeliveryStage.APPLIED }.result!!
                assertEquals(replied.text("text"), detail.text("text"))
                assertEquals(replied["sources"], detail["sources"])
                find("信息不足 · 目录发布已核对隔离持仓。", seek = true, partial = true)
                capture("report-publication", "detail")
                val source = detail.objects("sources").single()
                click(source.text("name")!!, seek = true)
                find("数据来源")
                find(source.text("ref")!!)
                capture("report-publication", "source")
                assertEquals(baseline, manager.activeNotifications.filter { it.notification.channelId == "tickdesk_analysis_report" }.map { it.key }.toSet())
                assertEquals(listOf(id), repo.state.value.intents.filter { it.command == "chat" }.map { it.id })
            } finally {
                ready.delete()
            }
        }

        suspend fun reportEvidence() {
            val symbol = "300502.SZ"
            suspend fun ask(text: String, fieldName: String = "输入消息", expectedError: String? = null): JsonObject {
                val existing = repo.state.value.replies.mapNotNull { it.text("requestId") }.toSet()
                field(fieldName, text)
                click("发送消息")
                val answer = confirmed("reply for isolated message") { snapshot ->
                    snapshot.replies.any { it.text("requestId") !in existing }
                }.replies.single { it.text("requestId") !in existing }
                assertEquals(expectedError, answer.text("errorCode"))
                assertEquals(if (expectedError == null) "completed" else "failed", answer.text("status"))
                return answer
            }
            suspend fun catalogue() {
                assertTrue(tap(find("分析报告", partial = true) { it.isClickable || it.parent?.isClickable == true }))
                find("刷新报告目录")
            }
            suspend fun reportRead(id: String, expected: String): JsonObject = confirmed("report $expected") { projection ->
                projection.intents.lastOrNull { it.command == "get_report" && it.data.text("reportId") == id }
                    ?.result?.obj("header")?.text("state") == expected
            }.intents.last { it.command == "get_report" && it.data.text("reportId") == id }.result!!

            adjustReportFixtureHolding(symbol, 100)
            tab("持仓")
            val beforeChat = repo.state.value.intents.filter { it.command == "chat" }.map { it.id }
            click("量化与 AI", seek = true)
            find("/分析 $symbol")
            assertEquals("Opening a draft must never submit a model request", beforeChat,
                repo.state.value.intents.filter { it.command == "chat" }.map { it.id })
            val savedDraft = "/分析 $symbol 核对仓位"
            field("输入消息", savedDraft)
            find(savedDraft)
            capture("analysis-entry", "holding-draft")
            assertTrue(automation.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK))
            find("当前持仓明细", seek = true, partial = true)
            click("量化与 AI", seek = true)
            find(savedDraft)
            assertEquals("Returning to a saved draft must not submit", beforeChat,
                repo.state.value.intents.filter { it.command == "chat" }.map { it.id })
            val original = ask("/分析 $symbol")
            val reportId = original.text("requestId")!!
            assertEquals(setOf("positions", "tasks"), original.objects("attachments").map { it.text("type") }.toSet())
            click("查看图表附件与来源 →", seek = true)
            find("持仓 100 · 可用 100", seek = true, partial = true)
            find("持仓快照", seek = true, partial = true)
            click("查看 新易盛 事项详情", seek = true)
            find("test://suspension", seek = true, partial = true)
            click("事项")
            click("查看 $symbol 日 K", seek = true)
            find("证券行情")
            find("K 线来源", seek = true, partial = true)
            click("返回")
            find("持仓明细", seek = true)
            click("返回普通对话")
            find("输入消息")
            assertTrue(automation.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK))
            find("当前持仓明细", seek = true, partial = true)
            tab("对话")

            catalogue()
            find("当前报告", seek = true, partial = true)
            click("查看报告", seek = true)
            find("报告详情")
            val detail = reportRead(reportId, "current")
            assertEquals(2, detail.objects("sources").size)
            find("证据截至", seek = true, partial = true)
            assertTrue(tap(find("本地positions快照", seek = true, partial = true)))
            find("数据来源")
            click("关闭")

            adjustReportFixtureHolding(symbol, 200)
            click("刷新报告状态", seek = true)
            val historicalReport = reportRead(reportId, "historical")
            assertEquals(detail.obj("header")!!.number("asOf"), historicalReport.obj("header")!!.number("asOf"))
            find("历史报告", seek = true, partial = true)
            click("按原报告追问")
            val previous = ask("请核对原数量", "继续追问")
            assertTrue(previous.text("text")!!.contains("依据原报告冻结证据"))
            find("持仓数量 100 股", seek = true, partial = true)
            click("按当前情况追问")
            val current = ask("请核对最新数量", "继续追问")
            assertTrue(current.text("text")!!.contains("当前数量已核对"))
            find("持仓数量 200 股", seek = true, partial = true)
            ask("联调无效来源", "继续追问", "AI_UNVERIFIED_SOURCE")
            find("答复失败", seek = true, partial = true)
            click("返回")
            click("返回")
            val second = ask("/分析 $symbol")
            assertNotEquals(reportId, second.text("requestId"))
            catalogue()
            click("刷新报告目录")
            find("新档持仓与事项已核对。", seek = true, partial = true)
            find("旧档持仓与事项已核对。", seek = true, partial = true)
        }

        suspend fun twoWorkspaceRounds() {
            val pair = repo.state.value.pairId!!
            val epoch = repo.state.value.epoch!!
            val fillTime = ZonedDateTime.now(ZoneId.of("Asia/Shanghai")).minusMinutes(1)
            val date = fillTime.toLocalDate().toString()
            val hour = fillTime.format(DateTimeFormatter.ofPattern("HH:mm"))
            val executionIds = mutableSetOf<String>()
            val reportIds = mutableSetOf<String>()
            var priorSearch = "证券代码或名称"
            var priorRevision = repo.state.value.meta!!.number("positionRevision")!!

            suspend fun action(label: String, seek: Boolean = false, partial: Boolean = false) {
                val control = find(label, seek, partial) { node ->
                    generateSequence(node) { it.parent }.take(4).any { it.isClickable && it.isEnabled }
                }
                assertTrue("Use $label", tap(control))
            }
            suspend fun replyAfter(submit: suspend () -> Unit): JsonObject {
                val previous = repo.state.value.replies.mapNotNull { it.text("requestId") }.toSet()
                submit()
                val reply = confirmed("new round reply") { snapshot -> snapshot.replies.any { it.text("requestId") !in previous } }
                    .replies.single { it.text("requestId") !in previous }
                assertEquals(reply.text("errorCode"), "completed", reply.text("status"))
                return reply
            }

            for ((round, symbol) in listOf("300502.SZ", "600000.SH").withIndex()) {
                val name = if (round == 0) "新易盛" else "浦发银行"
                val qty = if (round == 0) 100L else 200L
                val price = if (round == 0) "10.20" else "20.30"
                val query = if (round == 0) name else "600000"
                tab("选股")
                action("搜索证券 / 添加关注", seek = true)
                field(priorSearch, query, seek = true)
                priorSearch = query
                action("查询证券", seek = true)
                find(name, seek = true)
                action("加入关注", seek = true)
                confirmed("round ${round + 1} watch $symbol") { it.records["watchlist"]?.any { row -> row.text("symbol") == symbol } == true }
                action("返回")
                action("立即扫描全市场", seek = true)
                val created = confirmed("round ${round + 1} task $symbol") { projection ->
                    projection.records["tasks"]?.any { it.text("symbol") == symbol && it.text("status") == "pending" } == true
                }.records.getValue("tasks").single { it.text("symbol") == symbol }
                val taskId = created.text("taskId")!!
                assertEquals("buy", created.text("direction"))
                find("全量完成", seek = true, partial = true)

                tab("对话")
                val report = replyAfter {
                    field("输入消息", "/分析 $symbol")
                    action("发送消息")
                }
                assertTrue(reportIds.add(report.text("requestId")!!))
                assertTrue(report.objects("attachments").any { it.text("type") == "tasks" && it.stringList("ids") == listOf(taskId) })
                find("已核对本轮事项依据", seek = true, partial = true)

                tab("事项")
                action(name, seek = true, partial = true)
                find("刷新事项")
                action("回填", seek = true, partial = true)
                find("成交台账录入")
                field("实际成交价", price, seek = true)
                field("实际数量", qty.toString(), seek = true)
                field("上海日期 YYYY-MM-DD", date, seek = true)
                field("上海时间 HH:mm", hour, seek = true)
                find(date, seek = true)
                find(hour, seek = true)
                action("保存成交记录 · 非下单", seek = true)
                find("核对成交记录", seek = true, partial = true)
                action("核对无误", partial = true)
                val execution = confirmed("round ${round + 1} WSS persisted fill") {
                    it.records["executions"]?.any { entry -> entry.text("taskId") == taskId } == true
                }.records.getValue("executions").single { it.text("taskId") == taskId }
                val executionId = execution.text("executionId")!!
                assertTrue(executionIds.add(executionId))
                assertEquals(qty, execution.number("qty"))
                assertEquals("needs_reconciliation", execution.text("positionEffect"))

                tab("持仓")
                find("成交待对账", seek = true)
                action("$name · $symbol · 1 笔待对账", seek = true)
                find("成交对账 · $name")
                field("更正后数量", qty.toString(), seek = true)
                field("可用数量（留空为未知）", "0", seek = true)
                field("平均成本", price, seek = true)
                action("买入 $qty 股", seek = true, partial = true)
                action("确认成交对账", seek = true)
                val reconciled = confirmed("round ${round + 1} authoritative snapshot after reconciliation") { projection ->
                    projection.records["executions"]?.any { it.text("executionId") == executionId && it.text("positionEffect") == "reconciled" } == true &&
                        projection.records["positions"]?.any { it.text("symbol") == symbol && it.text("name") == name && it.number("qty") == qty } == true
                }
                val revision = reconciled.meta!!.number("positionRevision")!!
                assertTrue(revision > priorRevision)
                priorRevision = revision
                assertEquals(pair, reconciled.pairId)
                assertEquals(epoch, reconciled.epoch)
                assertEquals(round + 1, reconciled.records.getValue("positions").size)
                find("当前持仓明细", seek = true, partial = true)
                find(name, seek = true)
                find("$qty / 0", seek = true)
                capture("scene-20", "ledger-after-round-${round + 1}")

                tab("对话")
                replyAfter { action("今日复盘") }
                find("本轮按已确认台账完成复盘", seek = true, partial = true)
            }
            assertEquals(2, executionIds.size)
            assertEquals(2, reportIds.size)
        }

        suspend fun historyCoverage() {
            val args = InstrumentationRegistry.getArguments()
            val expectedSource = args.getString("historySource") ?: "isolated_history_provider"
            val expectedObserved = args.getString("historyObserved")?.toLong() ?: 372L
            val expectedSymbols = args.getString("historyExpected")?.toLong() ?: 500L
            val pair = repo.state.value.pairId!!
            val epoch = repo.state.value.epoch!!
            repo.requestAuxiliary("get_status", pair)
            confirmed("history coverage capability") {
                "get_history_coverage" in it.reads["get_status"]?.obj("protocol")?.stringList("supportedCommands").orEmpty()
            }
            fun history(projection: WorkbenchProjection): IntentView? = projection.intents.lastOrNull {
                it.command == "get_history_coverage" && it.data.text("serverEpoch") == epoch
            }
            suspend fun refreshed(after: String? = null) = history(confirmed("history coverage read") { projection ->
                history(projection)?.let { it.id != after && it.stage == DeliveryStage.APPLIED } == true
            })!!

            tab("设置")
            click("工作台服务状态", seek = true)
            find("行情与数据", seek = true)
            find("当前暂无持仓", seek = true)
            var current = refreshed()
            withTimeoutOrNull(60_000) {
                while (current.result?.text("status") == "unavailable") {
                    click("刷新服务状态", seek = true)
                    current = refreshed(current.id)
                    if (current.result?.text("status") == "unavailable") delay(1_000)
                }
                true
            } ?: error("No published history coverage. Last reason ${current.result?.text("reason")}")
            assertEquals("partial", current.result!!.text("status"))
            assertNotNull(current.result!!.number("checkedAt"))
            val coverage = current.result!!.obj("coverage")!!
            val latest = coverage.obj("latestDay")!!
            assertEquals(expectedObserved, latest.number("observed"))
            assertEquals(expectedSymbols, latest.number("expected"))
            find("全市场历史覆盖", seek = true, partial = true)
            find("沪深主板与创业板 · 不复权日线", seek = true)
            find(expectedSource, seek = true, partial = true)
            find("应至 ${coverage.text("expectedThrough")}", seek = true, partial = true)
            find("最近交易日已观察 $expectedObserved / $expectedSymbols", seek = true, partial = true)
            find("缺 ${expectedSymbols - expectedObserved}", seek = true, partial = true)
            capture("history-coverage", "partial")
            val statusId = repo.state.value.intents.lastOrNull { it.command == "get_status" }?.id
            click("刷新服务状态", seek = true)
            val reload = refreshed(current.id)
            assertEquals(coverage.text("source"), reload.result?.obj("coverage")?.text("source"))
            assertEquals(latest, reload.result?.obj("coverage")?.obj("latestDay"))
            assertTrue(reload.result!!.number("checkedAt")!! >= current.result!!.number("checkedAt")!!)
            confirmed("shared refresh also updates service status") { projection ->
                projection.intents.lastOrNull { it.command == "get_status" }
                    ?.let { it.id != statusId && it.stage == DeliveryStage.APPLIED } == true
            }
        }
    }
}
