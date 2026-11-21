package dev.tickdesk.app.ui

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import dev.tickdesk.core.*
import kotlinx.serialization.json.*

@Composable
internal fun ReportsPage(
    state: WorkbenchProjection, model: WorkbenchViewModel, busy: Boolean, go: (String, String) -> Unit,
) {
    var input by rememberSaveable { mutableStateOf("") }
    var filter by rememberSaveable { mutableStateOf("") }
    var cursor by rememberSaveable { mutableStateOf<String?>(null) }
    val normalizedInput = input.trim().uppercase()
    val requests = state.currentIntents("list_reports").filter {
        it.data.text("symbol").orEmpty() == filter && it.data.text("cursor") == cursor
    }
    val request = requests.lastOrNull()
    val result = state.reportReads.catalogues[ReportCatalogueQuery(filter, cursor)]
    val available = state.connection.localOnline && !state.requiresReview && !busy
    val publication = state.reads["analysis_report"]?.obj("header")
    val publicationId = publication?.text("reportId")
    var refreshedPublication by rememberSaveable(filter) { mutableStateOf<String?>(null) }

    fun load(symbol: String, next: String? = null) {
        model.submit("list_reports", buildJsonObject {
            if (symbol.isNotEmpty()) put("symbol", symbol)
            if (next != null) put("cursor", next)
            put("limit", 10)
        }) {
            filter = symbol
            cursor = next
        }
    }

    LaunchedEffect(available, request?.id, request?.stage, publicationId) {
        val publicationMatches = filter.isEmpty() || filter in publication?.stringList("symbols").orEmpty()
        if (available && request == null && result == null) {
            refreshedPublication = publicationId
            load(filter, cursor)
        } else if (available && cursor == null && publicationId != null && publicationId != refreshedPublication &&
            publicationMatches && request?.stage !in pendingStages) {
            refreshedPublication = publicationId
            if (result?.objects("items").orEmpty().none { it.text("reportId") == publicationId }) load(filter)
        }
    }
    DesignCard {
        Field("按证券代码筛选（可留空）", input, { input = it.take(12) },
            error = "请输入完整沪深代码，如 600000.SH。"
                .takeIf { normalizedInput.isNotEmpty() && !symbolPattern.matches(normalizedInput) })
        TextButton(
            enabled = available && request?.stage !in pendingStages &&
                (normalizedInput.isEmpty() || symbolPattern.matches(normalizedInput)),
            onClick = { load(normalizedInput) }, modifier = Modifier.heightIn(min = 48.dp),
        ) {
            Text(if (normalizedInput == filter) "刷新报告目录" else "搜索报告")
        }
    }
    if (cursor != null) TextButton(onClick = { load(filter) }, enabled = available, modifier = Modifier.heightIn(min = 48.dp)) {
        Text("返回最新报告")
    }
    val headers = result?.objects("items")
    ReportReadState(state, request, result != null, headers?.isEmpty() == true, "此范围暂无分析报告。")
    for (header in headers.orEmpty()) {
        val id = header.text("reportId") ?: continue
        DesignCard {
            ReportHeaderDetails(header, compact = true)
            TextButton(onClick = { go("report", id) }, modifier = Modifier.heightIn(min = 48.dp)) {
                Text("查看报告")
            }
        }
    }
    result?.text("nextCursor")?.let { next ->
        MainAction("更早的报告", enabled = available && request?.stage !in pendingStages) { load(filter, next) }
    }
}

internal fun WorkbenchProjection.reportDetails(id: String): JsonObject? =
    currentIntents("get_report").asReversed().firstNotNullOfOrNull { intent ->
        intent.result?.takeIf { intent.data.text("reportId") == id && it.obj("header")?.text("reportId") == id }
    }?.let { details ->
        reportReads.headers[id]?.let { header -> JsonObject(details + ("header" to header)) } ?: details
    }

