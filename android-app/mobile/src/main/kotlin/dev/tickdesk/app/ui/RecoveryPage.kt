package dev.tickdesk.app.ui

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import dev.tickdesk.core.*
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.launch
import kotlinx.serialization.json.*
import java.util.UUID

private const val ARCHIVE = "archive_operation"

@Composable
internal fun RecoveryPage(state: WorkbenchProjection, model: WorkbenchViewModel) {
    val scope = rememberCoroutineScope()
    val workflow = remember(state.pairId, state.epoch, model) { RecoveryWorkflow(model) }
    val ready = state.connection.synced && state.connection.localOnline && state.serviceRecoveryActive
    val service = workflow.service
    val current = ready && service != null && (state.sequence ?: -1) >= service.sequence
    val manifest = if (service != null) workflow.manifest(service) else null
    val modelBusy by model.busy.collectAsState()
    val idle = !workflow.busy && !modelBusy
    var sealPrompt by remember(workflow) { mutableStateOf(false) }
    var confirmPrompt by remember(workflow) { mutableStateOf(false) }
    var selectedItem by remember(workflow) { mutableStateOf<JsonObject?>(null) }
    var selectedDecision by remember(workflow) { mutableStateOf<Pair<JsonObject, String>?>(null) }

    LaunchedEffect(workflow, ready) {
        if (ready && workflow.service == null && !workflow.busy && !workflow.confirmed) {
            workflow.run { refresh() }
        }
    }

    PageHeading("恢复对账", "核对手机保存的旧记录与当前工作台快照", "只读")
    if (workflow.confirmed || (state.connection.synced && !state.serviceRecoveryActive)) {
        NoticeStrip(
            if (state.serviceRecoveryActive || !state.connection.synced) "服务已确认恢复。正在同步正常模式的完整快照。"
            else if (state.requiresReview) "服务已退出恢复。请回到设置核对隔离队列，再结束本机只读状态。"
            else "当前工作台已同步到正常模式。",
            DeskColors.amber,
        )
        return
    }
    if (!ready) NoticeStrip(
        if (state.connectionError == "INVALID_RELAY_FRAME") "无法读取恢复状态。请确认 App 与本地服务使用兼容版本。"
        else "等待本地服务在线并同步当前工作台的完整快照。",
        DeskColors.amber,
    )
    if (workflow.busy) LinearProgressIndicator(Modifier.fillMaxWidth())
    workflow.message?.let { NoticeStrip(it, if (workflow.failed) DeskColors.amber else DeskColors.blue) }

    DesignCard {
        SectionTitle("证据和差异", service?.let { "核对版本 ${it.sequence}" })
        if (service == null) Text("等待读取当前恢复会话。", color = DeskColors.muted)
        else {
            Text("已验证 ${service.verified} · 接收中 ${service.receiving} · 无效 ${service.invalid}")
            Text("未关闭差异 ${service.open.size} · 证据${if (service.sealed) "已封存" else "待封存"}")
        }
        workflow.frozen?.let { Text("手机证据已冻结 · ${it.size} 条", color = DeskColors.green) }
        OutlinedButton(
            onClick = { scope.launch { workflow.run { refresh() } } },
            enabled = idle && ready,
            modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp),
        ) { Text("刷新恢复状态") }
        if (workflow.frozen == null || manifest == null) {
            Button(
                onClick = { scope.launch { workflow.run { collectAndUpload() } } },
                enabled = idle && current,
                modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp),
            ) { Text("收集、上传并冻结手机证据") }
        }
        if (service != null && !current && ready) Text("正在同步此核对版本的工作台快照。", color = DeskColors.muted)
    }

    if (service != null) {
        for (item in service.items) {
            key(item.text("itemId")) {
                RecoveryItemCard(
                    item, state, workflow.evidenceFor(item), idle && current && manifest != null,
                    select = { selectedItem = item },
                    decide = { disposition -> selectedDecision = item to disposition },
                )
            }
        }
        DesignCard {
            SectionTitle("完成恢复")
            Text("需要全部差异关闭、证据验证完整并封存。成交、持仓和原图复核可能需要在本地服务处理。", color = DeskColors.muted)
            if (!service.sealed) {
                OutlinedButton(
                    onClick = { sealPrompt = true },
                    enabled = idle && current && manifest != null && service.open.isEmpty(),
                    modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp),
                ) { Text("封存证据清单") }
            }
            Button(
                onClick = { confirmPrompt = true },
                enabled = idle && current && manifest != null && service.sealed && service.open.isEmpty(),
                modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp),
            ) { Text("确认恢复") }
        }
    }

    selectedItem?.let { item ->
        ArchivePrompt(item, idle && current && manifest != null, { selectedItem = null }) { note ->
            selectedItem = null
            scope.launch { workflow.run { archive(item, note) } }
        }
    }
    selectedDecision?.let { (item, disposition) ->
        RecoveryDecisionPrompt(
            item, disposition, state, idle && current && manifest != null,
            model = model,
            reconciliationId = workflow.service?.id,
            dismiss = { selectedDecision = null },
            submit = { batch, note ->
                scope.launch {
                    val accepted = workflow.run {
                        when (disposition) {
                            "record" -> record(item, batch.record!!, batch.included, note)
                            "included" -> included(item, batch.included!!, note)
                            "discard_intent" -> discard(item, note)
                            else -> fail("不支持的恢复决定")
                        }
                    }
                    if (accepted) selectedDecision = null
                }
            },
        )
    }
    if (sealPrompt) Confirm(
        "封存已验证证据", "将封存本次核对版本的完整证据清单。", !idle || !current,
        { sealPrompt = false },
    ) {
        sealPrompt = false
        scope.launch { workflow.run { seal() } }
    }
    if (confirmPrompt) Confirm(
        "确认完成服务恢复", "服务会核验封存版本和剩余复核事项。收到服务确认后，手机将同步正常模式的完整快照。",
        !idle || !current, { confirmPrompt = false },
    ) {
        confirmPrompt = false
        scope.launch { workflow.run { confirm() } }
    }
}

