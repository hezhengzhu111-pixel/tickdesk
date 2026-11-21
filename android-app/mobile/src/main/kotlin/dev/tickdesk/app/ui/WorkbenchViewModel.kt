package dev.tickdesk.app.ui

import android.app.Application
import android.content.ContentResolver
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import android.provider.OpenableColumns
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import dev.tickdesk.app.R
import dev.tickdesk.app.TickDeskApplication
import dev.tickdesk.app.data.readBounded
import dev.tickdesk.core.*
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.serialization.json.JsonObject

class SelectedImage internal constructor(
    val name: String,
    val size: Int,
    val preview: Bitmap,
    internal val uri: Uri,
    internal val mime: String,
    internal val sha256: String,
    internal val scope: FileTransferScope,
)

internal data class FileTransferScope(val pairId: String, val epoch: String)

internal fun WorkbenchProjection.fileTransferScope(): FileTransferScope? {
    if (!canQueue || connection.recoveryRequired) return null
    return FileTransferScope(pairId ?: return null, epoch ?: return null)
}

enum class Tab(val label: Int, val icon: Int) {
    INBOX(R.string.tab_inbox, R.drawable.ic_inbox),
    CHAT(R.string.tab_chat, R.drawable.ic_chat),
    DISCOVER(R.string.tab_discover, R.drawable.ic_discover),
    PORTFOLIO(R.string.tab_portfolio, R.drawable.ic_portfolio),
    SETTINGS(R.string.tab_settings, R.drawable.ic_settings),
}

class WorkbenchViewModel(application: Application) : AndroidViewModel(application) {
    private val runtime = (application as TickDeskApplication).runtime
    val state = runtime.state
    internal val pairingDisplay = runtime.pairingDisplay
    internal val alert = runtime.reminders.alert
    internal val backgroundReminders = runtime.backgroundReminders.state
    internal fun setBackgroundReminders(enabled: Boolean) = runtime.backgroundReminders.setEnabled(enabled)
    private val mutableError = MutableStateFlow<String?>(null)
    val error = mutableError.asStateFlow()
    private val mutablePairing = MutableStateFlow<PairingConfig?>(null)
    val pairing = mutablePairing.asStateFlow()
    private val mutableBusy = MutableStateFlow(false)
    val busy = mutableBusy.asStateFlow()
    private val mutableImage = MutableStateFlow<SelectedImage?>(null)
    val selectedImage = mutableImage.asStateFlow()
    private val mutableHoldingImageError = MutableStateFlow<String?>(null)
    val holdingImageError = mutableHoldingImageError.asStateFlow()

    init {
        viewModelScope.launch {
            state.collect { current ->
                if (mutableImage.value?.scope != current.fileTransferScope()) mutableImage.value = null
            }
        }
    }

    fun retry() = runtime.retry()
    internal fun refreshPositions() = action("暂时无法刷新持仓快照，请检查连接。") {
        runtime.repository.refresh()
    }
    fun dismissAlert() = runtime.reminders.dismiss()
    fun clearError() { mutableError.value = null }
    fun dismissPairing() { mutablePairing.value = null }

    fun importPairing(uri: Uri) = action("无法读取或校验配对配置") {
        val config = withContext(Dispatchers.IO) {
            val resolver = getApplication<Application>().contentResolver
            val bytes = resolver.openInputStream(uri)?.use { it.readBounded(8192) } ?: error("Document not available")
            try { Protocol.pairing(CanonicalJson.encode(CanonicalJson.decode(bytes))) }
            finally { bytes.fill(0) }
        }
        mutablePairing.value = config
    }

    fun scanPairing(value: String) = action("此二维码不是有效的 TickDesk App 配对配置") {
        mutablePairing.value = Protocol.pairing(value)
    }

    fun confirmPairing() = action("无法保存配对凭据") {
        val config = mutablePairing.value ?: return@action
        runtime.pair(config)
        mutablePairing.value = null
    }

    internal fun pickImage(uri: Uri, scope: FileTransferScope) = action(
        "无法读取截图。请检查工作台配对，并选择不超过 20 MiB 和 4000 万像素的 JPEG 或 PNG。",
    ) {
        check(uri.scheme == ContentResolver.SCHEME_CONTENT && state.value.fileTransferScope() == scope)
        val selected = withContext(Dispatchers.IO) {
            val resolver = getApplication<Application>().contentResolver
            val name = resolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use {
                if (it.moveToFirst()) it.getString(0) else null
            }?.replace(unsafeFileName, "_")?.take(160)?.trimEnd { Character.isHighSurrogate(it) }?.trim()
                ?.takeIf { it.isNotEmpty() } ?: "截图"
            val bytes = readImageBytes(uri)
            try {
                val (mime, preview) = imagePreview(bytes)
                SelectedImage(name, bytes.size, preview, uri, mime, CanonicalJson.sha256(bytes), scope)
            } finally {
                bytes.fill(0)
            }
        }
        check(state.value.fileTransferScope() == scope)
        mutableImage.value = selected
    }

