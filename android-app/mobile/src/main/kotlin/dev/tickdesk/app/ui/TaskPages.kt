package dev.tickdesk.app.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.selection.selectableGroup
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawWithCache
import androidx.compose.ui.draw.drawWithContent
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import dev.tickdesk.app.R
import dev.tickdesk.core.*
import kotlinx.coroutines.delay
import kotlinx.serialization.json.*
import java.math.RoundingMode
import java.time.Instant
import java.time.format.DateTimeFormatter

@Composable
internal fun deskTime(): State<Long> = produceState(System.currentTimeMillis()) {
    while (true) {
        value = System.currentTimeMillis()
        delay(1_000)
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
internal fun InboxPage(
    state: WorkbenchProjection, filter: String,
    onFilterChange: (String) -> Unit, go: (String, String) -> Unit,
) {
    val now by deskTime()
    val tasks = state.rows("tasks")
    val active = tasks.filter { it.activeTask(now) }.sortedWith(
        compareBy<JsonObject> { task ->
            when (task.text("level")) { "critical" -> 0; "warn" -> 1; else -> 2 }
        }.thenBy { it.number("validUntil") }.thenByDescending { it.number("createdAt") },
    )
    val history = tasks.filterNot { it.activeTask(now) }.sortedByDescending { it.number("createdAt") }
    val filters = listOf("all" to "全部", "buy" to "买点", "sell" to "卖点", "risk" to "风险", "history" to "历史")
    val counts = active.groupingBy { it.text("direction") }.eachCount() +
        mapOf("all" to active.size, "history" to history.size)
    val filtered = if (filter == "history") history else {
        active.filter { filter == "all" || it.text("direction") == filter }
    }
    val urgent = filtered.firstOrNull { filter != "history" && it.text("level") == "critical" }
    val visible = filtered.filterNot { it == urgent }
    val open: (JsonObject) -> Unit = { task -> go("task", task.value("taskId")) }

    urgent?.let { task ->
        UrgentTask(task, now) { open(task) }
    }
    FlowRow(Modifier.fillMaxWidth().selectableGroup(), horizontalArrangement = Arrangement.spacedBy(5.dp),
        verticalArrangement = Arrangement.spacedBy(4.dp)) {
        for ((id, label) in filters) {
            val count = if (state.meta == null) null else counts[id] ?: 0
            InboxFilter(if (count == null) label else "$label $count", filter == id) { onFilterChange(id) }
        }
    }

    if (filtered.isEmpty()) {
        val title = when {
            state.meta == null -> "事项尚未同步"
            filter == "history" -> "没有已同步的历史事项"
            tasks.isEmpty() -> "当前没有事项"
            else -> "该筛选下暂无事项"
        }
        DesignCard {
            Text(title, style = MaterialTheme.typography.titleMedium)
            Text(if (state.meta == null) "连接工作台并完成首次同步后显示。" else
                "切换方向或历史筛选可查看其他事项。", color = DeskColors.muted)
        }
    } else {
        val groups = visible.groupBy { task ->
            task.number("createdAt")?.let { Instant.ofEpochMilli(it).atZone(shanghai).toLocalDate() }
        }.entries.sortedByDescending { it.key }
        for ((date, rows) in groups) {
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp), verticalAlignment = Alignment.CenterVertically) {
                val group = if (filter == "history") "历史归档" else "待决策"
                Text("${date ?: "日期未知"} · $group", Modifier.semantics { heading() }, color = DeskColors.muted,
                    style = MaterialTheme.typography.labelSmall, fontWeight = FontWeight.SemiBold)
                HorizontalDivider(Modifier.weight(1f), color = DeskColors.border)
            }
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                for (item in rows) TaskListCard(item, now) { open(item) }
            }
        }
    }
    if (state.meta != null) {
        val critical = active.count { it.text("level") == "critical" }
        Text("历史 ${history.size} 条 · 待决策 ${active.size} 项，其中紧急 $critical 项",
            color = DeskColors.muted, style = MaterialTheme.typography.labelSmall)
    }
    NoticeStrip("决策窗口默认 30 分钟。稍后提醒默认 5 分钟，恢复提醒后至少保留 3 分钟处理时间，总有效期最多 60 分钟。")
}

