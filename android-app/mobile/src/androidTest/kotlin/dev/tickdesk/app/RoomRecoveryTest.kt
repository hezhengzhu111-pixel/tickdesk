package dev.tickdesk.app

import androidx.test.platform.app.InstrumentationRegistry
import dev.tickdesk.app.data.RoomWorkbenchStore
import dev.tickdesk.core.*
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Test
import java.util.UUID

class RoomRecoveryTest {
    private val context = InstrumentationRegistry.getInstrumentation().targetContext
    private val testContext = InstrumentationRegistry.getInstrumentation().context
    private val cases by lazy {
        testContext.assets.open("v2/fixtures.json").bufferedReader().use {
            Json.parseToJsonElement(it.readText()).jsonObject.array("cases")!!.associate { case ->
                case.jsonObject.text("name")!! to case.jsonObject.obj("payload")!!
            }
        }
    }

    @Test fun failedTransactionLeavesNeitherIntentNorOutboxOnDisk() = runBlocking {
        val name = "rollback-${UUID.randomUUID()}.db"
        try {
            RoomWorkbenchStore.open(context, name).use { store ->
                try {
                    store.transaction {
                        put("pair", "command_intents", "msg", JsonObject(emptyMap()))
                        put("pair", "transport_outbox", "id", JsonObject(emptyMap()))
                        throw IllegalStateException("stop before commit")
                    }
                } catch (_: IllegalStateException) {
                    // Deliberate failure after both SQL writes.
                }
            }
            RoomWorkbenchStore.open(context, name).use { store ->
                store.transaction {
                    assertNull(get("pair", "command_intents", "msg"))
                    assertNull(get("pair", "transport_outbox", "id"))
                }
            }
        } finally {
            context.deleteDatabase(name)
        }
        Unit
    }

    @Test fun diskReopenPreservesPartialSnapshotQueueAndReceiptState() = runBlocking {
        val name = "reopen-${UUID.randomUUID()}.db"
        val pair = "test_pair"
        val peer = "test_local"
        val epoch = "epoch_demo_a"
        var now = 1794792300000L
        var install = ""
        lateinit var firstSend: RelayFrame
        try {
            RoomWorkbenchStore.open(context, name).use { store ->
                val repo = WorkbenchRepository(store, { now })
                repo.selectPair(pair)
                install = Protocol.frame(repo.hello()).json.text("incarnation")!!
                repo.peerOnline(peer, epoch)
                val sync = repo.nextFrames().single().message!!
                val page = Protocol.business(cases.getValue("snapshot_page_0"))
                repo.receive(Protocol.deliver(page, page.id, peer, install))
                repo.receive(Protocol.deliver(syncAck(sync, page), "del_first_ack", peer, install))
                assertFalse(repo.state.value.connection.synced)
            }
            RoomWorkbenchStore.open(context, name).use { store ->
                val repo = WorkbenchRepository(store, { now })
                repo.selectPair(pair)
                assertEquals(install, Protocol.frame(repo.hello()).json.text("incarnation"))
                repo.peerOnline(peer, epoch)
                assertTrue(repo.pendingReceipts().any { it.id == "del_first_ack" })
                val sync = repo.nextFrames().single().message!!
                assertEquals("snapshot_demo", sync.data.text("snapshotId"))
                val page = Protocol.business(cases.getValue("snapshot_page_1"))
                repo.receive(Protocol.deliver(syncAck(sync, page), "del_second_ack", peer, install))
                repo.receive(Protocol.deliver(page, page.id, peer, install))
                assertEquals(1, repo.state.value.records["positions"]?.size)
                val id = repo.enqueue("pause")
                firstSend = repo.nextFrames().single { it.message?.clientMsgId == id }
                repo.receive(Protocol.frame(Protocol.receipt(firstSend.delivery!!)))
                assertEquals(DeliveryStage.DELIVERED, repo.state.value.intents.single().stage)
            }
            RoomWorkbenchStore.open(context, name).use { store ->
                val repo = WorkbenchRepository(store, { now })
                repo.selectPair(pair)
                assertEquals(DeliveryStage.DELIVERED, repo.state.value.intents.single().stage)
                assertEquals(1, repo.state.value.records["positions"]?.size)
                repo.peerOnline("different_local", "epoch_b")
                assertTrue(repo.state.value.connection.recoveryRequired)
                assertEquals(DeliveryStage.QUARANTINED, repo.state.value.intents.single().stage)
                now += 32_000L
                assertTrue(repo.nextFrames().none { it.message?.clientMsgId == firstSend.message!!.clientMsgId })
            }
        } finally {
            context.deleteDatabase(name)
        }
        Unit
    }

    private fun syncAck(request: BusinessMessage, page: BusinessMessage): BusinessMessage {
        val id = request.clientMsgId!!
        return Protocol.business(JsonObject(cases.getValue("ack_sync") + mapOf(
            "id" to JsonPrimitive("ack_$id"), "dedupeKey" to JsonPrimitive("ack:$id"),
            "data" to buildJsonObject {
                put("cmd", "sync"); put("ok", true); put("error", JsonNull)
                put("refId", request.id); put("clientMsgId", id)
                put("requestHash", CanonicalJson.hash(request.data))
                putJsonObject("result") {
                    put("requestId", id); put("pageEventId", page.id)
                    for (key in listOf("serverEpoch", "snapshotId", "snapshotSeq", "pageIndex", "nextPageCursor", "hasMore")) {
                        put(key, page.data.getValue(key))
                    }
                }
            },
        )))
    }
}
