@file:OptIn(androidx.compose.foundation.layout.ExperimentalLayoutApi::class)

package dev.tickdesk.app.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.selection.selectableGroup
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawWithCache
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.withTransform
import androidx.compose.ui.graphics.vector.PathParser
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.selected
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import dev.tickdesk.core.DeliveryStage
import dev.tickdesk.core.WorkbenchProjection
import dev.tickdesk.core.flag
import dev.tickdesk.core.number
import dev.tickdesk.core.obj
import dev.tickdesk.core.text
import kotlinx.serialization.json.*
import kotlinx.coroutines.delay
import java.math.BigDecimal
import java.math.MathContext
import java.text.DecimalFormat
import java.text.DecimalFormatSymbols
import java.time.Duration
import java.time.Instant
import java.time.format.DateTimeFormatter
import java.util.Locale

@Composable
internal fun DiscoverHeading(state: WorkbenchProjection, go: (String, String) -> Unit, pickHolding: () -> Unit = {}) {
    val analysis = state.meta?.obj("analysis")
    val (label, tint) = when {
        !state.connection.paired -> "尚未配对" to DeskColors.faint
        !state.connection.localOnline -> "分析离线" to DeskColors.faint
        analysis?.flag("paused") == true -> "分析已暂停" to DeskColors.amber
        analysis?.text("kernelStatus") == "online" -> "引擎运行中" to DeskColors.green
        analysis?.text("kernelStatus") == "starting" -> "分析启动中" to DeskColors.amber
        analysis?.text("kernelStatus") == "offline" -> "生产扫描未就绪" to DeskColors.amber
        else -> "等待分析状态" to DeskColors.faint
    }
    MarketHeader("选股", "多因子量化候选池 · 策略归因") {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
            LivePill(label, tint)
            ImportHoldingButton("截屏导入持仓", pickHolding)
        }
    }
}

