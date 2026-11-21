package dev.tickdesk.core

import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Test

class FileTransferTest {
    private var clock = 1794792300000L
    private var id = 0
    private val store = TransferTestStore()
    private val fixtures = contractResource("fixtures").array("cases")!!.associate {
        it.jsonObject.text("name")!! to it.jsonObject.obj("payload")!!
    }
    private val epoch = "epoch_demo_a"
    private val peer = "local"
    private val install = "id_1"

    @Test fun uploadWindowResumesUsingDurableBlocksAndProofBeforeFinalize() = runBlocking {
        val repo = connect()
        val bytes = ByteArray(Protocol.CHUNK_SIZE * 5 + 7) { (it % 97).toByte() }
        val localId = repo.startUpload("sample.png", "image/png", bytes)
        val begin = request(repo, "begin_upload")
        assertEquals(CanonicalJson.sha256(bytes), begin.first.data.text("sha256"))
        acknowledge(repo, begin, upload(bytes.size, emptyList()))
        val first = repo.nextFrames()
        assertEquals(listOf(0, 1, 2, 3), first.map { chunk(it).number("index")!!.toInt() })
        assertTrue(repo.nextFrames().isEmpty())
        receiveReceipt(repo, first[2])
        assertEquals(4L, chunk(repo.nextFrames().single()).number("index"))

        val restarted = connect()
        val query = request(restarted, "get_upload")
        assertTrue(restarted.nextFrames().isEmpty())
        acknowledge(restarted, query, upload(bytes.size, listOf(0, 2, 4)))
        clock += 31_000
        val resumed = restarted.nextFrames()
        assertEquals(listOf(1L, 3L, 5L), resumed.map { chunk(it).number("index") })
        assertEquals(first[1].bytes, resumed[0].bytes)
        assertEquals(first[3].bytes, resumed[1].bytes)
        assertArrayEquals(bytes.takeLast(7).toByteArray(), Protocol.chunkBytes(chunk(resumed.last())))
        receiveReceipt(restarted, resumed[2])
        receiveReceipt(restarted, resumed[0])
        assertTrue(restarted.nextFrames().isEmpty())
        receiveReceipt(restarted, resumed[1])
        val finalize = request(restarted, "finalize_upload")
        receiveReceipt(restarted, finalize.second)
        assertEquals(FileTransferStatus.FINALIZING, restarted.state.value.transfers.single().status)
        clock += 31_000
        assertEquals(finalize.second.bytes, request(restarted, "finalize_upload").second.bytes)
        acknowledge(restarted, finalize, buildJsonObject { put("file", file(bytes)) })
        val result = restarted.state.value.transfers.single()
        assertEquals(localId, result.id)
        assertEquals(FileTransferStatus.COMPLETE, result.status)
        assertEquals("file_fixture", result.fileId)
        assertFalse(restarted.state.value.hasPendingTransfers)
    }

    @Test fun earlyOutOfOrderDownloadIsDurableButNotReleasedBeforeMatchingAckAndWholeHash() = runBlocking {
        val repo = connect()
        val bytes = ByteArray(Protocol.CHUNK_SIZE + 3) { (it % 101).toByte() }
        val localId = repo.startDownload("file_fixture")
        val read = request(repo, "read_file")
        val tail = downloadChunk("down_1", 1, bytes.takeLast(3).toByteArray(), "tail")
        repo.receive(tail)
        assertFalse(repo.pendingReceipts().any { it.id == "tail" })
        assertNull(repo.readDownloaded(localId))

        val restarted = connect()
        acknowledge(restarted, read, download("down_1", bytes))
        assertEquals(1, restarted.state.value.transfers.single().completedChunks)
        assertTrue(restarted.pendingReceipts().contains(tail.delivery))
        assertNull(restarted.readDownloaded(localId))
        val head = downloadChunk("down_1", 0, bytes.copyOfRange(0, Protocol.CHUNK_SIZE), "head")
        restarted.receive(head)
        assertArrayEquals(bytes, restarted.readDownloaded(localId))
        restarted.receiptSent(head.delivery!!)
        restarted.receive(head)
        assertTrue(restarted.pendingReceipts().contains(head.delivery))
        restarted.peerOnline("different_peer", "epoch_demo_b")
        assertNull(restarted.readDownloaded(localId))
    }

