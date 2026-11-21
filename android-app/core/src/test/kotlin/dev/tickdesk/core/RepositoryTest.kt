package dev.tickdesk.core

import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Test

class RepositoryTest {
    private var now = 1794792300000L
    private val store = TestStore()
    private var lastId = 0
    private fun repository() = WorkbenchRepository(store, { now }, { "id_${++lastId}" })
    private val cases = contractResource("fixtures").array("cases")!!.associate {
        val value = it.jsonObject
        value.text("name")!! to value.obj("payload")!!
    }

    @Test fun reportPublicationIsBoundToEpochAndCanNotifyOnlyOnceAcrossRestart() = runBlocking {
        val repo = synced()
        val template = fixture("analysis_report_publication")
        val reportId = template.data.obj("header")!!.text("reportId")!!
        val report = Protocol.business(JsonObject(template.json + mapOf(
            "ts" to JsonPrimitive(now),
            "data" to JsonObject(template.data + ("notifyUntil" to JsonPrimitive(now + 5_000))),
        )))
        repo.receive(fromLocal(report))
        repo.receive(fromLocal(report, "replayed_report"))
        assertEquals(listOf(reportId), repo.state.value.reportNotices.map { it.reportId })
        assertNotNull(repo.reportNotification("workspace", "epoch_demo_a", reportId, claim = true))
        assertNull(repo.reportNotification("workspace", "epoch_demo_a", reportId, claim = true))
        assertTrue(repo.state.value.reportNotices.isEmpty())
        assertNotNull(repo.reportNotification("workspace", "epoch_demo_a", reportId, claim = false))

        val restarted = connect()
        finishSnapshot(restarted)
        assertTrue(restarted.state.value.reportNotices.isEmpty())
        assertNull(restarted.reportNotification("other-pair", "epoch_demo_a", reportId, claim = false))
        assertNull(restarted.reportNotification("workspace", "epoch_other", reportId, claim = false))
        now += 5_000
        assertNull(restarted.reportNotification("workspace", "epoch_demo_a", reportId, claim = false))
    }

    @Test fun latestReportReadUsesBackgroundCatalogueWithoutLeakingAnotherQueryOrChangingBody() = runBlocking {
        val repo = synced()
        val catalogue = fixture("ack_list_reports").data.obj("result")!!
        val detail = fixture("ack_get_report").data.obj("result")!!
        val previousHeader = detail.obj("header")!!
        val id = previousHeader.text("reportId")!!
        val updatedHeader = JsonObject(previousHeader + ("summary" to JsonPrimitive("研究观察 · 入场未触发 4 项")))
        val updatedResult = JsonObject(catalogue + ("items" to JsonArray(listOf(updatedHeader))))

        fun acknowledgement(request: BusinessMessage, result: JsonObject, at: Long): BusinessMessage {
            val clientId = request.clientMsgId!!
            return Protocol.business(JsonObject(cases.getValue("ack_${request.command}") + mapOf(
                "id" to JsonPrimitive("report_ack_$clientId"), "dedupeKey" to JsonPrimitive("ack:$clientId"),
                "ts" to JsonPrimitive(at), "data" to buildJsonObject {
                    put("refId", request.id); put("clientMsgId", clientId); put("requestHash", CanonicalJson.hash(request.data))
                    put("cmd", request.command!!); put("ok", true); put("error", JsonNull); put("result", result)
                },
            )))
        }

        suspend fun enqueue(command: String, params: JsonObject): BusinessMessage {
            val requestId = repo.enqueue(command, params)
            return repo.nextFrames().first { it.message?.clientMsgId == requestId }.message!!
        }

        val oldCatalogue = enqueue("list_reports", buildJsonObject { put("limit", 10) })
        val oldDetail = enqueue("get_report", buildJsonObject { put("reportId", id) })
        repo.receive(fromLocal(acknowledgement(oldCatalogue, catalogue, now)))
        repo.receive(fromLocal(acknowledgement(oldDetail, detail, now)))
        val delayedCatalogue = enqueue("list_reports", buildJsonObject { put("limit", 10) })
        repo.requestAuxiliary("list_reports", "workspace")
        val background = repo.nextFrames().single { it.message?.data?.flag("subscribe") == true }.message!!
        repo.receive(fromLocal(acknowledgement(background, updatedResult, now + 2_000)))
        repo.receive(fromLocal(acknowledgement(delayedCatalogue, catalogue, now + 1_000)))
        val filtered = enqueue("list_reports", buildJsonObject { put("symbol", "000001.SZ"); put("limit", 10) })
        val emptyResult = JsonObject(catalogue + ("items" to JsonArray(emptyList())))
        repo.receive(fromLocal(acknowledgement(filtered, emptyResult, now + 3_000)))

        val reads = repo.state.value.reportReads
        assertEquals(updatedResult, reads.catalogues[ReportCatalogueQuery()])
        assertEquals(emptyResult, reads.catalogues[ReportCatalogueQuery("000001.SZ")])
        assertEquals(updatedHeader, reads.headers[id])
        assertEquals(detail, repo.state.value.intents.single { it.command == "get_report" }.result)
        assertTrue(repo.state.value.intents.none { it.id == background.clientMsgId })

        val reopened = connect()
        finishSnapshot(reopened)
        assertEquals(reads, reopened.state.value.reportReads)
        reopened.selectPair("other_workspace")
        assertEquals(ReportReads(), reopened.state.value.reportReads)
    }

