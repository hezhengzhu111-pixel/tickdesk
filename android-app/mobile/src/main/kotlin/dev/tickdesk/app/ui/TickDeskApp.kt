package dev.tickdesk.app.ui

import android.os.Build
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.ScrollState
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.saveable.rememberSaveableStateHolder
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import dev.tickdesk.app.R
import dev.tickdesk.app.BuildConfig
import dev.tickdesk.app.VerifiedReminder
import dev.tickdesk.app.ReportDestination
import dev.tickdesk.core.*
import kotlinx.coroutines.launch
import kotlinx.serialization.json.*
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

@Composable
internal fun TickDeskApp(
    model: WorkbenchViewModel = viewModel(), openTask: ReminderDestination? = null, onTaskOpened: () -> Unit = {},
    openReport: ReportDestination? = null, onReportOpened: () -> Unit = {},
) {
    val state by model.state.collectAsStateWithLifecycle()
    val alert by model.alert.collectAsStateWithLifecycle()
    TickDeskScreen(model, state, alert = alert, openTask = openTask, onTaskOpened = onTaskOpened,
        openReport = openReport, onReportOpened = onReportOpened)
}

@Composable
internal fun TickDeskScreen(
    model: WorkbenchViewModel,
    state: WorkbenchProjection,
    initialTab: Tab = Tab.INBOX,
    initialPage: String = "",
    initialTarget: String = "",
    alert: VerifiedReminder? = null,
    openTask: ReminderDestination? = null,
    onTaskOpened: () -> Unit = {},
    bodyScrollState: ScrollState? = null,
    openReport: ReportDestination? = null,
    onReportOpened: () -> Unit = {},
) {
    val error by model.error.collectAsStateWithLifecycle()
    val pairing by model.pairing.collectAsStateWithLifecycle()
    val busy by model.busy.collectAsStateWithLifecycle()
    var tab by rememberSaveable { mutableStateOf(initialTab) }
    var page by rememberSaveable { mutableStateOf(initialPage) }
    var target by rememberSaveable { mutableStateOf(initialTarget) }
    var pageHistory by rememberSaveable { mutableStateOf(emptyList<String>()) }
    var inboxFilter by rememberSaveable(state.pairId, state.epoch) { mutableStateOf("all") }
    val savedScreens = rememberSaveableStateHolder()
    val importDrafts = rememberImportDrafts(state)
    val symbolSearch = rememberSymbolSearchState(state.pairId, state.epoch)
    val selectTab: (Tab) -> Unit = { next ->
        tab = next
        page = ""
        target = ""
        pageHistory = emptyList()
    }
    val go: (String, String) -> Unit = { next, key ->
        if (page != next || target != key) {
            pageHistory = if (next.isBlank()) emptyList() else pageHistory + "$page:$target"
            page = next
            target = key
        }
    }
    LaunchedEffect(openTask) {
        if (openTask != null) {
            if (state.pairId == openTask.pair && state.epoch == openTask.epoch &&
                !state.connection.recoveryRequired && System.currentTimeMillis() < openTask.displayUntil) {
                selectTab(Tab.INBOX)
                go("task", openTask.taskId)
            }
            onTaskOpened()
        }
    }
    LaunchedEffect(openReport) {
        if (openReport != null) {
            if (state.pairId == openReport.pair && state.epoch == openReport.epoch &&
                state.connection.canWrite && System.currentTimeMillis() < openReport.displayUntil) {
                selectTab(Tab.CHAT)
                go("report", openReport.reportId)
            }
            onReportOpened()
        }
    }
    val back: () -> Unit = {
        val origin = pageHistory.lastOrNull()
        if (origin != null) {
            pageHistory = pageHistory.dropLast(1)
            page = origin.substringBefore(':')
            target = origin.substringAfter(':')
        } else if (page == "execution" && target.isNotEmpty()) {
            page = "task"
        } else if (page == "adjust_import") {
            page = "import"
            target = ""
        } else {
            page = ""
            target = ""
        }
    }
    val importPairing = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) {
        if (it != null) model.importPairing(it)
    }
    val imageError by model.holdingImageError.collectAsStateWithLifecycle()
    var imagePair by rememberSaveable { mutableStateOf<String?>(null) }
    var imageEpoch by rememberSaveable { mutableStateOf<String?>(null) }
    var imageNotice by rememberSaveable { mutableStateOf<String?>(null) }
    val holdingPicker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        val capturedPair = imagePair
        val capturedEpoch = imageEpoch
        val capturedNotice = imageNotice
        imagePair = null; imageEpoch = null; imageNotice = null
        if (uri != null && capturedPair != null && capturedEpoch != null && capturedNotice != null)
            model.importHoldingImage(uri, FileTransferScope(capturedPair, capturedEpoch), capturedNotice)
    }
    val pickHolding: () -> Unit = {
        val scope = state.fileTransferScope()
        val notice = state.meta?.obj("visionNotice")?.text("noticeVersion")
        if (scope != null && notice != null && !busy && state.holdingImport?.terminal != false && imagePair == null) {
            imagePair = scope.pairId; imageEpoch = scope.epoch; imageNotice = notice
            holdingPicker.launch(arrayOf("image/png", "image/jpeg"))
        }
    }
    LaunchedEffect(state.holdingImport?.id, state.holdingImport?.status) {
        if (state.holdingImport?.status == HoldingImportStatus.SUCCESS && tab == Tab.DISCOVER && page.isEmpty())
            selectTab(Tab.PORTFOLIO)
    }
    val scan = rememberLauncherForActivityResult(ScanContract()) {
        it.contents?.let(model::scanPairing)
    }
    BackHandler(page.isNotEmpty(), back)
    val route = "${state.pairId}:${state.epoch}:$tab:$page:$target"
    val footer = remember(route) { ScreenFooter() }
    val settings: () -> Unit = { selectTab(Tab.SETTINGS) }
    Scaffold(
        modifier = Modifier.imePadding(),
        containerColor = MaterialTheme.colorScheme.background,
        topBar = {
            ConnectionStrip(state, settings, atSettings = tab == Tab.SETTINGS && page.isEmpty())
        },
        bottomBar = {
            val now by deskTime()
            val activeTasks = state.rows("tasks").filter { it.activeTask(now) }
            val holdingCount = state.rows("positions").count { (it.number("qty") ?: 0L) > 0 }
            DesignTabBar(tab, activeTasks.size, activeTasks.any { it.text("level") == "critical" }, holdingCount) { item ->
                if (tab != item || page.isNotEmpty()) selectTab(item)
            }
        },
    ) { insets ->
        savedScreens.SaveableStateProvider(route) {
            val scroll = bodyScrollState ?: rememberScrollState()
            val hasComposer = (tab == Tab.CHAT && page.isEmpty()) || page in setOf("analyze", "rich", "report")
            val reportTarget = target.takeIf { page in setOf("rich", "report") }
            var chatDraft by rememberSaveable {
                mutableStateOf(if (page == "analyze" && symbolPattern.matches(target)) "/分析 $target" else "")
            }
            var sentChatCount by remember { mutableIntStateOf(0) }
            if (hasComposer) ChatAutoScroll(state, reportTarget, scroll, sentChatCount)
            val jumpThreshold = with(LocalDensity.current) { 300.dp.roundToPx() }
            val showTop by remember(scroll, jumpThreshold) { derivedStateOf { scroll.value > jumpThreshold } }
            val coroutine = rememberCoroutineScope()
            Column(Modifier.fillMaxSize().padding(insets).consumeWindowInsets(insets).ultraPageBackground()) {
                if (BuildConfig.MOCK_DEMO) {
                    Surface(color = DeskColors.amber.copy(alpha = 0.12f)) {
                        Text("联调演示 · 全部数据为模拟数据", Modifier.fillMaxWidth().padding(8.dp),
                            color = DeskColors.amber, textAlign = TextAlign.Center,
                            style = MaterialTheme.typography.labelMedium)
                    }
                }
                BoxWithConstraints(Modifier.fillMaxWidth().weight(1f)) {
                    // In a short window (such as above the IME), keep the full page reachable by scrolling.
                    val pinHeader = maxHeight >= 180.dp
                    val scrolled by remember(scroll) { derivedStateOf { scroll.value > 0 } }
                    val header: @Composable () -> Unit = {
                        ScreenHeading(state, tab, page, target, inboxFilter, go, back, model::retry, pickHolding)
                    }
                    Column(Modifier.fillMaxSize()) {
                        if (pinHeader) {
                            Surface(color = if (scrolled) DeskColors.background else Color.Transparent) {
                                Column {
                                    Box(Modifier.fillMaxWidth().padding(start = 16.dp, end = 16.dp, top = 15.dp, bottom = 9.dp)) {
                                        header()
                                    }
                                    HorizontalDivider(color = if (scrolled) DeskColors.border else Color.Transparent)
                                }
                            }
                        }
                        if (page == "reply_text") {
                            if (!pinHeader) Box(Modifier.fillMaxWidth().padding(start = 16.dp, end = 16.dp, top = 15.dp)) { header() }
                            ReplyTextPage(state, target, model, Modifier.fillMaxWidth().weight(1f), go)
                        } else Column(
                            Modifier.fillMaxWidth().weight(1f).verticalScroll(scroll)
                                .padding(start = 16.dp, end = 16.dp, top = if (pinHeader) 2.dp else 15.dp,
                                    bottom = if (hasComposer) 18.dp else 64.dp),
                            verticalArrangement = Arrangement.spacedBy(12.dp),
                        ) {
                            if (!pinHeader) header()
                            if (page.isEmpty() && tab in setOf(Tab.PORTFOLIO, Tab.DISCOVER)) {
                                HoldingImportPanel(
                                    state, busy, imageError, model::retryHoldingImage, manage = { go("import", "") },
                                    cancelImport = { id -> model.submit("cancel_import", buildJsonObject { put("importId", id) }) },
                                )
                            }
                            when (page) {
                                "task" -> TaskDetails(state, target, model, busy, go, footer)
                                "execution" -> ExecutionForm(state, target, model, busy, footer,
                                    onPortfolio = { selectTab(Tab.PORTFOLIO) })
                                "execution_manual" -> ExecutionForm(state, "", model, busy, footer, prefill = target,
                                    fromCandidate = tab == Tab.DISCOVER && target.isNotBlank(), onPortfolio = { selectTab(Tab.PORTFOLIO) })
                                "adjust", "adjust_import", "adjust_reconcile" -> DesignedAdjustPositionForm(
                                    state, target, model, busy, back = { selectTab(Tab.PORTFOLIO) },
                                    reconcile = page == "adjust_reconcile",
                                )
                                "holding" -> HoldingDetailsPage(state, target, model, busy,
                                    back = { selectTab(Tab.PORTFOLIO) },
                                    openProgress = { id -> go("execution_progress", id) })
                                "execution_progress" -> ExecutionProgressPage(state, target, model, busy,
                                    back = { go("task", target) })
                                "trade_review" -> TradeReviewPage(state, model, busy)
                                "analysis" -> AnalysisPage(state, model, busy)
                                "recovery" -> RecoveryPage(state, model)
                                "reminders" -> ReminderSettingsPage(state, model, busy)
                                "background_reminders" -> BackgroundReminderCard(state, model)
                                "analysis_profile" -> ScanProfileSettingsPage(state, model, busy)
                                "services" -> ServiceSettingsPage(state, model, busy)
                                "operation_queue" -> OperationQueuePage(state, model, busy)
                                "workbench" -> WorkstationSettingsPage(state, model, busy,
                                    scan = { scan.launch(ScanOptions().setDesiredBarcodeFormats(ScanOptions.QR_CODE)
                                        .setPrompt("扫描管理员提供的 TickDesk 配对二维码")
                                        .setBeepEnabled(false).setBarcodeImageEnabled(false).setOrientationLocked(false)) },
                                    importConfig = { importPairing.launch(arrayOf("application/json", "text/plain", "application/octet-stream")) },
                                )
                                "analyze" -> ChatPage(state, model, busy, go)
                                "rich" -> RichContentPage(state, target, model, busy, go)
                                "reports" -> ReportsPage(state, model, busy, go)
                                "report" -> ReportDetailsPage(state, target, model, busy, go)
                                "valuation" -> ValuationPage(state)
                                "chart" -> ChartPage(state, target, model, busy)
                                "symbols" -> SymbolSearchPage(state, symbolSearch, model, busy, go)
                                "import" -> ImportPage(state, model, busy, footer, back, manual = { go("adjust_import", "") },
                                    portfolio = { selectTab(Tab.PORTFOLIO) }, focusOcr = target == ImportOcrTarget, drafts = importDrafts)
                                else -> when (tab) {
                                    Tab.INBOX -> InboxPage(state, inboxFilter, { inboxFilter = it }, go)
                                    Tab.CHAT -> ChatPage(state, model, busy, go)
                                    Tab.DISCOVER -> DesignedDiscoverPage(state, model, busy, go)
                                    Tab.PORTFOLIO -> DesignedPortfolioPage(state, go, pickHolding)
                                    Tab.SETTINGS -> SettingsPage(state, model, busy, go)
                                }
                            }
                        }
                    }
                    if (!hasComposer && tab != Tab.CHAT && showTop) {
                        Surface(onClick = { coroutine.launch { scroll.scrollTo(0) } },
                            modifier = Modifier.align(Alignment.BottomEnd).padding(end = 15.dp, bottom = 10.dp),
                            shape = RoundedCornerShape(13.dp), color = DeskColors.navy, border = BorderStroke(1.dp, DeskColors.borderStrong)) {
                            Box(Modifier.size(48.dp), contentAlignment = Alignment.Center) {
                                Icon(painterResource(R.drawable.ic_ultra_top), "返回顶部", Modifier.size(17.dp), DeskColors.secondary)
                            }
                        }
                    }
                }
                error?.let { message ->
                    Surface(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp)
                        .semantics { liveRegion = LiveRegionMode.Polite }, color = Color(0xFF281822),
                        shape = RoundedCornerShape(13.dp), border = BorderStroke(1.dp, DeskColors.red.copy(alpha = .25f))) {
                        Row(Modifier.padding(start = 12.dp, end = 4.dp), verticalAlignment = Alignment.CenterVertically) {
                            Text(message, Modifier.weight(1f).padding(vertical = 8.dp), style = MaterialTheme.typography.bodySmall,
                                color = DeskColors.secondary)
                            TextButton(onClick = model::clearError, modifier = Modifier.heightIn(min = 48.dp)) { Text("关闭提示") }
                        }
                    }
                }
                if (hasComposer) {
                    ChatComposer(state, model, busy, chatDraft, reportTarget = reportTarget,
                        loadedReport = reportTarget?.takeIf { page == "report" }?.let(state::reportDetails),
                        onSubmitted = { sentChatCount++ }) { chatDraft = it }
                } else {
                    footer.content?.let { ActionDock(it) }
                }
            }
        }
    }
    pairing?.let { config ->
        var verified by remember(config) { mutableStateOf(false) }
        AlertDialog(onDismissRequest = model::dismissPairing, title = { Text("核对配对配置") },
            text = {
                Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    Text("工作台 ${config.pairId}")
                    Text(config.relayUrl)
                    Text("证书公钥 SHA-256")
                    config.pins.forEach { Text(it, style = MaterialTheme.typography.bodyMedium) }
                    Text("请与管理员通过可信渠道提供的完整指纹核对。")
                    CheckRow("我已核对工作台标识和完整指纹", verified) { verified = it }
                }
            },
            confirmButton = { TextButton(onClick = model::confirmPairing, enabled = verified && !busy) { Text("保存并连接") } },
            dismissButton = { TextButton(onClick = model::dismissPairing) { Text("取消") } })
    }
    if (alert != null && state.connection.canWrite) {
        val id = alert.task.text("taskId")!!
        val currentTask = state.task(id)
        val alertTask = currentTask?.takeIf { (it.number("version") ?: 0L) >= (alert.task.number("version") ?: 0L) } ?: alert.task
        ForegroundReminder(
            alert = alert.copy(task = alertTask), position = state.rows("positions").firstOrNull { it.text("symbol") == alertTask.text("symbol") },
            dismiss = model::dismissAlert,
            details = { model.dismissAlert(); selectTab(Tab.INBOX); go("task", id) },
        )
    }
}

