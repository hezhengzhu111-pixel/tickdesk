@file:OptIn(androidx.compose.foundation.layout.ExperimentalLayoutApi::class)

package dev.tickdesk.app.ui

import android.graphics.Bitmap
import android.os.Bundle
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.relocation.BringIntoViewRequester
import androidx.compose.foundation.relocation.bringIntoViewRequester
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.Saver
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawWithCache
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.scale
import androidx.compose.ui.graphics.vector.PathParser
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.layout.onGloballyPositioned
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import dev.tickdesk.app.R
import dev.tickdesk.core.*
import kotlinx.coroutines.launch
import kotlinx.serialization.json.*
import java.time.Instant
import java.time.LocalDate
import java.time.format.DateTimeFormatter
import java.util.Locale
import java.util.UUID

internal const val ImportOcrTarget = "ocr"

@Composable
internal fun ImportPage(
    state: WorkbenchProjection,
    model: WorkbenchViewModel,
    busy: Boolean,
    footer: ScreenFooter? = null,
    back: () -> Unit = {},
    manual: (() -> Unit)? = null,
    portfolio: () -> Unit = back,
    focusOcr: Boolean = false,
    drafts: ImportDrafts = rememberImportDrafts(state),
) {
    val transferScope = state.fileTransferScope()
    val pickedImage by model.selectedImage.collectAsStateWithLifecycle()
    var pickerPair by rememberSaveable { mutableStateOf<String?>(null) }
    var pickerEpoch by rememberSaveable { mutableStateOf<String?>(null) }
    val picker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) {
        val pair = pickerPair
        val epoch = pickerEpoch
        pickerPair = null
        pickerEpoch = null
        if (it != null && pair != null && epoch != null) model.pickImage(it, FileTransferScope(pair, epoch))
    }
    val canPick = transferScope != null && !busy
    val pick: () -> Unit = {
        if (transferScope != null && !busy && pickerPair == null) {
            pickerPair = transferScope.pairId
            pickerEpoch = transferScope.epoch
            picker.launch(arrayOf("image/png", "image/jpeg"))
        }
    }
    val batches = state.rows("imports").sortedByDescending { it.number("createdAt") }
    val record = batches.firstOrNull { it.text("importId") == drafts.selectedBatch }
        ?: batches.firstOrNull { it.text("status") == "recognized" }
        ?: batches.firstOrNull()
    LaunchedEffect(record?.text("importId")) {
        record?.text("importId")?.let { drafts.selectedBatch = it }
    }
    LaunchedEffect(batches) {
        batches.filter { it.text("status") in setOf("confirmed", "rejected", "failed", "cancelled") }
            .mapNotNull { it.text("importId") }
            .forEach(drafts::discard)
    }
    val reviewed = record?.takeIf { it.text("status") == "recognized" }?.let { batch ->
        val id = batch.text("importId")
        val candidate = state.intents.lastOrNull {
            it.command == "apply_import" && it.data.text("phase") == "preview" && it.data.text("importId") == id &&
                it.data.text("serverEpoch") == state.epoch && it.stage != DeliveryStage.QUARANTINED &&
                it.result?.number("importVersion") != null
        }?.result
        candidate?.takeIf { (it.number("importVersion") ?: 0L) >= (batch.number("importVersion") ?: 0L) }
    }
    val batch = reviewed ?: record
    val requester = remember(batch?.text("importId"), batch?.text("status")) { BringIntoViewRequester() }
    val coroutine = rememberCoroutineScope()
    var placed by remember(requester) { mutableStateOf(false) }
    val ocrAnchor = if (focusOcr || batch?.text("status") == "recognized") {
        LaunchedEffect(requester, placed) {
            if (focusOcr && placed) requester.bringIntoView()
        }
        Modifier.bringIntoViewRequester(requester).onGloballyPositioned { placed = true }
    } else Modifier
    val notice = state.meta?.obj("visionNotice")
    val consented = notice?.number("consentedAt") != null
    val consentIntent = state.intents.lastOrNull {
        it.command == "accept_vision_notice" && it.data.text("noticeVersion") == notice?.text("noticeVersion") &&
            it.data.text("serverEpoch") == state.epoch
    }
    val consentPending = consentIntent?.stage in pendingStages
    val canRecognize = consented && !consentPending

    VisionNoticeCard(state, model, notice, consentIntent, busy, back, manual)
    if (batch?.text("status") == "recognized") {
        TextButton(
            onClick = { coroutine.launch { requester.bringIntoView() } },
            enabled = placed,
            modifier = Modifier.heightIn(min = 48.dp),
        ) { Text("跳到识别结果") }
    }
    RetainedOriginals(state, model, batches, batch, busy, canRecognize) { hasArchive ->
        ImageEntry(state, pickedImage?.takeIf { it.scope == transferScope }, model, busy, canPick, pick, hasArchive)
    }

    if (batches.size > 1) {
        var pickingBatch by remember { mutableStateOf(false) }
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text("切换识别批次", Modifier.weight(1f), style = MaterialTheme.typography.titleSmall)
            Box {
                TextButton(onClick = { pickingBatch = true }) {
                    Text(batch?.text("importId").orEmpty(), maxLines = 1, overflow = TextOverflow.Ellipsis)
                }
                DropdownMenu(expanded = pickingBatch, onDismissRequest = { pickingBatch = false }) {
                    batches.forEach { item ->
                        DropdownMenuItem(text = { Text("${item.value("importId")} · ${batchStatus(item)}") }, onClick = {
                            drafts.selectedBatch = item.text("importId")
                            pickingBatch = false
                        })
                    }
                }
            }
        }
    }
    if (batch != null) {
        key(state.pairId, state.epoch, batch.text("importId")) {
            ImportReview(state, model, batch, busy, canRecognize, footer, portfolio, ocrAnchor, drafts)
        }
    } else {
        DesignCard(ocrAnchor) {
            SectionTitle("OCR 识别结果")
            val register = state.intents.lastOrNull { it.command == "register_image" && it.data.text("serverEpoch") == state.epoch }
            if (register == null) ImportCaption("收到工作台的识别结果后，可逐行核对并生成全量快照差异。")
            else IntentText(register)
        }
    }
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        OutlinedButton(
            onClick = pick, enabled = canPick, modifier = Modifier.weight(1f).heightIn(min = 48.dp),
            shape = RoundedCornerShape(13.dp), border = BorderStroke(1.dp, DeskColors.borderStrong),
            colors = ButtonDefaults.outlinedButtonColors(
                containerColor = Color.White.copy(alpha = 0.03f), contentColor = DeskColors.secondary,
                disabledContentColor = DeskColors.faint,
            ),
            contentPadding = PaddingValues(horizontal = 8.dp, vertical = 8.dp),
        ) {
            Text("重新选择截图")
        }
        if (batch?.text("status") != "confirmed") {
            OutlinedButton(
                onClick = portfolio, modifier = Modifier.weight(1f).heightIn(min = 48.dp),
                shape = RoundedCornerShape(13.dp), border = BorderStroke(1.dp, DeskColors.borderStrong),
                colors = ButtonDefaults.outlinedButtonColors(containerColor = DeskColors.navy, contentColor = DeskColors.text),
                contentPadding = PaddingValues(horizontal = 8.dp, vertical = 8.dp),
            ) {
                Text("返回持仓")
            }
        }
    }
}