    @Test fun mismatchedWholeHashNeverPublishesBytes() = runBlocking {
        val repo = connect()
        val localId = repo.startDownload("file_fixture")
        val read = request(repo, "read_file")
        acknowledge(repo, read, download("down_2", byteArrayOf(1, 2, 3)))
        repo.receive(downloadChunk("down_2", 0, byteArrayOf(3, 2, 1), "well_formed", 1))
        assertEquals(FileTransferStatus.FAILED, repo.state.value.transfers.single().status)
        assertNull(repo.readDownloaded(localId))
    }

    @Test fun cancellationRequiresMatchingBusinessResultAndRetriesOriginalAfterDisconnect() = runBlocking {
        val repo = connect()
        val localId = repo.startUpload("sample.png", "image/png", byteArrayOf(1, 2, 3))
        acknowledge(repo, request(repo, "begin_upload"), upload(3, emptyList()))
        val chunk = repo.nextFrames().single()
        repo.cancelTransfer(localId)
        val cancel = request(repo, "cancel_transfer")
        assertEquals("upload_fixture", cancel.first.data.text("transferId"))
        assertEquals("upload", cancel.first.data.text("kind"))
        receiveReceipt(repo, cancel.second)
        receiveReceipt(repo, chunk)
        assertEquals(FileTransferStatus.CANCELLING, repo.state.value.transfers.single().status)
        assertTrue(repo.state.value.hasPendingTransfers)

        val restarted = connect()
        assertTrue(restarted.nextFrames().isEmpty())
        clock += 31_000
        assertEquals(cancel.second.bytes, request(restarted, "cancel_transfer").second.bytes)
        acknowledge(restarted, cancel, buildJsonObject {
            put("transferId", "upload_fixture"); put("status", "cancelled")
        })
        assertEquals(FileTransferStatus.CANCELLED, restarted.state.value.transfers.single().status)
        assertTrue(restarted.nextFrames().isEmpty())
    }

    @Test fun pendingCancelStillResolvesAfterCorruptLastDownloadBlock() = runBlocking {
        val repo = connect()
        val localId = repo.startDownload("file_fixture")
        acknowledge(repo, request(repo, "read_file"), download("down_2", byteArrayOf(1, 2, 3)))
        repo.cancelTransfer(localId)
        val cancel = request(repo, "cancel_transfer")
        repo.receive(downloadChunk("down_2", 0, byteArrayOf(3, 2, 1), "bad_while_cancelling", 1))
        assertNull(repo.readDownloaded(localId))
        assertEquals(FileTransferStatus.CANCELLING, repo.state.value.transfers.single().status)
        acknowledge(repo, cancel, buildJsonObject {
            put("transferId", "down_2"); put("status", "cancelled")
        })
        assertEquals(FileTransferStatus.CANCELLED, repo.state.value.transfers.single().status)
    }

    @Test fun oldEpochDoesNotReissueUnknownBeginUnderNewIdentity() = runBlocking {
        val repo = connect()
        val localId = repo.startUpload("sample.png", "image/png", byteArrayOf(1, 2, 3), "workspace", epoch)
        val begin = request(repo, "begin_upload")
        receiveReceipt(repo, begin.second)
        repo.peerOnline("local_b", "epoch_demo_b")
        assertEquals(localId, repo.state.value.transfers.single().id)
        assertEquals(FileTransferStatus.QUARANTINED, repo.state.value.transfers.single().status)
        assertTrue(repo.nextFrames().none { it.message?.command == "begin_upload" })
    }

