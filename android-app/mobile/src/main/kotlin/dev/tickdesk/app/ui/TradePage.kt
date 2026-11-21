package dev.tickdesk.app.ui

import android.app.DatePickerDialog
import android.app.TimePickerDialog
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.relocation.BringIntoViewRequester
import androidx.compose.foundation.relocation.bringIntoViewRequester
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import dev.tickdesk.app.R
import dev.tickdesk.core.*
import kotlinx.serialization.json.*
import java.math.BigDecimal
import java.text.DecimalFormat
import java.text.DecimalFormatSymbols
import java.time.Instant
import java.time.LocalDate
import java.time.LocalTime
import java.util.Locale

@OptIn(ExperimentalLayoutApi::class)
@Composable
internal fun ExecutionForm(
    state: WorkbenchProjection, taskId: String, model: WorkbenchViewModel, busy: Boolean,
    footer: ScreenFooter, prefill: String = "", fromCandidate: Boolean = false,
    onPortfolio: () -> Unit,
) {
    val task = state.task(taskId)
    if (taskId.isNotEmpty() && task == null) {
        NoticeStrip("事项已不可用。已发生的实际成交可从持仓独立补录。", DeskColors.amber)
        return
    }
    val initialSymbol = task?.text("symbol") ?: prefill
    val currentActionVersion = task?.number("actionVersion") ?: -1L
    var baseActionVersion by rememberSaveable(taskId) { mutableLongStateOf(currentActionVersion) }
    val baseStillCurrent = baseActionVersion == currentActionVersion
    var symbol by rememberSaveable { mutableStateOf(initialSymbol) }
    var query by rememberSaveable { mutableStateOf("") }
    var requestedQuery by rememberSaveable { mutableStateOf("") }
    var side by rememberSaveable { mutableStateOf(task?.text("direction")?.takeIf { it in setOf("buy", "sell") } ?: "buy") }
    var price by rememberSaveable { mutableStateOf("") }
    var quantity by rememberSaveable { mutableStateOf("") }
    var date by rememberSaveable { mutableStateOf("") }
    var time by rememberSaveable { mutableStateOf("") }
    var orderId by rememberSaveable { mutableStateOf("") }
    var orderQty by rememberSaveable { mutableStateOf("") }
    var fees by rememberSaveable { mutableStateOf("") }
    var channel by rememberSaveable { mutableStateOf("auction") }
    var average by rememberSaveable { mutableStateOf(false) }
    var submitted by rememberSaveable { mutableStateOf(false) }
    var reviewedRequest by rememberSaveable { mutableStateOf<String?>(null) }
    var receiptId by rememberSaveable { mutableStateOf<String?>(null) }
    val now by deskTime()
    val ts = dateTime(date, time)
    val qty = whole(quantity, positive = true)
    val validPrice = decimal(price, positive = true)
    val code = symbol.trim().uppercase()
    val symbolOk = symbolPattern.matches(code)
    val orderOk = orderId.isBlank() || idPattern.matches(orderId.trim())
    val originalQuantity = whole(orderQty, positive = true)
    val orderQtyOk = orderQty.isBlank() || originalQuantity != null
    val timeOk = ts != null && ts <= now
    val symbolFocus = remember { FocusRequester() }
    val priceFocus = remember { FocusRequester() }
    val quantityFocus = remember { FocusRequester() }
    val timeFocus = remember { FocusRequester() }
    val orderFocus = remember { FocusRequester() }
    val orderQtyFocus = remember { FocusRequester() }
    val sideName = if (side == "buy") "买入" else "卖出"
    val amount = validPrice?.let { p -> qty?.let { p.multiply(BigDecimal.valueOf(it)) } }
    val position = state.rows("positions").firstOrNull { it.text("symbol") == code }
    val rules = state.rows("executions").asSequence().mapNotNull { it.obj("ruleSnapshot") }
        .filter { it.text("symbol") == code && it.text("tradeDate") == date.trim() && it.text("channel") == channel }
        .maxByOrNull { it.number("sourceTime") ?: 0 }
    val lookup = state.intents.lastOrNull {
        it.command == "search_symbols" && it.data.text("query") == requestedQuery
    }
    val security = lookup?.result?.objects("items")?.firstOrNull { it.text("symbol") == code }
    val candidatePage = state.reads["get_candidates"]?.takeIf { fromCandidate }
    val candidate = candidatePage?.objects("items")?.firstOrNull { it.text("symbol") == initialSymbol }
    val scan = candidatePage?.obj("scan")?.takeIf { candidate != null }
    val candidateName by rememberSaveable(initialSymbol, fromCandidate) { mutableStateOf(candidate?.text("name")) }
    val candidateBatch by rememberSaveable(initialSymbol, fromCandidate) { mutableStateOf(scan?.text("scanId")) }
    val candidateAsOf by rememberSaveable(initialSymbol, fromCandidate) {
        mutableStateOf(candidate?.number("dataAsOf") ?: scan?.number("dataAsOf"))
    }
    val candidateSelected = fromCandidate && code == initialSymbol
    val name = task?.text("name") ?: security?.text("name") ?: candidateName?.takeIf { candidateSelected } ?: position?.text("name")
    val valid = symbolOk && orderOk && orderQtyOk && validPrice != null && qty != null && timeOk
    val instrument = rules?.takeIf { it.text("dataStatus") == "complete" }?.text("instrumentType")
        ?: security?.text("instrument")
    val unit = when (instrument) {
        "etf" -> "份"
        "stock" -> "股"
        else -> ""
    }
    val request = if (valid) executionParams(
        code, taskId, baseActionVersion, side, price.trim(), qty, ts, channel, average,
        orderId.trim(), originalQuantity, fees.trim().ifBlank { null },
    ) else null
    val reviewing = request != null && reviewedRequest == request.toString()
    val receipt = state.intents.firstOrNull { it.id == receiptId }
        ?: state.latestExecution(taskId, code)
    val focusManager = LocalFocusManager.current

    if (receipt != null) ExecutionReceipt(state, receipt, reveal = receipt.id == receiptId)

    DesignCard {
        val largeText = LocalDensity.current.fontScale >= 1.5f
        val identity: @Composable () -> Unit = {
            Column(verticalArrangement = Arrangement.spacedBy(3.dp)) {
                FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text(name?.takeIf(String::isNotBlank) ?: if (code.isBlank()) "选择证券" else "证券名称未知",
                        Modifier.align(Alignment.CenterVertically).semantics { heading() },
                        style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
                    if (code.isNotBlank()) Text(code, Modifier.align(Alignment.CenterVertically), color = DeskColors.faint,
                        style = MaterialTheme.typography.labelSmall, fontFamily = deskMono)
                }
                Text("${sideName}成交记录 / 回填 · ${if (task == null) "独立补录" else "关联事项 #$taskId"}",
                    color = DeskColors.muted, style = MaterialTheme.typography.labelSmall)
            }
        }
        val priceSummary: @Composable () -> Unit = {
            val alignment = if (largeText) TextAlign.Start else TextAlign.End
            Column(verticalArrangement = Arrangement.spacedBy(3.dp),
                horizontalAlignment = if (largeText) Alignment.Start else Alignment.End) {
                Text(validPrice?.let { tradeNumber(it.toPlainString()) } ?: "待填写",
                    color = if (validPrice == null) DeskColors.muted else directionColor(side), textAlign = alignment,
                    style = MaterialTheme.typography.titleLarge, fontFamily = if (validPrice == null) null else deskMono,
                    fontWeight = FontWeight.Bold)
                Text(if (average) "多笔成交均价" else "逐笔成交价", style = MaterialTheme.typography.labelSmall,
                    color = DeskColors.muted, textAlign = alignment)
            }
        }
        if (largeText) {
            identity()
            priceSummary()
        } else {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(10.dp), verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.weight(1.45f)) { identity() }
                Box(Modifier.weight(1f), contentAlignment = Alignment.CenterEnd) { priceSummary() }
            }
        }
    }
    val source = when {
        task != null -> "标的来源 · 关联事项 #$taskId"
        candidateSelected -> listOfNotNull("标的来源 · 选股候选 $code", candidateBatch?.let { "批次 $it" },
            candidateAsOf?.let { "快照 ${timeText(it)}" }).joinToString(" · ")
        prefill.isNotBlank() && code == initialSymbol -> "标的来源 · 已选证券 $code"
        else -> "标的来源 · 手动录入"
    }
    Text(source, Modifier.padding(horizontal = 2.dp), color = DeskColors.muted, style = MaterialTheme.typography.labelSmall)
    DesignCard {
        SectionTitle("交易标的", if (task == null) "证券主数据" else "事项关联")
        if (task == null) {
            Field("证券代码（如 600000.SH）", symbol, { symbol = it.take(12) },
                error = when {
                    submitted && !symbolOk -> "请输入完整的沪深证券代码"
                    else -> null
                }, focus = symbolFocus)
            Field("按代码或名称查询证券", query, { query = it.take(80) })
            OutlinedButton(enabled = query.isNotBlank() && state.connection.localOnline && !busy,
                modifier = Modifier.heightIn(min = 48.dp), onClick = {
                requestedQuery = query.trim()
                model.submit("search_symbols", buildJsonObject { put("query", requestedQuery) })
            }) { Text("查询证券主数据") }
            lookup?.result?.objects("items")?.forEach { item ->
                TextButton(enabled = symbolPattern.matches(item.text("symbol").orEmpty()), modifier = Modifier.heightIn(min = 48.dp),
                    onClick = { symbol = item.text("symbol")!! }) {
                    Text("${item.value("name")} · ${item.value("symbol")} · ${item.value("board")}")
                }
            }
            if (lookup?.result?.objects("items")?.isEmpty() == true) Text("没有匹配的证券", color = DeskColors.muted)
            Text("支持沪深主板、创业板股票及场内 ETF 的实际成交回填。",
                color = DeskColors.muted, style = MaterialTheme.typography.labelSmall)
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                ChoiceChip("买入", side == "buy") { side = "buy" }
                ChoiceChip("卖出 / 清仓", side == "sell") { side = "sell" }
            }
        } else {
            Surface(Modifier.fillMaxWidth(), shape = RoundedCornerShape(10.dp), color = DeskColors.field,
                border = BorderStroke(1.dp, DeskColors.border)) {
                Text(listOfNotNull(name?.takeIf(String::isNotBlank), code, rules?.text("board")?.let(::tradeBoard)).joinToString(" · "),
                    Modifier.padding(12.dp), style = MaterialTheme.typography.bodyMedium)
            }
        }
        Spacer(Modifier.height(2.dp))
        Text("成交撮合渠道", style = MaterialTheme.typography.labelLarge)
        TradeChannelPicker(channel) { channel = it }
        val hint = if (channel == "after_hours_fixed") "按实际成交日的盘后固定价格规则核验。"
            else "集合竞价与连续竞价均使用此渠道。"
        Text(hint, style = MaterialTheme.typography.labelSmall, color = DeskColors.muted)
    }
    RuleSection(rules, date)
    DesignCard {
        SectionTitle("实际成交")
        TradeNumberField("实际成交价", price, { price = it.take(48) }, KeyboardType.Decimal, "CNY",
            if (submitted && validPrice == null) "请输入大于零的十进制价格" else null, priceFocus)
        rules?.takeIf { it.text("dataStatus") == "complete" }?.text("minPriceTick")?.let { tick ->
            Text("当日最小变动价位 ${tradeNumber(tick)} 元", color = DeskColors.muted, style = MaterialTheme.typography.labelSmall)
        }
        CheckRow("填入的是多笔平均价", average) { average = it }
        TradeNumberField("实际数量", quantity, { quantity = it.take(16) }, KeyboardType.Number, unit,
            if (submitted && qty == null) "请输入正整数" else null, quantityFocus)
        Text("分笔成交按实际数量录入。", style = MaterialTheme.typography.labelSmall, color = DeskColors.muted)
        Text("实际成交时间 · Asia/Shanghai", style = MaterialTheme.typography.labelLarge)
        Field("上海日期 YYYY-MM-DD", date, { date = it.take(10) })
        Field("上海时间 HH:mm", time, { time = it.take(8) },
            error = if (submitted && !timeOk) "请输入有效且已经发生的成交时间" else null, focus = timeFocus)
        TradeDateTimePicker(date, time, { date = it }, { time = it })
        HorizontalDivider(color = DeskColors.border)
        TradeFieldPair(
            first = {
                Field("原委托编号（可选）", orderId, { orderId = it.take(64) },
                    error = if (submitted && !orderOk) "请输入字母、数字、连字符或下划线" else null, focus = orderFocus)
            },
            second = {
                Field("原委托数量（可选）", orderQty, { orderQty = it.take(16) }, KeyboardType.Number,
                    if (submitted && !orderQtyOk) "请输入正整数" else null, focus = orderQtyFocus)
            },
        )
        OrderRuleStatus(orderId.isNotBlank(), orderQty.isNotBlank(), orderOk && orderQtyOk)
        Field("买卖费用合计（可选，仅用于复盘净额）", fees, { fees = it.take(24) }, KeyboardType.Decimal,
            error = if (fees.isNotBlank() && decimal(fees) == null) "请输入非负金额，或留空表示未知" else null)
        TradeAmountPreview(amount)
    }
    ConfirmedPositionForExecution(position, unit)
    if (!baseStillCurrent && task?.activeTask(now) == true) {
        NoticeStrip("事项版本已更新。请核对当前事项状态，再确认本次成交。", DeskColors.amber)
        TextButton(onClick = {
            reviewedRequest = null
            baseActionVersion = currentActionVersion
        }, modifier = Modifier.heightIn(min = 48.dp)) { Text("重新核对当前事项版本") }
    }
    val requestPending = (receiptId != null && receipt?.id != receiptId) ||
        receipt?.awaitingExecutionOutcome() == true || state.executionUnresolved(taskId, code)
    val duplicate = request != null && state.hasRecordedExecution(code, request)
    val unavailableReason = when {
        receipt?.stage == DeliveryStage.QUARANTINED && !receipt.reviewed -> "先核对原回填结果"
        requestPending -> "已保存 · 等待工作台确认"
        task?.activeTask(now) == false -> "事项已结束，可独立补录"
        task != null && receipt?.executionRecorded() == true -> "此事项已有成交记录"
        !baseStillCurrent -> "事项版本已更新"
        duplicate -> "这笔成交已有记录"
        !state.canQueue -> "同步后可保存"
        else -> null
    }
    val rejected = receipt?.stage == DeliveryStage.REJECTED
    val allowed = unavailableReason == null && !busy
    val action: () -> Unit = {
        if (allowed && task?.activeTask() != false) {
            submitted = true
            if (request != null) {
                focusManager.clearFocus()
                if (!reviewing) reviewedRequest = request.toString()
                else model.submitWithReceipt(if (taskId.isEmpty()) "record_execution" else "act", request) { id ->
                    receiptId = id
                    reviewedRequest = null
                    submitted = false
                }
            } else when {
                !symbolOk && task == null -> symbolFocus.requestFocus()
                validPrice == null -> priceFocus.requestFocus()
                qty == null -> quantityFocus.requestFocus()
                !timeOk -> timeFocus.requestFocus()
                !orderOk -> orderFocus.requestFocus()
                else -> orderQtyFocus.requestFocus()
            }
        }
    }
    if (reviewing && qty != null && validPrice != null) {
        val details = buildList {
            add("交易标的" to listOfNotNull(name, code).joinToString(" · "))
            add("方向 / 渠道" to "$sideName · ${tradeChannelLabel(channel)}")
            val priceBasis = if (average) "成交均价" else "逐笔成交价"
            add("数量 / $priceBasis" to "${quantityText(qty, unit)} @ ${tradeNumber(validPrice.toPlainString())} 元")
            add("成交时间" to "${date.trim()} ${time.trim()} · Asia/Shanghai")
            add("成交金额" to tradeAmountText(amount))
            if (fees.isNotBlank()) add("申报买卖费用" to fees.trim())
            if (orderId.isNotBlank()) add("原委托编号" to orderId.trim())
            if (originalQuantity != null) add("原委托数量" to quantityText(originalQuantity, unit))
        }
        ExecutionReview(details) { reviewedRequest = null }
    }
    footer.ResponsiveAction(
        inline = {
            Column(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(7.dp)) {
                Text(unavailableReason ?: if (rejected && !reviewing) "工作台拒绝 · 核对草稿后可重试"
                    else "保存后等待工作台确认 · 非下单", color = DeskColors.muted,
                    style = MaterialTheme.typography.labelSmall)
                MainAction(if (reviewing) "核对无误 · 写入成交台账" else "保存成交记录 · 非下单",
                    allowed, R.drawable.ic_ultra_check, action)
            }
        },
        shortcut = {
            val context = unavailableReason ?: if (rejected && !reviewing) "工作台拒绝 · 核对草稿后可重试"
                else tradeAmountText(amount, fallback = "待填写价格和数量")
            val label = if (reviewing) "核对无误 · 写入" else "确认写入"
            ContextAction(if (reviewing) "核对成交记录 · 非下单" else "成交金额 · 未含费用", context, label, allowed, action)
        },
    )
    TextButton(onClick = onPortfolio, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) {
        Text("前往持仓总览核对 →")
    }
}