@Composable
private fun VisionNoticeCard(
    state: WorkbenchProjection,
    model: WorkbenchViewModel,
    notice: JsonObject?,
    intent: IntentView?,
    busy: Boolean,
    back: () -> Unit,
    manual: (() -> Unit)?,
) {
    val version = notice?.text("noticeVersion")
    val checked = notice?.number("consentedAt") != null
    val pending = intent?.stage in pendingStages
    var expanded by rememberSaveable { mutableStateOf(false) }
    var consent by rememberSaveable(state.pairId, version) { mutableStateOf<Boolean?>(null) }
    val enabled = version != null && state.canQueue && !busy && !pending
    Surface(
        Modifier.fillMaxWidth(), shape = RoundedCornerShape(18.dp),
        color = Color(0xFF0F1623), border = BorderStroke(1.dp, DeskColors.blue.copy(alpha = 0.28f)),
    ) {
        Column(
            Modifier.drawWithCache {
                val base = Brush.linearGradient(listOf(Color(0xFF162032), Color(0xFF0F1623)))
                val glow = Brush.radialGradient(
                    listOf(DeskColors.blue.copy(alpha = 0.16f), Color.Transparent),
                    center = Offset(size.width * 0.92f, size.height * 0.10f),
                    radius = (size.width * 0.62f).coerceAtLeast(1f),
                )
                onDrawBehind {
                    drawRect(base)
                    drawRect(glow)
                }
            }.padding(15.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                ImportGlyph(ImportIcon.Shield, DeskColors.blue)
                Text("原图保留与外部识别", Modifier.semantics { heading() }, style = MaterialTheme.typography.titleSmall,
                    color = Color(0xFFDBEAFE), fontWeight = FontWeight.Bold)
            }
            Text(
                "原图保存在你的本地服务文件库，默认保留，可手动删除。云端只转发传输帧，不保存文件。" +
                    "启用识别后，本地服务会将图片发送到配置的外部视觉服务，外部服务的保留政策见其条款。",
                style = MaterialTheme.typography.bodySmall,
                color = Color(0xFFB6C8E2),
            )
            TextButton(onClick = { expanded = !expanded }, modifier = Modifier.heightIn(min = 48.dp),
                contentPadding = PaddingValues(0.dp)) {
                Text(if (expanded) "⌄ 收起安全边界与手动录入" else "› 查看安全边界与手动录入")
            }
            if (expanded) {
                ImportCaption(
                    "识别完成后原图仍保留。手动删除仅作用于本地服务文件，相册原图及已确认台账保留，" +
                        "第三方副本按服务方政策处理。旧备份默认保留 14 天，恢复时缺少最新删除记录的原图会隔离待核对。"
                )
                TextButton(
                    onClick = manual ?: back, enabled = manual == null || state.meta?.number("positionRevision") != null,
                    modifier = Modifier.heightIn(min = 48.dp), contentPadding = PaddingValues(0.dp),
                ) { Text(if (manual == null) "去持仓手动更正 →" else "改为手动录入 →") }
            }
            notice?.text("text")?.takeIf { it.isNotBlank() }?.let { ImportCaption(it) }
            HorizontalDivider(color = Color.White.copy(alpha = 0.09f))
            ImportCheck("我同意通过外部视觉服务识别截图", checked, enabled) { consent = it }
            when {
                pending && intent != null -> IntentText(intent)
                version == null -> ImportCaption("连接工作台并同步后可确认识别告知。")
                intent?.stage == DeliveryStage.REJECTED -> IntentText(intent)
            }
        }
    }
    consent?.let { next ->
        Confirm(
            if (next) "同意外部识别" else "撤销外部识别同意",
            if (next) "同意后可主动提交图片识别。原图继续保存在本地服务。"
            else "收到业务确认后将停用新的外部识别并取消尚未外发的任务。第三方已收到的数据按服务方条款处理。",
            !enabled,
            { consent = null },
        ) {
            if (version != null) {
                model.submit("accept_vision_notice", buildJsonObject {
                    put("noticeVersion", version)
                    put("accepted", next)
                }) { consent = null }
            }
        }
    }
}

@Composable
private fun ImageEntry(
    state: WorkbenchProjection,
    local: SelectedImage?,
    model: WorkbenchViewModel,
    busy: Boolean,
    canPick: Boolean,
    pick: () -> Unit,
    hasArchive: Boolean,
) {
    val scope = state.fileTransferScope()
    val uploads = state.transfers.filter { it.kind == FileTransferKind.UPLOAD }
    val outstanding = uploads.filter { it.status in activeTransfers || it.canCancel }
    val shownUploads = outstanding.ifEmpty {
        listOfNotNull(uploads.lastOrNull()?.takeUnless { hasArchive && it.status == FileTransferStatus.COMPLETE })
    }
    HorizontalDivider(color = DeskColors.border)
    if (hasArchive && local == null && shownUploads.isEmpty()) {
        TextButton(onClick = pick, enabled = canPick, modifier = Modifier.heightIn(min = 48.dp),
            contentPadding = PaddingValues(0.dp)) { Text("选择图片") }
    } else {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("选择截图", Modifier.weight(1f).semantics { heading() }, style = MaterialTheme.typography.titleSmall)
            OutlinedButton(
                onClick = pick, enabled = canPick, modifier = Modifier.heightIn(min = 48.dp),
                shape = RoundedCornerShape(11.dp), contentPadding = PaddingValues(horizontal = 14.dp, vertical = 8.dp),
            ) {
                Text(if (local == null) "选择图片" else "重新选择")
            }
        }
        if (scope == null) {
            ImportCaption(if (state.connection.recoveryRequired) "完成恢复核对后可传输原图。" else "同步可信工作台后可保存截图。")
        } else if (local == null) {
            ImportCaption("JPEG 或 PNG · 最大 20 MiB、4000 万像素")
        } else {
            OriginalThumbnail(local.preview, "本机选择的截图预览")
            ImportCaption("${local.name} · ${fileSize(local.size.toLong())}")
            val alreadyUploading = outstanding.any { it.sha256 == local.sha256 }
            if (alreadyUploading) ImportCaption("此截图已有上传任务。")
            MainAction("保存原图", !busy && !alreadyUploading) { model.saveOriginal() }
            TextButton(onClick = model::clearImage, enabled = !busy, modifier = Modifier.heightIn(min = 48.dp),
                contentPadding = PaddingValues(0.dp)) { Text("移除本次选择") }
        }
        shownUploads.forEach { upload ->
            HorizontalDivider(color = DeskColors.border)
            TransferProgress(upload, state, model, busy)
            if (local == null && upload.status in restartableTransfers && !upload.canCancel) {
                TextButton(onClick = pick, enabled = canPick, modifier = Modifier.heightIn(min = 48.dp),
                    contentPadding = PaddingValues(0.dp)) { Text("重新选择截图上传") }
            }
        }
    }
}

@Composable
private fun OriginalThumbnail(preview: Bitmap, description: String) {
    Surface(color = DeskColors.field, shape = RoundedCornerShape(10.dp)) {
        Image(preview.asImageBitmap(), contentDescription = description,
            modifier = Modifier.fillMaxWidth().heightIn(min = 72.dp, max = 180.dp).padding(4.dp),
            contentScale = ContentScale.Fit)
    }
}

private val activeTransfers = setOf(FileTransferStatus.PREPARING, FileTransferStatus.TRANSFERRING,
    FileTransferStatus.RESUMING, FileTransferStatus.FINALIZING, FileTransferStatus.CANCELLING)

private val restartableTransfers = setOf(FileTransferStatus.EXPIRED, FileTransferStatus.CANCELLED, FileTransferStatus.FAILED)

