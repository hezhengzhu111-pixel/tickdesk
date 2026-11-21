package dev.tickdesk.app.ui

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import dev.tickdesk.core.*

@Composable
internal fun HoldingImportPanel(
    state: WorkbenchProjection, busy: Boolean, imageError: String?, retry: () -> Unit = {}, manage: (() -> Unit)? = null,
    cancelImport: ((String) -> Unit)? = null,
) {
    val workflow = state.holdingImport
    Column(Modifier.fillMaxWidth().semantics { liveRegion = LiveRegionMode.Polite }, verticalArrangement = Arrangement.spacedBy(6.dp)) {
        state.meta?.obj("visionNotice")?.let { notice ->
            Text("点击截图导入并选择图片，即授权本地服务将截图发往已配置的视觉服务识别并保存持仓。取消选图不提交。",
                style = MaterialTheme.typography.bodySmall, color = DeskColors.secondary)
            if (notice.number("consentedAt") == null) Text(notice.text("text").orEmpty(),
                style = MaterialTheme.typography.bodySmall, color = DeskColors.muted)
        }
        val label = when {
            imageError != null -> "内容不合法"
            else -> when (workflow?.status) {
                HoldingImportStatus.UPLOADING -> {
                    val transfer = state.transfers.firstOrNull { it.id == workflow.transferId }
                    "正在上传 · ${transfer?.completedChunks ?: 0}/${transfer?.totalChunks ?: "—"}"
                }
                HoldingImportStatus.AUTHORIZING -> "正在准备识别…"
                HoldingImportStatus.RECOGNIZING -> "正在识别…"
                HoldingImportStatus.SYNCING -> "正在刷新持仓…"
                HoldingImportStatus.SUCCESS -> "成功 · 持仓已更新"
                HoldingImportStatus.INVALID -> "内容不合法"
                HoldingImportStatus.FAILED -> "识别失败"
                null -> null
            }
        }
        label?.let { Text(it, style = MaterialTheme.typography.titleSmall,
            color = if (workflow?.status == HoldingImportStatus.SUCCESS) DeskColors.green else DeskColors.secondary) }
        if (imageError != null) {
            Text(imageError, style = MaterialTheme.typography.bodySmall, color = DeskColors.amber)
        } else if (workflow?.status in setOf(HoldingImportStatus.FAILED, HoldingImportStatus.INVALID)) {
            Text(holdingImportProblem(workflow?.errorCode), style = MaterialTheme.typography.bodySmall, color = DeskColors.amber)
            if (state.rows("imports").any { it.text("importId") == workflow?.importId && it.text("fileStatus") == "deleted" }) {
                Text("本次原图已删除。可重新选择截图。", style = MaterialTheme.typography.bodySmall, color = DeskColors.muted)
            }
        } else if (workflow?.status == HoldingImportStatus.SUCCESS) {
            HoldingScreenshotEvidence(state, workflow.importId)
        }
        if (imageError == null && workflow?.canRetry == true && workflow.status in setOf(HoldingImportStatus.FAILED, HoldingImportStatus.INVALID)) {
            Text("重新识别将复用此图，按当前告知授权视觉服务识别并保存持仓。", style = MaterialTheme.typography.bodySmall, color = DeskColors.muted)
            TextButton(onClick = retry, enabled = state.canQueue && !busy && state.meta?.obj("visionNotice")?.text("noticeVersion") != null,
                modifier = Modifier.heightIn(min = 48.dp)) { Text("重新识别此图") }
        }
        if (workflow?.terminal == false && !state.connection.localOnline)
            Text("等待连接工作台，连接后自动继续", style = MaterialTheme.typography.bodySmall, color = DeskColors.muted)
        if (cancelImport != null && workflow?.status == HoldingImportStatus.RECOGNIZING &&
            state.rows("imports").any { it.text("importId") == workflow.importId && it.text("status") == "pending" }) {
            var confirming by rememberSaveable(workflow.id) { mutableStateOf(false) }
            val cancellation = state.intents.lastOrNull {
                it.command == "cancel_import" && it.data.text("importId") == workflow.importId
            }
            val available = state.canQueue && state.connection.localOnline && !busy && cancellation?.stage !in pendingStages
            TextButton(onClick = { confirming = true }, modifier = Modifier.heightIn(min = 48.dp), enabled = available) {
                Text("取消本次识别")
            }
            if (cancellation != null) IntentText(cancellation)
            if (confirming) Confirm(
                "取消本次识别", "等待工作台确认。若持仓已保存，请在持仓台账中核对结果。", !available,
                dismiss = { confirming = false },
            ) {
                cancelImport(workflow.importId)
                confirming = false
            }
        }
        if (manage != null && workflow != null && workflow.status != HoldingImportStatus.SUCCESS) {
            TextButton(onClick = manage, modifier = Modifier.heightIn(min = 48.dp)) {
                Text("原图与识别授权")
            }
        }
    }
}

