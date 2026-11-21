package dev.tickdesk.core

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.json.*
import java.util.UUID

enum class HoldingImportStatus { UPLOADING, AUTHORIZING, RECOGNIZING, SYNCING, SUCCESS, INVALID, FAILED }

data class HoldingImportView(
    val id: String, val status: HoldingImportStatus, val errorCode: String?,
    val importId: String, val transferId: String, val canRetry: Boolean = false,
) {
    val terminal: Boolean get() = status in setOf(HoldingImportStatus.SUCCESS, HoldingImportStatus.INVALID, HoldingImportStatus.FAILED)
}

data class IntentView(
    val id: String, val command: String, val data: JsonObject, val stage: DeliveryStage,
    val errorCode: String?, val result: JsonObject?, val at: Long, val queryState: String?, val reviewed: Boolean,
)

data class ReportNotice(val reportId: String, val displayUntil: Long)

data class WorkbenchProjection(
    val connection: ConnectionState = ConnectionState(),
    val pairId: String? = null,
    val epoch: String? = null,
    val canQueue: Boolean = false,
    val requiresReview: Boolean = false,
    val synchronizedAt: Long? = null,
    val sequence: Long? = null,
    val meta: JsonObject? = null,
    val records: Map<String, List<JsonObject>> = emptyMap(),
    val reads: Map<String, JsonObject> = emptyMap(),
    val drafts: Map<String, JsonObject> = emptyMap(),
    val messages: List<JsonObject> = emptyList(),
    val replies: List<JsonObject> = emptyList(),
    val intents: List<IntentView> = emptyList(),
    val connectionError: String? = null,
    val syncError: String? = null,
    val transfers: List<FileTransferView> = emptyList(),
    val holdingImport: HoldingImportView? = null,
    val reportNotices: List<ReportNotice> = emptyList(),
    val serviceRecoveryActive: Boolean = false,
    val hasPendingReceipts: Boolean = false,
    val peerIncarnation: String? = null,
    val reportReads: ReportReads = ReportReads(),
) {
    val hasPendingTransfers: Boolean get() = transfers.any(FileTransferView::needsDelivery)
    val hasPendingHoldingImport: Boolean get() = holdingImport?.terminal == false
}