@Composable
private fun TransferProgress(transfer: FileTransferView, state: WorkbenchProjection, model: WorkbenchViewModel, busy: Boolean) {
    val upload = transfer.kind == FileTransferKind.UPLOAD
    val direction = if (upload) "上传" else "下载"
    val status = when (transfer.status) {
        FileTransferStatus.PREPARING -> if (state.connection.localOnline) "准备${direction}原图" else "${direction}待连接"
        FileTransferStatus.TRANSFERRING -> if (state.connection.localOnline) "${direction}中" else "${direction}等待重连"
        FileTransferStatus.RESUMING -> "等待续传"
        FileTransferStatus.FINALIZING -> if (upload) "等待工作台确认保存" else "校验下载内容"
        FileTransferStatus.CANCELLING -> "等待确认取消${direction}"
        FileTransferStatus.COMPLETE -> if (upload) "原图已保存" else "下载已校验"
        FileTransferStatus.CANCELLED -> "${direction}已取消"
        FileTransferStatus.EXPIRED -> "${direction}已过期"
        FileTransferStatus.FAILED -> "${direction}失败"
        FileTransferStatus.QUARANTINED -> "${direction}已隔离 · 待核对"
    }
    val tint = when (transfer.status) {
        FileTransferStatus.COMPLETE -> DeskColors.green
        FileTransferStatus.FAILED, FileTransferStatus.EXPIRED, FileTransferStatus.QUARANTINED -> DeskColors.amber
        else -> DeskColors.muted
    }
    val detail = listOfNotNull(transfer.name, status, transfer.errorCode).joinToString(" · ")
    ImportCaption(detail, tint)
    val total = transfer.totalChunks
    if (transfer.status in activeTransfers && total != null && total > 0) {
        val done = transfer.completedChunks.coerceIn(0, total)
        LinearProgressIndicator(progress = { done.toFloat() / total }, modifier = Modifier.fillMaxWidth())
        ImportCaption("已确认 $done / $total 块")
    }
    val scope = state.fileTransferScope()
    if (transfer.canCancel && transfer.status != FileTransferStatus.CANCELLING && scope != null) {
        TextButton(onClick = { model.cancelTransfer(transfer.id, scope) }, enabled = !busy,
            modifier = Modifier.heightIn(min = 48.dp), contentPadding = PaddingValues(0.dp)) { Text("取消$direction") }
    }
}

@Composable
private fun RetainedOriginals(
    state: WorkbenchProjection,
    model: WorkbenchViewModel,
    batches: List<JsonObject>,
    batch: JsonObject?,
    busy: Boolean,
    consented: Boolean,
    uploadEntry: @Composable (Boolean) -> Unit,
) {
    val read = state.reads["list_files"]
    val listed = read?.objects("items").orEmpty()
    val savedUpload = state.transfers.lastOrNull {
        it.kind == FileTransferKind.UPLOAD && it.status == FileTransferStatus.COMPLETE && it.file != null
    }
    val uploaded = savedUpload?.file
    val uploadedId = uploaded?.text("fileId")
    val files = if (uploaded != null && listed.none { it.text("fileId") == uploadedId }) listOf(uploaded) + listed else listed
    val currentFile = batch?.text("fileId")
    var selected by rememberSaveable(state.pairId, state.epoch, currentFile) { mutableStateOf<String?>(null) }
    var selectedUpload by rememberSaveable(state.pairId, state.epoch) { mutableStateOf<String?>(null) }
    LaunchedEffect(savedUpload?.id) {
        if (savedUpload != null && uploadedId != null && selectedUpload != savedUpload.id) {
            selected = uploadedId
            selectedUpload = savedUpload.id
        }
    }
    var menu by remember(state.pairId, state.epoch) { mutableStateOf(false) }
    val file = files.firstOrNull { it.text("fileId") == selected }
        ?: files.firstOrNull { it.text("fileId") == currentFile }
        ?: files.firstOrNull()
    val fileId = file?.text("fileId")
    val deletions = if (fileId == null) emptyList() else state.intents.filter {
        it.command == "delete_file" && it.data.text("fileId") == fileId && it.data.text("serverEpoch") == state.epoch
    }
    val deletion = deletions.lastOrNull()
    val deleted = deletions.filter { it.stage.businessSucceeded }.mapNotNull { it.result?.obj("file") }
        .maxByOrNull { it.number("version") ?: -1L }
    val displayed = listOfNotNull(file, uploaded?.takeIf { uploadedId == fileId }, deleted).maxWithOrNull(
        compareBy<JsonObject> { it.number("version") ?: -1L }.thenBy { it.text("status") != "available" },
    )
    val otherDownloads = state.transfers.filter {
        it.kind == FileTransferKind.DOWNLOAD && it.fileId != fileId && (it.status in activeTransfers || it.canCancel)
    }
    DesignCard {
        FlowRow(
            Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            Text("工作台原图归档", Modifier.semantics { heading() }, style = MaterialTheme.typography.titleSmall,
                fontWeight = FontWeight.Bold)
            if (displayed != null && state.connection.recoveryRequired) {
                StatusBadge("恢复待核对", DeskColors.amber)
            } else if (displayed != null) {
                val status = displayed.text("status")
                StatusBadge(fileStatus(status), if (status == "available") DeskColors.blue else DeskColors.faint)
            }
        }
        if (displayed == null) {
            ImportCaption(if (read == null) "刷新后可查看工作台原图与保留状态。" else "本页没有原图。")
        } else {
            key(state.pairId, state.epoch, fileId, displayed.number("version"), displayed.text("status"), displayed.text("sha256")) {
                OriginalActions(state, model, displayed, deletion, batches, consented, busy)
            }
        }
        FlowRow(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            TextButton(
                onClick = { model.submit("list_files") },
                enabled = state.connection.localOnline && !busy && !state.listRequestPending(),
                modifier = Modifier.heightIn(min = 48.dp),
                contentPadding = PaddingValues(0.dp),
            ) { Text("刷新文件列表") }
            if (files.size > 1) {
                Box {
                    TextButton(onClick = { menu = true }, modifier = Modifier.heightIn(min = 48.dp)) { Text("切换原图") }
                    DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
                        files.forEach { item ->
                            DropdownMenuItem(text = { Text(item.value("name")) }, onClick = {
                                selected = item.text("fileId")
                                menu = false
                            })
                        }
                    }
                }
            }
        }
        read?.text("nextCursor")?.let { cursor ->
            TextButton(
                onClick = { model.submit("list_files", buildJsonObject { put("cursor", cursor) }) },
                enabled = state.connection.localOnline && !busy && !state.listRequestPending(),
                modifier = Modifier.heightIn(min = 48.dp),
                contentPadding = PaddingValues(0.dp),
            ) { Text("读取下一页原图") }
        }
        otherDownloads.forEach { download ->
            HorizontalDivider(color = DeskColors.border)
            TransferProgress(download, state, model, busy)
        }
        uploadEntry(displayed != null)
    }
}

private fun WorkbenchProjection.listRequestPending() =
    intents.any { it.command == "list_files" && it.stage in pendingStages }