@Composable
internal fun InboxHeading(state: WorkbenchProjection, filter: String) {
    val now by deskTime()
    val history = filter == "history"
    val selectedCount = state.rows("tasks").count { task ->
        if (history) !task.activeTask(now) else task.activeTask(now) && (filter == "all" || task.text("direction") == filter)
    }
    val connection = state.connection
    val live = connection.localOnline && connection.synced && !connection.recoveryRequired
    val status = when {
        live -> "实时同步"
        connection.recoveryRequired -> "待恢复"
        connection.localOnline -> "同步中"
        state.meta != null -> "离线快照"
        else -> "未同步"
    }
    val tint = if (live) DeskColors.green else DeskColors.amber
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(2.dp)) {
            Text("事项", Modifier.semantics { heading() }, style = MaterialTheme.typography.headlineLarge, fontWeight = FontWeight.Bold)
            Text(when {
                state.meta == null -> "待决策事项与风险提醒"
                history -> "$selectedCount 条历史事项 · 可追溯处理结果"
                else -> "$selectedCount 项待决策 · 按告警级别与剩余窗口排序"
            }, color = DeskColors.muted, style = MaterialTheme.typography.labelSmall)
        }
        LivePill(status, tint)
    }
}

@Composable
private fun InboxFilter(label: String, selected: Boolean, select: () -> Unit) {
    val fill = if (selected) Brush.verticalGradient(listOf(Color(0xFF3B82F6), Color(0xFF2563EB)))
        else Brush.verticalGradient(listOf(Color(0xFF161D2B), DeskColors.panel))
    val border = if (selected) Color(0xFF78A8FF) else DeskColors.border
    Box(Modifier.sizeIn(minWidth = 48.dp, minHeight = 48.dp).clip(CircleShape)
        .selectable(selected, role = Role.RadioButton, onClick = select),
        contentAlignment = Alignment.Center) {
        Text(label, Modifier.background(fill, CircleShape).border(1.dp, border, CircleShape)
            .padding(horizontal = 11.dp, vertical = 5.dp), color = if (selected) Color.White else DeskColors.muted,
            style = MaterialTheme.typography.labelSmall, fontWeight = FontWeight.SemiBold)
    }
}