    @Test fun holdingImportWaitsForNoticeAckAndResumesSameRegistration() = runBlocking {
        val repo = connect()
        val bytes = byteArrayOf(1, 2, 3)
        val workflow = repo.startHoldingImport("holding.png", "image/png", bytes, "vision-v1")
        acknowledge(repo, request(repo, "begin_upload", 1), upload(3, emptyList()))
        val uploadingFrames = repo.nextFrames()
        val notice = uploadingFrames.single { it.message?.command == "accept_vision_notice" }.let { it.message!! to it }
        uploadingFrames.filter { it.message == null }.forEach { receiveReceipt(repo, it) }
        acknowledge(repo, request(repo, "finalize_upload"), buildJsonObject { put("file", file(bytes)) })
        assertEquals(HoldingImportStatus.AUTHORIZING, repo.state.value.holdingImport!!.status)
        assertTrue(repo.nextFrames().none { it.message?.command == "register_image" })
        acknowledge(repo, notice, buildJsonObject {
            put("noticeVersion", "vision-v1"); put("snapshotSeq", 42); put("consentedAt", clock)
        })
        val registration = request(repo, "register_image")
        assertEquals(true, registration.first.data.flag("autoApply"))
        assertEquals("file_fixture", registration.first.data.text("fileId"))
        clock += 31_000
        val restarted = connect()
        clock += 31_000
        val repeated = request(restarted, "register_image")
        assertEquals(registration.second.bytes, repeated.second.bytes)
        assertEquals(workflow, restarted.state.value.holdingImport!!.id)
        restarted.peerOnline("new_peer", "new_epoch")
        assertEquals(HoldingImportStatus.FAILED, restarted.state.value.holdingImport!!.status)
        assertTrue(restarted.nextFrames().none { it.message?.command == "register_image" })
    }

    @Test fun unrelatedOldFileIsNeverRegisteredAndPairSwitchHidesWorkflow() = runBlocking {
        val repo = connect()
        val bytes = byteArrayOf(1, 2, 3)
        repo.startUpload("ordinary.png", "image/png", bytes)
        acknowledge(repo, request(repo, "begin_upload"), upload(3, emptyList()))
        receiveReceipt(repo, repo.nextFrames().single())
        acknowledge(repo, request(repo, "finalize_upload"), buildJsonObject { put("file", file(bytes)) })
        assertNull(repo.state.value.holdingImport)
        assertTrue(repo.nextFrames().none { it.message?.command == "register_image" })
        repo.startHoldingImport("holding.png", "image/png", bytes, "vision-v1")
        repo.selectPair("another_workspace")
        assertNull(repo.state.value.holdingImport)
    }

    @Test fun confirmationRequiresRefreshedSnapshotAndTerminalStateIsFencedOnEpochChange() = runBlocking {
        val repo = connect()
        startRecognition(repo)
        val completion = importCompletion(repo, "confirmed", null)
        repo.receive(Protocol.deliver(completion, "confirmed_delivery", peer, install))
        assertEquals(HoldingImportStatus.SYNCING, repo.state.value.holdingImport!!.status)
        assertTrue(repo.state.value.hasPendingHoldingImport)
        finishImportSnapshot(repo, completion.data)
        assertEquals(HoldingImportStatus.SUCCESS, repo.state.value.holdingImport!!.status)
        assertEquals(43L, repo.state.value.sequence)
        assertFalse(repo.state.value.hasPendingHoldingImport)
        repo.receive(Protocol.deliver(completion, "confirmed_duplicate", peer, install))
        assertEquals(HoldingImportStatus.SUCCESS, repo.state.value.holdingImport!!.status)
        repo.peerOnline("new_peer", "new_epoch")
        assertEquals(HoldingImportStatus.FAILED, repo.state.value.holdingImport!!.status)
    }

    @Test fun invalidContentAndProviderFailureHaveDistinctFinalStates() = runBlocking {
        val repo = connect()
        startRecognition(repo)
        repo.receive(Protocol.deliver(importCompletion(repo, "failed", "IMPORT_CONTENT_INVALID"), "invalid", peer, install))
        assertEquals(HoldingImportStatus.INVALID, repo.state.value.holdingImport!!.status)
        assertEquals("IMPORT_CONTENT_INVALID", repo.state.value.holdingImport!!.errorCode)
        // No position write is manufactured from the recognition result.
        assertEquals(1, repo.state.value.records["positions"]!!.size)
    }

    @Test fun providerFailureNeverReportsSuccess() = runBlocking {
        val repo = connect()
        startRecognition(repo)
        repo.receive(Protocol.deliver(importCompletion(repo, "failed", "VISION_AUTH_ERROR"), "provider_failed", peer, install))
        assertEquals(HoldingImportStatus.FAILED, repo.state.value.holdingImport!!.status)
        assertTrue(repo.nextFrames().none { it.message?.command == "apply_import" })
    }