@Composable
private fun OriginalActions(
    state: WorkbenchProjection,
    model: WorkbenchViewModel,
    file: JsonObject,
    deletion: IntentView?,
    batches: List<JsonObject>,
    consented: Boolean,
    busy: Boolean,
) {
    val id = file.text("fileId") ?: return
    val version = file.number("version") ?: return
    var metadata by rememberSaveable { mutableStateOf(false) }
    var delete by rememberSaveable(version) { mutableStateOf(false) }
    val associated = batches.filter { it.text("fileId") == id }
    val register = state.intents.lastOrNull {
        it.command == "register_image" && it.data.text("fileId") == id && it.data.text("serverEpoch") == state.epoch
    }
    val available = file.text("status") == "available"
    val canDelete = available && state.canQueue && !busy && deletion?.stage !in pendingStages
    ImportCaption("${file.value("name")} · ${fileSize(file.number("sizeBytes"))}")
    associated.firstOrNull()?.let { ImportCaption("识别批次 ${it.value("importId")}") }
    TextButton(onClick = { metadata = !metadata }, modifier = Modifier.heightIn(min = 48.dp),
        contentPadding = PaddingValues(0.dp)) {
        Text(if (metadata) "收起原图校验信息" else "查看原图校验信息")
    }
    if (metadata) {
        ImportCaption("文件标识 ${id}\n保存时间 ${timeText(file.number("createdAt"))}\nSHA-256 ${file.value("sha256")}")
        if (associated.size > 1) ImportCaption("相关批次 ${associated.joinToString("、") { it.value("importId") }}")
    }
    DownloadOriginal(state, model, file, deletion?.stage in pendingStages, busy)
    if (available) {
        TextButton(
            onClick = {
                model.submit("register_image", buildJsonObject {
                    put("fileId", id)
                    put("importId", UUID.randomUUID().toString())
                })
            },
            enabled = consented && state.canQueue && !busy && register?.stage !in pendingStages && deletion?.stage !in pendingStages,
            modifier = Modifier.heightIn(min = 48.dp),
            contentPadding = PaddingValues(0.dp),
        ) { Text("识别此原图") }
        OutlinedButton(onClick = { delete = true }, enabled = canDelete, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp),
            shape = RoundedCornerShape(13.dp),
            border = BorderStroke(1.dp, DeskColors.red.copy(alpha = if (canDelete) 0.48f else 0.18f)),
            colors = ButtonDefaults.outlinedButtonColors(
                containerColor = DeskColors.red.copy(alpha = 0.10f), contentColor = DeskColors.redBright,
                disabledContentColor = DeskColors.faint,
            )) {
            Text("删除本地服务原图")
        }
    }
    register?.takeIf { it.stage in pendingStages || it.stage == DeliveryStage.REJECTED }?.let { IntentText(it) }
    deletion?.let {
        if (it.errorCode == "FILE_BUSY") {
            NoticeStrip("原图仍被识别或下载占用。取消对应任务或等待完成后，再删除原图。", DeskColors.amber)
        } else IntentText(it)
    }
    if (delete && available) {
        Confirm(
            "确认删除这张原图",
            "${file.value("name")}（${fileSize(file.number("sizeBytes"))}）" +
                associated.firstOrNull()?.let { "\n识别批次 ${it.value("importId")}" }.orEmpty() +
                "\n删除无法撤销。已确认台账与相册原图保留，旧备份默认保留 14 天。" +
                "文件仍被识别或传输占用时，可取消对应任务后再发起删除。",
            !canDelete,
            { delete = false },
        ) {
            model.submit("delete_file", buildJsonObject {
                put("fileId", id)
                put("baseVersion", version)
                put("confirm", true)
            }) { delete = false }
        }
    }
}

@Composable
private fun DownloadOriginal(
    state: WorkbenchProjection,
    model: WorkbenchViewModel,
    file: JsonObject,
    deleting: Boolean,
    busy: Boolean,
) {
    val fileId = file.text("fileId") ?: return
    val scope = state.fileTransferScope()
    var opened by rememberSaveable { mutableStateOf<String?>(null) }
    val downloads = state.transfers.filter { it.kind == FileTransferKind.DOWNLOAD && it.fileId == fileId }
    val download = downloads.lastOrNull { it.status in activeTransfers || it.canCancel }
        ?: downloads.lastOrNull { it.id == opened }
        ?: downloads.lastOrNull()
    val available = file.text("status") == "available" && !deleting
    val working = download != null && (download.status in activeTransfers || download.canCancel)
    val verified = download?.takeIf {
        it.status == FileTransferStatus.COMPLETE && it.fileVersion == file.number("version") &&
            it.sha256 != null && it.sha256 == file.text("sha256") && it.sizeBytes == file.number("sizeBytes") &&
            it.mime == file.text("mime")
    }
    if (download != null && (working || download.status != FileTransferStatus.COMPLETE || verified != null)) {
        TransferProgress(download, state, model, busy)
    }
    if (!available || scope == null) return
    val start = { model.startDownload(fileId, scope) { opened = it } }
    if (verified != null) {
        TextButton(onClick = { opened = if (opened == verified.id) null else verified.id }, enabled = !busy,
            modifier = Modifier.heightIn(min = 48.dp), contentPadding = PaddingValues(0.dp)) {
            Text(if (opened == verified.id) "收起原图预览" else "查看已下载原图")
        }
        if (opened == verified.id) {
            key(verified.id) { DownloadedPreview(verified, model, scope, !busy, start) }
        }
    } else if (!working) {
        TextButton(onClick = start, enabled = !busy, modifier = Modifier.heightIn(min = 48.dp),
            contentPadding = PaddingValues(0.dp)) { Text(if (download == null) "下载原图" else "重新下载原图") }
    }
}

@Composable
private fun DownloadedPreview(
    download: FileTransferView,
    model: WorkbenchViewModel,
    scope: FileTransferScope,
    enabled: Boolean,
    restart: () -> Unit,
) {
    var preview by remember { mutableStateOf<Bitmap?>(null) }
    var loading by remember { mutableStateOf(true) }
    LaunchedEffect(download.id, scope) {
        preview = model.readDownloadedPreview(download, scope)
        loading = false
    }
    val image = preview
    when {
        image != null -> OriginalThumbnail(image, "已下载的工作台原图预览")
        loading -> ImportCaption("正在读取已校验的原图")
        else -> {
            ImportCaption("本机无法读取原图预览。", DeskColors.amber)
            TextButton(onClick = restart, enabled = enabled, modifier = Modifier.heightIn(min = 48.dp),
                contentPadding = PaddingValues(0.dp)) { Text("重新下载原图") }
        }
    }
}

private fun fileStatus(status: String?) = when (status) {
    "available" -> "已保留"
    "deleting" -> "待删除"
    "deleted" -> "已删除"
    "missing" -> "原图缺失"
    else -> "状态未知"
}

private fun fileSize(bytes: Long?): String = when {
    bytes == null -> "大小未知"
    bytes >= 1_048_576 -> String.format(Locale.ROOT, "%.1f MiB", bytes / 1_048_576.0)
    bytes >= 1024 -> String.format(Locale.ROOT, "%.1f KiB", bytes / 1024.0)
    else -> "$bytes B"
}

@Composable
private fun ImportReview(
    state: WorkbenchProjection,
    model: WorkbenchViewModel,
    batch: JsonObject,
    busy: Boolean,
    consented: Boolean,
    footer: ScreenFooter?,
    portfolio: () -> Unit,
    modifier: Modifier,
    drafts: ImportDrafts,
) {
    val id = batch.text("importId") ?: return
    val revision = batch.number("importVersion") ?: return
    val status = batch.text("status")
    val intent = state.intents.lastOrNull {
        it.command in setOf("register_image", "apply_import", "cancel_import") && it.data.text("importId") == id &&
            it.data.text("serverEpoch") == state.epoch
    }
    val pending = intent?.command != "register_image" && intent?.stage in pendingStages
    val canSubmit = state.canQueue && !busy && !pending
    var cancel by rememberSaveable { mutableStateOf(false) }
    DesignCard(if (status == "recognized") Modifier else modifier) {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("识别批次", Modifier.weight(1f), style = MaterialTheme.typography.titleSmall)
            val tint = when (status) {
                "confirmed" -> DeskColors.green
                "failed" -> DeskColors.red
                "recognized" -> DeskColors.amber
                else -> DeskColors.blue
            }
            StatusBadge(batchStatus(batch), tint)
        }
        ImportCaption("$id · 版本 $revision")
        ImportCaption("原图 ${fileStatus(batch.text("fileStatus"))}")
        batch.text("errorCode")?.let { NoticeStrip("工作台错误 · $it", DeskColors.red) }
        intent?.takeIf { it.stage in pendingStages || it.stage == DeliveryStage.REJECTED }?.let { IntentText(it) }
        if (status in setOf("pending", "recognized")) {
            TextButton(onClick = { cancel = true }, enabled = canSubmit, modifier = Modifier.heightIn(min = 48.dp),
                contentPadding = PaddingValues(0.dp)) {
                Text("取消导入")
            }
        }
    }
    if (cancel && status in setOf("pending", "recognized")) {
        Confirm("取消导入", "提交取消请求。原图仍按当前保留状态显示。", !canSubmit, { cancel = false }) {
            model.submit("cancel_import", buildJsonObject { put("importId", id) }) { cancel = false }
        }
    }
    if (status == "recognized") {
        key(revision) {
            val draft = remember(drafts, id, revision) { drafts.editor(id, revision) }
            ImportEditor(state, model, batch, canSubmit && consented, busy || pending, footer, modifier, draft)
        }
    } else if (status == "confirmed") {
        NoticeStrip("工作台已确认对账 · ${timeText(batch.number("updatedAt"))}", DeskColors.green)
        batch.obj("diff")?.let { diff ->
            SectionTitle("已确认台账差异")
            val removals = diff.objects("missing").filter { it.text("action") == "delete" }.mapNotNull { it.text("symbol") }
            ImportDiff(batch, diff, removals, enabled = false, setRemove = null)
        }
        ImportDock(footer, "本批差异已写入工作台台账", "返回持仓查看更新", "返回持仓", true, portfolio)
    }
}

