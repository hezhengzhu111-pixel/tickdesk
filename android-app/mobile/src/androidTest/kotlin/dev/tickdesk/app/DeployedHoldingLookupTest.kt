package dev.tickdesk.app

import android.accessibilityservice.AccessibilityService
import android.graphics.Bitmap
import android.graphics.Rect
import android.os.Bundle
import android.os.SystemClock
import android.view.accessibility.AccessibilityNodeInfo
import android.view.MotionEvent
import androidx.test.platform.app.InstrumentationRegistry
import dev.tickdesk.core.*
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File

/** Explicit opt-in: existing pairing, real Activity and real symbol search; no holdings submission. */
class DeployedHoldingLookupTest {
    @Test fun existingPairShowsDirectPickerAndRealManualSuggestions() = runBlocking {
        assumeTrue(InstrumentationRegistry.getArguments().getString("deployedHoldingLookupTest") == "true")
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val automation = instrumentation.uiAutomation
        val context = instrumentation.targetContext
        launchResumedMainActivity()
        val runtime = (context.applicationContext as TickDeskApplication).runtime
        val pair = requireNotNull(runtime.pairedScope())
        withTimeout(25_000) { runtime.state.first { it.connection.canWrite } }
        val writesBefore = runtime.state.value.intents.filter { it.command in setOf("adjust_position", "register_image") }.map { it.id }.toSet()
        val workflowBefore = runtime.state.value.holdingImport?.id
        fun descendants(root: AccessibilityNodeInfo): Sequence<AccessibilityNodeInfo> = sequence {
            yield(root)
            for (i in 0 until root.childCount) root.getChild(i)?.let { yieldAll(descendants(it)) }
        }
        fun nodes() = automation.rootInActiveWindow?.let { descendants(it).filter { node -> node.isVisibleToUser }.toList() }.orEmpty()
        fun has(label: String) = nodes().any { it.text?.contains(label) == true || it.contentDescription?.contains(label) == true }
        fun await(label: String, timeout: Long = 12_000, condition: () -> Boolean) {
            val deadline = SystemClock.uptimeMillis() + timeout
            while (SystemClock.uptimeMillis() < deadline) {
                if (condition()) return
                SystemClock.sleep(100)
            }
            throw AssertionError("$label; visible=${nodes().map { "${it.text} [${it.contentDescription}]" }}")
        }
        fun click(label: String) {
            if (label == "持仓") {
                await("Portfolio tab missing") { nodes().any { it.text?.toString() == label || it.contentDescription?.toString() == label } }
                val bounds = nodes().filter { it.text?.toString() == label || it.contentDescription?.toString() == label }
                    .map { Rect().also(it::getBoundsInScreen) }.maxBy { it.bottom }
                val down = SystemClock.uptimeMillis()
                val press = MotionEvent.obtain(down, down, MotionEvent.ACTION_DOWN, bounds.exactCenterX(), bounds.exactCenterY(), 0)
                try { assertTrue(automation.injectInputEvent(press, true)) } finally { press.recycle() }
                SystemClock.sleep(60)
                val release = MotionEvent.obtain(down, SystemClock.uptimeMillis(), MotionEvent.ACTION_UP, bounds.exactCenterX(), bounds.exactCenterY(), 0)
                try { assertTrue(automation.injectInputEvent(release, true)) } finally { release.recycle() }
                return
            }
            await("Cannot click $label") {
                nodes().filter { it.text?.toString() == label || it.contentDescription?.toString() == label }.any { found ->
                    generateSequence(found) { it.parent }.any { it.isClickable && it.isEnabled && it.performAction(AccessibilityNodeInfo.ACTION_CLICK) }
                }
            }
        }
        fun scrollForward() = nodes().filter { it.isScrollable }.maxByOrNull {
            val rect = Rect().also(it::getBoundsInScreen); rect.width().toLong() * rect.height()
        }?.performAction(AccessibilityNodeInfo.ACTION_SCROLL_FORWARD)
        fun enterQuery(query: String) {
            await("Search field missing") { nodes().any { it.isEditable } }
            val field = nodes().first { it.isEditable }
            assertTrue(field.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, Bundle().apply {
                putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, query)
            }))
        }
        click("持仓")
        await("Portfolio did not open") { has("本地持仓台账") }
        click("截图导入")
        await("System picker did not open") {
            automation.rootInActiveWindow?.packageName?.toString()?.let { it != context.packageName && it != "com.android.systemui" } == true
        }
        assertTrue(automation.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK))
        await("Picker cancellation left portfolio") { has("本地持仓台账") }
        assertFalse(has("原图保留与外部识别"))
        assertFalse(has("保存原图"))
        assertEquals(workflowBefore, runtime.state.value.holdingImport?.id)
        await("Manual holding action is unreachable") { if (has("手动新增持仓")) true else { scrollForward(); false } }
        click("手动新增持仓")
        await("Manual form did not open") { has("证券名称或代码") }
        enterQuery("平安")
        withTimeout(20_000) { runtime.state.first { state -> state.intents.any {
            it.command == "search_symbols" && it.data.text("query") == "平安" && it.stage == DeliveryStage.APPLIED
        } } }
        await("Name lookup omitted real Ping An Bank") { has("平安银行") && has("000001.SZ") }
        enterQuery("000001")
        withTimeout(20_000) { runtime.state.first { state -> state.intents.any {
            it.command == "search_symbols" && it.data.text("query") == "000001" && it.stage == DeliveryStage.APPLIED
        } } }
        await("Code lookup omitted real Ping An Bank") { has("平安银行") && has("000001.SZ") }
        click("平安银行 · 000001.SZ")
        await("Selecting real suggestion did not bind the form") { has("已选择 平安银行 · 000001.SZ") }
        assertFalse(has("补录原因"))
        val directory = requireNotNull(context.getExternalFilesDir("holdings"))
        assertTrue(directory.isDirectory || directory.mkdirs())
        val image = File(directory, "manual-lookup.png")
        val bitmap = requireNotNull(automation.takeScreenshot())
        try { image.outputStream().use { assertTrue(bitmap.compress(Bitmap.CompressFormat.PNG, 100, it)) } }
        finally { bitmap.recycle() }
        // Click the app's top back action once; global Back can first dismiss the keyboard,
        // and repeated global Back races recomposition and may leave the app entirely.
        await("Top back action missing") { nodes().any { it.text?.toString() == "返回" } }
        val backBounds = nodes().filter { it.text?.toString() == "返回" }
            .map { Rect().also(it::getBoundsInScreen) }.minBy { it.top }
        val backDown = SystemClock.uptimeMillis()
        val backPress = MotionEvent.obtain(backDown, backDown, MotionEvent.ACTION_DOWN, backBounds.exactCenterX(), backBounds.exactCenterY(), 0)
        try { assertTrue(automation.injectInputEvent(backPress, true)) } finally { backPress.recycle() }
        SystemClock.sleep(60)
        val backRelease = MotionEvent.obtain(backDown, SystemClock.uptimeMillis(), MotionEvent.ACTION_UP, backBounds.exactCenterX(), backBounds.exactCenterY(), 0)
        try { assertTrue(automation.injectInputEvent(backRelease, true)) } finally { backRelease.recycle() }
        await("Test must leave the portfolio visible") { has("本地持仓台账") }
        assertEquals(pair, runtime.state.value.pairId)
        assertEquals(writesBefore, runtime.state.value.intents.filter { it.command in setOf("adjust_position", "register_image") }.map { it.id }.toSet())
        assertEquals(workflowBefore, runtime.state.value.holdingImport?.id)
        println("DEPLOYED_HOLDING_LOOKUP_PASS: symbol=000001.SZ name=平安银行 screenshot=${image.absolutePath}")
    }
}
