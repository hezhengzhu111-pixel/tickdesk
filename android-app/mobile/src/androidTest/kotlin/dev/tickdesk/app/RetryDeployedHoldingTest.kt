package dev.tickdesk.app

import android.os.SystemClock
import android.graphics.Rect
import android.view.MotionEvent
import android.view.accessibility.AccessibilityNodeInfo
import androidx.test.platform.app.InstrumentationRegistry
import dev.tickdesk.core.*
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test

/** Explicit user-image retry, once. No fixture, pairing replacement, new upload or fabricated success. */
class RetryDeployedHoldingTest {
    @Test fun retriesCurrentUserImageOnceAndReportsAuthoritativeOutcome() = runBlocking {
        assumeTrue(InstrumentationRegistry.getArguments().getString("retryDeployedHolding") == "true")
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val automation = instrumentation.uiAutomation
        launchResumedMainActivity()
        val runtime = (instrumentation.targetContext.applicationContext as TickDeskApplication).runtime
        withTimeout(25_000) { runtime.state.first { it.connection.canWrite } }
        val before = requireNotNull(runtime.state.value.holdingImport) { "No retained user image" }
        assertTrue("Unsafe retry: ${before.status}/${before.errorCode}", before.canRetry)
        val pair = runtime.state.value.pairId
        val file = runtime.state.value.transfers.single { it.id == before.transferId }.fileId
        val transferCount = runtime.state.value.transfers.size
        fun descendants(root: AccessibilityNodeInfo): Sequence<AccessibilityNodeInfo> = sequence {
            yield(root)
            for (i in 0 until root.childCount) root.getChild(i)?.let { yieldAll(descendants(it)) }
        }
        fun nodes() = automation.rootInActiveWindow?.let { descendants(it).filter { node -> node.isVisibleToUser }.toList() }.orEmpty()
        fun touch(bounds: Rect) {
            val down = SystemClock.uptimeMillis()
            val press = MotionEvent.obtain(down, down, MotionEvent.ACTION_DOWN, bounds.exactCenterX(), bounds.exactCenterY(), 0)
            try { assertTrue(automation.injectInputEvent(press, true)) } finally { press.recycle() }
            SystemClock.sleep(60)
            val release = MotionEvent.obtain(down, SystemClock.uptimeMillis(), MotionEvent.ACTION_UP, bounds.exactCenterX(), bounds.exactCenterY(), 0)
            try { assertTrue(automation.injectInputEvent(release, true)) } finally { release.recycle() }
        }
        fun findClickable(label: String): AccessibilityNodeInfo? = nodes().filter {
            it.text?.toString() == label || it.contentDescription?.toString() == label
        }.firstNotNullOfOrNull { found -> generateSequence(found) { it.parent }.firstOrNull { it.isClickable && it.isEnabled } }
        fun awaitButton(label: String): AccessibilityNodeInfo {
            val deadline = SystemClock.uptimeMillis() + 10_000
            while (SystemClock.uptimeMillis() < deadline) {
                findClickable(label)?.let { return it }
                SystemClock.sleep(100)
            }
            error("Missing $label; visible=${nodes().map { it.text ?: it.contentDescription }}")
        }
        awaitButton("持仓")
        val portfolio = nodes().filter { it.text?.toString() == "持仓" || it.contentDescription?.toString() == "持仓" }
            .map { Rect().also(it::getBoundsInScreen) }.maxBy { it.bottom }
        touch(portfolio)
        val navigationDeadline = SystemClock.uptimeMillis() + 10_000
        while (nodes().none { it.text?.contains("本地持仓台账") == true } && SystemClock.uptimeMillis() < navigationDeadline) SystemClock.sleep(100)
        assertTrue("Portfolio navigation failed", nodes().any { it.text?.contains("本地持仓台账") == true })
        // One click only. Polling must never submit another recognition attempt.
        val retryBounds = Rect().also(awaitButton("重新识别此图")::getBoundsInScreen)
        touch(retryBounds)
        val started = withTimeout(15_000) { runtime.state.first { it.holdingImport?.id != null && it.holdingImport?.id != before.id } }
        val id = started.holdingImport!!.id
        val final = withTimeout(180_000) { runtime.state.first { it.holdingImport?.id == id && it.holdingImport?.terminal == true } }
        val result = final.holdingImport!!
        assertEquals(pair, final.pairId)
        assertEquals(before.transferId, result.transferId)
        assertEquals(file, final.transfers.single { it.id == result.transferId }.fileId)
        assertEquals(transferCount, final.transfers.size)
        val rows = final.records["positions"].orEmpty()
        if (result.status == HoldingImportStatus.SUCCESS) {
            assertTrue(final.records["imports"].orEmpty().any { it.text("importId") == result.importId && it.text("status") == "confirmed" })
            assertTrue(rows.isNotEmpty())
        } else {
            assertTrue(result.status in setOf(HoldingImportStatus.FAILED, HoldingImportStatus.INVALID))
            assertNotNull(result.errorCode)
        }
        println("DEPLOYED_HOLDING_RETRY_RESULT: status=${result.status} errorCode=${result.errorCode} rowCount=${rows.size} importId=${result.importId} fileId=$file")
    }
}
