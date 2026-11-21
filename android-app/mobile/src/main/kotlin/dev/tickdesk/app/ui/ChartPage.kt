package dev.tickdesk.app.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.gestures.awaitHorizontalTouchSlopOrCancellation
import androidx.compose.foundation.gestures.horizontalDrag
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.listSaver
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.semantics.CustomAccessibilityAction
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.customActions
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.unit.dp
import dev.tickdesk.core.*
import kotlinx.serialization.json.*
import java.math.BigDecimal
import java.math.MathContext
import java.time.Instant
import java.time.format.DateTimeFormatter

private val marketTime = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm").withZone(shanghai)
private fun marketTimeText(at: Long?) = at?.let { marketTime.format(Instant.ofEpochMilli(it)) } ?: "未知"

private enum class ChartTimeframe(val wire: String, val label: String, val minute: Boolean = false) {
    DAY("1d", "日线"), WEEK("1w", "周线"), MONTH("1mo", "月线"),
    MINUTE_1("1m", "1分", true), MINUTE_5("5m", "5分", true);

    fun matches(data: JsonObject): Boolean =
        data.text("timeframe") == wire || (this == DAY && "timeframe" !in data)
}

@Composable
internal fun ChartPage(state: WorkbenchProjection, symbol: String, model: WorkbenchViewModel, busy: Boolean) {
    val serviceStatus = state.reads["get_status"]
    val supportsMarketReads = "get_quote_status" in serviceStatus?.obj("protocol")?.stringList("supportedCommands").orEmpty()
    var selectedTimeframe by rememberSaveable(symbol) { mutableStateOf(ChartTimeframe.DAY) }
    val timeframe = if (supportsMarketReads) selectedTimeframe else ChartTimeframe.DAY
    var adjustment by rememberSaveable(symbol) { mutableStateOf("none") }
    var cursor by rememberSaveable(symbol) { mutableStateOf("") }
    var cursorOwner by rememberSaveable(symbol) { mutableStateOf("") }
    val selection = "${timeframe.wire}:$adjustment"
    val before = cursor.takeIf { cursorOwner == selection }.orEmpty()
    var refresh by remember(symbol) { mutableIntStateOf(0) }
    fun matchesPage(data: JsonObject): Boolean =
        if (timeframe.minute) data.text("cursor").orEmpty() == before
        else data.text("beforeDate").orEmpty() == before
    val requests = state.currentIntents("get_chart").filter {
        it.data.text("symbol") == symbol && timeframe.matches(it.data) &&
            it.data.text("adjustment") == adjustment && matchesPage(it.data)
    }
    val request = requests.lastOrNull()
    fun matchesResult(data: JsonObject) =
        data.text("symbol") == symbol && timeframe.matches(data) && data.text("adjustment") == adjustment
    val result = requests.asReversed().firstNotNullOfOrNull { intent ->
        intent.result?.takeIf(::matchesResult)
    }
    val candles = result?.objects("candles").orEmpty()
    val online = state.connection.localOnline
    val pair = state.pairId
    val epoch = state.epoch
    var selectedDate by rememberSaveable(pair, epoch, symbol, timeframe, adjustment, before) { mutableStateOf<String?>(null) }
    LaunchedEffect(symbol, timeframe, adjustment, before, refresh, online, pair, epoch) {
        if (online && pair != null && epoch != null && request?.stage !in pendingStages) {
            model.readMarket("get_chart", buildJsonObject {
                put("symbol", symbol); put("adjustment", adjustment)
                if (timeframe != ChartTimeframe.DAY) put("timeframe", timeframe.wire)
                if (before.isNotEmpty()) {
                    if (timeframe.minute) put("cursor", before) else put("beforeDate", before)
                }
            }, pair, epoch)
        }
    }
    SectionTitle(symbol, "${timeframe.label} · 只读行情")
    FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        ChartTimeframe.entries.forEach { period ->
            FilterChip(timeframe == period, {
                if (selectedTimeframe != period) cursor = ""
                if (period.minute && adjustment == "forward") adjustment = "none"
                selectedTimeframe = period
            }, label = { Text(period.label) },
                modifier = Modifier.heightIn(min = 48.dp),
                enabled = serviceStatus == null || period == ChartTimeframe.DAY || supportsMarketReads)
        }
    }
    if (serviceStatus != null && !supportsMarketReads) {
        Text("当前工作台仅提供日线。", style = MaterialTheme.typography.bodySmall, color = DeskColors.muted)
    } else if (timeframe.minute && serviceStatus != null && "get_chart" !in
        serviceStatus.obj("protocol")?.stringList("supportedCommands").orEmpty()) {
        Text("当前工作台不支持 K 线读取。", style = MaterialTheme.typography.bodySmall, color = DeskColors.muted)
    }
    FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        listOf("none" to "不复权", "forward" to "前复权").forEach { (code, label) ->
            FilterChip(adjustment == code, {
                if (adjustment != code) cursor = ""
                adjustment = code
            }, label = { Text(label) }, modifier = Modifier.heightIn(min = 48.dp),
                enabled = !(timeframe.minute && code == "forward"))
        }
    }
    if (timeframe.minute) {
        NoticeStrip("分钟线只使用本地已落盘的原始价；没有可靠 5 分钟源时由完整 1 分钟同口径分组，缺输入不会拼成完整 5 分钟。", DeskColors.blue)
    }
    Text("${if (adjustment == "none") "原始价格" else "前复权价格，仅用于指标与趋势查看"} · A 股红涨绿跌",
        style = MaterialTheme.typography.bodySmall, color = DeskColors.muted)
    if (supportsMarketReads) ChartQuote(state, symbol, model, refresh)
    if (!online) NoticeStrip(if (result == null) "本地服务离线，此页暂无 K 线缓存。" else "本地服务离线，显示已读取的 K 线。")
    val issue = when (request?.errorCode) {
        "CHART_PREPARING" -> "工作台正在准备行情。稍后可刷新本页。"
        "CHART_PREPARATION_FAILED" -> "工作台获取行情失败，稍后可刷新重试。"
        "DATA_UNAVAILABLE" -> "行情数据源暂不可用。可检查设置中的数据服务。"
        "CHART_CURSOR_EXPIRED" -> "分钟K线内容或查询已变化，旧游标已失效；请重新读取第一页。"
        null -> request?.result?.takeUnless(::matchesResult)?.let { "工作台返回的证券、周期或复权口径与本页不一致。" }
        else -> "行情读取失败 · ${request.errorCode}"
    }
    if (issue != null) NoticeStrip(issue + if (result != null) " 下方显示上次读取的数据。" else "", DeskColors.amber)
    else if (request?.stage in pendingStages) NoticeStrip("正在读取行情。")
    if (request?.errorCode == "CHART_CURSOR_EXPIRED") {
        TextButton({ cursor = "" }, Modifier.heightIn(min = 48.dp), enabled = online && !busy) {
            Text("重新读取第一页")
        }
    }
    when {
        result == null -> Unit
        result.text("dataStatus") == "missing" -> NoticeStrip("此口径没有可用行情数据。")
        result.text("dataStatus") == "partial" -> NoticeStrip("部分分钟存在重复或缺口，已标为未完整，不冒充终态。", DeskColors.amber)
        result.text("dataStatus") == "stale" -> NoticeStrip("K 线已过期 · ${marketTimeText(result.number("asOf"))}", DeskColors.amber)
        else -> Text("K 线截至 ${marketTimeText(result.number("asOf"))}", color = DeskColors.muted)
    }
    val gaps = result?.array("gaps").orEmpty()
    if (gaps.isNotEmpty()) {
        NoticeStrip(
            "有 ${gaps.size} 个 5 分钟窗口缺少首分钟，未生成 K 线或借用后一分钟价格。",
            DeskColors.amber,
        )
        for (gap in gaps.take(5).mapNotNull { it as? JsonObject }) {
            Text(
                "缺口 ${marketTimeText(gap.number("startAt"))} · 缺少 " +
                    (gap.array("missingMinutes").orEmpty().size) + " 个成分 · 已观测 " +
                    gap.value("observedMinutes") + " 分钟",
                color = DeskColors.muted,
            )
        }
    }
    result?.obj("source")?.let { source -> Text("K 线来源 ${source.value("name")} · ${source.value("ref")}", color = DeskColors.muted) }
    FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        val canRead = online && !busy && request?.stage !in pendingStages
        OutlinedButton({ refresh++ }, Modifier.heightIn(min = 48.dp), enabled = canRead) { Text("刷新本页") }
        if (before.isNotEmpty()) TextButton({ cursor = "" }, Modifier.heightIn(min = 48.dp)) { Text("返回最新") }
        val serverNext = if (timeframe.minute) result?.text("nextCursor")
        else result?.text("nextBeforeDate")?.takeIf { before.isEmpty() || it < before }
        if (result?.flag("hasMore") == true && serverNext != null) {
            TextButton({ cursorOwner = selection; cursor = serverNext }, Modifier.heightIn(min = 48.dp), enabled = canRead) {
                Text(if (timeframe.minute) "下一段${timeframe.label}" else "更早${timeframe.label}")
            }
        }
    }
    if (candles.isNotEmpty()) {
        fun key(row: JsonObject) = if (timeframe.minute) row.number("startAt").toString() else row.text("date")
        fun label(row: JsonObject) = if (timeframe.minute) marketTimeText(row.number("startAt")) else row.value("date")
        val selectedIndex = candles.indexOfFirst { key(it) == selectedDate }.takeIf { it >= 0 } ?: candles.lastIndex
        val candle = candles[selectedIndex]
        DesignCard {
            SectionTitle("所选 ${label(candle)}")
            if (timeframe.minute && candle.flag("complete") == false) {
                NoticeStrip("最后一根尚未完结，仅显示已落盘的片段。", DeskColors.amber)
            }
            FlowRow(horizontalArrangement = Arrangement.spacedBy(14.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                listOf("开" to "open", "高" to "high", "低" to "low", "收" to "close").forEach { (label, key) ->
                    Text("$label ${candle.value(key)}", style = MaterialTheme.typography.bodySmall)
                }
            }
            Text("成交量 ${candle.value("volume")} · 成交额 ${candle.text("amount") ?: "未知"}",
                style = MaterialTheme.typography.bodySmall, color = DeskColors.muted)
            if (timeframe.minute) {
                Text("区间 ${marketTimeText(candle.number("startAt"))} – ${marketTimeText(candle.number("endMs"))} · " +
                    "可得 ${marketTimeText(candle.number("availableAtMs"))} · " +
                    (if (candle.flag("complete") == true) "已完结" else "未完结") +
                    " · " + when (candle.text("quality")) {
                        "duplicate_minute" -> "重复分钟，未采用为终态"
                        "incomplete_group" -> "成分不足，未拼成完整 5 分钟"
                        "incomplete_amount" -> "有成分金额缺失，未伪造成交额"
                        "session_gap" -> "交易时段缺口"
                        else -> "质量正常"
                    },
                    style = MaterialTheme.typography.bodySmall, color = DeskColors.muted)
            }
            CandlePlot(candles, timeframe, selectedIndex) { index -> selectedDate = key(candles[index]) }
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Text(label(candles.first()), style = MaterialTheme.typography.labelSmall)
                if (candles.size > 1) Text(label(candles.last()), style = MaterialTheme.typography.labelSmall)
            }
            Text("轻触或沿图横滑查看", style = MaterialTheme.typography.labelSmall, color = DeskColors.muted)
        }
    } else if (result != null && result.text("dataStatus") != "missing") NoticeStrip("该页没有${timeframe.label}数据。")
}

