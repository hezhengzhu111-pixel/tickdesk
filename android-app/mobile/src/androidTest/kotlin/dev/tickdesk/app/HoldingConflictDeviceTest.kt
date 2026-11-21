package dev.tickdesk.app

import android.os.Bundle
import android.os.SystemClock
import android.view.accessibility.AccessibilityNodeInfo
import androidx.test.platform.app.InstrumentationRegistry
import dev.tickdesk.core.DeliveryStage
import dev.tickdesk.core.array
import dev.tickdesk.core.number
import dev.tickdesk.core.obj
import dev.tickdesk.core.text
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test

/**
 * Device acceptance for the partition draft conflict path. It uses the same
 * app pairing and runtime, never a second app session.
 */
class HoldingConflictDeviceTest {
    private val instrument = InstrumentationRegistry.getInstrumentation()
    private val automation = instrument.uiAutomation
    private val context = instrument.targetContext

    @Test(timeout = 180_000)
    fun draftSurvivesVersionConflictAndCanResubmit() = runBlocking(Dispatchers.IO) {
        assumeTrue(InstrumentationRegistry.getArguments().getString("holdingConflict") == "true")
        println("HC: launched")
        launchResumedMainActivity()
        click("持仓")
        click("持仓明细")
        println("HC: on holding page")
        waitForNode("逐个分区核对")
        val runtime = (context.applicationContext as TickDeskApplication).runtime
        val state = withTimeout(20_000) { runtime.state.first { it.canQueue && it.pairId != null } }
        val position = state.records["positions"].orEmpty().firstOrNull { it.text("symbol") == "300502.SZ" }
        assertNotNull("acceptance holding missing", position)
        // A draft from an earlier interrupted run may still be shown. Keep it
        // if it already has the two edited quantities; otherwise create it.
        if (find("250") == null) {
            if (find("放弃草稿并采用服务端分区") != null) {
                click("放弃草稿并采用服务端分区")
            }
            scrollTo("300", down = true)
            waitForNode("300")
            setText(waitForNode("300"), "250")
            setText(waitForNode("300"), "350")
        }
        val before = state.intents.map { it.id }.toSet()
        println("HC: before detail read")
        val detailId = runtime.enqueue(
            "get_holding_details",
            buildJsonObject { put("symbol", "300502.SZ") },
            state.pairId,
            state.epoch,
        )
        val detailed = withTimeout(20_000) {
            runtime.state.first { current ->
                current.intents.any { it.id == detailId && it.stage == DeliveryStage.APPLIED }
            }
        }
        println("HC: detail read done")
        val base = detailed.intents.single { it.id == detailId }.result
            ?.get("holding")?.let { holding -> (holding as? kotlinx.serialization.json.JsonObject)?.number("positionRevision") }
            ?: (position!!.number("positionRevision") ?: 0)
        android.util.Log.i("HoldingConflict", "base=$base")
        val bump = runtime.enqueue(
            "adjust_position",
            buildJsonObject {
                put("symbol", "300502.SZ")
                put("basePositionRevision", base)
                put("qty", 600)
                put("availableQty", 300)
                put("cost", "12")
                put("asOf", System.currentTimeMillis())
                put("reason", "device conflict test external update")
                put("resolvedExecutionIds", JsonArray(emptyList()))
                put("pendingLots", JsonNull)
            },
            state.pairId,
            state.epoch,
        )
        android.util.Log.i("HoldingConflict", "bump enqueued $bump")
        val bumped = withTimeout(90_000) {
            runtime.state.first { current ->
                current.intents.any { it.id == bump && it.stage == DeliveryStage.APPLIED }
            }
        }
        assertEquals(DeliveryStage.APPLIED, bumped.intents.single { it.id == bump }.stage)
        val bumpedRevision = bumped.records["positions"].orEmpty()
            .firstOrNull { it.text("symbol") == "300502.SZ" }
            ?.number("positionRevision") ?: (base + 1)
        withTimeout(90_000) {
            runtime.state.first { current ->
                current.records["positions"].orEmpty().any {
                    it.text("symbol") == "300502.SZ" &&
                        (it.number("positionRevision") ?: 0) >= bumpedRevision
                }
            }
        }
        android.util.Log.i("HoldingConflict", "bump stage ${bumped.intents.single { it.id == bump }.stage} error ${bumped.intents.single { it.id == bump }.errorCode}")
        click("核对并提交分区")
        android.util.Log.i("HoldingConflict", "submitted conflict attempt")
        val rejected = withTimeout(30_000) {
            runtime.state.first { current ->
                current.intents.any {
                    it.command == "reconcile_holding" && it.id !in before &&
                        it.stage == DeliveryStage.REJECTED &&
                        it.errorCode == "POSITION_VERSION_CONFLICT"
                }
            }
        }
        android.util.Log.i("HoldingConflict", "rejected confirmed")
        assertEquals("POSITION_VERSION_CONFLICT", rejected.intents.last { it.command == "reconcile_holding" }.errorCode)
        scrollTo("250", down = true)
        val visibleAfterReject = automation.rootInActiveWindow
            ?.let { descendants(it).mapNotNull { node -> node.text?.toString() }.take(60).toList() }
        assertNotNull("draft quantity 1 was lost; visible=$visibleAfterReject", find("250"))
        scrollTo("350")
        assertNotNull("draft quantity 2 was lost; visible=$visibleAfterReject", find("350"))
        scrollTo("采用最新版本并保留草稿", down = true)
        click("采用最新版本并保留草稿")
        click("核对并提交分区")
        val applied = withTimeout(90_000) {
            runtime.state.first { current ->
                current.intents.any {
                    it.command == "reconcile_holding" && it.id !in before &&
                        it.stage == DeliveryStage.APPLIED
                }
            }
        }
        assertEquals(DeliveryStage.APPLIED, applied.intents.last { it.command == "reconcile_holding" }.stage)
        val finalRevision = applied.records["positions"].orEmpty()
            .firstOrNull { it.text("symbol") == "300502.SZ" }
            ?.number("positionRevision") ?: 0
        assertTrue(finalRevision > bumpedRevision)
        withTimeout(30_000) {
            runtime.state.first { current ->
                val partitions = current.reads["get_holding_details"]?.obj("holding")?.array("partitions")
                partitions?.map { element: kotlinx.serialization.json.JsonElement ->
                    (element as? kotlinx.serialization.json.JsonObject)?.number("qty")
                }?.contains(250L) == true
            }
        }
        SystemClock.sleep(1_500)
        reveal("250")
        val afterFirst = visibleTexts()
        reveal("350")
        val visible = (afterFirst + visibleTexts()).distinct()
        assertTrue("screen did not confirm 250: $visible", "250" in visible)
        assertTrue("screen did not confirm 350: $visible", "350" in visible)
        val appliedIntent = applied.intents.last { it.command == "reconcile_holding" }
        val evidence = buildString {
            appendLine("stage=${appliedIntent.stage} error=${appliedIntent.errorCode}")
            appendLine("result=${appliedIntent.result}")
            appendLine("positionRevision=$finalRevision")
            appendLine("confirmed=250,350")
            appendLine(visible.joinToString(" | "))
        }
        java.io.File(context.filesDir, "r02-final.txt").writeText(evidence)
    }

