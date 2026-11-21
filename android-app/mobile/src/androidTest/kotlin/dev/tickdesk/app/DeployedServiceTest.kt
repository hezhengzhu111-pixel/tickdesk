package dev.tickdesk.app

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Rect
import android.os.SystemClock
import android.view.MotionEvent
import android.view.accessibility.AccessibilityNodeInfo
import androidx.test.platform.app.InstrumentationRegistry
import dev.tickdesk.core.*
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File

/** Existing paired workbench: no fixtures/settings changes; research scan requires an explicit flag. */
class DeployedServiceTest {
    @Test fun existingPairReadsNonemptyQuantResearchWithKnownExit() = runBlocking {
        assumeTrue(InstrumentationRegistry.getArguments().getString("quantResearchTest") == "true")
        launchResumedMainActivity()
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val runtime = (context.applicationContext as TickDeskApplication).runtime
        val pair = requireNotNull(runtime.pairedScope())
        runtime.withReminderConnection {
            withTimeout(20_000) { runtime.state.first { it.connection.canWrite } }
            suspend fun read(command: String, cursor: String? = null): kotlinx.serialization.json.JsonObject {
                val id = runtime.enqueue(command, buildJsonObject { cursor?.let { put("cursor", it) } })
                val result = withTimeout(15_000) {
                    runtime.state.first { state -> state.intents.any {
                        it.id == id && it.stage in setOf(DeliveryStage.APPLIED, DeliveryStage.REJECTED)
                    } }
                }.intents.single { it.id == id }
                assertEquals("$command ${result.errorCode}", DeliveryStage.APPLIED, result.stage)
                return requireNotNull(result.result)
            }
            val supported = read("get_status").obj("protocol")!!.array("supportedCommands")!!
            assertTrue(supported.any { it.toString() == "\"get_quant_observations\"" })
            if (InstrumentationRegistry.getArguments().getString("quantResearchScan") == "true") {
                val id = runtime.enqueue("scan_now", buildJsonObject {})
                val result = withTimeout(20_000) {
                    runtime.state.first { state -> state.intents.any {
                        it.id == id && it.stage in setOf(DeliveryStage.APPLIED, DeliveryStage.REJECTED)
                    } }
                }.intents.single { it.id == id }
                assertTrue("Unexpected scan rejection: ${result.errorCode}",
                    result.stage == DeliveryStage.APPLIED || result.errorCode in setOf("DATA_UNAVAILABLE", "SCAN_OUTSIDE_WINDOW"))
                println("QUANT_SCAN_RESULT: ${result.stage} ${result.errorCode}")
            }
            var page = read("get_quant_observations")
            assertEquals("available", page.text("status"))
            assertNotNull(page.text("scanId"))
            val scanId = page.text("scanId")
            var count = 0
            var knownExit = false
            var pages = 0
            val knownRows = mutableListOf<Pair<String, String>>()
            while (true) {
                assertEquals(scanId, page.text("scanId"))
                val rows = page.array("observations")!!
                count += rows.size
                knownExit = knownExit || rows.any { it.jsonObject.obj("exit")?.flag("value") != null }
                rows.forEach { value ->
                    val row = value.jsonObject
                    row.obj("exit")?.flag("value")?.let { exit ->
                        knownRows += requireNotNull(row.text("symbol")) to if (exit) "退出 · 已触发" else "退出 · 未触发"
                    }
                }
                assertTrue(rows.size <= 20)
                if (page.flag("hasMore") != true) break
                assertTrue("Research pagination must be bounded", ++pages < 25)
                page = read("get_quant_observations", requireNotNull(page.text("nextCursor")))
            }
            assertTrue("No real research observations", count > 0)
            assertTrue("All exit verdicts are unknown", knownExit)
            assertEquals(pair, runtime.state.value.pairId)
            println("QUANT_RESEARCH_PASS: scan=$scanId rows=$count knownExit=$knownExit")
            if (InstrumentationRegistry.getArguments().getString("quantResearchUi") == "true") {
                verifyLiveResearchCard(context, knownRows)
            }
        }
    }