@Composable
private fun ExecutionReview(details: List<Pair<String, String>>, dismiss: () -> Unit) {
    val requester = remember { BringIntoViewRequester() }
    Column(Modifier.fillMaxWidth().bringIntoViewRequester(requester), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        SectionTitle("核对成交记录")
        DesignCard {
            for ((label, value) in details) TradeFact(label, value)
            TextButton(onClick = dismiss, modifier = Modifier.heightIn(min = 48.dp)) {
                Text("返回修改")
            }
        }
    }
    LaunchedEffect(requester) { requester.bringIntoView() }
}

@Composable
internal fun SellExecutionDialog(
    state: WorkbenchProjection, task: JsonObject, model: WorkbenchViewModel, busy: Boolean,
    opened: Boolean, dismiss: () -> Unit,
) {
    val taskId = task.value("taskId")
    val code = task.value("symbol")
    val currentVersion = task.number("actionVersion")!!
    var baseVersion by rememberSaveable(taskId) { mutableStateOf<Long?>(null) }
    var quantity by rememberSaveable(taskId) { mutableStateOf("") }
    var price by rememberSaveable(taskId) { mutableStateOf("") }
    var date by rememberSaveable(taskId) { mutableStateOf("") }
    var time by rememberSaveable(taskId) { mutableStateOf("") }
    var channel by rememberSaveable(taskId) { mutableStateOf("auction") }
    var average by rememberSaveable(taskId) { mutableStateOf(false) }
    var orderId by rememberSaveable(taskId) { mutableStateOf("") }
    var orderQty by rememberSaveable(taskId) { mutableStateOf("") }
    var fees by rememberSaveable(taskId) { mutableStateOf("") }
    var extraFields by rememberSaveable(taskId) { mutableStateOf(false) }
    var attempted by rememberSaveable(taskId) { mutableStateOf(false) }
    var receiptId by rememberSaveable(taskId) { mutableStateOf<String?>(null) }
    if (!opened) return
    val now by deskTime()
    val focusManager = LocalFocusManager.current
    val quantityFocus = remember { FocusRequester() }
    val priceFocus = remember { FocusRequester() }
    val dateFocus = remember { FocusRequester() }
    val orderIdFocus = remember { FocusRequester() }
    val orderQtyFocus = remember { FocusRequester() }
    LaunchedEffect(taskId) {
        if (baseVersion == null) baseVersion = currentVersion
    }
    val position = state.rows("positions").firstOrNull { it.text("symbol") == code }
    val available = position?.number("availableQty")
    val total = position?.number("qty")
    val cost = position?.text("cost")?.let(::tradeNumber)
    val quote = position?.text("lastPrice")?.let(::tradeNumber)
    val stop = task.text("stopPrice")?.let(::tradeNumber)
    val qty = whole(quantity, positive = true)
    val executionPrice = decimal(price, positive = true)
    val at = dateTime(date, time)
    val timeOk = at != null && at <= now
    val originalQty = whole(orderQty, positive = true)
    val orderIdOk = orderId.isBlank() || idPattern.matches(orderId.trim())
    val orderQtyOk = orderQty.isBlank() || originalQty != null
    val amount = if (executionPrice != null && qty != null) executionPrice * BigDecimal.valueOf(qty) else null
    val request = if (baseVersion == currentVersion && qty != null && executionPrice != null &&
        at != null && timeOk && orderIdOk && orderQtyOk) {
        executionParams(code, taskId, currentVersion, "sell", price.trim(), qty, at, channel, average, orderId.trim(), originalQty, fees.trim().ifBlank { null })
    } else null
    val receipt = state.intents.firstOrNull { it.id == receiptId } ?: state.latestExecution(taskId, code)
    val pending = (receiptId != null && receipt?.id != receiptId) || state.taskDecisionUnresolved(taskId)
    val unavailable = when {
        pending -> "等待工作台确认"
        !task.activeTask(now) -> "事项已结束"
        receipt?.executionRecorded() == true -> "此事项已有成交记录"
        baseVersion != currentVersion -> "事项版本已更新"
        request != null && state.hasRecordedExecution(code, request) -> "这笔成交已有记录"
        !state.canQueue -> "同步后可保存"
        else -> null
    }
    val close: () -> Unit = {
        baseVersion = null
        attempted = false
        dismiss()
    }
    AlertDialog(
        onDismissRequest = close,
        title = { Text("卖出成交回填") },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("${task.value("name")} · $code · 关联事项 #$taskId",
                    color = DeskColors.muted, style = MaterialTheme.typography.labelSmall)
                receipt?.let { ExecutionReceipt(state, it, reveal = it.id == receiptId) }
                TradeFact("已同步可用持仓", available?.let { tradeNumber(it.toString()) } ?: "未知")
                TradeFact("总持仓 / 成本价", "${total?.let { tradeNumber(it.toString()) } ?: "未知"} / ${cost?.let { "$it 元" } ?: "未知"}")
                if (quote != null || stop != null) {
                    TradeFact("持仓报价 / 止损位", "${quote?.let { "$it 元" } ?: "未知"} / ${stop?.let { "$it 元" } ?: "未知"}")
                }
                TradeFieldPair(
                    first = {
                        TradeNumberField("实际卖出数量", quantity, { quantity = it.take(16) }, KeyboardType.Number, "",
                            if (attempted && qty == null) "请输入正整数" else null, quantityFocus)
                    },
                    second = {
                        TradeNumberField("实际卖出成交价", price, { price = it.take(48) }, KeyboardType.Decimal, "CNY",
                            if (attempted && executionPrice == null) "请输入大于零的十进制价格" else null, priceFocus)
                    },
                )
                Field("上海日期 YYYY-MM-DD", date, { date = it.take(10) }, focus = dateFocus)
                Field("上海时间 HH:mm", time, { time = it.take(8) },
                    error = if (attempted && !timeOk) "请输入已经发生的有效成交时间" else null)
                TradeDateTimePicker(date, time, { date = it }, { time = it })
                val holdingNote = when {
                    available == null -> "可用持仓未知，持仓变动待工作台核对。"
                    qty != null && qty > available -> "实际数量超过已同步可用持仓，持仓变动待工作台核对。"
                    position?.flag("reconciliationRequired") == true -> "该持仓存在待对账项，持仓变动待工作台核对。"
                    else -> null
                }
                holdingNote?.let { Text(it, color = DeskColors.amber, style = MaterialTheme.typography.labelSmall) }
                Field("买卖费用合计（可选，仅用于复盘净额）", fees, { fees = it.take(24) }, KeyboardType.Decimal,
                    error = if (fees.isNotBlank() && decimal(fees) == null) "请输入非负金额，或留空表示未知" else null)
                TradeAmountPreview(amount)
                TextButton(onClick = { extraFields = !extraFields }, modifier = Modifier.heightIn(min = 48.dp)) {
                    Text(if (extraFields) "收起成交渠道及原委托" else "成交渠道及原委托")
                }
                if (extraFields) {
                    TradeChannelPicker(channel) { channel = it }
                    CheckRow("填入的是多笔平均价", average) { average = it }
                    Field("原委托编号（可选）", orderId, { orderId = it.take(64) },
                        error = if (attempted && !orderIdOk) "请输入字母、数字、连字符或下划线" else null, focus = orderIdFocus)
                    Field("原委托数量（可选）", orderQty, { orderQty = it.take(16) }, type = KeyboardType.Number,
                        error = if (attempted && !orderQtyOk) "请输入正整数" else null, focus = orderQtyFocus)
                }
                Text(unavailable ?: "确认后等待工作台处理 · 非下单", color = DeskColors.muted,
                    style = MaterialTheme.typography.labelSmall)
                if (baseVersion != null && baseVersion != currentVersion && task.activeTask(now)) {
                    TextButton(onClick = { baseVersion = currentVersion }, modifier = Modifier.heightIn(min = 48.dp)) {
                        Text("重新核对当前事项版本")
                    }
                }
            }
        },
        dismissButton = {
            TextButton(onClick = close, modifier = Modifier.heightIn(min = 48.dp)) { Text("返回核对") }
        },
        confirmButton = {
            TextButton(enabled = unavailable == null && !busy, modifier = Modifier.heightIn(min = 48.dp), onClick = {
                if (task.activeTask()) {
                    attempted = true
                    if (request != null) {
                        focusManager.clearFocus()
                        model.submitWithReceipt("act", request) { id ->
                            receiptId = id
                            attempted = false
                        }
                    } else when {
                        qty == null -> quantityFocus.requestFocus()
                        executionPrice == null -> priceFocus.requestFocus()
                        !timeOk -> dateFocus.requestFocus()
                        !orderIdOk && extraFields -> orderIdFocus.requestFocus()
                        !orderQtyOk && extraFields -> orderQtyFocus.requestFocus()
                        else -> extraFields = true
                    }
                }
            }) { Text("确认写入") }
        },
    )
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun TradeFact(label: String, value: String) {
    FlowRow(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween,
        verticalArrangement = Arrangement.spacedBy(2.dp)) {
        Text(label, color = DeskColors.muted, style = MaterialTheme.typography.labelSmall)
        Text(value, style = MaterialTheme.typography.bodySmall, fontWeight = FontWeight.SemiBold)
    }
}