@Composable
private fun ScreenHeading(
    state: WorkbenchProjection,
    tab: Tab,
    page: String,
    target: String,
    inboxFilter: String,
    go: (String, String) -> Unit,
    back: () -> Unit,
    refresh: () -> Unit,
    pickHolding: () -> Unit,
) {
    when {
        page.isEmpty() -> when (tab) {
            Tab.INBOX -> InboxHeading(state, inboxFilter)
            Tab.DISCOVER -> DiscoverHeading(state, go, pickHolding)
            Tab.PORTFOLIO -> PortfolioHeading(go, pickHolding)
            Tab.SETTINGS -> PageHeading(stringResource(tab.label))
            Tab.CHAT -> {
                val online = state.connection.synced && state.meta?.obj("analysis")?.text("kernelStatus") == "online"
                val status = when {
                    online -> null
                    state.connection.synced -> "已同步"
                    state.synchronizedAt != null -> "本机缓存"
                    else -> "未同步"
                }
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Box(Modifier.weight(1f)) {
                        PageHeading(stringResource(tab.label), badge = status, live = if (online) "引擎在线" else null)
                    }
                    TextButton(onClick = { go("reports", "") }, modifier = Modifier.heightIn(min = 48.dp)) {
                        val publication = state.reads["analysis_report"]
                        val now by deskTime()
                        val fresh = publication?.number("notifyUntil")?.let { it > now } == true
                        Text(if (fresh) "分析报告 · 有更新" else "分析报告")
                    }
                }
            }
        }
        page == "rich" -> Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            Box(Modifier.weight(1f)) { PageHeading("对话", "结构化投研卡片 · 本地工作台数据") }
            Surface(onClick = back, shape = RoundedCornerShape(12.dp), color = DeskColors.navy,
                border = BorderStroke(1.dp, DeskColors.border)) {
                Box(Modifier.size(48.dp), contentAlignment = Alignment.Center) {
                    Icon(painterResource(R.drawable.ic_chat), "返回普通对话", Modifier.size(18.dp), DeskColors.secondary)
                }
            }
        }
        else -> Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp),
                verticalAlignment = Alignment.CenterVertically) {
                SubpageBack(when (page) {
                    "task" -> "事项"
                    "import" -> when (tab) { Tab.DISCOVER -> "选股"; Tab.SETTINGS -> "设置"; else -> "持仓" }
                    "analyze" -> "持仓"
                    "analysis", "recovery", "reminders", "background_reminders", "analysis_profile", "services", "operation_queue", "workbench" -> "设置"
                    else -> "返回"
                }, back)
                when (page) {
                    "task" -> state.task(target)?.let { task ->
                        val at by deskTime()
                        val tint = if (task.activeTask(at) && (task.number("snoozeUntil") ?: 0L) <= at)
                            severityColor(task.text("level")) else DeskColors.muted
                        Box(Modifier.weight(1f), contentAlignment = Alignment.Center) {
                            StatusBadge("${task.value("level").uppercase()} · ${taskState(task, at)}", tint)
                        }
                        IconButton(onClick = refresh) {
                            Text("↻", Modifier.semantics { contentDescription = "刷新事项" },
                                style = MaterialTheme.typography.titleLarge, color = DeskColors.blue)
                        }
                    }
                    "execution", "execution_manual" -> Box(Modifier.weight(1f), contentAlignment = Alignment.CenterEnd) {
                        StatusBadge("成交台账录入")
                    }
                    "import" -> Box(Modifier.weight(1f), contentAlignment = Alignment.CenterEnd) {
                        StatusBadge("全量持仓快照")
                    }
                    "analysis" -> Box(Modifier.weight(1f), contentAlignment = Alignment.CenterEnd) {
                        when (state.meta?.obj("analysis")?.flag("paused")) {
                            true -> StatusBadge("引擎已挂起", DeskColors.amber)
                            false -> StatusBadge("分析运行中", DeskColors.green)
                            null -> StatusBadge("状态未同步")
                        }
                    }
                }
            }
            pageTitle(page)?.let { PageHeading(it) }
        }
    }
}