@Composable
private fun RecoveryItemCard(
    item: JsonObject,
    state: WorkbenchProjection,
    evidence: List<RecoveryEvidence>,
    enabled: Boolean,
    select: () -> Unit,
    decide: (String) -> Unit,
) {
    val original = evidence.firstOrNull { it.source.sourceType == "command" }?.source
    val ack = evidence.firstOrNull { it.source.sourceType == "ack" }?.source
    val data = original?.payload?.obj("data")
    val ackData = ack?.payload?.obj("data")
    val command = data?.text("cmd") ?: ackData?.text("cmd")
    val refs = item.recordIds()
    val canArchive = ARCHIVE in item.decisions() && original != null && evidence.size == refs.size
    val disposition = item.text("disposition")
    val title = command?.let(::commandLabel) ?: "工作台差异"
    DesignCard {
        SectionTitle(title, when (disposition) {
            ARCHIVE -> "已核对归档"
            null -> "待核对"
            else -> dispositionLabel(disposition)
        })
        Text(item.text("summary").orEmpty(), color = DeskColors.muted)
        if (original != null && data != null) {
            Text("旧操作 · ${timeText(original.capturedAt)}", fontWeight = FontWeight.SemiBold)
            originalDetails(data)?.let { Text(it) }
            Text("历史回执 · " + when (ackData?.flag("ok")) {
                true -> "成功"
                false -> "拒绝（${ackData.obj("error")?.text("code") ?: "未知"}）"
                null -> "无已验证回执"
            }, color = DeskColors.muted)
            Text("当前状态 · ${currentState(state, data)}")
        }
        SelectionContainer {
            Text(
                "原纪元 ${item.text("oldEpoch")}\n差异 ${item.text("itemId")}\n关联证据 ${refs.size} 条" +
                    (item.text("oldClientMsgId")?.let { "\n原操作 $it" } ?: ""),
                style = MaterialTheme.typography.bodySmall, color = DeskColors.muted,
            )
        }
        if (disposition == null) {
            if (canArchive) {
                OutlinedButton(
                    onClick = select,
                    enabled = enabled,
                    modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp),
                ) { Text("已核对，保留恢复后的状态") }
            }
            for (decision in item.decisions().filter { it != ARCHIVE }) {
                OutlinedButton(
                    onClick = { decide(decision) },
                    enabled = enabled,
                    modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp),
                ) { Text(dispositionLabel(decision)) }
            }
            if (!canArchive && item.decisions().none { it != ARCHIVE }) {
                Text(
                    "请在本地服务人工处理 · ${item.decisions().joinToString("、", transform = ::dispositionLabel)}",
                    color = DeskColors.amber,
                )
            }
        }
    }
}

@Composable
private fun ArchivePrompt(item: JsonObject, enabled: Boolean, dismiss: () -> Unit, submit: (String) -> Unit) {
    var note by remember(item.text("itemId")) { mutableStateOf("") }
    AlertDialog(
        onDismissRequest = dismiss,
        title = { Text("归档原操作") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text("保留当前工作台状态，归档全部 ${item.recordIds().size} 条关联证据。")
                OutlinedTextField(
                    value = note,
                    onValueChange = { if (it.length <= 1000) note = it },
                    label = { Text("人工核对说明") },
                    minLines = 2,
                    modifier = Modifier.fillMaxWidth(),
                )
            }
        },
        confirmButton = {
            TextButton(
                enabled = enabled && note.isNotBlank(), onClick = { submit(note.trim()) },
                modifier = Modifier.heightIn(min = 48.dp),
            ) { Text("提交归档") }
        },
        dismissButton = {
            TextButton(onClick = dismiss, modifier = Modifier.heightIn(min = 48.dp)) { Text("取消") }
        },
    )
}

