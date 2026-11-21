package dev.tickdesk.app

import android.os.Bundle
import android.os.SystemClock
import android.view.accessibility.AccessibilityNodeInfo
import androidx.test.platform.app.InstrumentationRegistry
import dev.tickdesk.core.DeliveryStage
import dev.tickdesk.core.ProtocolException
import dev.tickdesk.core.array
import dev.tickdesk.core.number
import dev.tickdesk.core.obj
import dev.tickdesk.core.text
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test

/**
 * Device acceptance for the three independent review views, cursor paging,
 * original execution drill-down and a manually saved review note.
 */
class TradeReviewDeviceTest {
    private val instrument = InstrumentationRegistry.getInstrumentation()
    private val automation = instrument.uiAutomation
    private val context = instrument.targetContext
    private val day = 86_400_000L
    private val symbol = "300502.SZ"
    private val notePrefix = "R06 人工复核留痕验收：亏损片段已核对原始成交"
    private val reviewer = "R06设备验收"

    @Test(timeout = 1_800_000)
    fun reviewViewsPageDrillDownAndSaveNote() = runBlocking(Dispatchers.IO) {
        assumeTrue(InstrumentationRegistry.getArguments().getString("tradeReviewFlow") == "true")
        // Keep the app in the foreground while seeding; a backgrounded process
        // can be frozen and lose its relay lease mid-seed.
        launchResumedMainActivity()
        keepAwake()
        val runtime = (context.applicationContext as TickDeskApplication).runtime
        val seedState = withTimeout(30_000) {
            runtime.state.first { it.canQueue && it.pairId != null && it.epoch != null }
        }
        withTimeout(30_000) {
            runtime.state.first { it.connection.canWrite && it.connection.synced }
        }
        val ready = runtime.state.value
        val pairId = ready.pairId!!
        val epoch = ready.epoch!!
        var review = readReview(runtime, buildJsonObject { put("limit", 50) }, pairId, epoch)
        val summary = review.obj("summary")
        if ((summary?.number("closedTrades") ?: 0) < 51 ||
            (summary?.number("excludedCount") ?: 0) < 3
        ) {
            val now = System.currentTimeMillis()
            execute(runtime, buildJsonObject {
                put("symbol", symbol)
                put("side", "buy")
                put("price", "10")
                put("qty", 51)
                put("fees", "0")
                put("executedAt", now - 40 * day)
            }, pairId, epoch)
            for (index in 0 until 51) {
                val price = if (index == 50) "9" else "11"
                execute(runtime, buildJsonObject {
                    put("symbol", symbol)
                    put("side", "sell")
                    put("price", price)
                    put("qty", 1)
                    put("fees", "0")
                    put("executedAt", now - 30 * day + index * 1_000L)
                }, pairId, epoch)
            }
            repeat(3) { index ->
                execute(runtime, buildJsonObject {
                    put("symbol", "600000.SH")
                    put("side", "sell")
                    put("price", "12")
                    put("qty", 1)
                    put("fees", "0")
                    put("executedAt", now - day + index * 1_000L)
                }, pairId, epoch)
            }
            review = readReview(runtime, buildJsonObject { put("limit", 50) }, pairId, epoch)
        }
        val reviewSummary = review.obj("summary") ?: error("review summary missing: $review")
        assertEquals(51, reviewSummary.number("closedTrades")?.toInt())
        assertEquals(3, reviewSummary.number("excludedCount")?.toInt())
        assertEquals(1, reviewSummary.number("closedSamples")?.toInt())
        val firstPageItems = review.array("items").orEmpty()
        assertEquals(50, firstPageItems.size)
        val firstItem = firstPageItems.firstOrNull() as? JsonObject ?: error("first item missing")
        val buyId = firstItem.text("buyExecutionId") ?: error("first item buy missing")
        val firstSellId = firstItem.text("sellExecutionId") ?: error("first item sell missing")
        assertEquals("1", firstItem.text("netPnl"))
        val cursor = review.text("nextCursor") ?: error("page cursor missing")
        val secondPage = readReview(runtime, buildJsonObject {
            put("limit", 50)
            put("cursor", cursor)
        }, pairId, epoch)
        val secondPageItems = secondPage.array("items").orEmpty()
        assertEquals(1, secondPageItems.size)
        val lossItem = secondPageItems.firstOrNull() as? JsonObject ?: error("loss item missing")
        assertEquals("-1", lossItem.text("netPnl"))
        val lossSellId = lossItem.text("sellExecutionId") ?: error("loss sell missing")

        click("持仓")
        click("成交复盘")
        waitForNode("已闭合配对片段 51 · 净额口径 51 笔 · 盈 50 · 亏 1 · 平 0", 60_000)
        stage("page1 summary visible")
        scrollTo("记录价差 1 · 净额 1", down = false)
        waitForNode("记录价差 1 · 净额 1", 30_000)
        scrollTo("买 $buyId · 卖 $firstSellId", down = false)
        waitForNode("买 $buyId · 卖 $firstSellId")
        stage("page1 original pair visible")

        var before = runtime.state.value.intents.map { it.id }.toSet()
        scrollTo("下一页", down = true)
        click("下一页")
        stage("clicked next page")
        val pageTwo = awaitReview(runtime, before)
        stage("page2 applied items=${pageTwo.array("items")?.size}")
        assertEquals(1, pageTwo.array("items")?.size)
        waitForNode("当前为游标续页；样本与排除清单只在第一页展示。", 30_000)
        scrollTo("记录价差 -1 · 净额 -1", down = false)
        waitForNode("记录价差 -1 · 净额 -1", 30_000)
        scrollTo("买 $buyId · 卖 $lossSellId", down = false)
        waitForNode("买 $buyId · 卖 $lossSellId")
        stage("page2 loss original pair visible")

        val noteText = "$notePrefix ${System.currentTimeMillis()}"
        val noteBefore = runtime.state.value.intents.map { it.id }.toSet()
        scrollTo("复核说明（必填）", down = false)
        val noteField = editableNear("复核说明（必填）")
            ?: editableFields().firstOrNull { !it.isFocused && it.text.isNullOrEmpty() }
            ?: editableFields().first()
        setText(noteField, noteText)
        waitForSubstring(noteText.take(12), 5_000)
        val reviewerField = editableNear("复核人（本机记录，不是券商身份）")
            ?: editableFields().firstOrNull { it.text?.toString() == "本机用户" }
            ?: editableFields().last()
        setText(reviewerField, reviewer)
        waitForSubstring(reviewer, 5_000)
        stage("note text set")
        var saved: dev.tickdesk.core.WorkbenchProjection? = null
        for (attempt in 0 until 3) {
            withTimeout(30_000) {
                runtime.state.first { it.connection.canWrite && it.connection.synced }
            }
            click("保存人工复核")
            stage("clicked save note attempt=$attempt")
            val appeared = withTimeoutOrNull(20_000) {
                runtime.state.first { current ->
                    current.intents.any {
                        it.command == "record_trade_review_note" && it.id !in noteBefore
                    }
                }
            }
            if (appeared == null) continue
            saved = withTimeout(90_000) {
                runtime.state.first { current ->
                    current.intents.any {
                        it.command == "record_trade_review_note" && it.id !in noteBefore &&
                            it.stage in setOf(
                                DeliveryStage.APPLIED, DeliveryStage.REJECTED,
                                DeliveryStage.QUARANTINED, DeliveryStage.NEEDS_RECONCILIATION,
                            )
                    }
                }
            }
            break
        }
        val finalSaved = checkNotNull(saved) { "save note never became a queued intent" }
        val noteIntent = finalSaved.intents.last { it.command == "record_trade_review_note" }
        assertEquals("note rejected: ${noteIntent.errorCode}", DeliveryStage.APPLIED, noteIntent.stage)
        stage("note applied")
        withTimeout(60_000) {
            runtime.state.first { current ->
                current.reads["get_trade_review_notes"]?.array("items")
                    ?.any { element -> element is JsonObject && element.text("note") == noteText } == true
            }
        }
        stage("notes read contains text")

        before = runtime.state.value.intents.map { it.id }.toSet()
        scrollTo("完全平仓样本", down = true)
        click("完全平仓样本")
        val samples = awaitReview(runtime, before)
        assertEquals(1, samples.array("samples")?.size)
        stage("samples view applied")
        scrollTo("完全平仓入场组", down = false)

        before = runtime.state.value.intents.map { it.id }.toSet()
        scrollTo("未配对排除", down = true)
        click("未配对排除")
        val exclusions = awaitReview(runtime, before)
        assertEquals(3, exclusions.array("exclusions")?.size)
        stage("exclusions view applied")
        scrollTo("未计入配对的成交", down = false)

        before = runtime.state.value.intents.map { it.id }.toSet()
        scrollTo("配对成交", down = true)
        click("配对成交")
        awaitReview(runtime, before)
        scrollTo("人工复核记录", down = true)
        waitForNode(noteText, 30_000)
        waitForSubstring(reviewer, 30_000)

        val visible = visibleTexts()
        val evidence = buildString {
            appendLine("buy=$buyId lossSell=$lossSellId")
            appendLine("items=51 page2=${pageTwo.array("items")?.size}")
            appendLine("samples=${samples.array("samples")?.size} exclusions=${exclusions.array("exclusions")?.size}")
            appendLine("noteStage=${noteIntent.stage} note=$noteText reviewer=$reviewer")
            appendLine(visible.joinToString(" | "))
        }
        java.io.File(context.filesDir, "r06-flow.txt").writeText(evidence)
    }