@Composable
internal fun DesignedDiscoverPage(
    state: WorkbenchProjection,
    model: WorkbenchViewModel,
    busy: Boolean,
    go: (String, String) -> Unit,
) {
    val reading = state.reads["get_candidates"]
    val readScan = reading?.obj("scan")
    val scan = listOfNotNull(readScan, state.reads["scan_result"], state.reads["scan_now"])
        .maxByOrNull { it.number("finishedAt") ?: Long.MIN_VALUE }
    val scanId = scan?.text("scanId")
    val currentReading = reading?.takeIf { scanId != null && readScan?.text("scanId") == scanId }
    val candidates = reading?.objects("items").orEmpty()
    val analysis = state.meta?.obj("analysis")
    val profile = analysis?.text("profile")
    val paused = analysis?.flag("paused") == true
    val profilePending = state.intents.lastOrNull { it.command == "set_profile" && it.stage in pendingStages }
    val latestScanIntent = state.intents.lastOrNull { it.command == "scan_now" }
    val scanPending = latestScanIntent?.takeIf { it.stage in pendingStages }
    val candidateRead = state.intents.lastOrNull { it.command == "get_candidates" }
    val readingPending = candidateRead?.stage in pendingStages
    val canRead = state.connection.localOnline && !busy && !readingPending
    val autoReadScanId = when {
        scan == null && reading == null -> analysis?.text("lastScanId")
        currentReading == null && scan?.text("status") in setOf("succeeded", "partial") -> scanId
        else -> null
    }
    var lastAutoReadScanId by rememberSaveable(state.pairId, state.epoch) { mutableStateOf<String?>(null) }
    LaunchedEffect(state.pairId, state.epoch, autoReadScanId, canRead) {
        if (canRead && autoReadScanId != null && autoReadScanId != lastAutoReadScanId) {
            lastAutoReadScanId = autoReadScanId
            model.submit("get_candidates")
        }
    }
    val watches = state.rows("watchlist").associateBy { it.text("symbol") }
    val pendingWatches = state.intents.filter { it.command in setOf("watch", "unwatch") && it.stage in pendingStages }
    val publishedTasks = readScan?.stringList("publishedTaskIds").orEmpty().toSet()
    val scanTasks = state.rows("tasks").filter { it.text("taskId") in publishedTasks }
        .sortedByDescending { it.number("createdAt") }.distinctBy { it.text("symbol") }.associateBy { it.text("symbol") }
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        MarketSummaryCard {
            if (scan == null) {
                SectionTitle("最近一轮")
                MarketText(if (reading == null) "尚未同步扫描结果" else "工作台暂无历史扫描")
            } else {
                val status = scan.text("status")
                val (statusLabel, statusTint) = when (status) {
                    "succeeded" -> "全量完成" to DeskColors.green
                    "partial" -> "部分失败" to DeskColors.amber
                    "failed" -> "扫描失败" to DeskColors.red
                    "rejected" -> "已拒绝" to DeskColors.amber
                    "skipped" -> "已跳过" to DeskColors.muted
                    "suppressed" -> "已抑制" to DeskColors.amber
                    else -> "状态未知" to DeskColors.faint
                }
                val startedAt = scan.number("startedAt")
                val finishedAt = scan.number("finishedAt")
                val duration = if (startedAt != null && finishedAt != null && finishedAt >= startedAt)
                    " · ${finishedAt - startedAt} ms" else ""
                Row(
                    Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    val round = when {
                        currentReading?.flag("historical") == true -> "历史轮次"
                        scan.text("trigger") == "scheduled" -> "定时扫描"
                        scan.text("trigger") == "manual" -> "手动扫描"
                        scan.text("trigger") == "resume" -> "恢复扫描"
                        else -> "最近一轮"
                    }
                    val count = currentReading?.let { " · 本页 ${candidates.size} 只" }.orEmpty()
                    Text(round + count, style = MaterialTheme.typography.titleSmall,
                        modifier = Modifier.weight(1f).semantics { heading() })
                    Box(Modifier.weight(0.75f), contentAlignment = Alignment.CenterEnd) {
                        StatusBadge(statusLabel + if (status == "succeeded") duration else "", statusTint)
                    }
                }
                FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
                    SelectionContainer { MarketText("批次 · ${scan.value("scanId")}", numeric = true) }
                    val olderQuote = scan.text("dataStatus") == "stale" || currentReading?.flag("historical") == true
                    MarketText("快照 ${if (olderQuote) timeText(scan.number("dataAsOf")) else quoteTime(scan.number("dataAsOf"))}")
                    MarketText("策略集 · ${marketProfile(scan.text("profile"))}")
                }
                if (finishedAt != null) {
                    val timeLabel = when (status) {
                        "skipped", "rejected", "suppressed" -> "记录于"
                        else -> "扫描结束于"
                    }
                    MarketText("$timeLabel ${timeText(finishedAt)}")
                }
                when (scan.text("dataStatus")) {
                    "stale" -> StatusBadge("行情已过期", DeskColors.amber)
                    "unknown" -> MarketText("行情状态未知")
                }
                if (status == "partial") {
                    val strategies = scan.obj("strategy")?.stringList("failed").orEmpty()
                    val failure = if (strategies.isEmpty()) "部分策略失败" else "${strategies.size} 个策略失败"
                    MarketText("$failure · 仅展示成功策略返回的结果", color = DeskColors.amber)
                    if (strategies.isNotEmpty()) MarketText(strategies.joinToString("、"), color = DeskColors.amber)
                }
                scan.obj("error")?.text("message")?.takeIf(String::isNotBlank)?.let { MarketText(it, color = statusTint) }
                scan.objects("rejectionSummary").forEach { excluded ->
                    val reason = when (excluded.text("reasonCode")) {
                        "NO_PRODUCTION_APPROVAL" -> "暂无已准入的生产策略"
                        "TRADING_CALENDAR_UNAVAILABLE" -> "交易日历待核验"
                        "PRODUCTION_EVALUATION_FAILED" -> "生产策略计算失败"
                        "ACTIVE_TASK_EXISTS" -> "已有待处理事项"
                        "TRADING_STATUS_UNCONFIRMED" -> "今日停复牌待核实"
                        "TRADING_SUSPENDED" -> "当日竞价停牌"
                        "EXPECTED_DECISION_BAR_UNAVAILABLE" -> "已收盘日线待核验"
                        "REVIEWED_UNIVERSE_INCOMPLETE" -> "审阅证券资料不齐"
                        "REVIEWED_DIRECTION_UNAVAILABLE" -> "买卖条件待核验"
                        "CURRENT_EXECUTION_QUOTE_UNAVAILABLE" -> "逐证券实时报价待核验"
                        "CURRENT_TURNOVER_UNAVAILABLE" -> "当日成交额待核验"
                        "EXIT_SCORE_UNVERIFIED" -> "卖出评分待核验"
                        "ENTRY_SCORE_UNVERIFIED" -> "买入评分待核验"
                        else -> excluded.value("reasonCode")
                    }
                    MarketText("$reason · ${excluded.value("count")}")
                }
            }
            if (scanPending != null) {
                MarketText("扫描请求 · ${scanPending.stage.label()}", color = DeskColors.blue)
            } else if (paused) {
                MarketText("分析已暂停。保留工作台已发布的扫描结果。", color = DeskColors.amber)
            }
            latestScanIntent?.takeIf { it.stage == DeliveryStage.REJECTED && it.result == null }?.let { IntentText(it) }
        }
        Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
            SectionTitle("扫描策略档案切换", if (profile == null) "尚未同步" else "当前 · ${marketProfile(profile)}")
            Surface(
                color = Color(0xFF090D14),
                shape = RoundedCornerShape(13.dp),
                border = BorderStroke(1.dp, DeskColors.border),
            ) {
                Row(
                    Modifier.fillMaxWidth().selectableGroup().padding(4.dp),
                    horizontalArrangement = Arrangement.spacedBy(4.dp),
                ) {
                    listOf("conservative", "aggressive").forEach { option ->
                        val selected = option == profile
                        val enabled = state.canQueue && !busy && profilePending == null
                        Box(
                            Modifier.weight(1f).heightIn(min = 48.dp)
                                .clip(RoundedCornerShape(10.dp))
                                .background(Brush.verticalGradient(
                                    if (selected) listOf(Color(0xFF28364F), Color(0xFF1E293B))
                                    else listOf(Color.Transparent, Color.Transparent),
                                ))
                                .selectable(selected, enabled = enabled, role = Role.Tab) {
                                    if (!selected) model.submit("set_profile", buildJsonObject { put("profile", option) })
                                }
                                .padding(horizontal = 8.dp, vertical = 7.dp),
                            contentAlignment = Alignment.Center,
                        ) {
                            Text(
                                marketProfile(option), style = MaterialTheme.typography.bodyMedium,
                                color = if (selected) DeskColors.text else DeskColors.muted,
                                fontWeight = if (selected) FontWeight.SemiBold else FontWeight.Medium,
                                textAlign = TextAlign.Center,
                            )
                        }
                    }
                }
            }
            MarketText("切换后自下一轮扫描起生效，已生成事项不受影响。")
            profilePending?.let { pending ->
                MarketText("${marketProfile(pending.data.text("profile"))} · ${pending.stage.label()}", color = DeskColors.amber)
            }
        }
        MarketActionPair(
            first = {
                val scanLabel = when {
                    paused -> "引擎已暂停 · 无法扫描"
                    scanPending != null -> scanPending.stage.label()
                    else -> "立即扫描全市场"
                }
                MarketButton(scanLabel, MarketIcon.Scan, state.canQueue && !busy && !paused && scanPending == null,
                    primary = true) { model.submit("scan_now") }
            },
            second = {
                MarketButton(if (readingPending) "正在读取结果" else "同步扫描结果", MarketIcon.Refresh, canRead) {
                    model.submit("get_candidates")
                }
            },
        )
        QuantResearchCard(state, busy) { command, params -> model.submit(command, params) }
        Column(verticalArrangement = Arrangement.spacedBy(9.dp)) {
            SectionTitle("候选标的（按综合评分排序）", if (state.meta == null) null else "自选 ${watches.size}")
            candidateRead?.takeIf { it.stage == DeliveryStage.REJECTED }?.let { IntentText(it) }
            when {
                candidates.isNotEmpty() && (currentReading == null || scanPending != null) ->
                    NoticeStrip("下方为已发布批次 ${readScan?.value("scanId")} 的结果 · 快照 ${timeText(readScan?.number("dataAsOf"))}。")
                currentReading == null && scan?.text("status") in setOf("succeeded", "partial") && !readingPending ->
                    NoticeStrip("候选尚未载入，可同步扫描结果。")
                currentReading != null && candidates.isEmpty() && scan?.text("status") in setOf("succeeded", "partial") ->
                    NoticeStrip("当前截面没有符合条件的候选标的。")
            }
            MarketLink("搜索证券 / 添加关注") { go("symbols", "") }
            candidates.forEach { candidate ->
                val symbol = candidate.value("symbol")
                val watched = watches.containsKey(symbol)
                val watchPending = pendingWatches.lastOrNull { symbol in it.data.stringList("symbols") }
                val taskId = scanTasks[symbol]?.text("taskId")
                CandidateCard(
                    candidate = candidate,
                    watched = watched,
                    watchPending = watchPending?.stage?.label(),
                    watchEnabled = state.meta != null && state.canQueue && !busy && watchPending == null,
                    watch = {
                        model.submit(if (watched) "unwatch" else "watch", buildJsonObject {
                            putJsonArray("symbols") { add(JsonPrimitive(symbol)) }
                        })
                    },
                    record = { go("execution_manual", symbol) },
                    chart = { go("chart", symbol) },
                    review = taskId?.let { { go("task", it) } },
                )
            }
            reading?.text("nextPageCursor")?.let { cursor ->
                val cursorSource = state.intents.lastOrNull {
                    it.command == "get_candidates" && it.result?.text("nextPageCursor") == cursor
                }
                val requestedScanId = cursorSource?.data?.text("scanId")
                MarketButton("读取下一页", enabled = canRead) {
                    model.submit("get_candidates", buildJsonObject {
                        // The cursor is bound to the original query, including an omitted scanId.
                        requestedScanId?.let { put("scanId", it) }
                        put("pageCursor", cursor)
                    })
                }
            }
            if (watches.isNotEmpty()) {
                Spacer(Modifier.height(4.dp))
                SectionTitle("关注池 · ${watches.size}", "仅关注")
                watches.values.forEach { row ->
                    val symbol = row.value("symbol")
                    val watchPending = pendingWatches.lastOrNull { symbol in it.data.stringList("symbols") }
                    DesignCard {
                        Text(row.text("name")?.takeIf(String::isNotBlank) ?: symbol,
                            style = MaterialTheme.typography.titleMedium)
                        MarketText("${tickerLabel(symbol)} · 行情 ${quoteTime(row.number("dataAsOf"))}")
                        StatusBadge(marketRisk(row.text("riskStatus")), riskTint(row.text("riskStatus")))
                        MarketLink("查看 K 线") { go("chart", symbol) }
                        MarketLink(watchPending?.stage?.label() ?: "取消关注",
                            enabled = state.canQueue && !busy && watchPending == null) {
                            model.submit("unwatch", buildJsonObject { putJsonArray("symbols") { add(JsonPrimitive(symbol)) } })
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun CandidateCard(
    candidate: JsonObject,
    watched: Boolean,
    watchPending: String?,
    watchEnabled: Boolean,
    watch: () -> Unit,
    record: () -> Unit,
    chart: () -> Unit,
    review: (() -> Unit)?,
) {
    val strategies = candidate.stringList("strategies")
    val change = candidate.amount("changeRatio")
    val risk = candidate.text("riskStatus")
    DesignCard {
        MarketValueHeading(
            name = candidate.value("name").ifBlank { candidate.value("symbol") },
            symbol = candidate.value("symbol"),
            amount = marketPercent(change),
            secondary = displayNumber(candidate.amount("lastPrice"), 2),
            tint = signTint(change),
            inlineSymbol = true,
            badge = {
                StatusBadge("综合评分 ${displayNumber(candidate.amount("score"))} · ${strategies.size} 策略", riskTint(risk))
            },
        )
        if (strategies.isNotEmpty()) {
            FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                strategies.forEach { strategy ->
                    Surface(
                        color = DeskColors.blue.copy(alpha = 0.10f),
                        shape = CircleShape,
                        border = BorderStroke(1.dp, DeskColors.blue.copy(alpha = 0.22f)),
                    ) {
                        MarketText(strategy, color = Color(0xFFBFDBFE),
                            modifier = Modifier.padding(horizontal = 9.dp, vertical = 3.dp))
                    }
                }
            }
        }
        MarketStatGrid(
            listOf(
                "量比" to displayNumber(candidate.amount("volumeRatio")),
                "总市值" to marketCap(candidate.amount("marketCap")),
                "截面时间" to quoteTime(candidate.number("dataAsOf")),
            ),
        )
        if (risk != "none") MarketText(marketRisk(risk), color = riskTint(risk))
        HorizontalDivider(color = DeskColors.border.copy(alpha = 0.65f))
        FlowRow(
            Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalArrangement = Arrangement.spacedBy(0.dp),
        ) {
            MarketLink(
                watchPending ?: if (watched) "✓ 已在自选 · 取消" else "＋ 加入自选监控",
                enabled = watchEnabled,
                modifier = Modifier.semantics { selected = watched },
                onClick = watch,
            )
            review?.let { MarketLink("查看核验事项 →", onClick = it) }
            MarketLink("买入成交记录 / 回填 →", onClick = record)
            MarketLink("K 线", onClick = chart)
        }
    }
}

@Composable
internal fun PortfolioHeading(go: (String, String) -> Unit, pickHolding: () -> Unit = {}) {
    MarketHeader("持仓", "本地持仓台账 · 风险与卖点监控") {
        Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            TextButton(onClick = { go("trade_review", "") }, modifier = Modifier.heightIn(min = 48.dp)) {
                Text("成交复盘")
            }
            ImportHoldingButton("截图导入", pickHolding)
        }
    }
}

@Composable
internal fun DesignedPortfolioPage(state: WorkbenchProjection, go: (String, String) -> Unit, pickHolding: () -> Unit = {}) {
    val valuation = state.meta?.obj("valuation")
    val (positions, closed) = state.rows("positions").sortedBy { it.text("symbol") }
        .partition { (it.number("qty") ?: 0) > 0 }
    val adjustments = state.rows("adjustments")
    val latestRegistration = remember(adjustments) {
        adjustments.groupBy { it.text("symbol") }.mapValues { (_, rows) ->
            rows.mapNotNull { it.number("asOf") }.maxOrNull()
        }
    }
    val reviewCutoff = System.currentTimeMillis() - Duration.ofDays(30).toMillis()
    val activeTasks = state.rows("tasks").filter {
        it.text("direction") in setOf("sell", "risk") && it.activeTask()
    }.groupBy { it.text("symbol") }
    val analysis = state.meta?.obj("analysis")
    val monitoring = state.connection.localOnline && analysis != null &&
        analysis.flag("paused") == false && analysis.text("kernelStatus") == "online"
    val marketValue = valuation?.amount("positionMarketValue")
    val referenceTotal = if (marketValue == null) positions.referenceMarketValue() else null
    val pnl = valuation?.amount("unrealizedPnl")
    val cost = if (marketValue != null && pnl != null) marketValue - pnl else null
    val pnlRatio = if (pnl != null && cost != null && cost.signum() > 0) pnl.divide(cost, MathContext.DECIMAL64) else null
    val pendingBySymbol = state.pendingPositionExecutions().groupBy { it.value("symbol") }
    val namedPositions = (positions + closed).associateBy { it.text("symbol") }
    val recent = state.intents.filter { it.command in setOf("act", "record_execution", "adjust_position") }.takeLast(5)
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        MarketSummaryCard(portfolio = true) {
            val covered = valuation?.number("coveredCount")
            val total = valuation?.number("totalCount")
            val (coverageLabel, coverageTint) = when {
                covered == null || total == null -> "实时估值覆盖未知" to DeskColors.muted
                covered < total -> "实时估值 $covered / $total" to DeskColors.amber
                total == 0L -> "暂无持仓" to DeskColors.muted
                else -> "实时估值 $covered / $total" to DeskColors.green
            }
            FlowRow(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalArrangement = Arrangement.spacedBy(5.dp),
            ) {
                MarketText(if (referenceTotal == null) "持仓市值估算（CNY）" else "参考持仓市值（CNY）", color = DeskColors.secondary)
                StatusBadge(coverageLabel, coverageTint)
            }
            Text(
                (marketValue ?: referenceTotal?.amount)?.let { "¥ ${displayNumber(it, 2)}" } ?: "未知",
                style = MaterialTheme.typography.displaySmall.copy(letterSpacing = (-0.8).sp),
                fontWeight = FontWeight.Bold,
            )
            FlowRow(horizontalArrangement = Arrangement.spacedBy(10.dp), verticalArrangement = Arrangement.spacedBy(3.dp)) {
                MarketText("浮动盈亏估算")
                MarketText(displayNumber(pnl, 2, plus = true), color = signTint(pnl), bold = true, numeric = true)
                if (pnlRatio != null) MarketText(marketPercent(pnlRatio), color = signTint(pnlRatio), bold = true, numeric = true)
            }
            Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                if (referenceTotal != null) {
                    MarketText("参考行情最早 ${timeText(referenceTotal.asOf)}")
                } else MarketText("行情截面 ${timeText(valuation?.number("asOf"))}")
                MarketText("按登记持仓与有价行情估算，未计现金及交易费用。")
            }
            val unpriced = valuation?.stringList("unpricedSymbols").orEmpty()
            if (referenceTotal == null && unpriced.isNotEmpty()) {
                val subtotal = valuation?.amount("pricedSubtotal")
                MarketText("缺可估值行情 ${unpriced.size} 行 · 有价小计 ¥ ${displayNumber(subtotal, 2)}", color = DeskColors.amber)
            }
            PositionAllocation(positions)
        }
        if (pendingBySymbol.isNotEmpty()) DesignCard {
            SectionTitle("成交待对账")
            for ((symbol, executions) in pendingBySymbol.toSortedMap()) {
                val name = namedPositions[symbol]?.text("name")?.takeUnless { it == symbol }
                    ?: state.reconciliationSecurityName(symbol, executions)
                val label = listOfNotNull(name, symbol, "${executions.size} 笔待对账").joinToString(" · ")
                MarketLink(label, enabled = state.meta?.number("positionRevision") != null) {
                    go("adjust_reconcile", symbol)
                }
            }
        }
        MarketActionPair(
            first = { MarketButton("截图导入", MarketIcon.Image, enabled = state.canQueue && state.holdingImport?.terminal != false, onClick = pickHolding) },
            second = {
                MarketButton("手动新增持仓", MarketIcon.Plus, enabled = state.meta?.number("positionRevision") != null) {
                    go("adjust", "")
                }
            },
        )
        Column(verticalArrangement = Arrangement.spacedBy(9.dp)) {
            SectionTitle(
                if (state.meta == null) "当前持仓明细" else "当前持仓明细 · ${positions.size} 只",
                if (monitoring) "风险 / 卖点监控运行中" else null,
            )
            if (positions.isEmpty()) {
                DesignCard { MarketText(if (state.meta == null) "尚未同步持仓" else "工作台无已记录持仓") }
            }
            positions.forEach { position ->
                val symbol = position.text("symbol")
                val task = activeTasks[symbol]?.maxWithOrNull(
                    compareBy<JsonObject> { when (it.text("level")) { "critical" -> 2; "warn" -> 1; else -> 0 } }
                        .thenBy { it.number("createdAt") ?: 0L },
                )
                val registeredAt = latestRegistration[symbol] ?: position.number("asOf")
                key(symbol) {
                    HoldingCard(position, task, monitoring, state.canQueue && state.meta?.number("positionRevision") != null, go,
                        registeredAt = registeredAt, reviewDue = registeredAt != null && registeredAt <= reviewCutoff)
                }
            }
        }
        if (closed.isNotEmpty()) {
            Column(verticalArrangement = Arrangement.spacedBy(9.dp)) {
                SectionTitle("已清仓记录 · ${closed.size}")
                closed.forEach { position ->
                    key(position.text("symbol")) {
                        HoldingCard(position, null, false, state.canQueue && state.meta?.number("positionRevision") != null, go)
                    }
                }
            }
        }
        FlowRow(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            MarketLink("成交记录 / 独立补录") { go("execution", "") }
            MarketLink("持仓估值明细") { go("valuation", "") }
        }
        if (recent.isNotEmpty()) {
            DesignCard {
                SectionTitle("最近提交")
                recent.forEach { IntentText(it) }
            }
        }
    }
}

private fun WorkbenchProjection.pendingPositionExecutions() =
    rows("executions").filter { it.text("positionEffect") == "needs_reconciliation" }
        .sortedByDescending { it.number("executedAt") }

private fun WorkbenchProjection.reconciliationSecurityName(symbol: String, executions: List<JsonObject>): String? =
    (rows("watchlist").asSequence() + executions.asSequence().mapNotNull { execution ->
        execution.text("taskId")?.let { task(it) }
    }).filter { it.text("symbol") == symbol }
        .firstNotNullOfOrNull { it.text("name")?.takeIf { name -> name.isNotBlank() && name != symbol } }

@Composable
private fun PositionAllocation(positions: List<JsonObject>) {
    val amounts = positions.mapNotNull { position ->
        val amount = position.amount("marketValue")?.takeIf { it.signum() > 0 } ?: return@mapNotNull null
        position.value("name").ifBlank { position.value("symbol") } to amount
    }.sortedByDescending { it.second }
    if (amounts.isEmpty()) return
    val subtotal = amounts.sumOf { it.second }
    val legend = if (amounts.size <= 3) amounts else amounts.take(3) + ("其他" to amounts.drop(3).sumOf { it.second })
    val gradients = listOf(
        listOf(DeskColors.red, DeskColors.redBright),
        listOf(Color(0xFF3B82F6), DeskColors.blue),
        listOf(DeskColors.amber, Color(0xFFFBBF24)),
        listOf(DeskColors.muted, DeskColors.muted),
    )
    val allocations = legend.map { (name, amount) -> name to amount.divide(subtotal, MathContext.DECIMAL64) }
    Row(
        Modifier.fillMaxWidth().height(7.dp).clip(CircleShape)
            .background(Color.White.copy(alpha = 0.05f)),
        horizontalArrangement = Arrangement.spacedBy(4.dp),
    ) {
        allocations.forEachIndexed { index, (_, fraction) ->
            Box(Modifier.weight(fraction.toFloat().coerceAtLeast(0.0001f)).fillMaxHeight()
                .background(Brush.horizontalGradient(gradients[index]), CircleShape))
        }
    }
    FlowRow(
        Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(10.dp),
        verticalArrangement = Arrangement.spacedBy(3.dp),
    ) {
        allocations.forEach { (name, fraction) ->
            val percent = displayNumber(fraction.movePointRight(2).setScale(1, java.math.RoundingMode.HALF_UP), 1)
            MarketText("$name $percent%")
        }
    }
}

@Composable
private fun HoldingCard(
    position: JsonObject,
    task: JsonObject?,
    monitoring: Boolean,
    canEdit: Boolean,
    go: (String, String) -> Unit,
    registeredAt: Long? = null,
    reviewDue: Boolean = false,
) {
    val symbol = position.value("symbol")
    val hasHolding = (position.number("qty") ?: 0) > 0
    val pnl = position.amount("unrealizedPnl")
    val marketValue = position.amount("marketValue")
    val referenceMarketValue = if (marketValue == null) position.referenceMarketValue()?.amount else null
    DesignCard {
        MarketValueHeading(position.value("name").ifBlank { symbol }, symbol,
            displayNumber(pnl, 2, plus = true), marketPercent(position.amount("unrealizedPnlRatio")), signTint(pnl),
            tintSecondary = true)
        MarketStatGrid(
            listOf(
                "登记股数 / 台账可用" to "${position.value("qty")} / ${position.value("availableQty")}",
                "登记成本" to displayNumber(position.amount("cost"), 2),
                "行情参考价" to displayNumber(position.amount("lastPrice"), 2),
                (if (referenceMarketValue == null) "市值估算" else "参考市值") to displayNumber(marketValue ?: referenceMarketValue),
            ),
            grouped = true,
        )
        FlowRow(
            Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalArrangement = Arrangement.spacedBy(5.dp),
        ) {
            if (task != null) {
                val level = task.text("level")
                val direction = if (task.text("direction") == "sell") "卖点" else "风险"
                val stop = task.amount("stopPrice")?.let { " · 止损位 ${displayNumber(it, 2)}" }.orEmpty()
                StatusBadge(
                    "${if (level == "critical") "紧急" else ""}$direction · ${taskState(task)}$stop",
                    when (level) { "critical" -> DeskColors.red; "warn" -> DeskColors.amber; else -> DeskColors.blue },
                )
            } else if (monitoring) {
                StatusBadge("风险 / 卖点监控中", DeskColors.green)
            }
            if (hasHolding) {
                MarketText("登记时点 ${registeredAt?.let { holdingDate.format(Instant.ofEpochMilli(it)) } ?: "未知"}")
                if (reviewDue) StatusBadge("建议校对", DeskColors.amber)
            }
            if (position.flag("reconciliationRequired") == true) StatusBadge("待对账", DeskColors.amber)
            when (position.text("priceStatus")) {
                "stale" -> StatusBadge("行情已过期 · ${timeText(position.number("priceAsOf"))}", DeskColors.amber)
                "missing" -> StatusBadge("行情待同步", DeskColors.muted)
                "fresh" -> MarketText(quoteTime(position.number("priceAsOf")), numeric = true)
                else -> MarketText("行情状态未知")
            }
            if (task != null) {
                val label = when {
                    task.text("direction") != "sell" -> "处理风险事项 →"
                    task.amount("stopPrice") != null -> "处理止损事项 →"
                    else -> "处理卖点事项 →"
                }
                task.text("taskId")?.let { id -> MarketLink(label) { go("task", id) } }
            }
        }
        FlowRow(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            MarketLink("更正持仓", enabled = canEdit) { go("adjust", symbol) }
            MarketLink("持仓明细") { go("holding", symbol) }
            if (hasHolding) MarketLink("量化与 AI") { go("analyze", symbol) }
            MarketLink("查看 K 线") { go("chart", symbol) }
        }
    }
}

@Composable
internal fun DesignedAdjustPositionForm(
    state: WorkbenchProjection,
    symbol: String,
    model: WorkbenchViewModel,
    busy: Boolean,
    back: () -> Unit,
    reconcile: Boolean = false,
) {
    HoldingPositionForm(state, symbol, busy, back, reconcile, model::refreshPositions, model::submitWithReceipt)
}

@Composable
internal fun HoldingPositionForm(
    state: WorkbenchProjection, symbol: String, busy: Boolean, back: () -> Unit,
    reconcile: Boolean = false, refresh: () -> Unit = {},
    submit: (String, JsonObject, (String) -> Unit) -> Unit,
) {
    val creating = symbol.isEmpty()
    val positions = state.rows("positions")
    val position = positions.firstOrNull { it.text("symbol") == symbol }
    val currentRevision = state.meta?.number("positionRevision")
    if (!creating && !reconcile && position == null) {
        NoticeStrip("当前快照中没有该持仓。", DeskColors.amber)
        return
    }
    var base by rememberSaveable(state.pairId, state.epoch, symbol) { mutableStateOf(currentRevision) }
    var enteredSymbol by rememberSaveable(symbol) { mutableStateOf(symbol) }
    var quantity by rememberSaveable(symbol) { mutableStateOf(position?.number("qty")?.toString().orEmpty()) }
    var available by rememberSaveable(symbol) { mutableStateOf(position?.number("availableQty")?.toString().orEmpty()) }
    var cost by rememberSaveable(symbol) { mutableStateOf(position?.text("cost").orEmpty()) }
    var reason by rememberSaveable(symbol) { mutableStateOf("") }
    var zero by rememberSaveable(symbol) { mutableStateOf(false) }
    var selectedSymbol by rememberSaveable(state.pairId, state.epoch, symbol) { mutableStateOf(if (creating) "" else symbol) }
    var selectedName by rememberSaveable(state.pairId, state.epoch, symbol) { mutableStateOf(position?.text("name").orEmpty()) }
    var requestId by rememberSaveable(state.pairId, state.epoch, symbol) { mutableStateOf<String?>(null) }
    var submitted by rememberSaveable(state.pairId, state.epoch, symbol) { mutableStateOf<String?>(null) }
    var selectedExecutions by rememberSaveable(state.pairId, state.epoch, symbol) { mutableStateOf(arrayListOf<String>()) }
    val searchQuery = enteredSymbol.trim()
    LaunchedEffect(searchQuery, selectedSymbol, state.pairId, state.epoch, state.connection.localOnline) {
        requestId = null
        if (creating && selectedSymbol.isEmpty() && searchQuery.isNotEmpty() && state.connection.localOnline) {
            delay(300)
            submit("search_symbols", buildJsonObject { put("query", searchQuery) }) { requestId = it }
        }
    }
    val search = state.intents.firstOrNull { it.id == requestId && it.data.text("query") == searchQuery }
    val saving = state.intents.firstOrNull { it.id == submitted }
    val completedAtEntry = remember(state.pairId, state.epoch, symbol) {
        saving?.takeIf { it.stage == DeliveryStage.APPLIED }?.id
    }
    LaunchedEffect(saving?.id, saving?.stage) {
        if (saving?.stage == DeliveryStage.APPLIED) {
            base = currentRevision
            enteredSymbol = symbol
            quantity = position?.number("qty")?.toString().orEmpty()
            available = position?.number("availableQty")?.toString().orEmpty()
            cost = position?.text("cost").orEmpty()
            reason = ""
            zero = false
            selectedSymbol = symbol
            selectedName = position?.text("name").orEmpty()
            selectedExecutions = arrayListOf()
            requestId = null
            submitted = null
            if (saving.id != completedAtEntry) back()
        }
    }
    val normalizedSymbol = selectedSymbol
    val pending = state.pendingPositionExecutions().filter { it.text("symbol") == normalizedSymbol }
    val reconciliationName = if (reconcile && position == null) state.reconciliationSecurityName(normalizedSymbol, pending) else null
    val pendingIds = pending.mapNotNull { it.text("executionId") }
    val resolvedIds = if (reconcile) selectedExecutions.filter { it in pendingIds } else emptyList()
    val awaiting = (submitted != null && (saving == null || saving.awaitingExecutionOutcome())) ||
        (normalizedSymbol.isNotEmpty() && state.intents.any {
            it.command == "adjust_position" && it.data.text("symbol") == normalizedSymbol && it.awaitingExecutionOutcome()
        })
    val existing = creating && positions.any { it.text("symbol") == normalizedSymbol }
    val qty = whole(quantity, positive = creating)
    val availableQty = whole(available)
    val conflict = saving?.errorCode == "POSITION_VERSION_CONFLICT"
    val current = base != null && base == currentRevision && !conflict
    DesignCard {
        SectionTitle(when {
            creating -> "手动新增持仓"
            reconcile -> "成交对账 · ${position?.value("name") ?: reconciliationName ?: symbol}"
            else -> position?.value("name") ?: symbol
        })
        MarketText(if (creating) "已记录持仓口径" else "${tickerLabel(symbol)} · 当前已确认版本 ${base ?: "未知"}")
        if (creating) {
            Field("证券名称或代码", enteredSymbol, {
                enteredSymbol = it.take(100); selectedSymbol = ""; selectedName = ""; requestId = null
            }, error = if (existing) "已有该标的，请从持仓列表打开更正。" else null)
            if (selectedSymbol.isNotEmpty()) MarketText("已选择 $selectedName · $selectedSymbol")
            else {
                if (searchQuery.isNotEmpty() && search?.result == null) MarketText(if (state.connection.localOnline) "正在查询证券…" else "连接工作台后可查询证券")
                search?.result?.objects("items")?.filter { item ->
                    item.text("instrument") in setOf("stock", "etf") && symbolPattern.matches(item.text("symbol").orEmpty()) &&
                        !item.text("name").isNullOrBlank()
                }?.forEach { item ->
                    TextButton(onClick = {
                        selectedSymbol = item.text("symbol")!!; selectedName = item.text("name")!!
                        enteredSymbol = selectedName
                    }, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp), enabled = !awaiting) {
                        Text("${item.value("name")} · ${item.value("symbol")}")
                    }
                }
                if (search?.result?.objects("items")?.isEmpty() == true) MarketText("没有匹配的证券")
                search?.takeIf { it.stage == DeliveryStage.REJECTED }?.let { IntentText(it) }
            }
        }
        Field(if (creating) "登记股数" else "更正后股数", quantity, { quantity = it }, KeyboardType.Number,
            error = "请输入${if (creating) "正" else "非负"}整数。".takeIf { quantity.isNotBlank() && qty == null })
        Field("可用数量（留空为未知）", available, { available = it }, KeyboardType.Number,
            error = "可用数量须为非负整数且不超过总仓。"
                .takeIf { available.isNotBlank() && (availableQty == null || qty != null && availableQty > qty) })
        Field("登记平均成本", cost, { cost = it }, KeyboardType.Decimal,
            error = "请输入有效成本。".takeIf { cost.isNotBlank() && decimal(cost) == null })
        if (!creating && !reconcile) Field("更正原因（选填）", reason, { reason = it.take(500) })
        if (qty == 0L) CheckRow("确认将数量更正为零", zero) { zero = it }
    }
    if (reconcile && pending.isNotEmpty()) DesignCard {
        SectionTitle("待对账成交")
        MarketText("填入券商当前的持仓快照，勾选已包含在该快照中的成交。其余成交继续待对账。")
        for (execution in pending) {
            val id = execution.text("executionId") ?: continue
            val direction = if (execution.text("side") == "buy") "买入" else "卖出"
            val qtyText = execution.value("qty")
            val price = execution.value("price")
            val at = timeText(execution.number("executedAt"))
            val order = execution.text("orderId")?.let { " · 委托 $it" }.orEmpty()
            CheckRow("$direction $qtyText 股 · $price 元 · $at$order", id in resolvedIds) { checked ->
                if (!awaiting) selectedExecutions = ArrayList(if (checked) resolvedIds + id else resolvedIds - id)
            }
        }
    } else if (reconcile && !awaiting) {
        NoticeStrip("此标的已无待对账成交。", DeskColors.green)
    } else if (!reconcile && pending.isNotEmpty()) {
        NoticeStrip("另有 ${pending.size} 笔成交待对账，可从持仓页按券商快照核对。", DeskColors.amber)
    }
    if (!current && !awaiting) DesignCard {
        SectionTitle("重新核对持仓版本")
        val latest = positions.firstOrNull { it.text("symbol") == normalizedSymbol }
        MarketText("打开表单时版本 ${base ?: "未知"} · 最新快照版本 ${currentRevision ?: "未知"}")
        MarketText(if (latest == null) "最新快照中无该持仓。" else
            "最新快照 · 持仓 ${latest.value("qty")} 股 · 可用 ${latest.value("availableQty")} · 成本 ${latest.value("cost")}")
        MarketText("当前填写 · 持仓 ${quantity.ifBlank { "未填写" }} · 可用 ${available.ifBlank { "未知" }} · 成本 ${cost.ifBlank { "未填写" }}")
        TextButton(onClick = refresh, enabled = state.connection.localOnline && !busy,
            modifier = Modifier.heightIn(min = 48.dp)) { Text("刷新持仓快照") }
        if (currentRevision != null && currentRevision != base) {
            TextButton(onClick = {
                base = currentRevision
                submitted = null
                selectedExecutions = arrayListOf()
            }, enabled = !busy && state.canQueue, modifier = Modifier.heightIn(min = 48.dp)) { Text("已核对最新快照") }
        }
    }
    val valid = symbolPattern.matches(normalizedSymbol) && !existing && qty != null && (qty != 0L || zero) &&
        (available.isBlank() || availableQty != null && availableQty <= qty) && decimal(cost) != null &&
        (!reconcile || resolvedIds.isNotEmpty())
    saving?.let { IntentText(it) }
    val action = when {
        awaiting -> "正在保存持仓…"
        creating -> "保存持仓"
        reconcile -> "确认成交对账"
        else -> "提交台账更正"
    }
    MainAction(action, state.canQueue && !busy && !awaiting && current && valid) {
        submit("adjust_position", buildJsonObject {
            put("symbol", normalizedSymbol)
            (if (creating) selectedName else reconciliationName)?.let { put("name", it) }
            put("basePositionRevision", base!!)
            put("qty", qty!!)
            put("availableQty", availableQty?.let(::JsonPrimitive) ?: JsonNull)
            put("cost", cost.trim())
            put("asOf", System.currentTimeMillis())
            put("reason", when {
                creating -> "手动添加持仓"
                reconcile -> "按券商持仓快照确认成交"
                else -> reason.trim().ifBlank { "手动核对持仓" }
            })
            put("resolvedExecutionIds", JsonArray(resolvedIds.map(::JsonPrimitive)))
            put("pendingLots", JsonNull)
            if (qty == 0L) put("confirmZero", true)
        }) { submitted = it }
    }
}

