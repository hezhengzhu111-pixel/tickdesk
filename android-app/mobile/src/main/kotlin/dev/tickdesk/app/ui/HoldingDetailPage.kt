package dev.tickdesk.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import dev.tickdesk.core.DeliveryStage
import dev.tickdesk.core.WorkbenchProjection
import dev.tickdesk.core.array
import dev.tickdesk.core.flag
import dev.tickdesk.core.number
import dev.tickdesk.core.obj
import dev.tickdesk.core.text
import kotlinx.coroutines.delay
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put
import java.util.UUID

private const val MAX_PARTITIONS = 32

/** A read is correlated to the exact request that produced it. */
private sealed interface ReadState {
    data object Idle : ReadState
    data object Pending : ReadState
    data class Ready(val result: JsonObject) : ReadState
    data class Failed(val code: String?) : ReadState
}

private fun readState(state: WorkbenchProjection, requestId: String?): ReadState {
    if (requestId == null) return ReadState.Idle
    val intent = state.intents.firstOrNull { it.id == requestId } ?: return ReadState.Pending
    return when (intent.stage) {
        DeliveryStage.APPLIED -> intent.result?.let(ReadState::Ready) ?: ReadState.Failed(intent.errorCode)
        DeliveryStage.REJECTED, DeliveryStage.CANCELLED, DeliveryStage.QUARANTINED,
        DeliveryStage.NEEDS_RECONCILIATION -> ReadState.Failed(intent.errorCode)
        else -> ReadState.Pending
    }
}

private fun partitionDraft(row: JsonObject): String = buildJsonObject {
    put("qty", row.text("qty") ?: row.number("qty")?.toString() ?: "0")
    put("restrictions", JsonArray(row.array("restrictions")?.toList() ?: emptyList()))
    row.obj("origin")?.let { put("origin", it) }
}.toString()

private fun draftJson(value: String): JsonObject? =
    runCatching { Json.parseToJsonElement(value).jsonObject }.getOrNull()

private fun JsonObject.draftRows(): List<JsonObject> =
    array("items")?.mapNotNull { (it as? JsonPrimitive)?.content?.let(::draftJson) }.orEmpty()

private fun JsonObject.resolvedIds(): Set<String> =
    array("resolved")?.mapNotNull { (it as? JsonPrimitive)?.content }.orEmpty().toSet()

private fun restrictionLabel(row: JsonObject): String {
    val restrictions = row.objects("restrictions")
    if (restrictions.isEmpty()) return "可卖"
    return restrictions.joinToString(" + ") { restriction ->
        when (restriction.text("kind")) {
            "settlement" ->
                "T+1 交收（${restriction.text("acquiredOn") ?: "买入日未知"} 买入，最早 ${restriction.text("earliestSession") ?: "未知"} 可卖）"
            "freeze" -> "其他冻结（凭证 ${restriction.text("evidenceRef") ?: "未填"}）"
            else -> "限制原因未知"
        }
    }
}

private fun holdingBaseline(holding: JsonObject): JsonObject {
    val checkedAt = listOfNotNull(
        holding.number("snapshotAsOf"),
        holding.number("positionAsOf"),
    ).maxOrNull() ?: 0
    return buildJsonObject {
        put("baseRevision", holding.number("positionRevision") ?: 0)
        put("items", JsonArray(holding.objects("partitions").map { JsonPrimitive(partitionDraft(it)) }))
        put("reason", "")
        put("asOf", checkedAt)
        put("useCurrentTime", false)
        put("resolved", JsonArray(emptyList()))
    }
}

private fun mergeDraft(draft: JsonObject, key: String, value: kotlinx.serialization.json.JsonElement): JsonObject =
    JsonObject(draft.toMutableMap().also { it[key] = value })

private fun holdingDraftKey(symbol: String) = "holding:$symbol"
private fun progressDraftKey(taskId: String) = "execution_progress:$taskId"

private val terminalStages = setOf(
    DeliveryStage.APPLIED, DeliveryStage.REJECTED, DeliveryStage.CANCELLED,
    DeliveryStage.QUARANTINED, DeliveryStage.NEEDS_RECONCILIATION,
)

