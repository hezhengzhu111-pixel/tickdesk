package dev.tickdesk.app

import android.os.Bundle
import android.os.SystemClock
import android.graphics.Rect
import android.view.MotionEvent
import android.view.accessibility.AccessibilityNodeInfo
import androidx.activity.compose.setContent
import androidx.compose.foundation.ScrollState
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveableStateHolder
import androidx.compose.ui.Modifier
import androidx.test.platform.app.InstrumentationRegistry
import dev.tickdesk.app.ui.*
import dev.tickdesk.core.*
import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Test
import java.time.Instant
import java.time.ZoneId
import java.util.concurrent.CopyOnWriteArrayList

/** UI-only responses. Never mutates the normal paired repository. */
class HoldingManualRenderTest {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()

    @Test fun staleRegistrationOffersAnalysisAndCorrectionWithoutRequiringAReason() {
        val activity = launchResumedMainActivity()
        val scroll = ScrollState(0)
        val showForm = mutableStateOf(false)
        val destinations = CopyOnWriteArrayList<Pair<String, String>>()
        val sent = CopyOnWriteArrayList<IntentView>()
        val now = System.currentTimeMillis()
        val day = 86_400_000L
        val symbol = "600000.SH"
        val oldRegistration = now - 31 * day
        val referenceDate = Instant.ofEpochMilli(oldRegistration).atZone(ZoneId.of("Asia/Shanghai")).toLocalDate()
        val view = WorkbenchProjection(
            pairId = "ui-test", epoch = "test-epoch", canQueue = true,
            connection = ConnectionState(true, true, true, true, false),
            meta = buildJsonObject { put("positionRevision", 8) },
            records = mapOf(
                "positions" to listOf(buildJsonObject {
                    put("symbol", symbol); put("name", "浦发银行"); put("qty", 100)
                    put("cost", "10.123"); put("availableQty", JsonNull); put("asOf", now - day)
                }),
                "adjustments" to listOf(buildJsonObject {
                    put("symbol", symbol); put("asOf", oldRegistration)
                }, buildJsonObject {
                    put("symbol", symbol); put("asOf", now - 60 * day)
                }),
                "executions" to listOf(buildJsonObject {
                    put("symbol", symbol); put("executionId", "pending-fill"); put("side", "buy")
                    put("qty", 100); put("price", "10.123"); put("executedAt", now - day)
                    put("positionEffect", "needs_reconciliation")
                }),
            ),
        )
        try {
            onTestMainThread {
                activity.setContent { TickDeskTheme {
                    Column(Modifier.fillMaxSize().safeDrawingPadding().verticalScroll(scroll)) {
                        if (showForm.value) {
                            HoldingPositionForm(view, symbol, false, {}) { command, params, queued ->
                                val intent = IntentView("manual-correction", command, params,
                                    DeliveryStage.PENDING, null, null, 0, null, false)
                                sent += intent
                                queued(intent.id)
                            }
                        } else {
                            DesignedPortfolioPage(view, { page, target ->
                                destinations += page to target
                                if (page == "adjust") {
                                    showForm.value = true
                                    scroll.dispatchRawDelta(-scroll.value.toFloat())
                                }
                            })
                        }
                    }
                } }
            }
            click(scroll, "量化与 AI")
            assertEquals("analyze" to symbol, destinations.single())
            waitUntil {
                val labels = nodes().mapNotNull { it.text?.toString() }
                "建议校对" in labels && "登记时点 $referenceDate" in labels
            }
            click(scroll, "更正持仓")
            click(scroll, "提交台账更正")
            waitUntil { sent.size == 1 }
            val request = sent.single()
            assertEquals("adjust_position", request.command)
            assertEquals("手动核对持仓", request.data.text("reason"))
            assertEquals(100L, request.data.number("qty"))
            assertEquals("10.123", request.data.text("cost"))
            assertEquals(JsonNull, request.data["availableQty"])
            assertEquals(JsonArray(emptyList()), request.data["resolvedExecutionIds"])
            assertEquals(8L, request.data.number("basePositionRevision"))
        } finally { onTestMainThread { activity.finish() } }
    }