@Composable
private fun MarketHeader(title: String, subtitle: String, action: @Composable () -> Unit) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(title, Modifier.semantics { heading() }, style = MaterialTheme.typography.headlineLarge)
            Text(subtitle, style = MaterialTheme.typography.bodyMedium.copy(fontSize = 13.sp), color = DeskColors.muted)
        }
        action()
    }
}

@Composable
private fun ImportHoldingButton(label: String, onClick: () -> Unit) {
    val shape = RoundedCornerShape(12.dp)
    IconButton(
        onClick = onClick,
        modifier = Modifier.size(48.dp).clip(shape)
            .background(Color.White.copy(alpha = 0.04f))
            .border(1.dp, DeskColors.border, shape)
            .semantics { contentDescription = label },
    ) {
        MarketGlyph(MarketIcon.Camera, DeskColors.muted)
    }
}

@Composable
private fun MarketSummaryCard(portfolio: Boolean = false, content: @Composable ColumnScope.() -> Unit) {
    val shape = RoundedCornerShape(if (portfolio) 20.dp else 18.dp)
    val outline = if (portfolio) Color.White.copy(alpha = 0.12f) else DeskColors.blue.copy(alpha = 0.27f)
    val start = if (portfolio) Color(0xFF1B253B) else Color(0xFF182236)
    val end = if (portfolio) Color(0xFF0F1523) else Color(0xFF0F1522)
    Surface(shape = shape, border = BorderStroke(1.dp, outline), color = end, contentColor = DeskColors.text) {
        Column(
            Modifier.fillMaxWidth()
                .drawWithCache {
                    val base = Brush.linearGradient(
                        listOf(start, end),
                        start = Offset.Zero,
                        end = Offset(size.width, size.height),
                    )
                    val upper = Brush.radialGradient(
                        listOf((if (portfolio) DeskColors.red else DeskColors.blue).copy(alpha = 0.20f), Color.Transparent),
                        center = Offset(size.width * 0.92f, size.height * 0.12f),
                        radius = (size.width * 0.64f).coerceAtLeast(1f),
                    )
                    val lower = Brush.radialGradient(
                        listOf(DeskColors.blue.copy(alpha = 0.16f), Color.Transparent),
                        center = Offset(size.width * 0.12f, size.height * 0.90f),
                        radius = (size.width * 0.65f).coerceAtLeast(1f),
                    )
                    onDrawBehind {
                        drawRect(base)
                        if (portfolio) drawRect(lower)
                        drawRect(upper)
                    }
                }
                .padding(horizontal = if (portfolio) 17.dp else 16.dp, vertical = if (portfolio) 18.dp else 15.dp),
            verticalArrangement = Arrangement.spacedBy(if (portfolio) 9.dp else 7.dp),
            content = content,
        )
    }
}