@Composable
private fun ExecutionReceipt(state: WorkbenchProjection, receipt: IntentView, reveal: Boolean) {
    val requester = remember { BringIntoViewRequester() }
    val execution = receipt.result?.obj("execution")
    val executionId = execution?.text("executionId")
    val snapshot = executionId?.let { id -> state.rows("executions").firstOrNull { it.text("executionId") == id } }
    val status = when ((snapshot ?: execution)?.text("positionEffect")) {
        "needs_reconciliation" -> DeliveryStage.NEEDS_RECONCILIATION
        "reconciled" -> DeliveryStage.APPLIED
        else -> receipt.stage
    }
    val tint = when (status) {
        DeliveryStage.APPLIED -> DeskColors.green
        DeliveryStage.REJECTED -> DeskColors.red
        DeliveryStage.NEEDS_RECONCILIATION, DeliveryStage.QUARANTINED -> DeskColors.amber
        else -> DeskColors.blue
    }
    DesignCard(Modifier.bringIntoViewRequester(requester).semantics { liveRegion = LiveRegionMode.Polite }) {
        SectionTitle("最近一次回填")
        StatusBadge(if (snapshot?.text("positionEffect") == "reconciled") "成交已对账" else status.label(), tint)
        receipt.errorCode?.let { Text("工作台反馈 · $it", style = MaterialTheme.typography.labelSmall, color = DeskColors.muted) }
        if ((status == DeliveryStage.APPLIED || status == DeliveryStage.NEEDS_RECONCILIATION) && executionId != null) {
            Text("成交记录号 $executionId", color = DeskColors.muted, style = MaterialTheme.typography.labelSmall)
        }
    }
    LaunchedEffect(receipt.id, reveal) { if (reveal) requester.bringIntoView() }
}

