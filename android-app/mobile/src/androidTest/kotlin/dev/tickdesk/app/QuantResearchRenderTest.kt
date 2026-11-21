package dev.tickdesk.app

import androidx.activity.compose.setContent
import android.os.SystemClock
import android.view.accessibility.AccessibilityNodeInfo
import androidx.compose.foundation.ScrollState
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.key
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.test.platform.app.InstrumentationRegistry
import dev.tickdesk.app.ui.QuantResearchPanel
import dev.tickdesk.app.ui.TickDeskTheme
import dev.tickdesk.core.*
import org.junit.Assert.*
import org.junit.Test

/** Presentation-only fixture; no repository, ledger or network mutations. */
class QuantResearchRenderTest {
    @Test fun researchCardShowsIndependentVerdictsAndReadOnlyStates() {
        val instrument = InstrumentationRegistry.getInstrumentation()
        val activity = launchResumedMainActivity()
        val row = ResearchObservation("300502.SZ", "research-only", "daily_swing", "1d", "research",
            1794792300000, "complete", ResearchVerdict(null, "missing_fundamental"),
            ResearchVerdict(false, null), "0", null)
        val base = QuantResearchView(true, false, "available", null, "test-scan", 1794792300000,
            false, listOf(row), null, null)
        fun descendants(root: AccessibilityNodeInfo): Sequence<AccessibilityNodeInfo> = sequence {
            yield(root)
            for (index in 0 until root.childCount) root.getChild(index)?.let { yieldAll(descendants(it)) }
        }
        fun nodes() = instrument.uiAutomation.rootInActiveWindow?.let { descendants(it).toList() }.orEmpty()
        // Compose's virtual-node provider does not implement findAccessibilityNodeInfosByText.
        // Traverse its exported tree instead, as the full-screen design test does.
        fun contains(text: String) = nodes().any {
            it.isVisibleToUser && (it.text?.contains(text) == true || it.contentDescription?.contains(text) == true)
        }
        fun awaitVisible(text: String, scroll: ScrollState? = null) {
            val until = SystemClock.uptimeMillis() + 6_000
            while (SystemClock.uptimeMillis() < until) {
                if (contains(text)) return
                if (scroll != null) onTestMainThread {
                    if (scroll.value < scroll.maxValue) scroll.dispatchRawDelta(140f)
                }
                SystemClock.sleep(100)
            }
            throw AssertionError("Missing '$text'; visible nodes: ${nodes().filter { it.isVisibleToUser }.map {
                "${it.text ?: ""} [${it.contentDescription ?: ""}]"
            }}; scroll=${scroll?.value}/${scroll?.maxValue}")
        }
        var scenario = 0
        fun render(view: QuantResearchView, expected: String) {
            val marker = "quant-research-render-${++scenario}"
            val scroll = ScrollState(0)
            onTestMainThread {
                activity.setContent {
                    key(marker) {
                        Box(Modifier.fillMaxSize().safeDrawingPadding().semantics { contentDescription = marker }) {
                            TickDeskTheme {
                                Column(Modifier.fillMaxSize().verticalScroll(scroll)) {
                                    QuantResearchPanel(view, true, {}, {}, {})
                                }
                            }
                        }
                    }
                }
            }
            // Wait for this scenario, not an old activity frame or global animation idleness.
            awaitVisible(marker)
            awaitVisible(expected, scroll)
            assertFalse(contains("回填成交"))
            assertFalse(contains("记录成交"))
        }
        try {
            render(base, "入场 · 未知 · 缺少基本面数据")
            render(base, "退出 · 未触发")
            render(base, "分数 · 0")
            render(base.copy(rows = listOf(row.copy(entry = ResearchVerdict(null, "missing_field:ext_industry_regime")))), "行业环境资料缺失")
            val unknownWindow = ResearchVerdict(null, "calendar_session_or_window_unknown:ext_industry_regime")
            render(base.copy(rows = listOf(row.copy(entry = unknownWindow))), "入场 · 未知 · 行业交易日或观察窗口待核实")
            render(base.copy(rows = listOf(row.copy(exit = ResearchVerdict(true, null)))), "退出 · 已触发")
            render(base.copy(status = "missing", rows = emptyList()), "本轮暂无研究观察")
            render(base.copy(status = "failed", error = "DATA_UNAVAILABLE", rows = emptyList()), "数据不可用")
            render(base.copy(loading = true), "正在读取研究观察")
            render(base.copy(supported = false), "工作台暂不支持研究观察读取")
        } finally {
            onTestMainThread { activity.finish() }
        }
    }
}
