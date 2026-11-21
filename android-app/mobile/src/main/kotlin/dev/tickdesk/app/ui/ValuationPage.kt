package dev.tickdesk.app.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.selectableGroup
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.CollectionInfo
import androidx.compose.ui.semantics.CollectionItemInfo
import androidx.compose.ui.semantics.collectionInfo
import androidx.compose.ui.semantics.collectionItemInfo
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import dev.tickdesk.core.*
import kotlinx.serialization.json.JsonObject
import java.math.BigDecimal
import java.math.MathContext
import java.math.RoundingMode
import java.text.DecimalFormat
import java.text.DecimalFormatSymbols
import java.util.Locale
import kotlin.math.roundToInt

@Composable
internal fun ValuationPage(state: WorkbenchProjection) {
    val valuation = state.meta?.obj("valuation")
    val positions = state.rows("positions").filter { (it.number("qty") ?: 0) > 0 }
    val amount = valuation?.text("positionMarketValue")?.toBigDecimalOrNull()
    val reference = if (amount == null) positions.referenceMarketValue() else null
    val points = state.rows("valuationPoints").groupBy { it.text("tradeDate") }.values
        .mapNotNull { day -> day.maxByOrNull { it.number("revision") ?: 0 } }.sortedBy { it.text("tradeDate") }
    DesignCard {
        SectionTitle(if (reference == null) "已记录持仓口径" else "参考持仓市值（CNY）", "当前台账")
        Text((amount ?: reference?.amount)?.let { "¥ ${formattedMoney(it)}" } ?: "市值未知", style = MaterialTheme.typography.headlineSmall)
        valuation?.text("unrealizedPnl")?.toBigDecimalOrNull()?.let {
            ValuationCaption("浮动盈亏 ${signedMoney(it)} 元", tint = changeTint(it))
        }
        ValuationCaption("实时估值 ${valuation?.value("coveredCount") ?: "未知"} / ${valuation?.value("totalCount") ?: "未知"}")
        val clockLabel = if (reference == null) "最早有效行情" else "参考行情最早"
        ValuationCaption("$clockLabel ${timeText(reference?.asOf ?: valuation?.number("asOf"))} · 不含现金及费用")
        valuation?.stringList("unpricedSymbols")?.takeIf { it.isNotEmpty() }?.let {
            ValuationCaption("缺实时估值 ${it.joinToString("、")}", tint = DeskColors.amber)
        }
        valuation?.stringList("reconciliationSymbols")?.takeIf { it.isNotEmpty() }?.let {
            ValuationCaption("待对账 ${it.joinToString("、")}", tint = DeskColors.amber)
        }
    }
    ValuationChart(points, detail = "已确认日采样")
    PositionDistribution(positions, state.meta != null)
    PositionsTable(positions, state.meta != null)
}

@Composable
internal fun AttachmentCard(
    title: String,
    detail: String?,
    modifier: Modifier = Modifier,
    content: @Composable ColumnScope.() -> Unit,
) {
    Surface(modifier.fillMaxWidth(), color = DeskColors.panel, shape = RoundedCornerShape(17.dp),
        border = BorderStroke(1.dp, DeskColors.border)) {
        Column(Modifier.background(Brush.verticalGradient(listOf(DeskColors.navy, DeskColors.panel)))) {
            Row(
                Modifier.fillMaxWidth().background(DeskColors.field).padding(horizontal = 14.dp, vertical = 10.dp),
                horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(title, Modifier.weight(1f).semantics { heading() }, style = MaterialTheme.typography.titleSmall)
                if (detail != null) {
                    Text(detail, Modifier.weight(.84f, fill = false), style = MaterialTheme.typography.labelSmall,
                        color = DeskColors.muted, textAlign = TextAlign.End, fontFamily = deskMono)
                }
            }
            HorizontalDivider(color = DeskColors.border)
            content()
        }
    }
}

