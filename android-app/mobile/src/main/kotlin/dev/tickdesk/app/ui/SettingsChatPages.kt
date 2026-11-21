package dev.tickdesk.app.ui

import android.Manifest
import android.app.NotificationManager
import android.content.Intent
import android.os.Build
import android.provider.Settings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.ScrollState
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LifecycleEventEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import dev.tickdesk.app.R
import dev.tickdesk.core.*
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.*
import java.time.Instant
import java.time.LocalDate
import java.time.format.DateTimeFormatter
import java.util.Locale

@Composable
internal fun SettingsPage(
    state: WorkbenchProjection,
    model: WorkbenchViewModel,
    busy: Boolean,
    go: (String, String) -> Unit,
) {
    val status = state.reads["get_status"]
    val analysis = state.meta?.obj("analysis")
    val settings = state.meta?.obj("settings")
    val permissions = rememberReminderPermissions()
    val background by model.backgroundReminders.collectAsStateWithLifecycle()
    val connection = state.connection
    val backgroundLabel = when {
        background.enabledPair == null || background.enabledPair != state.pairId -> "已关闭"
        !permissions.notifications -> "通知未开启"
        connection.recoveryRequired || state.serviceRecoveryActive -> "恢复核对中"
        !background.running -> "未运行"
        !connection.relayOnline || !connection.localOnline -> "连接中断"
        !connection.synced -> "同步中"
        else -> "已开启"
    }
    val reminderLabel = settings?.let { "夜间 ${it.value("nightStart")}–${it.value("nightEnd")}" } ?: "时段待同步"
    val services = status?.objects("dependencies").orEmpty().associateBy { it.text("name") }
    val serviceLabel = when {
        !connection.localOnline -> "工作台离线"
        !connection.synced || status == null -> "待核对"
        else -> "AI ${dependencyLabel(services["deepseek"])} · 数据${dependencyLabel(services["mcp"])}"
    }
    SettingsRecovery(state, model, busy, go)
    SettingsOverviewGroup("工作台") {
        SettingsLink(DeskGlyph.Phone, "配对与连接", workbenchConnectionLabel(state)) { go("workbench", "") }
        val queue = pendingSettingsOperations(state)
        if (queue.isNotEmpty()) {
            HorizontalDivider(color = DeskColors.border)
            SettingsLink(DeskGlyph.Clock, "待处理操作", "${queue.size} 条") { go("operation_queue", "") }
        }
    }
    SettingsOverviewGroup("量化") {
        SettingsLink(DeskGlyph.Scan, "盘中扫描", when (analysis?.flag("paused")) {
            true -> "已暂停"
            false -> "运行中"
            null -> "待同步"
        }) { go("analysis", "") }
        HorizontalDivider(color = DeskColors.border)
        SettingsLink(DeskGlyph.Edit, "扫描档案", scanProfileLabel(analysis?.text("profile"))) { go("analysis_profile", "") }
    }
    SettingsOverviewGroup("提醒") {
        SettingsLink(DeskGlyph.Bell, "后台提醒", backgroundLabel) { go("background_reminders", "") }
        HorizontalDivider(color = DeskColors.border)
        SettingsLink(DeskGlyph.Moon, "提醒设置", when {
            !permissions.notifications -> "$reminderLabel · 通知未开"
            permissions.channelBlocked -> "$reminderLabel · 渠道已关闭"
            else -> reminderLabel
        }) { go("reminders", "") }
    }
    SettingsOverviewGroup("服务") {
        SettingsLink(DeskGlyph.Pulse, "工作台服务状态", serviceLabel) { go("services", "") }
        HorizontalDivider(color = DeskColors.border)
        SettingsLink(DeskGlyph.File, "原图与高级导入管理") { go("import", "") }
    }
}

@Composable
private fun SettingsRecovery(state: WorkbenchProjection, model: WorkbenchViewModel, busy: Boolean, go: (String, String) -> Unit) {
    if (state.serviceRecoveryActive || state.requiresReview) {
        DesignCard {
            if (state.serviceRecoveryActive) {
                NoticeStrip("工作台处于恢复模式，普通业务保持只读。", DeskColors.amber)
                MainAction("打开恢复对账", onClick = { go("recovery", "") })
            } else {
                NoticeStrip("工作台数据库已更换。同步完整快照并逐项核对原操作结果后，再开放写入。", DeskColors.amber)
                val requiresReview = state.intents.filter { it.stage == DeliveryStage.QUARANTINED && !it.reviewed }
                requiresReview.forEach { QueueEntry(it, state.connection.localOnline, model, busy) }
                MainAction("核对完成，重新开放写入", state.connection.synced &&
                    requiresReview.isEmpty() && !busy, model::finishReview)
            }
        }
    }
}

private fun scanProfileLabel(profile: String?) = when (profile) {
    "conservative" -> "保守趋势"
    "aggressive" -> "激进动量"
    else -> "待同步"
}

@Composable
internal fun ScanProfileSettingsPage(state: WorkbenchProjection, model: WorkbenchViewModel, busy: Boolean) {
    val profile = state.meta?.obj("analysis")?.text("profile")
    val profileIntent = state.intents.lastOrNull { it.command == "set_profile" }
    DesignCard {
        ChatMeta("下一轮扫描生效 · 已生成事项保留原参数")
        val profiles = listOf("conservative", "aggressive")
        SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth().heightIn(min = 48.dp)) {
            profiles.forEachIndexed { index, key ->
                SegmentedButton(
                    selected = profile == key,
                    enabled = !busy && state.canQueue && profileIntent?.stage !in pendingStages,
                    onClick = { if (profile != key) model.submit("set_profile", buildJsonObject { put("profile", key) }) },
                    shape = SegmentedButtonDefaults.itemShape(index, profiles.size),
                    icon = {},
                    label = { Text(scanProfileLabel(key), style = MaterialTheme.typography.labelMedium, textAlign = TextAlign.Center) },
                    colors = SegmentedButtonDefaults.colors(
                        activeContainerColor = DeskColors.blue.copy(alpha = .14f),
                        activeContentColor = DeskColors.blue, activeBorderColor = DeskColors.blue.copy(alpha = .50f),
                        inactiveContainerColor = DeskColors.field, inactiveBorderColor = DeskColors.border,
                    ),
                    modifier = Modifier.heightIn(min = 48.dp),
                )
            }
        }
        if (profile == null) ChatMeta("当前档案待同步")
        ChatMeta("保守冷却 60 分钟 · 激进冷却 30 分钟")
        if (profileIntent != null && !profileIntent.stage.businessSucceeded) IntentText(profileIntent)
    }
}

@Composable
internal fun ServiceSettingsPage(state: WorkbenchProjection, model: WorkbenchViewModel, busy: Boolean) {
    val status = state.reads["get_status"]
    val analysis = state.meta?.obj("analysis")
    val dependencies = status?.objects("dependencies").orEmpty().associateBy { it.text("name") }
    var showDiagnostics by rememberSaveable { mutableStateOf(false) }
    var serviceRefresh by remember(state.pairId, state.epoch) { mutableIntStateOf(0) }
    val valuation = state.meta?.obj("valuation")
    val coverage = valuation?.number("coveredCount")
    val tracked = valuation?.number("totalCount")
    val coverageLabel = when {
        coverage == null || tracked == null -> "行情覆盖待同步"
        tracked == 0L -> "当前暂无持仓"
        else -> "持仓行情覆盖 $coverage / $tracked"
    }
    DesignCard {
        if (!state.connection.localOnline) NoticeStrip("工作台离线，显示最近同步。")
        SettingsRow(DeskGlyph.Pulse, "AI 分析", "模型供应商状态") { DependencyStatus(dependencies["deepseek"], showDiagnostics) }
        HorizontalDivider(color = DeskColors.border)
        SettingsRow(DeskGlyph.Pulse, "行情与数据", coverageLabel) {
            DependencyStatus(dependencies["mcp"], showDiagnostics)
        }
        HistoryCoverageStatus(state, model, serviceRefresh)
        StorageStatus(state, model, serviceRefresh)
        HorizontalDivider(color = DeskColors.border)
        SettingsRow(DeskGlyph.Scan, "量化内核", when {
            dependencies["kernel"]?.text("errorCode") == "KERNEL_NO_PRODUCTION_STRATEGIES" ->
                "已连接，尚无通过准入的生产策略"
            dependencies["kernel"]?.text("status") == "degraded" -> "已连接，分析能力受限"
            else -> when (analysis?.text("kernelStatus")) {
            "online" -> "最后同步时在线"
            "offline" -> "最后同步时离线"
            "starting" -> "最后同步时启动中"
            else -> "尚未同步"
            }
        }) { DependencyStatus(dependencies["kernel"], showDiagnostics) }
        SideBySide {
            TextButton(onClick = { model.submit("get_status"); serviceRefresh++ }, enabled = state.connection.localOnline && !busy,
                modifier = Modifier.heightIn(min = 48.dp)) { Text("刷新服务状态") }
            TextButton(onClick = { showDiagnostics = !showDiagnostics }, modifier = Modifier.heightIn(min = 48.dp)) {
                Text(if (showDiagnostics) "收起诊断信息" else "查看诊断信息")
            }
        }
        state.intents.lastOrNull { it.command == "get_status" }
            ?.takeIf { it.stage != DeliveryStage.APPLIED }?.let { IntentText(it) }
    }
    LogsCard(state, model, busy)
}

private fun pendingSettingsOperations(state: WorkbenchProjection) = state.intents.filter {
    it.stage != DeliveryStage.APPLIED && (it.stage != DeliveryStage.QUARANTINED || it.reviewed ||
        !state.requiresReview || state.serviceRecoveryActive)
}

@Composable
internal fun OperationQueuePage(state: WorkbenchProjection, model: WorkbenchViewModel, busy: Boolean) {
    val queue = pendingSettingsOperations(state)
    if (queue.isEmpty()) NoticeStrip("没有待处理操作。") else DesignCard {
        val review = queue.filter { it.stage == DeliveryStage.QUARANTINED && !it.reviewed }
        val recent = queue.filterNot { it in review }.takeLast(30).asReversed()
        (review + recent).forEach { QueueEntry(it, state.connection.localOnline, model, busy) }
    }
}