@Composable
private fun RecoveryDecisionPrompt(
    item: JsonObject,
    disposition: String,
    state: WorkbenchProjection,
    enabled: Boolean,
    model: WorkbenchViewModel,
    reconciliationId: String?,
    dismiss: () -> Unit,
    submit: (RecoveryFormBatch, String) -> Unit,
) {
    val draftId = "recovery:${reconciliationId ?: "pending"}:${item.text("itemId")}:$disposition"
    var draftText by rememberSaveable(draftId) { mutableStateOf<String?>(null) }
    var fieldErrors by remember(draftId) { mutableStateOf<Map<String, String>>(emptyMap()) }
    val stored = state.drafts[draftId]
    LaunchedEffect(draftId, stored) {
        if (draftText == null) {
            draftText = (stored ?: initialRecoveryDraft(disposition)).toString()
        }
    }
    LaunchedEffect(draftText) {
        draftText?.let(::recoveryDraft)?.let { model.saveRecoveryDraft(draftId, it) }
    }
    val draft = draftText?.let(::recoveryDraft) ?: JsonObject(emptyMap())
    fun update(key: String, value: JsonElement) {
        draftText = JsonObject(draft.toMutableMap().also { it[key] = value }).toString()
    }
    fun text(key: String) = draft.text(key).orEmpty()
    fun flag(key: String) = draft.flag(key) == true
    val globalRevision = state.meta?.number("positionRevision")
    val positions = state.records["positions"].orEmpty()
    val resolved = text("resolvedCsv").split(',').map { it.trim() }.filter { it.isNotEmpty() }.toSet()
    val recordInput = if (disposition == "record") {
        RecoveryForms.record(draft, text("newExecutionId"))
    } else {
        null
    }
    val includedInput = if (disposition == "included" || (disposition == "record" && flag("withPosition"))) {
        RecoveryForms.included(draft, globalRevision, resolved)
    } else {
        null
    }
    val valid = when (disposition) {
        "record" -> recordInput?.ok == true && (includedInput == null || includedInput.ok) && text("note").isNotBlank()
        "included" -> includedInput?.ok == true && text("note").isNotBlank()
        else -> text("note").isNotBlank()
    }
    @Composable
    fun FieldError(key: String) {
        fieldErrors[key]?.let { Text(it, color = DeskColors.amber, style = MaterialTheme.typography.bodySmall) }
    }
    @Composable
    fun PositionFields() {
        Text("当前全局持仓基线版本 ${globalRevision ?: "未知"}；事实时点与本次核对时点分开记录。", color = DeskColors.muted)
        positions.forEach { position ->
            Text(
                "当前 ${position.text("symbol")} · 数量 ${position.value("qty")} · " +
                    "可卖 ${position.value("availableQty")} · 版本 ${position.value("positionRevision")} · " +
                    "事实时点 ${timeText(position.number("asOf"))}",
                color = DeskColors.muted,
            )
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            positions.forEach { position ->
                TextButton(
                    onClick = {
                        update("positionSymbol", JsonPrimitive(position.text("symbol").orEmpty()))
                        update("basePositionRevision", globalRevision?.let(::JsonPrimitive) ?: JsonNull)
                        update("positionQty", JsonPrimitive(position.number("qty")?.toString().orEmpty()))
                        update("availableQty", JsonPrimitive(position.number("availableQty")?.toString().orEmpty()))
                        update("cost", JsonPrimitive(position.text("cost").orEmpty()))
                        update("positionAsOf", JsonPrimitive(System.currentTimeMillis()))
                    },
                    modifier = Modifier.heightIn(min = 48.dp),
                ) { Text("采用 ${position.text("symbol")}") }
            }
        }
        Field("证券代码", text("positionSymbol"), { update("positionSymbol", JsonPrimitive(it)) })
        FieldError("position.positionSymbol")
        Field("本次核对时点（毫秒）", text("positionAsOf"), { update("positionAsOf", JsonPrimitive(it)) }, KeyboardType.Number)
        FieldError("position.positionAsOf")
        Field("数量", text("positionQty"), { update("positionQty", JsonPrimitive(it)) }, KeyboardType.Number)
        FieldError("position.positionQty")
        Field("可卖数量（未知留空）", text("availableQty"), { update("availableQty", JsonPrimitive(it)) }, KeyboardType.Number)
        FieldError("position.availableQty")
        Field("成本", text("cost"), { update("cost", JsonPrimitive(it)) }, KeyboardType.Decimal)
        FieldError("position.cost")
        Text("basePositionRevision ${globalRevision ?: "未知"}", color = DeskColors.muted)
        FieldError("position.basePositionRevision")
        val unresolved = state.records["executions"].orEmpty().filter {
            it.text("symbol") == text("positionSymbol") && it.flag("reconciliationRequired") == true
        }
        if (unresolved.isNotEmpty()) {
            Text("同批可核销成交", color = DeskColors.muted)
            for (execution in unresolved) {
                val id = execution.text("executionId") ?: continue
                CheckRow(
                    "${execution.text("side")} ${execution.value("qty")} @ ${execution.value("price")} · $id",
                    id in resolved,
                ) { checked ->
                    val next = if (checked) resolved + id else resolved - id
                    update("resolvedCsv", JsonPrimitive(next.sorted().joinToString(",")))
                }
            }
        }
        FieldError("position.resolvedExecutionIds")
    }
    AlertDialog(
        onDismissRequest = dismiss,
        title = { Text(dispositionLabel(disposition)) },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text(item.text("summary").orEmpty(), color = DeskColors.muted)
                when (disposition) {
                    "record" -> {
                        Text("只有关联到真实成交证据时才补录；新成交 ID 由手机预分配，失败或进程重建后沿用同一 ID。", color = DeskColors.muted)
                        SelectionContainer { Text("预分配成交 ID ${text("newExecutionId")}", style = MaterialTheme.typography.bodySmall) }
                        fieldErrors["newExecutionId"]?.let { Text(it, color = DeskColors.amber) }
                        Field("证券代码", text("symbol"), { update("symbol", JsonPrimitive(it)) })
                        FieldError("symbol")
                        Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                            CheckRow("买入", text("side") == "buy") { if (it) update("side", JsonPrimitive("buy")) }
                            CheckRow("卖出", text("side") == "sell") { if (it) update("side", JsonPrimitive("sell")) }
                        }
                        FieldError("side")
                        Field("成交价", text("price"), { update("price", JsonPrimitive(it)) }, KeyboardType.Decimal)
                        FieldError("price")
                        Field("数量", text("qty"), { update("qty", JsonPrimitive(it)) }, KeyboardType.Number)
                        FieldError("qty")
                        Field("成交时间（毫秒）", text("executedAt"), { update("executedAt", JsonPrimitive(it)) }, KeyboardType.Number)
                        FieldError("executedAt")
                        Field("费用（可选，用户申报）", text("fees"), { update("fees", JsonPrimitive(it)) }, KeyboardType.Decimal)
                        FieldError("fees")
                        CheckRow("同批确认当前持仓基线（引用本次新成交 ID）", flag("withPosition")) {
                            update("withPosition", JsonPrimitive(it))
                        }
                        if (flag("withPosition")) PositionFields()
                    }
                    "included" -> {
                        Text("确认该差异已包含在当前权威快照。手机必须提交真实当前基线；服务端会按证据和版本核对。", color = DeskColors.muted)
                        PositionFields()
                    }
                    "discard_intent" -> Text(
                        "仅可对服务已确认未执行、且有确定性拒绝回执的旧意图使用；结果未知或 not_found 不能默认丢弃。",
                        color = DeskColors.amber,
                    )
                }
                OutlinedTextField(
                    value = text("note"),
                    onValueChange = { if (it.length <= 1000) update("note", JsonPrimitive(it)) },
                    label = { Text("人工核对说明（必填）") },
                    minLines = 2,
                    modifier = Modifier.fillMaxWidth(),
                )
                fieldErrors["note"]?.let { Text(it, color = DeskColors.amber) }
            }
        },
        confirmButton = {
            TextButton(
                enabled = enabled,
                onClick = {
                    if (!valid) {
                        fieldErrors = buildMap {
                            recordInput?.errors?.let(::putAll)
                            includedInput?.errors?.forEach { (key, value) -> put("position.$key", value) }
                            if (text("note").isBlank()) put("note", "请填写人工核对说明")
                            if (disposition == "record" && recordInput?.value == null) {
                                if ("newExecutionId" !in this) put("newExecutionId", "预分配成交 ID 无效")
                            }
                        }
                        return@TextButton
                    }
                    fieldErrors = emptyMap()
                    val batch = when (disposition) {
                        "record" -> RecoveryFormBatch(recordInput!!.value, includedInput?.value)
                        "included" -> RecoveryFormBatch(included = includedInput!!.value)
                        else -> RecoveryFormBatch()
                    }
                    submit(batch, text("note").trim())
                },
                modifier = Modifier.heightIn(min = 48.dp),
            ) { Text("提交核对") }
        },
        dismissButton = {
            TextButton(onClick = dismiss, modifier = Modifier.heightIn(min = 48.dp)) { Text("取消") }
        },
    )
}