@Composable
private fun HoldingScreenshotEvidence(state: WorkbenchProjection, importId: String) {
    val snapshot = state.rows("imports").firstOrNull { it.text("importId") == importId } ?: return
    val rawRows = snapshot.objects("recognizedRows").associateBy { it.text("rowId") }
    val entries = snapshot.objects("rows").mapNotNull { position ->
        val raw = rawRows[position.text("rowId")]?.obj("raw") ?: return@mapNotNull null
        val values = listOf("lastPrice" to "截图价格", "marketValue" to "截图市值", "pnl" to "截图盈亏")
            .mapNotNull { (field, name) -> raw.text(field)?.takeIf(String::isNotBlank)?.let { "$name $it" } }
        if (values.isEmpty()) null else position.value("name") to values.joinToString(" · ")
    }
    if (entries.isEmpty()) return
    var expanded by rememberSaveable(importId) { mutableStateOf(false) }
    TextButton(onClick = { expanded = !expanded }, modifier = Modifier.heightIn(min = 48.dp)) {
        Text(if (expanded) "收起截图识别值" else "核对截图识别值")
    }
    if (expanded) DesignCard {
        Text("来源为本次截图，截图拍摄时点未知。工作台接收于 ${timeText(snapshot.number("createdAt"))}。",
            color = DeskColors.muted, style = MaterialTheme.typography.bodySmall)
        entries.forEach { (name, values) ->
            SectionTitle(name)
            Text(values, style = MaterialTheme.typography.bodySmall)
        }
    }
}

private fun holdingImportProblem(code: String?): String = when (code) {
    "IMPORT_CANCELLED" -> "工作台已取消本次识别。原图状态可在原图与识别授权中核对。"
    "IMPORT_CONTENT_INVALID", "IMPORT_ROWS_UNRESOLVED", "IMPORT_EMPTY_SNAPSHOT" ->
        "整批校验未通过。请检查截图清晰度、证券名称与代码、重复标的、数量和成本后重新选图。"
    "IMPORT_DIFF_STALE", "IMPORT_ASOF_STALE", "POSITION_VERSION_CONFLICT" ->
        "识别期间持仓已有更新，请核对当前台账后重新选择截图。"
    "VISION_DEADLINE_EXCEEDED", "PROVIDER_TIMEOUT", "AI_TIMEOUT" ->
        "视觉服务超时。连接恢复后可复用保留的原图。"
    "VISION_CONSENT_REQUIRED", "VISION_CONSENT_REVOKED", "VISION_NOTICE_CHANGED" ->
        "识别授权已失效或撤销。可在原图与识别授权中核对。"
    "FILE_TOO_LARGE", "IMAGE_TOO_LARGE" -> "截图超过允许的文件大小或像素数，请裁剪后重选。"
    "VISION_SYMBOLS_UNAVAILABLE" -> "暂时无法核验证券身份。可先手动录入后再检查数据服务。"
    "UPLOAD_FAILED", "TRANSFER_IDENTITY_CHANGED" -> "原图上传未完成。请检查连接和当前配对。"
    else -> "工作台未确认保存。可核对连接、原图与识别授权后处理。"
}