@Composable
internal fun HoldingDetailsPage(
    state: WorkbenchProjection,
    symbol: String,
    model: WorkbenchViewModel,
    busy: Boolean,
    back: () -> Unit,
    openProgress: (String) -> Unit,
) {
    val online = state.connection.localOnline
    // A read queued before the first authoritative snapshot has no epoch-bound
    // projection to apply. Wait for the actual sync boundary instead of
    // generating more pending intents and then calling the wait a timeout.
    val ready = online && state.connection.synced && state.epoch != null
    val supported = "get_holding_details" in
        state.reads["get_status"]?.obj("protocol")?.stringList("supportedCommands").orEmpty()
    val scopeKey = listOf(state.pairId, state.epoch, symbol)

    var requestId by rememberSaveable(*scopeKey.toTypedArray()) { mutableStateOf<String?>(null) }
    fun reload() {
        if (!ready) return
        model.submitWithReceipt("get_holding_details", buildJsonObject { put("symbol", symbol) }) {
            requestId = it
        }
    }
    LaunchedEffect(state.pairId, state.epoch, symbol, ready) {
        if (ready && requestId == null) reload()
    }
    val read = readState(state, requestId)
    val result = (read as? ReadState.Ready)?.result
    val holding = result?.obj("holding")?.takeIf { it.text("symbol") == symbol }

    var draftText by rememberSaveable(*scopeKey.toTypedArray()) { mutableStateOf<String?>(null) }
    val draft = draftText?.let(::draftJson)
    // After a successful submit, ignore the pre-submit read so clearing the
    // draft cannot resurrect the old baseline. Adopt the first read whose
    // revision actually moved past the submitted base revision.
    var refreshAfterRevision by rememberSaveable(*scopeKey.toTypedArray()) { mutableStateOf<Long?>(null) }
    // Adopt the server baseline only when there is no draft. A new snapshot
    // never overwrites a draft the user is editing.
    LaunchedEffect(
        holding?.number("positionRevision"),
        holding?.text("symbol"),
        state.epoch,
        state.drafts[holdingDraftKey(symbol)],
        refreshAfterRevision,
    ) {
        val pending = refreshAfterRevision
        if (pending != null) {
            val revision = holding?.number("positionRevision")
            if (read is ReadState.Ready && revision != null && revision != pending) {
                refreshAfterRevision = null
                draftText = holdingBaseline(holding).toString()
            }
        } else if (draftText == null && state.epoch != null) {
            val stored = state.drafts[holdingDraftKey(symbol)]
            draftText = (stored ?: holding?.let(::holdingBaseline))?.toString()
        }
    }
    // Debounced persistence into Room via the repository, so rotation, back
    // navigation and process death keep the same draft.
    LaunchedEffect(draftText) {
        val value = draftText ?: return@LaunchedEffect
        delay(250)
        draftJson(value)?.let { model.saveDraft("holding", symbol, it) }
    }

    var submitted by rememberSaveable(*scopeKey.toTypedArray()) { mutableStateOf<String?>(null) }
    val saving = submitted?.let { id -> state.intents.firstOrNull { it.id == id } }
    val inFlight = saving != null && saving.stage !in terminalStages
    LaunchedEffect(saving?.id, saving?.stage) {
        when (saving?.stage) {
            DeliveryStage.APPLIED -> {
                refreshAfterRevision = draft?.number("baseRevision")
                submitted = null
                model.clearDraft("holding", symbol)
                draftText = null
                reload()
            }
            DeliveryStage.REJECTED, DeliveryStage.QUARANTINED, DeliveryStage.CANCELLED,
            DeliveryStage.NEEDS_RECONCILIATION -> {
                // Keep the rejected intent for the error/merge UI. A version
                // conflict also needs the new authoritative baseline before
                // the user can choose how to merge.
                if (saving.errorCode == "POSITION_VERSION_CONFLICT") reload()
            }
            else -> Unit
        }
    }

    val rows = draft?.draftRows().orEmpty()
    val quantities = rows.map { it.text("qty")?.trim()?.toLongOrNull() }
    val total = quantities.sumOf { it ?: 0L }
    val validRow = rows.isNotEmpty() && rows.size <= MAX_PARTITIONS &&
        quantities.all { it != null && it in 1..CanonicalJsonMax } &&
        rows.all { row ->
            row.objects("restrictions").all {
                it.text("kind") != "freeze" || !it.text("evidenceRef").isNullOrBlank()
            }
        }
    val latestRevision = holding?.number("positionRevision")
    val baseRevision = draft?.number("baseRevision")
    val stale = baseRevision != null && latestRevision != null && baseRevision != latestRevision
    val conflict = saving?.stage == DeliveryStage.REJECTED &&
        saving.errorCode == "POSITION_VERSION_CONFLICT"
    val unresolved = state.rows("executions").filter {
        it.text("symbol") == symbol && it.flag("reconciliationRequired") == true &&
            it.text("positionEffect") != "superseded"
    }

    SectionTitle("持仓分区 · ${holding?.text("symbol") ?: symbol}")
    DesignCard {
        when {
            !supported && state.reads["get_status"] != null ->
                Text("当前本地服务版本不支持权威持仓分区，不能在此提交。请升级同版服务后重试。", color = DeskColors.amber)
            !online -> Text("当前离线，无法读取或提交持仓分区；草稿会保留。", color = DeskColors.amber)
            !state.connection.synced || state.epoch == null ->
                Text("等待配对后的首次完整同步；同步完成前不发送持仓读取。", color = DeskColors.amber)
            read is ReadState.Failed ->
                Text("读取失败：${read.code ?: "传输未完成"}。草稿已保留，可重试。", color = DeskColors.amber)
            read == ReadState.Pending || (read == ReadState.Idle && online) ->
                Text("正在读取权威持仓分区…", color = DeskColors.muted)
            holding == null ->
                Text("服务端没有该标的的权威持仓分区。可用「更正持仓」登记券商快照。", color = DeskColors.muted)
            else -> {
                Text("数量 ${holding.value("qty")} · 可卖 ${holding.value("availableQty")}")
                Text(
                    "可卖依据 " + when (holding.text("sellableBasis")) {
                        "confirmed_snapshot" -> "用户确认的券商快照"
                        "local_inferred" -> "本地推算（仅作计划上限）"
                        else -> "未知"
                    } + " · 快照 ${timeText(holding.number("snapshotAsOf"))}",
                    color = DeskColors.muted,
                )
                Text(
                    "来源归属 " + when (holding.text("originStatus")) {
                        "assigned" -> "已分配"
                        "partial" -> "部分已分配"
                        "unallocated" -> "未分配"
                        else -> "未知"
                    } + if (holding.flag("reconciliationRequired") == true) " · 待对账" else "",
                    color = DeskColors.muted,
                )
                if (holding.flag("reconciliationRequired") == true) {
                    NoticeStrip("成交量或可卖依据尚未核实，数量建议待确认。", DeskColors.amber)
                }
            }
        }
    }
    if (holding != null && draft != null) {
        if (stale || conflict) {
            DesignCard {
                NoticeStrip(
                    "服务端持仓版本已从 ${baseRevision ?: "未知"} 前进到 ${latestRevision ?: "未知"}；界面草稿未丢失。",
                    DeskColors.amber,
                )
                Text(
                    "最新权威值：数量 ${holding.value("qty")} · 可卖 ${holding.value("availableQty")} · " +
                        "分区 ${holding.objects("partitions").size} 个",
                    color = DeskColors.muted,
                )
                Text(
                    "你的草稿：合计 $total 股 · ${rows.size} 个分区。请显式选择如何合并后再提交。",
                    color = DeskColors.muted,
                )
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedAction(
                        "采用最新版本并保留草稿",
                        enabled = latestRevision != null && !busy && !inFlight,
                    ) {
                        val adoptedAsOf = listOfNotNull(
                            draft.number("asOf"),
                            holding.number("positionAsOf"),
                            holding.number("snapshotAsOf"),
                        ).maxOrNull() ?: System.currentTimeMillis()
                        draftText = mergeDraft(
                            mergeDraft(draft, "baseRevision", JsonPrimitive(latestRevision)),
                            "asOf",
                            JsonPrimitive(adoptedAsOf),
                        ).toString()
                        submitted = null
                    }
                    OutlinedAction("放弃草稿并采用服务端分区", enabled = !busy && !inFlight) {
                        draftText = holdingBaseline(holding).toString()
                        submitted = null
                    }
                }
            }
        }
        DesignCard {
            SectionTitle("逐个分区核对", "提交后由本地服务校验规则与版本，App 不推算权威结果。")
            if (rows.isEmpty()) Text("暂无分区。", color = DeskColors.muted)
            rows.forEachIndexed { index, row ->
                val current = row.text("qty").orEmpty()
                Field("分区 ${index + 1} 数量", current, { value ->
                    val updated = row.toMutableMap()
                    updated["qty"] = JsonPrimitive(value)
                    val item = JsonObject(updated).toString()
                    val items = rows.map { it.toString() }.toMutableList()
                    items[index] = item
                    draftText = mergeDraft(
                        draft,
                        "items",
                        JsonArray(items.map { JsonPrimitive(it.toString()) }),
                    ).toString()
                }, KeyboardType.Number)
                Text(row.let(::restrictionLabel), color = DeskColors.muted)
                TextButton(
                    onClick = {
                        val items = rows.toMutableList().also { it.removeAt(index) }
                        draftText = mergeDraft(
                            draft,
                            "items",
                            JsonArray(items.map { JsonPrimitive(it.toString()) }),
                        ).toString()
                    },
                    enabled = !busy && !inFlight,
                    modifier = Modifier.heightIn(min = 48.dp),
                ) { Text("移除此分区") }
            }
            if (rows.isEmpty() || rows.any { it.text("qty")?.toLongOrNull() == null }) {
                Text("请为每个分区填写正整数数量。", color = DeskColors.amber)
            }
            if (rows.size > MAX_PARTITIONS) {
                Text("分区最多 $MAX_PARTITIONS 个，请先合并。", color = DeskColors.amber)
            }
        }
        DesignCard {
            SectionTitle("追加快照分区", "只能登记可核对的数量；T+1 交收批次必须来自实际买入与已核验规则。")
            var newQty by rememberSaveable(*scopeKey.toTypedArray()) { mutableStateOf("") }
            var newRef by rememberSaveable(*scopeKey.toTypedArray()) { mutableStateOf("") }
            Field("新增可卖 / 其他冻结数量", newQty, { newQty = it }, KeyboardType.Number)
            Field("冻结凭证（仅其他冻结需要）", newRef, { newRef = it })
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedAction(
                    "追加可卖",
                    enabled = !busy && !inFlight && rows.size < MAX_PARTITIONS && whole(newQty, true) != null,
                ) {
                    val row = buildJsonObject {
                        put("qty", newQty.trim())
                        put("restrictions", JsonArray(emptyList()))
                    }
                    draftText = mergeDraft(
                        draft,
                        "items",
                        JsonArray((rows.map { JsonPrimitive(it.toString()) } + JsonPrimitive(row.toString()))),
                    ).toString()
                    newQty = ""
                }
                OutlinedAction(
                    "追加其他冻结",
                    enabled = !busy && !inFlight && rows.size < MAX_PARTITIONS &&
                        whole(newQty, true) != null && newRef.isNotBlank(),
                ) {
                    val row = buildJsonObject {
                        put("qty", newQty.trim())
                        put("restrictions", JsonArray(listOf(buildJsonObject {
                            put("kind", "freeze")
                            put("evidenceRef", newRef.trim())
                        })))
                    }
                    draftText = mergeDraft(
                        draft,
                        "items",
                        JsonArray((rows.map { JsonPrimitive(it.toString()) } + JsonPrimitive(row.toString()))),
                    ).toString()
                    newQty = ""
                    newRef = ""
                }
            }
        }
        if (unresolved.isNotEmpty()) {
            DesignCard {
                SectionTitle("同一快照可核销的成交", "勾选后随本次权威核对一起核销；不勾选不会替你猜。")
                val resolved = draft.resolvedIds()
                for (execution in unresolved) {
                    val id = execution.text("executionId") ?: continue
                    Row {
                        CheckRow("${execution.text("side") ?: "?"} ${execution.value("qty")} 股 @ ${execution.value("price")} · $id", id in resolved) { checked ->
                            val next = if (checked) resolved + id else resolved - id
                            draftText = mergeDraft(
                                draft,
                                "resolved",
                                JsonArray(next.sorted().map { JsonPrimitive(it) }),
                            ).toString()
                        }
                    }
                }
            }
        }
        DesignCard {
            Field("核对说明", draft.text("reason").orEmpty(), { value ->
                draftText = mergeDraft(draft, "reason", JsonPrimitive(value)).toString()
            })
            val asOf = draft.number("asOf") ?: 0
            Text(
                "核对时点 ${timeText(asOf)} · 分区合计 $total 股 · 与持仓数量 ${holding.value("qty")} 一致后提交。",
                color = DeskColors.muted,
            )
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedAction("改用当前时间", enabled = !busy && !inFlight) {
                    draftText = mergeDraft(draft, "asOf", JsonPrimitive(System.currentTimeMillis())).toString()
                }
            }
            Text("默认沿用已读取快照的时点；只有确实刚核对过券商快照时才改用当前时间。", color = DeskColors.muted)
            val cost = state.rows("positions").firstOrNull { it.text("symbol") == symbol }
                ?.text("cost")?.takeIf { decimal(it) != null }
            if (cost == null) Text("登记成本缺失，请先在「更正持仓」补充。", color = DeskColors.amber)
            saving?.let { IntentText(it) }
            when {
                conflict -> NoticeStrip(
                    "提交被版本冲突拒绝。草稿和错误都保留：请采用最新版本或放弃草稿后，再用新的提交重试。",
                    DeskColors.amber,
                )
                saving?.stage == DeliveryStage.REJECTED ->
                    NoticeStrip("提交被拒绝：${saving.errorCode ?: "INVALID_PARAMS"}。草稿保留，可核对后重试。", DeskColors.amber)
                saving?.stage == DeliveryStage.QUARANTINED -> {
                    NoticeStrip("传输结果未知。请先查询原命令，不要换 ID 重发。", DeskColors.amber)
                    saving?.let { intent ->
                        OutlinedAction("查询原命令", enabled = online && !busy) { model.query(intent.id) }
                    }
                }
                inFlight -> NoticeStrip("已提交，正在等待服务端业务确认与权威快照；此期间不会重复发送。", DeskColors.blue)
            }
            OutlinedAction("重新读取权威分区", enabled = online && !busy && !inFlight) { reload() }
            OutlinedAction("放弃草稿并采用服务端分区", enabled = !busy && !inFlight) {
                draftText = holdingBaseline(holding).toString()
                submitted = null
            }
            MainAction(
                "核对并提交分区",
                enabled = state.canQueue && !busy && !inFlight && online && latestRevision != null &&
                    cost != null && validRow && total == holding.number("qty") &&
                    rows.isNotEmpty() && !stale,
            ) {
                val base = draft.number("baseRevision") ?: return@MainAction
                val asOfValue = draft.number("asOf") ?: return@MainAction
                val finalAsOf = if (asOfValue <= 0) System.currentTimeMillis() else asOfValue
                model.submitWithReceipt("reconcile_holding", buildJsonObject {
                    put("symbol", symbol)
                    put("basePositionRevision", base)
                    put("qty", total)
                    put("cost", cost)
                    put("asOf", finalAsOf)
                    put("reason", draft.text("reason")?.trim()?.ifBlank { "按券商分区快照核对" } ?: "按券商分区快照核对")
                    put("resolvedExecutionIds", JsonArray(draft.resolvedIds().sorted().map { JsonPrimitive(it) }))
                    put("partitions", JsonArray(rows.map { row ->
                        buildJsonObject {
                            put("qty", row.text("qty")?.toLongOrNull() ?: 0L)
                            put("restrictions", JsonArray(row.array("restrictions")?.toList() ?: emptyList()))
                            row.obj("origin")?.let { put("origin", it) }
                        }
                    }))
                }) { id -> submitted = id }
            }
        }
    }
    if (holding != null) {
        val plans = state.rows("tasks").filter { it.text("symbol") == symbol }
        if (plans.isNotEmpty()) {
            DesignCard {
                SectionTitle("该标的的执行计划", "事项过期或重启后仍可从这里核对未结束剩余量。")
                for (plan in plans) {
                    val id = plan.text("taskId") ?: continue
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Text(
                            "${taskDirection(plan.text("direction"))} · ${plan.value("plannedQty") ?: ""} · ${taskState(plan)}",
                            color = DeskColors.muted,
                            modifier = Modifier.weight(1f),
                        )
                        TextButton(onClick = { openProgress(id) }, modifier = Modifier.heightIn(min = 48.dp)) {
                            Text("执行余量")
                        }
                    }
                }
            }
        }
    }
    TextButton(onClick = back, modifier = Modifier.heightIn(min = 48.dp)) { Text("返回持仓") }
}