@Composable
private fun StorageStatus(state: WorkbenchProjection, model: WorkbenchViewModel, refresh: Int) {
    val status = state.reads["get_status"] ?: return
    if ("get_storage_status" !in status.obj("protocol")?.stringList("supportedCommands").orEmpty()) return
    val requests = state.currentIntents("get_storage_status")
    val request = requests.lastOrNull()
    val result = requests.asReversed().firstNotNullOfOrNull { it.result }
    val pair = state.pairId
    val epoch = state.epoch
    val online = state.connection.localOnline
    val canRead = online && state.connection.synced && !state.requiresReview && !state.serviceRecoveryActive
    LaunchedEffect(pair, epoch, canRead, refresh) {
        if (canRead && pair != null && epoch != null && request?.stage !in pendingStages) {
            model.readMarket("get_storage_status", buildJsonObject {}, pair, epoch)
        }
    }
    Column(Modifier.fillMaxWidth().padding(bottom = 8.dp), verticalArrangement = Arrangement.spacedBy(3.dp)) {
        SettingTitle("本地存储与备份")
        when {
            result == null && request?.stage in pendingStages -> ChatMeta("正在读取权威目录占用。")
            result == null && request?.errorCode != null -> ChatMeta("读取失败：${request.errorCode}。可刷新服务状态重试。", DeskColors.amber)
            result == null -> ChatMeta("尚未读取存储状态。", DeskColors.faint)
            result.text("status") != "available" -> ChatMeta(
                "占用或可用空间未知（${result.text("reason") ?: "测量失败"}）；不以手工读数冒充。",
                DeskColors.amber,
            )
            else -> {
                ChatMeta(
                    "原图库 ${formatBytes(result.number("originalsBytes"))} · " +
                        "隔离区 ${formatBytes(result.number("quarantineBytes"))} · " +
                        "暂存 ${formatBytes(result.number("stagingBytes"))}",
                )
                ChatMeta("磁盘可用 ${formatBytes(result.number("availableBytes"))}")
                ChatMeta(
                    result.number("lastSuccessfulBackupAt")?.let { "最近成功备份 ${timeText(it)}" }
                        ?: "尚无同版成功备份记录",
                    if (result.number("lastSuccessfulBackupAt") == null) DeskColors.amber else DeskColors.faint,
                )
            }
        }
    }
}

private fun formatBytes(bytes: Long?): String {
    if (bytes == null) return "未知"
    val units = listOf("B", "KiB", "MiB", "GiB", "TiB")
    var value = bytes.toDouble()
    var index = 0
    while (value >= 1024 && index < units.lastIndex) {
        value /= 1024
        index++
    }
    return if (index == 0) "${bytes} ${units[index]}" else "%.1f %s".format(Locale.ROOT, value, units[index])
}

@Composable
private fun HistoryCoverageStatus(state: WorkbenchProjection, model: WorkbenchViewModel, refresh: Int) {
    val status = state.reads["get_status"] ?: return
    if ("get_history_coverage" !in status.obj("protocol")?.stringList("supportedCommands").orEmpty()) return
    val requests = state.currentIntents("get_history_coverage")
    val request = requests.lastOrNull()
    val result = requests.asReversed().firstNotNullOfOrNull { it.result }
    val pair = state.pairId
    val epoch = state.epoch
    val online = state.connection.localOnline
    val canRead = online && state.connection.synced && !state.requiresReview && !state.serviceRecoveryActive
    LaunchedEffect(pair, epoch, canRead, refresh) {
        if (canRead && pair != null && epoch != null && request?.stage !in pendingStages) {
            model.readMarket("get_history_coverage", buildJsonObject {}, pair, epoch)
        }
    }
    val coverage = result?.obj("coverage")
    val statusLabel = when (result?.text("status")) {
        "ready" -> "数据就绪"
        "partial" -> "部分覆盖"
        "unavailable" -> "暂不可用"
        "disabled" -> "未启用"
        else -> "待核对"
    }
    val detail = when {
        !online -> if (result == null) "工作台离线，暂无覆盖统计。" else "工作台离线，显示上次核对。"
        request?.stage in pendingStages -> if (result == null) "正在读取历史覆盖。" else "正在刷新，显示上次核对。"
        request?.errorCode != null -> if (result == null) "读取失败，可用「刷新服务状态」重试。" else
            "读取失败，显示上次核对。可用「刷新服务状态」重试。"
        result?.text("status") == "unavailable" && coverage != null -> "当前覆盖状态未确认，显示上次记录。"
        else -> null
    }
    Column(Modifier.fillMaxWidth().padding(bottom = 8.dp), verticalArrangement = Arrangement.spacedBy(3.dp)) {
        SettingTitle("全市场历史覆盖 · $statusLabel")
        ChatMeta("沪深主板与创业板 · 不复权日线")
        if (coverage != null) {
            ChatMeta("数据源 ${coverage.value("source")} · ${coverage.value("start")} 至 ${coverage.value("through")} · 应至 ${coverage.value("expectedThrough")}")
            ChatMeta("已观察证券 ${coverage.value("observedSymbols")} / ${coverage.value("requestedSymbols")} · ${coverage.value("rows")} 条历史记录")
            val latest = coverage.obj("latestDay")
            val observed = latest?.number("observed")
            val expected = latest?.number("expected")
            if (observed != null && expected != null) {
                val gap = (expected - observed).coerceAtLeast(0L)
                ChatMeta("最近交易日已观察 $observed / $expected · 缺 $gap", if (gap > 0L) DeskColors.amber else DeskColors.faint)
            }
            val attribution = coverage.obj("attribution")
            if (attribution != null) {
                val missing = attribution.number("missingCells") ?: 0L
                val attributed = attribution.number("attributedCells") ?: 0L
                val unexplained = attribution.number("unexplainedCells") ?: 0L
                val window = attribution.obj("window")
                val sources = attribution.obj("sources")
                val complete = attribution.text("status") == "all_attributed"
                ChatMeta(
                    "缺失归因 ${if (complete) "全部可解释" else "部分未解释"} · 缺失 $missing 单元 · 交易所停牌解释 $attributed" +
                        if (unexplained > 0L) " · 未解释 $unexplained" else "",
                    if (unexplained > 0L) DeskColors.amber else DeskColors.faint,
                )
                if (window != null) {
                    ChatMeta("归因窗口 ${window.value("start")} 至 ${window.value("end")} · ${window.value("sessions")} 个交易日")
                }
                if (sources != null) {
                    ChatMeta("停牌证据 · 上交所 ${sources.value("sseDays")} 日 · 深交所 ${sources.value("szseDays")} 日 · 归档 ${sources.value("archiveDays")} 日")
                }
            } else {
                ChatMeta("缺失归因待工作台同步", DeskColors.faint)
            }
            val conflicts = coverage.number("conflictingRows") ?: 0L
            if (conflicts > 0L) ChatMeta("冲突记录 $conflicts 条", DeskColors.amber)
            ChatMeta("数据更新 ${evidenceTimeText(coverage.number("updatedAt"))}")
        }
        val reason = when (result?.text("reason")) {
            "history_pending" -> "全市场历史数据正在准备。"
            "provider_pending" -> "数据源历史资料准备中。"
            "source_unavailable" -> "历史数据来源暂不可用。"
            "snapshot_invalid" -> "历史覆盖记录未通过核验。"
            "refresh_failed" -> "最近一次历史数据更新失败。"
            else -> null
        }
        reason?.let { ChatMeta(it, DeskColors.amber) }
        result?.number("checkedAt")?.let { ChatMeta("工作台核对时间 ${evidenceTimeText(it)}") }
        detail?.let { ChatMeta(it, DeskColors.amber) }
    }
}

private fun workbenchConnectionLabel(state: WorkbenchProjection): String {
    val connection = state.connection
    return when {
        !connection.paired -> "待配对"
        state.connectionError != null -> "连接异常"
        connection.recoveryRequired || state.serviceRecoveryActive -> "恢复待核对"
        !connection.relayOnline -> "未连接"
        !connection.localOnline -> "本地工作台离线"
        state.syncError != null -> "同步未完成"
        !connection.synced -> "同步中"
        else -> "已连接"
    }
}

@Composable
internal fun WorkstationSettingsPage(
    state: WorkbenchProjection,
    model: WorkbenchViewModel,
    busy: Boolean,
    scan: () -> Unit,
    importConfig: () -> Unit,
) {
    val connection = state.connection
    val pairingDisplay by model.pairingDisplay.collectAsStateWithLifecycle()
    val pairing = pairingDisplay?.takeIf { connection.paired && it.pairId == state.pairId }
    val label = workbenchConnectionLabel(state)
    Surface(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(20.dp),
        color = DeskColors.panel,
        border = BorderStroke(1.dp, DeskColors.blue.copy(alpha = .26f)),
    ) {
        Column(
            Modifier.background(Brush.linearGradient(listOf(Color(0xFF192742), DeskColors.panel))).padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp), verticalAlignment = Alignment.Top) {
                Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(3.dp)) {
                    ChatMeta("工作台配对 · TLS", DeskColors.blue)
                    Text(
                        state.pairId ?: "连接本地工作台",
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.Bold,
                    )
                    if (!connection.paired) ChatMeta("扫描管理员提供的二维码，核对工作台标识和指纹。")
                }
                Box(Modifier.weight(.60f, fill = false)) {
                    StatusBadge(label, if (label == "已连接") DeskColors.green else DeskColors.amber)
                }
            }
            pairing?.endpoint?.let {
                Text(it, color = DeskColors.secondary, style = MaterialTheme.typography.labelMedium, fontFamily = deskMono)
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                WorkstationCell("公钥指纹 · SHA-256", pairing?.pinSummary ?: "配对时核验", Modifier.weight(1f))
                WorkstationCell("最近同步", state.synchronizedAt?.let(::timeText) ?: "尚未同步", Modifier.weight(1f))
            }
            IconAction("工作台扫码配对", DeskGlyph.Qr, !busy, scan)
            SideBySide {
                TextButton(onClick = importConfig, enabled = !busy, modifier = Modifier.heightIn(min = 48.dp)) {
                    Text("导入配对配置")
                }
                if (connection.paired) {
                    TextButton(onClick = model::retry, enabled = !busy, modifier = Modifier.heightIn(min = 48.dp)) {
                        Text("重新连接与同步")
                    }
                }
            }
        }
    }
}