    @Test(timeout = 300_000)
    fun restartShowsSavedManualReview() = runBlocking(Dispatchers.IO) {
        assumeTrue(InstrumentationRegistry.getArguments().getString("tradeReviewRestart") == "true")
        launchResumedMainActivity()
        keepAwake()
        click("持仓")
        click("成交复盘")
        val runtime = (context.applicationContext as TickDeskApplication).runtime
        val notesState = withTimeout(60_000) {
            runtime.state.first { current ->
                current.reads["get_trade_review_notes"]?.array("items")?.any { element ->
                    element is JsonObject && element.text("reviewer") == reviewer &&
                        element.text("status") == "active" &&
                        element.text("note")?.startsWith(notePrefix) == true
                } == true
            }
        }
        val noteText = notesState.reads["get_trade_review_notes"]!!
            .array("items")!!
            .mapNotNull { element -> element as? JsonObject }
            .first { note ->
                note.text("reviewer") == reviewer && note.text("status") == "active" &&
                    note.text("note")?.startsWith(notePrefix) == true
            }
            .text("note")!!
        scrollTo("人工复核记录", down = false)
        waitForNode(noteText, 30_000)
        waitForSubstring(reviewer, 30_000)
        val visible = visibleTexts()
        java.io.File(context.filesDir, "r06-restart.txt").writeText(
            "note=$noteText reviewer=$reviewer\n" + visible.joinToString(" | ")
        )
    }