internal fun IntentView.awaitingExecutionOutcome(): Boolean =
    stage in pendingStages || (stage == DeliveryStage.QUARANTINED && !reviewed)

private fun IntentView.executionRecorded(): Boolean =
    stage == DeliveryStage.APPLIED || stage == DeliveryStage.NEEDS_RECONCILIATION

private fun WorkbenchProjection.latestExecution(taskId: String, code: String): IntentView? =
    intents.lastOrNull { intent ->
        if (taskId.isNotEmpty()) intent.command == "act" && intent.data.text("taskId") == taskId &&
            intent.data.text("side") in setOf("buy", "sell")
        else intent.command == "record_execution" && intent.data.text("symbol") == code
    }

private fun WorkbenchProjection.executionUnresolved(taskId: String, code: String): Boolean =
    if (taskId.isNotEmpty()) taskDecisionUnresolved(taskId) else intents.any {
        it.command == "record_execution" && it.data.text("symbol") == code && it.awaitingExecutionOutcome()
    }

private fun WorkbenchProjection.hasRecordedExecution(code: String, request: JsonObject): Boolean {
    val price = request.value("price").toBigDecimal()
    fun matches(item: JsonObject) = item.text("side") == request.text("side") &&
        item.number("qty") == request.number("qty") && item.number("executedAt") == request.number("executedAt") &&
        item.text("price")?.toBigDecimalOrNull()?.compareTo(price) == 0
    val executions = rows("executions")
    return executions.any {
        it.text("symbol") == code && it.text("positionEffect") != "superseded" && matches(it)
    } || intents.any {
        val execution = it.result?.obj("execution")
        it.executionRecorded() && execution != null && execution.text("symbol") == code && matches(execution) &&
            executions.none { row ->
                row.text("executionId") == execution.text("executionId") && row.text("positionEffect") == "superseded"
            }
    }
}