    private fun visibleTexts(): List<String> = automation.rootInActiveWindow
        ?.let { descendants(it).mapNotNull { node -> node.text?.toString() }.toList() }
        .orEmpty()

    private fun reveal(label: String) {
        if (find(label) != null) return
        repeat(8) {
            val descriptor = automation.executeShellCommand("input swipe 672 2300 672 800 250")
            android.os.ParcelFileDescriptor.AutoCloseInputStream(descriptor).use { it.readBytes() }
            SystemClock.sleep(200)
            if (find(label) != null) return
        }
        repeat(20) {
            val descriptor = automation.executeShellCommand("input swipe 672 800 672 2300 250")
            android.os.ParcelFileDescriptor.AutoCloseInputStream(descriptor).use { it.readBytes() }
            SystemClock.sleep(200)
            if (find(label) != null) return
        }
        waitForNode(label)
    }

    private fun scrollTo(label: String, down: Boolean = false) {
        repeat(8) {
            if (find(label) != null) return
            val descriptor = if (down) {
                automation.executeShellCommand("input swipe 672 800 672 2300 250")
            } else {
                automation.executeShellCommand("input swipe 672 2300 672 800 250")
            }
            android.os.ParcelFileDescriptor.AutoCloseInputStream(descriptor).use { it.readBytes() }
            SystemClock.sleep(250)
        }
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
        waitForNode(text)
    }

    private fun waitForNode(label: String): AccessibilityNodeInfo {
        val deadline = SystemClock.uptimeMillis() + 10_000
        while (SystemClock.uptimeMillis() < deadline) {
            find(label)?.let { return it }
            SystemClock.sleep(100)
        }
        val visible = automation.rootInActiveWindow
            ?.let { descendants(it).mapNotNull { node -> node.text }.take(40).toList() }
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