    private fun verifyLiveResearchCard(context: Context, knownRows: List<Pair<String, String>>) {
        val automation = InstrumentationRegistry.getInstrumentation().uiAutomation
        fun descendants(root: AccessibilityNodeInfo): Sequence<AccessibilityNodeInfo> = sequence {
            yield(root)
            for (index in 0 until root.childCount) root.getChild(index)?.let { yieldAll(descendants(it)) }
        }
        fun visibleNodes() = automation.rootInActiveWindow?.let { root ->
            descendants(root).filter { it.isVisibleToUser }.toList()
        }.orEmpty()
        fun has(label: String) = visibleNodes().any {
            it.text?.contains(label) == true || it.contentDescription?.contains(label) == true
        }
        fun poll(label: String, timeoutMs: Long = 8_000, predicate: () -> Boolean) {
            val deadline = SystemClock.uptimeMillis() + timeoutMs
            while (SystemClock.uptimeMillis() < deadline) {
                if (predicate()) return
                SystemClock.sleep(100)
            }
            throw AssertionError("$label. Visible: ${visibleNodes().mapNotNull { it.text }.take(35)}")
        }
        // Operate the existing activity and its real repository state; no replacement composition.
        poll("Cannot click the existing Discover navigation") {
            visibleNodes().filter { it.text?.toString() == "选股" || it.contentDescription?.toString() == "选股" }
                .any { found -> generateSequence(found) { it.parent }
                    .any { it.isClickable && it.performAction(AccessibilityNodeInfo.ACTION_CLICK) } }
        }
        poll("Discover page did not open") { has("多因子量化候选池") || has("扫描策略档案切换") || has("量化研究观察") }
        fun scrollable() = visibleNodes().filter { it.isScrollable }.maxByOrNull {
            val bounds = Rect().also(it::getBoundsInScreen)
            bounds.width().toLong() * bounds.height()
        }
        // Main-tab scroll positions persist. Return toward the top before searching downward.
        val resetDeadline = SystemClock.uptimeMillis() + 5_000
        while (SystemClock.uptimeMillis() < resetDeadline &&
            scrollable()?.performAction(AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD) == true) {
            SystemClock.sleep(120)
        }
        fun smallSwipe() {
            val area = Rect().also { requireNotNull(scrollable()) { "Discover has no scrollable body" }.getBoundsInScreen(it) }
            val down = SystemClock.uptimeMillis()
            val start = area.top + area.height() * 0.7f
            val distance = area.height() * 0.16f
            for (step in 0..8) {
                val action = when (step) { 0 -> MotionEvent.ACTION_DOWN; 8 -> MotionEvent.ACTION_UP; else -> MotionEvent.ACTION_MOVE }
                val event = MotionEvent.obtain(down, SystemClock.uptimeMillis(), action,
                    area.exactCenterX(), start - distance * step / 8, 0)
                try { assertTrue("Could not scroll the real Discover page", automation.injectInputEvent(event, true)) }
                finally { event.recycle() }
                SystemClock.sleep(20)
            }
            SystemClock.sleep(140)
        }
        poll("Research card was not reachable", 15_000) {
            if (has("量化研究观察")) true else { smallSwipe(); false }
        }
        poll("Real research observations did not load", 20_000) {
            if (has("研究观察已载入")) true else {
                if (!has("正在读取研究观察")) smallSwipe()
                false
            }
        }
        var visibleRow: Pair<String, String>? = null
        poll("No known real symbol and exit verdict are visible", 15_000) {
            visibleRow = knownRows.firstOrNull { (symbol, exit) -> has(symbol) && has(exit) }
            if (visibleRow != null) true else { smallSwipe(); false }
        }
        val directory = requireNotNull(context.getExternalFilesDir("quant-research"))
        assertTrue("Cannot create screenshot directory", directory.isDirectory || directory.mkdirs())
        val path = File(directory, "live-card.png")
        val bitmap = requireNotNull(automation.takeScreenshot()) { "Cannot capture real research card" }
        try { path.outputStream().use { assertTrue(bitmap.compress(Bitmap.CompressFormat.PNG, 100, it)) } }
        finally { bitmap.recycle() }
        assertTrue("Empty research screenshot", path.length() > 0)
        println("QUANT_RESEARCH_UI_PASS: symbol=${visibleRow!!.first} exit=${visibleRow!!.second} screenshot=${path.absolutePath}")
    }

    @Test fun existingPairSynchronizesAndReturnsSupportedReads() = runBlocking {
        assumeTrue(InstrumentationRegistry.getArguments().getString("deployedServiceTest") == "true")
        launchResumedMainActivity()
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val runtime = (context.applicationContext as TickDeskApplication).runtime
        val pair = requireNotNull(runtime.pairedScope())
        runtime.withReminderConnection {
            withTimeout(20_000) { runtime.state.first { it.connection.canWrite } }
            assertEquals(pair, runtime.state.value.pairId)
            assertFalse(runtime.state.value.requiresReview)
            for (command in listOf("get_status", "get_logs", "get_messages", "list_files")) {
                val id = runtime.enqueue(command, buildJsonObject {})
                val result = withTimeout(15_000) {
                    runtime.state.first { state -> state.intents.any {
                        it.id == id && it.stage in setOf(DeliveryStage.APPLIED, DeliveryStage.REJECTED)
                    } }
                }.intents.single { it.id == id }
                assertEquals("$command ${result.errorCode}", DeliveryStage.APPLIED, result.stage)
                assertNotNull(result.result)
                if (command == "get_status") {
                    val supported = result.result!!.obj("protocol")!!.array("supportedCommands")!!
                    assertTrue(supported.size >= 29)
                    if (InstrumentationRegistry.getArguments().getString("dependencyProbeTest") == "true") {
                        val dependencies = result.result!!.array("dependencies")!!.map { it.jsonObject }.associateBy { it.text("name") }
                        val mcp = requireNotNull(dependencies["mcp"])
                        assertEquals(mcp.toString(), "online", mcp.text("status"))
                        val kernel = requireNotNull(dependencies["kernel"])
                        assertTrue(kernel.toString(), kernel.text("status") in setOf("online", "degraded"))
                        assertNotNull(result.result!!.obj("versions")!!.text("kernel"))
                        println("DEPENDENCY_PROBE_PASS: mcp=${mcp.text("status")} kernel=${kernel.text("status")} reason=${kernel.text("errorCode")}")
                    }
                }
                println("DEPLOYED_READ_PASS: $command")
            }
            assertEquals(pair, runtime.state.value.pairId)
        }
    }
}