private fun pageTitle(page: String) = when (page) {
    "adjust", "adjust_import" -> "手动维护持仓"
    "adjust_reconcile" -> "核对实际成交与持仓"
    "holding" -> "持仓分区与可卖依据"
    "execution_progress" -> "申报执行进度"
    "trade_review" -> "实际成交复盘"
    "reminders" -> "提醒设置"
    "background_reminders" -> "后台提醒"
    "analysis_profile" -> "扫描档案"
    "services" -> "工作台服务状态"
    "operation_queue" -> "待处理操作"
    "workbench" -> "配对与连接"
    "analyze" -> "量化与 AI"
    "recovery" -> "恢复对账"
    "valuation" -> "持仓估值与行情覆盖"
    "reports" -> "分析报告"
    "report" -> "报告详情"
    "reply_text" -> "全文"
    "chart" -> "证券行情"
    "symbols" -> "搜索与关注"
    else -> null
}

@Composable
private fun ConnectionStrip(state: WorkbenchProjection, settings: () -> Unit, atSettings: Boolean) {
    val connection = state.connection
    val largeText = LocalDensity.current.fontScale >= 1.5f
    val status = when {
        state.connectionError == "CREDENTIAL_REVOKED" -> "凭据已失效，请重新导入"
        state.connectionError == "TLS_VALIDATION" -> "证书核验失败，请核对可信配置"
        state.connectionError == "SESSION_REPLACED" -> "连接已被另一实例替换"
        state.connectionError == "PAIRING_KEY_UNAVAILABLE" -> "无法解密配对凭据，请重新导入"
        state.connectionError in setOf("PAIR_MISMATCH", "PROTOCOL_MISMATCH", "INVALID_RELAY_FRAME") -> "工作台身份或协议核验失败"
        !connection.paired -> "尚未配对"
        connection.recoveryRequired -> "恢复待对账 · 只读"
        !connection.relayOnline -> "工作台未连接"
        !connection.localOnline -> "中继在线 · 本地服务离线"
        state.syncError != null -> "同步尚未完成 · ${state.syncError}"
        !connection.synced -> "正在同步"
        else -> "本地服务已连接"
    }
    val pending = state.intents.count { it.stage in pendingStages }
    val healthy = connection.synced && !connection.recoveryRequired && state.syncError == null
    val statusColor = when {
        healthy -> DeskColors.green
        state.connectionError != null -> DeskColors.red
        else -> DeskColors.amber
    }
    val device = when (Build.MODEL) {
        "25128PNA1C", "Xiaomi 17 Ultra" -> "小米 17 Ultra"
        else -> "当前设备"
    }
    Surface(Modifier.fillMaxWidth().background(DeskColors.background).statusBarsPadding(), color = Color.Transparent) {
        Column {
            Surface(onClick = settings, enabled = !atSettings, color = Color.Transparent) {
                Row(Modifier.fillMaxWidth().background(Brush.horizontalGradient(listOf(Color(0xFF101827), Color(0xFF0B101B))))
                    .heightIn(min = 48.dp).padding(horizontal = 16.dp, vertical = 6.dp),
                    verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    Column(Modifier.weight(1f)) {
                        Row(horizontalArrangement = Arrangement.spacedBy(7.dp), verticalAlignment = Alignment.CenterVertically) {
                            Box(Modifier.size(6.dp).background(statusColor, CircleShape))
                            val name = state.pairId?.takeIf { !atSettings && healthy && !largeText }?.let { "$device ⇄ $it" } ?: status
                            Text(name, style = MaterialTheme.typography.labelMedium, color = Color(0xFFE2ECFF),
                                fontWeight = FontWeight.SemiBold)
                        }
                        val last = state.synchronizedAt?.let { syncTime.format(Instant.ofEpochMilli(it)) }
                        val syncLabel = when {
                            last == null -> "尚未同步"
                            largeText -> last
                            else -> "同步于 $last"
                        }
                        val queueLabel = if (largeText) "待发 $pending" else "待发送 $pending 条"
                        Text(if (atSettings && pending == 0) syncLabel else "$syncLabel · $queueLabel",
                            style = MaterialTheme.typography.labelSmall, color = DeskColors.faint, fontFamily = deskMono)
                    }
                    if (!atSettings) Icon(painterResource(R.drawable.ic_ultra_chevron), null, Modifier.size(15.dp), DeskColors.faint)
                }
            }
            HorizontalDivider(color = DeskColors.border)
        }
    }
}