@Composable
private fun WorkstationCell(title: String, value: String, modifier: Modifier) {
    Surface(modifier, color = DeskColors.field, shape = RoundedCornerShape(11.dp),
        border = BorderStroke(1.dp, DeskColors.border)) {
        Column(Modifier.padding(horizontal = 10.dp, vertical = 8.dp), verticalArrangement = Arrangement.spacedBy(3.dp)) {
            ChatMeta(title)
            Text(value, style = MaterialTheme.typography.labelMedium,
                fontWeight = FontWeight.SemiBold, fontFamily = deskMono)
        }
    }
}

@Composable
private fun IconAction(label: String, glyph: DeskGlyph, enabled: Boolean, onClick: () -> Unit) {
    Button(
        onClick = onClick, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp),
        enabled = enabled, shape = RoundedCornerShape(12.dp),
        colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF2563EB), contentColor = Color.White),
    ) {
        DeskIcon(glyph, LocalContentColor.current, Modifier.size(18.dp))
        Spacer(Modifier.width(8.dp))
        Text(label)
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun SideBySide(content: @Composable FlowRowScope.() -> Unit) {
    FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(4.dp), content = content)
}

@Composable
private fun SettingsGroup(title: String, caption: String, content: @Composable ColumnScope.() -> Unit) {
    Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
        SectionTitle(title, caption)
        DesignCard(content = content)
    }
}

@Composable
private fun SettingsOverviewGroup(title: String, content: @Composable ColumnScope.() -> Unit) {
    Column(verticalArrangement = Arrangement.spacedBy(5.dp)) {
        Text(title, Modifier.padding(start = 8.dp).semantics { heading() },
            style = MaterialTheme.typography.labelLarge, color = DeskColors.muted)
        Surface(shape = RoundedCornerShape(14.dp), color = DeskColors.panel, border = BorderStroke(1.dp, DeskColors.border)) {
            Column(Modifier.fillMaxWidth(), content = content)
        }
    }
}

@Composable
private fun SettingsLink(glyph: DeskGlyph, label: String, value: String? = null, onClick: () -> Unit) {
    Row(Modifier.fillMaxWidth().heightIn(min = 49.dp).clickable(role = Role.Button, onClick = onClick)
        .padding(horizontal = 13.dp, vertical = 8.dp), horizontalArrangement = Arrangement.spacedBy(10.dp),
        verticalAlignment = Alignment.CenterVertically) {
        DeskIcon(glyph, DeskColors.blue, Modifier.size(18.dp))
        Text(label, Modifier.weight(1f), style = MaterialTheme.typography.bodyMedium, fontWeight = FontWeight.SemiBold)
        value?.let { Text(it, Modifier.weight(.84f), style = MaterialTheme.typography.labelMedium,
            textAlign = TextAlign.End, color = DeskColors.muted) }
        Icon(painterResource(R.drawable.ic_ultra_chevron), null, Modifier.size(14.dp), DeskColors.faint)
    }
}

@Composable
private fun SettingsRow(
    glyph: DeskGlyph,
    label: String,
    detail: String,
    iconTint: Color = DeskColors.blue,
    trailing: @Composable (() -> Unit)? = null,
) {
    Row(Modifier.fillMaxWidth().padding(vertical = 4.dp), horizontalArrangement = Arrangement.spacedBy(8.dp),
        verticalAlignment = Alignment.CenterVertically) {
        DeskIcon(glyph, iconTint, Modifier.padding(top = 2.dp).size(18.dp).align(Alignment.Top))
        Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            SettingTitle(label)
            ChatMeta(detail)
        }
        if (trailing != null) Box(Modifier.weight(.72f, fill = false)) { trailing() }
    }
}

@Composable
private fun SettingTitle(text: String) {
    Text(text, style = MaterialTheme.typography.bodyMedium, fontWeight = FontWeight.SemiBold)
}

@Composable
private fun ReminderLevel(level: String, tint: Color, title: String, detail: String) {
    Row(Modifier.padding(vertical = 5.dp), horizontalArrangement = Arrangement.spacedBy(10.dp),
        verticalAlignment = Alignment.Top) {
        Box(Modifier.weight(.45f, fill = false)) { StatusBadge(level, tint) }
        Column(Modifier.weight(1f)) {
            SettingTitle(title)
            ChatMeta(detail)
        }
    }
}

@Composable
private fun DependencyStatus(dependency: JsonObject?, showDiagnostics: Boolean) {
    val tint = when (dependency?.text("status")) {
        "online" -> DeskColors.green
        "offline", "degraded" -> DeskColors.amber
        else -> DeskColors.faint
    }
    Column(horizontalAlignment = Alignment.End) {
        ChatMeta(dependencyLabel(dependency), tint)
        if (showDiagnostics) {
            dependency?.number("checkedAt")?.let { ChatMeta(timeText(it)) }
            dependency?.text("errorCode")?.let { ChatMeta(it, tint) }
        }
    }
}

private fun dependencyLabel(dependency: JsonObject?) = when (dependency?.text("status")) {
    "online" -> "可用"
    "offline" -> "不可用"
    "degraded" -> "降级"
    else -> "未知"
}

@Composable
private fun LogsCard(state: WorkbenchProjection, model: WorkbenchViewModel, busy: Boolean) {
    val logs = state.reads["get_logs"]?.objects("items")
    var show by rememberSaveable { mutableStateOf(false) }
    var exported by rememberSaveable { mutableStateOf<String?>(null) }
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val export = rememberLauncherForActivityResult(ActivityResultContracts.CreateDocument("text/plain")) { destination ->
        if (destination != null && logs != null) {
            scope.launch {
                exported = withContext(Dispatchers.IO) {
                    runCatching {
                        val output = requireNotNull(context.contentResolver.openOutputStream(destination, "w"))
                        output.bufferedWriter(Charsets.UTF_8).use { writer ->
                            writer.appendLine("TickDesk 本地服务诊断日志")
                            for (item in logs) {
                                writer.appendLine("${timeText(item.number("at"))} · ${item.value("level")} · ${item.value("code")}")
                                writer.appendLine(item.value("text"))
                            }
                        }
                    }.fold({ "本页日志已导出。" }, { "无法写入日志文件。" })
                }
            }
        }
    }
    DesignCard {
        SettingsRow(DeskGlyph.File, "运行诊断日志", "读取并导出本地服务的脱敏日志") {
            TextButton(
                enabled = logs != null || (state.connection.localOnline && !busy),
                onClick = {
                    if (logs == null) model.submit("get_logs")
                    show = true
                },
                modifier = Modifier.heightIn(min = 48.dp),
            ) { Text("查看") }
        }
        if (show) {
            when {
                logs == null -> ChatMeta("等待服务日志。")
                logs.isEmpty() -> ChatMeta("没有可显示的日志。")
                else -> logs.forEach {
                    HorizontalDivider(color = DeskColors.border)
                    ChatMeta("${timeText(it.number("at"))} · ${it.value("level")} · ${it.value("code")}")
                    Text(it.value("text"), style = MaterialTheme.typography.bodyMedium)
                }
            }
            SideBySide {
                if (!logs.isNullOrEmpty()) {
                    TextButton(onClick = { export.launch("tickdesk-service-logs.txt") }, modifier = Modifier.heightIn(min = 48.dp)) {
                        Text("导出本页日志")
                    }
                }
                TextButton(onClick = { show = false }, modifier = Modifier.heightIn(min = 48.dp)) { Text("收起日志") }
            }
            exported?.let { ChatMeta(it) }
        }
    }
}



@Composable
private fun QueueEntry(entry: IntentView, online: Boolean, model: WorkbenchViewModel, busy: Boolean) {
    var review by remember(entry.id) { mutableStateOf(false) }
    IntentText(entry)
    ChatMeta(timeText(entry.at))
    val data = entry.data
    val objectId = data.text("taskId") ?: data.text("symbol") ?: data.text("fileId") ?: data.text("importId")
    objectId?.let { ChatMeta(it) }
    entry.queryState?.let {
        ChatMeta("原键查询 · ${when (it) {
            "found" -> "发现原回执"
            "processing" -> "仍在处理"
            "quarantined" -> "服务端已隔离"
            "not_found" -> "结果未知"
            "hash_mismatch" -> "原载荷摘要不匹配"
            else -> it
        }}")
    }
    if (entry.stage == DeliveryStage.PENDING) {
        TextButton(onClick = { model.cancel(entry.id) }, enabled = !busy, modifier = Modifier.heightIn(min = 48.dp)) {
            Text("取消未发送操作")
        }
    }
    if (entry.stage in setOf(DeliveryStage.SENT, DeliveryStage.DELIVERED, DeliveryStage.QUARANTINED)) {
        TextButton(onClick = { model.query(entry.id) }, enabled = !busy && online, modifier = Modifier.heightIn(min = 48.dp)) {
            Text("查询原操作结果")
        }
    }
    if (entry.stage == DeliveryStage.QUARANTINED && !entry.reviewed) {
        TextButton(onClick = { review = true }, enabled = !busy, modifier = Modifier.heightIn(min = 48.dp)) { Text("标记已人工核对") }
    }
    if (entry.reviewed) ChatMeta("已人工核对 · 原记录保留")
    if (review) Confirm("记录人工核对", "确认你已经在工作台核对这条原操作。原意图和结果会保留在本机。",
        busy, { review = false }) { model.review(entry.id); review = false }
    HorizontalDivider(color = DeskColors.border)
}