private fun initialRecoveryDraft(disposition: String): JsonObject = buildJsonObject {
    put("newExecutionId", UUID.randomUUID().toString())
    put("side", "buy")
    put("note", "")
    if (disposition == "included") put("withPosition", true)
}

private fun recoveryDraft(value: String): JsonObject? =
    runCatching { Json.parseToJsonElement(value) as? JsonObject }.getOrNull()

private fun dispositionLabel(name: String): String = when (name) {
    ARCHIVE -> "归档原操作"
    "included" -> "核对已包含"
    "record" -> "独立补录成交"
    "discard_intent" -> "放弃未执行意图"
    else -> "未知决定，需更新 App"
}

private fun originalDetails(data: JsonObject): String? {
    val fields = listOf("symbol", "taskId", "fileId", "importId", "uploadId", "transferId", "scanId")
        .mapNotNull(data::text).distinct().toMutableList()
    data.text("profile")?.let { fields.add(profileLabel(it)) }
    data.text("noticeVersion")?.let { fields.add("告知版本 $it") }
    val settings = data.obj("settings") ?: data
    if (data.text("cmd") == "set_settings") fields.add(settingsLabel(settings))
    data.number("snoozeUntil")?.let { fields.add("稍后至 ${timeText(it)}") }
    data.text("text")?.let { fields.add(it.take(180) + if (it.length > 180) "…" else "") }
    return fields.joinToString(" · ").takeIf { it.isNotBlank() }
}