@Composable
private fun SubpageBack(label: String, onClick: () -> Unit) {
    Surface(onClick = onClick, shape = RoundedCornerShape(11.dp), color = Color.White.copy(alpha = 0.04f),
        border = BorderStroke(1.dp, Color.White.copy(alpha = 0.065f)), contentColor = DeskColors.secondary) {
        Row(Modifier.heightIn(min = 48.dp).padding(horizontal = 10.dp), verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(4.dp)) {
            Icon(painterResource(R.drawable.ic_ultra_back), null, Modifier.size(16.dp))
            Text(label, style = MaterialTheme.typography.labelMedium)
        }
    }
}

@Composable
internal fun Panel(title: String, content: @Composable ColumnScope.() -> Unit) {
    Card(shape = RoundedCornerShape(18.dp), colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)) {
        Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(title, style = MaterialTheme.typography.titleMedium, modifier = Modifier.semantics { heading() })
            content()
        }
    }
}

@Composable
internal fun MainAction(label: String, enabled: Boolean = true, onClick: () -> Unit) =
    MainAction(label, enabled, leadingIcon = null, onClick = onClick)

@Composable
internal fun MainAction(label: String, enabled: Boolean, leadingIcon: Int?, onClick: () -> Unit) {
    val shape = RoundedCornerShape(13.dp)
    val fill = if (enabled) listOf(Color(0xFF3B82F6), Color(0xFF2563EB)) else listOf(DeskColors.navy, DeskColors.panel)
    Button(onClick, Modifier.fillMaxWidth().heightIn(min = 48.dp).clip(shape).background(Brush.verticalGradient(fill)), enabled = enabled,
        shape = shape, border = BorderStroke(1.dp, if (enabled) Color(0xFF6BA1FB) else DeskColors.border),
        colors = ButtonDefaults.buttonColors(containerColor = Color.Transparent, contentColor = Color.White,
            disabledContainerColor = Color.Transparent, disabledContentColor = DeskColors.muted)) {
        if (leadingIcon != null) {
            Icon(painterResource(leadingIcon), null, Modifier.size(16.dp))
            Spacer(Modifier.width(7.dp))
        }
        Text(label, textAlign = TextAlign.Center)
    }
}