    @Test fun failedImageRetryReusesOnlyItsFileAndBindsANewNoticeAndImport() = runBlocking {
        val repo = connect()
        startRecognition(repo)
        val original = repo.state.value.holdingImport!!
        repo.receive(Protocol.deliver(importCompletion(repo, "failed", "VISION_AUTH_ERROR"), "retry_failure", peer, install))
        assertTrue(repo.state.value.holdingImport!!.canRetry)
        val retryId = repo.retryHoldingImport("vision-v3", "workspace", epoch)
        assertNotEquals(original.id, retryId)
        assertEquals(original.transferId, repo.state.value.holdingImport!!.transferId)
        val frames = repo.nextFrames()
        assertTrue(frames.none { it.message?.command == "begin_upload" })
        assertTrue(frames.none { it.message?.command == "register_image" })
        val notice = frames.single { it.message?.command == "accept_vision_notice" }.let { it.message!! to it }
        assertEquals("vision-v3", notice.first.data.text("noticeVersion"))
        acknowledge(repo, notice, buildJsonObject { put("noticeVersion", "vision-v3"); put("consentedAt", clock); put("snapshotSeq", 42) })
        val registration = request(repo, "register_image")
        assertEquals(retryId, registration.first.data.text("importId"))
        assertEquals("file_fixture", registration.first.data.text("fileId"))
        assertTrue(registration.first.data.flag("autoApply")!!)
        assertEquals(1, repo.state.value.transfers.size)
    }

    @Test fun activeOrSuccessfulImportCannotBeRetried() = runBlocking {
        val repo = connect()
        startRecognition(repo)
        assertFalse(repo.state.value.holdingImport!!.canRetry)
        try { repo.retryHoldingImport("vision-v2"); fail("Active import must be rejected") } catch (_: ProtocolException) { }
        val completed = importCompletion(repo, "confirmed", null)
        repo.receive(Protocol.deliver(completed, "retry_success", peer, install))
        finishImportSnapshot(repo, completed.data)
        assertEquals(HoldingImportStatus.SUCCESS, repo.state.value.holdingImport!!.status)
        assertFalse(repo.state.value.holdingImport!!.canRetry)
        try { repo.retryHoldingImport("vision-v2"); fail("Successful import must be rejected") } catch (_: ProtocolException) { }
    }

    @Test fun relaySchemaRejectionCanRetrySavedImageWithCurrentNotice() = runBlocking {
        val repo = connect()
        startRecognition(repo)
        val original = repo.state.value.holdingImport!!
        val frame = store.transaction { list("workspace", "transport_outbox").values }
            .map { Protocol.frame(it.text("bytes")!!) }.single { it.message?.command == "register_image" }
        repo.receive(Protocol.frame(CanonicalJson.encode(buildJsonObject {
            put("v", 2); put("type", "error"); put("refDeliveryId", frame.delivery!!.id)
            put("code", "INVALID_FRAME"); put("retryable", false); put("retryAfterMs", 0)
        })))
        assertEquals(HoldingImportStatus.FAILED, repo.state.value.holdingImport!!.status)
        assertTrue(repo.state.value.holdingImport!!.canRetry)
        val retry = repo.retryHoldingImport("vision-v2", "workspace", epoch)
        assertNotEquals(original.id, retry)
        val queued = repo.nextFrames()
        assertTrue(queued.none { it.message?.command in setOf("begin_upload", "register_image") })
        val notice = queued.single { it.message?.command == "accept_vision_notice" }.message!!
        assertEquals("vision-v2", notice.data.text("noticeVersion"))
        assertEquals(original.transferId, repo.state.value.holdingImport!!.transferId)
    }

    @Test fun recognizedImportCannotBeRetriedWhileItCanStillBeConfirmed() = runBlocking {
        val repo = connect()
        startRecognition(repo)
        repo.receive(Protocol.deliver(importCompletion(repo, "recognized", null), "recognized_only", peer, install))
        assertFalse(repo.state.value.holdingImport!!.canRetry)
        try { repo.retryHoldingImport("vision-v2"); fail("Recognized import remains actionable") } catch (_: ProtocolException) { }
    }

