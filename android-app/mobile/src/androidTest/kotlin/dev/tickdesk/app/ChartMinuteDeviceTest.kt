package dev.tickdesk.app

import android.os.Bundle
import android.os.SystemClock
import android.view.accessibility.AccessibilityNodeInfo
import androidx.test.platform.app.InstrumentationRegistry
import dev.tickdesk.core.DeliveryStage
import dev.tickdesk.core.IntentView
import dev.tickdesk.core.ProtocolException
import dev.tickdesk.core.array
import dev.tickdesk.core.flag
import dev.tickdesk.core.number
import dev.tickdesk.core.obj
import dev.tickdesk.core.text
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test

/**
 * Four-component minute-chain acceptance: Python synthetic kernel -> Rust local
 * service -> TLS relay -> this APK. The fixture carries one duplicate minute
 * and an unclosed last bar.
 */
class ChartMinuteDeviceTest {
    private val instrument = InstrumentationRegistry.getInstrumentation()
    private val automation = instrument.uiAutomation
    private val context = instrument.targetContext
    private val symbol = "300502.SZ"

    @Test(timeout = 600_000)
    fun minuteChartShowsPartialQualityAndPagesThroughRelay() = runBlocking(Dispatchers.IO) {
        assumeTrue(InstrumentationRegistry.getArguments().getString("chartMinute") == "true")
        launchResumedMainActivity()
        keepAwake()
        val runtime = (context.applicationContext as TickDeskApplication).runtime
        val ready = withTimeout(30_000) {
            runtime.state.first {
                it.canQueue && it.connection.canWrite && it.connection.synced &&
                    it.pairId != null && it.epoch != null
            }
        }
        val pairId = ready.pairId!!
        val epoch = ready.epoch!!
        if (ready.records["positions"].orEmpty().none { it.text("symbol") == symbol }) {
            execute(runtime, "reconcile_holding", buildJsonObject {
                put("symbol", symbol)
                put("basePositionRevision", 0)
                put("qty", 600)
                put("cost", "12")
                put("asOf", System.currentTimeMillis())
                put("reason", "chart fixture holding")
                put("resolvedExecutionIds", JsonArray(emptyList()))
                put("partitions", JsonArray(listOf(
                    buildJsonObject {
                        put("qty", 600)
                        put("restrictions", JsonArray(emptyList()))
                    },
                )))
            }, pairId, epoch)
        }
        click("持仓")
        scrollTo("查看 K 线", down = false)
        click("查看 K 线")
        waitForNode(symbol, 30_000)

        var before = runtime.state.value.intents.map { it.id }.toSet()
        click("1分")
        val oneMinute = awaitChart(runtime, before)
        val oneCandles = oneMinute.array("candles").orEmpty()
        assertEquals(20, oneCandles.size)
        val oneLast = oneCandles.last() as JsonObject
        assertEquals(false, oneLast.flag("complete") ?: true)
        assertTrue(oneLast.number("availableAtMs") == null)
        assertTrue(oneMinute.text("nextCursor") != null)
        scrollTo("最后一根尚未完结，仅显示已落盘的片段。", down = false)
        waitForNode("最后一根尚未完结，仅显示已落盘的片段。", 30_000)

        before = runtime.state.value.intents.map { it.id }.toSet()
        scrollTo("下一段1分", down = false)
        click("下一段1分")
        val older = awaitChart(runtime, before)
        val olderCandles = older.array("candles").orEmpty()
        assertEquals(9, olderCandles.size)
        val duplicate = olderCandles.mapNotNull { it as? JsonObject }
            .first { it.text("quality") == "duplicate_minute" }
        assertEquals(false, duplicate.flag("complete") ?: true)
        assertTrue(duplicate.number("availableAtMs") == null)
        assertTrue(older.text("nextCursor") == null)
        scrollTo("部分分钟存在重复或缺口，已标为未完整，不冒充终态。", down = true)
        waitForNode("部分分钟存在重复或缺口，已标为未完整，不冒充终态。", 30_000)

        before = runtime.state.value.intents.map { it.id }.toSet()
        scrollTo("返回最新", down = true)
        click("返回最新")
        awaitChart(runtime, before)

        before = runtime.state.value.intents.map { it.id }.toSet()
        scrollTo("5分", down = true)
        click("5分")
        val fiveMinute = awaitChart(runtime, before)
        val fiveCandles = fiveMinute.array("candles").orEmpty()
        assertEquals(6, fiveCandles.size)
        assertTrue(fiveCandles.mapNotNull { it as? JsonObject }
            .any { it.text("quality") == "duplicate_minute" })
        assertTrue(fiveCandles.mapNotNull { it as? JsonObject }
            .any { it.text("quality") == "incomplete_group" })
        assertTrue(fiveCandles.mapNotNull { it as? JsonObject }
            .filter { it.text("quality") != "ok" }
            .all { it.flag("complete") == false && it.number("availableAtMs") == null })
        scrollTo("部分分钟存在重复或缺口，已标为未完整，不冒充终态。", down = true)
        waitForNode("部分分钟存在重复或缺口，已标为未完整，不冒充终态。", 30_000)

        val expired = chartCommand(runtime, buildJsonObject {
            put("symbol", symbol)
            put("adjustment", "none")
            put("timeframe", "1m")
            put("limit", 5)
            put("cursor", "v2:1:20:deadbeef")
        }, pairId, epoch)
        assertEquals(DeliveryStage.REJECTED, expired.stage)
        assertEquals("CHART_CURSOR_EXPIRED", expired.errorCode)

        val visible = visibleTexts()
        val evidence = buildString {
            appendLine("oneMinute=${oneCandles.size} lastComplete=${oneLast.flag("complete")} duplicateQuality=${duplicate.text("quality")}")
            appendLine("older=${olderCandles.size} hasDuplicate=${olderCandles.mapNotNull { it as? JsonObject }.any { it.text("quality") == "duplicate_minute" }}")
            appendLine("fiveMinute=${fiveCandles.size} qualities=${fiveCandles.mapNotNull { it as? JsonObject }.map { it.text("quality") }}")
            appendLine("expiredStage=${expired.stage} expiredCode=${expired.errorCode}")
            appendLine(visible.joinToString(" | "))
        }
        java.io.File(context.filesDir, "r05-chart.txt").writeText(evidence)
    }