@Composable
private fun MarketValueHeading(
    name: String,
    symbol: String,
    amount: String,
    secondary: String,
    tint: Color,
    tintSecondary: Boolean = false,
    inlineSymbol: Boolean = false,
    badge: (@Composable () -> Unit)? = null,
) {
    BoxWithConstraints(Modifier.fillMaxWidth()) {
        val stacked = maxWidth / LocalDensity.current.fontScale < 195.dp
        val identity: @Composable () -> Unit = {
            Column(verticalArrangement = Arrangement.spacedBy(3.dp)) {
                if (inlineSymbol) {
                    FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        Text(name, Modifier.alignByBaseline(), style = MaterialTheme.typography.titleMedium,
                            fontWeight = FontWeight.Bold)
                        MarketText(symbol, modifier = Modifier.alignByBaseline(), numeric = true)
                    }
                } else {
                    Text(name, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
                    MarketText(symbol, numeric = true)
                }
                badge?.invoke()
            }
        }
        val value: @Composable (TextAlign) -> Unit = { textAlign ->
            Column(horizontalAlignment = if (textAlign == TextAlign.End) Alignment.End else Alignment.Start) {
                Text(amount, style = MaterialTheme.typography.titleMedium.copy(fontFamily = deskMono), color = tint,
                    fontWeight = FontWeight.Bold, textAlign = textAlign)
                MarketText(secondary, color = if (tintSecondary) tint else DeskColors.faint, textAlign = textAlign, numeric = true)
            }
        }
        if (stacked) {
            Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                identity()
                value(TextAlign.Start)
            }
        } else {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Box(Modifier.weight(1f)) { identity() }
                Box(Modifier.weight(0.65f), contentAlignment = Alignment.TopEnd) { value(TextAlign.End) }
            }
        }
    }
}