@Composable
private fun UrgentTask(task: JsonObject, now: Long, open: () -> Unit) {
    SignalCard(DeskColors.red, Modifier.clickable(role = Role.Button, onClickLabel = "查看事项详情", onClick = open), urgent = true) {
        val label: @Composable RowScope.() -> Unit = {
            val count = task.number("remindedCount") ?: 0
            Icon(painterResource(R.drawable.ic_ultra_bell), null, Modifier.size(16.dp), tint = DeskColors.redBright)
            Text(when {
                task.snoozedUntil(now) != null -> "紧急 · 提醒已暂缓"
                count > 0 -> "紧急 · 第 $count 次强提醒"
                else -> "紧急事项"
            },
                Modifier.weight(1f), color = DeskColors.redBright, style = MaterialTheme.typography.labelSmall,
                fontWeight = FontWeight.Bold)
        }
        if (LocalDensity.current.fontScale >= 1.5f) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalAlignment = Alignment.CenterVertically, content = label)
            StatusBadge("CRITICAL · 置顶", DeskColors.red)
        } else {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalAlignment = Alignment.CenterVertically) {
                label()
                StatusBadge("CRITICAL · 置顶", DeskColors.red)
            }
        }
        TaskSymbol(task, urgent = true)
        val quant = task.obj("reasons")?.text("quant")?.takeIf(String::isNotBlank)
        Text(listOfNotNull(taskDirection(task.text("direction")), quant).joinToString(" · "),
            maxLines = 3, overflow = TextOverflow.Ellipsis, color = Color(0xFFF1D7DA), style = MaterialTheme.typography.bodyMedium)
        HorizontalDivider(color = Color.White.copy(alpha = .09f))
        val snoozed = task.snoozedUntil(now)
        TaskMeta(snoozed?.let { "恢复提醒 ${taskClock(it)}" }
            ?: "行情 ${taskClock(task.number("dataAsOf"))} · ${verification(task)}",
            "窗口剩余 ${remaining(task.number("validUntil"), now)} →",
            if (snoozed == null) DeskColors.redBright else DeskColors.amber, Color(0xFFD8B2B8))
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun TaskListCard(task: JsonObject, now: Long, open: () -> Unit) {
    val active = task.activeTask(now)
    val snoozed = task.snoozedUntil(now)
    val state = taskState(task, now)
    val tint = if (active) severityColor(task.text("level")) else DeskColors.muted
    val shape = RoundedCornerShape(17.dp)
    Column(Modifier.fillMaxWidth().clip(shape).border(1.dp, DeskColors.border, shape)
        .background(Brush.verticalGradient(listOf(Color(0xFF161D2B), DeskColors.panel)))
        .clickable(role = Role.Button, onClickLabel = "查看事项详情", onClick = open)
        .drawWithContent {
            drawContent()
            drawRoundRect(tint, Offset(6.dp.toPx(), 14.dp.toPx()),
                Size(3.dp.toPx(), size.height - 28.dp.toPx()), CornerRadius(2.dp.toPx()))
        }.padding(start = 18.dp, end = 14.dp, top = 12.dp, bottom = 12.dp),
        verticalArrangement = Arrangement.spacedBy(5.dp)) {
        val direction = task.text("direction")
        val badge = if (active) "${task.value("level").uppercase()} · ${taskDirection(direction)}" else state
        if (LocalDensity.current.fontScale >= 1.5f) {
            TaskSymbol(task)
            StatusBadge(badge, tint)
        } else {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(7.dp),
                verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.weight(1f)) { TaskSymbol(task) }
                StatusBadge(badge, tint)
            }
        }
        Text(task.text("levelReason")?.takeIf(String::isNotBlank) ?: taskDirection(direction),
            color = directionColor(direction), style = MaterialTheme.typography.labelLarge, fontWeight = FontWeight.SemiBold)
        task.obj("reasons")?.text("quant")?.takeIf(String::isNotBlank)?.let {
            Text(it, color = DeskColors.muted, maxLines = 3, overflow = TextOverflow.Ellipsis, style = MaterialTheme.typography.bodyMedium)
        }
        Spacer(Modifier.height(2.dp))
        HorizontalDivider(color = Color.White.copy(alpha = .06f))
        val count = task.number("remindedCount")?.takeIf { it > 0 }?.let { " #$it" }.orEmpty()
        val timing = when {
            !active -> "触发 ${taskClock(task.number("createdAt"))} · $state"
            snoozed != null -> "已暂缓 · ${taskClock(snoozed)} 恢复"
            else -> "${taskClock(task.number("lastRemindedAt") ?: task.number("createdAt"))} · $state$count"
        }
        val window = if (active) "剩余 ${remaining(task.number("validUntil"), now)}"
            else "有效期至 ${timeText(task.number("validUntil"))}"
        TaskMeta(timing, window, if (snoozed != null) DeskColors.amber else DeskColors.secondary)
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun TaskSymbol(task: JsonObject, urgent: Boolean = false) {
    FlowRow(horizontalArrangement = Arrangement.spacedBy(7.dp), verticalArrangement = Arrangement.spacedBy(1.dp)) {
        Text(task.value("name"), Modifier.align(Alignment.CenterVertically),
            style = MaterialTheme.typography.titleMedium, fontSize = if (urgent) 19.sp else 16.sp, fontWeight = FontWeight.Bold)
        val capsule = if (urgent) Modifier.background(Color.White.copy(alpha = .07f), RoundedCornerShape(5.dp))
            .padding(horizontal = 5.dp) else Modifier
        Text(task.value("symbol"), Modifier.align(Alignment.CenterVertically).then(capsule),
            color = if (urgent) Color(0xFFE2B8BE) else DeskColors.faint, style = MaterialTheme.typography.labelSmall, fontFamily = deskMono)
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun TaskMeta(label: String, expiry: String, expiryTint: Color, tint: Color = DeskColors.faint) {
    FlowRow(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp),
        verticalArrangement = Arrangement.spacedBy(2.dp)) {
        Text(label, Modifier.weight(1f), color = tint, style = MaterialTheme.typography.labelSmall)
        Text(expiry, color = expiryTint, style = MaterialTheme.typography.labelSmall, fontWeight = FontWeight.SemiBold)
    }
}

@Composable
internal fun SignalCard(tint: Color, modifier: Modifier = Modifier, urgent: Boolean = false, content: @Composable ColumnScope.() -> Unit) {
    val shape = RoundedCornerShape(20.dp)
    val start = if (urgent) Color(0xFF2C121C) else Color(0xFF161F30)
    val end = if (urgent) Color(0xFF140E18) else DeskColors.panel
    val border = if (urgent) tint.copy(alpha = .38f) else DeskColors.border
    Column(Modifier.fillMaxWidth().clip(shape).border(1.dp, border, shape)
        .background(Brush.linearGradient(listOf(start, end)))
        .drawWithCache {
            val glow = Brush.radialGradient(listOf(tint.copy(alpha = if (urgent) .28f else .16f), Color.Transparent),
                center = Offset(size.width * .92f, size.height * .12f), radius = (size.width * .54f).coerceAtLeast(1f))
            onDrawBehind { drawRect(glow) }
        }.then(modifier).padding(16.dp), verticalArrangement = Arrangement.spacedBy(7.dp), content = content)
}

private val taskClockFormatter = DateTimeFormatter.ofPattern("HH:mm:ss").withZone(shanghai)
private val taskTimestampFormatter = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss").withZone(shanghai)

private fun taskClock(at: Long?) = at?.let { taskClockFormatter.format(Instant.ofEpochMilli(it)) } ?: "未知"
private fun taskTimestamp(at: Long?) = at?.let { taskTimestampFormatter.format(Instant.ofEpochMilli(it)) } ?: "未知"

@Composable
internal fun TaskDetails(
    state: WorkbenchProjection, id: String, model: WorkbenchViewModel, busy: Boolean,
    go: (String, String) -> Unit, footer: ScreenFooter,
) {
    val task = state.task(id)
    if (task == null) {
        DesignCard {
            Text("本机暂无此事项", style = MaterialTheme.typography.titleMedium)
            Text("连接工作台后可重新同步。", color = DeskColors.muted)
            TextButton(onClick = model::retry, modifier = Modifier.heightIn(min = 48.dp)) { Text("重新同步") }
        }
        return
    }
    val now by deskTime()
    var sellOpen by rememberSaveable(id) { mutableStateOf(false) }
    val reasons = task.obj("reasons")!!
    val direction = task.text("direction")
    val level = task.text("level")
    val pending = state.intents.filter { it.command in taskDecisionCommands && it.data.text("taskId") == id }
    val validUntil = task.number("validUntil")!!
    val active = task.activeTask(now)
    val position = state.rows("positions").firstOrNull { it.text("symbol") == task.text("symbol") }
    val scanId = task.text("scanId")
    val quoteAt = task.number("dataAsOf")
    val candidate = state.reads["get_candidates"]
        ?.takeIf { scanId != null && quoteAt != null && it.obj("scan")?.text("scanId") == scanId }
        ?.objects("items")?.firstOrNull { it.text("symbol") == task.text("symbol") && it.number("dataAsOf") == quoteAt }
    val snoozed = task.snoozedUntil(now)
    TaskDetailHero(task, position, candidate)
    TaskValidity(task, validUntil, now, active)
    TaskFacts(task, position, now)
    TaskRationale(task, reasons)
    TaskExitEvidence(task)
    val lastReminded = task.number("lastRemindedAt")?.takeIf { it > 0 }?.let { " · 最近 ${taskClock(it)}" }.orEmpty()
    if (active) {
        NoticeStrip(if (snoozed != null) {
            "提醒暂缓至 ${taskClock(snoozed)} · 决策窗口截止 ${taskClock(validUntil)}"
        } else {
            "已触发 ${task.value("remindedCount")} 次提醒$lastReminded · 当前未暂缓"
        }, DeskColors.amber)
    } else {
        val outcome = taskState(task, now)
        val note = if (direction == "risk") "可查询操作记录。" else "已发生的成交可在持仓页独立补录。"
        NoticeStrip("$outcome · $note", if (task.text("status") == "acted") DeskColors.green else DeskColors.muted)
    }
    if (pending.isNotEmpty()) {
        SectionTitle("本机操作")
        DesignCard {
            for (intent in pending.takeLast(4)) {
                if (direction == "risk" && intent.command == "act" && intent.data.text("side") == null) {
                    Text(listOfNotNull("风险核对", intent.stage.label(), intent.errorCode).joinToString(" · "))
                } else {
                    IntentText(intent)
                }
            }
        }
    }
    val history = state.reads["get_task_actions"]?.takeIf { it.text("taskId") == id }
    if (history != null) {
        SectionTitle("操作记录")
        DesignCard {
            if (history.objects("items").isEmpty()) Text("本页没有记录", color = DeskColors.muted)
            history.objects("items").forEach { Text("${timeText(it.number("at"))} · ${historyLabel(it.text("type"))}") }
        }
    }
    TextButton(enabled = state.connection.localOnline && !busy, modifier = Modifier.heightIn(min = 48.dp), onClick = {
        model.submit("get_task_actions", buildJsonObject { put("taskId", id) })
    }) { Text("查询操作记录") }
    if (direction == "risk") {
        TextButton(onClick = { go("execution_manual", task.value("symbol")) }, modifier = Modifier.heightIn(min = 48.dp)) {
            Text("独立补录此标的实际成交")
        }
    }
    if (direction == "buy") {
        TextButton(onClick = { go("execution_progress", id) }, modifier = Modifier.heightIn(min = 48.dp)) {
            Text("申报执行进度与剩余量")
        }
    }
    if (direction == "sell") SellExecutionDialog(state, task, model, busy, sellOpen) { sellOpen = false }
    if (!active) return
    var ignore by rememberSaveable(id) { mutableStateOf(false) }
    val waiting = state.taskDecisionUnresolved(id)
    val enabled = state.canQueue && !busy && !waiting
    val supportsSnooze = level == "critical" || (level == "warn" && task.number("remindedCount")!! < 2)
    val snoozeLimit = now + 8 * 60_000L > task.number("createdAt")!! + 60 * 60_000L
    val snoozeLabel = when {
        snoozed != null -> "暂缓至 ${taskClock(snoozed)}"
        snoozeLimit -> "已到稍后上限"
        else -> "稍后 5 分钟"
    }
    val context = when {
        waiting -> "待工作台处理"
        !state.canQueue -> "同步后可处理"
        snoozed != null -> "暂缓至 ${taskClock(snoozed)}"
        else -> "剩余 ${remaining(validUntil, now)}"
    }
    val label = when (direction) {
        "risk" -> "确认已核对风险"
        "sell" -> "卖出已成交 · 记入台账"
        else -> "买入成交回填"
    }
    val takeAction: () -> Unit = {
        if (task.activeTask()) {
            when (direction) {
                "risk" -> model.submit("act", taskParams(task))
                "sell" -> sellOpen = true
                else -> go("execution", id)
            }
        }
    }
    footer.ResponsiveAction(
        inline = {
            Column(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
                    if (supportsSnooze) OutlinedButton(enabled = enabled && snoozed == null && !snoozeLimit,
                        modifier = Modifier.weight(1f).heightIn(min = 48.dp), onClick = {
                        if (task.activeTask()) model.submit("snooze", taskParams(task) { put("minutes", 5) })
                    }) { Text(snoozeLabel) }
                    OutlinedButton(onClick = { ignore = true }, enabled = enabled, modifier = Modifier.weight(1f).heightIn(min = 48.dp)) {
                        Text("忽略本次")
                    }
                }
                MainAction(label, enabled, R.drawable.ic_ultra_check, takeAction)
            }
        },
        shortcut = {
            val eyebrow = if (level == "critical") "紧急事项 · 快捷处置" else "事项处置"
            val shortLabel = when (direction) { "sell" -> "卖出回填"; "buy" -> "买入回填"; else -> label }
            ContextAction(eyebrow, "${task.value("name")} · $context", shortLabel, enabled, takeAction)
        },
    )
    if (ignore && active) Confirm("忽略此事项", "提交后，等待工作台确认处理结果。", busy, { ignore = false }) {
        if (enabled && task.activeTask()) model.submit("ignore", taskParams(task)) { ignore = false }
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun TaskDetailHero(task: JsonObject, position: JsonObject?, candidate: JsonObject?) {
    val direction = task.text("direction")
    val tint = directionColor(direction)
    val dataAsOf = task.number("dataAsOf")
    val matchingPosition = position?.takeIf { dataAsOf != null && it.number("priceAsOf") == dataAsOf }
    val scanQuote = candidate?.text("lastPrice")
    val quote = scanQuote ?: matchingPosition?.text("lastPrice")
    SignalCard(tint) {
        StatusBadge(when (direction) {
            "sell" -> if (task.text("stopPrice") != null) "持仓止损卖点" else "卖出信号"
            "buy" -> "买入信号"
            else -> "风险提醒"
        }, tint)
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(1.dp)) {
            Text(task.value("name"), Modifier.align(Alignment.CenterVertically).semantics { heading() },
                style = MaterialTheme.typography.headlineLarge, fontWeight = FontWeight.Bold)
            Text(task.value("symbol"), Modifier.align(Alignment.CenterVertically), color = DeskColors.muted,
                style = MaterialTheme.typography.labelSmall, fontFamily = deskMono)
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
            if (quote == null) {
                Text("报价未知", color = DeskColors.muted, style = MaterialTheme.typography.titleMedium)
            } else {
                Text(tradeNumber(quote), Modifier.weight(1f, fill = false), color = tint,
                    style = MaterialTheme.typography.displaySmall, fontFamily = deskMono, fontWeight = FontWeight.Bold)
                if (scanQuote != null) {
                    candidate.text("changeRatio")?.toBigDecimalOrNull()?.let { ratio ->
                        val change = ratio.movePointRight(2).setScale(2, RoundingMode.HALF_UP).toPlainString()
                        val sign = if (ratio.signum() > 0) "+" else ""
                        Text("$sign$change%", color = DeskColors.secondary, style = MaterialTheme.typography.labelSmall, fontFamily = deskMono)
                    }
                } else if (matchingPosition?.text("priceStatus") == "stale") {
                    StatusBadge("报价已滞后", DeskColors.amber)
                }
            }
        }
        val dataSource = if (dataAsOf == null) null else task.obj("reasons")?.objects("sources")
            ?.firstOrNull { it.number("asOf") == dataAsOf }?.text("name")?.takeIf(String::isNotBlank)
        TaskSourceLine("${dataSource ?: "信号行情"} · ${taskTimestamp(dataAsOf)}")
    }
}

@Composable
private fun TaskValidity(task: JsonObject, validUntil: Long, now: Long, active: Boolean) {
    val tint = if (active) severityColor(task.text("level")) else DeskColors.muted
    val created = task.number("createdAt")!!
    val windowMinutes = (validUntil - created).takeIf { it > 0 && it % 60_000 == 0L }?.div(60_000)
    val window = if (windowMinutes == null) "决策窗口" else "决策窗口（$windowMinutes 分钟）"
    val shape = RoundedCornerShape(16.dp)
    Column(Modifier.fillMaxWidth().clip(shape).border(1.dp, tint.copy(alpha = .30f), shape)
        .background(Brush.horizontalGradient(listOf(tint.copy(alpha = .16f), tint.copy(alpha = .04f))))
        .padding(horizontal = 14.dp, vertical = 11.dp), verticalArrangement = Arrangement.spacedBy(7.dp)) {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
            Text(window, Modifier.weight(1f), color = DeskColors.text, style = MaterialTheme.typography.labelSmall)
            Text(if (active) remaining(validUntil, now) else taskState(task, now), color = tint,
                style = MaterialTheme.typography.titleLarge, fontFamily = if (active) deskMono else null, fontWeight = FontWeight.Bold)
        }
        if (validUntil > created) {
            val elapsedFraction = ((now - created).toDouble() / (validUntil - created)).toFloat().coerceIn(0f, 1f)
            LinearProgressIndicator(progress = { if (active) elapsedFraction else 1f },
                modifier = Modifier.fillMaxWidth().height(5.dp), color = tint, trackColor = Color.White.copy(alpha = .09f),
                gapSize = 0.dp, drawStopIndicator = {})
        }
        TaskMeta("触发于 ${taskClock(created)}", "窗口截止 ${taskClock(validUntil)}", DeskColors.muted)
    }
}