private fun executionParams(
    code: String, taskId: String, baseActionVersion: Long, side: String, price: String, qty: Long,
    executedAt: Long, channel: String, average: Boolean, orderId: String, orderQty: Long?,
    fees: String? = null,
): JsonObject = buildJsonObject {
    if (taskId.isEmpty()) put("symbol", code) else {
        put("taskId", taskId)
        put("baseActionVersion", baseActionVersion)
    }
    put("side", side)
    put("price", price)
    put("qty", qty)
    put("executedAt", executedAt)
    put("priceBasis", if (average) "average" else "single")
    put("channel", channel)
    if (orderId.isNotBlank()) put("orderId", orderId)
    if (orderQty != null) put("orderQty", orderQty)
    fees?.let { put("fees", it) }
}

@Composable
private fun ChoiceChip(label: String, selected: Boolean, onClick: () -> Unit) {
    FilterChip(selected, onClick, { Text(label) }, modifier = Modifier.heightIn(min = 48.dp),
        colors = FilterChipDefaults.filterChipColors(selectedContainerColor = DeskColors.navy,
            selectedLabelColor = MaterialTheme.colorScheme.primary))
}

@Composable
private fun TradeChannelPicker(channel: String, select: (String) -> Unit) {
    var expanded by remember { mutableStateOf(false) }
    val channels = listOf("auction", "after_hours_fixed")
    Box(Modifier.fillMaxWidth()) {
        OutlinedButton(onClick = { expanded = true }, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp),
            shape = RoundedCornerShape(10.dp), border = BorderStroke(1.dp, DeskColors.borderStrong),
            colors = ButtonDefaults.outlinedButtonColors(containerColor = DeskColors.field, contentColor = DeskColors.text),
            contentPadding = PaddingValues(horizontal = 12.dp, vertical = 8.dp)) {
            Text(tradeChannelLabel(channel), Modifier.weight(1f), style = MaterialTheme.typography.bodyMedium)
            Text("⌄", Modifier.clearAndSetSemantics { }, color = DeskColors.muted)
        }
        DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
            channels.forEach { id ->
                DropdownMenuItem(text = { Text(tradeChannelLabel(id)) }, onClick = {
                    select(id)
                    expanded = false
                })
            }
        }
    }
}