    @Test fun realSelectionAndServiceAppliedGateManualSave() {
        val activity = launchResumedMainActivity()
        val sent = CopyOnWriteArrayList<IntentView>()
        val returned = java.util.concurrent.atomic.AtomicBoolean(false)
        val showForm = mutableStateOf(true)
        val view = mutableStateOf(WorkbenchProjection(pairId = "ui-test", epoch = "test-epoch", canQueue = true,
            connection = ConnectionState(true, true, true, true, false), meta = buildJsonObject { put("positionRevision", 1) }))
        val scroll = ScrollState(0)
        try {
            onTestMainThread {
                activity.setContent { TickDeskTheme {
                    val screens = rememberSaveableStateHolder()
                    if (showForm.value) screens.SaveableStateProvider("manual-position") {
                        Column(Modifier.fillMaxSize().safeDrawingPadding().verticalScroll(scroll)) {
                            HoldingPositionForm(view.value, "", false, {
                                returned.set(true)
                                showForm.value = false
                            }) { command, params, queued ->
                                val id = "ui-${sent.size}"
                                val intent = IntentView(id, command, params, DeliveryStage.PENDING, null, null, 0, null, false)
                                sent += intent
                                view.value = view.value.copy(intents = view.value.intents + intent)
                                queued(id)
                            }
                        }
                    }
                } }
            }
            edit(scroll, 0, "福耀")
            waitUntil { sent.any { it.command == "search_symbols" } }
            val search = sent.last()
            assertEquals("福耀", search.data.text("query"))
            val result = buildJsonObject { putJsonArray("items") { add(buildJsonObject {
                put("symbol", "600660.SH"); put("name", "福耀玻璃"); put("instrument", "stock"); put("supported", false)
            }) } }
            onTestMainThread { view.value = view.value.copy(intents = listOf(search.copy(stage = DeliveryStage.APPLIED, result = result))) }
            click(scroll, "福耀玻璃 · 600660.SH")
            edit(scroll, 1, "100")
            edit(scroll, 3, "10.5")
            assertFalse(nodes().any { it.text?.contains("补录原因") == true })
            click(scroll, "保存持仓")
            waitUntil { sent.any { it.command == "adjust_position" } }
            val save = sent.last()
            assertEquals("600660.SH", save.data.text("symbol"))
            assertEquals("福耀玻璃", save.data.text("name"))
            assertEquals("手动添加持仓", save.data.text("reason"))
            assertEquals(JsonNull, save.data["availableQty"])
            assertFalse(returned.get())
            onTestMainThread { view.value = view.value.copy(intents = listOf(save.copy(stage = DeliveryStage.WAITING_FOR_SNAPSHOT))) }
            SystemClock.sleep(200)
            assertFalse(returned.get())
            onTestMainThread {
                view.value = view.value.copy(
                    meta = buildJsonObject { put("positionRevision", 2) },
                    intents = listOf(save.copy(stage = DeliveryStage.APPLIED)),
                )
            }
            waitUntil { returned.get() }
            onTestMainThread {
                scroll.dispatchRawDelta(-scroll.value.toFloat())
                returned.set(false)
                showForm.value = true
            }
            waitUntil { nodes().any { it.isEditable } }
            assertTrue("Reopened form must use the latest position revision and retain no previous selection",
                nodes().none { it.text?.contains("已选择 福耀玻璃") == true })
            edit(scroll, 0, "茅台")
            waitUntil { sent.last().data.text("query") == "茅台" }
            val nextSearch = sent.last()
            onTestMainThread {
                view.value = view.value.copy(intents = view.value.intents.filterNot { it.id == nextSearch.id } + nextSearch.copy(
                    stage = DeliveryStage.APPLIED, result = buildJsonObject { putJsonArray("items") { add(buildJsonObject {
                        put("symbol", "600519.SH"); put("name", "贵州茅台"); put("instrument", "stock")
                    }) } },
                ))
            }
            click(scroll, "贵州茅台 · 600519.SH")
            edit(scroll, 1, "200")
            edit(scroll, 3, "12")
            click(scroll, "保存持仓")
            waitUntil { sent.count { it.command == "adjust_position" } == 2 }
            val secondSave = sent.last()
            assertEquals(2L, secondSave.data.number("basePositionRevision"))
            assertEquals("600519.SH", secondSave.data.text("symbol"))
            onTestMainThread {
                view.value = view.value.copy(
                    meta = buildJsonObject { put("positionRevision", 3) },
                    intents = listOf(secondSave.copy(stage = DeliveryStage.REJECTED, errorCode = "POSITION_VERSION_CONFLICT")),
                )
            }
            click(scroll, "已核对最新快照")
            click(scroll, "保存持仓")
            waitUntil { sent.count { it.command == "adjust_position" } == 3 }
            assertEquals(3L, sent.last().data.number("basePositionRevision"))
            assertEquals("600519.SH", sent.last().data.text("symbol"))
            assertEquals(200L, sent.last().data.number("qty"))
        } finally { onTestMainThread { activity.finish() } }
    }