@Composable
internal fun ValuationChart(points: List<JsonObject>, modifier: Modifier = Modifier, detail: String) {
    val amounts = points.map { point ->
        point.text("marketValue")?.toBigDecimalOrNull()?.takeIf { point.flag("complete") == true }
    }
    val latest = points.lastOrNull()
    val start = points.firstOrNull()?.text("tradeDate")
    val end = latest?.text("tradeDate")
    val dateRange = if (start != null && end != null && start != end) "$start – $end" else end
    var selectedId by rememberSaveable { mutableStateOf<String?>(null) }
    var showTable by rememberSaveable { mutableStateOf(false) }
    val selected = points.indexOfFirst { selectedId != null && it.text("pointId") == selectedId }
        .takeIf { it >= 0 } ?: points.lastIndex
    AttachmentCard("持仓市值走势", dateRange ?: detail, modifier) {
        Column(Modifier.padding(horizontal = 12.dp, vertical = 10.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            if (latest != null) {
                val lastAmount = amounts.last()
                Text(lastAmount?.let { "¥ ${formattedMoney(it)}" } ?: "最新采样市值未知",
                    style = MaterialTheme.typography.titleLarge, fontFamily = deskMono, fontWeight = FontWeight.Bold)
                val firstAmount = amounts.first()
                if (dateRange != null && start != end && firstAmount != null && lastAmount != null) {
                    val delta = lastAmount - firstAmount
                    ValuationCaption("区间市值变化 ${signedMoney(delta)} 元", tint = changeTint(delta))
                }
            }
            if (selected < 0) {
                ValuationCaption("本机暂无日采样。")
            } else {
                ValuationSample(points[selected], amounts[selected])
                if (amounts.any { it != null }) {
                    ValuationPlot(amounts, selected) { selectedId = points[it].text("pointId") }
                }
                if (points.size > 1) {
                    ValuationPointSelector(points, selected) { selectedId = points[it].text("pointId") }
                }
                TextButton(onClick = { showTable = !showTable }, modifier = Modifier.heightIn(min = 48.dp)) {
                    Text(if (showTable) "收起市值采样表" else "展开市值采样表")
                }
                if (showTable) ValuationSamplesTable(points, amounts)
            }
        }
        AttachmentFoot("$detail · 市值含持仓数量变化，不含现金与费用 · 缺价留空")
    }
}

@Composable
private fun ValuationSample(point: JsonObject, amount: BigDecimal?) {
    Surface(
        Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(10.dp),
        color = DeskColors.blue.copy(alpha = .10f),
        border = BorderStroke(1.dp, DeskColors.blue.copy(alpha = .25f)),
    ) {
        Column(Modifier.padding(horizontal = 10.dp, vertical = 8.dp), verticalArrangement = Arrangement.spacedBy(3.dp)) {
            val value = amount?.let { "市值 ¥ ${formattedMoney(it)}" } ?: "市值未知"
            Text("${point.value("tradeDate")} · $value", style = MaterialTheme.typography.labelMedium)
            ValuationCaption("采样 ${timeText(point.number("asOf"))} · 版本 ${point.value("revision")}")
            val missing = point.stringList("missingSymbols")
            if (missing.isNotEmpty()) {
                ValuationCaption("缺价 ${missing.joinToString("、")}", tint = DeskColors.amber)
            } else if (amount == null) {
                ValuationCaption("采样不完整", tint = DeskColors.amber)
            }
        }
    }
}

@Composable
private fun ValuationPlot(amounts: List<BigDecimal?>, selected: Int, selectPoint: (Int) -> Unit) {
    val known = amounts.filterNotNull()
    val min = known.min()
    val span = known.max() - min
    val onSelect by rememberUpdatedState(selectPoint)
    Canvas(Modifier.fillMaxWidth().height(112.dp)
        .pointerInput(amounts.size) {
            if (amounts.size < 2) return@pointerInput
            detectTapGestures { tap ->
                val inset = 7.dp.toPx()
                val width = (size.width - 2 * inset).coerceAtLeast(1f)
                val fraction = ((tap.x - inset) / width).coerceIn(0f, 1f)
                onSelect((fraction * amounts.lastIndex).roundToInt())
            }
        }
        .semantics {
            contentDescription = if (amounts.size > 1) "持仓市值曲线，${known.size} 个完整采样。可用下方日期切换采样。"
            else "持仓市值曲线，1 个完整采样。"
        }
    ) {
        val inset = 7.dp.toPx()
        val width = (size.width - 2 * inset).coerceAtLeast(1f)
        val height = size.height - 2 * inset
        fun x(index: Int) = inset + if (amounts.size == 1) width / 2 else index * width / amounts.lastIndex
        for (fraction in listOf(.12f, .5f, .88f)) {
            val y = inset + fraction * height
            drawLine(DeskColors.border, Offset(inset, y), Offset(inset + width, y), 1.dp.toPx())
        }
        val samples = amounts.mapIndexed { index, amount ->
            amount?.let {
                val value = if (span.signum() == 0) .5f else (it - min).divide(span, MathContext.DECIMAL64).toFloat()
                Offset(x(index), inset + (1 - value) * height)
            }
        }
        val runs = mutableListOf(mutableListOf<Offset>())
        for (sample in samples) {
            if (sample == null) runs.add(mutableListOf()) else runs.last().add(sample)
        }
        for (run in runs.filter { it.size > 1 }) {
            val line = Path().apply {
                moveTo(run.first().x, run.first().y)
                run.drop(1).forEach { lineTo(it.x, it.y) }
            }
            val area = Path().apply {
                moveTo(run.first().x, size.height)
                run.forEach { lineTo(it.x, it.y) }
                lineTo(run.last().x, size.height)
                close()
            }
            drawPath(area, Brush.verticalGradient(listOf(DeskColors.red.copy(alpha = .26f), Color.Transparent)))
            drawPath(line, DeskColors.red, style = Stroke(2.4.dp.toPx(), cap = StrokeCap.Round, join = StrokeJoin.Round))
        }
        val crosshair = x(selected)
        drawLine(DeskColors.blue, Offset(crosshair, 0f), Offset(crosshair, size.height), strokeWidth = 1.dp.toPx(),
            pathEffect = PathEffect.dashPathEffect(floatArrayOf(3.dp.toPx(), 3.dp.toPx())))
        samples.forEachIndexed { index, sample ->
            if (sample != null) {
                val active = index == selected
                drawCircle(if (active) DeskColors.text else DeskColors.panel, if (active) 5.5.dp.toPx() else 4.5.dp.toPx(), sample)
                drawCircle(if (active) DeskColors.red else DeskColors.muted, if (active) 3.8.dp.toPx() else 3.dp.toPx(), sample)
            }
        }
    }
}

@Composable
private fun ValuationPointSelector(points: List<JsonObject>, selected: Int, selectPoint: (Int) -> Unit) {
    val sameDays = points.groupingBy { it.text("tradeDate") }.eachCount()
    Row(
        Modifier.fillMaxWidth().horizontalScroll(rememberScrollState(initial = Int.MAX_VALUE)).selectableGroup(),
        horizontalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        for ((index, point) in points.withIndex()) {
            val date = point.value("tradeDate")
            val revision = point.value("revision").takeIf { (sameDays[date] ?: 0) > 1 }
            val label = date.substringAfter('-').replace('-', '/') + (revision?.let { " · v$it" } ?: "")
            FilterChip(index == selected, { selectPoint(index) }, label = { Text(label, fontFamily = deskMono) },
                colors = FilterChipDefaults.filterChipColors(
                    selectedContainerColor = DeskColors.blue.copy(alpha = .18f),
                    selectedLabelColor = DeskColors.blue,
                ),
                modifier = Modifier.sizeIn(minWidth = 48.dp, minHeight = 48.dp).semantics {
                    contentDescription = if (revision == null) date else "$date · 版本 $revision"
                })
        }
    }
}

@Composable
private fun ValuationSamplesTable(points: List<JsonObject>, amounts: List<BigDecimal?>) {
    Column(Modifier.fillMaxWidth().semantics {
        collectionInfo = CollectionInfo(points.size, 2)
    }) {
        Row(Modifier.fillMaxWidth().background(DeskColors.field).padding(vertical = 7.dp, horizontal = 6.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            ValuationCaption("交易日与记录", Modifier.weight(1f))
            ValuationCaption("持仓市值", Modifier.weight(1f))
        }
        for ((index, point) in points.withIndex()) {
            HorizontalDivider(color = DeskColors.border)
            Column(Modifier.fillMaxWidth().semantics(mergeDescendants = true) {
                collectionItemInfo = CollectionItemInfo(index, 1, 0, 2)
            }.padding(vertical = 8.dp, horizontal = 6.dp), verticalArrangement = Arrangement.spacedBy(3.dp)) {
                val amount = amounts[index]
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(point.value("tradeDate"), Modifier.weight(1f), style = MaterialTheme.typography.labelMedium)
                    Text(amount?.let { "¥ ${formattedMoney(it)}" } ?: "市值未知",
                        Modifier.weight(1f), style = MaterialTheme.typography.labelMedium)
                }
                ValuationCaption("${if (amount != null) "完整采样" else "采样不完整"} · 版本 ${point.value("revision")} · ${timeText(point.number("asOf"))}")
                ValuationCaption("记录 ${point.value("pointId")}")
                val missing = point.stringList("missingSymbols")
                if (missing.isNotEmpty()) {
                    ValuationCaption("缺价 ${missing.joinToString("、")}", tint = DeskColors.amber)
                }
            }
        }
    }
}

private val distributionColors = listOf(
    DeskColors.red, Color(0xFF3B82F6), DeskColors.amber, DeskColors.green, Color(0xFFA48ADB),
)

@Composable
private fun PositionDistribution(positions: List<JsonObject>, available: Boolean) {
    val priced = positions.mapNotNull { row ->
        row.text("marketValue")?.toBigDecimalOrNull()?.takeIf { it.signum() >= 0 }?.let { row to it }
    }.sortedByDescending { it.second }
    val sum = priced.fold(BigDecimal.ZERO) { total, current -> total + current.second }
    AttachmentCard("标的仓位权重分布", "覆盖 ${priced.size} / ${positions.size} 只") {
        if (sum.signum() <= 0) {
            ValuationCaption(when {
                !available -> "工作台尚未同步。"
                positions.isEmpty() -> "暂无持仓。"
                else -> "暂无可绘制的持仓市值分布。"
            }, Modifier.padding(12.dp))
        } else {
            val shares = priced.map { it.second.divide(sum, MathContext.DECIMAL64) }
            Row(Modifier.fillMaxWidth().padding(12.dp), verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Box(Modifier.size(92.dp), contentAlignment = Alignment.Center) {
                    Canvas(Modifier.fillMaxSize().semantics { contentDescription = "已记录持仓市值分布，${priced.size} 只。" }) {
                        val stroke = 16.dp.toPx()
                        var start = -90f
                        shares.forEachIndexed { index, share ->
                            val sweep = share.toFloat() * 360f
                            drawArc(distributionColors[index % distributionColors.size], start, sweep, useCenter = false,
                                topLeft = Offset(stroke / 2, stroke / 2), size = Size(size.width - stroke, size.height - stroke),
                                style = Stroke(width = stroke))
                            start += sweep
                        }
                    }
                    if (LocalDensity.current.fontScale <= 1.3f) {
                        Column(horizontalAlignment = Alignment.CenterHorizontally) {
                            Text("${priced.size} 只", style = MaterialTheme.typography.labelLarge, fontFamily = deskMono)
                            ValuationCaption("有价持仓")
                        }
                    }
                }
                Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    priced.forEachIndexed { index, (row, _) ->
                        Row(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = Alignment.Top) {
                            Canvas(Modifier.padding(top = 5.dp).size(8.dp)) {
                                drawCircle(distributionColors[index % distributionColors.size])
                            }
                            ValuationCaption(row.value("name"), Modifier.weight(1f))
                            Text(formattedPercent(shares[index]), Modifier.weight(.70f, fill = false),
                                style = MaterialTheme.typography.labelSmall, color = DeskColors.text,
                                fontFamily = deskMono, textAlign = TextAlign.End)
                        }
                    }
                }
            }
        }
        AttachmentFoot("以有有效市值的已记录持仓为分母")
    }
}