private fun tradeChannelLabel(channel: String) =
    if (channel == "after_hours_fixed") "创业板盘后固定价格" else "二级市场连续竞价 / 集合竞价"

@Composable
private fun TradeNumberField(
    label: String, value: String, change: (String) -> Unit, type: KeyboardType, unit: String,
    error: String?, focus: FocusRequester,
) {
    Column(verticalArrangement = Arrangement.spacedBy(5.dp)) {
        Text(label, Modifier.clearAndSetSemantics { }, color = DeskColors.secondary, style = MaterialTheme.typography.labelMedium)
        OutlinedTextField(value, change,
            Modifier.fillMaxWidth().focusRequester(focus).semantics { contentDescription = label },
            textStyle = MaterialTheme.typography.titleMedium.copy(fontFamily = deskMono),
            trailingIcon = if (unit.isNotEmpty()) {
                { Text(unit, Modifier.padding(end = 8.dp), color = DeskColors.muted, style = MaterialTheme.typography.labelSmall) }
            } else null,
            shape = RoundedCornerShape(12.dp),
            colors = OutlinedTextFieldDefaults.colors(focusedContainerColor = DeskColors.field,
                unfocusedContainerColor = DeskColors.field, unfocusedBorderColor = DeskColors.borderStrong),
            supportingText = error?.let { message -> { Text(message) } }, isError = error != null,
            keyboardOptions = KeyboardOptions(keyboardType = type, imeAction = ImeAction.Next), singleLine = true)
    }
}

