package dev.tickdesk.core

import kotlinx.serialization.json.*
import java.util.Base64

enum class FileTransferKind { UPLOAD, DOWNLOAD }

enum class FileTransferStatus {
    PREPARING, TRANSFERRING, RESUMING, FINALIZING, CANCELLING,
    COMPLETE, CANCELLED, EXPIRED, FAILED, QUARANTINED,
}

data class FileTransferView(
    val id: String,
    val kind: FileTransferKind,
    val status: FileTransferStatus,
    val name: String?,
    val mime: String?,
    val fileId: String?,
    val sizeBytes: Long?,
    val completedChunks: Int,
    val totalChunks: Int?,
    val errorCode: String?,
    val at: Long,
    val needsDelivery: Boolean,
    val canCancel: Boolean,
    val sha256: String?,
    val fileVersion: Long?,
    val file: JsonObject?,
)

internal data class TransferBinding(val peer: String, val epoch: String, val install: String)

/** Invoked under the repository mutex, in the same transaction as the relay outbox/inbox. */
internal class FileTransfers(
    private val now: () -> Long,
    private val newId: () -> String,
    private val enqueue: suspend WorkbenchTransaction.(String, String, String, JsonObject) -> String,
) {
    suspend fun WorkbenchTransaction.startUpload(
        scope: String, binding: TransferBinding, name: String, mime: String, bytes: ByteArray,
    ): String {
        protocolCheck(bytes.size in 1..Protocol.MAX_FILE_BYTES, "Invalid upload size")
        val rows = list(scope, TRANSFERS)
        ensureCapacity(rows)
        val retained = rows.values.filter { it.flag("sourceRetained") == true }.sumOf { it.number("sizeBytes")!! }
        protocolCheck(retained + bytes.size <= MAX_RETAINED_UPLOAD_BYTES, "Local upload storage limit reached")
        val id = newId()
        val hash = CanonicalJson.sha256(bytes)
        val row = initial(binding, FileTransferKind.UPLOAD).update(
            "name" to name, "mime" to mime, "sizeBytes" to bytes.size, "sha256" to hash,
            "totalChunks" to blockCount(bytes.size.toLong()), "sourceRetained" to true,
        )
        queue(scope, id, row, "begin_upload", fields(
            "name" to name, "mime" to mime, "sizeBytes" to bytes.size, "sha256" to hash,
        ))
        for (index in 0 until row.number("totalChunks")!!.toInt()) {
            val start = index * Protocol.CHUNK_SIZE
            val block = bytes.copyOfRange(start, minOf(start + Protocol.CHUNK_SIZE, bytes.size))
            put(scope, blocks(id), index.toString(), fields(
                "bytes" to Base64.getEncoder().encodeToString(block), "chunkHash" to CanonicalJson.sha256(block),
            ))
        }
        return id
    }

    suspend fun WorkbenchTransaction.startDownload(scope: String, binding: TransferBinding, fileId: String): String {
        val rows = list(scope, TRANSFERS)
        rows.entries.firstOrNull { (_, row) ->
            row.boundTo(binding) && row.kind() == FileTransferKind.DOWNLOAD && row.text("fileId") == fileId && row.active()
        }?.let { return it.key }
        ensureCapacity(rows)
        val id = newId()
        queue(scope, id, initial(binding, FileTransferKind.DOWNLOAD).update("fileId" to fileId),
            "read_file", fields("fileId" to fileId))
        return id
    }

    suspend fun WorkbenchTransaction.cancel(scope: String, binding: TransferBinding, id: String) {
        val row = get(scope, TRANSFERS, id) ?: throw ProtocolException("Unknown transfer")
        protocolCheck(row.boundTo(binding) && row.canCancel(), "Transfer cannot be cancelled")
        put(scope, TRANSFERS, id, row.withStatus(FileTransferStatus.CANCELLING).update("cancelRequested" to true))
    }

    suspend fun WorkbenchTransaction.readDownload(scope: String, binding: TransferBinding, id: String): ByteArray? {
        val row = get(scope, TRANSFERS, id) ?: return null
        if (!row.boundTo(binding) || row.kind() != FileTransferKind.DOWNLOAD || row.status() != FileTransferStatus.COMPLETE) return null
        return assembled(scope, id, row)
    }

    suspend fun WorkbenchTransaction.reconnected(scope: String) {
        for ((id, row) in list(scope, TRANSFERS)) {
            if (row.kind() == FileTransferKind.UPLOAD && row.active() && row.text("transferId") != null &&
                row.text("pending") == null && row.flag("cancelRequested") != true) {
                put(scope, TRANSFERS, id, row.withStatus(FileTransferStatus.RESUMING).update("needsQuery" to true))
            }
        }
    }

    suspend fun WorkbenchTransaction.quarantine(scope: String) {
        for ((id, row) in list(scope, TRANSFERS)) {
            if (row.active() || row.status() == FileTransferStatus.COMPLETE || row.canCancel()) {
                terminate(scope, id, row, FileTransferStatus.QUARANTINED, "TRANSFER_IDENTITY_CHANGED")
            }
        }
    }

    suspend fun WorkbenchTransaction.quarantineCommand(scope: String, commandId: String, error: String?) {
        val (id, row) = list(scope, TRANSFERS).entries.firstOrNull { it.value.text("pending") == commandId } ?: return
        terminate(scope, id, row, FileTransferStatus.QUARANTINED, error ?: "TRANSFER_OUTCOME_UNKNOWN")
    }

    suspend fun WorkbenchTransaction.prepare(scope: String, binding: TransferBinding): Boolean {
        var changed = false
        for ((id, row) in list(scope, TRANSFERS)) {
            if (!row.boundTo(binding) || !row.active() || row.text("pending") != null) continue
            val remote = row.text("transferId") ?: continue
            when {
                row.flag("cancelRequested") == true ->
                    queue(scope, id, row.withStatus(FileTransferStatus.CANCELLING), "cancel_transfer",
                        fields("transferId" to remote, "kind" to row.kind().name.lowercase()))
                row.kind() == FileTransferKind.DOWNLOAD -> {
                    if ((row.number("expiresAt") ?: Long.MAX_VALUE) > now()) continue
                    terminate(scope, id, row, FileTransferStatus.EXPIRED, "DOWNLOAD_EXPIRED")
                }
                row.flag("needsQuery") == true ->
                    queue(scope, id, row.withStatus(FileTransferStatus.RESUMING), "get_upload", fields("uploadId" to remote))
                row.indexes().size.toLong() == row.number("totalChunks") ->
                    queue(scope, id, row.withStatus(FileTransferStatus.FINALIZING), "finalize_upload", fields("uploadId" to remote))
                else -> continue
            }
            changed = true
        }
        return changed
    }

    suspend fun WorkbenchTransaction.outgoing(scope: String, binding: TransferBinding, limit: Int): List<RelayFrame> {
        val rows = list(scope, TRANSFERS)
        val outstanding = list(scope, SLOTS)
        val frames = mutableListOf<RelayFrame>()
        val at = now()
        for ((delivery, slot) in outstanding.entries.sortedBy { it.value.number("at") }) {
            if (frames.size == limit) return frames
            val id = slot.text("localId")!!
            val row = rows[id] ?: continue
            if (!row.sending(binding) || (slot.number("retryAt") ?: 0L) > at) continue
            val frame = outgoingFrame(scope, id, row, slot.number("index")!!.toInt(), delivery)
            protocolCheck(frame.delivery!!.hash == slot.text("hash"), "Persisted file chunk changed")
            frames += frame
            put(scope, SLOTS, delivery, slot.update("retryAt" to (at + 30_000)))
        }
        // Slots remain occupied until an exact receipt or the authoritative manifest proves each block.
        for ((id, row) in rows.entries.sortedBy { it.value.number("at") }) {
            if (!row.sending(binding)) continue
            val reserved = outstanding.values.filter { it.text("localId") == id }.map { it.number("index")!!.toInt() }.toSet()
            var available = WINDOW - reserved.size
            val received = row.indexes()
            for (index in 0 until row.number("totalChunks")!!.toInt()) {
                if (frames.size == minOf(limit, WINDOW)) return frames
                if (available <= 0) break
                if (index in received || index in reserved) continue
                val deliveryId = newId()
                val frame = outgoingFrame(scope, id, row, index, deliveryId)
                put(scope, SLOTS, deliveryId, fields(
                    "localId" to id, "index" to index, "hash" to frame.delivery!!.hash, "at" to at,
                    "retryAt" to (at + 15_000),
                ))
                frames += frame
                available--
            }
        }
        return frames
    }

    suspend fun WorkbenchTransaction.receiveReceipt(scope: String, key: DeliveryKey) {
        val slot = get(scope, SLOTS, key.id) ?: return
        val id = slot.text("localId")!!
        val row = get(scope, TRANSFERS, id) ?: return
        protocolCheck(key.hash == slot.text("hash") && key.from == row.text("install") && key.to == row.text("peer"),
            "File receipt identity or hash mismatch")
        if (!row.active() && row.status() != FileTransferStatus.FAILED) return
        put(scope, TRANSFERS, id, row.withIndexes(row.indexes() + slot.number("index")!!.toInt()))
        remove(scope, SLOTS, key.id)
    }

    suspend fun WorkbenchTransaction.transportError(scope: String, deliveryId: String, frame: JsonObject) {
        val slot = get(scope, SLOTS, deliveryId) ?: return
        val id = slot.text("localId")!!
        val row = get(scope, TRANSFERS, id) ?: return
        if (!row.active()) return
        val error = frame.text("code") ?: "TRANSFER_DELIVERY_FAILED"
        if (frame.flag("retryable") == true) {
            val wait = (frame.number("retryAfterMs") ?: 0L).coerceIn(1_000, 300_000)
            put(scope, SLOTS, deliveryId, slot.update("retryAt" to maxOf(slot.number("retryAt")!!, now() + wait)))
        } else if (error == "UPLOAD_NOT_FOUND" || error == "CHUNK_CONFLICT") {
            // The error is not proof of expiry or cancellation. Ask the service for its durable manifest.
            put(scope, SLOTS, deliveryId, slot.update("rejected" to error))
            val next = row.update("needsQuery" to true)
            put(scope, TRANSFERS, id, if (row.flag("cancelRequested") == true) next else next.withStatus(FileTransferStatus.RESUMING))
        } else {
            terminate(scope, id, row, FileTransferStatus.QUARANTINED, error)
        }
    }

    suspend fun WorkbenchTransaction.onAck(scope: String, request: BusinessMessage, ack: BusinessMessage) {
        val (id, original) = list(scope, TRANSFERS).entries.firstOrNull { it.value.text("pending") == request.clientMsgId } ?: return
        if (original.text("epoch") != ack.epoch || !original.active()) return
        val row = original.update("pending" to null, "errorCode" to null)
        val result = ack.data.obj("result")
        if (ack.data.flag("ok") != true || result == null) {
            val code = ack.data.obj("error")?.text("code") ?: "TRANSFER_REJECTED"
            when {
                request.command == "cancel_transfer" -> {
                    // A rejected cancel cannot discard a stream or stop an otherwise valid upload.
                    val next = row.update("cancelRequested" to false, "errorCode" to code)
                    val status = when {
                        row.text("blockedError") != null -> FileTransferStatus.FAILED
                        row.kind() == FileTransferKind.DOWNLOAD && assembled(scope, id, next) != null ->
                            FileTransferStatus.COMPLETE
                        else -> FileTransferStatus.TRANSFERRING
                    }
                    put(scope, TRANSFERS, id, next.withStatus(status))
                    if (status == FileTransferStatus.COMPLETE) trimDownloadCache(scope, id)
                }
                request.command == "finalize_upload" && code == "UPLOAD_EXPIRED" ->
                    put(scope, TRANSFERS, id, row.withStatus(FileTransferStatus.RESUMING)
                        .update("needsQuery" to true, "blockedError" to code, "errorCode" to code))
                else -> fail(scope, id, row, code)
            }
            return
        }
        when (request.command) {
            "begin_upload", "get_upload" -> uploadManifest(scope, id, row, result.obj("upload")!!, request.command == "begin_upload")
            "finalize_upload" -> {
                val file = result.obj("file")!!
                if (!validFile(file, row.text("fileId")) || file.number("sizeBytes") != row.number("sizeBytes") ||
                    file.text("sha256") != row.text("sha256") || file.text("mime") != row.text("mime")) {
                    fail(scope, id, row, "FILE_MANIFEST_MISMATCH")
                } else {
                    terminate(scope, id, row.update("file" to file), FileTransferStatus.COMPLETE)
                }
            }
            "read_file" -> downloadManifest(scope, id, row, result)
            "cancel_transfer" -> {
                if (result.text("transferId") != row.text("transferId"))
                    fail(scope, id, row, "FILE_MANIFEST_MISMATCH")
                else if (result.text("status") == "cancelled")
                    terminate(scope, id, row, FileTransferStatus.CANCELLED)
                else if (row.kind() == FileTransferKind.UPLOAD)
                    put(scope, TRANSFERS, id, row.withStatus(FileTransferStatus.RESUMING)
                        .update("cancelRequested" to false, "needsQuery" to true))
                else {
                    // Service already released the download. Without all verified local bytes we cannot preview it.
                    val next = row.update("cancelRequested" to false)
                    if (assembled(scope, id, next) == null) fail(scope, id, next, "DOWNLOAD_ALREADY_COMPLETE")
                    else {
                        put(scope, TRANSFERS, id, next.withStatus(FileTransferStatus.COMPLETE))
                        trimDownloadCache(scope, id)
                    }
                }
            }
        }
    }

    suspend fun WorkbenchTransaction.receiveChunk(scope: String, binding: TransferBinding, frame: RelayFrame) {
        val key = frame.delivery!!
        val chunk = frame.json.obj("payload")!!
        val rows = list(scope, TRANSFERS)
        val entry = rows.entries.firstOrNull { (_, row) ->
            row.boundTo(binding) && row.kind() == FileTransferKind.DOWNLOAD && row.text("transferId") == chunk.text("transferId")
        }
        if (entry != null) {
            persistDownloadBlock(scope, entry.key, entry.value, key, chunk)
            return
        }
        // The protocol does not require the read ack to arrive before its chunks. Do not receipt an unbound grant.
        val (id, _) = rows.entries.firstOrNull { (_, row) ->
            row.boundTo(binding) && row.kind() == FileTransferKind.DOWNLOAD && row.active() &&
                row.text("transferId") == null && row.text("fileId") == chunk.text("fileId")
        } ?: return
        checkInbox(scope, key)
        val prior = get(scope, early(id), key.id)
        protocolCheck(prior == null || prior.text("hash") == key.hash, "Delivery ID reused")
        if (prior == null && list(scope, early(id)).size < WINDOW) {
            put(scope, early(id), key.id, fields("hash" to key.hash, "chunk" to chunk))
        }
    }

    suspend fun WorkbenchTransaction.views(scope: String): List<FileTransferView> =
        list(scope, TRANSFERS).map { (id, row) ->
            val file = row.obj("file")
            FileTransferView(id, row.kind(), row.status(), row.text("name"), row.text("mime"), row.text("fileId"),
                row.number("sizeBytes"), row.indexes().size, row.number("totalChunks")?.toInt(), row.text("errorCode"),
                row.number("at")!!, row.active(), row.canCancel(), row.text("sha256"), file?.number("version"), file)
        }.sortedByDescending { it.at }

    private suspend fun WorkbenchTransaction.uploadManifest(
        scope: String, id: String, row: JsonObject, manifest: JsonObject, begins: Boolean,
    ) {
        val total = row.number("totalChunks")!!.toInt()
        val received = manifest.array("receivedChunks")!!.map { it.jsonPrimitive.int }
        val status = manifest.text("status")
        if (manifest.number("sizeBytes") != row.number("sizeBytes") ||
            manifest.number("totalChunks") != total.toLong() || manifest.number("chunkSize") != Protocol.CHUNK_SIZE.toLong() ||
            received.any { it !in 0 until total } || received.toSet().size != received.size ||
            (!begins && (manifest.text("uploadId") != row.text("transferId") || manifest.text("fileId") != row.text("fileId"))) ||
            (status == "complete" && received.size != total)) {
            fail(scope, id, row, "FILE_MANIFEST_MISMATCH")
            return
        }
        val proof = row.indexes() + received
        var next = row.withIndexes(proof).update(
            "transferId" to manifest.text("uploadId"), "fileId" to manifest.text("fileId"),
            "expiresAt" to manifest.number("expiresAt"), "needsQuery" to false,
        )
        when (status) {
            "expired" -> terminate(scope, id, next, FileTransferStatus.EXPIRED, "UPLOAD_EXPIRED")
            "cancelled" -> terminate(scope, id, next, FileTransferStatus.CANCELLED)
            else -> {
                var rejected = row.text("blockedError")
                for ((delivery, slot) in list(scope, SLOTS)) if (slot.text("localId") == id) {
                    if (slot.number("index")!!.toInt() in proof) remove(scope, SLOTS, delivery)
                    else if (slot.text("rejected") != null) rejected = slot.text("rejected")
                }
                if (status == "complete") next = next.update("cancelRequested" to false)
                if (rejected != null && next.flag("cancelRequested") != true) fail(scope, id, next, rejected)
                else put(scope, TRANSFERS, id, next.withStatus(
                    if (next.flag("cancelRequested") == true) FileTransferStatus.CANCELLING else FileTransferStatus.TRANSFERRING,
                ))
            }
        }
    }

    private suspend fun WorkbenchTransaction.downloadManifest(scope: String, id: String, row: JsonObject, result: JsonObject) {
        val file = result.obj("file")!!
        val total = result.number("totalChunks")!!
        if (!validFile(file, row.text("fileId")) || total != blockCount(file.number("sizeBytes")!!).toLong() ||
            result.number("chunkSize") != Protocol.CHUNK_SIZE.toLong()) {
            fail(scope, id, row, "FILE_MANIFEST_MISMATCH")
            return
        }
        val remote = result.text("downloadId")!!
        if (list(scope, TRANSFERS).any { (otherId, other) ->
                otherId != id && other.kind() == FileTransferKind.DOWNLOAD && other.text("peer") == row.text("peer") &&
                    other.text("epoch") == row.text("epoch") && other.text("transferId") == remote
            }) {
            fail(scope, id, row, "FILE_MANIFEST_MISMATCH")
            return
        }
        val next = row.withStatus(if (row.flag("cancelRequested") == true) FileTransferStatus.CANCELLING
            else FileTransferStatus.TRANSFERRING).update(
            "transferId" to remote, "file" to file, "name" to file.text("name"), "mime" to file.text("mime"),
            "sizeBytes" to file.number("sizeBytes"), "sha256" to file.text("sha256"), "totalChunks" to total,
            "expiresAt" to result.number("expiresAt"),
        )
        put(scope, TRANSFERS, id, next)
        for ((delivery, buffered) in list(scope, early(id))) {
            val chunk = buffered.obj("chunk")!!
            if (chunk.text("transferId") == remote) {
                val key = DeliveryKey(delivery, buffered.text("hash")!!, row.text("peer")!!, row.text("install")!!)
                persistDownloadBlock(scope, id, get(scope, TRANSFERS, id)!!, key, chunk)
            }
        }
        clear(scope, early(id))
    }

    private suspend fun WorkbenchTransaction.persistDownloadBlock(
        scope: String, id: String, row: JsonObject, key: DeliveryKey, chunk: JsonObject,
    ) {
        val status = row.status()
        if (!row.active() && status != FileTransferStatus.COMPLETE) return
        if (row.text("blockedError") != null) return
        if (row.text("fileId") != chunk.text("fileId") || row.number("totalChunks") != chunk.number("totalChunks")) return
        if (status != FileTransferStatus.COMPLETE && (row.number("expiresAt") ?: Long.MAX_VALUE) <= now() &&
            row.flag("cancelRequested") != true) {
            terminate(scope, id, row, FileTransferStatus.EXPIRED, "DOWNLOAD_EXPIRED")
            return
        }
        val index = chunk.number("index")!!.toInt()
        val bytes = Protocol.chunkBytes(chunk)
        if (bytes.size.toLong() != blockSize(row.number("sizeBytes")!!, index)) return
        val alreadyAccepted = checkInbox(scope, key)
        val previous = get(scope, blocks(id), index.toString())
        if (previous != null && previous != chunk) return
        if (previous == null) {
            put(scope, blocks(id), index.toString(), chunk)
            val next = row.withIndexes(row.indexes() + index)
            if (next.indexes().size.toLong() == next.number("totalChunks")) {
                if (assembled(scope, id, next) == null) {
                    fail(scope, id, next, "FILE_HASH_MISMATCH")
                    return
                }
                if (next.flag("cancelRequested") != true) {
                    put(scope, TRANSFERS, id, next.withStatus(FileTransferStatus.COMPLETE))
                    trimDownloadCache(scope, id)
                } else put(scope, TRANSFERS, id, next)
            } else put(scope, TRANSFERS, id, next)
        }
        val receiptId = "${key.from}:${key.id}"
        if (!alreadyAccepted) put(scope, INBOX, receiptId, fields("hash" to key.hash, "fileChunk" to true))
        put(scope, RECEIPTS, receiptId, fields("bytes" to Protocol.receipt(key)))
    }

    private suspend fun WorkbenchTransaction.checkInbox(scope: String, key: DeliveryKey): Boolean {
        val prior = get(scope, INBOX, "${key.from}:${key.id}") ?: return false
        protocolCheck(prior.flag("fileChunk") == true && prior.text("hash") == key.hash, "Delivery ID reused")
        return true
    }

    private suspend fun WorkbenchTransaction.assembled(scope: String, id: String, row: JsonObject): ByteArray? {
        val size = row.number("sizeBytes") ?: return null
        val expected = row.text("sha256") ?: return null
        val count = row.number("totalChunks")?.toInt() ?: return null
        if (row.indexes().size != count || size !in 1..Protocol.MAX_FILE_BYTES.toLong()) return null
        val bytes = ByteArray(size.toInt())
        for (index in 0 until count) {
            val chunk = get(scope, blocks(id), index.toString()) ?: return null
            val block = try { Protocol.chunkBytes(chunk) } catch (_: ProtocolException) { return null }
            if (block.size.toLong() != blockSize(size, index)) return null
            block.copyInto(bytes, index * Protocol.CHUNK_SIZE)
        }
        return bytes.takeIf { CanonicalJson.sha256(it) == expected }
    }

    private suspend fun WorkbenchTransaction.trimDownloadCache(scope: String, keep: String) {
        val completed = list(scope, TRANSFERS).entries.filter {
            it.key != keep && it.value.kind() == FileTransferKind.DOWNLOAD && it.value.status() == FileTransferStatus.COMPLETE
        }.sortedByDescending { it.value.number("at") }
        for ((id, row) in completed.drop(CACHE_SIZE - 1)) {
            terminate(scope, id, row, FileTransferStatus.EXPIRED, "LOCAL_CACHE_EVICTED")
        }
    }

    private suspend fun WorkbenchTransaction.outgoingFrame(
        scope: String, id: String, row: JsonObject, index: Int, deliveryId: String,
    ): RelayFrame {
        val block = get(scope, blocks(id), index.toString()) ?: throw ProtocolException("Missing persisted upload block")
        return Protocol.deliverChunk(block.update("transferId" to row.text("transferId"), "fileId" to row.text("fileId"),
            "index" to index, "totalChunks" to row.number("totalChunks")), deliveryId, row.text("install")!!, row.text("peer")!!)
    }

    private suspend fun WorkbenchTransaction.queue(
        scope: String, id: String, row: JsonObject, cmd: String, params: JsonObject,
    ) {
        val command = enqueue(scope, cmd, row.text("epoch")!!, params)
        put(scope, TRANSFERS, id, row.update("pending" to command))
    }

    private suspend fun WorkbenchTransaction.fail(scope: String, id: String, row: JsonObject, code: String) {
        var next = row.update("blockedError" to code, "errorCode" to code)
        if (row.flag("cancelRequested") == true && row.text("transferId") != null) {
            if (row.kind() == FileTransferKind.DOWNLOAD) {
                clear(scope, blocks(id))
                next = next.withIndexes(emptySet())
            }
            // A locally detected error must not consume the pending cancellation intent.
            put(scope, TRANSFERS, id, next.withStatus(FileTransferStatus.CANCELLING))
        } else terminate(scope, id, next, FileTransferStatus.FAILED, code)
    }

    private suspend fun WorkbenchTransaction.terminate(
        scope: String, id: String, row: JsonObject, status: FileTransferStatus, code: String? = null,
    ) {
        val releaseSource = status in setOf(FileTransferStatus.COMPLETE, FileTransferStatus.CANCELLED, FileTransferStatus.EXPIRED)
        val retain = row.flag("sourceRetained") == true && !releaseSource
        if (!retain) clear(scope, blocks(id))
        clear(scope, early(id))
        // Keep unresolved upload slots (including their immutable delivery IDs) until explicit cleanup or identity fencing.
        if (!retain || status == FileTransferStatus.QUARANTINED) {
            for ((delivery, slot) in list(scope, SLOTS)) if (slot.text("localId") == id) remove(scope, SLOTS, delivery)
        }
        put(scope, TRANSFERS, id, row.withStatus(status).update(
            "errorCode" to code, "sourceRetained" to retain, "cancelRequested" to false, "pending" to null,
            "file" to if (status == FileTransferStatus.QUARANTINED) null else row.obj("file"),
        ))
    }

    private fun initial(binding: TransferBinding, kind: FileTransferKind) = fields(
        "peer" to binding.peer, "epoch" to binding.epoch, "install" to binding.install,
        "kind" to kind.name, "status" to FileTransferStatus.PREPARING.name, "at" to now(),
    )

    private fun ensureCapacity(rows: Map<String, JsonObject>) =
        protocolCheck(rows.values.count { it.active() } < 2, "Too many active file transfers")

    private fun JsonObject.boundTo(binding: TransferBinding) =
        text("peer") == binding.peer && text("epoch") == binding.epoch && text("install") == binding.install

    private fun JsonObject.sending(binding: TransferBinding) =
        boundTo(binding) && kind() == FileTransferKind.UPLOAD && status() == FileTransferStatus.TRANSFERRING

    private fun JsonObject.withStatus(status: FileTransferStatus) = update("status" to status.name)
    private fun JsonObject.kind() = FileTransferKind.valueOf(text("kind")!!)
    private fun JsonObject.status() = FileTransferStatus.valueOf(text("status")!!)
    private fun JsonObject.active() = status() in ACTIVE
    private fun JsonObject.canCancel() = status() != FileTransferStatus.CANCELLING &&
        (active() || (status() == FileTransferStatus.FAILED && text("transferId") != null))
    private fun JsonObject.indexes() = array("indexes")?.map { it.jsonPrimitive.int }?.toSet().orEmpty()
    private fun JsonObject.withIndexes(indexes: Set<Int>) = update("indexes" to JsonArray(indexes.sorted().map(::JsonPrimitive)))
    private fun blocks(id: String) = "file_blocks:$id"
    private fun early(id: String) = "early_file_blocks:$id"
    private fun blockCount(size: Long) = ((size + Protocol.CHUNK_SIZE - 1) / Protocol.CHUNK_SIZE).toInt()
    private fun blockSize(size: Long, index: Int) = (size - index * Protocol.CHUNK_SIZE).coerceIn(0L, Protocol.CHUNK_SIZE.toLong())
    private fun validFile(file: JsonObject, fileId: String?) = file.text("fileId") == fileId && file.text("status") == "available" &&
        file["deletedAt"] == JsonNull &&
        file.text("mime") in setOf("image/png", "image/jpeg") && file.number("sizeBytes") in 1..Protocol.MAX_FILE_BYTES.toLong()

    private companion object {
        const val TRANSFERS = "file_transfers"
        const val SLOTS = "upload_chunk_slots"
        const val INBOX = "transport_inbox"
        const val RECEIPTS = "receipt_outbox"
        const val WINDOW = 4
        const val CACHE_SIZE = 2
        const val MAX_RETAINED_UPLOAD_BYTES = 4L * Protocol.MAX_FILE_BYTES
        val ACTIVE = setOf(FileTransferStatus.PREPARING, FileTransferStatus.TRANSFERRING, FileTransferStatus.RESUMING,
            FileTransferStatus.FINALIZING, FileTransferStatus.CANCELLING)
    }
}

private fun fields(vararg values: Pair<String, Any?>): JsonObject = JsonObject(values.associate { (key, value) ->
    key to when (value) {
        null -> JsonNull
        is JsonElement -> value
        is String -> JsonPrimitive(value)
        is Number -> JsonPrimitive(value)
        is Boolean -> JsonPrimitive(value)
        else -> error("Invalid file transfer field")
    }
})

private fun JsonObject.update(vararg values: Pair<String, Any?>) = JsonObject(this + fields(*values))