@Composable
internal fun AnalysisPage(state: WorkbenchProjection, model: WorkbenchViewModel, busy: Boolean) {
    val analysis = state.meta?.obj("analysis")
    val paused = analysis?.flag("paused")
    val pending = state.intents.filter { it.command in setOf("pause", "resume") }
    val tint = when (paused) { true -> DeskColors.amber; false -> DeskColors.green; null -> DeskColors.faint }
    Column(Modifier.fillMaxWidth().padding(top = 12.dp, bottom = 8.dp),
        horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(8.dp)) {
        val heroGlyph = when (paused) { true -> DeskGlyph.Pause; false -> DeskGlyph.Play; null -> DeskGlyph.Clock }
        Box(Modifier.size(128.dp), contentAlignment = Alignment.Center) {
            Canvas(Modifier.fillMaxSize()) {
                drawCircle(Brush.radialGradient(listOf(tint.copy(alpha = .19f), Color.Transparent),
                    radius = 64.dp.toPx()), radius = 64.dp.toPx())
                listOf(37, 51, 63).forEach { radius ->
                    drawCircle(tint.copy(alpha = if (radius == 51) .37f else .14f), radius.dp.toPx(),
                        style = Stroke(1.dp.toPx()))
                }
            }
            DeskIcon(heroGlyph, tint, Modifier.size(32.dp))
        }
        Text(when (paused) {
            true -> "盘中量化扫描已暂停"
            false -> "盘中量化扫描运行中"
            null -> "分析状态尚未同步"
        }, Modifier.semantics { heading() }, style = MaterialTheme.typography.headlineSmall,
            fontWeight = FontWeight.Bold, textAlign = TextAlign.Center)
        Text(when (paused) {
            true -> "工作台已挂起定时扫描与新信号生成。已有事项仍按有效期倒计时，照常提醒。"
            false -> "工作台按交易时段扫描并生成新信号。暂停扫描后，已有事项照常提醒。"
            null -> "同步工作台后可查看最后确认状态并提交操作。"
        }, Modifier.widthIn(max = 290.dp), textAlign = TextAlign.Center, color = DeskColors.muted,
            style = MaterialTheme.typography.bodyMedium)
    }
    DesignCard {
        SettingsRow(DeskGlyph.Scan, when (paused) {
            true -> "新扫描任务已挂起"
            false -> "分析按扫描时段运行"
            null -> "暂停控制扫描与新事项"
        }, "暂停时中止当前扫描批次，未发布的中间结果不会生成事项。", DeskColors.amber)
        HorizontalDivider(color = DeskColors.border)
        SettingsRow(DeskGlyph.Bell, "存量事项继续按规则提醒",
            "仍可稍后处理、回填成交或忽略，未处理事项按原有效期过期。", DeskColors.amber)
        HorizontalDivider(color = DeskColors.border)
        SettingsRow(DeskGlyph.Clock, "恢复运行对齐扫描时段",
            "扫描时段内恢复会执行一轮，其他时段等待下一扫描窗口。", DeskColors.amber)
    }

    if (paused != null) {
        val criticalTasks = state.rows("tasks").filter { it.text("level") == "critical" }
        var now by remember { mutableLongStateOf(System.currentTimeMillis()) }
        LaunchedEffect(criticalTasks) {
            now = System.currentTimeMillis()
            while (criticalTasks.any { it.activeTask(now) }) {
                delay(1000)
                now = System.currentTimeMillis()
            }
        }
        val critical = criticalTasks.filter { it.activeTask(now) }.minByOrNull { it.number("validUntil")!! }
        if (critical != null) {
            val remaining = ((critical.number("validUntil")!! - now + 999) / 1000).coerceAtLeast(0)
            DesignCard {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(5.dp)) {
                        StatusBadge("存量紧急事项", DeskColors.red)
                        val direction = when (critical.text("direction")) {
                            "sell" -> "持仓卖点"
                            "buy" -> "买点"
                            else -> "风险"
                        }
                        SettingTitle("${critical.value("name")} · $direction")
                        critical.text("symbol")?.let { ChatMeta(it) }
                    }
                    Column(Modifier.weight(.65f, fill = false), horizontalAlignment = Alignment.End) {
                        Text(String.format(Locale.ROOT, "%02d:%02d", remaining / 60, remaining % 60),
                            color = DeskColors.red, style = MaterialTheme.typography.titleMedium,
                            fontFamily = deskMono, fontWeight = FontWeight.Bold)
                        ChatMeta("决策窗口剩余")
                    }
                }
            }
        }
    }
    if (pending.isNotEmpty()) DesignCard {
        SectionTitle("操作回执")
        pending.takeLast(3).forEach { IntentText(it) }
        val version = analysis?.number("controlVersion")
        if (paused == false && version != null) {
            val resume = pending.lastOrNull { it.command == "resume" }?.result
            resume?.takeIf { it.number("controlVersion") == version }?.obj("scan")?.let { scan ->
                when (scan.text("status")) {
                    "accepted" -> ChatMeta("恢复后的扫描已受理 · 批次 ${scan.value("scanId")}", DeskColors.blue)
                    "rejected" -> ChatMeta(when (scan.text("code")) {
                        "SCAN_OUTSIDE_WINDOW" -> "恢复时不在扫描时段，等待下一扫描窗口"
                        "SCAN_DATA_STALE" -> "恢复时的行情已过期，首轮扫描被拒绝"
                        "DATA_UNAVAILABLE" -> "恢复时数据不可用，首轮扫描被拒绝"
                        else -> "恢复后的首轮扫描未通过 · ${scan.value("code")}"
                    }, DeskColors.amber)
                    else -> Unit
                }
            }
        }
    }
    if (analysis?.text("lastScanId") != null) DesignCard { AnalysisScanSummary(state) }
    if (paused == null) return
    var confirm by rememberSaveable { mutableStateOf(false) }
    val action = if (paused) "恢复盘中量化扫描" else "暂停盘中量化扫描"
    IconAction(action, if (paused) DeskGlyph.Play else DeskGlyph.Pause,
        state.canQueue && !busy && pending.none { it.stage in pendingStages }) { confirm = true }
    Text(if (paused) "恢复指令经 TLS 通道同步 · 重试沿用同一操作"
        else "暂停结果以工作台回执和已同步状态为准",
        Modifier.fillMaxWidth(), textAlign = TextAlign.Center, style = MaterialTheme.typography.labelSmall, color = DeskColors.faint)
    if (confirm) Confirm(action, "工作台确认并同步后，更新分析状态。", busy, { confirm = false }) {
        model.submit(if (paused) "resume" else "pause") { confirm = false }
    }
}

@Composable
private fun AnalysisScanSummary(state: WorkbenchProjection) {
    val scanId = state.meta?.obj("analysis")?.text("lastScanId")
    if (scanId == null) {
        ChatMeta(if (state.meta == null) "扫描批次尚未同步" else "暂无已确认扫描批次")
        return
    }
    val scan = sequenceOf(state.reads["scan_result"], state.reads["scan_now"], state.reads["get_candidates"]?.obj("scan"))
        .filterNotNull().firstOrNull { it.text("scanId") == scanId }
    val status = when (scan?.text("status")) {
        "succeeded" -> "已完成"
        "partial" -> "部分失败"
        "failed" -> "失败"
        "rejected" -> "已拒绝"
        "skipped" -> "已跳过"
        "suppressed" -> "已抑制"
        else -> null
    }
    ChatMeta("最近确认批次 · $scanId" + (status?.let { " · $it" } ?: ""))
    scan?.number("dataAsOf")?.let { ChatMeta("行情快照 ${timeText(it)}") }
}

private data class ReminderPermissions(val notifications: Boolean, val urgentImportance: Int?, val fullScreen: Boolean?) {
    val channelBlocked get() = urgentImportance == NotificationManager.IMPORTANCE_NONE
    val notificationLabel: String
        get() = when {
            !notifications -> "未开启"
            channelBlocked -> "渠道已关闭"
            else -> "已允许"
        }
    val urgentChannelLabel: String
        get() = when {
            urgentImportance == null -> "重要通知渠道 · 尚未建立"
            channelBlocked -> "重要通知渠道 · 已关闭"
            else -> "重要通知渠道 · 已开启"
        }
    val fullScreenLabel: String
        get() = when (fullScreen) {
            true -> "全屏通知权限 · 系统允许"
            false -> "全屏通知权限 · 系统未授权"
            null -> "当前 Android 版本无独立全屏权限状态"
        }
}

private fun NotificationManager.permissions() = ReminderPermissions(
    notifications = areNotificationsEnabled(),
    urgentImportance = getNotificationChannel("tickdesk_urgent")?.importance,
    fullScreen = if (Build.VERSION.SDK_INT >= 34) canUseFullScreenIntent() else null,
)

@Composable
private fun rememberReminderPermissions(): ReminderPermissions {
    val context = LocalContext.current
    val manager = context.getSystemService(NotificationManager::class.java)
    var permissions by remember(manager) { mutableStateOf(manager.permissions()) }
    LifecycleEventEffect(Lifecycle.Event.ON_RESUME) { permissions = manager.permissions() }
    return permissions
}