private fun currentState(state: WorkbenchProjection, data: JsonObject): String {
    val analysis = state.meta?.obj("analysis")
    fun record(group: String, id: String) = state.records[group]?.firstOrNull { it[id] == data[id] }
    return when (data.text("cmd")) {
        "pause", "resume" -> when (analysis?.flag("paused")) {
            true -> "分析已暂停"
            false -> "分析运行中"
            null -> "分析状态未知"
        }
        "set_profile" -> profileLabel(analysis?.text("profile"))
        "set_settings" -> state.meta?.obj("settings")?.let(::settingsLabel) ?: "提醒设置未知"
        "register_push" -> when (state.reads["get_status"]?.obj("device")?.flag("registrationValid")) {
            true -> "设备登记有效"
            false -> "设备登记未确认"
            null -> "请在工作台核对设备登记"
        }
        "watch", "unwatch" -> if (record("watchlist", "symbol") != null) "已关注" else "当前关注列表无此标的"
        "scan_now" -> analysis?.text("lastScanId")?.let { "最近扫描 $it" } ?: "当前快照无最近扫描"
        "accept_vision_notice" -> state.meta?.obj("visionNotice")?.let {
            "告知版本 ${it.text("noticeVersion")} · 同意时间 ${timeText(it.number("consentedAt"))}"
        } ?: "识别告知状态未知"
        "register_image", "cancel_import" -> record("imports", "importId")?.text("status")
            ?: "当前快照无对应导入"
        "ignore", "snooze" -> record("tasks", "taskId")?.let {
            "事项状态 ${it.text("status")} · 稍后至 ${timeText(it.number("snoozeUntil"))}"
        } ?: "当前快照无对应事项"
        "delete_file", "begin_upload", "finalize_upload", "cancel_transfer" -> "请在本地服务核对文件与传输状态"
        "chat" -> "请在对话中核对当前历史"
        else -> "需在本地服务核对"
    }
}

private fun profileLabel(profile: String?): String = when (profile) {
    "conservative" -> "保守档案"
    "aggressive" -> "激进档案"
    else -> "扫描档案未知"
}

private fun settingsLabel(settings: JsonObject): String =
    "夜间 ${settings.text("nightStart") ?: "未知"}–${settings.text("nightEnd") ?: "未知"} · " +
        "夜间严重提醒 ${enabledLabel(settings.flag("criticalAtNight"))} · 推送 ${enabledLabel(settings.flag("pushEnabled"))}"

private fun enabledLabel(enabled: Boolean?) = when (enabled) {
    true -> "开"
    false -> "关"
    null -> "未知"
}

private fun JsonObject.recordIds() = array("evidenceRecordIds")!!.map { it.jsonPrimitive.content }
private fun JsonObject.decisions() = array("allowedDecisions")!!.map { it.jsonPrimitive.content }