    private suspend fun readReview(
        runtime: WorkbenchRuntime,
        fields: JsonObject,
        pairId: String,
        epoch: String,
    ): JsonObject {
        val id = withCommandReady {
            runtime.enqueue("get_trade_review", fields, pairId, epoch)
        }
        val state = withTimeout(60_000) {
            runtime.state.first { current ->
                current.intents.any {
                    it.id == id && it.stage in setOf(
                        DeliveryStage.APPLIED, DeliveryStage.REJECTED,
                        DeliveryStage.QUARANTINED, DeliveryStage.NEEDS_RECONCILIATION,
                    )
                }
            }
        }
        val intent = state.intents.single { it.id == id }
        assertEquals("get_trade_review rejected: ${intent.errorCode}", DeliveryStage.APPLIED, intent.stage)
        return intent.result ?: error("get_trade_review result missing")
    }

    private suspend fun execute(
        runtime: WorkbenchRuntime,
        body: JsonObject,
        pairId: String,
        epoch: String,
    ): String {
        val id = withCommandReady {
            runtime.enqueue("record_execution", body, pairId, epoch)
        }
        val state = withTimeout(240_000) {
            runtime.state.first { current ->
                current.intents.any {
                    it.id == id && it.stage in setOf(
                        DeliveryStage.APPLIED, DeliveryStage.REJECTED,
                        DeliveryStage.QUARANTINED, DeliveryStage.NEEDS_RECONCILIATION,
                    )
                }
            }
        }
        val intent = state.intents.single { it.id == id }
        assertEquals("record_execution rejected: ${intent.errorCode}", DeliveryStage.APPLIED, intent.stage)
        return intent.result?.obj("execution")?.text("executionId")
            ?: error("record_execution result missing executionId: ${intent.result}")
    }

    private suspend fun awaitReview(runtime: WorkbenchRuntime, before: Set<String>): JsonObject {
        val state = withTimeout(60_000) {
            runtime.state.first { current ->
                current.intents.any {
                    it.command == "get_trade_review" && it.id !in before &&
                        it.stage == DeliveryStage.APPLIED
                }
            }
        }
        return state.intents.last {
            it.command == "get_trade_review" && it.id !in before
        }.result ?: error("trade review result missing")
    }

    private suspend fun <T> withCommandReady(block: suspend () -> T): T {
        var last: ProtocolException? = null
        repeat(40) {
            try {
                return block()
            } catch (error: ProtocolException) {
                last = error
                delay(500)
            }
        }
        throw last ?: IllegalStateException("command was never ready")
    }

    private fun stage(message: String) {
        android.util.Log.i("R06Device", message)
        println("R06Device: $message")
    }