@Composable
private fun PositionsTable(positions: List<JsonObject>, available: Boolean) {
    val scale = LocalDensity.current.fontScale
    AttachmentCard("持仓盈亏明细表", "当前台账") {
        if (positions.isEmpty()) {
            ValuationCaption(if (available) "暂无持仓。" else "工作台尚未同步。", Modifier.padding(12.dp))
        } else {
            val groups = positions.groupBy { row ->
                row.text("priceStatus")?.takeIf { it in setOf("fresh", "stale", "missing") }
            }
            BoxWithConstraints(Modifier.fillMaxWidth()) {
                val tableWidth = maxOf(maxWidth, (300 * scale.coerceAtLeast(1f)).dp)
                Column(Modifier.horizontalScroll(rememberScrollState()).width(tableWidth)) {
                    TableRow(listOf("标的", "数量", "报价", "盈亏率"), true)
                    for ((status, label) in listOf(
                        "fresh" to "有效行情", "stale" to "滞后行情",
                        "missing" to "行情缺失", null to "行情状态未知",
                    )) {
                        val rows = groups[status].orEmpty()
                        if (rows.isEmpty()) continue
                        HorizontalDivider(color = DeskColors.border)
                        Text("$label · ${rows.size} 只", Modifier.padding(horizontal = 12.dp, vertical = 7.dp)
                            .semantics { heading() }, style = MaterialTheme.typography.labelMedium,
                            color = if (status == "fresh") DeskColors.blue else DeskColors.amber)
                        for (position in rows) {
                            HorizontalDivider(color = DeskColors.border)
                            val pnl = position.text("unrealizedPnlRatio")?.toBigDecimalOrNull()
                            TableRow(listOf(
                                "${position.value("name")}\n${position.value("symbol")}",
                                position.value("qty"),
                                position.text("lastPrice")?.toBigDecimalOrNull()?.let(::formattedMoney) ?: "缺价",
                                pnl?.let(::formattedPercent) ?: "未知",
                            ), false, pnl, position.number("priceAsOf")?.let(::timeText))
                        }
                    }
                }
            }
        }
        AttachmentFoot("来源 · 工作台台账与行情快照 · 盈亏未含费用")
    }
}