@Composable
internal fun ReminderSettingsPage(state: WorkbenchProjection, model: WorkbenchViewModel, busy: Boolean) {
    val context = LocalContext.current
    val manager = context.getSystemService(NotificationManager::class.java)
    var permissions by remember(manager) { mutableStateOf(manager.permissions()) }
    LifecycleEventEffect(Lifecycle.Event.ON_RESUME) { permissions = manager.permissions() }
    val permission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) {
        permissions = manager.permissions()
    }
    SettingsGroup("系统权限", "本机实时回读") {
        SettingsRow(DeskGlyph.Bell, "系统通知", permissions.notificationLabel)
        ChatMeta(permissions.urgentChannelLabel)
        HorizontalDivider(color = DeskColors.border)
        SettingsRow(DeskGlyph.Phone, "锁屏强提醒", permissions.fullScreenLabel)
        ChatMeta("Android 隐藏敏感内容时显示脱敏通知。")
        if (!permissions.notifications && Build.VERSION.SDK_INT >= 33) {
            OutlinedButton(onClick = { permission.launch(Manifest.permission.POST_NOTIFICATIONS) },
                modifier = Modifier.heightIn(min = 48.dp)) { Text("申请通知权限") }
        }
        OutlinedButton(onClick = {
            context.startActivity(Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                .putExtra(Settings.EXTRA_APP_PACKAGE, context.packageName))
        }, modifier = Modifier.heightIn(min = 48.dp)) { Text("打开 Android 通知设置") }
    }
    var showRules by rememberSaveable { mutableStateOf(false) }
    TextButton(onClick = { showRules = !showRules }, modifier = Modifier.heightIn(min = 48.dp)) {
        Text(if (showRules) "收起提醒规则" else "查看提醒规则")
    }
    if (showRules) SettingsGroup("分级提醒", "工作台规则") {
        ReminderLevel("严重", DeskColors.red, "前台应用内提醒 · 后台通知", "创建时和 5 分钟后提醒，之后每 3 分钟")
        HorizontalDivider(color = DeskColors.border)
        ReminderLevel("警告", DeskColors.amber, "系统通知", "创建时与 5 分钟后提醒")
        HorizontalDivider(color = DeskColors.border)
        ReminderLevel("信息", DeskColors.blue, "普通通知", "创建时提醒一次")
    }
    val settings = state.meta?.obj("settings")
    if (settings == null) {
        NoticeStrip("工作台提醒设置尚未同步。")
        return
    }
    val revision = settings.number("settingsRevision")!!
    var start by rememberSaveable(revision) { mutableStateOf(settings.text("nightStart").orEmpty()) }
    var end by rememberSaveable(revision) { mutableStateOf(settings.text("nightEnd").orEmpty()) }
    var night by rememberSaveable(revision) { mutableStateOf(settings.flag("criticalAtNight") == true) }
    var push by rememberSaveable(revision) { mutableStateOf(settings.flag("pushEnabled") == true) }
    SettingsGroup("夜间免打扰", "以工作台确认为准") {
        Field("夜间开始 HH:mm", start, { start = it })
        Field("夜间结束 HH:mm", end, { end = it })
        CheckRow("夜间允许严重提醒", night) { night = it }
        CheckRow("工作台启用推送", push) { push = it }
        ChatMeta("最后确认 · ${settings.value("nightStart")}–${settings.value("nightEnd")} · 推送${if (settings.flag("pushEnabled") == true) "开启" else "关闭"}")
    }
    val pending = state.intents.filter { it.command == "set_settings" }.takeLast(3)
    pending.forEach { IntentText(it) }
    val clock = Regex("([01][0-9]|2[0-3]):[0-5][0-9]")
    MainAction("提交提醒设置", state.canQueue && !busy && clock.matches(start) && clock.matches(end) &&
        pending.none { it.stage in pendingStages }) {
        model.submit("set_settings", buildJsonObject {
            put("baseSettingsRevision", revision)
            put("reminderModes", settings.obj("reminderModes")!!)
            put("nightStart", start)
            put("nightEnd", end)
            put("criticalAtNight", night)
            put("pushEnabled", push)
        })
    }
}


@Composable
internal fun ChatPage(
    state: WorkbenchProjection,
    model: WorkbenchViewModel,
    busy: Boolean,
    go: (String, String) -> Unit,
) {
    val messages = state.reads["get_messages"]
    val cursor = messages?.text("nextCursor")
    val items = rememberChatItems(state)
    if (items.isEmpty()) {
        DesignCard {
            SectionTitle(if (messages == null) "对话历史尚未同步" else "暂无对话")
            if (messages == null) ChatHistoryAction(state, model, busy, null)
        }
    } else {
        if (messages == null || cursor != null) ChatHistoryAction(state, model, busy, cursor)
        items.forEachIndexed { index, item ->
            val previous = items.getOrNull(index - 1)
            if (previous == null || chatDate(item.at) != chatDate(previous.at)) ChatDay(item.at)
            key(item.requestId, item.sentByUser) { ChatBubble(item, state, go) { id -> go("rich", id) } }
        }
    }
}

@Composable
private fun ChatHistoryAction(state: WorkbenchProjection, model: WorkbenchViewModel, busy: Boolean, cursor: String?) {
    TextButton(enabled = state.connection.localOnline && !busy, modifier = Modifier.heightIn(min = 48.dp), onClick = {
        model.submit("get_messages", buildJsonObject { if (cursor != null) put("cursor", cursor) })
    }) { Text(if (cursor == null) "读取对话历史" else "加载更早的对话") }
}

private data class ChatItem(
    val requestId: String,
    val at: Long?,
    val text: String,
    val sentByUser: Boolean,
    val stage: String? = null,
    val reply: JsonObject? = null,
    val reportTarget: String? = null,
)

private const val CHAT_TEXT_LIMIT = 8000
private const val REPORT_EXCERPT_LIMIT = 1600
private const val REPORT_PREFIX = "【TickDesk 报告追问】"
private const val REPORT_EXCERPT_LABEL = "上文摘录（未核验，仅供理解指代）："
private const val QUESTION_SEPARATOR = "\n追问：\n"
private const val ORIGINAL_QUESTION = "【按原报告】"
private const val CURRENT_QUESTION = "【按当前情况】"

private fun String.takeChatText(limit: Int): String = take(limit).trimEnd(Char::isHighSurrogate)

private fun reportQuestionPrefix(target: String, reply: JsonObject): String {
    val excerpt = buildString {
        for (char in reply.value("text").takeChatText(REPORT_EXCERPT_LIMIT)) {
            append(if (char.isISOControl()) ' ' else char)
        }
    }
    // The v2 chat command only has text. Keep the report reference recoverable from chat history.
    return "$REPORT_PREFIX$target\n$REPORT_EXCERPT_LABEL${JsonPrimitive(excerpt)}$QUESTION_SEPARATOR"
}

private fun chatUserItem(id: String, at: Long?, rawText: String, stage: String): ChatItem {
    val target = rawText.takeIf {
        it.startsWith(REPORT_PREFIX) && it.substringAfter('\n', "").startsWith(REPORT_EXCERPT_LABEL) && QUESTION_SEPARATOR in it
    }
        ?.substringBefore('\n')?.removePrefix(REPORT_PREFIX)?.takeIf(idPattern::matches)
    val text = if (target == null) rawText else rawText.substringAfter(QUESTION_SEPARATOR)
        .removePrefix(ORIGINAL_QUESTION).removePrefix(CURRENT_QUESTION)
    return ChatItem(id, at, text, true, stage, reportTarget = target)
}

@Composable
private fun rememberChatItems(state: WorkbenchProjection) = remember(state.messages, state.replies, state.intents) {
    chatItems(state)
}

private fun chatItems(state: WorkbenchProjection): List<ChatItem> {
    val intents = state.intents.filter { it.command == "chat" }.associateBy { it.id }
    val replies = state.replies.mapNotNull { reply -> reply.text("requestId")?.let { it to reply } }.toMap()
    val userRows = state.messages.filter { it.text("role") == "user" }
        .mapNotNull { row -> row.text("requestId")?.let { it to row } }.toMap()
    val assistantRows = state.messages.filter { it.text("role") == "assistant" }
        .mapNotNull { row -> row.text("requestId")?.let { it to row } }.toMap()
    val userItems = (userRows.keys + intents.keys).associateWith { id ->
        val row = userRows[id]
        val intent = intents[id]
        val reply = assistantRows[id]?.obj("reply") ?: replies[id]
        val stage = when {
            reply?.text("status") == "failed" -> "答复失败"
            reply != null -> "已处理"
            intent?.errorCode != null -> "${intent.stage.label()} · ${intent.errorCode}"
            intent != null -> intent.stage.label()
            else -> "已受理 · 等待答复"
        }
        val text = row?.text("text") ?: intent?.data?.text("text") ?: "未知"
        chatUserItem(id, row?.number("at") ?: intent?.at, text, stage)
    }
    val assistantItems = (assistantRows.keys + replies.keys).mapNotNull { id ->
        val row = assistantRows[id]
        val reply = row?.obj("reply") ?: replies[id] ?: return@mapNotNull null
        ChatItem(id, row?.number("at") ?: reply.number("receivedAt"), reply.value("text"), false,
            reply = reply, reportTarget = userItems[id]?.reportTarget)
    }
    return (userItems.values + assistantItems).sortedWith(
        compareBy<ChatItem> { maxOf(it.at ?: Long.MIN_VALUE, userItems[it.requestId]?.at ?: Long.MIN_VALUE) }
            .thenBy { !it.sentByUser }.thenBy { it.requestId },
    )
}

@Composable
internal fun ChatAutoScroll(state: WorkbenchProjection, reportTarget: String?, scroll: ScrollState, sentCount: Int) {
    val items = rememberChatItems(state)
    val latest = if (reportTarget == null) items.lastOrNull()
        else items.lastOrNull { it.reportTarget == reportTarget && it.requestId != reportTarget }
    val latestKey = latest?.let { "${it.requestId}:${it.sentByUser}" }
    val threshold = with(LocalDensity.current) { 72.dp.roundToPx() }
    var nearBottom by remember(scroll) { mutableStateOf(false) }
    var seenLatest by rememberSaveable { mutableStateOf<String?>(null) }
    var opened by rememberSaveable { mutableStateOf(false) }
    var seenSentCount by remember(scroll) { mutableIntStateOf(sentCount) }

    LaunchedEffect(scroll, threshold) {
        // Content can grow without a scroll gesture. Keep the user's prior position in that case.
        snapshotFlow { scroll.value }.collect { value ->
            nearBottom = scroll.maxValue != Int.MAX_VALUE && scroll.maxValue - value <= threshold
        }
    }
    LaunchedEffect(scroll, latestKey, sentCount) {
        val submitted = sentCount != seenSentCount
        val follow = submitted || (!opened && reportTarget == null) ||
            (opened && latestKey != seenLatest && nearBottom && !scroll.isScrollInProgress)
        seenSentCount = sentCount
        seenLatest = latestKey
        if (latestKey == null) return@LaunchedEffect
        opened = true
        if (follow) {
            withFrameNanos { }
            if (scroll.maxValue != Int.MAX_VALUE && (submitted || !scroll.isScrollInProgress)) {
                scroll.scrollTo(scroll.maxValue)
                nearBottom = true
            }
        }
    }
}