    private fun keepAwake() {
        android.os.ParcelFileDescriptor.AutoCloseInputStream(
            automation.executeShellCommand("svc power stayon true")
        ).use { it.readBytes() }
        android.os.ParcelFileDescriptor.AutoCloseInputStream(
            automation.executeShellCommand("input keyevent KEYCODE_WAKEUP")
        ).use { it.readBytes() }
    }

    private fun editableNear(label: String): AccessibilityNodeInfo? {
        var anchor: AccessibilityNodeInfo? = find(label) ?: return null
        repeat(5) {
            val node = anchor ?: return null
            descendants(node).firstOrNull { it.isEditable }?.let { return it }
            anchor = node.parent
        }
        return null
    }

    private fun editableFields(): List<AccessibilityNodeInfo> {
        if (android.os.Build.VERSION.SDK_INT >= 34) automation.clearCache()
        val root = automation.rootInActiveWindow ?: return emptyList()
        return descendants(root).filter { it.isVisibleToUser && it.isEditable }.toList()
    }

    private fun visibleTexts(): List<String> {
        if (android.os.Build.VERSION.SDK_INT >= 34) automation.clearCache()
        return automation.rootInActiveWindow
            ?.let { descendants(it).mapNotNull { node -> node.text?.toString() }.toList() }
            .orEmpty()
    }

    private fun click(label: String) {
        scrollTo(label)
        val found = waitForNode(label)
        val clicked = generateSequence(found) { it.parent }.any {
            it.performAction(AccessibilityNodeInfo.ACTION_CLICK)
        }
        if (!clicked) {
            val rect = android.graphics.Rect()
            found.getBoundsInScreen(rect)
            val down = SystemClock.uptimeMillis()
            for (action in listOf(android.view.MotionEvent.ACTION_DOWN, android.view.MotionEvent.ACTION_UP)) {
                val event = android.view.MotionEvent.obtain(
                    down, SystemClock.uptimeMillis(), action,
                    rect.exactCenterX(), rect.exactCenterY(), 0,
                )
                try {
                    automation.injectInputEvent(event, true)
                } finally {
                    event.recycle()
                }
            }
        }
        automation.waitForIdle(150, 2_000)
    }

    private fun setText(node: AccessibilityNodeInfo, text: String) {
        val arguments = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        }
        check(generateSequence(node) { it.parent }.any {
            it.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, arguments)
        }) { "field is not editable" }
        SystemClock.sleep(250)
    }

    private fun scrollTo(label: String, down: Boolean = false) {
        repeat(10) {
            if (find(label) != null) return
            val command = if (down) {
                "input swipe 672 800 672 2300 250"
            } else {
                "input swipe 672 2300 672 800 250"
            }
            android.os.ParcelFileDescriptor.AutoCloseInputStream(
                automation.executeShellCommand(command)
            ).use { it.readBytes() }
            SystemClock.sleep(250)
        }
    }

    private fun waitForSubstring(needle: String, timeoutMs: Long = 10_000): AccessibilityNodeInfo {
        val deadline = SystemClock.uptimeMillis() + timeoutMs
        while (SystemClock.uptimeMillis() < deadline) {
            findContaining(needle)?.let { return it }
            SystemClock.sleep(150)
        }
        val visible = visibleTexts().take(60)
        throw AssertionError("Expected visible text containing '$needle'. Found $visible")
    }

    private fun findContaining(needle: String): AccessibilityNodeInfo? {
        if (android.os.Build.VERSION.SDK_INT >= 34) automation.clearCache()
        return automation.rootInActiveWindow?.let { root ->
            descendants(root).firstOrNull {
                it.isVisibleToUser && it.text?.toString()?.contains(needle) == true
            }
        }
    }

    private fun waitForNode(label: String, timeoutMs: Long = 10_000): AccessibilityNodeInfo {
        val deadline = SystemClock.uptimeMillis() + timeoutMs
        while (SystemClock.uptimeMillis() < deadline) {
            find(label)?.let { return it }
            SystemClock.sleep(150)
        }
        val visible = visibleTexts().take(60)
        throw AssertionError("Expected visible '$label'. Found $visible")
    }

    private fun find(label: String): AccessibilityNodeInfo? {
        if (android.os.Build.VERSION.SDK_INT >= 34) automation.clearCache()
        return automation.rootInActiveWindow?.let { root ->
            descendants(root).firstOrNull {
                it.isVisibleToUser && (it.text?.toString() == label || it.contentDescription?.toString() == label)
            }
        }
    }

    private fun descendants(root: AccessibilityNodeInfo): Sequence<AccessibilityNodeInfo> = sequence {
        yield(root)
        for (index in 0 until root.childCount) {
            root.getChild(index)?.let { yieldAll(descendants(it)) }
        }
    }
}
