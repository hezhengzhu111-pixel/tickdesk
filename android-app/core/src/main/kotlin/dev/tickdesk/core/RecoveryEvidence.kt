package dev.tickdesk.core

import kotlinx.serialization.json.*
import java.util.Base64

data class RecoveryEvidenceCollection(
    val sources: List<RecoveryEvidenceSource>,
    val issues: List<String> = emptyList(),
)

data class RecoveryEvidenceSource(
    val sourceType: String,
    val capturedAt: Long,
    val observedSnapshotSeq: Long?,
    val payload: JsonObject,
) {
    fun prepare(currentEpoch: String): RecoveryEvidence {
        val epoch = payload.text("serverEpoch")
        protocolCheck(epoch != null && epoch != currentEpoch, "Evidence must retain an earlier epoch")
        protocolCheck(capturedAt in 0..CanonicalJson.MAX_INTEGER &&
            (observedSnapshotSeq == null || observedSnapshotSeq in 0..CanonicalJson.MAX_INTEGER),
            "Invalid evidence capture metadata")
        protocolCheck(sourceType in setOf("command", "ack", "task_snapshot", "position_snapshot"),
            "Invalid evidence source type")
        WireSchema.verify(payload, WireSchema.business, "Evidence_$sourceType")
        when (sourceType) {
            "command" -> protocolCheck(payload.obj("data")?.text("serverEpoch") == epoch, "Command epoch mismatch")
            "ack" -> protocolCheck(observedSnapshotSeq != null && payload.number("snapshotSeq")!! <= observedSnapshotSeq,
                "ACK capture sequence unavailable")
            else -> protocolCheck(capturedAt == payload.number("capturedAt") &&
                observedSnapshotSeq == payload.number("snapshotSeq"), "Snapshot capture metadata mismatch")
        }
        val bytes = CanonicalJson.encode(payload).toByteArray(Charsets.UTF_8)
        protocolCheck(bytes.size <= Protocol.BUSINESS_LIMIT, "Recovery evidence exceeds the record byte limit")
        return RecoveryEvidence(this, epoch!!, bytes)
    }
}

fun recoverySnapshotEvidence(
    sourceType: String, epoch: String, sequence: Long, capturedAt: Long, record: JsonObject,
) = RecoveryEvidenceSource(sourceType, capturedAt, sequence, buildJsonObject {
    put("serverEpoch", epoch)
    put("snapshotSeq", sequence)
    put("capturedAt", capturedAt)
    put("record", record)
})

class RecoveryEvidence internal constructor(
    val source: RecoveryEvidenceSource,
    private val epoch: String,
    private val bytes: ByteArray,
) {
    val payloadHash = CanonicalJson.sha256(bytes)
    val recordId = CanonicalJson.hash(buildJsonObject {
        put("sourceType", source.sourceType)
        put("payloadHash", payloadHash)
    })
    val partCount = (bytes.size + PART_SIZE - 1) / PART_SIZE

    fun uploadParts(sessionId: String): List<JsonObject> = List(partCount) { index ->
        val start = index * PART_SIZE
        val part = bytes.copyOfRange(start, minOf(start + PART_SIZE, bytes.size))
        buildJsonObject {
            put("reconciliationId", sessionId)
            put("recordId", recordId)
            put("sourceType", source.sourceType)
            put("payloadHash", payloadHash)
            put("partIndex", index)
            put("partCount", partCount)
            put("bytesBase64", Base64.getEncoder().encodeToString(part))
            put("evidenceEpoch", epoch)
            put("capturedAt", source.capturedAt)
            put("observedSnapshotSeq", source.observedSnapshotSeq)
        }
    }

    fun matches(remote: JsonObject): Boolean =
        recordId == remote.text("recordId") && source.sourceType == remote.text("sourceType") &&
            payloadHash == remote.text("payloadHash") && partCount.toLong() == remote.number("partCount")

    private companion object {
        const val PART_SIZE = 4096
    }
}

/** Digest every verified service record, including ones uploaded before this attempt. */
fun recoveryManifest(frozen: List<RecoveryEvidence>, records: List<JsonObject>): String? {
    val byId = records.associateBy { it.text("recordId")!! }
    if (byId.size != records.size || records.any {
            val partCount = it.number("partCount")
            it.text("status") != "verified" || partCount == null || partCount !in 1L..16L ||
                it.number("receivedParts") != partCount
        } || frozen.any { evidence -> byId[evidence.recordId]?.let(evidence::matches) != true }
    ) return null
    val manifest = byId.toSortedMap().values.map { record ->
        JsonObject(record.filterKeys { it in setOf("recordId", "sourceType", "payloadHash") })
    }
    return CanonicalJson.hash(JsonArray(manifest))
}