private val chatClock = DateTimeFormatter.ofPattern("HH:mm").withZone(shanghai)
private val chatDayClock = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm").withZone(shanghai)

private fun chatDate(at: Long?): LocalDate? = at?.let { Instant.ofEpochMilli(it).atZone(shanghai).toLocalDate() }
private fun chatTime(at: Long?) = at?.let { chatClock.format(Instant.ofEpochMilli(it)) }
internal fun evidenceTimeText(at: Long?) = at?.let { chatDayClock.format(Instant.ofEpochMilli(it)) } ?: "未知"

@Composable
private fun ChatDay(at: Long?) {
    if (at == null) return
    Text(chatDayClock.format(Instant.ofEpochMilli(at)), Modifier.fillMaxWidth().padding(vertical = 3.dp),
        textAlign = TextAlign.Center, color = DeskColors.faint, style = MaterialTheme.typography.labelSmall,
        fontFamily = deskMono)
}

@Composable
private fun ChatBubble(
    item: ChatItem,
    state: WorkbenchProjection,
    go: (String, String) -> Unit,
    open: ((String) -> Unit)? = null,
) {
    val reply = item.reply
    val sources = reply?.objects("sources").orEmpty()
    val failed = reply?.text("status") == "failed"
    val shape = if (item.sentByUser) RoundedCornerShape(16.dp, 5.dp, 16.dp, 16.dp)
        else RoundedCornerShape(5.dp, 16.dp, 16.dp, 16.dp)
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
        if (item.sentByUser) Spacer(Modifier.weight(.16f))
        else Box(
            Modifier.size(30.dp).background(
                Brush.linearGradient(listOf(Color(0xFFFF6B78), Color(0xFFE11D48))),
                RoundedCornerShape(10.dp),
            ), contentAlignment = Alignment.Center,
        ) {
            DeskIcon(DeskGlyph.Pulse, Color.White, Modifier.size(16.dp))
        }
        Column(Modifier.weight(1f),
            horizontalAlignment = if (item.sentByUser) Alignment.End else Alignment.Start,
            verticalArrangement = Arrangement.spacedBy(5.dp)) {
            if (!item.sentByUser) ChatMeta("TickDesk 投研助手")
            val bubbleColors = if (item.sentByUser) listOf(Color(0xFF2563EB), Color(0xFF1D4ED8))
                else listOf(DeskColors.navy, DeskColors.panel)
            val outline = if (item.sentByUser) DeskColors.blue.copy(alpha = .35f) else DeskColors.border
            Column(
                Modifier.clip(shape).background(Brush.verticalGradient(bubbleColors)).border(1.dp, outline, shape)
                    .padding(horizontal = 14.dp, vertical = 11.dp),
                verticalArrangement = Arrangement.spacedBy(7.dp),
            ) {
                if (failed || reply?.flag("unverified") == true) SideBySide {
                    if (failed) StatusBadge("答复失败", DeskColors.red)
                    if (reply?.flag("unverified") == true) StatusBadge("未核验", DeskColors.amber)
                }
                Text(item.text, style = MaterialTheme.typography.bodyMedium,
                    color = if (item.sentByUser) Color.White else Color(0xFFE2E8F0))
                if (open == null) reply?.text("errorCode")?.let { ChatMeta("错误 · $it", DeskColors.red) }
                if (sources.isNotEmpty()) key(reply?.text("requestId")) {
                    ChatSourceChips(sources) { go("task", it) }
                }
            }
            val contextLabel = "报告追问".takeIf { open != null && item.sentByUser && item.reportTarget != null }
            val footer = listOfNotNull(chatTime(item.at), contextLabel, item.stage).joinToString(" · ")
            if (footer.isNotEmpty()) ChatMeta(footer)
            val id = reply?.text("requestId")
            if (id != null) FullReplyButton(state, id, reply, go)
            if (id != null && open != null) {
                TextButton(onClick = { open(id) }, modifier = Modifier.heightIn(min = 48.dp)) {
                    Text(if (reply.objects("attachments").isEmpty()) "查看答复与来源 →" else "查看图表附件与来源 →")
                }
            }
        }
    }
}

@Composable
internal fun ChatSourceChips(sources: List<JsonObject>, openTask: (String) -> Unit) {
    val uriHandler = LocalUriHandler.current
    var activeIndex by rememberSaveable { mutableIntStateOf(-1) }
    SideBySide {
        sources.forEachIndexed { index, source ->
            val name = source.text("name")?.takeIf(String::isNotBlank) ?: "来源详情"
            val at = chatTime(source.number("asOf"))
            Surface(
                onClick = { activeIndex = index },
                shape = CircleShape,
                color = DeskColors.blue.copy(alpha = .10f),
                contentColor = Color(0xFFBFDBFE),
                border = BorderStroke(1.dp, DeskColors.blue.copy(alpha = .28f)),
            ) {
                Row(
                    Modifier.sizeIn(minWidth = 48.dp, minHeight = 48.dp).padding(horizontal = 10.dp, vertical = 4.dp),
                    horizontalArrangement = Arrangement.spacedBy(6.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(name, Modifier.weight(1f, fill = false), maxLines = 2, overflow = TextOverflow.Ellipsis,
                        style = MaterialTheme.typography.labelMedium)
                    at?.let { Text(it, style = MaterialTheme.typography.labelSmall, fontFamily = deskMono) }
                }
            }
        }
    }
    sources.getOrNull(activeIndex)?.let { source ->
        AlertDialog(
            onDismissRequest = { activeIndex = -1 },
            title = { Text("数据来源") },
            text = {
                Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    SettingTitle(source.text("name")?.takeIf(String::isNotBlank) ?: "来源名称未知")
                    ChatMeta("采样 ${evidenceTimeText(source.number("asOf"))}")
                    val reference = source.text("ref")?.takeIf(String::isNotBlank) ?: "引用标识未知"
                    SelectionContainer { Text(reference, style = MaterialTheme.typography.bodyMedium) }
                    val parts = reference.split(':', limit = 6)
                    if (parts.size == 5 && parts[0] == "local" && parts[1] == "scan" && parts[3] == "task" &&
                        idPattern.matches(parts[2]) && idPattern.matches(parts[4])) {
                        TextButton(onClick = { activeIndex = -1; openTask(parts[4]) },
                            modifier = Modifier.heightIn(min = 48.dp)) { Text("查看量化事项") }
                    }
                    val uri = android.net.Uri.parse(reference)
                    if (uri.scheme in setOf("https", "http") && !uri.host.isNullOrBlank()) {
                        var failed by remember(reference) { mutableStateOf(false) }
                        TextButton(onClick = { failed = runCatching { uriHandler.openUri(reference) }.isFailure },
                            modifier = Modifier.heightIn(min = 48.dp)) { Text("打开来源网页") }
                        if (failed) ChatMeta("无法打开网页，可复制上方地址。", DeskColors.amber)
                    }
                }
            },
            confirmButton = {
                TextButton(onClick = { activeIndex = -1 }, modifier = Modifier.heightIn(min = 48.dp)) { Text("关闭") }
            },
        )
    }
}

@Composable
internal fun ChatComposer(
    state: WorkbenchProjection,
    model: WorkbenchViewModel,
    busy: Boolean,
    draft: String,
    reportTarget: String? = null,
    loadedReport: JsonObject? = null,
    onSubmitted: () -> Unit = {},
    change: (String) -> Unit,
) {
    val followUp = reportTarget != null
    val reply = if (reportTarget != null && loadedReport == null)
        rememberChatItems(state).firstOrNull { it.requestId == reportTarget && !it.sentByUser }?.reply else null
    val report = loadedReport ?: reply?.takeIf { it.text("status") == "completed" }
    var currentQuestion by rememberSaveable(reportTarget) { mutableStateOf(false) }
    val prefix = remember(reportTarget, report, loadedReport, currentQuestion) {
        if (reportTarget == null || report == null) "" else buildString {
            append(reportQuestionPrefix(reportTarget, report))
            if (loadedReport != null) {
                append(if (currentQuestion) CURRENT_QUESTION else ORIGINAL_QUESTION)
                if (currentQuestion) {
                    val symbols = loadedReport.obj("header")?.stringList("symbols").orEmpty()
                    if (symbols.isNotEmpty()) append(symbols.joinToString("、")).append(' ')
                }
            }
        }
    }
    val draftLimit = CHAT_TEXT_LIMIT - prefix.length
    val canChat = state.canQueue && !busy
    val enabled = draft.isNotBlank() && draft.length <= draftLimit && canChat && (!followUp || report != null)
    val latestDraft by rememberUpdatedState(draft)
    val changeDraft by rememberUpdatedState(change)
    val submitted by rememberUpdatedState(onSubmitted)
    val placeholder = if (followUp) "继续追问数据来源与口径…" else "询问持仓、风险或扫描状态…"
    Surface(color = DeskColors.dock) {
        Column {
            HorizontalDivider(color = DeskColors.border)
            Column(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
                verticalArrangement = Arrangement.spacedBy(4.dp)) {
                if (!followUp) ChatCommands(state, model, busy, onSubmitted)
                if (loadedReport != null) SideBySide {
                    FilterChip(selected = !currentQuestion, onClick = { currentQuestion = false },
                        label = { Text("按原报告追问") }, modifier = Modifier.heightIn(min = 48.dp))
                    FilterChip(selected = currentQuestion, onClick = { currentQuestion = true },
                        label = { Text("按当前情况追问") }, modifier = Modifier.heightIn(min = 48.dp))
                }
                Surface(
                    modifier = Modifier.fillMaxWidth(),
                    color = DeskColors.navy,
                    shape = RoundedCornerShape(15.dp),
                    border = BorderStroke(1.dp, DeskColors.border),
                ) {
                    Column(
                        Modifier.padding(start = 11.dp, end = 6.dp, top = 3.dp, bottom = 4.dp),
                        horizontalAlignment = Alignment.CenterHorizontally,
                    ) {
                        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            if (!followUp) DeskIcon(DeskGlyph.Edit, DeskColors.faint, Modifier.size(16.dp))
                            BasicTextField(
                                value = draft,
                                onValueChange = { change(it.takeChatText(draftLimit)) },
                                modifier = Modifier.weight(1f).heightIn(min = 48.dp).semantics {
                                    contentDescription = if (followUp) "继续追问" else "输入消息"
                                },
                                textStyle = MaterialTheme.typography.bodyMedium.copy(color = MaterialTheme.colorScheme.onSurface),
                                maxLines = 3,
                                cursorBrush = SolidColor(DeskColors.blue),
                                decorationBox = { inner ->
                                    Box(Modifier.fillMaxWidth().heightIn(min = 48.dp).padding(vertical = 6.dp),
                                        contentAlignment = Alignment.CenterStart) {
                                        if (draft.isEmpty()) Text(placeholder, color = DeskColors.faint,
                                            style = MaterialTheme.typography.bodyMedium)
                                        inner()
                                    }
                                },
                            )
                            IconButton(
                                onClick = {
                                    model.submit("chat", buildJsonObject { put("text", prefix + draft) }) {
                                        if (latestDraft == draft) changeDraft("")
                                        submitted()
                                    }
                                },
                                enabled = enabled,
                                modifier = Modifier.size(48.dp).semantics { contentDescription = "发送消息" },
                            ) {
                                val sendColors = if (enabled) listOf(Color(0xFF3B82F6), Color(0xFF2563EB))
                                    else listOf(Color(0xFF243856), Color(0xFF26354D))
                                Box(
                                    Modifier.size(38.dp).background(Brush.verticalGradient(sendColors), RoundedCornerShape(10.dp)),
                                    contentAlignment = Alignment.Center,
                                ) {
                                    DeskIcon(DeskGlyph.Send, if (enabled) Color.White else DeskColors.faint, Modifier.size(17.dp))
                                }
                            }
                        }
                        val connectionLabel = when {
                            followUp && reply?.text("status") == "failed" -> "原答复失败 · 可返回对话重新提问"
                            followUp && report == null -> "报告尚未载入 · 草稿已保留"
                            !state.canQueue -> "当前保留草稿，工作台同步后可提交"
                            !state.connection.localOnline -> "本地服务离线 · 提交后待发送"
                            !followUp -> "本地工作台已连接"
                            else -> null
                        }
                        if (connectionLabel != null) {
                            Text(connectionLabel, Modifier.padding(bottom = 3.dp), color = DeskColors.faint,
                                style = MaterialTheme.typography.labelSmall, textAlign = TextAlign.Center)
                        }
                    }
                }
            }
        }
    }
}

