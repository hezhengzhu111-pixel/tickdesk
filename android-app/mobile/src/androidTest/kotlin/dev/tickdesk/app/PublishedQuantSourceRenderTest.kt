package dev.tickdesk.app

import android.os.SystemClock
import android.view.accessibility.AccessibilityNodeInfo
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.ui.Modifier
import androidx.test.platform.app.InstrumentationRegistry
import dev.tickdesk.app.ui.ChatSourceChips
import dev.tickdesk.app.ui.TickDeskTheme
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.util.concurrent.atomic.AtomicReference

class PublishedQuantSourceRenderTest {
    @Test fun savedScanSourceOpensItsOwnTask() {
        assumeTrue("Use the isolated demo app", BuildConfig.MOCK_DEMO)
        val activity = launchResumedMainActivity()
        val automation = InstrumentationRegistry.getInstrumentation().uiAutomation
        val destination = AtomicReference<String>()
        fun children(node: AccessibilityNodeInfo): Sequence<AccessibilityNodeInfo> = sequence {
            yield(node)
            for (index in 0 until node.childCount) node.getChild(index)?.let { yieldAll(children(it)) }
        }
        fun find(label: String) = automation.rootInActiveWindow?.let { root ->
            children(root).firstOrNull { it.isVisibleToUser && it.text?.toString() == label }
        }
        fun visible(label: String): AccessibilityNodeInfo {
            val end = SystemClock.uptimeMillis() + 6000
            while (SystemClock.uptimeMillis() < end) {
                find(label)?.let { return it }
                SystemClock.sleep(60)
            }
            throw AssertionError("Source UI did not display $label")
        }
        fun tap(label: String) {
            var node: AccessibilityNodeInfo? = visible(label)
            while (node != null && !node.isClickable) node = node.parent
            assertTrue("No clickable element for $label", node?.performAction(AccessibilityNodeInfo.ACTION_CLICK) == true)
        }
        fun source(name: String, ref: String) = buildJsonObject {
            put("name", name)
            put("ref", ref)
            put("asOf", 1794794400000)
        }
        try {
            onTestMainThread {
                activity.setContent {
                    TickDeskTheme {
                        Column(Modifier.safeDrawingPadding()) {
                            ChatSourceChips(listOf(
                                source("隔离量化来源", "local:scan:scan_fixture:task:task_fixture"),
                                source("非量化来源", "local:scan:scan_fixture:task:task_fixture:extra"),
                            )) { destination.set(it) }
                        }
                    }
                }
            }
            tap("非量化来源")
            visible("数据来源")
            assertNull(find("查看量化事项"))
            tap("关闭")
            tap("隔离量化来源")
            tap("查看量化事项")
            onTestMainThread { assertEquals("task_fixture", destination.get()) }
            android.util.Log.i("TickDeskReportSource", "报告来源打开量化事项 task_fixture；普通来源无跳转")
        } finally {
            onTestMainThread { activity.finish() }
        }
    }
}