    @Test fun newerConfirmedSnapshotBlocksRetryDespiteOldFailedCompletion() = runBlocking {
        val repo = connect()
        startRecognition(repo)
        val completion = importCompletion(repo, "failed", "IMPORT_CONTENT_INVALID")
        repo.receive(Protocol.deliver(completion, "old_failure", peer, install))
        assertTrue(repo.state.value.holdingImport!!.canRetry)
        val confirmed = JsonObject(completion.data + mapOf("status" to JsonPrimitive("confirmed"),
            "errorCode" to JsonNull, "importVersion" to JsonPrimitive(3)))
        finishImportSnapshot(repo, confirmed)
        assertFalse(repo.state.value.holdingImport!!.canRetry)
        try { repo.retryHoldingImport("vision-v2"); fail("Known confirmed snapshot must take precedence") } catch (_: ProtocolException) { }
    }

    @Test fun lateCancellationCannotRestoreRetryOfADeletedOriginal() = runBlocking {
        val repo = connect()
        startRecognition(repo)
        val cancelled = importCompletion(repo, "cancelled", null)
        val id = cancelled.data.text("importId")!!
        repo.receive(Protocol.deliver(cancelled, "original_cancel", peer, install))
        assertTrue(repo.state.value.holdingImport!!.canRetry)
        val deleted = Protocol.business(JsonObject(cancelled.json + mapOf(
            "id" to JsonPrimitive("deleted_original"),
            "dedupeKey" to JsonPrimitive("import:$id:3"),
            "snapshotSeq" to JsonPrimitive(44),
            "data" to JsonObject(cancelled.data + mapOf(
                "importVersion" to JsonPrimitive(3), "fileStatus" to JsonPrimitive("deleted"),
            )),
        )))
        repo.receive(Protocol.deliver(deleted, "deleted_original", peer, install))
        val late = Protocol.business(JsonObject(cancelled.json + mapOf(
            "id" to JsonPrimitive("late_cancel"),
            "dedupeKey" to JsonPrimitive("late-cancel:$id:2"),
        )))
        repo.receive(Protocol.deliver(late, "late_cancel", peer, install))
        val persisted = store.transaction { get("workspace", "completions:$epoch:import_result", id) }
        assertEquals("deleted", persisted?.text("fileStatus"))
        assertEquals(3L, persisted?.number("importVersion"))
        assertFalse(repo.state.value.holdingImport!!.canRetry)
    }

    @Test fun revocationDuringUploadNeverCreatesALaterConsentGrant() = runBlocking {
        val repo = connect()
        val bytes = byteArrayOf(1, 2, 3)
        repo.startHoldingImport("holding.png", "image/png", bytes, "vision-v2")
        val begin = request(repo, "begin_upload", 1)
        val revokeId = repo.enqueue("accept_vision_notice", buildJsonObject {
            put("noticeVersion", "vision-v2"); put("accepted", false)
        })
        acknowledge(repo, begin, upload(3, emptyList()))
        val uploading = repo.nextFrames()
        val grant = uploading.single { it.message?.command == "accept_vision_notice" }.let { it.message!! to it }
        acknowledge(repo, grant, buildJsonObject { put("noticeVersion", "vision-v2"); put("consentedAt", clock); put("snapshotSeq", 42) })
        val revoke = request(repo, "accept_vision_notice")
        assertEquals(revokeId, revoke.first.clientMsgId)
        acknowledge(repo, revoke, buildJsonObject { put("noticeVersion", "vision-v2"); put("consentedAt", JsonNull); put("snapshotSeq", 42) })
        uploading.filter { it.message == null }.forEach { receiveReceipt(repo, it) }
        acknowledge(repo, request(repo, "finalize_upload"), buildJsonObject { put("file", file(bytes)) })
        val registration = request(repo, "register_image")
        acknowledge(repo, registration, JsonObject(emptyMap()), "VISION_CONSENT_REQUIRED")
        val grants = store.transaction { list("workspace", "command_intents").values }.mapNotNull { it.obj("request")?.obj("data") }
            .filter { it.text("cmd") == "accept_vision_notice" }
        assertEquals(2, grants.size)
        assertEquals(1, grants.count { it.flag("accepted") == true })
        assertEquals(HoldingImportStatus.FAILED, repo.state.value.holdingImport!!.status)
    }