@Composable
private fun MarketStatGrid(stats: List<Pair<String, String>>, grouped: Boolean = false) {
    val shape = RoundedCornerShape(if (grouped) 12.dp else 10.dp)
    val field = Color(0xFF0A0F18)
    val container = if (grouped) {
        Modifier.fillMaxWidth().background(field, shape).border(1.dp, DeskColors.border, shape).padding(10.dp)
    } else Modifier.fillMaxWidth()
    BoxWithConstraints(container) {
        val effectiveWidth = maxWidth / LocalDensity.current.fontScale
        val columns = when {
            grouped && effectiveWidth >= 284.dp -> 4
            !grouped && effectiveWidth >= 226.dp -> 3
            effectiveWidth >= 160.dp -> 2
            else -> 1
        }
        val spacing = 7.dp
        val tileWidth = (maxWidth - spacing * (columns - 1)) / columns
        FlowRow(
            Modifier.fillMaxWidth(),
            maxItemsInEachRow = columns,
            horizontalArrangement = Arrangement.spacedBy(spacing),
            verticalArrangement = Arrangement.spacedBy(spacing),
        ) {
            stats.forEach { (label, value) ->
                val tile = if (grouped) Modifier.weight(1f) else Modifier.width(tileWidth)
                Column(
                    if (grouped) tile else tile.fillMaxRowHeight()
                        .background(field, shape).border(1.dp, DeskColors.border, shape)
                        .padding(horizontal = 9.dp, vertical = 8.dp),
                    verticalArrangement = Arrangement.spacedBy(2.dp),
                ) {
                    MarketText(label)
                    Text(value, style = MaterialTheme.typography.labelMedium.copy(fontFamily = deskMono),
                        fontWeight = FontWeight.SemiBold)
                }
            }
        }
    }
}

