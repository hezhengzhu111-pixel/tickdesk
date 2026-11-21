package dev.tickdesk.app.ui

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import dev.tickdesk.core.*
import kotlinx.serialization.json.*

@Composable
internal fun QuantResearchCard(state: WorkbenchProjection, busy: Boolean, submit: (String, JsonObject) -> Unit) {
    val view = state.quantResearch()
    val canRead = state.connection.localOnline && !busy && !view.loading && !state.requiresReview
    val latest = listOfNotNull(state.reads["scan_result"], state.reads["scan_now"])
        .maxByOrNull { it.number("finishedAt") ?: Long.MIN_VALUE }?.text("scanId")
        ?: state.meta?.obj("analysis")?.text("lastScanId")
    var requested by rememberSaveable(state.pairId, state.epoch) { mutableStateOf<String?>(null) }
    LaunchedEffect(latest, canRead, view.supported) {
        if (canRead && view.supported == true && latest != null && requested != latest) {
            requested = latest
            submit("get_quant_observations", buildJsonObject {})
        }
    }
    QuantResearchPanel(view, canRead,
        read = { submit("get_quant_observations", buildJsonObject {}) },
        next = { submit("get_quant_observations", buildJsonObject {
            view.requestedScanId?.let { put("scanId", it) }
            put("cursor", view.nextCursor)
        }) },
        capabilities = { submit("get_status", buildJsonObject {}) })
}

@Composable
internal fun QuantResearchPanel(
    view: QuantResearchView, canRead: Boolean, read: () -> Unit, next: () -> Unit, capabilities: () -> Unit,
) {
    DesignCard {
        SectionTitle("量化研究观察", "只读研究 · 未获生产准入")
        Text("研究结果独立于生产候选，不构成买卖操作或持仓。", style = MaterialTheme.typography.bodySmall)
        val status = when {
            view.supported == null -> "尚未核验工作台读取能力"
            view.supported == false -> "工作台暂不支持研究观察读取"
            view.loading -> "正在读取研究观察"
            view.status == "failed" -> "研究观察读取失败 · ${researchReason(view.error)}"
            view.status == "missing" -> "本轮暂无研究观察"
            view.status == "stale" -> "新一轮扫描已结束，等待读取本轮研究观察"
            view.status == "unread" -> "尚未读取研究观察"
            view.rows.isEmpty() -> "本轮研究已完成，暂无观察条目"
            else -> "研究观察已载入 · 本页 ${view.rows.size} 条"
        }
        Text(status, style = MaterialTheme.typography.bodyMedium)
        if (view.supported == true && view.status !in setOf("unread", "stale")) {
            view.scanId?.let { Text("${if (view.historical) "历史批次" else "批次"} · $it", style = MaterialTheme.typography.bodySmall) }
            view.decisionCutoffMs?.let { Text("决策截止 · ${timeText(it)}", style = MaterialTheme.typography.bodySmall) }
            view.rows.forEach { row ->
                HorizontalDivider()
                Text("${row.symbol} · ${row.strategyId}", style = MaterialTheme.typography.titleSmall)
                Text("${researchAdmission(row.admission)} · ${researchGroup(row.group)} · ${row.timeframe}", style = MaterialTheme.typography.bodySmall)
                Text("K 线结束 · ${timeText(row.barEndMs)}${if (row.barStatus != "complete") " · 数据未知" else ""}", style = MaterialTheme.typography.bodySmall)
                Text("入场 · ${researchVerdict(row.entry)}", style = MaterialTheme.typography.bodyMedium)
                Text("退出 · ${researchVerdict(row.exit)}", style = MaterialTheme.typography.bodyMedium)
                Text("分数 · ${row.score ?: "未知 · ${researchReason(row.scoreReason)}"}", style = MaterialTheme.typography.bodyMedium)
            }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            if (view.supported == null || view.supported == false) {
                OutlinedButton(onClick = capabilities, enabled = canRead, modifier = Modifier.heightIn(min = 48.dp)) {
                    Text("核验读取能力")
                }
            } else {
                OutlinedButton(onClick = read, enabled = canRead, modifier = Modifier.heightIn(min = 48.dp)) {
                    Text("读取最新研究")
                }
                if (view.nextCursor != null) {
                    OutlinedButton(onClick = next, enabled = canRead, modifier = Modifier.heightIn(min = 48.dp)) {
                        Text("研究下一页")
                    }
                }
            }
        }
    }
}

private fun researchVerdict(value: ResearchVerdict) = when (value.value) {
    true -> "已触发"
    false -> "未触发"
    null -> "未知 · ${researchReason(value.reason)}"
}
private fun researchAdmission(value: String) = when (value) {
    "research" -> "研究中"
    "causal_checked" -> "已做因果检查"
    "oos_checked" -> "已做样本外检查"
    "shadow" -> "影子观察"
    else -> value
}
private fun researchGroup(value: String) = when (value) {
    "daily_swing" -> "日线波段"
    "short_term" -> "短线"
    "long_term" -> "长线"
    else -> value
}
private fun researchReason(value: String?) = when (value) {
    "DATA_UNAVAILABLE" -> "数据不可用"
    "missing_fundamental" -> "缺少基本面数据"
    "missing_field:ext_industry_regime" -> "行业环境资料缺失"
    "calendar_session_or_window_unknown:ext_industry_regime" -> "行业交易日或观察窗口待核实"
    "calendar_session_or_window_unknown:ext_market_regime" -> "大盘交易日或观察窗口待核实"
    "outside_scoring_pool" -> "不在当前评分范围内"
    "not_entry_candidate" -> "未满足入场条件"
    "insufficient_history" -> "历史数据不足"
    "SNAPSHOT_EXPIRED" -> "分页已过期，请读取最新研究"
    null -> "原因未知"
    else -> value
}