    @Test fun aPagedReplyReadCannotCrossTheCapturedPeerEvenWithinAnEpoch() = runBlocking {
        val repo = synced()
        assertEquals("local", repo.state.value.peerIncarnation)
        val request = ReplyPageSequence("reply").request()
        val first = repo.enqueue("get_reply_page", request, "workspace", "epoch_demo_a", "local")
        assertTrue(repo.nextFrames().any { it.message?.clientMsgId == first })
        repo.peerOnline("another_local", "epoch_demo_a")
        assertEquals("another_local", repo.state.value.peerIncarnation)
        try {
            repo.enqueue("get_reply_page", request, "workspace", "epoch_demo_a", "local")
            fail("A read opened for a prior peer must not be queued")
        } catch (_: ProtocolException) {
            assertTrue(repo.nextFrames().none { it.message?.command == "get_reply_page" })
        }
    }

    @Test fun incompleteOrUnlinkedSnapshotNeverReplacesProjection() = runBlocking {
        val repo = connect()
        val sync = repo.nextFrames().single().message!!
        val first = fixture("snapshot_page_0")
        val last = fixture("snapshot_page_1")
        repo.receive(fromLocal(last))
        repo.receive(fromLocal(first))
        assertFalse(repo.state.value.connection.synced)
        repo.receive(fromLocal(syncAck(sync, first)))
        assertFalse(repo.state.value.connection.synced)
        val continuation = repo.nextFrames().first { it.message?.command == "sync" }.message!!
        repo.receive(fromLocal(syncAck(continuation, last)))
        assertTrue(repo.state.value.connection.synced)
        assertEquals(1, repo.state.value.records["positions"]?.size)
    }

    @Test fun frozenPageRequestIdCanRemainOriginalWhileContinuationAckBindsCurrentRequest() = runBlocking {
        val repo = connect()
        val firstRequest = repo.nextFrames().single().message!!
        fun frozenPage(name: String): BusinessMessage {
            val page = fixture(name)
            return Protocol.business(JsonObject(page.json + ("data" to JsonObject(page.data +
                ("requestId" to JsonPrimitive(firstRequest.clientMsgId!!))))))
        }
        val first = frozenPage("snapshot_page_0")
        val last = frozenPage("snapshot_page_1")
        repo.receive(fromLocal(first))
        repo.receive(fromLocal(syncAck(firstRequest, first)))
        assertFalse(repo.state.value.connection.synced)
        val continuation = repo.nextFrames().first { it.message?.command == "sync" }.message!!
        assertNotEquals(firstRequest.clientMsgId, continuation.clientMsgId)
        assertEquals(first.data.text("snapshotId"), continuation.data.text("snapshotId"))
        assertEquals(first.data.text("nextPageCursor"), continuation.data.text("pageCursor"))
        val ack = syncAck(continuation, last)
        assertEquals(continuation.clientMsgId, ack.data.obj("result")!!.text("requestId"))
        assertEquals(firstRequest.clientMsgId, last.data.text("requestId"))
        // ACK can arrive before its immutable page; neither is enough on its own.
        repo.receive(fromLocal(ack))
        assertFalse(repo.state.value.connection.synced)
        repo.receive(fromLocal(last))
        assertTrue(repo.state.value.connection.canWrite)
        assertNull(repo.state.value.syncError)
        assertEquals(1, repo.state.value.records["positions"]?.size)
    }