@Composable
private fun MarketActionPair(first: @Composable () -> Unit, second: @Composable () -> Unit) {
    if (LocalDensity.current.fontScale >= 1.5f) {
        Column(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            first()
            second()
        }
    } else {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Box(Modifier.weight(1f)) { first() }
            Box(Modifier.weight(1f)) { second() }
        }
    }
}

@Composable
private fun MarketButton(
    label: String,
    icon: MarketIcon? = null,
    enabled: Boolean = true,
    primary: Boolean = false,
    onClick: () -> Unit,
) {
    val shape = RoundedCornerShape(13.dp)
    val tint = if (!enabled) DeskColors.muted else DeskColors.text
    val fill = when {
        primary && enabled -> listOf(Color(0xFF2F73E7), Color(0xFF2563EB))
        enabled -> listOf(Color(0xFF1E293E), Color(0xFF151D2D))
        else -> listOf(Color(0xFF151B26), Color(0xFF111621))
    }
    Button(
        onClick,
        modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)
            .background(Brush.verticalGradient(fill), shape),
        enabled = enabled,
        shape = shape,
        border = BorderStroke(1.dp, if (primary && enabled) DeskColors.blue.copy(alpha = 0.60f) else DeskColors.border),
        colors = ButtonDefaults.buttonColors(containerColor = Color.Transparent, contentColor = tint,
            disabledContainerColor = Color.Transparent, disabledContentColor = tint),
        contentPadding = PaddingValues(horizontal = 8.dp, vertical = 8.dp),
    ) {
        if (icon != null) {
            MarketGlyph(icon, LocalContentColor.current)
            Spacer(Modifier.width(6.dp))
        }
        Text(label, style = MaterialTheme.typography.bodyMedium, fontWeight = FontWeight.SemiBold, textAlign = TextAlign.Center)
    }
}