@Composable
internal fun CheckRow(label: String, checked: Boolean, change: (Boolean) -> Unit) {
    Row(Modifier.fillMaxWidth().heightIn(min = 48.dp)
        .toggleable(value = checked, role = Role.Checkbox, onValueChange = change)
        .padding(horizontal = 4.dp), verticalAlignment = androidx.compose.ui.Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        Checkbox(checked, onCheckedChange = null)
        Text(label, modifier = Modifier.weight(1f))
    }
}

internal fun WorkbenchProjection.rows(collection: String) = records[collection].orEmpty()
internal fun WorkbenchProjection.task(id: String) = rows("tasks").firstOrNull { it.text("taskId") == id }
internal fun JsonObject.value(key: String) = text(key) ?: number(key)?.toString() ?: "未知"
internal fun JsonObject.stringList(key: String) = array(key)?.mapNotNull { (it as? JsonPrimitive)?.content }.orEmpty()
internal fun JsonObject.objects(key: String) = array(key)?.map { it.jsonObject }.orEmpty()
internal fun WorkbenchProjection.currentIntents(command: String): List<IntentView> {
    val currentEpoch = epoch ?: return emptyList()
    return intents.filter { it.command == command && it.data.text("serverEpoch") == currentEpoch }
}
internal val shanghai: ZoneId = ZoneId.of("Asia/Shanghai")
private val timeFormatter = DateTimeFormatter.ofPattern("MM-dd HH:mm").withZone(shanghai)
private val syncTime = DateTimeFormatter.ofPattern("HH:mm:ss").withZone(shanghai)
internal fun timeText(at: Long?): String = at?.let { timeFormatter.format(Instant.ofEpochMilli(it)) } ?: "未知"
internal fun JsonObject.activeTask(at: Long = System.currentTimeMillis()) =
    text("status") in setOf("pending", "reminded") && (number("validUntil") ?: 0) > at