    private suspend fun startRecognition(repo: WorkbenchRepository) {
        val bytes = byteArrayOf(1, 2, 3)
        repo.startHoldingImport("holding.png", "image/png", bytes, "vision-v2")
        acknowledge(repo, request(repo, "begin_upload", 1), upload(3, emptyList()))
        val uploadingFrames = repo.nextFrames()
        val notice = uploadingFrames.single { it.message?.command == "accept_vision_notice" }.let { it.message!! to it }
        uploadingFrames.filter { it.message == null }.forEach { receiveReceipt(repo, it) }
        acknowledge(repo, request(repo, "finalize_upload"), buildJsonObject { put("file", file(bytes)) })
        acknowledge(repo, notice, buildJsonObject {
            put("noticeVersion", "vision-v2"); put("snapshotSeq", 42); put("consentedAt", clock)
        })
        request(repo, "register_image")
    }

    private fun importCompletion(repo: WorkbenchRepository, status: String, error: String?): BusinessMessage {
        val template = fixtures.getValue("import_result")
        val workflow = repo.state.value.holdingImport!!
        return Protocol.business(JsonObject(template + mapOf(
            "id" to JsonPrimitive("result_${workflow.id}"), "dedupeKey" to JsonPrimitive("import:${workflow.id}:2"),
            "snapshotSeq" to JsonPrimitive(43), "data" to JsonObject(template.obj("data")!! + mapOf(
                "importId" to JsonPrimitive(workflow.importId), "status" to JsonPrimitive(status),
                "importVersion" to JsonPrimitive(2), "fileId" to JsonPrimitive("file_fixture"),
                "errorCode" to (error?.let(::JsonPrimitive) ?: JsonNull),
            )),
        )))
    }

    private suspend fun finishImportSnapshot(repo: WorkbenchRepository, imported: JsonObject) {
        clock += 31_000
        for (n in 0..1) {
            val request = request(repo, "sync", 8)
            val template = fixtures.getValue("snapshot_page_$n")
            val data = template.obj("data")!!
            val imports = JsonArray(listOf(imported))
            val updated = data.toMutableMap().apply {
                put("snapshotSeq", JsonPrimitive(43))
                put("snapshotId", JsonPrimitive("after_import"))
                put("imports", if (n == 1) imports else JsonArray(emptyList()))
                if (n == 0) put("manifest", JsonObject(data.obj("manifest")!! + ("imports" to buildJsonObject {
                    put("count", 1); put("sha256", CanonicalJson.hash(imports))
                })))
            }
            val page = Protocol.business(JsonObject(template + mapOf("id" to JsonPrimitive("after_import_$n"),
                "dedupeKey" to JsonPrimitive("after_import_$n"), "snapshotSeq" to JsonPrimitive(43), "data" to JsonObject(updated))))
            repo.receive(Protocol.deliver(page, "import_page_$n", peer, install))
            acknowledge(repo, request, buildJsonObject {
                put("requestId", request.first.clientMsgId!!); put("pageEventId", page.id)
                for (key in listOf("serverEpoch", "snapshotId", "snapshotSeq", "pageIndex", "nextPageCursor", "hasMore")) put(key, page.data.getValue(key))
            })
        }
    }

    private fun chunk(frame: RelayFrame) = frame.json.obj("payload")!!

    private fun downloadChunk(transfer: String, index: Int, bytes: ByteArray, deliveryId: String, total: Int = 2) =
        Protocol.deliverChunk(transfer, "file_fixture", index, total, bytes, deliveryId, peer, install)

    private fun download(transfer: String, bytes: ByteArray) = buildJsonObject {
        put("downloadId", transfer); put("file", file(bytes)); put("chunkSize", Protocol.CHUNK_SIZE)
        put("totalChunks", count(bytes.size)); put("expiresAt", clock + 86_400_000)
    }