private enum class ChatOperation(val command: String, val label: String) {
    SCAN("scan_now", "立即扫描"),
    PAUSE("pause", "暂停分析"),
    PROFILE("set_profile", "切换档案"),
}

@Composable
private fun ChatCommands(state: WorkbenchProjection, model: WorkbenchViewModel, busy: Boolean, onSubmitted: () -> Unit) {
    var selected by rememberSaveable { mutableStateOf<ChatOperation?>(null) }
    var requested by rememberSaveable { mutableStateOf<ChatOperation?>(null) }
    var requestedAt by rememberSaveable { mutableLongStateOf(0L) }
    val submitted by rememberUpdatedState(onSubmitted)
    Row(
        Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
        horizontalArrangement = Arrangement.spacedBy(7.dp),
    ) {
        for (question in listOf("今日复盘", "持仓风险")) {
            ChatCommandChip(question, state.canQueue && !busy) {
                model.submit("chat", buildJsonObject { put("text", question) }) { submitted() }
            }
        }
        ChatOperation.entries.forEach { operation ->
            ChatCommandChip(operation.label, !busy) { selected = operation }
        }
    }
    if (requested != null) {
        state.intents.lastOrNull { it.command == requested?.command && it.at >= requestedAt }?.let { intent ->
            ChatMeta("${commandLabel(intent.command)} · ${intent.stage.label()}" +
                intent.errorCode?.let { " · $it" }.orEmpty(),
                if (intent.stage == DeliveryStage.REJECTED) DeskColors.red else DeskColors.secondary)
        }
    }
    selected?.let { operation ->
        ChatOperationDialog(operation, state, busy, dismiss = { selected = null }) { args ->
            val at = System.currentTimeMillis()
            model.submit(operation.command, args) {
                requested = operation
                requestedAt = at
                selected = null
            }
        }
    }
}

@Composable
private fun ChatCommandChip(label: String, enabled: Boolean, onClick: () -> Unit) {
    Surface(
        onClick = onClick, enabled = enabled, shape = CircleShape,
        color = DeskColors.blue.copy(alpha = .10f),
        contentColor = if (enabled) Color(0xFFBFDBFE) else DeskColors.faint,
        border = BorderStroke(1.dp, DeskColors.blue.copy(alpha = .25f)),
    ) {
        Box(Modifier.sizeIn(minWidth = 48.dp, minHeight = 48.dp).padding(horizontal = 12.dp, vertical = 4.dp),
            contentAlignment = Alignment.Center) {
            Text(label, style = MaterialTheme.typography.labelMedium)
        }
    }
}

@Composable
private fun ChatOperationDialog(
    operation: ChatOperation,
    state: WorkbenchProjection,
    busy: Boolean,
    dismiss: () -> Unit,
    submit: (JsonObject) -> Unit,
) {
    val analysis = state.meta?.obj("analysis")
    val paused = analysis?.flag("paused")
    val profile = analysis?.text("profile")
    val relevantCommands = when (operation) {
        ChatOperation.SCAN -> setOf("scan_now", "pause", "resume")
        ChatOperation.PAUSE -> setOf("pause", "resume")
        ChatOperation.PROFILE -> setOf("set_profile")
    }
    val blocker = when {
        !state.canQueue || !state.connection.localOnline -> "工作台在线并同步后可提交。"
        state.intents.any { it.command in relevantCommands && it.stage in pendingStages } -> "已有相关操作等待工作台确认。"
        operation != ChatOperation.PROFILE && paused == null -> "分析状态尚未同步。"
        operation == ChatOperation.SCAN && paused == true -> "分析已暂停，恢复后可扫描。"
        operation == ChatOperation.PAUSE && paused == true -> "分析已暂停。"
        else -> null
    }
    var nextProfile by rememberSaveable { mutableStateOf<String?>(null) }
    val enabled = blocker == null && !busy &&
        (operation != ChatOperation.PROFILE || (nextProfile != null && nextProfile != profile))
    AlertDialog(
        onDismissRequest = dismiss,
        title = { Text(operation.label) },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(when (operation) {
                    ChatOperation.SCAN -> "请求工作台按当前档案扫描全市场。结果以工作台回执为准。"
                    ChatOperation.PAUSE -> "暂停后停止新扫描，已有事项继续按规则提醒。"
                    ChatOperation.PROFILE -> "新的档案自下一轮扫描起生效。"
                })
                if (operation == ChatOperation.PROFILE) {
                    for ((key, label) in listOf("conservative" to "保守趋势", "aggressive" to "激进动量")) {
                        TextButton(onClick = { nextProfile = key }, enabled = blocker == null && !busy && key != profile,
                            modifier = Modifier.heightIn(min = 48.dp)) {
                            Text(when (key) {
                                profile -> "$label · 当前"
                                nextProfile -> "$label · 已选"
                                else -> label
                            })
                        }
                    }
                }
                blocker?.let { ChatMeta(it, DeskColors.amber) }
            }
        },
        dismissButton = { TextButton(onClick = dismiss, modifier = Modifier.heightIn(min = 48.dp)) { Text("取消") } },
        confirmButton = {
            TextButton(enabled = enabled, modifier = Modifier.heightIn(min = 48.dp), onClick = {
                submit(buildJsonObject { if (operation == ChatOperation.PROFILE) put("profile", checkNotNull(nextProfile)) })
            }) { Text("确认") }
        },
    )
}

@Composable
internal fun RichContentPage(
    state: WorkbenchProjection, target: String, model: WorkbenchViewModel, busy: Boolean, go: (String, String) -> Unit,
) {
    val items = rememberChatItems(state)
    val assistant = items.firstOrNull { it.requestId == target && !it.sentByUser }
    if (assistant == null) {
        NoticeStrip("本机暂无这条答复。", DeskColors.amber)
        val history = state.currentIntents("get_messages").lastOrNull()
        AttachmentAction("刷新对话历史", state.connection.localOnline && !state.requiresReview && !busy &&
            history?.stage !in pendingStages) { model.submit("get_messages") }
        AttachmentAction("返回对话") { go("", "") }
        return
    }
    val user = items.firstOrNull { it.requestId == target && it.sentByUser }
    ChatDay(user?.at ?: assistant.at)
    user?.let { ChatBubble(it, state, go) }
    ChatBubble(assistant, state, go)
    if (assistant.reply?.text("status") == "failed") {
        AttachmentAction("返回对话重新提问") { go("", "") }
        return
    }
    assistant.reply?.let { ReplyAttachments(state, target, it, model, busy, go) }
    ReportFollowUps(state, target, model, busy, go)
}

@Composable
internal fun ReportFollowUps(
    state: WorkbenchProjection, target: String, model: WorkbenchViewModel, busy: Boolean,
    go: (String, String) -> Unit,
) {
    for (item in rememberChatItems(state)) {
        if (item.reportTarget != target || item.requestId == target) continue
        key(item.requestId, item.sentByUser) {
            ChatBubble(item, state, go)
            item.reply?.let { ReplyAttachments(state, item.requestId, it, model, busy, go) }
        }
    }
}

@Composable
private fun ReplyAttachments(
    state: WorkbenchProjection, target: String, reply: JsonObject, model: WorkbenchViewModel, busy: Boolean,
    go: (String, String) -> Unit,
) {
    val attachments = reply.objects("attachments")
    val valuationIndices = attachments.indices.filter { index ->
        val attachment = attachments[index]
        attachment.text("type") == "valuation" && attachment.stringList("ids").isNotEmpty()
    }
    val pointIds = valuationIndices.flatMap { attachments[it].stringList("ids") }.distinct()
    for ((index, attachment) in attachments.withIndex()) {
        key(target, index) {
            when {
                index !in valuationIndices -> ReplyAttachment(state, attachment, model, busy, go)
                index == valuationIndices.first() -> ReplyAttachment(state, attachment, model, busy, go, pointIds)
                else -> Unit
            }
        }
    }
}