@Composable
private fun TaskFacts(task: JsonObject, position: JsonObject?, now: Long) {
    val direction = task.text("direction")
    val available = position?.number("availableQty")?.let { tradeNumber(it.toString()) } ?: "未知"
    val total = position?.number("qty")?.let { "总仓 ${tradeNumber(it.toString())}" }
    val cost = position?.text("cost")?.let { "成本 ${tradeNumber(it)}" }
    val holdingDetails = listOfNotNull(total, cost).joinToString(" · ").let { if (it.isEmpty()) "" else "（$it）" }
    val stop = task.text("stopPrice")?.let { "${tradeNumber(it)} 元" } ?: "未知"
    val strategies = task.stringList("strategies")
    val score = task.text("score")?.let { "${tradeNumber(it)} 分" }
    val basis = if (direction == "risk" && score == null && strategies.isEmpty()) {
        "规则证据 · 无量化评分"
    } else listOfNotNull(strategies.joinToString("、").takeIf(String::isNotBlank), score).joinToString(" · ").ifBlank { "待核验" }
    val cells = listOf(
        "信号方向 / 状态" to "${taskDirection(direction)} · ${taskState(task, now)}",
        "触发依据 / 评分" to basis,
        "设定止损价" to stop, "当前可用持仓" to "$available$holdingDetails",
    )
    Column(verticalArrangement = Arrangement.spacedBy(9.dp)) {
        cells.chunked(2).forEachIndexed { row, pair ->
            Row(Modifier.fillMaxWidth().height(IntrinsicSize.Min), horizontalArrangement = Arrangement.spacedBy(9.dp)) {
                pair.forEachIndexed { column, (label, value) ->
                    Surface(Modifier.weight(1f).fillMaxHeight(), shape = RoundedCornerShape(14.dp), color = Color(0xFF121826),
                        border = BorderStroke(1.dp, DeskColors.border)) {
                        Column(Modifier.padding(horizontal = 11.dp, vertical = 9.dp), verticalArrangement = Arrangement.spacedBy(3.dp)) {
                            Text(label, color = DeskColors.muted, style = MaterialTheme.typography.labelSmall)
                            Text(value, color = if (row == 0 && column == 0 && task.activeTask(now)) directionColor(direction) else DeskColors.text,
                                style = MaterialTheme.typography.labelLarge, fontWeight = FontWeight.SemiBold)
                        }
                    }
                }
            }
        }
        val target = task.text("targetPrice")?.let { "目标价 ${tradeNumber(it)} 元" }
        val entry = task.stringList("entryZone").takeIf { it.isNotEmpty() }?.joinToString("–") { tradeNumber(it) }
            ?.let { "入场区间 $it 元" }
        val levels = listOfNotNull(target, entry).joinToString(" · ")
        if (levels.isNotEmpty()) Text(levels, color = DeskColors.muted, style = MaterialTheme.typography.labelSmall)
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun TaskRationale(task: JsonObject, reasons: JsonObject) {
    val evidence = reasons.objects("riskEvidence")
    val quant = reasons.text("quant")?.takeIf(String::isNotBlank)?.lineSequence()?.filter(String::isNotBlank)?.toList().orEmpty()
    Column(verticalArrangement = Arrangement.spacedBy(7.dp)) {
        val detail = if (evidence.isNotEmpty()) "${evidence.size} 条证据" else quant.takeIf { it.isNotEmpty() }?.let { "${it.size} 条理由" }
        SectionTitle(if (evidence.isNotEmpty() && task.text("direction") == "risk") "风险证据" else "量化触发因子", detail)
        DesignCard {
            if (quant.isEmpty() && evidence.isEmpty()) Text("暂无量化理由", color = DeskColors.muted)
            for (factor in quant) {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("✓", Modifier.clearAndSetSemantics { }, color = DeskColors.green, style = MaterialTheme.typography.labelLarge)
                    Text(factor, Modifier.weight(1f), style = MaterialTheme.typography.bodyMedium)
                }
            }
            if (quant.isNotEmpty() && evidence.isNotEmpty()) HorizontalDivider(color = DeskColors.border)
            for (item in evidence) {
                val label = when (item.text("type")) {
                    "trading_suspension" -> "交易停牌"
                    else -> item.value("type")
                }
                Text(label, style = MaterialTheme.typography.labelLarge)
                val lineage = listOfNotNull(item.value("source"), taskTimestamp(item.number("sourceTime")),
                    item.text("sourceRef")?.takeIf(String::isNotBlank))
                TaskSourceLine(lineage.joinToString(" · "))
            }
        }
    }
    Column(verticalArrangement = Arrangement.spacedBy(7.dp)) {
        SectionTitle("AI 事实核验与归因", verification(task))
        DesignCard {
            Text(reasons.text("ai")?.takeIf(String::isNotBlank) ?: "暂无 AI 研判", color = DeskColors.muted,
                style = MaterialTheme.typography.bodyMedium)
            val risks = reasons.stringList("risks").filter(String::isNotBlank)
            if (risks.isNotEmpty()) {
                FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    risks.forEach { StatusBadge(it, DeskColors.amber) }
                }
            }
            val sources = reasons.objects("sources")
            if (sources.isNotEmpty()) {
                HorizontalDivider(color = DeskColors.border)
                for (source in sources) {
                    val lineage = listOfNotNull(source.value("name"), taskTimestamp(source.number("asOf")),
                        source.text("ref")?.takeIf(String::isNotBlank))
                    TaskSourceLine(lineage.joinToString(" · "))
                }
            }
        }
    }
}