private data class RecoveryStatus(
    val id: String,
    val sequence: Long,
    val sealed: Boolean,
    val verified: Long,
    val receiving: Long,
    val invalid: Long,
    val records: List<JsonObject>,
    val items: List<JsonObject>,
) {
    val open = items.filter { it["disposition"] == JsonNull }
    val byId = records.associateBy { it.text("recordId")!! }

    fun params() = buildJsonObject {
        put("reconciliationId", id)
        put("reviewedSnapshotSeq", sequence)
    }
}

private class RecoveryWorkflow(private val model: WorkbenchViewModel) {
    var busy by mutableStateOf(false)
        private set
    var service by mutableStateOf<RecoveryStatus?>(null)
        private set
    var message by mutableStateOf<String?>(null)
        private set
    var failed by mutableStateOf(false)
        private set
    var confirmed by mutableStateOf(false)
        private set
    var frozen by mutableStateOf<List<RecoveryEvidence>?>(null)
        private set

    suspend fun run(action: suspend RecoveryWorkflow.() -> Unit): Boolean {
        if (busy) return false
        busy = true
        message = null
        failed = false
        try {
            action()
        } catch (cancel: CancellationException) {
            throw cancel
        } catch (error: Exception) {
            failed = true
            message = when {
                error is RecoveryProblem -> error.message
                error.message in setOf("RECOVERY_EVIDENCE_CHANGED", "本机证据已变化，请重新核对并上传") -> {
                    frozen = null
                    "手机证据已变化，请重新收集和封存。"
                }
                error.message == "RECOVERY_VERSION_CONFLICT" -> "服务端核对版本已变化，请刷新恢复状态。"
                error.message == "RECOVERY_EVIDENCE_INCOMPLETE" ->
                    "服务仍有待复核记录。请核对证据、成交、持仓及隔离原图，然后刷新恢复状态。"
                error.message?.matches(Regex("[A-Z][A-Z_0-9]{2,79}")) == true ->
                    "恢复操作未完成 · ${error.message}"
                else -> "恢复操作未完成。请刷新恢复状态，查看服务是否已确认。"
            }
        } finally {
            busy = false
        }
        return !failed
    }

    suspend fun refresh() {
        val status = readStatus()
        if (service?.id != null && service?.id != status.id) frozen = null
        service = status
        if ((model.state.value.sequence ?: -1) < status.sequence) model.refreshRecoverySnapshot()
    }

    fun manifest(status: RecoveryStatus): String? =
        frozen?.let { recoveryManifest(it, status.records) }

    fun evidenceFor(item: JsonObject): List<RecoveryEvidence> {
        val keys = item.recordIds().toSet()
        val records = service?.byId.orEmpty()
        return frozen.orEmpty().filter { evidence ->
            evidence.recordId in keys && records[evidence.recordId]?.let {
                it.text("status") == "verified" && evidence.matches(it)
            } == true
        }
    }

    suspend fun collectAndUpload() {
        val local = model.state.value
        val epoch = local.epoch ?: fail("等待当前纪元的完整快照。")
        val existing = service ?: fail("请先读取恢复状态。")
        requireCurrent(existing)
        frozen = null
        val collection = collect()
        val prepared = collection.map {
            try { it.prepare(epoch) } catch (_: ProtocolException) { fail("一条原始证据不符合 v2 契约，请在本地服务核对。") }
        }
        requireRecovery(prepared.distinctBy { it.recordId }.size == prepared.size, "存在冲突的原始证据元数据。")
        refresh()
        val status = service ?: fail("请先读取恢复状态。")
        requireRecovery(status.id == existing.id, "恢复会话已变化，请重新收集。")
        requireRecovery(status.invalid == 0L, "存在无效证据，请在本地服务核对。")

        for ((index, evidence) in prepared.withIndex()) {
            val remote = status.byId[evidence.recordId]
            requireRecovery(remote == null || evidence.matches(remote), "服务端已有记录与手机证据不一致。")
            if (remote?.text("status") == "verified") continue
            message = "正在处理证据 ${index + 1}/${prepared.size}"
            for (params in evidence.uploadParts(status.id)) {
                val ack = model.recoveryRequest("recovery_evidence", params)
                val record = ack.obj("record")
                requireRecovery(ack.text("reconciliationId") == status.id &&
                    record != null && evidence.matches(record) && record.text("status") != "invalid",
                    "服务未验证原始证据，请在本地服务核对。")
            }
        }
        refresh()
        val updated = service ?: fail("请先读取恢复状态。")
        requireRecovery(updated.id == status.id, "恢复会话已变化，请重新收集。")
        requireRecovery(recoveryManifest(prepared, updated.records) != null, "仍有缺片、待配对回执或无效证据。")
        requireRecovery(collect() == collection, "收集后发现新的手机证据，请重新收集。")
        frozen = prepared
        message = "已冻结 ${prepared.size} 条手机证据。"
    }