@Composable
internal fun ReportDetailsPage(
    state: WorkbenchProjection, id: String, model: WorkbenchViewModel, busy: Boolean, go: (String, String) -> Unit,
) {
    val request = state.currentIntents("get_report").lastOrNull { it.data.text("reportId") == id }
    val details = state.reportDetails(id)
    val available = state.connection.localOnline && !state.requiresReview && !busy && idPattern.matches(id)
    val read = { model.submit("get_report", buildJsonObject { put("reportId", id) }) }

    var refreshedOnOpen by remember(state.pairId, state.epoch, id) { mutableStateOf(false) }
    LaunchedEffect(id, available, request?.stage) {
        if (available && !refreshedOnOpen) {
            refreshedOnOpen = true
            if (request?.stage !in pendingStages) read()
        }
    }
    details?.let { report ->
        report.obj("header")?.let { header ->
            DesignCard { ReportHeaderDetails(header, compact = false) }
        }
        DesignCard {
            FullReplyButton(state, id, report, go)
            SelectionContainer { Text(report.value("text"), style = MaterialTheme.typography.bodyMedium) }
            val sources = report.objects("sources")
            if (sources.isEmpty()) Text("本报告无可核对的引用来源。", color = DeskColors.muted)
            else ChatSourceChips(sources) { go("task", it) }
        }
    }
    ReportReadState(state, request, details != null, false, "")
    TextButton(onClick = read, enabled = available && request?.stage !in pendingStages,
        modifier = Modifier.heightIn(min = 48.dp)) { Text("刷新报告状态") }
    ReportFollowUps(state, id, model, busy, go)
}

@Composable
private fun ReportHeaderDetails(header: JsonObject, compact: Boolean) {
    val now by deskTime()
    val scope = header.stringList("symbols").joinToString("、").ifBlank { "组合与市场" }
    val expired = header.number("validUntil")?.let { it <= now } == true
    val state = when {
        header.text("state") == "historical" -> if (expired) "历史报告 · 已过期" else "历史报告"
        header.text("state") == "expired" || expired -> "已过期"
        header.text("state") == "current" -> "当前报告"
        else -> "状态未知"
    }
    val stance = when (header.text("stance")) {
        "watch" -> "继续观察"
        "insufficient" -> "证据不足"
        "restricted" -> "受约束"
        "positive" -> "偏正面"
        "negative" -> "偏负面"
        "mixed" -> "判断分歧"
        else -> "结论未知"
    }
    SectionTitle(scope, "$state · $stance")
    val coverage = header.obj("coverage")
    val labels = listOf("business" to "业务", "quant" to "量化", "external" to "外部事实")
    Text(labels.joinToString(" · ") { (key, label) ->
        val status = when (coverage?.text(key)) {
            "available" -> "已覆盖"
            "partial" -> "部分覆盖"
            "unavailable" -> "缺失"
            else -> "未知"
        }
        "$label $status"
    }, color = DeskColors.muted)
    Text("证据截至 ${evidenceTimeText(header.number("asOf"))} · 有效至 ${evidenceTimeText(header.number("validUntil"))}",
        color = DeskColors.muted, style = MaterialTheme.typography.labelSmall)
    if (!compact) {
        val origin = if (header.text("origin") == "automatic") "自动分析" else "用户发起"
        Text("$origin · 生成于 ${evidenceTimeText(header.number("generatedAt"))}", color = DeskColors.muted)
    }
    header.text("summary")?.takeIf(String::isNotBlank)?.let { summary ->
        Text("报告摘要", color = DeskColors.muted, style = MaterialTheme.typography.labelSmall)
        Text(summary, style = MaterialTheme.typography.bodyMedium,
            maxLines = if (compact) 3 else Int.MAX_VALUE, overflow = TextOverflow.Ellipsis)
    }
}

@Composable
private fun ReportReadState(state: WorkbenchProjection, request: IntentView?, hasCached: Boolean, empty: Boolean, emptyMessage: String) {
    val message = when {
        request?.errorCode in setOf("UNKNOWN_COMMAND", "UNSUPPORTED_COMMAND", "INVALID_FRAME") ->
            "当前工作台尚未支持报告读取，请核对中继和本地服务版本。"
        request?.errorCode == "CURSOR_EXPIRED" -> "目录页已过期，请刷新最新目录。"
        request?.errorCode == "AI_REPORT_UNAVAILABLE" -> "工作台无法读取此报告，请返回报告目录重新核对。"
        request?.errorCode != null -> "报告读取失败 · ${request.errorCode}"
        empty -> emptyMessage
        request?.stage in pendingStages -> "正在读取工作台报告…"
        !state.connection.localOnline -> if (hasCached) "本地服务离线，显示已读取的报告。" else "本地服务离线，暂无可读报告。"
        request?.stage == DeliveryStage.APPLIED && !hasCached -> "报告回执与此引用不符，可刷新重试。"
        request == null && !hasCached -> "正在准备读取报告…"
        else -> null
    }
    message?.let { NoticeStrip(it, DeskColors.amber) }
}