@Composable
private fun TaskExitEvidence(task: JsonObject) {
    val exit = task.obj("exit") ?: return
    DesignCard {
        SectionTitle("独立退出规则", "不依赖入场共振；条件成立不代表已经成交。")
        Text("规则 ${exit.value("ruleId")} v${exit.value("ruleVersion")} · 策略 ${exit.value("strategyId")}")
        Text(exit.text("condition") ?: "条件说明缺失", color = DeskColors.muted)
        Text(
            "决策时钟 " + when (exit.text("decisionClock")) {
                "completed_daily_bar" -> "完整日线收盘"
                else -> "未知"
            } + " · bar 截至 ${timeText(exit.number("barEndMs"))} · 当时可得 ${timeText(exit.number("barAvailableAtMs"))}",
            color = DeskColors.muted,
        )
        Text(
            "可执行 " + if (exit.flag("executable") == true) "是" else "否" +
                " · 可卖 ${exit.value("availableQty")} 股 · 复核 ${timeText(exit.number("reviewAt"))}",
        )
        val restriction = exit.text("restriction")
        if (restriction != null) {
            StatusBadge(
                when (restriction) {
                    "t_plus_one" -> "T+1 交收受限"
                    "frozen" -> "其他冻结"
                    "partially_sellable" -> "仅部分可卖"
                    "trading_suspended" -> "交易受限"
                    "quote_unavailable" -> "缺少实时报价"
                    "market_closed" -> "非交易时段"
                    "strategy_origin_unverified" -> "策略归属未核实"
                    else -> "可卖依据未核实"
                },
                DeskColors.amber,
            )
        }
        if (exit.flag("executable") != true) {
            NoticeStrip("退出条件成立但当前不可执行；受限数量不催卖，待重新核验后复核。", DeskColors.amber)
        } else if (restriction != null) {
            NoticeStrip("退出条件成立，仅可在可卖数量内处理；受限部分不催卖。", DeskColors.blue)
        }
    }
}