    private fun upload(size: Int, indexes: List<Int>) = buildJsonObject {
        putJsonObject("upload") {
            put("uploadId", "upload_fixture"); put("fileId", "file_fixture"); put("sizeBytes", size)
            put("totalChunks", count(size)); put("chunkSize", Protocol.CHUNK_SIZE)
            put("receivedChunks", JsonArray(indexes.map(::JsonPrimitive)))
            put("expiresAt", clock + 86_400_000); put("status", "receiving")
        }
    }

    private fun file(bytes: ByteArray) = buildJsonObject {
        put("fileId", "file_fixture"); put("version", 1); put("name", "sample.png")
        put("mime", "image/png"); put("sizeBytes", bytes.size); put("sha256", CanonicalJson.sha256(bytes))
        put("status", "available"); put("createdAt", clock); put("deletedAt", JsonNull)
    }

    private fun count(size: Int) = (size + Protocol.CHUNK_SIZE - 1) / Protocol.CHUNK_SIZE

    private suspend fun connect(): WorkbenchRepository {
        val repo = WorkbenchRepository(store, { clock }, { "id_${++id}" })
        repo.selectPair("workspace")
        repo.peerOnline(peer, epoch)
        for (n in 0..1) {
            val request = request(repo, "sync", 1)
            val page = Protocol.business(fixtures.getValue("snapshot_page_$n"))
            repo.receive(Protocol.deliver(page, "page_${++id}", peer, install))
            acknowledge(repo, request, buildJsonObject {
                put("requestId", request.first.clientMsgId!!)
                put("pageEventId", page.id)
                for (key in listOf("serverEpoch", "snapshotId", "snapshotSeq", "pageIndex", "nextPageCursor", "hasMore")) {
                    put(key, page.data.getValue(key))
                }
            })
        }
        return repo
    }

    private suspend fun request(repo: WorkbenchRepository, cmd: String, limit: Int = 8) =
        repo.nextFrames(limit).single { it.message?.command == cmd }.let { it.message!! to it }

    private suspend fun receiveReceipt(repo: WorkbenchRepository, frame: RelayFrame) =
        repo.receive(Protocol.frame(Protocol.receipt(frame.delivery!!)))

    private suspend fun acknowledge(repo: WorkbenchRepository, pair: Pair<BusinessMessage, RelayFrame>, result: JsonObject, errorCode: String? = null) {
        val request = pair.first
        val client = request.clientMsgId!!
        val ack = Protocol.business(buildJsonObject {
            put("v", 2); put("kind", "ack"); put("id", "ack_$client"); put("dedupeKey", "ack:$client")
            put("ts", clock); put("serverEpoch", epoch); put("snapshotSeq", 42)
            putJsonObject("data") {
                put("cmd", request.command!!); put("refId", request.id); put("clientMsgId", client)
                put("requestHash", CanonicalJson.hash(request.data)); put("ok", errorCode == null)
                put("result", if (errorCode == null) result else JsonNull)
                put("error", if (errorCode == null) JsonNull else buildJsonObject {
                    put("code", errorCode); put("message", "Rejected"); put("retryable", false); put("details", JsonObject(emptyMap()))
                })
            }
        })
        repo.receive(Protocol.deliver(ack, "ack_frame_${++id}", peer, install))
    }
}

private class TransferTestStore : WorkbenchStore {
    private var committed = mapOf<Triple<String, String, String>, JsonObject>()

    override suspend fun <T> transaction(block: suspend WorkbenchTransaction.() -> T): T {
        val staged = committed.toMutableMap()
        val result = block(object : WorkbenchTransaction {
            override suspend fun get(scope: String, group: String, id: String) = staged[Triple(scope, group, id)]
            override suspend fun list(scope: String, group: String) =
                staged.filterKeys { it.first == scope && it.second == group }.mapKeys { it.key.third }
            override suspend fun put(scope: String, group: String, id: String, value: JsonObject) {
                staged[Triple(scope, group, id)] = value
            }
            override suspend fun remove(scope: String, group: String, id: String) {
                staged.remove(Triple(scope, group, id))
            }
            override suspend fun clear(scope: String, group: String) {
                staged.keys.removeAll { it.first == scope && it.second == group }
            }
        })
        committed = staged
        return result
    }
}