    fun clearImage() { mutableImage.value = null }

    internal fun importHoldingImage(uri: Uri, scope: FileTransferScope, noticeVersion: String) {
        if (mutableBusy.value) return
        mutableBusy.value = true
        mutableHoldingImageError.value = null
        viewModelScope.launch {
            try {
                check(uri.scheme == ContentResolver.SCHEME_CONTENT && state.value.fileTransferScope() == scope)
                withContext(Dispatchers.IO) {
                    val bytes = readImageBytes(uri)
                    try {
                        val (mime, preview) = imagePreview(bytes)
                        preview.recycle()
                        runtime.startHoldingImport("持仓截图", mime, bytes, noticeVersion, scope.pairId, scope.epoch)
                    } finally { bytes.fill(0) }
                }
            } catch (cancel: CancellationException) {
                throw cancel
            } catch (_: Exception) {
                if (state.value.fileTransferScope() == scope) {
                    mutableHoldingImageError.value = "无法读取图片，请选择不超过 20 MiB 和 4000 万像素的 JPEG 或 PNG。"
                }
            } finally { mutableBusy.value = false }
        }
    }

    internal fun retryHoldingImage() {
        val current = state.value
        val scope = current.fileTransferScope() ?: return
        val notice = current.meta?.obj("visionNotice")?.text("noticeVersion") ?: return
        if (current.holdingImport?.canRetry != true) return
        action("无法重新识别此图，请核对连接状态。") {
            mutableHoldingImageError.value = null
            runtime.retryHoldingImport(notice, scope.pairId, scope.epoch)
        }
    }

    internal fun saveOriginal() = action("无法暂存原图。请检查设备存储和工作台配对，必要时重新选择截图。") {
        val selected = mutableImage.value ?: return@action
        val scope = selected.scope
        check(state.value.fileTransferScope() == scope)
        withContext(Dispatchers.IO) {
            val bytes = readImageBytes(selected.uri)
            try {
                check(bytes.size == selected.size && CanonicalJson.sha256(bytes) == selected.sha256)
                runtime.startUpload(selected.name, selected.mime, bytes, scope.pairId, scope.epoch)
            } finally {
                bytes.fill(0)
            }
        }
        mutableImage.value = null
    }

    internal fun startDownload(fileId: String, scope: FileTransferScope, started: (String) -> Unit) =
        action("无法保存下载任务。请检查设备存储和工作台配对。") {
            started(runtime.startDownload(fileId, scope.pairId, scope.epoch))
        }

    internal fun cancelTransfer(id: String, scope: FileTransferScope) = action("无法保存取消请求。") {
        runtime.cancelTransfer(id, scope.pairId, scope.epoch)
    }

    internal suspend fun readDownloadedPreview(transfer: FileTransferView, scope: FileTransferScope): Bitmap? =
        withContext(Dispatchers.IO) {
            try {
                val bytes = runtime.repository.readDownloaded(transfer.id, scope.pairId, scope.epoch)
                    ?: return@withContext null
                try {
                    if (bytes.size.toLong() != transfer.sizeBytes || CanonicalJson.sha256(bytes) != transfer.sha256) {
                        return@withContext null
                    }
                    val (mime, preview) = imagePreview(bytes)
                    preview.takeIf { mime == transfer.mime && state.value.fileTransferScope() == scope }
                } finally {
                    bytes.fill(0)
                }
            } catch (cancel: CancellationException) {
                throw cancel
            } catch (_: Exception) {
                null
            }
        }

    private fun readImageBytes(uri: Uri): ByteArray =
        getApplication<Application>().contentResolver.openInputStream(uri)?.use { it.readBounded(MAX_IMAGE_BYTES) }
            ?: error("Image unavailable")

    private fun imagePreview(bytes: ByteArray): Pair<String, Bitmap> {
        require(bytes.size in 1..MAX_IMAGE_BYTES)
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
        val mime = bounds.outMimeType
        require((mime == "image/png" || mime == "image/jpeg") && bounds.outWidth > 0 && bounds.outHeight > 0 &&
            bounds.outWidth.toLong() * bounds.outHeight <= 40_000_000)
        var sample = 1
        while (maxOf(bounds.outWidth, bounds.outHeight) / sample > 1024) sample *= 2
        val preview = BitmapFactory.decodeByteArray(bytes, 0, bytes.size,
            BitmapFactory.Options().apply { inSampleSize = sample }) ?: error("Image cannot be decoded")
        return checkNotNull(mime) to preview
    }