@Composable
private fun ReplyAttachment(
    state: WorkbenchProjection, ref: JsonObject, model: WorkbenchViewModel, busy: Boolean, go: (String, String) -> Unit,
    ids: List<String> = ref.stringList("ids"),
) {
    val modifier = Modifier.padding(start = 18.dp)
    val scanId = ref.text("scanId")
    val symbol = ref.text("symbol")
    val type = ref.text("type")
    if (type == "valuation" && ids.isNotEmpty()) {
        val exact = state.rows("valuationPoints").associateBy { it.text("pointId") }
        val points = ids.distinct().mapNotNull(exact::get)
        if (points.size == ids.distinct().size) {
            ValuationChart(points.sortedWith(compareBy({ it.text("tradeDate") }, { it.number("revision") })), modifier,
                "本答复引用的采样")
            return
        }
    }
    val title = when (type) {
        "valuation" -> "持仓市值曲线"
        "positions" -> "持仓明细"
        "tasks" -> "事项"
        "candidates" -> "候选证券"
        "chart" -> "行情图表"
        else -> "引用的数据"
    }
    val matchingScan = state.currentIntents("get_candidates").lastOrNull { scanId != null && it.data.text("scanId") == scanId }
    val candidatePage = matchingScan?.result ?: state.reads["get_candidates"]?.takeIf {
        scanId != null && it.obj("scan")?.text("scanId") == scanId
    }
    val positions = if (type == "positions") state.rows("positions").filter {
        it.text("symbol") == symbol || it.text("symbol") in ids
    } else emptyList()
    val tasks = if (type == "tasks") ids.distinct().mapNotNull(state::task) else emptyList()
    val hasResult = state.connection.localOnline && !state.requiresReview && !busy
    AttachmentCard(title, when (type) {
        "positions", "tasks", "chart" -> "当前记录"
        else -> "历史引用"
    }, modifier) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            when (type) {
                "positions" -> {
                    for (position in positions) {
                        val code = position.value("symbol")
                        SettingTitle("${position.value("name")} · $code")
                        ChatMeta("持仓 ${position.value("qty")} · 可用 ${position.value("availableQty")} · 成本 ${position.value("cost")}")
                        ChatMeta("持仓快照时间 ${evidenceTimeText(position.number("asOf"))}")
                        position.number("priceAsOf")?.let { ChatMeta("行情时间 ${evidenceTimeText(it)}") }
                        AttachmentAction("查看 $code 日 K") { go("chart", code) }
                    }
                    if (positions.isEmpty()) {
                        ChatMeta("当前持仓快照未找到该证券。可核对报告正文中的历史口径。")
                        AttachmentAction("刷新持仓快照", hasResult, model::refreshPositions)
                    }
                }
                "tasks" -> {
                    for (task in tasks) {
                        val code = task.value("symbol")
                        SettingTitle("${task.value("name")} · $code")
                        ChatMeta("${taskDirection(task.text("direction"))} · ${taskState(task)} · ${timeText(task.number("createdAt"))}")
                        AttachmentAction("查看 ${task.value("name")} 事项详情") { go("task", task.value("taskId")) }
                    }
                    if (tasks.size < ids.distinct().size || ids.isEmpty()) {
                        ChatMeta("部分事项暂未同步，可刷新后重新打开。")
                        AttachmentAction("刷新事项快照", hasResult, model::refreshPositions)
                    }
                }
                "chart" -> {
                    if (symbol != null) {
                        ChatMeta("$symbol · 打开后显示工作台可读取的行情及来源时间。")
                        AttachmentAction("查看 $symbol 日 K") { go("chart", symbol) }
                    } else ChatMeta("此历史引用缺少证券，无法打开图表。")
                }
                "candidates" -> {
                    val candidates = candidatePage?.objects("items").orEmpty().filter {
                        symbol == null || it.text("symbol") == symbol
                    }
                    for (candidate in candidates) {
                        val code = candidate.value("symbol")
                        SettingTitle("${candidate.value("name")} · $code")
                        ChatMeta("评分 ${candidate.value("score")} · 现价 ${candidate.value("lastPrice")}")
                        ChatMeta("本轮采样 ${timeText(candidate.number("dataAsOf"))}")
                        AttachmentAction("查看 $code 日 K") { go("chart", code) }
                    }
                    if (candidates.isEmpty()) {
                        ChatMeta(if (scanId == null) "此引用未保存扫描批次。" else "本机暂无对应批次的候选详情。")
                        matchingScan?.takeIf { it.stage == DeliveryStage.REJECTED }?.let { IntentText(it) }
                        if (scanId != null) AttachmentAction("读取引用批次", hasResult && matchingScan?.stage !in pendingStages) {
                            model.submit("get_candidates", buildJsonObject { put("scanId", scanId) })
                        }
                        if (symbol != null) AttachmentAction("查看 $symbol 日 K") { go("chart", symbol) }
                    }
                }
                "valuation" -> {
                    ChatMeta("本机缺少原答复引用的部分采样。")
                    AttachmentAction("刷新持仓快照", hasResult, model::refreshPositions)
                    AttachmentAction("查看当前估值") { go("valuation", "") }
                }
                else -> ChatMeta("此类历史引用当前无法读取。")
            }
        }
    }
}

@Composable
private fun AttachmentAction(label: String, enabled: Boolean = true, action: () -> Unit) {
    TextButton(onClick = action, enabled = enabled, modifier = Modifier.heightIn(min = 48.dp)) { Text(label) }
}

@Composable
private fun ChatMeta(text: String, tint: Color = DeskColors.faint) {
    Text(text, color = tint, style = MaterialTheme.typography.labelSmall)
}

private enum class DeskGlyph { Pause, Play, Scan, Bell, Clock, Phone, Pulse, Edit, Send, Qr, Moon, File, Shield }

@Composable
private fun DeskIcon(glyph: DeskGlyph, tint: Color, modifier: Modifier) {
    Canvas(modifier) {
        val unit = size.minDimension / 24f
        fun mark(x: Float, y: Float) = Offset(x * unit, y * unit)
        fun path(vararg coordinates: Int) = Path().apply {
            moveTo(coordinates[0] * unit, coordinates[1] * unit)
            for (index in 2 until coordinates.size step 2) lineTo(coordinates[index] * unit, coordinates[index + 1] * unit)
        }
        fun outline(vararg coordinates: Int) {
            drawPath(path(*coordinates), tint, style = Stroke(1.7f * unit, cap = StrokeCap.Round, join = StrokeJoin.Round))
        }
        when (glyph) {
            DeskGlyph.Pause -> {
                drawLine(tint, mark(8f, 5f), mark(8f, 19f), 3.5f * unit, StrokeCap.Round)
                drawLine(tint, mark(16f, 5f), mark(16f, 19f), 3.5f * unit, StrokeCap.Round)
            }
            DeskGlyph.Play -> outline(7, 4, 20, 12, 7, 20, 7, 4)
            DeskGlyph.Scan -> {
                outline(9, 4, 4, 4, 4, 9)
                outline(15, 4, 20, 4, 20, 9)
                outline(20, 15, 20, 20, 15, 20)
                outline(9, 20, 4, 20, 4, 15)
                outline(4, 12, 20, 12)
            }
            DeskGlyph.Bell -> {
                outline(5, 17, 7, 14, 7, 8, 9, 5, 15, 5, 17, 8, 17, 14, 19, 17, 5, 17)
                outline(10, 20, 14, 20)
            }
            DeskGlyph.Clock -> {
                drawCircle(tint, 8.5f * unit, style = Stroke(1.7f * unit))
                outline(12, 7, 12, 12, 16, 14)
            }
            DeskGlyph.Phone -> {
                drawRoundRect(tint, mark(6f, 3f), Size(12 * unit, 18 * unit),
                    androidx.compose.ui.geometry.CornerRadius(2 * unit), style = Stroke(1.7f * unit))
                outline(11, 18, 13, 18)
            }
            DeskGlyph.Pulse -> outline(2, 12, 7, 12, 10, 5, 14, 19, 17, 12, 22, 12)
            DeskGlyph.Edit -> outline(4, 20, 5, 15, 16, 4, 20, 8, 9, 19, 4, 20)
            DeskGlyph.Send -> {
                outline(3, 10, 21, 3, 14, 21, 11, 13, 3, 10)
                outline(11, 13, 21, 3)
            }
            DeskGlyph.Qr -> {
                outline(3, 3, 9, 3, 9, 9, 3, 9, 3, 3)
                outline(15, 3, 21, 3, 21, 9, 15, 9, 15, 3)
                outline(3, 15, 9, 15, 9, 21, 3, 21, 3, 15)
                outline(14, 14, 17, 14, 17, 18, 21, 18, 21, 21)
                outline(13, 18, 13, 21, 16, 21)
                outline(21, 13, 21, 14)
            }
            DeskGlyph.Moon -> drawPath(Path().apply {
                moveTo(20f * unit, 14f * unit)
                cubicTo(12f * unit, 17f * unit, 7f * unit, 10f * unit, 10f * unit, 4f * unit)
                cubicTo(0f * unit, 7f * unit, 3f * unit, 21f * unit, 12f * unit, 21f * unit)
                cubicTo(16f * unit, 21f * unit, 19f * unit, 18f * unit, 20f * unit, 14f * unit)
            }, tint, style = Stroke(1.7f * unit, cap = StrokeCap.Round, join = StrokeJoin.Round))
            DeskGlyph.File -> {
                outline(6, 3, 14, 3, 19, 8, 19, 21, 6, 21, 6, 3)
                outline(14, 3, 14, 8, 19, 8)
                outline(9, 12, 16, 12)
                outline(9, 16, 16, 16)
            }
            DeskGlyph.Shield -> {
                outline(12, 3, 5, 6, 5, 12, 7, 17, 12, 21, 17, 17, 19, 12, 19, 6, 12, 3)
                outline(9, 12, 11, 14, 15, 9)
            }
        }
    }
}