    @Test fun continuationAckCannotReuseTheOriginalRequestIdEvenWhenPageIsFrozen() = runBlocking {
        val repo = connect()
        val firstRequest = repo.nextFrames().single().message!!
        val first = fixture("snapshot_page_0")
        val template = fixture("snapshot_page_1")
        val last = Protocol.business(JsonObject(template.json + ("data" to JsonObject(template.data +
            ("requestId" to JsonPrimitive(firstRequest.clientMsgId!!))))))
        repo.receive(fromLocal(first))
        repo.receive(fromLocal(syncAck(firstRequest, first)))
        val continuation = repo.nextFrames().first { it.message?.command == "sync" }.message!!
        val ack = syncAck(continuation, last)
        val staleResult = JsonObject(ack.data.obj("result")!! + ("requestId" to JsonPrimitive(firstRequest.clientMsgId!!)))
        val wrongAck = Protocol.business(JsonObject(ack.json + ("data" to JsonObject(ack.data + ("result" to staleResult)))))
        repo.receive(fromLocal(last))
        repo.receive(fromLocal(wrongAck))
        assertFalse(repo.state.value.connection.synced)
        assertFalse(repo.state.value.connection.canWrite)
        assertEquals("INVALID_SNAPSHOT", repo.state.value.syncError)
        assertTrue(repo.state.value.records.isEmpty())
    }

    @Test fun receiptAndRestartPreserveOriginalIntentWhenBusinessAckIsLost() = runBlocking {
        val repo = synced()
        val command = repo.enqueue("pause")
        val delivery = repo.nextFrames().single { it.message?.clientMsgId == command }
        repo.receive(Protocol.frame(Protocol.receipt(delivery.delivery!!)))
        assertEquals(DeliveryStage.DELIVERED, repo.state.value.intents.single().stage)
        assertFalse(repo.state.value.meta!!.obj("analysis")!!.flag("paused")!!)
        val restarted = repository()
        restarted.selectPair("workspace")
        restarted.peerOnline("local", "epoch_demo_a")
        finishSnapshot(restarted)
        now += 31_000
        val resend = restarted.nextFrames().single { it.message?.clientMsgId == command }
        assertEquals(delivery.bytes, resend.bytes)
        assertEquals(DeliveryStage.DELIVERED, restarted.state.value.intents.single().stage)
    }

    @Test fun duplicateDeliveryRequeuesReceiptAndConflictingIdCannotOverwrite() = runBlocking {
        val repo = synced()
        val event = fixture("snapshot_page_0")
        val duplicate = fromLocal(event, "repeat")
        repo.receive(duplicate)
        repo.receiptSent(duplicate.delivery!!)
        repo.receive(duplicate)
        assertTrue(repo.pendingReceipts().any { it.id == "repeat" })
        try {
            repo.receive(fromLocal(fixture("snapshot_page_1"), "repeat"))
            fail("Delivery ID collision must fail")
        } catch (_: ProtocolException) {
            assertEquals(duplicate.delivery, repo.pendingReceipts().single { it.id == "repeat" })
        }
    }

    @Test fun peerIdentityChangeQuarantinesPendingWritesBeforeSendingAgain() = runBlocking {
        val repo = synced()
        val pending = repo.enqueue("pause")
        repo.peerOnline("local_b", "epoch_demo_b")
        assertTrue(repo.state.value.connection.recoveryRequired)
        assertEquals(DeliveryStage.QUARANTINED, repo.state.value.intents.single().stage)
        assertTrue(repo.nextFrames().none { it.message?.clientMsgId == pending })
    }

    @Test fun staleScreenEpochCannotEnqueueAWriteAgainstTheConfirmedSnapshot() = runBlocking {
        val repo = synced()
        try {
            repo.enqueue("pause", expectedPair = repo.state.value.pairId, expectedEpoch = "epoch_demo_old")
            fail("Stale screen scope must fail")
        } catch (_: ProtocolException) {
            assertTrue(repo.state.value.intents.isEmpty())
            assertTrue(repo.nextFrames().none { it.message?.command == "pause" })
        }
        val id = repo.enqueue("pause", expectedPair = repo.state.value.pairId, expectedEpoch = "epoch_demo_a")
        assertEquals(id, repo.state.value.intents.single().id)
    }

    @Test fun storageFailureCannotPublishAnIntent() = runBlocking {
        val repo = synced()
        store.rejectCommit = true
        try {
            repo.enqueue("pause")
            fail("Expected disk failure")
        } catch (_: IllegalStateException) {
            assertTrue(repo.state.value.intents.isEmpty())
        }
        store.rejectCommit = false
        assertTrue(repo.nextFrames().none { it.message?.command == "pause" })
    }

    @Test fun reminderLinksRequireAPersistedClaimInTheSameWorkspaceAndEpoch() = runBlocking {
        val repo = synced()
        val pair = "workspace"
        val epoch = "epoch_demo_a"
        val deadline = now + 30_000
        assertNull(repo.reminderDestination(pair, epoch, "hint"))
        assertTrue(repo.claimReminder(epoch, "hint", "known_task", deadline, pair))
        assertFalse(repo.claimReminder(epoch, "hint", "untrusted_task", deadline, pair))
        val expected = ReminderDestination(pair, epoch, "known_task", deadline)
        assertEquals(expected, repo.reminderDestination(pair, epoch, "hint"))
        assertNull(repo.reminderDestination(pair, "other_epoch", "hint"))
        assertNull(repo.reminderDestination("other_pair", epoch, "hint"))

        val restarted = repository()
        restarted.selectPair(pair)
        assertEquals(expected, restarted.reminderDestination(pair, epoch, "hint"))
        now = deadline
        assertNull(restarted.reminderDestination(pair, epoch, "hint"))
        now = deadline - 1
        restarted.peerOnline("new_local", epoch)
        assertNull(restarted.reminderDestination(pair, epoch, "hint"))
    }