private val CanonicalJsonMax = 9_007_199_254_740_991L

@Composable
private fun OutlinedAction(label: String, enabled: Boolean, onClick: () -> Unit) {
    TextButton(onClick = onClick, enabled = enabled, modifier = Modifier.heightIn(min = 48.dp)) {
        Text(label)
    }
}

@Composable
internal fun ExecutionProgressPage(
    state: WorkbenchProjection,
    taskId: String,
    model: WorkbenchViewModel,
    busy: Boolean,
    back: () -> Unit,
) {
    val online = state.connection.localOnline
    val supported = "get_execution_progress" in
        state.reads["get_status"]?.obj("protocol")?.stringList("supportedCommands").orEmpty()
    val scopeKey = listOf(state.pairId, state.epoch, taskId)
    var requestId by rememberSaveable(*scopeKey.toTypedArray()) { mutableStateOf<String?>(null) }
    fun reload() {
        if (!online) return
        model.submitWithReceipt("get_execution_progress", buildJsonObject { put("taskId", taskId) }) {
            requestId = it
        }
    }
    LaunchedEffect(state.pairId, state.epoch, taskId, online) {
        if (online && requestId == null) reload()
    }
    val read = readState(state, requestId)
    val result = (read as? ReadState.Ready)?.result
    val progress = result?.obj("progress")?.takeIf { it.text("taskId") == taskId }
    val task = state.task(taskId)

    var draftText by rememberSaveable(*scopeKey.toTypedArray()) { mutableStateOf<String?>(null) }
    val draft = draftText?.let(::draftJson)
    LaunchedEffect(progress?.number("version"), state.epoch, state.drafts[progressDraftKey(taskId)]) {
        if (draftText == null && state.epoch != null) {
            val stored = state.drafts[progressDraftKey(taskId)]
            draftText = stored?.toString() ?: progress?.let(::progressBaseline)?.toString()
        }
    }
    LaunchedEffect(draftText) {
        val value = draftText ?: return@LaunchedEffect
        delay(250)
        draftJson(value)?.let { model.saveDraft("execution_progress", taskId, it) }
    }

    var submitted by rememberSaveable(*scopeKey.toTypedArray()) { mutableStateOf<String?>(null) }
    val saving = submitted?.let { id -> state.intents.firstOrNull { it.id == id } }
    val inFlight = saving != null && saving.stage !in terminalStages
    LaunchedEffect(saving?.id, saving?.stage) {
        when (saving?.stage) {
            DeliveryStage.APPLIED -> {
                submitted = null
                model.clearDraft("execution_progress", taskId)
                draftText = null
                reload()
            }
            DeliveryStage.REJECTED, DeliveryStage.QUARANTINED, DeliveryStage.CANCELLED,
            DeliveryStage.NEEDS_RECONCILIATION -> {
                // Keep the rejected intent for the error/merge UI. A version
                // conflict also needs the new authoritative baseline before
                // the user can choose how to merge.
                if (saving.errorCode == "PROGRESS_VERSION_CONFLICT") reload()
            }
            else -> Unit
        }
    }

    val choice = draft?.text("choice") ?: "processing"
    val remaining = draft?.text("remaining").orEmpty()
    val baseVersion = draft?.number("baseVersion")
    val latestVersion = progress?.number("version")
    val stale = baseVersion != null && latestVersion != null && baseVersion != latestVersion
    val conflict = saving?.stage == DeliveryStage.REJECTED &&
        saving.errorCode == "PROGRESS_VERSION_CONFLICT"
    val planned = progress?.number("plannedQty") ?: 0
    val confirmed = progress?.number("confirmedFillQty")
    val rest = whole(remaining)

    SectionTitle("执行进度申报 · ${task?.text("name") ?: taskId}")
    DesignCard {
        if (task == null) {
            Text("本机暂无此事项；若事项卡已过期，可从持仓明细的「执行余量」进入。", color = DeskColors.muted)
        } else {
            Text("${taskDirection(task.text("direction"))} · ${task.text("symbol")} · ${taskState(task)}")
            Text(task.obj("reasons")?.text("quant")?.takeIf(String::isNotBlank) ?: "暂无事项理由", color = DeskColors.muted)
        }
        NoticeStrip("这里只记录人对申报状态和剩余量的说明，不会产生成交；只有实际成交才进入台账。", DeskColors.blue)
    }
    DesignCard {
        when {
            !supported && state.reads["get_status"] != null ->
                Text("当前本地服务版本不支持执行进度申报。请升级同版服务；这不等于该事项没有执行计划。", color = DeskColors.amber)
            !online -> Text("当前离线，无法读取或提交执行进度；草稿会保留。", color = DeskColors.amber)
            read is ReadState.Failed ->
                Text("读取失败：${read.code ?: "传输未完成"}。草稿已保留，可重试。", color = DeskColors.amber)
            read == ReadState.Pending || (read == ReadState.Idle && online) ->
                Text("正在读取执行进度…", color = DeskColors.muted)
            result != null && result.obj("progress") == null ->
                Text("该事项没有登记执行计划，因此没有可申报的余量。", color = DeskColors.muted)
            progress == null -> Text("服务端返回的进度无法关联到该事项。", color = DeskColors.amber)
            else -> {
                Text("计划 ${progress.value("plannedQty")} 股 · 已核销成交 ${progress.value("confirmedFillQty")} 股")
                Text("申报余量 ${progress.value("reportedRemainingQty")} · 预留 ${progress.value("reservedQty")} 股")
                Text(
                    "状态 " + when (progress.text("state")) {
                        "processing" -> "处理中"
                        "partially_filled" -> "部分成交"
                        "ended" -> "余量结束"
                        "unknown" -> "结果未知"
                        else -> "尚未申报"
                    } + " · 版本 ${progress.value("version")} · 申报于 ${timeText(progress.number("reportedAt"))}",
                    color = DeskColors.muted,
                )
                if (progress.flag("reconciliationRequired") == true) {
                    NoticeStrip("存在未解决成交或版本差异，释放余量前需先完成对账。", DeskColors.amber)
                }
            }
        }
    }
    if (progress != null && draft != null) {
        if (stale || conflict) {
            DesignCard {
                NoticeStrip(
                    "服务端进度版本已从 ${baseVersion ?: "未知"} 前进到 ${latestVersion ?: "未知"}；草稿未丢失。",
                    DeskColors.amber,
                )
                Text(
                    "最新：状态 ${progress.text("state")} · 已核销 ${progress.value("confirmedFillQty")} · " +
                        "预留 ${progress.value("reservedQty")} · 版本 ${progress.value("version")}",
                    color = DeskColors.muted,
                )
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedAction(
                        "采用最新版本并保留草稿",
                        enabled = latestVersion != null && !busy && !inFlight,
                    ) {
                        draftText = mergeDraft(draft, "baseVersion", JsonPrimitive(latestVersion)).toString()
                        submitted = null
                    }
                    OutlinedAction("放弃草稿并重新读取", enabled = !busy && !inFlight) {
                        draftText = progressBaseline(progress).toString()
                        submitted = null
                    }
                }
            }
        }
        DesignCard {
            SectionTitle("申报处理中 / 部分成交 / 余量结束 / 结果未知", "按有效状态提交，服务端校验版本、成交核对与剩余量。")
            val options = listOf(
                "processing" to "处理中",
                "partially_filled" to "部分成交",
                "ended" to "余量结束",
                "unknown" to "结果未知",
            )
            Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                for ((id, label) in options) {
                    Row {
                        CheckRow(label, choice == id) {
                            if (it) draftText = mergeDraft(draft, "choice", JsonPrimitive(id)).toString()
                        }
                    }
                }
            }
            if (choice == "processing" || choice == "partially_filled") {
                Field("申报剩余数量", remaining, { value ->
                    draftText = mergeDraft(draft, "remaining", JsonPrimitive(value)).toString()
                }, KeyboardType.Number)
                if (rest == null || rest <= 0 || rest > planned - (confirmed ?: 0)) {
                    Text("剩余数量须为正整数，且不超过计划量减去已核销成交。", color = DeskColors.amber)
                }
                if (choice == "partially_filled" && (confirmed ?: 0) == 0L) {
                    Text("尚未核销任何成交，不能申报部分成交；请先回填实际成交。", color = DeskColors.amber)
                }
            }
            if (choice == "ended") {
                Text(
                    if (confirmed == null) "尚无已核销成交，不能结束余量。"
                    else "余量结束要求申报剩余 0，且已核销成交 $confirmed 股与账面一致。",
                    color = if (confirmed == null) DeskColors.amber else DeskColors.muted,
                )
            }
            if (choice == "unknown") {
                Text("结果未知会保留全部预留，直到你核对原命令或回填实际成交。", color = DeskColors.muted)
            }
            saving?.let { IntentText(it) }
            when {
                conflict -> NoticeStrip(
                    "提交被版本冲突拒绝。草稿和错误都保留：请采用最新版本或放弃草稿后，再用新的提交重试。",
                    DeskColors.amber,
                )
                saving?.stage == DeliveryStage.REJECTED ->
                    NoticeStrip("申报未通过：${saving.errorCode ?: "INVALID_PARAMS"}。草稿保留，请核对状态与数量。", DeskColors.amber)
                saving?.stage == DeliveryStage.QUARANTINED -> {
                    NoticeStrip("传输结果未知。请先查询原命令，不要换 ID 重发。", DeskColors.amber)
                    saving?.let { intent ->
                        OutlinedAction("查询原命令", enabled = online && !busy) { model.query(intent.id) }
                    }
                }
                inFlight -> NoticeStrip("已提交，正在等待业务 ACK 与权威进度；此期间不会重复发送。", DeskColors.blue)
            }
            OutlinedAction("重新读取服务端进度", enabled = online && !busy && !inFlight) { reload() }
            val enabled = state.canQueue && !busy && !inFlight && online && !stale && when (choice) {
                "processing" -> rest != null && rest <= planned - (confirmed ?: 0)
                "partially_filled" ->
                    (confirmed ?: 0) > 0 && rest != null && rest <= planned - (confirmed ?: 0)
                "ended" -> confirmed != null
                else -> true
            }
            MainAction("提交申报状态", enabled) {
                val base = draft.number("baseVersion") ?: return@MainAction
                model.submitWithReceipt("set_execution_progress", buildJsonObject {
                    put("taskId", taskId)
                    put("baseVersion", base)
                    put("state", choice)
                    when (choice) {
                        "processing", "partially_filled" -> {
                            put("reportedRemainingQty", whole(remaining))
                            put("expectedConfirmedQty", JsonNull)
                        }
                        "ended" -> {
                            put("reportedRemainingQty", 0L)
                            put("expectedConfirmedQty", confirmed)
                        }
                        else -> {
                            put("reportedRemainingQty", JsonNull)
                            put("expectedConfirmedQty", JsonNull)
                        }
                    }
                    put("asOf", System.currentTimeMillis())
                }) { id -> submitted = id }
            }
        }
    }
    TextButton(onClick = back, modifier = Modifier.heightIn(min = 48.dp)) { Text("返回事项") }
}