private fun batchStatus(batch: JsonObject) = when (batch.text("status")) {
    "pending" -> "识别中"
    "recognized" -> "已识别 · 待核对"
    "confirmed" -> "台账已确认"
    "rejected" -> "已放弃"
    "failed" -> "识别失败"
    "cancelled" -> "已取消"
    else -> "状态未知"
}

@Composable
private fun ImportEditor(
    state: WorkbenchProjection,
    model: WorkbenchViewModel,
    batch: JsonObject,
    canSubmit: Boolean,
    locked: Boolean,
    footer: ScreenFooter?,
    modifier: Modifier,
    draft: ImportEditorDraft,
) {
    val id = batch.text("importId") ?: return
    val revision = batch.number("importVersion") ?: return
    val hash = batch.text("diffHash")
    val diff = batch.obj("diff")
    val missing = diff?.objects("missing").orEmpty()
    val originalAsOf = batch.number("snapshotAsOf")
    val originalTime = originalAsOf?.let { Instant.ofEpochMilli(it).atZone(shanghai) }
    val defaultDate = remember(originalTime) { originalTime?.toLocalDate()?.toString() ?: LocalDate.now(shanghai).toString() }
    val defaultTime = originalTime?.format(DateTimeFormatter.ISO_LOCAL_TIME).orEmpty()
    val date = draft.date ?: defaultDate
    val time = draft.time ?: defaultTime
    val asOf = dateTime(date, time)
    var confirm by rememberSaveable(hash) { mutableStateOf(false) }
    val corrections = draft.corrections
    val actions = draft.removals
    val recognized = batch.objects("recognizedRows").associateBy { it.text("rowId") }
    val committed = batch.objects("rows").associateBy { it.text("rowId") }
    val excluded = batch.objects("excludedRows").associateBy { it.text("rowId") }
    val defaults = (recognized.keys + committed.keys).filterNotNull().associateWith { rowId ->
        val raw = recognized[rowId]?.obj("raw")
        val row = committed[rowId]
        DraftImportRow(
            symbol = row?.text("symbol") ?: raw?.text("symbol").orEmpty(),
            name = row?.text("name") ?: raw?.text("name").orEmpty(),
            qty = row?.number("qty")?.toString() ?: raw?.text("qty").orEmpty(),
            available = if (row != null) row.number("availableQty")?.toString().orEmpty() else raw?.text("availableQty").orEmpty(),
            cost = row?.text("cost") ?: raw?.text("cost").orEmpty(),
            excluded = rowId in excluded,
            reason = excluded[rowId]?.text("reason").orEmpty(),
        )
    }
    val edited = defaults + corrections
    val candidates = edited.filterValues { !it.excluded }
    val symbols = candidates.values.map { it.normalizedSymbol }
    val removals = missing.mapNotNull { row ->
        row.text("symbol")?.takeIf { actions[it] ?: (row.text("action") == "delete") }
    }
    val positionRevision = state.meta?.number("positionRevision")
    val stale = positionRevision != null && batch.number("basePositionRevision") != positionRevision
    val modified = edited != defaults || asOf != originalAsOf || missing.any { row ->
        actions[row.text("symbol")]?.let { it != (row.text("action") == "delete") } == true
    }
    val warnings = batch.objects("warnings")
    val reviewCount = recognized.values.count { it.objects("issues").isNotEmpty() }
    val valid = asOf != null && candidates.isNotEmpty() && edited.values.all { it.valid() } &&
        symbols.distinct().size == symbols.size
    val ready = canSubmit && valid && hash != null && diff != null && !modified && !stale && batch.flag("readyForConfirm") == true

    Column(modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        FlowRow(
            Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween,
            verticalArrangement = Arrangement.spacedBy(5.dp),
        ) {
            Text("OCR 识别结果 · 可逐行修正", Modifier.semantics { heading() }, style = MaterialTheme.typography.titleSmall)
            if (reviewCount > 0) StatusBadge("$reviewCount 项需复核", DeskColors.amber)
        }
        ImportTable(edited, recognized, warnings, locked) { rowId, item ->
            if (defaults[rowId] == item) corrections.remove(rowId) else corrections[rowId] = item
        }
    }
    DesignCard {
        SectionTitle("截图时点", "上海时间")
        BoxWithConstraints(Modifier.fillMaxWidth()) {
            val dateField: @Composable (Modifier) -> Unit = { layout ->
                ImportField("截图日期 YYYY-MM-DD", date, { draft.date = it.take(10).takeUnless { it == defaultDate } }, layout,
                    enabled = !locked, invalid = date.isNotBlank() && runCatching { LocalDate.parse(date) }.isFailure)
            }
            val timeField: @Composable (Modifier) -> Unit = { layout ->
                ImportField("截图时间 HH:mm", time, { draft.time = it.take(18).takeUnless { it == defaultTime } }, layout,
                    enabled = !locked, invalid = time.isNotBlank() && asOf == null)
            }
            if (maxWidth / LocalDensity.current.fontScale >= 232.dp) {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    dateField(Modifier.weight(1.2f))
                    timeField(Modifier.weight(1f))
                }
            } else {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    dateField(Modifier.fillMaxWidth())
                    timeField(Modifier.fillMaxWidth())
                }
            }
        }
        ImportCaption("使用截图实际时点。工作台会校验冻结时点与台账版本。")
    }
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(4.dp)) {
        Text("台账全量对账差异预览", Modifier.weight(1f).semantics { heading() }, style = MaterialTheme.typography.titleSmall)
        TextButton(modifier = Modifier.heightIn(min = 48.dp), enabled = canSubmit && valid, onClick = {
            model.submit("apply_import", buildJsonObject {
                put("importId", id)
                put("phase", "preview")
                put("snapshotAsOf", checkNotNull(asOf))
                put("rows", JsonArray(candidates.map { (rowId, row) -> row.encode(rowId, committed[rowId]) }))
                put("excludedRows", JsonArray(edited.filterValues { it.excluded }.map { (rowId, row) ->
                    buildJsonObject { put("rowId", rowId); put("reason", row.reason.trim()) }
                }))
                put("missingActions", JsonArray(removals.filterNot { it in symbols }.map { symbol ->
                    buildJsonObject { put("symbol", symbol); put("action", "delete") }
                }))
            })
        }) { Text(if (hash == null) "生成预览" else "重新比对") }
    }
    if (diff == null || hash == null) ImportCaption("尚未比对。由工作台比较截图和当前台账后生成差异。")
    else {
        if (stale || modified) NoticeStrip(
            if (stale) "台账版本已变化。重新预览后可提交。" else "核对内容已修改。重新预览后可提交。",
            DeskColors.amber,
        ) else ImportCaption("已比对 ${timeText(batch.number("updatedAt"))} · 台账版本 ${batch.value("basePositionRevision")}")
        ImportDiff(batch, diff, removals, !locked) { symbol, remove ->
            val expected = missing.firstOrNull { it.text("symbol") == symbol }?.text("action") == "delete"
            if (remove == expected) actions.remove(symbol) else actions[symbol] = remove
        }
    }
    when {
        edited.isEmpty() || candidates.isEmpty() -> ImportCaption("需要至少一行有效持仓才能提交全量快照。")
        symbols.distinct().size != symbols.size -> NoticeStrip("证券代码重复，请逐行核对。", DeskColors.amber)
    }
    if (removals.isNotEmpty()) {
        NoticeStrip("本批拟移除 ${removals.size} 只未覆盖持仓。提交时须再次确认。", DeskColors.red)
    } else if (missing.isNotEmpty()) {
        ImportCaption("截图未覆盖的 ${missing.size} 只持仓默认保留。")
    }
    Surface(
        Modifier.fillMaxWidth(), color = DeskColors.blue.copy(alpha = 0.09f), shape = RoundedCornerShape(12.dp),
        border = BorderStroke(1.dp, DeskColors.blue.copy(alpha = 0.22f)),
    ) {
        Row(Modifier.padding(11.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            ImportGlyph(ImportIcon.Database, DeskColors.blue)
            ImportCaption("工作台在同一事务内应用本批差异。任一行失败时整批保留原账。\n批次 $id", DeskColors.secondary)
        }
    }
    val actionSummary = when {
        locked -> "正在等待工作台回执"
        !valid -> "请修正识别结果和截图时点"
        stale || modified || hash == null || diff == null -> "请先生成台账差异预览"
        !canSubmit -> "等待工作台与识别授权就绪"
        batch.flag("readyForConfirm") != true -> "工作台尚未确认差异预览"
        removals.isNotEmpty() -> "将移除 ${removals.size} 只未覆盖持仓 · 需二次确认"
        missing.isNotEmpty() -> "保留 ${missing.size} 只未覆盖持仓"
        else -> "核对后写入 · 收到工作台回执后生效"
    }
    ImportDock(footer, actionSummary, "提交对账确认", "提交对账", ready) { confirm = true }
    if (confirm && hash != null) {
        val deleting = removals.isNotEmpty()
        Confirm(
            if (deleting) "再次确认删除 ${removals.size} 项并回填" else "确认提交对账",
            if (deleting) "将删除截图未覆盖的持仓 ${removals.joinToString("、")}。其余差异与删除在同一事务内应用。"
            else "确认按 ${timeText(asOf)} 的冻结快照提交本批差异。收到工作台业务回执并同步后更新持仓。",
            !ready,
            { confirm = false },
        ) {
            model.submit("apply_import", buildJsonObject {
                put("importId", id)
                put("phase", "confirm")
                put("importVersion", revision)
                put("diffHash", hash)
                if (deleting) put("confirmRemovals", true)
            }) { confirm = false }
        }
    }
}