    @Test fun successfulSyncAckWithMissingPageEventuallyStartsNewRequest() = runBlocking {
        val repo = connect()
        val first = repo.nextFrames().single().message!!
        repo.receive(fromLocal(syncAck(first, fixture("snapshot_page_0"))))
        assertFalse(repo.state.value.connection.synced)
        assertTrue(repo.nextFrames().isEmpty())
        now += 600_001
        repo.refresh()
        val next = repo.nextFrames().single().message!!
        assertEquals("sync", next.command)
        assertNotEquals(first.clientMsgId, next.clientMsgId)
        assertNull(next.data.text("snapshotId"))
    }

    @Test fun failedReplyReceivedBeforeAcceptanceNeverBecomesSuccess() = runBlocking {
        val repo = synced()
        val id = repo.enqueue("chat", buildJsonObject { put("text", "Fixture command") })
        val request = repo.nextFrames().single { it.message?.clientMsgId == id }.message!!
        val reply = fixture("reply")
        val failed = Protocol.business(JsonObject(reply.json + ("data" to JsonObject(reply.data + mapOf(
            "requestId" to JsonPrimitive(id), "status" to JsonPrimitive("failed"),
            "errorCode" to JsonPrimitive("AI_UNAVAILABLE"),
        )))))
        repo.receive(fromLocal(failed))
        val ack = fixture("ack_chat")
        val accepted = Protocol.business(JsonObject(ack.json + ("data" to JsonObject(ack.data + mapOf(
            "refId" to JsonPrimitive(request.id), "clientMsgId" to JsonPrimitive(id),
            "requestHash" to JsonPrimitive(CanonicalJson.hash(request.data)),
            "result" to buildJsonObject { put("accepted", true); put("requestId", id) },
        )))))
        repo.receive(fromLocal(accepted))
        val intent = repo.state.value.intents.single()
        assertEquals(DeliveryStage.REJECTED, intent.stage)
        assertEquals("AI_UNAVAILABLE", intent.errorCode)
    }

    private fun fixture(name: String) = Protocol.business(cases.getValue(name))

    private suspend fun connect() = repository().also {
        it.selectPair("workspace")
        it.peerOnline("local", "epoch_demo_a")
    }

    private suspend fun synced() = connect().also { finishSnapshot(it) }

    private suspend fun finishSnapshot(repo: WorkbenchRepository) {
        for (name in listOf("snapshot_page_0", "snapshot_page_1")) {
            val request = repo.nextFrames().first { it.message?.command == "sync" }.message!!
            val page = fixture(name)
            repo.receive(fromLocal(page))
            repo.receive(fromLocal(syncAck(request, page)))
        }
    }

    private fun fromLocal(message: BusinessMessage, deliveryId: String = message.id) =
        Protocol.deliver(message, deliveryId, "local", "id_1")

    private fun syncAck(request: BusinessMessage, page: BusinessMessage): BusinessMessage {
        val clientId = request.clientMsgId!!
        return Protocol.business(JsonObject(cases.getValue("ack_sync") + mapOf(
            "id" to JsonPrimitive("ack_$clientId"), "dedupeKey" to JsonPrimitive("ack:$clientId"),
            "data" to buildJsonObject {
                put("cmd", "sync"); put("ok", true); put("error", JsonNull)
                put("refId", request.id); put("clientMsgId", clientId)
                put("requestHash", CanonicalJson.hash(request.data))
                putJsonObject("result") {
                    put("requestId", clientId); put("pageEventId", page.id)
                    for (key in listOf("serverEpoch", "snapshotId", "snapshotSeq", "pageIndex", "nextPageCursor", "hasMore")) {
                        put(key, page.data.getValue(key))
                    }
                }
            },
        )))
    }
}

private class TestStore : WorkbenchStore {
    private var committed = mutableMapOf<Triple<String, String, String>, JsonObject>()
    var rejectCommit = false

    override suspend fun <T> transaction(block: suspend WorkbenchTransaction.() -> T): T {
        val staged = committed.toMutableMap()
        val tx = object : WorkbenchTransaction {
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
        }
        val result = block(tx)
        check(!rejectCommit)
        committed = staged
        return result
    }
}