@Composable
private fun TradeFieldPair(first: @Composable () -> Unit, second: @Composable () -> Unit) {
    BoxWithConstraints(Modifier.fillMaxWidth()) {
        if (maxWidth >= 308.dp && LocalDensity.current.fontScale <= 1.15f) {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Box(Modifier.weight(1f)) { first() }
                Box(Modifier.weight(1f)) { second() }
            }
        } else {
            Column(verticalArrangement = Arrangement.spacedBy(7.dp)) {
                first()
                second()
            }
        }
    }
}

@Composable
private fun OrderRuleStatus(hasId: Boolean, hasQty: Boolean, wellFormed: Boolean) {
    Surface(Modifier.fillMaxWidth(), shape = RoundedCornerShape(12.dp), color = DeskColors.field,
        border = BorderStroke(1.dp, DeskColors.border)) {
        Column(Modifier.padding(horizontal = 11.dp, vertical = 9.dp), verticalArrangement = Arrangement.spacedBy(5.dp)) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = Alignment.CenterVertically) {
                Text("原委托核验", Modifier.weight(1f), color = DeskColors.muted, style = MaterialTheme.typography.labelSmall)
                when {
                    !hasId && !hasQty -> StatusBadge("未填写", DeskColors.muted)
                    !wellFormed -> StatusBadge("格式需核对", DeskColors.amber)
                    else -> StatusBadge("待工作台核验", DeskColors.blue)
                }
            }
            Text("原委托数量与本笔实际成交数量分别记录。", color = DeskColors.muted, style = MaterialTheme.typography.labelSmall)
        }
    }
}

@Composable
private fun TradeAmountPreview(amount: BigDecimal?) {
    val shape = RoundedCornerShape(12.dp)
    Row(Modifier.fillMaxWidth().clip(shape).border(1.dp, DeskColors.blue.copy(alpha = .27f), shape)
        .background(Brush.horizontalGradient(listOf(DeskColors.blue.copy(alpha = .10f), DeskColors.panel)))
        .padding(horizontal = 11.dp, vertical = 10.dp), horizontalArrangement = Arrangement.spacedBy(8.dp),
        verticalAlignment = Alignment.CenterVertically) {
        Text("本笔成交金额（未含费用）", Modifier.weight(1f), color = DeskColors.muted, style = MaterialTheme.typography.labelSmall)
        Text(tradeAmountText(amount),
            Modifier.weight(1f, fill = false), textAlign = TextAlign.End, style = MaterialTheme.typography.titleMedium,
            fontFamily = if (amount == null) null else deskMono, fontWeight = FontWeight.Bold)
    }
}