@Composable
private fun ImportTable(
    edited: Map<String, DraftImportRow>,
    recognized: Map<String?, JsonObject>,
    warnings: List<JsonObject>,
    locked: Boolean,
    change: (String, DraftImportRow) -> Unit,
) {
    Surface(Modifier.fillMaxWidth(), color = DeskColors.panel, shape = RoundedCornerShape(16.dp),
        border = BorderStroke(1.dp, DeskColors.border)) {
        BoxWithConstraints(Modifier.fillMaxWidth()) {
            val multiColumn = maxWidth / LocalDensity.current.fontScale >= 254.dp
            Column {
                if (multiColumn) {
                    Surface(color = DeskColors.field) {
                        Row(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 10.dp),
                            horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            ImportTableLabel("名称 / 代码", Modifier.weight(1.25f))
                            ImportTableLabel("数量（股）", Modifier.weight(0.78f))
                            ImportTableLabel("成本（元）", Modifier.weight(0.95f))
                        }
                    }
                }
                if (edited.isEmpty()) Box(Modifier.padding(12.dp)) { ImportCaption("本批没有识别到持仓行。") }
                edited.entries.forEachIndexed { index, (rowId, item) ->
                    key(rowId) {
                        if (index > 0 || multiColumn) HorizontalDivider(color = DeskColors.border)
                        EditableImportRow(item, recognized[rowId], !locked, multiColumn) { change(rowId, it) }
                    }
                }
                warnings.forEach { warning ->
                    Surface(
                        Modifier.fillMaxWidth().padding(start = 12.dp, end = 12.dp, bottom = 12.dp),
                        color = DeskColors.amber.copy(alpha = 0.09f), shape = RoundedCornerShape(10.dp),
                        border = BorderStroke(1.dp, DeskColors.amber.copy(alpha = 0.27f)),
                    ) {
                        Box(Modifier.padding(horizontal = 10.dp, vertical = 8.dp)) {
                            ImportCaption(
                                listOfNotNull(warning.text("symbol"), warning.text("message") ?: warning.text("code"))
                                    .joinToString(" · "),
                                DeskColors.amber,
                            )
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun ImportTableLabel(label: String, modifier: Modifier = Modifier) {
    Text(label, modifier, color = DeskColors.faint, style = MaterialTheme.typography.labelSmall,
        fontWeight = FontWeight.SemiBold)
}

@Composable
private fun EditableImportRow(
    item: DraftImportRow,
    recognition: JsonObject?,
    enabled: Boolean,
    multiColumn: Boolean,
    change: (DraftImportRow) -> Unit,
) {
    var expanded by rememberSaveable { mutableStateOf(false) }
    val stock = item.name.ifBlank { item.symbol.ifBlank { "待核对证券" } }
    val issues = recognition?.objects("issues").orEmpty()
    val confidence = recognition?.obj("confidence")?.text("symbol")?.toBigDecimalOrNull()?.let {
        it.movePointRight(2).stripTrailingZeros().toPlainString()
    }
    val nameField: @Composable (Modifier) -> Unit = { layout ->
        Column(layout, verticalArrangement = Arrangement.spacedBy(3.dp)) {
            if (!multiColumn) ImportTableLabel("名称 / 代码")
            ImportField("$stock 名称", item.name, { change(item.copy(name = it.take(100))) },
                enabled = enabled && !item.excluded, invalid = !item.excluded && item.name.isBlank())
            val codeAndConfidence = listOfNotNull(item.symbol.ifBlank { null }, confidence?.let { "置信度 $it%" })
            if (codeAndConfidence.isNotEmpty()) ImportCaption(codeAndConfidence.joinToString(" · "))
        }
    }
    val qtyField: @Composable (Modifier) -> Unit = { layout ->
        Column(layout, verticalArrangement = Arrangement.spacedBy(3.dp)) {
            if (!multiColumn) ImportTableLabel("数量（股）")
            ImportField("$stock 数量", item.qty, { change(item.copy(qty = it.take(24))) }, type = KeyboardType.Number,
                enabled = enabled && !item.excluded, invalid = !item.excluded && whole(item.qty) == null)
        }
    }
    val costField: @Composable (Modifier) -> Unit = { layout ->
        Column(layout, verticalArrangement = Arrangement.spacedBy(3.dp)) {
            if (!multiColumn) ImportTableLabel("成本（元）")
            ImportField("$stock 成本", item.cost, { change(item.copy(cost = it.take(48))) }, type = KeyboardType.Decimal,
                enabled = enabled && !item.excluded, invalid = !item.excluded && decimal(item.cost) == null)
        }
    }
    Column(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 10.dp)) {
        if (multiColumn) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                nameField(Modifier.weight(1.25f))
                qtyField(Modifier.weight(0.78f))
                costField(Modifier.weight(0.95f))
            }
        } else {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                nameField(Modifier.fillMaxWidth())
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    qtyField(Modifier.weight(1f))
                    costField(Modifier.weight(1f))
                }
            }
        }
        issues.forEach { issue ->
            ImportCaption(issue.text("message") ?: issue.value("code"), DeskColors.amber)
        }
        TextButton(onClick = { expanded = !expanded }, modifier = Modifier.heightIn(min = 48.dp),
            contentPadding = PaddingValues(0.dp)) {
            Text(if (expanded) "收起行详情" else if (item.excluded) "已排除 · 查看原因" else "更多字段与排除")
        }
        if (expanded) {
            ImportCheck("排除此行", item.excluded, enabled) { change(item.copy(excluded = it)) }
            if (item.excluded) {
                OutlinedTextField(
                    item.reason, { change(item.copy(reason = it.take(1000))) },
                    modifier = Modifier.fillMaxWidth(),
                    enabled = enabled, label = { Text("排除原因（必填）") },
                )
            } else {
                ImportField("证券代码（如 600000.SH）", item.symbol, { change(item.copy(symbol = it.take(12))) },
                    enabled = enabled, invalid = !symbolPattern.matches(item.normalizedSymbol))
                Spacer(Modifier.height(8.dp))
                ImportField("可用股数（留空为未知）", item.available, { change(item.copy(available = it.take(24))) },
                    type = KeyboardType.Number, enabled = enabled,
                    invalid = item.available.isNotBlank() && !item.validAvailable())
            }
            recognition?.obj("raw")?.let { raw ->
                Spacer(Modifier.height(8.dp))
                ImportCaption("识别原值 · ${raw.value("name")} · ${raw.value("symbol")}")
                ImportCaption(positionValues(raw))
            }
        }
    }
}

@Composable
private fun ImportField(
    label: String,
    value: String,
    change: (String) -> Unit,
    modifier: Modifier = Modifier,
    type: KeyboardType = KeyboardType.Text,
    enabled: Boolean = true,
    invalid: Boolean = false,
) {
    var focused by remember { mutableStateOf(false) }
    val border = when {
        focused -> DeskColors.blue
        invalid -> DeskColors.amber.copy(alpha = 0.65f)
        else -> DeskColors.borderStrong
    }
    val tint = if (enabled) MaterialTheme.colorScheme.onSurface else DeskColors.faint
    val typography = MaterialTheme.typography.bodyMedium
    BasicTextField(
        value, change, modifier.onFocusChanged { focused = it.isFocused }.semantics { contentDescription = label },
        enabled = enabled, singleLine = true, keyboardOptions = KeyboardOptions(keyboardType = type),
        cursorBrush = SolidColor(DeskColors.blue),
        textStyle = if (type == KeyboardType.Text) typography.copy(color = tint) else
            typography.copy(color = tint, fontFamily = deskMono, textAlign = TextAlign.End),
        decorationBox = { inner ->
            Surface(color = DeskColors.field, shape = RoundedCornerShape(9.dp), border = BorderStroke(1.dp, border)) {
                Box(Modifier.fillMaxWidth().heightIn(min = 48.dp).padding(horizontal = 8.dp),
                    contentAlignment = Alignment.CenterStart) {
                    if (value.isEmpty()) Text(label, color = DeskColors.faint, style = MaterialTheme.typography.labelSmall,
                        maxLines = 1, overflow = TextOverflow.Ellipsis)
                    inner()
                }
            }
        },
    )
}

@Composable
internal fun rememberImportDrafts(state: WorkbenchProjection): ImportDrafts = key(state.pairId, state.epoch) {
    rememberSaveable(saver = ImportDrafts.saver) { ImportDrafts() }
}

internal class ImportDrafts(
    selectedBatch: String? = null,
    private val editors: MutableMap<String, ImportEditorDraft> = mutableMapOf(),
) {
    var selectedBatch by mutableStateOf(selectedBatch)

    fun editor(id: String, revision: Long): ImportEditorDraft = editors[id]?.takeIf { it.revision == revision }
        ?: ImportEditorDraft(revision).also { editors[id] = it }

    fun discard(id: String) {
        editors.remove(id)
    }

    companion object {
        val saver: Saver<ImportDrafts, Bundle> = Saver(
            save = { drafts ->
                Bundle().apply {
                    putString("selectedBatch", drafts.selectedBatch)
                    putBundle("editors", Bundle().apply {
                        drafts.editors.forEach { (id, draft) -> draft.save()?.let { putBundle(id, it) } }
                    })
                }
            },
            restore = { saved ->
                val restored = mutableMapOf<String, ImportEditorDraft>()
                saved.getBundle("editors")?.let { editors ->
                    editors.keySet().forEach { id ->
                        editors.getBundle(id)?.let { restored[id] = ImportEditorDraft.restore(it) }
                    }
                }
                ImportDrafts(saved.getString("selectedBatch"), restored)
            },
        )
    }
}

internal class ImportEditorDraft(val revision: Long) {
    var date by mutableStateOf<String?>(null)
    var time by mutableStateOf<String?>(null)
    val corrections = mutableStateMapOf<String, DraftImportRow>()
    val removals = mutableStateMapOf<String, Boolean>()

    fun save(): Bundle? {
        if (date == null && time == null && corrections.isEmpty() && removals.isEmpty()) return null
        return Bundle().apply {
            putLong("revision", revision)
            putString("date", date)
            putString("time", time)
            putBundle("corrections", Bundle().apply {
                corrections.forEach { (id, row) -> putStringArray(id, row.saveFields()) }
            })
            putBundle("removals", Bundle().apply {
                removals.forEach { (symbol, remove) -> putBoolean(symbol, remove) }
            })
        }
    }

    companion object {
        fun restore(saved: Bundle) = ImportEditorDraft(saved.getLong("revision")).apply {
            date = saved.getString("date")
            time = saved.getString("time")
            saved.getBundle("corrections")?.let { rows ->
                rows.keySet().forEach { id ->
                    rows.getStringArray(id)?.let { fields -> corrections[id] = DraftImportRow(fields) }
                }
            }
            saved.getBundle("removals")?.let { actions ->
                actions.keySet().forEach { symbol -> removals[symbol] = actions.getBoolean(symbol) }
            }
        }
    }
}

internal data class DraftImportRow(
    val symbol: String,
    val name: String,
    val qty: String,
    val available: String,
    val cost: String,
    val excluded: Boolean,
    val reason: String,
) {
    constructor(fields: Array<String>) : this(
        symbol = fields[0], name = fields[1], qty = fields[2], available = fields[3],
        cost = fields[4], excluded = fields[5].toBoolean(), reason = fields[6],
    )

    fun saveFields() = arrayOf(symbol, name, qty, available, cost, excluded.toString(), reason)

    val normalizedSymbol get() = symbol.trim().uppercase(Locale.ROOT)

    fun validAvailable() = available.isBlank() || whole(available)?.let { it <= (whole(qty) ?: -1L) } == true

    fun valid() = if (excluded) reason.isNotBlank() else
        symbolPattern.matches(normalizedSymbol) && name.isNotBlank() && name.length <= 100 &&
            whole(qty) != null && decimal(cost) != null && validAvailable()

    fun encode(id: String, previous: JsonObject?) = buildJsonObject {
        put("rowId", id)
        put("symbol", normalizedSymbol)
        put("name", name.trim())
        put("qty", checkNotNull(whole(qty)))
        put("availableQty", whole(available)?.let(::JsonPrimitive) ?: JsonNull)
        put("cost", cost.trim())
        val matching = previous?.takeIf { it.text("symbol") == normalizedSymbol }
        put("pendingLots", matching?.get("pendingLots") ?: JsonNull)
        put("resolvedExecutionIds", matching?.array("resolvedExecutionIds") ?: JsonArray(emptyList()))
    }
}

@Composable
private fun ImportDiff(
    batch: JsonObject,
    diff: JsonObject,
    removals: List<String>,
    enabled: Boolean,
    setRemove: ((String, Boolean) -> Unit)?,
) {
    val added = diff.stringList("added")
    val changed = diff.objects("changed")
    val unchanged = diff.stringList("unchanged")
    val missing = diff.objects("missing")
    val positions = batch.objects("rows").associateBy { it.text("symbol") }
    BoxWithConstraints(Modifier.fillMaxWidth()) {
        val columns = if (maxWidth / LocalDensity.current.fontScale >= 255.dp) 4 else 2
        FlowRow(
            Modifier.fillMaxWidth(), maxItemsInEachRow = columns,
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            val tile = Modifier.weight(1f).fillMaxRowHeight()
            DiffCount("新增标的", added.size, DeskColors.blue, tile)
            DiffCount("持仓变更", changed.size, DeskColors.amber, tile)
            DiffCount("完全一致", unchanged.size, DeskColors.text, tile)
            DiffCount("未覆盖保留", missing.size - removals.size, DeskColors.green, tile)
        }
    }
    Surface(Modifier.fillMaxWidth(), color = DeskColors.panel, shape = RoundedCornerShape(16.dp),
        border = BorderStroke(1.dp, DeskColors.border)) {
        Column {
            var first = true
            for (row in changed) {
                val symbol = row.text("symbol") ?: continue
                val before = row.obj("before")
                val after = row.obj("after")
                if (!first) HorizontalDivider(color = DeskColors.border)
                first = false
                DiffEntry(
                    diffTitle(after?.text("name") ?: before?.text("name"), symbol),
                    "持仓变更", DeskColors.amber,
                    "原 ${positionValues(before)}\n新 ${positionValues(after)}",
                )
            }
            for (symbol in added) {
                if (!first) HorizontalDivider(color = DeskColors.border)
                first = false
                DiffEntry(diffTitle(positions[symbol]?.text("name"), symbol), "新增建仓", DeskColors.blue,
                    "新 ${positionValues(positions[symbol])}")
            }
            for (symbol in unchanged) {
                if (!first) HorizontalDivider(color = DeskColors.border)
                first = false
                DiffEntry(diffTitle(positions[symbol]?.text("name"), symbol), "完全一致", DeskColors.faint,
                    positionValues(positions[symbol]))
            }
            for (row in missing) {
                val symbol = row.text("symbol") ?: continue
                val before = row.obj("before")
                if (!first) HorizontalDivider(color = DeskColors.border)
                first = false
                val remove = symbol in removals
                Column(Modifier.fillMaxWidth().padding(horizontal = 14.dp, vertical = 10.dp)) {
                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Column(Modifier.weight(1f)) {
                            Text(diffTitle(before?.text("name"), symbol), style = MaterialTheme.typography.titleSmall)
                            ImportCaption("本张截图未覆盖")
                        }
                        if (remove) StatusBadge(if (setRemove == null) "已移除" else "拟删除", DeskColors.red)
                    }
                    ImportCaption("既有 ${positionValues(before)}")
                    if (setRemove != null) {
                        ImportCheck("保留既有持仓", checked = !remove, enabled = enabled) { setRemove(symbol, !it) }
                    } else if (!remove) {
                        StatusBadge("已保留", DeskColors.green)
                    }
                }
            }
            if (first) Box(Modifier.padding(12.dp)) { ImportCaption("当前预览没有持仓差异。") }
        }
    }
}