    private suspend fun chartCommand(
        runtime: WorkbenchRuntime,
        fields: JsonObject,
        pairId: String,
        epoch: String,
    ): IntentView {
        val id = withCommandReady {
            runtime.enqueue("get_chart", fields, pairId, epoch)
        }
        val state = withTimeout(90_000) {
            runtime.state.first { current ->
                current.intents.any {
                    it.id == id && it.stage in setOf(
                        DeliveryStage.APPLIED, DeliveryStage.REJECTED,
                        DeliveryStage.QUARANTINED, DeliveryStage.NEEDS_RECONCILIATION,
                    )
                }
            }
        }
        return state.intents.single { it.id == id }
    }

    private suspend fun awaitChart(runtime: WorkbenchRuntime, before: Set<String>): JsonObject {
        val state = withTimeout(90_000) {
            runtime.state.first { current ->
                current.intents.any {
                    it.command == "get_chart" && it.id !in before &&
                        it.stage == DeliveryStage.APPLIED
                }
            }
        }
        return state.intents.last {
            it.command == "get_chart" && it.id !in before
        }.result ?: error("get_chart result missing")
    }

    private suspend fun execute(
        runtime: WorkbenchRuntime,
        command: String,
        body: JsonObject,
        pairId: String,
        epoch: String,
    ): IntentView {
        val id = withCommandReady { runtime.enqueue(command, body, pairId, epoch) }
        val state = withTimeout(90_000) {
            runtime.state.first { current ->
                current.intents.any {
                    it.id == id && it.stage in setOf(
                        DeliveryStage.APPLIED, DeliveryStage.REJECTED,
                        DeliveryStage.QUARANTINED, DeliveryStage.NEEDS_RECONCILIATION,
                    )
                }
            }
        }
        return state.intents.single { it.id == id }
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

    private fun keepAwake() {
        android.os.ParcelFileDescriptor.AutoCloseInputStream(
            automation.executeShellCommand("svc power stayon true")
        ).use { it.readBytes() }
        android.os.ParcelFileDescriptor.AutoCloseInputStream(
            automation.executeShellCommand("input keyevent KEYCODE_WAKEUP")
        ).use { it.readBytes() }
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

    private fun scrollTo(label: String, down: Boolean = false) {
        repeat(12) {
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

    private fun waitForNode(label: String, timeoutMs: Long = 10_000): AccessibilityNodeInfo {
        val deadline = SystemClock.uptimeMillis() + timeoutMs
        while (SystemClock.uptimeMillis() < deadline) {
            find(label)?.let { return it }
            SystemClock.sleep(150)
        }
        throw AssertionError("Expected visible '$label'. Found ${visibleTexts().take(60)}")
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