    suspend fun archive(item: JsonObject, note: String) {
        val status = service ?: fail("请先读取恢复状态。")
        requireCurrent(status)
        val evidence = evidenceFor(item)
        requireRecovery(manifest(status) != null && item in status.open && ARCHIVE in item.decisions() &&
            evidence.size == item.recordIds().size && evidence.any { it.source.sourceType == "command" } &&
            note.isNotBlank() && note.length <= 1000, "需先核对该差异的全部原始证据。")
        val itemId = item.text("itemId")!!
        val decision = buildJsonObject {
            put("itemId", itemId)
            put("evidenceRecordIds", item.getValue("evidenceRecordIds"))
            put("disposition", ARCHIVE)
            put("note", note)
        }
        validateFrozen()
        val result = model.recoveryRequest("recovery_reconcile", buildJsonObject {
            status.params().forEach { (key, value) -> put(key, value) }
            put("decisions", JsonArray(listOf(decision)))
            put("positions", JsonArray(emptyList()))
        })
        requireRecovery(result.text("reconciliationId") == status.id &&
            result.array("closedItemIds")?.contains(JsonPrimitive(itemId)) == true, "服务未确认该差异已归档。")
        refresh()
        message = "服务已确认归档。"
    }

    suspend fun record(
        item: JsonObject,
        input: RecoveryRecordInput,
        included: RecoveryIncludedInput?,
        note: String,
    ) {
        val status = service ?: fail("请先读取恢复状态。")
        requireCurrent(status)
        requireRecovery(manifest(status) != null && item in status.open && "record" in item.decisions() &&
            evidenceFor(item).size == item.recordIds().size, "需先核对该差异的全部原始证据。")
        val itemId = item.text("itemId")!!
        val decision = buildJsonObject {
            put("itemId", itemId)
            put("evidenceRecordIds", item.getValue("evidenceRecordIds"))
            put("disposition", "record")
            put("note", note)
            put("newExecutionId", input.newExecutionId)
            put("execution", input.executionJson())
        }
        val resolved = (included?.resolvedExecutionIds.orEmpty() + input.newExecutionId).distinct().sorted()
        val positions = included?.let {
            JsonArray(listOf(it.copy(resolvedExecutionIds = resolved).positionJson()))
        } ?: JsonArray(emptyList())
        reconcile(status, decision, positions, "服务未确认成交补录。")
        message = "服务已确认补录；成交台账与新快照需重新核对。"
    }

    suspend fun included(item: JsonObject, input: RecoveryIncludedInput, note: String) {
        val status = service ?: fail("请先读取恢复状态。")
        requireCurrent(status)
        requireRecovery(manifest(status) != null && item in status.open && "included" in item.decisions() &&
            evidenceFor(item).size == item.recordIds().size, "需先核对该差异的全部原始证据。")
        val decision = buildJsonObject {
            put("itemId", item.text("itemId"))
            put("evidenceRecordIds", item.getValue("evidenceRecordIds"))
            put("disposition", "included")
            put("note", note)
        }
        val positions = JsonArray(listOf(input.positionJson()))
        reconcile(status, decision, positions, "服务未确认该成交已包含在快照中。")
        message = "服务已确认该差异已包含在当前快照。"
    }

    suspend fun discard(item: JsonObject, note: String) {
        val status = service ?: fail("请先读取恢复状态。")
        requireCurrent(status)
        requireRecovery(manifest(status) != null && item in status.open && "discard_intent" in item.decisions() &&
            evidenceFor(item).size == item.recordIds().size, "需先核对该差异的全部原始证据。")
        val decision = buildJsonObject {
            put("itemId", item.text("itemId"))
            put("evidenceRecordIds", item.getValue("evidenceRecordIds"))
            put("disposition", "discard_intent")
            put("note", note)
        }
        reconcile(status, decision, JsonArray(emptyList()), "服务未确认该意图可丢弃。")
        message = "服务已确认该未执行意图可丢弃。"
    }

    private suspend fun reconcile(
        status: RecoveryStatus,
        decision: JsonObject,
        positions: JsonArray,
        failure: String,
    ) {
        validateFrozen()
        val result = model.recoveryRequest("recovery_reconcile", buildJsonObject {
            status.params().forEach { (key, value) -> put(key, value) }
            put("decisions", JsonArray(listOf(decision)))
            put("positions", positions)
        })
        requireRecovery(result.text("reconciliationId") == status.id &&
            result.array("closedItemIds")?.contains(JsonPrimitive(decision.text("itemId"))) == true, failure)
        refresh()
    }