@Composable
private fun TableRow(values: List<String>, heading: Boolean, pnl: BigDecimal? = null, quoteTime: String? = null) {
    val weights = listOf(1.45f, .70f, 1.22f, 1f)
    Row(Modifier.fillMaxWidth().then(if (heading) Modifier.background(DeskColors.field) else Modifier)
        .padding(horizontal = 8.dp, vertical = 8.dp)) {
        values.forEachIndexed { index, value ->
            val tint = if (heading) DeskColors.faint else if (index != 3) DeskColors.text else when (pnl?.signum()) {
                1 -> DeskColors.red
                -1 -> DeskColors.green
                else -> DeskColors.faint
            }
            Column(Modifier.weight(weights[index]).padding(horizontal = 4.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
                Text(value, Modifier.fillMaxWidth(), color = tint,
                    style = MaterialTheme.typography.labelSmall,
                    fontFamily = if (index == 0 || heading) null else deskMono,
                    textAlign = if (index == 0) TextAlign.Start else TextAlign.End,
                    fontWeight = if (!heading && index == 0) FontWeight.SemiBold else FontWeight.Normal)
                if (index == 2 && quoteTime != null) {
                    Text(quoteTime, Modifier.fillMaxWidth(), style = MaterialTheme.typography.labelSmall,
                        color = DeskColors.faint, textAlign = TextAlign.End, fontFamily = deskMono)
                }
            }
        }
    }
}

@Composable
private fun AttachmentFoot(text: String) {
    HorizontalDivider(color = DeskColors.border)
    ValuationCaption(text, Modifier.padding(horizontal = 12.dp, vertical = 9.dp))
}

@Composable
private fun ValuationCaption(text: String, modifier: Modifier = Modifier, tint: Color = DeskColors.faint) {
    Text(text, modifier, style = MaterialTheme.typography.labelSmall, color = tint)
}

private fun formattedMoney(amount: BigDecimal): String =
    DecimalFormat("#,##0.00", DecimalFormatSymbols.getInstance(Locale.ROOT)).format(amount)

private fun signedMoney(amount: BigDecimal): String =
    (if (amount.signum() > 0) "+" else "") + formattedMoney(amount)

private fun changeTint(amount: BigDecimal): Color = when (amount.signum()) {
    1 -> DeskColors.red
    -1 -> DeskColors.green
    else -> DeskColors.muted
}

private fun formattedPercent(ratio: BigDecimal): String =
    "${ratio.movePointRight(2).setScale(2, RoundingMode.HALF_UP).stripTrailingZeros().toPlainString()}%"