@Composable
private fun ChartQuote(state: WorkbenchProjection, symbol: String, model: WorkbenchViewModel, refresh: Int) {
    val requests = state.currentIntents("get_quote_status").filter { symbol in it.data.stringList("symbols") }
    val request = requests.lastOrNull()
    val received = requests.asReversed().firstNotNullOfOrNull { intent ->
        val body = intent.result ?: return@firstNotNullOfOrNull null
        body.objects("items").singleOrNull { it.text("symbol") == symbol }?.let { Triple(it, body.text("session"), intent) }
    }
    val online = state.connection.localOnline
    val pair = state.pairId
    val epoch = state.epoch
    LaunchedEffect(symbol, refresh, online, pair, epoch) {
        if (online && pair != null && epoch != null && request?.stage !in pendingStages) {
            model.readMarket("get_quote_status", buildJsonObject { putJsonArray("symbols") { add(symbol) } }, pair, epoch)
        }
    }
    if (received == null) {
        NoticeStrip(when {
            !online -> "工作台离线，暂无报价状态。"
            request?.stage == DeliveryStage.REJECTED -> "报价状态读取失败，可刷新本页重试。"
            else -> "正在读取报价状态。"
        })
        return
    }
    val (quote, session, receivedFrom) = received
    val now by deskTime()
    val labels = listOfNotNull(
        when (session) { "open" -> "交易时段"; "closed" -> "休市"; else -> "交易时段待核实" },
        "停牌".takeIf { quote.text("tradingStatus") == "suspended" },
        when (quote.text("delivery")) {
            "missing" -> "数据源暂无报价"
            "rate_limited" -> "数据源限流"
            "offline" -> "数据源离线"
            "unavailable" -> "数据源暂不可用"
            "available" -> null
            else -> "数据源状态待核实"
        },
    )
    val price = quote.text("price")
    val at = quote.number("asOf")
    val checked = online && receivedFrom.id == request?.id && now - receivedFrom.at in 0L..90_000L
    val recent = checked && at != null && now - at in 0L..90_000L && quote.text("quality") == "fresh" && session == "open" &&
        quote.text("delivery") == "available" && quote.text("tradingStatus") != "suspended"
    val priceLabel = when {
        price == null -> "暂无参考价"
        recent -> "最近参考价 $price"
        else -> "上次参考价 $price"
    }
    val basis = when (quote.text("timeBasis")) {
        "snapshot" -> "快照时间"
        "trade" -> "成交时间"
        "settlement" -> "结算时间"
        else -> "报价时间"
    }
    DesignCard {
        SectionTitle(priceLabel, labels.joinToString(" · "))
        Text("$basis ${marketTimeText(at)}", color = DeskColors.muted, style = MaterialTheme.typography.bodySmall)
        quote.obj("source")?.let { source ->
            Text("报价来源 ${source.value("name")} · ${source.value("ref")}", color = DeskColors.muted,
                style = MaterialTheme.typography.bodySmall)
        }
        if (!checked) Text(if (online) "显示上次查询。" else "工作台离线，显示上次查询。",
            color = DeskColors.amber, style = MaterialTheme.typography.bodySmall)
    }
}