private fun tradeAmountText(amount: BigDecimal?, fallback: String = "未知") =
    amount?.let { "¥ ${tradeNumber(it.toPlainString(), minimumDecimals = 2)}" } ?: fallback

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun RuleSection(rules: JsonObject?, date: String) {
    var expanded by rememberSaveable { mutableStateOf(false) }
    DesignCard {
        val status = rules?.text("dataStatus")
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(2.dp)) {
                Text("证券当日交易规则快照", Modifier.semantics { heading() }, style = MaterialTheme.typography.labelLarge)
                if (rules != null) Text("交易日 ${rules.value("tradeDate")} · ${timeText(rules.number("sourceTime"))}",
                    color = DeskColors.muted, style = MaterialTheme.typography.labelSmall)
            }
            StatusBadge(when (status) {
                "complete" -> "已载入规则快照"
                "conflict" -> "规则冲突"
                else -> "规则未核验"
            }, if (status == "complete") DeskColors.blue else DeskColors.amber)
        }
        if (rules == null) {
            Text(if (date.isBlank()) "填写实际成交日后查看对应规则。" else "本机没有该日期和渠道的可信规则快照。",
                style = MaterialTheme.typography.labelSmall, color = DeskColors.muted)
        } else {
            FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                StatusBadge("${rules.value("exchange")} · ${tradeBoard(rules.value("board"))}", DeskColors.muted)
                rules.text("settlement")?.takeUnless { it == "UNKNOWN" }?.let { StatusBadge(it) }
                rules.text("minPriceTick")?.let { StatusBadge("最小价位 ${tradeNumber(it)} 元", DeskColors.muted) }
            }
            HorizontalDivider(color = DeskColors.border)
            TextButton(onClick = { expanded = !expanded }, modifier = Modifier.heightIn(min = 48.dp),
                contentPadding = PaddingValues(end = 8.dp)) {
                Text(if (expanded) "收起交易所参数与来源" else "展开交易所参数与来源")
            }
            if (expanded) {
                val details = listOf(
                    "品种 / 板块" to "${rules.value("instrumentType")} / ${tradeBoard(rules.value("board"))}",
                    "当日回转 / 交割" to "${rules.value("dayTrading")} / ${rules.value("settlement")}",
                    "申报单位 / 单笔上限" to "${rules.value("quantityUnit")} / ${rules.value("maxOrderQty")}",
                    "最小价位 / 涨跌幅比例" to "${rules.value("minPriceTick")} / ${rules.value("limitRatio")}",
                    "调整后前收盘价" to rules.value("adjustedPrevClose"), "参考价" to rules.value("referencePrice"),
                    "跌停价 / 涨停价" to "${rules.value("lowerLimit")} / ${rules.value("upperLimit")}",
                    "风险 / 交易状态" to "${rules.value("riskStatus")} / ${rules.value("tradingStatus")}",
                    "规则版本" to rules.value("ruleVersion"), "来源时间" to timeText(rules.number("sourceTime")),
                )
                for (pair in details.chunked(2)) {
                    Row(Modifier.fillMaxWidth().height(IntrinsicSize.Min), horizontalArrangement = Arrangement.spacedBy(7.dp)) {
                        for ((label, value) in pair) {
                            Surface(Modifier.weight(1f).fillMaxHeight(), color = DeskColors.field, shape = RoundedCornerShape(10.dp),
                                border = BorderStroke(1.dp, DeskColors.border)) {
                                Column(Modifier.padding(8.dp), verticalArrangement = Arrangement.spacedBy(3.dp)) {
                                    Text(label, style = MaterialTheme.typography.labelSmall, color = DeskColors.muted)
                                    Text(value, style = MaterialTheme.typography.labelSmall, fontWeight = FontWeight.SemiBold)
                                }
                            }
                        }
                    }
                }
                val issueFields = rules.stringList("issueFields")
                if (issueFields.isNotEmpty()) Text("待核对字段 ${issueFields.joinToString("、")}", color = DeskColors.amber,
                    style = MaterialTheme.typography.labelSmall)
                rules.objects("sources").forEach { SourceText(it) }
            }
        }
    }
}

@Composable
private fun ConfirmedPositionForExecution(position: JsonObject?, unit: String) {
    DesignCard {
        SectionTitle("已同步持仓", timeText(position?.number("asOf")))
        val held = position?.number("qty")?.let { quantityText(it, unit) } ?: "未知"
        val available = position?.number("availableQty")?.let { quantityText(it, unit) } ?: "未知"
        TradeFact("持仓总量", held)
        TradeFact("已确认可卖", available)
        TradeFact("平均成本", position?.text("cost")?.let { tradeNumber(it) } ?: "未知")
        val warning = if (position?.flag("reconciliationRequired") == true) "此证券仍有待对账成交。"
            else "回填后的持仓和可卖数量由工作台核验。"
        Text(warning, color = DeskColors.muted, style = MaterialTheme.typography.labelSmall)
    }
}

private fun tradeBoard(board: String) = when (board) {
    "main" -> "主板"
    "chinext" -> "创业板"
    "fund" -> "场内基金"
    else -> board
}

private fun quantityText(qty: Long, unit: String) = listOf(tradeNumber(qty.toString()), unit).filter(String::isNotEmpty).joinToString(" ")

internal fun tradeNumber(value: String, minimumDecimals: Int = 0): String {
    val number = value.toBigDecimalOrNull() ?: return value
    return DecimalFormat("#,##0", DecimalFormatSymbols.getInstance(Locale.ROOT)).apply {
        maximumFractionDigits = maxOf(number.scale(), minimumDecimals)
        minimumFractionDigits = maxOf(number.scale(), minimumDecimals)
    }.format(number)
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun TradeDateTimePicker(date: String, time: String, changeDate: (String) -> Unit, changeTime: (String) -> Unit) {
    val context = LocalContext.current
    FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        OutlinedButton(modifier = Modifier.heightIn(min = 48.dp), onClick = {
            val initial = runCatching { LocalDate.parse(date) }.getOrDefault(LocalDate.now(shanghai))
            DatePickerDialog(context, { _, year, month, day -> changeDate(LocalDate.of(year, month + 1, day).toString()) },
                initial.year, initial.monthValue - 1, initial.dayOfMonth).show()
        }) { Text("选择日期") }
        OutlinedButton(modifier = Modifier.heightIn(min = 48.dp), onClick = {
            val initial = runCatching { LocalTime.parse(time) }.getOrDefault(LocalTime.now(shanghai))
            TimePickerDialog(context, { _, hour, minute -> changeTime(LocalTime.of(hour, minute).toString()) },
                initial.hour, initial.minute, true).show()
        }) { Text("选择时间") }
    }
}