private fun progressBaseline(progress: JsonObject): JsonObject = buildJsonObject {
    put("baseVersion", progress.number("version") ?: 0)
    put("choice", "processing")
    put("remaining", "")
}

@Composable
private fun ReviewNoteEditor(
    item: JsonObject,
    state: WorkbenchProjection,
    model: WorkbenchViewModel,
    onSaved: () -> Unit,
) {
    val sellId = item.text("sellExecutionId") ?: return
    val buyId = item.text("buyExecutionId") ?: return
    val scope = listOf(state.pairId, state.epoch, sellId)
    var verdict by rememberSaveable(*scope.toTypedArray()) { mutableStateOf("false_positive") }
    var note by rememberSaveable(*scope.toTypedArray()) { mutableStateOf("") }
    var reviewer by rememberSaveable(*scope.toTypedArray()) { mutableStateOf("本机用户") }
    var noteId by rememberSaveable(*scope.toTypedArray()) { mutableStateOf(UUID.randomUUID().toString()) }
    var submitted by rememberSaveable(*scope.toTypedArray()) { mutableStateOf<String?>(null) }
    val saving = submitted?.let { id -> state.intents.firstOrNull { it.id == id } }
    val inFlight = saving != null && saving.stage !in terminalStages
    LaunchedEffect(saving?.stage) {
        when (saving?.stage) {
            DeliveryStage.APPLIED -> {
                submitted = null
                note = ""
                noteId = UUID.randomUUID().toString()
                onSaved()
            }
            DeliveryStage.REJECTED, DeliveryStage.QUARANTINED, DeliveryStage.CANCELLED,
            DeliveryStage.NEEDS_RECONCILIATION -> Unit // keep the note and its error for retry
            else -> Unit
        }
    }
    Text("人工复核（不改成交、不调参、不自动通知）", color = DeskColors.muted)
    val options = listOf(
        "false_positive" to "误报",
        "missed_opportunity" to "漏报",
        "risk_confirmed" to "风险成立",
        "risk_rejected" to "风险不成立",
        "evidence_insufficient" to "证据不足",
    )
    Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
        for ((id, label) in options) {
            Row { CheckRow(label, verdict == id) { if (it) verdict = id } }
        }
    }
    Field("复核说明（必填）", note, { note = it }, lines = 3)
    Field("复核人（本机记录，不是券商身份）", reviewer, { reviewer = it })
    saving?.let { IntentText(it) }
    if (saving?.stage == DeliveryStage.REJECTED) {
        NoticeStrip("复核保存失败：${saving.errorCode ?: "INVALID_PARAMS"}。原说明保留。", DeskColors.amber)
    }
    OutlinedAction(
        "保存人工复核",
        enabled = state.canQueue && !inFlight && note.isNotBlank() && reviewer.isNotBlank(),
    ) {
        model.submitWithReceipt("record_trade_review_note", buildJsonObject {
            put("noteId", noteId)
            put("subjectType", "execution")
            put("subjectId", sellId)
            put("verdict", verdict)
            put("note", note.trim())
            put("reviewer", reviewer.trim())
            put("evidenceExecutionIds", JsonArray(listOf(buyId, sellId).distinct().map { JsonPrimitive(it) }))
            put("at", System.currentTimeMillis())
        }) { submitted = it }
    }
}