    @Test fun reconcileFirstFillWithoutAnExistingHoldingAndSendOnlySelectedExecution() {
        val activity = launchResumedMainActivity()
        val scroll = ScrollState(0)
        val back = java.util.concurrent.atomic.AtomicBoolean(false)
        val sent = CopyOnWriteArrayList<IntentView>()
        fun fill(id: String, side: String, qty: Long, executedAt: Long) = buildJsonObject {
            put("executionId", id); put("symbol", "600000.SH"); put("positionEffect", "needs_reconciliation")
            put("side", side); put("qty", qty); put("price", "10"); put("executedAt", executedAt)
        }
        val buy = fill("isolated-buy", "buy", 100, 1_794_792_300_000L)
        val sell = fill("isolated-sell", "sell", 25, 1_794_792_350_000L)
        val view = mutableStateOf(WorkbenchProjection(
            pairId = "ui-test", epoch = "test-epoch", canQueue = true,
            connection = ConnectionState(true, true, true, true, false),
            meta = buildJsonObject { put("positionRevision", 1) },
            records = mapOf("executions" to listOf(buy, sell)),
        ))
        val showForm = mutableStateOf(false)
        try {
            onTestMainThread {
                activity.setContent { TickDeskTheme {
                    Column(Modifier.fillMaxSize().safeDrawingPadding().verticalScroll(scroll)) {
                        if (showForm.value) {
                            HoldingPositionForm(view.value, "600000.SH", false, {
                                back.set(true)
                                showForm.value = false
                            }, reconcile = true) { command, params, queued ->
                                val intent = IntentView("reconcile", command, params, DeliveryStage.PENDING, null, null, 0, null, false)
                                sent += intent
                                view.value = view.value.copy(intents = listOf(intent))
                                queued(intent.id)
                            }
                        } else {
                            DesignedPortfolioPage(view.value, { page, symbol ->
                                assertEquals("adjust_reconcile", page)
                                assertEquals("600000.SH", symbol)
                                showForm.value = true
                                scroll.dispatchRawDelta(-scroll.value.toFloat())
                            })
                        }
                    }
                } }
            }
            click(scroll, "600000.SH · 2 笔待对账")
            edit(scroll, 0, "100")
            edit(scroll, 2, "10")
            val executionLabel = "买入 100 股 · 10 元 · ${timeText(buy.number("executedAt"))}"
            click(scroll, executionLabel)
            click(scroll, "确认成交对账")
            waitUntil { sent.size == 1 }
            val request = sent.single()
            assertEquals("adjust_position", request.command)
            assertEquals(JsonArray(listOf(JsonPrimitive("isolated-buy"))), request.data["resolvedExecutionIds"])
            assertEquals(100L, request.data.number("qty"))
            onTestMainThread {
                view.value = view.value.copy(intents = listOf(request.copy(stage = DeliveryStage.WAITING_FOR_SNAPSHOT)))
            }
            assertFalse(back.get())
            onTestMainThread {
                view.value = view.value.copy(
                    intents = listOf(request.copy(stage = DeliveryStage.APPLIED)),
                    records = mapOf("executions" to listOf(JsonObject(buy + ("positionEffect" to JsonPrimitive("reconciled"))), sell)),
                )
            }
            waitUntil { back.get() }
            onTestMainThread { scroll.dispatchRawDelta(-scroll.value.toFloat()) }
            click(scroll, "600000.SH · 1 笔待对账")
            waitUntil { nodes().any { it.text?.contains("成交对账") == true } }
            assertTrue(nodes().none { it.text?.contains("买入 100 股") == true })
        } finally { onTestMainThread { activity.finish() } }
    }

    private fun descendants(node: AccessibilityNodeInfo): Sequence<AccessibilityNodeInfo> = sequence {
        yield(node)
        for (i in 0 until node.childCount) node.getChild(i)?.let { yieldAll(descendants(it)) }
    }
    private fun nodes() = instrumentation.uiAutomation.rootInActiveWindow?.let { descendants(it).toList() }.orEmpty()
    private fun waitUntil(check: () -> Boolean) {
        val until = SystemClock.uptimeMillis() + 6_000
        while (!check() && SystemClock.uptimeMillis() < until) SystemClock.sleep(80)
        assertTrue(check())
    }
    private fun edit(scroll: ScrollState, index: Int, text: String) {
        onTestMainThread { scroll.dispatchRawDelta(-scroll.value.toFloat()) }
        waitUntil { nodes().count { it.isEditable } > index }
        val field = nodes().filter { it.isEditable }[index]
        assertTrue(field.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        }))
    }
    private fun click(scroll: ScrollState, label: String) {
        fun target() = nodes().filter { it.isVisibleToUser && it.text?.toString() == label }.firstNotNullOfOrNull { node ->
            generateSequence(node) { it.parent }.firstOrNull { it.isClickable && it.isEnabled }
        }
        waitUntil {
            val found = target()
            if (found != null) true else { onTestMainThread { scroll.dispatchRawDelta(200f) }; false }
        }
        val bounds = Rect().also(requireNotNull(target())::getBoundsInScreen)
        val down = SystemClock.uptimeMillis()
        val press = MotionEvent.obtain(down, down, MotionEvent.ACTION_DOWN, bounds.exactCenterX(), bounds.exactCenterY(), 0)
        try { assertTrue(instrumentation.uiAutomation.injectInputEvent(press, true)) } finally { press.recycle() }
        SystemClock.sleep(60)
        val release = MotionEvent.obtain(down, SystemClock.uptimeMillis(), MotionEvent.ACTION_UP, bounds.exactCenterX(), bounds.exactCenterY(), 0)
        try { assertTrue(instrumentation.uiAutomation.injectInputEvent(release, true)) } finally { release.recycle() }
    }
}