@Composable
private fun CandlePlot(candles: List<JsonObject>, timeframe: ChartTimeframe, selectedIndex: Int, onSelected: (Int) -> Unit) {
    val prices = remember(candles) {
        candles.mapNotNull { row ->
            val values = listOf("open", "high", "low", "close").map { row.text(it)?.toBigDecimalOrNull() }
            if (values.any { it == null }) null else values.filterNotNull()
        }
    }
    if (prices.size != candles.size || prices.isEmpty()) return
    val low = prices.minOf { it[2] }
    val high = prices.maxOf { it[1] }
    val span = (high - low).takeIf { it.signum() > 0 } ?: BigDecimal.ONE
    val red = DeskColors.red
    val green = DeskColors.green
    val guide = DeskColors.blue
    val select by rememberUpdatedState(onSelected)
    Canvas(Modifier.fillMaxWidth().height(200.dp)
        .pointerInput(candles) {
            fun selectAt(x: Float) {
                if (size.width > 0) select((x * candles.size / size.width).toInt().coerceIn(candles.indices))
            }
            awaitEachGesture {
                val down = awaitFirstDown(requireUnconsumed = false)
                selectAt(down.position.x)
                val drag = awaitHorizontalTouchSlopOrCancellation(down.id) { change, _ ->
                    change.consume()
                    selectAt(change.position.x)
                }
                if (drag != null) horizontalDrag(drag.id) { change ->
                    change.consume()
                    selectAt(change.position.x)
                }
            }
        }
        .semantics {
            val selectedLabel = if (timeframe.minute) marketTimeText(candles[selectedIndex].number("startAt"))
            else candles[selectedIndex].value("date")
            contentDescription = "${candles.size} 根${timeframe.label}，最低 $low，最高 $high"
            stateDescription = "所选 $selectedLabel"
            customActions = buildList {
                if (selectedIndex > 0) add(CustomAccessibilityAction("选择更早一根") { select(selectedIndex - 1); true })
                if (selectedIndex < candles.lastIndex) add(CustomAccessibilityAction("选择更新一根") { select(selectedIndex + 1); true })
            }
        }) {
        val gap = size.width / prices.size
        val padding = 5.dp.toPx()
        fun y(price: BigDecimal) = padding + (size.height - 2 * padding) *
            (1f - price.subtract(low).divide(span, MathContext.DECIMAL64).toFloat())
        val selectedX = gap * (selectedIndex + 0.5f)
        drawLine(guide.copy(alpha = 0.70f), Offset(selectedX, 0f), Offset(selectedX, size.height), 1.dp.toPx())
        prices.forEachIndexed { index, price ->
            val x = gap * (index + 0.5f)
            val color = if (price[3] >= price[0]) red else green
            drawLine(color, Offset(x, y(price[1])), Offset(x, y(price[2])), 1.dp.toPx())
            val top = minOf(y(price[0]), y(price[3]))
            drawRect(color, Offset(x - gap * 0.3f, top), Size(gap * 0.6f, maxOf(1.dp.toPx(), kotlin.math.abs(y(price[3]) - y(price[0])))))
        }
        drawCircle(guide, 3.dp.toPx(), Offset(selectedX, y(prices[selectedIndex][3])))
    }
}

