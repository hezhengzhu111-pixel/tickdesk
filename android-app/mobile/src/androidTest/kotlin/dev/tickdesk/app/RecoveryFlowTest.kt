package dev.tickdesk.app

import androidx.test.platform.app.InstrumentationRegistry
import dev.tickdesk.app.data.RoomWorkbenchStore
import dev.tickdesk.core.*
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Test
import java.util.UUID

class RecoveryFlowTest {
    private val instrument = InstrumentationRegistry.getInstrumentation()
    private val fixtures = instrument.context.assets.open("v2/fixtures.json").bufferedReader().use { input ->
        Json.parseToJsonElement(input.readText()).jsonObject.array("cases")!!.associate {
            it.jsonObject.text("name")!! to it.jsonObject.obj("payload")!!
        }
    }
    private val now = 1794792300000L
    private val originalEpoch = "epoch_demo_a"
    private val restoredEpoch = "epoch_restored"
    private val pair = "test_pair"
    private val install = "test_install"

    @Test fun lateOriginalAckInvalidatesQueuedConfirmationAndNormalSnapshotPreservesManualReview() = runBlocking {
        val database = "recovery-flow-${UUID.randomUUID()}.db"
        val context = instrument.targetContext
        try {
            RoomWorkbenchStore.open(context, database).use { store ->
                var lastId = 0
                val repo = WorkbenchRepository(store, { now }, { if (lastId++ == 0) install else "id_$lastId" })
                repo.selectPair(pair)
                repo.peerOnline("original_peer", originalEpoch)
                sync(repo, "original_peer", originalEpoch, "before_restore", 42, false)
                val pauseId = repo.enqueue("pause")
                val pause = repo.nextFrames().single { it.message?.clientMsgId == pauseId }.message!!
                repo.peerOnline("restored_peer", restoredEpoch)
                sync(repo, "restored_peer", restoredEpoch, "during_restore", 43, true)
                assertTrue(repo.state.value.serviceRecoveryActive)
                assertFalse(repo.state.value.connection.canWrite)
                assertEquals(DeliveryStage.QUARANTINED, repo.intent(pauseId).stage)

                val initial = repo.collectRecoveryEvidence()
                assertTrue(initial.issues.isEmpty())
                assertEquals(setOf("command", "task_snapshot", "position_snapshot"), initial.sources.map { it.sourceType }.toSet())
                assertEquals(pause.json, initial.sources.single { it.sourceType == "command" }.payload)
                val params = buildJsonObject { put("reconciliationId", "restore_session"); put("reviewedSnapshotSeq", 43) }
                val stale = repo.enqueueRecovery("recovery_confirm", params, pair, initial.sources.toSet())

                val originalAck = ack(
                    pause, originalEpoch, 42, fixtures.getValue("ack_pause").obj("data")!!.obj("result")!!,
                )
                repo.receive(Protocol.deliver(originalAck, originalAck.id, "restored_peer", install))
                assertEquals("found", repo.intent(pauseId).queryState)
                assertEquals(false, repo.state.value.meta!!.obj("analysis")!!.flag("paused"))
                assertTrue(repo.nextFrames().none { it.message?.clientMsgId == stale })
                assertEquals("RECOVERY_EVIDENCE_CHANGED", repo.intent(stale).errorCode)

                val current = repo.collectRecoveryEvidence()
                assertTrue(current.issues.isEmpty())
                val ackSource = current.sources.single { it.sourceType == "ack" }
                assertEquals(originalAck.json, ackSource.payload)
                assertEquals(now, ackSource.capturedAt)
                assertEquals(42L, ackSource.observedSnapshotSeq)
                current.sources.forEach { it.prepare(restoredEpoch) }

                val confirmation = repo.enqueueRecovery("recovery_confirm", params, pair, current.sources.toSet())
                val request = repo.nextFrames().single { it.message?.clientMsgId == confirmation }.message!!
                val result = buildJsonObject {
                    put("reconciliationId", "restore_session"); put("serverEpoch", restoredEpoch)
                    put("recoveryMode", false); put("snapshotSeq", 44)
                }
                val confirmed = ack(request, restoredEpoch, 44, result)
                repo.receive(Protocol.deliver(confirmed, confirmed.id, "restored_peer", install))
                assertEquals(DeliveryStage.WAITING_FOR_SNAPSHOT, repo.intent(confirmation).stage)
                assertTrue(repo.state.value.serviceRecoveryActive)
                assertFalse(repo.state.value.canQueue)

                sync(repo, "restored_peer", restoredEpoch, "after_restore", 44, false)
                assertFalse(repo.state.value.serviceRecoveryActive)
                assertTrue(repo.state.value.requiresReview)
                assertEquals(DeliveryStage.APPLIED, repo.intent(confirmation).stage)
                assertFalse(repo.state.value.connection.canWrite)
                repo.markReviewed(pauseId)
                repo.finishReview()
                assertTrue(repo.state.value.connection.canWrite)
            }
        } finally {
            context.deleteDatabase(database)
        }
        Unit
    }

    private fun WorkbenchRepository.intent(id: String) = state.value.intents.single { it.id == id }

    private suspend fun sync(
        repo: WorkbenchRepository, peer: String, epoch: String, snapshot: String, sequence: Long, recovery: Boolean,
    ) {
        for (index in 0..1) {
            val request = repo.nextFrames().single { it.message?.command == "sync" }.message!!
            val template = fixtures.getValue("snapshot_page_$index")
            val fields = mapOf(
                "serverEpoch" to JsonPrimitive(epoch), "snapshotSeq" to JsonPrimitive(sequence),
                "snapshotId" to JsonPrimitive(snapshot), "recoveryMode" to JsonPrimitive(recovery),
                "requestId" to JsonPrimitive(request.clientMsgId!!),
            )
            val data = JsonObject(template.obj("data")!! + fields)
            val page = Protocol.business(JsonObject(template + mapOf(
                "id" to JsonPrimitive("${snapshot}_$index"), "dedupeKey" to JsonPrimitive("snapshot:$epoch:$snapshot:$index"),
                "serverEpoch" to fields.getValue("serverEpoch"), "snapshotSeq" to fields.getValue("snapshotSeq"),
                "data" to data,
            )))
            val reply = ack(request, epoch, sequence, buildJsonObject {
                put("requestId", request.clientMsgId); put("pageEventId", page.id)
                for (key in listOf("serverEpoch", "snapshotId", "snapshotSeq", "pageIndex", "nextPageCursor", "hasMore")) {
                    put(key, page.data.getValue(key))
                }
            })
            repo.receive(Protocol.deliver(page, page.id, peer, install))
            repo.receive(Protocol.deliver(reply, reply.id, peer, install))
        }
    }

    private fun ack(request: BusinessMessage, epoch: String, sequence: Long, result: JsonObject): BusinessMessage =
        Protocol.business(buildJsonObject {
            put("v", 2); put("kind", "ack"); put("id", "ack_${request.clientMsgId}")
            put("dedupeKey", "ack:${request.clientMsgId}"); put("ts", now)
            put("serverEpoch", epoch); put("snapshotSeq", sequence)
            putJsonObject("data") {
                put("refId", request.id); put("clientMsgId", request.clientMsgId)
                put("cmd", request.command); put("ok", true); put("error", JsonNull)
                put("requestHash", CanonicalJson.hash(request.data)); put("result", result)
            }
        })
}
