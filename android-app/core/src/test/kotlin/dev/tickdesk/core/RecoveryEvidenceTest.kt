package dev.tickdesk.core

import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Test
import java.util.Base64

class RecoveryEvidenceTest {
    @Test fun encodesFullRecordsFromTheCrossPlatformVectors() {
        for (vector in contractResource("evidence-vectors").array("records")!!.map { it.jsonObject }) {
            val payload = vector.obj("payload")!!
            val evidence = RecoveryEvidenceSource(
                vector.text("sourceType")!!,
                payload.number("capturedAt") ?: payload.number("ts")!!,
                payload.number("snapshotSeq"),
                payload,
            ).prepare("restored")
            assertEquals(vector.text("payloadHash"), evidence.payloadHash)
            val parts = evidence.uploadParts("session")
            assertEquals(vector.array("parts"), JsonArray(parts.map { it.getValue("bytesBase64") }))
            assertTrue(parts.all { it.text("reconciliationId") == "session" })
        }
    }

    @Test fun splitsCanonicalUtf8AndKeepsPartMetadataStable() {
        val command = Protocol.command("chat", "event", "intent", "old", 123, buildJsonObject {
            put("text", "消息".repeat(1800))
        })
        val source = RecoveryEvidenceSource("command", 123, null, command.json)
        val evidence = source.prepare("new")
        val parts = evidence.uploadParts("session")
        assertTrue(parts.size > 1)
        assertEquals(parts.map { it.number("partIndex") }, parts.indices.map { it.toLong() })
        assertTrue(parts.all { it.number("partCount") == parts.size.toLong() })
        assertTrue(parts.all { it.text("recordId") == evidence.recordId && it["observedSnapshotSeq"] == JsonNull })
        val bytes = parts.flatMap { Base64.getDecoder().decode(it.text("bytesBase64")!!).toList() }.toByteArray()
        assertEquals(command.json, CanonicalJson.decode(bytes))
        assertEquals(evidence.recordId, source.prepare("new").recordId)
    }

    @Test fun refusesCurrentEpochAndAckWithUnknownObservedSequence() {
        val vectors = contractResource("evidence-vectors").array("records")!!.map { it.jsonObject }
        val command = vectors.first { it.text("sourceType") == "command" }.obj("payload")!!
        val ack = vectors.first { it.text("sourceType") == "ack" }.obj("payload")!!
        assertThrows(ProtocolException::class.java) {
            RecoveryEvidenceSource("command", 123, null, command).prepare(command.text("serverEpoch")!!)
        }
        assertThrows(ProtocolException::class.java) {
            RecoveryEvidenceSource("ack", 123, null, ack).prepare("new")
        }
    }

    @Test fun sealRequiresEveryFrozenRecordAndEveryServiceRecordToBeVerified() {
        val command = Protocol.command("pause", "event", "intent", "old", 123)
        val evidence = RecoveryEvidenceSource("command", 123, null, command.json).prepare("new")
        val verified = buildJsonObject {
            put("recordId", evidence.recordId)
            put("sourceType", evidence.source.sourceType)
            put("payloadHash", evidence.payloadHash)
            put("partCount", 1)
            put("receivedParts", 1)
            put("status", "verified")
        }
        val incomplete = JsonObject(verified + ("status" to JsonPrimitive("receiving")))
        assertNull(recoveryManifest(listOf(evidence), emptyList()))
        assertNull(recoveryManifest(listOf(evidence), listOf(incomplete)))
        assertEquals(CanonicalJson.hash(JsonArray(emptyList())), recoveryManifest(emptyList(), emptyList()))
        assertEquals(CanonicalJson.hash(JsonArray(listOf(JsonObject(
            verified.filterKeys { it in setOf("recordId", "sourceType", "payloadHash") },
        )))), recoveryManifest(listOf(evidence), listOf(verified)))
    }
}