@Composable
private fun MarketLink(label: String, modifier: Modifier = Modifier, enabled: Boolean = true, onClick: () -> Unit) {
    TextButton(
        onClick, modifier.sizeIn(minWidth = 48.dp, minHeight = 48.dp), enabled = enabled,
        contentPadding = PaddingValues(horizontal = 3.dp),
    ) {
        Text(label, style = MaterialTheme.typography.bodyMedium, fontWeight = FontWeight.SemiBold)
    }
}

@Composable
private fun MarketText(
    text: String,
    modifier: Modifier = Modifier,
    color: Color = DeskColors.faint,
    bold: Boolean = false,
    textAlign: TextAlign? = null,
    numeric: Boolean = false,
) {
    val style = MaterialTheme.typography.labelSmall
    Text(text, modifier, color = color, style = if (numeric) style.copy(fontFamily = deskMono) else style,
        fontWeight = if (bold) FontWeight.SemiBold else FontWeight.Normal, textAlign = textAlign)
}

private enum class MarketIcon(val path: String) {
    Camera("M4 7h3l2-3h6l2 3h3a2 2 0 0 1 2 2v10H2V9a2 2 0 0 1 2-2z M16 13a4 4 0 1 0-8 0a4 4 0 1 0 8 0"),
    Image("M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z M21 15l-5-5L5 21 M10 8.5a1.5 1.5 0 1 0-3 0a1.5 1.5 0 1 0 3 0"),
    Plus("M12 5v14M5 12h14"),
    Scan("M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M8 21H5a2 2 0 0 1-2-2v-3M16 21h3a2 2 0 0 0 2-2v-3M7 12h10"),
    Refresh("M20 7v5h-5M4 17v-5h5 M6.1 8a7 7 0 0 1 11.8-1L20 12M4 12l2.1 5a7 7 0 0 0 11.8-1"),
}