@Stable
internal class SymbolSearchState(query: String = "", submitted: String = "", cursor: String = "") {
    var query by mutableStateOf(query)
    var submitted by mutableStateOf(submitted)
    var cursor by mutableStateOf(cursor)
}

private val symbolSearchSaver = listSaver<SymbolSearchState, String>(
    save = { listOf(it.query, it.submitted, it.cursor) },
    restore = { SymbolSearchState(it[0], it[1], it[2]) },
)

@Composable
internal fun rememberSymbolSearchState(pair: String?, epoch: String?): SymbolSearchState =
    rememberSaveable(pair, epoch, saver = symbolSearchSaver) { SymbolSearchState() }

@Composable
internal fun SymbolSearchPage(state: WorkbenchProjection, searchState: SymbolSearchState, model: WorkbenchViewModel,
                              busy: Boolean, go: (String, String) -> Unit) {
    val request = state.currentIntents("search_symbols").lastOrNull {
        it.data.text("query") == searchState.submitted && it.data.text("pageCursor").orEmpty() == searchState.cursor
    }
    val result = request?.result
    fun search(next: String = "") {
        if (next.isEmpty()) searchState.submitted = searchState.query.trim()
        searchState.cursor = next
        model.submit("search_symbols", buildJsonObject {
            put("query", searchState.submitted)
            if (next.isNotEmpty()) put("pageCursor", next)
        })
    }
    Field("证券代码或名称", searchState.query, { searchState.query = it.take(100) })
    MainAction("查询证券", searchState.query.isNotBlank() && state.connection.localOnline && !busy) { search() }
    request?.let { IntentText(it) }
    if (result?.objects("items")?.isEmpty() == true) NoticeStrip("没有匹配的证券。")
    result?.objects("items")?.forEach { item ->
        val symbol = item.value("symbol")
        val watched = state.rows("watchlist").any { it.text("symbol") == symbol }
        val identified = symbolPattern.matches(symbol) && item.obj("source") != null
        val pending = state.intents.lastOrNull {
            it.command in setOf("watch", "unwatch") && it.stage in pendingStages && symbol in it.data.stringList("symbols")
        }
        DesignCard {
            SectionTitle(item.value("name"), symbol)
            val instrument = when (item.text("instrument")) {
                "stock" -> "股票"
                "etf" -> "ETF"
                else -> "品种未核验"
            }
            Text("${item.value("exchange")} · ${item.value("board")} · $instrument")
            if (item.flag("supported") != true) NoticeStrip("当前未进入量化分析范围。", DeskColors.amber)
            if (!identified && !watched) NoticeStrip("证券身份尚未核验，暂时无法加入关注。", DeskColors.amber)
            item.obj("source")?.let { SourceText(it) }
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                TextButton({ go("chart", symbol) }, Modifier.heightIn(min = 48.dp)) { Text("查看 K 线") }
                TextButton({
                    model.submit(if (watched) "unwatch" else "watch", buildJsonObject {
                        putJsonArray("symbols") { add(JsonPrimitive(symbol)) }
                    })
                }, Modifier.heightIn(min = 48.dp), enabled = (watched || identified) && state.canQueue && !busy && pending == null) {
                    Text(pending?.stage?.label() ?: if (watched) "取消关注" else "加入关注")
                }
            }
        }
    }
    result?.text("nextPageCursor")?.let { next ->
        MainAction("读取下一页", state.connection.localOnline && !busy) { search(next) }
    }
}