    suspend fun seal() {
        val status = service ?: fail("请先读取恢复状态。")
        requireCurrent(status)
        requireRecovery(status.open.isEmpty(), "还有未关闭的差异。")
        val hash = manifest(status) ?: fail("证据清单尚未完整验证。")
        val evidence = validateFrozen()
        val result = model.recoveryRequest("recovery_seal_evidence", buildJsonObject {
            status.params().forEach { (key, value) -> put(key, value) }
            put("manifestHash", hash)
            put("recordCount", status.records.size)
        }, evidence)
        requireRecovery(result.text("reconciliationId") == status.id &&
            result.number("reviewedSnapshotSeq") == status.sequence && result.text("evidenceStatus") == "sealed",
            "服务未确认封存。")
        refresh()
        message = if (service?.sealed == true) "服务已确认封存。" else "核对版本已变化，需重新封存。"
    }

    suspend fun confirm() {
        val status = service ?: fail("请先读取恢复状态。")
        val local = requireCurrent(status)
        requireRecovery(status.sealed && status.open.isEmpty() && manifest(status) != null, "证据封存或差异状态待核对。")
        val result = model.recoveryRequest("recovery_confirm", status.params(), validateFrozen())
        requireRecovery(result.text("reconciliationId") == status.id && result.flag("recoveryMode") == false &&
            result.text("serverEpoch") == local.epoch, "服务未确认当前纪元的恢复。")
        confirmed = true
    }

    private suspend fun validateFrozen(): Set<RecoveryEvidenceSource> {
        val expected = frozen?.map { it.source }?.toSet() ?: fail("请先收集并冻结证据。")
        if (collect() != expected) {
            frozen = null
            fail("手机证据已变化，请重新收集和封存。")
        }
        return expected
    }

    private suspend fun collect(): Set<RecoveryEvidenceSource> {
        val collection = model.collectRecoveryEvidence()
        if (collection.issues.isNotEmpty()) {
            frozen = null
            fail("有 ${collection.issues.size} 条证据无法完整收集，请保留原记录并在本地服务核对。")
        }
        return collection.sources.toSet()
    }

    private fun requireCurrent(status: RecoveryStatus): WorkbenchProjection {
        val state = model.state.value
        requireRecovery(state.connection.localOnline && state.connection.synced && state.serviceRecoveryActive &&
            (state.sequence ?: -1) >= status.sequence, "等待当前核对版本的完整快照。")
        return state
    }

    private suspend fun readStatus(): RecoveryStatus {
        val first = model.recoveryRequest("get_recovery", JsonObject(emptyMap()))
        val id = first.text("reconciliationId")!!
        val sequence = first.number("reviewedSnapshotSeq")!!
        val verified = first.number("verifiedCount")!!
        val receiving = first.number("receivingCount")!!
        val invalid = first.number("invalidCount")!!
        val status = first.text("evidenceStatus")
        val records = mutableListOf<JsonObject>()
        val items = mutableListOf<JsonObject>()
        val cursors = mutableSetOf<String>()
        var page = first
        while (true) {
            requireRecovery(page.flag("recoveryMode") == true && page.text("reconciliationId") == id &&
                page.number("reviewedSnapshotSeq") == sequence && page.text("evidenceStatus") == status &&
                page.number("verifiedCount") == verified && page.number("receivingCount") == receiving &&
                page.number("invalidCount") == invalid, "分页期间恢复状态已变化，请刷新。")
            records += page.array("records")!!.map { it.jsonObject }
            items += page.array("items")!!.map { it.jsonObject }
            val cursor = page.text("nextPageCursor")
            requireRecovery(page.flag("hasMore") == (cursor != null), "服务返回的恢复分页不完整。")
            if (cursor == null) break
            requireRecovery(cursors.add(cursor), "服务返回的恢复分页不完整。")
            page = model.recoveryRequest("get_recovery", buildJsonObject {
                put("reconciliationId", id)
                put("pageCursor", cursor)
            })
        }
        requireRecovery(
            records.count { it.text("status") == "verified" }.toLong() == verified &&
                records.count { it.text("status") == "receiving" }.toLong() == receiving &&
                records.count { it.text("status") == "invalid" }.toLong() == invalid &&
                records.distinctBy { it.text("recordId") }.size == records.size &&
                items.distinctBy { it.text("itemId") }.size == items.size, "服务返回的恢复清单不完整。",
        )
        return RecoveryStatus(id, sequence, status == "sealed", verified, receiving, invalid, records, items)
    }
}

private class RecoveryProblem(message: String) : IllegalStateException(message)
private fun fail(message: String): Nothing = throw RecoveryProblem(message)
private fun requireRecovery(condition: Boolean, message: String) {
    if (!condition) fail(message)
}