@Composable
private fun MarketGlyph(icon: MarketIcon, tint: Color) {
    val path = remember(icon) { PathParser().parsePathString(icon.path).toPath() }
    Canvas(Modifier.size(if (icon == MarketIcon.Camera) 21.dp else 16.dp)) {
        withTransform({ scale(size.width / 24f, size.height / 24f, Offset.Zero) }) {
            drawPath(path, tint, style = Stroke(width = 1.7f, cap = StrokeCap.Round, join = StrokeJoin.Round))
        }
    }
}

private val quoteClock = DateTimeFormatter.ofPattern("HH:mm:ss").withZone(shanghai)
private val holdingDate = DateTimeFormatter.ofPattern("yyyy-MM-dd").withZone(shanghai)
private fun quoteTime(at: Long?) = at?.let { quoteClock.format(Instant.ofEpochMilli(it)) } ?: "未知"
private fun JsonObject.amount(key: String) = text(key)?.toBigDecimalOrNull()

internal data class ReferenceMarketValue(val amount: BigDecimal, val asOf: Long)

internal fun List<JsonObject>.referenceMarketValue(): ReferenceMarketValue? {
    val references = mapNotNull { it.referenceMarketValue() }
    if (references.isEmpty() || references.size != size) return null
    return ReferenceMarketValue(references.sumOf { it.amount }, references.minOf { it.asOf })
}

private fun JsonObject.referenceMarketValue(): ReferenceMarketValue? {
    if (flag("reconciliationRequired") != false || text("priceStatus") !in setOf("fresh", "stale")) return null
    val price = amount("lastPrice")?.takeIf { it.signum() > 0 } ?: return null
    val shares = number("qty")?.takeIf { it > 0 } ?: return null
    val at = number("priceAsOf")?.takeIf { it > 0 } ?: return null
    return ReferenceMarketValue(price * BigDecimal.valueOf(shares), at)
}

private fun tickerLabel(symbol: String) = symbol.replace(".", " · ")

private fun displayNumber(value: BigDecimal?, minDecimals: Int = 0, plus: Boolean = false): String {
    if (value == null) return "未知"
    val formatter = DecimalFormat("#,##0", DecimalFormatSymbols(Locale.CHINA)).apply {
        maximumFractionDigits = maxOf(minDecimals, value.stripTrailingZeros().scale())
        minimumFractionDigits = minDecimals
    }
    return (if (plus && value.signum() > 0) "+" else "") + formatter.format(value)
}

private fun marketPercent(ratio: BigDecimal?) =
    ratio?.let { displayNumber(it.movePointRight(2).setScale(2, java.math.RoundingMode.HALF_UP), 2, plus = true) + "%" } ?: "未知"

private fun marketCap(amount: BigDecimal?) = when {
    amount == null -> "未知"
    amount.abs() < BigDecimal("100000000") -> "¥ ${displayNumber(amount)}"
    else -> "${displayNumber(amount.movePointLeft(8).setScale(2, java.math.RoundingMode.HALF_UP))} 亿"
}

private fun signTint(amount: BigDecimal?) = when (amount?.signum()) {
    1 -> DeskColors.red
    -1 -> DeskColors.green
    else -> DeskColors.muted
}

private fun marketProfile(profile: String?) = when (profile) {
    "conservative" -> "保守"
    "aggressive" -> "激进"
    else -> "未知"
}

private fun marketRisk(risk: String?) = when (risk) {
    "none" -> "未标记风险"
    "risk" -> "风险警示"
    else -> "风险状态未知"
}

private fun riskTint(risk: String?) = if (risk == "risk") DeskColors.amber else DeskColors.blue