class WorkbenchRepository(
    private val store: WorkbenchStore,
    private val now: () -> Long = System::currentTimeMillis,
    private val newId: () -> String = { UUID.randomUUID().toString() },
) {
    private val mutex = Mutex()
    private val mutableState = MutableStateFlow(WorkbenchProjection())
    val state = mutableState.asStateFlow()
    private var pair: String? = null
    private var relay = false
    private var peerReady = false
    private var fresh = false
    private var error: String? = null
    private var preferFiles = false
    private val files = FileTransfers(now, newId) { scope, command, epoch, params ->
        queue(scope, command, epoch, params, visible = false, fileTransfer = true)
    }

    suspend fun selectPair(pairId: String?) = mutex.withLock {
        if (pair == pairId && pairId != null) return@withLock
        pair = pairId
        relay = false
        peerReady = false
        fresh = false
        error = null
        publish()
    }

    suspend fun hello(): String = mutex.withLock {
        val epoch = pair?.let { scope -> store.transaction { checkpoint(scope).text("epoch") } }
        val incarnation = store.transaction { identity() }
        CanonicalJson.encode(buildJsonObject {
            put("v", 2); put("type", "hello"); put("incarnation", incarnation); put("serverEpoch", epoch)
        })
    }

    suspend fun disconnect(reason: String? = null, relayStillOnline: Boolean = false) = mutex.withLock {
        relay = relayStillOnline
        peerReady = false
        fresh = false
        error = reason
        publish()
    }

    suspend fun peerOnline(incarnation: String, announcedEpoch: String) = mutex.withLock {
        val scope = requirePair()
        val previous = store.transaction { checkpoint(scope) }
        if (peerReady && previous.text("peer") == incarnation &&
            previous.text("announcedEpoch") == announcedEpoch) return@withLock
        store.transaction {
            val changed = previous.text("peer")?.let { it != incarnation } == true ||
                previous.text("announcedEpoch")?.let { it != announcedEpoch } == true
            if (changed) {
                if (previous.text("epoch") != announcedEpoch) archiveRecoveryProjection(scope, previous)
                for ((id, intent) in list(scope, INTENT)) {
                    if (intent.stage() in unresolved) {
                        put(scope, INTENT, id, intent.withStage(DeliveryStage.QUARANTINED))
                    }
                }
                clear(scope, OUTBOX)
                clear(scope, RECEIPTS)
                with(files) { quarantine(scope) }
                saveCheckpoint(scope, EMPTY)
            }
            val checkpoint = checkpoint(scope).patch(
                "peer" to incarnation, "announcedEpoch" to announcedEpoch,
                "review" to (changed || previous.flag("review") == true),
            )
            saveCheckpoint(scope, checkpoint)
            identity()
            ensureSync(scope)
            if (!changed) with(files) { reconnected(scope) }
        }
        relay = true
        peerReady = true
        fresh = false
        error = null
        publish()
    }

    suspend fun enqueue(
        command: String, params: JsonObject = EMPTY, expectedPair: String? = null, expectedEpoch: String? = null,
        expectedPeer: String? = null,
    ): String = mutex.withLock {
        val scope = requirePair()
        protocolCheck(expectedPair == null || expectedPair == scope, "Pairing changed")
        protocolCheck(command !in evidenceGates, "Recovery requires a frozen evidence set")
        val id = store.transaction {
            val cp = checkpoint(scope)
            val rule = Protocol.commands[command] ?: throw ProtocolException("Unknown command")
            val epoch = cp.text("epoch")
            protocolCheck(expectedEpoch == null || expectedEpoch == epoch, "Server epoch changed")
            val recovery = cp.flag("review") == true || cp.flag("recovery") == true
            protocolCheck(expectedPeer == null || (expectedPeer == cp.text("peer") && peerReady && fresh && !recovery),
                "Current workbench session is unavailable")
            val permitted = if (rule.recoveryOnly) peerReady && fresh && cp.flag("recovery") == true else
                if (rule.writes) epoch != null && !recovery else peerReady && (!recovery || rule.duringRecovery)
            protocolCheck(permitted && command != "sync", "Command is unavailable in current connection state")
            queue(scope, command, epoch ?: cp.text("announcedEpoch"), params, visible = true)
        }
        publish()
        id
    }

    /**
     * UI drafts are owned by the workbench store, not by a composable. They are
     * scoped by pairing, server epoch and the edited object, and carry the peer
     * incarnation that produced them. A new epoch therefore cannot resurrect an
     * old draft under a different recovery identity.
     */
    suspend fun saveDraft(kind: String, objectId: String, value: JsonObject) = mutex.withLock {
        val scope = requirePair()
        store.transaction {
            val cp = checkpoint(scope)
            protocolCheck(cp.flag("review") != true && cp.flag("recovery") != true, "Recovery blocks drafts")
            val epoch = cp.text("epoch")
            val peer = cp.text("peer")
            if (epoch != null && peer != null) put(scope, "drafts:$epoch", "$kind:$objectId", buildJsonObject {
                value.forEach { (key, element) -> put(key, element) }
                put("peer", peer)
                put("savedAt", now())
            })
        }
        publish()
    }

    suspend fun clearDraft(kind: String, objectId: String) = mutex.withLock {
        val scope = requirePair()
        store.transaction {
            checkpoint(scope).text("epoch")?.let { epoch ->
                remove(scope, "drafts:$epoch", "$kind:$objectId")
            }
        }
        publish()
    }

    /**
     * Recovery drafts are deliberately separate from ordinary edit drafts:
     * recovery mode blocks normal writes, but a failed reconciliation must
     * still retain its session-scoped fields and preallocated execution ID.
     */
    suspend fun saveRecoveryDraft(draftId: String, value: JsonObject) = mutex.withLock {
        val scope = requirePair()
        store.transaction {
            val cp = checkpoint(scope)
            val epoch = cp.text("epoch")
            val peer = cp.text("peer")
            if (epoch != null && peer != null) put(scope, "drafts:$epoch", draftId, buildJsonObject {
                value.forEach { (key, element) -> put(key, element) }
                put("peer", peer)
                put("savedAt", now())
            })
        }
        publish()
    }

    suspend fun clearRecoveryDraft(draftId: String) = mutex.withLock {
        val scope = requirePair()
        store.transaction {
            checkpoint(scope).text("epoch")?.let { epoch ->
                remove(scope, "drafts:$epoch", draftId)
            }
        }
        publish()
    }

    suspend fun collectRecoveryEvidence(): RecoveryEvidenceCollection = mutex.withLock {
        val scope = requirePair()
        store.transaction {
            val cp = checkpoint(scope)
            protocolCheck(cp.flag("recovery") == true, "工作台未处于恢复模式")
            recoverySources(scope, cp.text("epoch") ?: throw ProtocolException("尚无已确认恢复快照"))
        }
    }

    suspend fun enqueueRecovery(
        command: String, params: JsonObject, expectedPair: String, expectedEvidence: Set<RecoveryEvidenceSource>?,
    ): String = mutex.withLock {
        val scope = requirePair()
        protocolCheck(scope == expectedPair && fresh && peerReady, "恢复需要已连接的当前工作台快照")
        val id = store.transaction {
            val cp = checkpoint(scope)
            protocolCheck(cp.flag("recovery") == true &&
                (command == "get_recovery" || Protocol.commands[command]?.recoveryOnly == true), "工作台未处于恢复模式")
            val epoch = cp.text("epoch") ?: throw ProtocolException("尚无已确认恢复快照")
            val digest = if (command in evidenceGates) {
                val current = recoverySources(scope, epoch)
                protocolCheck(expectedEvidence != null && current.issues.isEmpty() && current.sources.toSet() == expectedEvidence,
                    "本机证据已变化，请重新核对并上传")
                evidenceFingerprint(current.sources)
            } else null
            queue(scope, command, epoch, params, visible = true).also { queued ->
                if (digest != null) put(scope, INTENT, queued, get(scope, INTENT, queued)!!.patch("evidenceFingerprint" to digest))
            }
        }
        publish()
        id
    }

    suspend fun startUpload(
        name: String, mime: String, bytes: ByteArray,
        expectedPairId: String? = null, expectedEpoch: String? = null,
    ): String = mutex.withLock {
        val scope = requirePair()
        val id = store.transaction {
            val binding = transferBinding(scope, expectedPairId, expectedEpoch)
            with(files) { startUpload(scope, binding, name, mime, bytes) }
        }
        publish()
        id
    }

    suspend fun startHoldingImport(
        name: String, mime: String, bytes: ByteArray, noticeVersion: String,
        expectedPairId: String? = null, expectedEpoch: String? = null,
    ): String = mutex.withLock {
        val scope = requirePair()
        val id = store.transaction {
            val binding = transferBinding(scope, expectedPairId, expectedEpoch)
            val previous = get(scope, "holding_import", "current")
            protocolCheck(previous == null || previous.text("status") in setOf("SUCCESS", "INVALID", "FAILED"),
                "A holding import is already running")
            protocolCheck(noticeVersion.isNotBlank(), "Vision notice is unavailable")
            val upload = with(files) { startUpload(scope, binding, name, mime, bytes) }
            val authorization = queue(scope, "accept_vision_notice", binding.epoch, EMPTY.patch(
                "noticeVersion" to noticeVersion, "accepted" to true), visible = false)
            val id = newId()
            put(scope, "holding_import", "current", EMPTY.patch(
                "id" to id, "importId" to id, "transferId" to upload, "status" to "UPLOADING",
                "peer" to binding.peer, "epoch" to binding.epoch, "install" to binding.install,
                "noticeVersion" to noticeVersion, "authorization" to authorization,
            ))
            id
        }
        publish()
        id
    }

    /** New explicit recognition intent for the exact saved file; never retries an uncertain write. */
    suspend fun retryHoldingImport(
        noticeVersion: String, expectedPairId: String? = null, expectedEpoch: String? = null,
    ): String = mutex.withLock {
        val scope = requirePair()
        val id = store.transaction {
            val binding = transferBinding(scope, expectedPairId, expectedEpoch)
            val previous = get(scope, "holding_import", "current") ?: throw ProtocolException("No holding image to retry")
            protocolCheck(canRetryHoldingImport(scope, previous), "Holding image cannot safely be retried")
            protocolCheck(noticeVersion.isNotBlank(), "Vision notice is unavailable")
            val authorization = queue(scope, "accept_vision_notice", binding.epoch, EMPTY.patch(
                "noticeVersion" to noticeVersion, "accepted" to true), visible = false)
            val id = newId()
            put(scope, "holding_import", "current", EMPTY.patch(
                "id" to id, "importId" to id, "transferId" to previous.text("transferId"), "status" to "AUTHORIZING",
                "peer" to binding.peer, "epoch" to binding.epoch, "install" to binding.install,
                "noticeVersion" to noticeVersion, "authorization" to authorization,
            ))
            id
        }
        publish()
        id
    }

    private suspend fun WorkbenchTransaction.canRetryHoldingImport(scope: String, row: JsonObject): Boolean {
        if (row.text("status") !in setOf("FAILED", "INVALID")) return false
        val cp = checkpoint(scope)
        if (cp.flag("review") == true || cp.flag("recovery") == true || row.text("epoch") != cp.text("epoch") ||
            row.text("epoch") != cp.text("announcedEpoch") || row.text("peer") != cp.text("peer") || row.text("install") != identity()) return false
        val transfer = with(files) { views(scope) }.firstOrNull { it.id == row.text("transferId") } ?: return false
        if (transfer.status != FileTransferStatus.COMPLETE) return false
        val fileId = transfer.fileId ?: return false
        val epoch = row.text("epoch")!!
        val importId = row.text("importId")!!
        val result = latestImportResult(scope, epoch, importId)
        if (result?.text("fileStatus") in unavailableOriginalStates) return false
        val listed = get(scope, "reads:$epoch", "list_files")?.obj("result")?.array("items")
        if (listed?.any { file ->
                file.jsonObject.text("fileId") == fileId && file.jsonObject.text("status") in unavailableOriginalStates
            } == true || list(scope, INTENT).values.any { intent ->
                val ack = intent.obj("ack")
                val data = ack?.obj("data")
                val deleted = data?.obj("result")?.obj("file")
                ack?.text("serverEpoch") == epoch && data?.text("cmd") == "delete_file" && data.flag("ok") == true &&
                    deleted?.text("fileId") == fileId && deleted.text("status") == "deleted"
            }) return false
        if (result?.text("status") == "confirmed" ||
            cp.array("confirmedImports")?.any { it.jsonPrimitive.content == importId } == true) return false
        if (result?.text("status") == "recognized") return false
        val registrationId = row.text("registration") ?: return true // No registration could have been sent.
        val registration = get(scope, INTENT, registrationId) ?: return false
        if (registration.obj("ack")?.obj("data")?.flag("ok") == false) return true
        // Relay schema validation rejects this exact delivery before forwarding it to the service.
        if (registration.stage() == DeliveryStage.QUARANTINED && registration["ack"] == null &&
            registration.text("completionError") == "INVALID_FRAME") return true
        return result?.text("status") in setOf("failed", "rejected", "cancelled")
    }

    private suspend fun WorkbenchTransaction.latestImportResult(scope: String, epoch: String, id: String): JsonObject? =
        listOfNotNull(get(scope, "entity:$epoch:imports", id), get(scope, "completions:$epoch:import_result", id))
            .maxByOrNull { it.number("importVersion") ?: -1L }

    private suspend fun WorkbenchTransaction.advanceHoldingImport(scope: String) {
        var row = get(scope, "holding_import", "current") ?: return
        val cp = checkpoint(scope)
        fun failed(code: String) = row.patch("status" to if (code in setOf(
            "IMPORT_CONTENT_INVALID", "FILE_INVALID", "INVALID_IMAGE", "UNSUPPORTED_MEDIA_TYPE", "FILE_TOO_LARGE",
            "IMAGE_INVALID", "IMAGE_TOO_LARGE", "FILE_TYPE_MISMATCH",
        )) "INVALID" else "FAILED", "errorCode" to code)
        if (row.text("epoch") != cp.text("announcedEpoch") || row.text("peer") != cp.text("peer") ||
            row.text("install") != identity()) {
            put(scope, "holding_import", "current", failed("TRANSFER_IDENTITY_CHANGED"))
            return
        }
        if (row.text("status") in setOf("SUCCESS", "INVALID", "FAILED")) return
        if (!fresh || !peerReady || cp.flag("review") == true || cp.flag("recovery") == true) return
        val transfer = with(files) { views(scope) }.firstOrNull { it.id == row.text("transferId") } ?: return
        if (transfer.status in setOf(FileTransferStatus.FAILED, FileTransferStatus.EXPIRED,
                FileTransferStatus.CANCELLED, FileTransferStatus.QUARANTINED)) {
            val code = if (transfer.status == FileTransferStatus.CANCELLED) "IMPORT_CANCELLED"
                else transfer.errorCode ?: "UPLOAD_FAILED"
            put(scope, "holding_import", "current", failed(code))
            return
        }
        if (transfer.status != FileTransferStatus.COMPLETE) return
        val authId = row.text("authorization")
        if (authId == null) {
            put(scope, "holding_import", "current", failed("VISION_CONSENT_REQUIRED"))
            return
        }
        val auth = get(scope, INTENT, authId) ?: return
        if (auth.stage() in setOf(DeliveryStage.REJECTED, DeliveryStage.QUARANTINED)) {
            put(scope, "holding_import", "current", failed(auth.obj("ack")?.obj("data")?.obj("error")?.text("code") ?: "VISION_CONSENT_REQUIRED"))
            return
        }
        if (auth.obj("ack")?.obj("data")?.flag("ok") != true) {
            put(scope, "holding_import", "current", row.patch("status" to "AUTHORIZING"))
            return
        }
        val registrationId = row.text("registration")
        if (registrationId == null) {
            val id = queue(scope, "register_image", row.text("epoch"), EMPTY.patch(
                "fileId" to transfer.fileId, "importId" to row.text("importId"), "autoApply" to true), visible = false)
            put(scope, "holding_import", "current", row.patch("registration" to id, "status" to "RECOGNIZING"))
            return
        }
        val registration = get(scope, INTENT, registrationId) ?: return
        if (registration.stage() in setOf(DeliveryStage.REJECTED, DeliveryStage.QUARANTINED)) {
            put(scope, "holding_import", "current", failed(registration.text("completionError") ?:
                registration.obj("ack")?.obj("data")?.obj("error")?.text("code") ?: "RECOGNITION_FAILED"))
            return
        }
        val epoch = row.text("epoch")!!
        val importId = row.text("importId")!!
        val result = latestImportResult(scope, epoch, importId) ?: return
        when (result.text("status")) {
            "cancelled" -> row = failed(result.text("errorCode") ?: "IMPORT_CANCELLED")
            "failed", "rejected", "recognized" -> row = failed(result.text("errorCode") ?: "IMPORT_CONTENT_INVALID")
            "confirmed" -> {
                // The snapshot must prove both this confirmation and the refreshed position collection.
                if (cp.array("confirmedImports")?.any { it.jsonPrimitive.content == importId } == true) {
                    row = row.patch("status" to "SUCCESS")
                } else if (row.flag("refreshRequested") != true) {
                    row = row.patch("status" to "SYNCING", "refreshRequested" to true)
                    saveCheckpoint(scope, checkpoint(scope).patch("refreshRequested" to true))
                    ensureSync(scope)
                }
            }
        }
        put(scope, "holding_import", "current", row)
    }

    suspend fun startDownload(
        fileId: String, expectedPairId: String? = null, expectedEpoch: String? = null,
    ): String = mutex.withLock {
        val scope = requirePair()
        val id = store.transaction {
            val binding = transferBinding(scope, expectedPairId, expectedEpoch)
            with(files) { startDownload(scope, binding, fileId) }
        }
        publish()
        id
    }

    suspend fun cancelTransfer(
        id: String, expectedPairId: String? = null, expectedEpoch: String? = null,
    ): Unit = mutex.withLock {
        val scope = requirePair()
        store.transaction {
            val binding = transferBinding(scope, expectedPairId, expectedEpoch)
            with(files) { cancel(scope, binding, id) }
        }
        publish()
    }

    suspend fun readDownloaded(
        id: String, expectedPairId: String? = null, expectedEpoch: String? = null,
    ): ByteArray? = mutex.withLock {
        val scope = pair ?: return@withLock null
        store.transaction {
            val binding = try { transferBinding(scope, expectedPairId, expectedEpoch) }
                catch (_: ProtocolException) { return@transaction null }
            with(files) { readDownload(scope, binding, id) }
        }
    }

    private suspend fun WorkbenchTransaction.transferBinding(
        scope: String, expectedPairId: String? = null, expectedEpoch: String? = null,
    ): TransferBinding {
        val cp = checkpoint(scope)
        val epoch = cp.text("epoch")
        val peer = cp.text("peer")
        protocolCheck((expectedPairId == null || expectedPairId == scope) &&
            (expectedEpoch == null || expectedEpoch == epoch) && epoch != null && peer != null &&
            epoch == cp.text("announcedEpoch") && cp.flag("recovery") != true && cp.flag("review") != true,
            "File transfer is unavailable for this pairing")
        return TransferBinding(peer!!, epoch!!, identity())
    }

    suspend fun refresh() = mutex.withLock {
        val scope = pair ?: return@withLock
        if (peerReady) store.transaction { ensureSync(scope) }
        publish()
    }

    suspend fun requestAuxiliary(command: String, expectedPair: String) = mutex.withLock {
        val scope = pair ?: return@withLock
        if (scope != expectedPair || command !in auxiliaryCommands || !peerReady || !fresh) return@withLock
        store.transaction {
            val cp = checkpoint(scope)
            val epoch = cp.text("epoch") ?: return@transaction
            if (cp.flag("review") == true || cp.flag("recovery") == true) return@transaction
            val pending = list(scope, OUTBOX).keys.any { id ->
                val data = get(scope, INTENT, id)?.obj("request")?.obj("data")
                data?.text("cmd") == command && (command != "list_reports" || data.flag("subscribe") == true)
            }
            val params = if (command == "list_reports") EMPTY.patch("subscribe" to true, "limit" to 10) else EMPTY
            if (!pending) queue(scope, command, epoch, params, visible = false)
        }
    }

    suspend fun cancelUnsent(id: String): Boolean = mutex.withLock {
        val scope = requirePair()
        val cancelled = store.transaction {
            val intent = get(scope, INTENT, id) ?: return@transaction false
            val outbox = get(scope, OUTBOX, id) ?: return@transaction false
            if (intent.stage() != DeliveryStage.PENDING || outbox.number("attempts") != 0L) return@transaction false
            put(scope, INTENT, id, intent.withStage(DeliveryStage.CANCELLED))
            remove(scope, OUTBOX, id)
            true
        }
        publish()
        cancelled
    }

    suspend fun checkOriginal(id: String) = mutex.withLock {
        val scope = requirePair()
        protocolCheck(peerReady, "Peer offline")
        store.transaction {
            val intent = get(scope, INTENT, id) ?: throw ProtocolException("Missing original intent")
            val original = Protocol.business(intent.obj("request")!!)
            val epoch = original.epoch ?: throw ProtocolException("Original epoch unknown")
            queue(scope, "get_command_status", checkpoint(scope).text("epoch"), buildJsonObject {
                put("originalEpoch", epoch)
                put("targetClientMsgId", id)
                put("targetRequestHash", intent.text("requestHash")!!)
            }, visible = false)
        }
    }

    suspend fun markReviewed(id: String) = mutex.withLock {
        val scope = requirePair()
        store.transaction {
            val intent = get(scope, INTENT, id) ?: throw ProtocolException("Missing original intent")
            protocolCheck(intent.stage() == DeliveryStage.QUARANTINED && intent.flag("visible") == true,
                "Intent is not awaiting manual review")
            put(scope, INTENT, id, intent.patch("reviewed" to true))
        }
        publish()
    }

    suspend fun finishReview() = mutex.withLock {
        val scope = requirePair()
        protocolCheck(fresh && peerReady, "Verify a fresh snapshot before ending review")
        store.transaction {
            val cp = checkpoint(scope)
            protocolCheck(cp.flag("recovery") == false && cp.flag("review") == true, "Service remains in recovery")
            protocolCheck(list(scope, INTENT).values.none {
                it.stage() == DeliveryStage.QUARANTINED && it.flag("visible") == true && it.flag("reviewed") != true
            }, "Quarantined intents require manual review")
            saveCheckpoint(scope, cp.patch("review" to false))
        }
        publish()
    }

    suspend fun claimReminder(
        epoch: String, reminderId: String, taskId: String, validUntil: Long, expectedPair: String? = null,
    ): Boolean = mutex.withLock {
        val scope = pair ?: return@withLock false
        if ((expectedPair != null && expectedPair != scope) || !fresh || !peerReady || now() >= validUntil) return@withLock false
        store.transaction {
            val cp = checkpoint(scope)
            if (cp.text("epoch") != epoch || cp.flag("recovery") == true || cp.flag("review") == true) return@transaction false
            val group = "notification_dedupe:$epoch"
            if (get(scope, group, reminderId) != null) return@transaction false
            put(scope, group, reminderId, EMPTY.patch("taskId" to taskId, "displayUntil" to validUntil, "peer" to cp.text("peer")))
            true
        }
    }

    suspend fun reminderDestination(expectedPair: String, epoch: String, reminderId: String): ReminderDestination? =
        mutex.withLock {
            if (pair != expectedPair) return@withLock null
            store.transaction {
                val checkpoint = checkpoint(expectedPair)
                if (checkpoint.text("epoch") != epoch || checkpoint.flag("recovery") == true ||
                    checkpoint.flag("review") == true) return@transaction null
                val link = get(expectedPair, "notification_dedupe:$epoch", reminderId) ?: return@transaction null
                val peer = link.text("peer") ?: return@transaction null
                if (peer != checkpoint.text("peer")) return@transaction null
                val task = link.text("taskId") ?: return@transaction null
                val until = link.number("displayUntil")?.takeIf { now() < it } ?: return@transaction null
                ReminderDestination(expectedPair, epoch, task, until)
            }
        }

    suspend fun reportNotification(expectedPair: String, epoch: String, reportId: String, claim: Boolean): ReportNotice? =
        mutex.withLock {
            if (pair != expectedPair) return@withLock null
            val link = store.transaction {
                val cp = checkpoint(expectedPair)
                if (cp.text("epoch") != epoch || cp.text("announcedEpoch") != epoch ||
                    cp.flag("recovery") == true || cp.flag("review") == true) return@transaction null
                val group = "report_notices:$epoch"
                val row = get(expectedPair, group, reportId) ?: return@transaction null
                if (cp.text("peer") != row.text("peer")) return@transaction null
                val until = row.number("displayUntil")?.takeIf { now() < it } ?: return@transaction null
                val wasClaimed = row.flag("claimed") == true
                if (claim) {
                    if (wasClaimed || !fresh || !peerReady) return@transaction null
                    put(expectedPair, group, reportId, row.patch("claimed" to true))
                } else if (!wasClaimed) return@transaction null
                ReportNotice(reportId, until)
            }
            if (claim && link != null) publish()
            link
        }

    suspend fun nextFrames(limit: Int = 8): List<RelayFrame> = mutex.withLock {
        val scope = pair ?: return@withLock emptyList()
        if (!peerReady || limit <= 0) return@withLock emptyList()
        val (frames, transferChanged) = store.transaction {
            val cp = checkpoint(scope)
            val at = now()
            val binding = if (fresh && cp.flag("review") != true && cp.flag("recovery") != true)
                transferBinding(scope) else null
            var recordsChanged = binding != null && with(files) { prepare(scope, binding) }
            val fileLimit = if (limit == 1 && !preferFiles) 0 else (limit - 1).coerceAtLeast(1)
            val fileFrames = if (binding == null || fileLimit == 0) emptyList() else
                with(files) { outgoing(scope, binding, fileLimit) }
            val frames = mutableListOf<RelayFrame>()
            val blockedLanes = mutableSetOf<String>()
            val queue = list(scope, OUTBOX).entries.sortedWith(compareBy(
                { it.value.number("queuedAt") }, { it.value.number("order") },
            ))
            for ((id, row) in queue) {
                if (frames.size == limit - fileFrames.size) break
                val frame = Protocol.frame(row.text("bytes")!!)
                val message = frame.message!!
                val rule = Protocol.commands.getValue(message.command!!)
                val intent = get(scope, INTENT, id)!!
                if (intent.flag("fileTransfer") == true && binding == null) continue
                if (rule.writes) {
                    val lane = lane(message)
                    if (!blockedLanes.add(lane)) continue
                    val normal = fresh && cp.flag("review") != true && cp.flag("recovery") != true
                    val recovery = fresh && cp.flag("recovery") == true && rule.duringRecovery
                    if (!(normal || recovery) || cp.text("epoch") != message.epoch) continue
                }
                if ((row.number("retryAt") ?: 0) > at) continue
                val attempt = row.number("attempts")!! + 1
                val frozenEvidence = intent.text("evidenceFingerprint")
                if (attempt == 1L && frozenEvidence != null) {
                    val current = recoverySources(scope, message.epoch!!)
                    if (current.issues.isNotEmpty() || evidenceFingerprint(current.sources) != frozenEvidence) {
                        put(scope, INTENT, id, intent.withStage(DeliveryStage.REJECTED)
                            .patch("completionError" to "RECOVERY_EVIDENCE_CHANGED"))
                        remove(scope, OUTBOX, id)
                        recordsChanged = true
                        continue
                    }
                }
                put(scope, OUTBOX, id, row.patch("retryAt" to (at + if (attempt == 1L) 15_000 else 30_000),
                    "attempts" to attempt))
                if (intent.stage() == DeliveryStage.PENDING) put(scope, INTENT, id, intent.withStage(DeliveryStage.SENT))
                frames.add(frame)
            }
            (frames + fileFrames) to recordsChanged
        }
        preferFiles = !preferFiles
        if (frames.isNotEmpty() || transferChanged) publish()
        frames
    }

    suspend fun pendingReceipts(): List<DeliveryKey> = mutex.withLock {
        val scope = pair ?: return@withLock emptyList()
        if (!peerReady) return@withLock emptyList()
        store.transaction {
            val peer = checkpoint(scope).text("peer")
            list(scope, RECEIPTS).values.mapNotNull { row ->
                Protocol.frame(row.text("bytes")!!).delivery?.takeIf { it.from == peer }
            }.take(16)
        }
    }

    suspend fun receiptSent(key: DeliveryKey) = mutex.withLock {
        val scope = requirePair()
        val remaining = store.transaction {
            remove(scope, RECEIPTS, "${key.from}:${key.id}")
            list(scope, RECEIPTS).isNotEmpty()
        }
        if (!remaining && mutableState.value.hasPendingReceipts) {
            mutableState.value = mutableState.value.copy(hasPendingReceipts = false)
        }
    }

    suspend fun receive(frame: RelayFrame) = mutex.withLock {
        val scope = requirePair()
        protocolCheck(peerReady, "Unexpected data before peer presence")
        val publishedSnapshot = store.transaction {
            val cp = checkpoint(scope)
            val peer = cp.text("peer")
            val install = identity()
            when (frame.type) {
                FrameType.DELIVER -> {
                    val key = frame.delivery!!
                    protocolCheck(key.from == peer && key.to == install, "Wrong delivery identity")
                    val message = frame.message
                    if (message == null) {
                        with(files) { receiveChunk(scope, TransferBinding(peer!!, cp.text("announcedEpoch")!!, install), frame) }
                        false
                    } else {
                        protocolCheck(message.kind != "command", "Wrong business direction")
                        persistDelivery(scope, key, message)
                        advanceSync(scope)
                    }
                }
                FrameType.RECEIPT -> {
                    val key = frame.delivery!!
                    protocolCheck(key.from == install && key.to == peer, "Wrong receipt identity")
                    val row = list(scope, OUTBOX).entries.firstOrNull {
                        it.value.text("deliveryId") == key.id
                    } ?: run {
                        with(files) { receiveReceipt(scope, key) }
                        return@transaction false
                    }
                    val expected = Protocol.frame(row.value.text("bytes")!!).delivery
                    protocolCheck(key == expected, "Receipt hash mismatch")
                    val intent = get(scope, INTENT, row.key)!!
                    put(scope, INTENT, row.key, intent.withStage(DeliveryStage.DELIVERED))
                    // Preserve immutable retry material until the business ack.
                    false
                }
                FrameType.ERROR -> {
                    handleTransportError(scope, frame.json)
                    false
                }
                else -> throw ProtocolException("Unexpected data frame")
            }
        }
        if (publishedSnapshot) fresh = true
        publish()
    }

    private suspend fun WorkbenchTransaction.persistDelivery(scope: String, key: DeliveryKey, message: BusinessMessage) {
        val receiptId = "${key.from}:${key.id}"
        val prior = get(scope, INBOX, receiptId)
        protocolCheck(prior == null || (prior.flag("fileChunk") != true && prior.text("hash") == key.hash), "Delivery ID reused")
        if (prior == null) {
            put(scope, INBOX, receiptId, buildJsonObject {
                put("hash", key.hash); put("receivedAt", now()); put("payload", message.json)
            })
            val eventId = "${key.from}:${message.epoch}:${message.id}"
            val dedupeId = "${key.from}:${message.epoch}:${message.dedupeKey}"
            val existing = get(scope, EVENTS, eventId)
            val priorDedupe = get(scope, DEDUPE, dedupeId)
            protocolCheck(existing == null || existing == message.json, "Event ID reused")
            protocolCheck(priorDedupe == null || priorDedupe.text("event") == eventId, "Business dedupe conflict")
            if (existing == null) {
                put(scope, EVENTS, eventId, message.json)
                put(scope, DEDUPE, dedupeId, EMPTY.patch("event" to eventId))
                if (message.kind == "ack") applyAck(scope, message)
                else applyEvent(scope, message)
            }
        }
        put(scope, RECEIPTS, receiptId, EMPTY.patch("bytes" to Protocol.receipt(key)))
    }

    private suspend fun WorkbenchTransaction.applyAck(scope: String, ack: BusinessMessage) {
        val id = ack.clientMsgId!!
        val intent = get(scope, INTENT, id) ?: return
        val request = Protocol.business(intent.obj("request")!!)
        if (request.id != ack.data.text("refId") || request.command != ack.command ||
            intent.text("requestHash") != ack.data.text("requestHash")) return
        val cp = checkpoint(scope)
        val ok = ack.data.flag("ok")!!
        val result = ack.data.obj("result")
        val error = ack.data.obj("error")?.text("code")
        if (intent.stage() == DeliveryStage.QUARANTINED) {
            if (ack.epoch == request.epoch && error != "SERVER_EPOCH_MISMATCH") {
                put(scope, INTENT, id, JsonObject(intent + ("recoveredAck" to ack.json)).patch("queryState" to "found"))
            }
            return
        }
        if (ack.epoch != cp.text("announcedEpoch") || request.epoch?.let { ack.epoch != it } == true) return
        if (intent["ack"] != null) return
        val fileTransfer = intent.flag("fileTransfer") == true
        val rule = Protocol.commands.getValue(ack.command!!)
        val stage = when {
            !ok -> DeliveryStage.REJECTED
            result?.text("positionEffect") == "needs_reconciliation" -> DeliveryStage.NEEDS_RECONCILIATION
            ack.command == "chat" || ack.command == "register_image" -> DeliveryStage.ACCEPTED
            rule.writes && !fileTransfer -> DeliveryStage.WAITING_FOR_SNAPSHOT
            else -> DeliveryStage.APPLIED
        }
        var updated = JsonObject(intent + ("ack" to ack.json)).withStage(stage)
        if (stage == DeliveryStage.ACCEPTED) {
            previousCompletion(scope, request)?.let { updated = applyCompletion(updated, it) }
        }
        put(scope, INTENT, id, updated)
        remove(scope, OUTBOX, id)
        if (fileTransfer) {
            with(files) { onAck(scope, request, ack) }
            return
        }
        when (ack.command) {
            "sync" -> if (!ok && cp.text("sync") == id) {
                clear(scope, STAGED)
                saveCheckpoint(scope, cp.patch("sync" to null, "syncError" to (error ?: "SYNC_FAILED")))
            }
            "get_command_status" -> if (ok) {
                val target = request.data.text("targetClientMsgId")!!
                get(scope, INTENT, target)?.let {
                    put(scope, INTENT, target, it.patch("queryState" to if (it.obj("recoveredAck") != null) "found" else result?.text("state")))
                }
            }
            "get_messages" -> if (ok && result != null) {
                for (item in result.array("items")!!) {
                    val row = item.jsonObject
                    put(scope, "messages:${ack.epoch}", row.text("messageId")!!, row)
                }
                publishRead(scope, ack, result)
            }
            else -> if (result != null && (!rule.writes || ack.command == "scan_now" || ack.command == "apply_import")) {
                publishRead(scope, ack, result)
            }
        }
        if (rule.writes && ok) {
            // A snapshot already in flight may have been captured before this mutation.
            saveCheckpoint(scope, checkpoint(scope).patch("refreshRequested" to true))
            ensureSync(scope)
        }
    }

    private suspend fun WorkbenchTransaction.publishRead(scope: String, ack: BusinessMessage, result: JsonObject) {
        val group = "reads:${ack.epoch}"
        val command = ack.command!!
        val previous = get(scope, group, command)
        if (previous == null || ack.sequence!! >= previous.number("sequence")!!) {
            put(scope, group, command, JsonObject(mapOf("result" to result, "sequence" to JsonPrimitive(ack.sequence))))
        }
    }

    private suspend fun WorkbenchTransaction.previousCompletion(scope: String, request: BusinessMessage): JsonObject? {
        val epoch = request.epoch ?: return null
        val (kind, id) = if (request.command == "chat") "reply" to request.clientMsgId!! else
            "import_result" to request.data.text("importId")!!
        return get(scope, "completions:$epoch:$kind", id)
    }

    private suspend fun WorkbenchTransaction.applyEvent(scope: String, message: BusinessMessage) {
        val cp = checkpoint(scope)
        if (message.epoch != cp.text("announcedEpoch")) return
        val epoch = message.epoch!!
        when (message.kind) {
            "task", "task_update", "reminder" -> if (epoch == cp.text("epoch") && message.sequence!! > cp.number("sequence")!!) {
                mergeEntity(scope, epoch, "tasks", "taskId", "version", message.data)
            }
            "import_result" -> {
                val id = message.data.text("importId")!!
                val version = message.data.number("importVersion")!!
                val group = "completions:$epoch:import_result"
                val snapshotVersion = get(scope, "entity:$epoch:imports", id)?.number("importVersion") ?: -1L
                if (epoch == cp.text("epoch") && message.sequence!! > cp.number("sequence")!!) {
                    mergeEntity(scope, epoch, "imports", "importId", "importVersion", message.data)
                }
                val completedVersion = get(scope, group, id)?.number("importVersion") ?: -1L
                if (message.data.text("status") != "pending" && version >= snapshotVersion && version > completedVersion) {
                    put(scope, group, id, message.data)
                    completeAsync(scope, "register_image", epoch, message.data)
                }
            }
            "reply" -> {
                put(scope, "replies:$epoch", message.id, message.data.patch("receivedAt" to message.timestamp))
                put(scope, "completions:$epoch:reply", message.data.text("requestId")!!, message.data)
                completeAsync(scope, "chat", epoch, message.data)
            }
            "scan_result" -> {
                val readGroup = "reads:$epoch"
                val previous = get(scope, readGroup, "scan_result")
                if (previous == null || message.sequence!! >= previous.number("sequence")!!) {
                    put(scope, readGroup, "scan_result", JsonObject(mapOf(
                        "result" to message.data, "sequence" to JsonPrimitive(message.sequence),
                    )))
                }
            }
            "analysis_report" -> {
                if (cp.flag("recovery") == true || cp.flag("review") == true) return
                val header = message.data.obj("header") ?: return
                val reportId = header.text("reportId") ?: return
                val sequence = message.sequence!!
                val readGroup = "reads:$epoch"
                val previous = get(scope, readGroup, "analysis_report")
                if (previous == null || sequence >= previous.number("sequence")!!) {
                    put(scope, readGroup, "analysis_report", buildJsonObject {
                        put("result", message.data)
                        put("sequence", sequence)
                    })
                }
                val deadline = message.data.number("notifyUntil") ?: return
                val validUntil = header.number("validUntil") ?: return
                val until = minOf(deadline, validUntil)
                if (header.text("state") != "current" || now() >= until || message.timestamp >= until) return
                val group = "report_notices:$epoch"
                for ((id, row) in list(scope, group)) {
                    if ((row.number("displayUntil") ?: 0) <= now()) remove(scope, group, id)
                }
                if (get(scope, group, reportId) == null) {
                    put(scope, group, reportId, EMPTY.patch("peer" to cp.text("peer"), "displayUntil" to until))
                }
            }
        }
    }

    private suspend fun WorkbenchTransaction.completeAsync(scope: String, command: String, epoch: String, data: JsonObject) {
        for ((id, row) in list(scope, INTENT)) {
            if (row.stage() != DeliveryStage.ACCEPTED) continue
            val request = row.obj("request")!!
            val matches = when (command) {
                "chat" -> id == data.text("requestId")
                else -> data.text("importId") == request.obj("data")?.text("importId")
            }
            if (matches && request.text("serverEpoch") == epoch && request.obj("data")?.text("cmd") == command) {
                put(scope, INTENT, id, applyCompletion(row, data))
            }
        }
    }

    private fun applyCompletion(intent: JsonObject, data: JsonObject): JsonObject {
        val stage = when (data.text("status")) {
            "failed", "rejected" -> DeliveryStage.REJECTED
            "cancelled" -> DeliveryStage.CANCELLED
            else -> if (data.text("errorCode") != null) DeliveryStage.REJECTED else DeliveryStage.APPLIED
        }
        return intent.withStage(stage).patch("completionError" to data.text("errorCode"))
    }

    private suspend fun WorkbenchTransaction.mergeEntity(scope: String, epoch: String, collection: String,
                                                         idField: String, version: String, value: JsonObject) {
        val group = "entity:$epoch:$collection"
        val id = value.text(idField)!!
        val previous = get(scope, group, id)
        if (previous == null || value.number(version)!! > previous.number(version)!!) put(scope, group, id, value)
    }

    private suspend fun WorkbenchTransaction.advanceSync(scope: String): Boolean {
        val cp = checkpoint(scope)
        val id = cp.text("sync") ?: return false
        val intent = get(scope, INTENT, id) ?: return false
        val ack = intent.obj("ack")?.let(Protocol::business) ?: return false
        val result = ack.data.obj("result") ?: return false
        val eventId = result.text("pageEventId")!!
        val raw = get(scope, EVENTS, "${cp.text("peer")}:${ack.epoch}:$eventId") ?: return false
        val page = Protocol.business(raw)
        try {
            protocolCheck(SnapshotAssembly.linked(page, ack) && result.text("requestId") == id, "Unlinked snapshot result")
            val index = page.data.number("pageIndex")!!
            val pages = list(scope, STAGED)
            protocolCheck(index == pages.size.toLong(), "Unexpected snapshot page index")
            val request = intent.obj("request")!!.obj("data")!!
            protocolCheck(index == 0L || request.text("snapshotId") == page.data.text("snapshotId"), "Wrong snapshot continuation")
            put(scope, STAGED, index.toString(), raw)
            val snapshot = SnapshotAssembly.assemble((pages.values + raw).map(Protocol::business), now())
            if (snapshot == null) {
                val cursor = page.data.text("nextPageCursor") ?: throw ProtocolException("Incomplete snapshot")
                val next = queue(scope, "sync", ack.epoch, buildJsonObject {
                    put("snapshotId", page.data.text("snapshotId")!!)
                    put("pageCursor", cursor)
                }, visible = false)
                saveCheckpoint(scope, checkpoint(scope).patch("sync" to next))
                return false
            }
            if (cp.text("epoch") == snapshot.epoch) {
                protocolCheck(snapshot.sequence >= cp.number("sequence")!!, "Snapshot sequence went backwards")
            }
            publishSnapshot(scope, cp, snapshot)
            return true
        } catch (_: ProtocolException) {
            clear(scope, STAGED)
            saveCheckpoint(scope, checkpoint(scope).patch("sync" to null, "syncError" to "INVALID_SNAPSHOT"))
            return false
        }
    }

    private suspend fun WorkbenchTransaction.publishSnapshot(scope: String, cp: JsonObject, snapshot: ConfirmedSnapshot) {
        val epoch = snapshot.epoch
        for ((collection, values) in snapshot.records) {
            val group = "entity:$epoch:$collection"
            clear(scope, group)
            for (row in values) put(scope, group, row.text(SnapshotAssembly.collections.getValue(collection))!!, row)
        }
        val refresh = cp.flag("refreshRequested") == true
        var checkpoint = cp.patch("epoch" to epoch, "sequence" to snapshot.sequence, "recovery" to snapshot.recoveryMode,
            "completedAt" to now(), "syncError" to null, "sync" to null, "refreshRequested" to false)
        checkpoint = JsonObject(checkpoint + ("meta" to snapshot.meta) + ("confirmedImports" to JsonArray(
            snapshot.records["imports"].orEmpty().filter { it.text("status") == "confirmed" }
                .map { JsonPrimitive(it.text("importId")!!) })))
        saveCheckpoint(scope, checkpoint)
        clear(scope, STAGED)
        for ((eventKey, raw) in list(scope, EVENTS)) {
            if (eventKey.startsWith("${cp.text("peer")}:") &&
                raw.text("serverEpoch") == epoch && (raw.number("snapshotSeq") ?: -1) > snapshot.sequence &&
                raw.text("kind") in setOf("task", "task_update", "reminder", "import_result")) {
                applyEvent(scope, Protocol.business(raw))
            }
        }
        for ((id, intent) in list(scope, INTENT)) {
            val ack = intent.obj("ack")
            if (intent.stage() == DeliveryStage.WAITING_FOR_SNAPSHOT &&
                ack?.text("serverEpoch") == epoch && (ack.number("snapshotSeq") ?: Long.MAX_VALUE) <= snapshot.sequence) {
                put(scope, INTENT, id, intent.withStage(DeliveryStage.APPLIED))
            }
        }
        if (refresh) ensureSync(scope)
    }

    private suspend fun WorkbenchTransaction.ensureSync(scope: String) {
        val cp = checkpoint(scope)
        cp.text("sync")?.let { current ->
            val at = get(scope, INTENT, current)?.obj("request")?.number("ts") ?: now()
            val deadline = if (at > now()) at else at + 10 * 60_000L
            if (now() < deadline) return
            remove(scope, OUTBOX, current)
        }
        clear(scope, STAGED)
        val id = queue(scope, "sync", cp.text("epoch"), EMPTY, visible = false)
        saveCheckpoint(scope, checkpoint(scope).patch("sync" to id, "refreshRequested" to false))
    }

    private suspend fun WorkbenchTransaction.queue(scope: String, cmd: String, epoch: String?, params: JsonObject,
                                                  visible: Boolean, fileTransfer: Boolean = false): String {
        val cp = checkpoint(scope)
        val peer = cp.text("peer") ?: throw ProtocolException("No known peer")
        val at = now()
        val id = newId()
        val request = Protocol.command(cmd, newId(), id, epoch, at, params)
        val out = Protocol.deliver(request, newId(), identity(), peer)
        val order = (cp.number("queueOrder") ?: 0) + 1
        put(scope, INTENT, id, buildJsonObject {
            put("request", request.json); put("stage", DeliveryStage.PENDING.name)
            put("requestHash", CanonicalJson.hash(request.data)); put("visible", visible)
            if (fileTransfer) put("fileTransfer", true)
        })
        put(scope, OUTBOX, id, buildJsonObject {
            put("bytes", out.bytes); put("queuedAt", at); put("order", order)
            put("retryAt", 0); put("attempts", 0); put("deliveryId", out.delivery!!.id)
        })
        saveCheckpoint(scope, cp.patch("queueOrder" to order))
        return id
    }

    private suspend fun WorkbenchTransaction.handleTransportError(scope: String, frame: JsonObject) {
        val ref = frame.text("refDeliveryId") ?: return
        val (id, out) = list(scope, OUTBOX).entries.firstOrNull { it.value.text("deliveryId") == ref } ?: run {
            with(files) { transportError(scope, ref, frame) }
            return
        }
        if (frame.flag("retryable") == true) {
            val delay = (frame.number("retryAfterMs") ?: 0).coerceIn(1_000, 300_000)
            put(scope, OUTBOX, id, out.patch("retryAt" to maxOf(now() + delay, out.number("retryAt")!!)))
        } else {
            val intent = get(scope, INTENT, id)!!
            put(scope, INTENT, id, intent.withStage(DeliveryStage.QUARANTINED)
                .patch("completionError" to frame.text("code")))
            remove(scope, OUTBOX, id)
            if (intent.flag("fileTransfer") == true) with(files) { quarantineCommand(scope, id, frame.text("code")) }
            val cp = checkpoint(scope)
            if (cp.text("sync") == id) saveCheckpoint(scope, cp.patch("sync" to null, "syncError" to frame.text("code")))
        }
    }

    private suspend fun publish() {
        val scope = pair
        if (scope == null) {
            mutableState.value = WorkbenchProjection(connectionError = error)
            return
        }
        mutableState.value = store.transaction {
            advanceHoldingImport(scope)
            val cp = checkpoint(scope)
            val epoch = cp.text("epoch")
            val review = cp.flag("review") == true
            val recovery = cp.flag("recovery") == true || review
            val commandRows = list(scope, INTENT)
            WorkbenchProjection(
                connection = ConnectionState(true, relay, peerReady, fresh, recovery),
                pairId = scope, epoch = epoch, canQueue = epoch != null && !recovery, requiresReview = review,
                synchronizedAt = cp.number("completedAt"), sequence = cp.number("sequence"), meta = cp.obj("meta"),
                records = if (epoch == null) emptyMap() else SnapshotAssembly.collections.keys.associateWith {
                    list(scope, "entity:$epoch:$it").values.toList()
                },
                reads = if (epoch == null) emptyMap() else list(scope, "reads:$epoch").mapValues { it.value.obj("result")!! },
                drafts = if (epoch == null) emptyMap() else list(scope, "drafts:$epoch")
                    .filterValues { it.text("peer") == cp.text("peer") },
                messages = if (epoch == null) emptyList() else list(scope, "messages:$epoch").values.sortedBy { it.number("at") },
                replies = if (epoch == null) emptyList() else list(scope, "replies:$epoch").values.sortedBy { it.number("receivedAt") },
                intents = commandRows.mapNotNull { (id, row) ->
                    if (row.flag("visible") != true) return@mapNotNull null
                    val request = row.obj("request")!!
                    val data = request.obj("data")!!
                    val ack = row.obj("ack")?.obj("data")
                    IntentView(id, data.text("cmd")!!, data, row.stage(),
                        row.text("completionError") ?: ack?.obj("error")?.text("code"),
                        ack?.obj("result"), request.number("ts")!!, row.text("queryState"), row.flag("reviewed") == true)
                }.sortedBy { it.at },
                connectionError = error, syncError = cp.text("syncError"),
                transfers = with(files) { views(scope) },
                holdingImport = get(scope, "holding_import", "current")?.let { row ->
                    HoldingImportView(row.text("id")!!, HoldingImportStatus.valueOf(row.text("status")!!),
                        row.text("errorCode"), row.text("importId")!!, row.text("transferId")!!, canRetryHoldingImport(scope, row))
                },
                reportNotices = if (epoch == null || !fresh || !peerReady || recovery) emptyList() else
                    list(scope, "report_notices:$epoch").mapNotNull { (id, row) ->
                        val until = row.number("displayUntil") ?: return@mapNotNull null
                        if (row.flag("claimed") == true || cp.text("peer") != row.text("peer") || until <= now()) null
                        else ReportNotice(id, until)
                    },
                serviceRecoveryActive = cp.flag("recovery") == true,
                hasPendingReceipts = list(scope, RECEIPTS).isNotEmpty(),
                peerIncarnation = cp.text("peer"),
                reportReads = reportReads(epoch, commandRows.values),
            )
        }
    }

    private suspend fun WorkbenchTransaction.identity(): String {
        get("", CONTROL, "identity")?.text("id")?.let { return it }
        val id = newId()
        put("", CONTROL, "identity", EMPTY.patch("id" to id))
        return id
    }

    private suspend fun WorkbenchTransaction.archiveRecoveryProjection(scope: String, checkpoint: JsonObject) {
        val epoch = checkpoint.text("epoch") ?: return
        val sequence = checkpoint.number("sequence") ?: return
        val captured = now()
        val from = checkpoint.text("peer")
        val taskEvents = list(scope, EVENTS).asSequence()
            .filter { (key, value) -> key.startsWith("$from:$epoch:") &&
                value.text("kind") in setOf("task", "task_update", "reminder") && (value.number("snapshotSeq") ?: 0L) > sequence }
            .groupBy { it.value.obj("data")?.text("taskId") }
        for ((collection, type) in recoveryCollections) {
            for ((id, record) in list(scope, "entity:$epoch:$collection")) {
                val lastEvent = if (collection == "tasks") taskEvents[id].orEmpty()
                    .filter { it.value.obj("data") == record }
                    .maxOfOrNull { it.value.number("snapshotSeq")!! } else null
                val source = recoverySnapshotEvidence(type, epoch, lastEvent ?: sequence, captured, record)
                put(scope, RECOVERY_ARCHIVE, "$epoch:$type:$id", buildJsonObject {
                    put("sourceType", type); put("payload", source.payload)
                })
            }
        }
    }

    private suspend fun WorkbenchTransaction.recoverySources(scope: String, currentEpoch: String): RecoveryEvidenceCollection {
        val sources = mutableListOf<RecoveryEvidenceSource>()
        val issues = mutableSetOf<String>()
        val oldAcksByBody = list(scope, INBOX).values.asSequence()
            .filter { it.obj("payload")?.text("kind") == "ack" && it.obj("payload")?.text("serverEpoch") != currentEpoch }
            .groupBy { it.obj("payload")!! }
            .mapValues { (_, rows) -> rows.mapNotNull { it.number("receivedAt") }.minOrNull() }
        for (intent in list(scope, INTENT).values) {
            val request = intent.obj("request") ?: run {
                issues += "命令证据缺少原始请求"
                continue
            }
            val epoch = request.text("serverEpoch") ?: continue
            if (epoch == currentEpoch) continue
            val command = request.obj("data")?.text("cmd")
            if (Protocol.commands[command]?.writes != true || command in recoveryProtocolCommands) continue
            val at = request.number("ts")
            if (at == null) issues += "命令证据缺少本机保存时间"
            else sources += RecoveryEvidenceSource("command", at, null, request)
            for (ack in listOfNotNull(intent.obj("ack"), intent.obj("recoveredAck")).distinct()) {
                val captured = oldAcksByBody[ack]
                val sequence = ack.number("snapshotSeq")
                if (captured == null || sequence == null) issues += "回执证据缺少原始接收时间或序号"
                else sources += RecoveryEvidenceSource("ack", captured, sequence, ack)
            }
        }
        for (row in list(scope, RECOVERY_ARCHIVE).values) {
            val payload = row.obj("payload")
            val at = payload?.number("capturedAt")
            val seq = payload?.number("snapshotSeq")
            val type = row.text("sourceType")
            if (payload == null || at == null || seq == null || type == null) issues += "快照证据缺少原始记录"
            else if (payload.text("serverEpoch") != currentEpoch) sources += RecoveryEvidenceSource(type, at, seq, payload)
        }
        return RecoveryEvidenceCollection(sources.distinct(), issues.toList())
    }

    private suspend fun WorkbenchTransaction.checkpoint(scope: String) = get(scope, CONTROL, "checkpoint") ?: EMPTY
    private suspend fun WorkbenchTransaction.saveCheckpoint(scope: String, cp: JsonObject) =
        put(scope, CONTROL, "checkpoint", cp)

    private fun requirePair() = pair ?: throw ProtocolException("No trusted pairing")

    private fun lane(message: BusinessMessage): String {
        val data = message.data
        return listOf("taskId", "symbol", "importId", "fileId", "uploadId", "transferId")
            .firstNotNullOfOrNull { key -> data.text(key)?.let { "$key:$it" } } ?: "workbench"
    }

    private fun JsonObject.stage() = DeliveryStage.valueOf(text("stage")!!)
    private fun JsonObject.withStage(stage: DeliveryStage) = patch("stage" to stage.name)

    private companion object {
        val EMPTY = JsonObject(emptyMap())
        val unresolved = setOf(DeliveryStage.PENDING, DeliveryStage.SENT, DeliveryStage.DELIVERED,
            DeliveryStage.ACCEPTED, DeliveryStage.WAITING_FOR_SNAPSHOT)
        const val CONTROL = "sync_checkpoint"
        const val INTENT = "command_intents"
        const val OUTBOX = "transport_outbox"
        const val INBOX = "transport_inbox"
        const val RECEIPTS = "receipt_outbox"
        const val EVENTS = "events"
        const val DEDUPE = "event_dedupe"
        const val STAGED = "snapshot_staging"
        const val RECOVERY_ARCHIVE = "recovery_snapshot_archive"
        val recoveryCollections = mapOf("tasks" to "task_snapshot", "positions" to "position_snapshot")
        val evidenceGates = setOf("recovery_seal_evidence", "recovery_confirm")
        val recoveryProtocolCommands = setOf("recovery_evidence", "recovery_seal_evidence", "recovery_confirm")
        val auxiliaryCommands = setOf("get_status", "get_messages", "list_files", "list_reports")
        val unavailableOriginalStates = setOf("deleting", "deleted", "missing")
    }
}

private fun evidenceFingerprint(sources: List<RecoveryEvidenceSource>): String {
    val ordered = sources.map { source ->
        CanonicalJson.hash(buildJsonObject {
            put("sourceType", source.sourceType); put("capturedAt", source.capturedAt)
            put("observedSnapshotSeq", source.observedSnapshotSeq); put("payload", source.payload)
        })
    }.sorted().map(::JsonPrimitive)
    return CanonicalJson.hash(JsonArray(ordered))
}

private fun JsonObject.patch(vararg fields: Pair<String, Any?>): JsonObject = JsonObject(this + fields.associate { (key, value) ->
    key to when (value) {
        null -> JsonNull
        is String -> JsonPrimitive(value)
        is Number -> JsonPrimitive(value)
        is Boolean -> JsonPrimitive(value)
        else -> error("Invalid persistent field")
    }
})
