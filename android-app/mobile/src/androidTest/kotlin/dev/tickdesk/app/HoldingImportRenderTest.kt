package dev.tickdesk.app

import android.os.SystemClock
import android.view.accessibility.AccessibilityNodeInfo
import androidx.activity.compose.setContent
import dev.tickdesk.app.ui.HoldingImportPanel
import dev.tickdesk.app.ui.TickDeskTheme
import dev.tickdesk.core.*
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.*
import org.junit.Test

/** Presentation fixtures only; no production ledger writes. */
class HoldingImportRenderTest {
    @Test fun inlineImportShowsOnlyConfirmedSuccessAndTwoFailureKinds() {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val activity = launchResumedMainActivity()
        fun descendants(node: AccessibilityNodeInfo): Sequence<AccessibilityNodeInfo> = sequence {
            yield(node)
            for (i in 0 until node.childCount) node.getChild(i)?.let { yieldAll(descendants(it)) }
        }
        fun text() = instrumentation.uiAutomation.rootInActiveWindow?.let { root ->
            descendants(root).filter { it.isVisibleToUser }.joinToString(" ") { "${it.text?.toString().orEmpty()} ${it.contentDescription?.toString().orEmpty()}" }
        }.orEmpty()
        try {
            for ((status, label) in listOf(HoldingImportStatus.UPLOADING to "正在上传", HoldingImportStatus.RECOGNIZING to "正在识别",
                HoldingImportStatus.SYNCING to "正在刷新持仓", HoldingImportStatus.INVALID to "内容不合法",
                HoldingImportStatus.FAILED to "识别失败", HoldingImportStatus.SUCCESS to "成功")) {
                onTestMainThread {
                    activity.setContent {
                        TickDeskTheme { HoldingImportPanel(WorkbenchProjection(holdingImport =
                            HoldingImportView("test", status, null, "test", "upload")), false, null) }
                    }
                }
                val until = SystemClock.uptimeMillis() + 6_000
                while (!text().contains(label) && SystemClock.uptimeMillis() < until) SystemClock.sleep(100)
                assertTrue("Missing $label: ${text()}", text().contains(label))
                assertFalse(text().contains("预览差异"))
                assertFalse(text().contains("确认入账"))
                if (status != HoldingImportStatus.SUCCESS) assertFalse(text().contains("成功"))
            }
        } finally { onTestMainThread { activity.finish() } }
    }
}