@Composable
private fun ExecutionEvidencePrompt(
    executionId: String,
    side: String,
    item: JsonObject,
    state: WorkbenchProjection,
    dismiss: () -> Unit,
) {
    val execution = state.records["executions"].orEmpty()
        .firstOrNull { it.text("executionId") == executionId }
    val notes = state.reads["get_trade_review_notes"]?.objects("items").orEmpty()
        .filter { it.text("subjectId") == executionId }
    AlertDialog(
        onDismissRequest = dismiss,
        title = { Text("${side}原始成交") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                SelectionContainer { Text(executionId, style = androidx.compose.material3.MaterialTheme.typography.bodySmall) }
                if (execution == null) {
                    Text("不可用：当前作用域快照没有该成交。配对卡片中的数据不能替代原始成交。", color = DeskColors.amber)
                    Text(
                        "配对卡片记录：${item.value("symbol")} · ${item.value("qty")} 股 · " +
                            "${item.value("buyPrice")} / ${item.value("sellPrice")}",
                        color = DeskColors.muted,
                    )
                } else {
                    val matches = execution.text("symbol") == item.text("symbol") &&
                        (side != "买入" || execution.text("side") == "buy") &&
                        (side != "卖出" || execution.text("side") == "sell")
                    Text(if (matches) "与当前配对卡片一致" else "与当前配对卡片不一致", color = if (matches) DeskColors.green else DeskColors.amber)
                    Text("证券 ${execution.value("symbol")} · ${execution.value("side")} · ${execution.value("qty")} 股")
                    Text("价格 ${execution.value("price")} · 时间 ${timeText(execution.number("executedAt"))}")
                    Text(
                        "费用 ${execution.value("fees")} · 来源 ${execution.value("feeSource")} · " +
                            "状态 ${execution.value("positionEffect")} · 待对账 ${execution.value("reconciliationRequired")}",
                        color = DeskColors.muted,
                    )
                    Text("相关事项 ${execution.text("taskId") ?: "无"}", color = DeskColors.muted)
                    Text(
                        "配对证据：毛额 ${item.value("grossPnl")} · 净额 ${item.value("netPnl")} · " +
                            "买费 ${item.value("buyFee")} · 卖费 ${item.value("sellFee")}",
                        color = DeskColors.muted,
                    )
                }
                if (notes.isNotEmpty()) {
                    Text("人工复核 ${notes.size} 条")
                    for (note in notes) Text("${note.text("verdict")} · ${note.text("note")}", color = DeskColors.muted)
                }
            }
        },
        confirmButton = {
            TextButton(onClick = dismiss, modifier = Modifier.heightIn(min = 48.dp)) { Text("关闭") }
        },
    )
}