internal val pendingStages = setOf(DeliveryStage.PENDING, DeliveryStage.SENT, DeliveryStage.DELIVERED,
    DeliveryStage.WAITING_FOR_SNAPSHOT, DeliveryStage.ACCEPTED)

internal fun DeliveryStage.label() = when (this) {
    DeliveryStage.PENDING -> "待发送"
    DeliveryStage.SENT -> "已尝试发送 · 等待确认"
    DeliveryStage.DELIVERED -> "对端已保存 · 业务待确认"
    DeliveryStage.ACCEPTED -> "已受理 · 等待结果"
    DeliveryStage.WAITING_FOR_SNAPSHOT -> "已收到业务回执 · 等待状态同步"
    DeliveryStage.APPLIED -> "已确认"
    DeliveryStage.REJECTED -> "服务端拒绝或任务失败"
    DeliveryStage.NEEDS_RECONCILIATION -> "成交已记录 · 持仓待对账"
    DeliveryStage.QUARANTINED -> "已隔离 · 待核对"
    DeliveryStage.CANCELLED -> "已取消"
}

internal fun commandLabel(command: String) = when (command) {
    "act", "record_execution" -> "成交记录"
    "ignore" -> "忽略事项"
    "snooze" -> "稍后提醒"
    "chat" -> "发送消息"
    "scan_now" -> "扫描"
    "pause" -> "暂停分析"
    "resume" -> "恢复分析"
    "set_profile" -> "调整扫描档案"
    "set_settings" -> "更新提醒设置"
    "adjust_position" -> "更正持仓"
    "accept_vision_notice" -> "更新识别同意"
    "delete_file" -> "删除原图"
    "apply_import" -> "导入差异"
    else -> command
}

