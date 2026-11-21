package dev.tickdesk.core

import kotlinx.serialization.json.*

data class ConfirmedSnapshot(
    val epoch: String,
    val sequence: Long,
    val recoveryMode: Boolean,
    val generatedAt: Long,
    val meta: JsonObject,
    val records: Map<String, List<JsonObject>>,
)

object SnapshotAssembly {
    val collections = linkedMapOf(
        "tasks" to "taskId", "positions" to "symbol", "executions" to "executionId",
        "watchlist" to "symbol", "imports" to "importId", "valuationPoints" to "pointId",
        "adjustments" to "adjustmentId",
    )

    /** A page can retain an earlier requestId when the server reuses an immutable event. */
    fun linked(page: BusinessMessage, ack: BusinessMessage): Boolean {
        val result = ack.data.obj("result") ?: return false
        return page.kind == "state_snapshot" && ack.kind == "ack" && ack.command == "sync" &&
            ack.data.flag("ok") == true && ack.epoch == page.epoch && result.text("pageEventId") == page.id &&
            listOf("serverEpoch", "snapshotId", "snapshotSeq", "pageIndex", "nextPageCursor", "hasMore")
                .all { result[it] == page.data[it] }
    }

    fun assemble(pages: Collection<BusinessMessage>, now: Long): ConfirmedSnapshot? {
        if (pages.isEmpty()) return null
        val ordered = pages.sortedBy { it.data.number("pageIndex") }
        val first = ordered.first().data
        val fields = listOf("serverEpoch", "snapshotId", "snapshotSeq", "recoveryMode", "generatedAt", "expiresAt")
        protocolCheck(ordered.all {
            it.kind == "state_snapshot" && fields.all { field -> first[field] == it.data[field] }
        }, "Mixed snapshot pages")
        protocolCheck(first.number("expiresAt")!! > now, "Snapshot expired")
        protocolCheck(ordered.map { it.data.number("pageIndex") }.distinct().size == ordered.size, "Duplicate snapshot index")
        for (page in ordered) {
            val data = page.data
            protocolCheck((data.number("pageIndex") == 0L) == (data.obj("manifest") != null) &&
                (data.number("pageIndex") == 0L) == (data.obj("meta") != null), "Invalid snapshot metadata placement")
            protocolCheck(data.flag("hasMore") == (data.text("nextPageCursor") != null), "Invalid snapshot cursor")
        }
        protocolCheck(ordered.dropLast(1).all { it.data.flag("hasMore") == true }, "Data follows final snapshot page")
        if (ordered.last().data.flag("hasMore") == true ||
            ordered.withIndex().any { (index, page) -> page.data.number("pageIndex") != index.toLong() }) return null
        val records = collections.mapValues { (name, key) ->
            val rows = ordered.flatMap { page -> page.data.array(name)!!.map { it.jsonObject } }
            val ids = rows.map { it.text(key)!! }
            protocolCheck(ids.zipWithNext().all { (a, b) -> a < b }, "Unsorted or duplicate snapshot keys")
            rows
        }
        val manifest = first.obj("manifest")!!
        val meta = first.obj("meta")!!
        (records + ("meta" to listOf(meta))).forEach { (key, rows) ->
            val entry = manifest.obj(key)!!
            protocolCheck(entry.number("count") == rows.size.toLong() &&
                entry.text("sha256") == CanonicalJson.hash(JsonArray(rows)), "Snapshot manifest mismatch")
        }
        return ConfirmedSnapshot(first.text("serverEpoch")!!, first.number("snapshotSeq")!!,
            first.flag("recoveryMode")!!, first.number("generatedAt")!!, meta, records)
    }
}