@Composable
private fun SampleEvidencePrompt(
    sample: JsonObject,
    state: WorkbenchProjection,
    model: WorkbenchViewModel,
    online: Boolean,
    dismiss: () -> Unit,
) {
    var requestId by rememberSaveable(sample.text("entryKey")) { mutableStateOf<String?>(null) }
    var page by remember(sample.text("entryKey")) { mutableStateOf(sample) }
    var cursor by remember(sample.text("entryKey")) {
        mutableStateOf(sample.text("buyExecutionIdsNextCursor"))
    }
    fun load(next: String?) {
        if (!online) return
        model.submitWithReceipt("get_trade_review", buildJsonObject {
            put("limit", 50)
            put("view", "samples")
            put("sampleEntryKey", sample.text("entryKey")!!)
            next?.let { put("sampleCursor", it) }
        }) { requestId = it }
    }
    val read = readState(state, requestId)
    LaunchedEffect(read) {
        if (read is ReadState.Ready) {
            read.result.objects("samples").singleOrNull()?.let {
                page = it
                cursor = it.text("buyExecutionIdsNextCursor")
            }
        }
    }
    AlertDialog(
        onDismissRequest = dismiss,
        title = { Text("买入证据") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("共 ${page.value("buyExecutionCount")} 笔，当前显示 ${page.array("buyExecutionIds")?.size ?: 0} 个。")
                for (id in page.array("buyExecutionIds").orEmpty()) {
                    SelectionContainer { Text((id as JsonPrimitive).content, style = androidx.compose.material3.MaterialTheme.typography.bodySmall) }
                }
                if (read is ReadState.Failed) {
                    Text("读取失败：${read.code ?: "传输未完成"}；请重新查询。", color = DeskColors.amber)
                }
            }
        },
        confirmButton = {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                if (cursor != null) {
                    TextButton(
                        onClick = { load(cursor) },
                        enabled = online,
                        modifier = Modifier.heightIn(min = 48.dp),
                    ) { Text("下一页买入证据") }
                }
                TextButton(onClick = dismiss, modifier = Modifier.heightIn(min = 48.dp)) { Text("关闭") }
            }
        },
    )
}