@Composable
private fun TaskSourceLine(text: String) {
    Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
        Text("▤", Modifier.clearAndSetSemantics { }, color = DeskColors.muted, style = MaterialTheme.typography.labelSmall)
        Text(text, Modifier.weight(1f), color = DeskColors.muted, style = MaterialTheme.typography.labelSmall)
    }
}

internal fun taskParams(task: JsonObject, extra: JsonObjectBuilder.() -> Unit = {}) = buildJsonObject {
    put("taskId", task.text("taskId")!!)
    put("baseActionVersion", task.number("actionVersion")!!)
    extra()
}

internal val taskDecisionCommands = setOf("act", "ignore", "snooze")

internal fun WorkbenchProjection.taskDecisionUnresolved(taskId: String): Boolean =
    intents.any { it.command in taskDecisionCommands && it.data.text("taskId") == taskId && it.awaitingExecutionOutcome() }

internal fun severity(value: String?) = when (value) { "critical" -> "严重"; "warn" -> "警告"; else -> "信息" }
internal fun severityColor(value: String?) = when (value) { "critical" -> DeskColors.red; "warn" -> DeskColors.amber; else -> DeskColors.blue }
internal fun directionColor(value: String?) = when (value) { "buy" -> DeskColors.red; "sell" -> DeskColors.green; else -> DeskColors.blue }
internal fun taskDirection(value: String?) = when (value) { "buy" -> "买点"; "sell" -> "卖点"; else -> "风险" }
internal fun JsonObject.snoozedUntil(now: Long = System.currentTimeMillis()) =
    number("snoozeUntil")?.takeIf { it > now && activeTask(now) }

internal fun taskState(task: JsonObject, now: Long = System.currentTimeMillis()) = when (task.text("status")) {
    "acted" -> "已处理"; "ignored" -> "已忽略"; "expired" -> "已过期"
    else -> when {
        !task.activeTask(now) -> "已过期"
        task.snoozedUntil(now) != null -> "已暂缓"
        task.text("status") == "reminded" -> "已提醒"
        else -> "待处理"
    }
}
internal fun verification(task: JsonObject) = when (task.obj("reasons")?.text("verificationStatus")) {
    "verified" -> "已核验"; "partial" -> "部分核验"; else -> "未核验"
}
internal fun remaining(until: Long?, now: Long): String {
    val seconds = until?.let { ((it - now + 999L) / 1_000L).coerceAtLeast(0) } ?: return "未知"
    return "${(seconds / 60).toString().padStart(2, '0')}:${(seconds % 60).toString().padStart(2, '0')}"
}

private fun historyLabel(type: String?) = when (type) {
    "created" -> "创建"; "reminded" -> "提醒"; "snoozed" -> "稍后"; "acted" -> "已处理"
    "ignored" -> "忽略"; "expired" -> "过期"; "evidence_added" -> "风险补证"; else -> type ?: "未知"
}