@Composable
private fun DiffCount(label: String, value: Int, tint: Color, modifier: Modifier) {
    Surface(modifier, color = DeskColors.panel, shape = RoundedCornerShape(13.dp),
        border = BorderStroke(1.dp, DeskColors.border)) {
        Column(
            Modifier.background(Brush.verticalGradient(listOf(Color(0xFF182132), Color(0xFF0F1521))))
                .padding(horizontal = 3.dp, vertical = 7.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(value.toString(), color = tint, style = MaterialTheme.typography.titleSmall.copy(fontFamily = deskMono),
                fontWeight = FontWeight.Bold)
            Text(label, color = DeskColors.faint, style = MaterialTheme.typography.labelSmall, textAlign = TextAlign.Center)
        }
    }
}

@Composable
private fun DiffEntry(title: String, label: String, tint: Color, detail: String) {
    Column(Modifier.fillMaxWidth().padding(horizontal = 14.dp, vertical = 12.dp), verticalArrangement = Arrangement.spacedBy(5.dp)) {
        FlowRow(
            Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween,
            verticalArrangement = Arrangement.spacedBy(5.dp),
        ) {
            Text(title, style = MaterialTheme.typography.titleSmall)
            StatusBadge(label, tint)
        }
        ImportCaption(detail)
    }
}

private fun diffTitle(name: String?, symbol: String) =
    name?.takeIf { it.isNotBlank() }?.let { "$it ($symbol)" } ?: symbol

private fun positionValues(row: JsonObject?): String {
    if (row == null) return "数量与成本未知"
    val lots = row.array("pendingLots")
    return buildString {
        append("${row.value("qty")} 股 · 成本 ${row.value("cost")} · 可用 ${row.value("availableQty")}")
        if ("asOf" in row) append(" · 时点 ${timeText(row.number("asOf"))}")
        if (!lots.isNullOrEmpty()) {
            append("\n待可用批次 ")
            append(lots.joinToString("、") { entry ->
                val lot = entry.jsonObject
                "${lot.value("qty")} 股 / ${lot.value("sellableDate")}"
            })
        }
    }
}

@Composable
private fun ImportCheck(label: String, checked: Boolean, enabled: Boolean, onChange: (Boolean) -> Unit) {
    Row(
        Modifier.fillMaxWidth().heightIn(min = 48.dp)
            .toggleable(checked, enabled = enabled, role = Role.Checkbox, onValueChange = onChange),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Checkbox(checked, onCheckedChange = null, enabled = enabled)
        Text(label, color = if (enabled) MaterialTheme.colorScheme.onSurface else DeskColors.faint,
            style = MaterialTheme.typography.bodyMedium)
    }
}

@Composable
private fun ImportCaption(text: String, color: Color = DeskColors.muted) {
    Text(text, color = color, style = MaterialTheme.typography.bodySmall)
}

private enum class ImportIcon(val path: String) {
    Shield("M12 22s7-4 7-11V5l-7-3-7 3v6c0 7 7 11 7 11z M9 12l2 2 4-4"),
    Database("M21 7a9 4 0 1 1-18 0 9 4 0 1 1 18 0 M3 7v10a9 4 0 0 0 18 0V7 M3 12a9 4 0 0 0 18 0"),
}

@Composable
private fun ImportGlyph(icon: ImportIcon, tint: Color) {
    val path = remember(icon) { PathParser().parsePathString(icon.path).toPath() }
    Canvas(Modifier.size(16.dp)) {
        scale(size.width / 24f, size.height / 24f, Offset.Zero) {
            drawPath(path, tint, style = Stroke(width = 1.8f, cap = StrokeCap.Round, join = StrokeJoin.Round))
        }
    }
}

@Composable
private fun ImportDock(
    footer: ScreenFooter?,
    summary: String,
    label: String,
    shortcutLabel: String,
    enabled: Boolean,
    confirm: () -> Unit,
) {
    val inline: @Composable () -> Unit = {
        Column(Modifier.fillMaxWidth(), horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(summary, style = MaterialTheme.typography.bodySmall, color = DeskColors.muted,
                textAlign = TextAlign.Center)
            MainAction(label, enabled, R.drawable.ic_ultra_check, confirm)
        }
    }
    if (footer == null) inline() else {
        footer.ResponsiveAction(showShortcut = enabled, inline = inline) {
            ContextAction("对账确认 · 事务更新", summary, shortcutLabel, enabled, confirm)
        }
    }
}