@Composable
internal fun TradeReviewPage(
    state: WorkbenchProjection,
    model: WorkbenchViewModel,
    busy: Boolean,
) {
    val online = state.connection.localOnline
    var requestId by rememberSaveable(state.pairId, state.epoch) { mutableStateOf<String?>(null) }
    var cursor by rememberSaveable(state.pairId, state.epoch) { mutableStateOf<String?>(null) }
    var view by rememberSaveable(state.pairId, state.epoch) { mutableStateOf("items") }
    fun reload(next: String? = cursor, selectedView: String = view) {
        if (!online) return
        model.submitWithReceipt("get_trade_review", buildJsonObject {
            put("limit", 50)
            put("view", selectedView)
            next?.let { put("cursor", it) }
        }) {
            requestId = it
            cursor = next
        }
    }
    LaunchedEffect(state.pairId, state.epoch, online, view) {
        if (online) reload(null, view)
    }
    val read = readState(state, requestId)
    val review = (read as? ReadState.Ready)?.result
    val nextCursor = review?.text("nextCursor")
    var notesRequestId by rememberSaveable(state.pairId, state.epoch) { mutableStateOf<String?>(null) }
    fun reloadNotes() {
        if (!online) return
        model.submitWithReceipt("get_trade_review_notes", buildJsonObject { put("limit", 50) }) {
            notesRequestId = it
        }
    }
    LaunchedEffect(state.pairId, state.epoch, online) {
        if (online && notesRequestId == null) reloadNotes()
    }
    val notesRead = readState(state, notesRequestId)
    // Keep the last notes read even if its intent is later pruned from the
    // bounded intent list by unrelated review paging.
    val notesResult = (notesRead as? ReadState.Ready)?.result
        ?: state.reads["get_trade_review_notes"]
    val notes = notesResult?.objects("items").orEmpty()
    SectionTitle("实际成交复盘")
    NoticeStrip(
        "按记录价差与已知费用分列；人工申报不是券商验真，也不替代持仓浮盈或账户净收益。",
        DeskColors.blue,
    )
    when {
        !online -> DesignCard { Text("连接工作台后可读取实际成交复盘。", color = DeskColors.muted) }
        read is ReadState.Failed -> DesignCard {
            Text(
                if (read.code == "TRADE_REVIEW_CURSOR_EXPIRED")
                    "复盘数据已变化，游标已过期；请重新查询第一页。"
                else "读取失败：${read.code ?: "传输未完成"}。",
                color = DeskColors.amber,
            )
            TextButton(
                onClick = { reload(null, view) },
                enabled = online && !busy,
                modifier = Modifier.heightIn(min = 48.dp),
            ) { Text("重新查询第一页") }
        }
        read == ReadState.Pending || (read == ReadState.Idle && online) -> DesignCard {
            Text("正在读取复盘…", color = DeskColors.muted)
        }
        review == null -> DesignCard { Text("复盘结果格式无法读取。", color = DeskColors.amber) }
        else -> {
            val summary = review.obj("summary")
            DesignCard {
                if (summary == null) {
                    Text("复盘结果格式无法读取。", color = DeskColors.amber)
                } else {
                    val closed = summary.number("closedTrades") ?: 0
                    val netKnown = summary.number("netKnownClosedTrades") ?: 0
                    Text("已闭合配对片段 $closed · 净额口径 $netKnown 笔 · 盈 ${summary.value("wins")} · 亏 ${summary.value("losses")} · 平 ${summary.value("flat")}")
                    Text(
                        "净额胜率 " + (summary.number("winRateBasisPoints")?.let { "%.2f%%".format(it / 100.0) }
                            ?: if (netKnown == 0L) "无已定样本，不计算" else "缺评估集") +
                            " · 未平批次 ${summary.value("openLots")} · 未平 ${summary.value("openQty")} 股",
                        color = DeskColors.muted,
                    )
                    Text(
                        "仅价差口径：盈 ${summary.value("priceWins")} · 亏 ${summary.value("priceLosses")} · " +
                            "平 ${summary.value("priceFlat")} · 未扣费用价差 ${summary.value("grossPnl")}",
                        color = DeskColors.muted,
                    )
                    Text("扣已知费用净额 ${summary.value("netPnl")}")
                    if (summary.flag("netPnlComplete") != true) {
                        Text("部分成交缺少费用，净额未知（${summary.value("missingFeeCount")} 笔），不以 0 补齐。", color = DeskColors.amber)
                    }
                    Text(
                        "已完全平仓入场组 ${summary.value("closedSamples")} 个 · 净额口径 " +
                            "${summary.value("sampleNetKnown")} 个 · 胜率 " +
                            (summary.number("sampleWinRateBasisPoints")?.let { "%.2f%%".format(it / 100.0) } ?: "不计算"),
                        color = DeskColors.muted,
                    )
                    Text("入场组毛额 ${summary.value("sampleGrossPnl")} · 入场组净额 ${summary.value("sampleNetPnl")}")
                    Text(
                        "按 ${review.obj("basis")?.text("pairing") ?: "fifo"} 配对 v${review.obj("basis")?.value("version")} · " +
                            "区间 [${timeText(review.obj("period")?.number("from"))}, ${timeText(review.obj("period")?.number("to"))}) " +
                            "· 排除 ${summary.value("excludedCount")} 笔 · 读取于 ${timeText(review.obj("basis")?.number("asOf"))}",
                        color = DeskColors.muted,
                    )
                }
            }
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                listOf(
                    "items" to "配对成交",
                    "samples" to "完全平仓样本",
                    "exclusions" to "未配对排除",
                ).forEach { (id, label) ->
                    FilterChip(view == id, {
                        view = id
                        cursor = null
                    }, label = { Text(label) }, modifier = Modifier.heightIn(min = 48.dp))
                }
            }
            // Keep paging next to the view switch. A long first page otherwise
            // buries the only "next page" control below every review card.
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedAction("重新查询第一页", enabled = online && !busy) { reload(null) }
                OutlinedAction("下一页", enabled = online && !busy && nextCursor != null) {
                    nextCursor?.let { reload(it) }
                }
            }
            if (cursor != null) {
                Text("当前为所选视图的游标续页；切换视图会重新查询第一页，摘要始终对应完整查询。", color = DeskColors.muted)
            }
            if (notes.isNotEmpty()) {
                DesignCard {
                    SectionTitle(
                        "人工复核记录",
                        "只记录人的判断，不改成交、不调参、不自动通知；共 ${notesResult?.number("manualCount") ?: notes.size} 条当前有效。",
                    )
                    for (note in notes) {
                        Text(
                            "${note.text("verdict") ?: "?"} · ${note.text("reviewer") ?: "?"} · " +
                                "${timeText(note.number("createdAt"))} · v${note.value("version")} · ${note.text("status")}",
                            color = DeskColors.muted,
                        )
                        Text(note.text("note").orEmpty())
                    }
                }
            }
            val items = review.objects("items")
            val samples = review.objects("samples")
            val exclusions = review.objects("exclusions")
            val visible = when (view) {
                "samples" -> samples.size
                "exclusions" -> exclusions.size
                else -> items.size
            }
            if (visible == 0) {
                DesignCard {
                    Text(
                        when (view) {
                            "samples" -> "本页没有完全平仓入场组。"
                            "exclusions" -> "本页没有未配对排除项。"
                            else -> "本页没有可闭合的实际成交配对。"
                        },
                        color = DeskColors.muted,
                    )
                }
            }
            if (view == "items") {
                for (item in items) {
                    DesignCard {
                        Text("${item.value("symbol")} · ${item.value("qty")} 股")
                        Text("${item.value("buyPrice")} → ${item.value("sellPrice")} 元 · 持有 ${item.value("holdingDays")} 天")
                        Text("记录价差 ${item.value("grossPnl")} · 净额 ${item.value("netPnl")}")
                        Text(
                            "买入费用 ${item.value("buyFee")} · 卖出费用 ${item.value("sellFee")} · " +
                                if (item.text("feeSource") == "user") "费用由用户申报" else "费用未知",
                            color = DeskColors.muted,
                        )
                        Text(
                            "成交 ${timeText(item.number("openedAt"))} → ${timeText(item.number("closedAt"))}",
                            color = DeskColors.muted,
                        )
                        var evidence by remember(item.text("buyExecutionId"), item.text("sellExecutionId")) {
                            mutableStateOf<Pair<String, String>?>(null)
                        }
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            TextButton(
                                onClick = { item.text("buyExecutionId")?.let { evidence = it to "买入" } },
                                enabled = item.text("buyExecutionId") != null,
                                modifier = Modifier.heightIn(min = 48.dp),
                            ) { Text("买入 ${item.text("buyExecutionId") ?: "不可读"}") }
                            TextButton(
                                onClick = { item.text("sellExecutionId")?.let { evidence = it to "卖出" } },
                                enabled = item.text("sellExecutionId") != null,
                                modifier = Modifier.heightIn(min = 48.dp),
                            ) { Text("卖出 ${item.text("sellExecutionId") ?: "不可读"}") }
                        }
                        Text(
                            item.text("taskId")?.let { "相关事项 $it" } ?: "无关联事项",
                            color = DeskColors.muted,
                        )
                        evidence?.let { (executionId, side) ->
                            ExecutionEvidencePrompt(
                                executionId, side, item, state,
                                dismiss = { evidence = null },
                            )
                        }
                        ReviewNoteEditor(
                            item, state, model,
                            onSaved = {
                                reloadNotes()
                            },
                        )
                    }
                }
            }
            if (view == "samples") {
                for (sample in samples) {
                    var showIds by remember(sample.text("entryKey")) { mutableStateOf(false) }
                    DesignCard {
                        Text(
                            "${sample.value("symbol")} · ${sample.value("qty")} 股 · 毛额 ${sample.value("grossPnl")} · " +
                                "净额 ${sample.value("netPnl")} · 最终平仓 ${timeText(sample.number("closedAt"))}",
                            color = DeskColors.muted,
                        )
                        Text(
                            "买入证据 ${sample.value("buyExecutionCount")} 笔 · 当前显示 " +
                                "${sample.array("buyExecutionIds")?.size ?: 0} 个 ID",
                            color = DeskColors.muted,
                        )
                        TextButton(
                            onClick = { showIds = true },
                            modifier = Modifier.heightIn(min = 48.dp),
                        ) { Text("逐个查看买入证据") }
                        if (showIds) {
                            SampleEvidencePrompt(sample, state, model, online) { showIds = false }
                        }
                    }
                }
            }
            if (view == "exclusions") {
                for (row in exclusions) {
                    DesignCard {
                        Text(
                            "${row.value("executionId")} · " + when (row.text("reason")) {
                                "superseded" -> "被纠错成交取代"
                                "unreconciled" -> "尚未完成持仓对账"
                                "missing_buy_lot" -> "没有可关联的买入批次"
                                else -> "未核实"
                            },
                            color = DeskColors.muted,
                        )
                        Text("当前作用域快照没有可展示的原成交字段；该排除项仅用于说明未计入配对。", color = DeskColors.muted)
                    }
                }
            }
        }
    }
}