    fun submit(command: String, params: JsonObject = JsonObject(emptyMap()), committed: () -> Unit = {}) =
        submitWithReceipt(command, params) { committed() }

    internal fun submitWithReceipt(command: String, params: JsonObject, committed: (String) -> Unit) {
        val current = state.value
        action("操作未能保存。请核对表单和连接状态后重试。") {
            check(current.pairId != null)
            val id = runtime.enqueue(command, params, current.pairId, current.epoch)
            committed(id)
        }
    }

    /** Draft persistence is best-effort and never claims a business result. */
    internal fun saveDraft(kind: String, objectId: String, draft: JsonObject) {
        viewModelScope.launch {
            runCatching { runtime.repository.saveDraft(kind, objectId, draft) }
        }
    }

    internal fun clearDraft(kind: String, objectId: String) {
        viewModelScope.launch {
            runCatching { runtime.repository.clearDraft(kind, objectId) }
        }
    }

    internal fun saveRecoveryDraft(draftId: String, draft: JsonObject) {
        viewModelScope.launch {
            runCatching { runtime.repository.saveRecoveryDraft(draftId, draft) }
        }
    }

    internal fun clearRecoveryDraft(draftId: String) {
        viewModelScope.launch {
            runCatching { runtime.repository.clearRecoveryDraft(draftId) }
        }
    }

    internal suspend fun readMarket(command: String, params: JsonObject, pair: String, epoch: String) {
        try {
            runtime.enqueue(command, params, pair, epoch)
        } catch (cancel: CancellationException) {
            throw cancel
        } catch (_: Exception) {
            mutableError.value = "行情读取未能保存。请检查连接后刷新本页。"
        }
    }

    internal suspend fun readReplyPage(scope: ReplyReadScope, params: JsonObject): JsonObject {
        fun WorkbenchProjection.isCurrent() = replyReadScope() == scope && supportsReplyPages() && connection.canWrite
        if (!state.value.isCurrent()) throw ReplyPageReadException()
        val id = try {
            runtime.enqueue(GET_REPLY_PAGE, params, scope.pair, scope.epoch, scope.peer)
        } catch (cancel: CancellationException) {
            throw cancel
        } catch (_: Exception) {
            throw ReplyPageReadException()
        }
        var received = false
        try {
            val projection = withTimeoutOrNull(15_000) {
                state.first { current -> !current.isCurrent() ||
                    current.currentIntents(GET_REPLY_PAGE).any { it.id == id && it.stage !in pendingStages } }
            } ?: throw ReplyPageReadException()
            if (!projection.isCurrent() || !state.value.isCurrent()) throw ReplyPageReadException()
            val reply = projection.currentIntents(GET_REPLY_PAGE).singleOrNull { it.id == id }
                ?: throw ReplyPageReadException()
            received = true
            return reply.result?.takeIf { reply.stage == DeliveryStage.APPLIED } ?: throw ReplyPageReadException(reply.errorCode)
        } finally {
            if (!received && state.value.pairId == scope.pair) {
                withContext(NonCancellable) { runCatching { runtime.repository.cancelUnsent(id) } }
            }
        }
    }

    fun cancel(id: String) = action("这项操作已尝试发送，请查询原结果。") {
        check(runtime.repository.cancelUnsent(id))
    }

    fun query(id: String) = action("暂时无法查询原操作，请检查连接。") { runtime.repository.checkOriginal(id) }
    fun review(id: String) = action("无法保存核对记录。") { runtime.repository.markReviewed(id) }
    fun finishReview() = action("须先完成事项核对，并取得服务端正常模式的完整快照。") {
        runtime.repository.finishReview()
    }

    suspend fun collectRecoveryEvidence(): RecoveryEvidenceCollection = runtime.repository.collectRecoveryEvidence()

    suspend fun refreshRecoverySnapshot() = runtime.repository.refresh()

    suspend fun recoveryRequest(
        command: String,
        params: JsonObject,
        expectedEvidence: Set<RecoveryEvidenceSource>? = null,
    ): JsonObject = runtime.recoveryRequest(command, params, expectedEvidence)

    private fun action(onFailure: String, work: suspend () -> Unit) {
        if (mutableBusy.value) return
        mutableBusy.value = true
        viewModelScope.launch {
            try {
                mutableError.value = null
                work()
            } catch (cancel: CancellationException) {
                throw cancel
            } catch (_: Exception) {
                mutableError.value = onFailure
            } finally {
                mutableBusy.value = false
            }
        }
    }

    private companion object {
        const val MAX_IMAGE_BYTES = 20 * 1024 * 1024
        val unsafeFileName = Regex("[\\p{Cc}\\p{Cf}\\p{Cs}/\\\\]")
    }
}
