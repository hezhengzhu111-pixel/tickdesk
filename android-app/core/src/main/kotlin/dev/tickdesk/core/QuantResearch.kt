package dev.tickdesk.core

import kotlinx.serialization.json.*

data class ResearchVerdict(val value: Boolean?, val reason: String?)
data class ResearchObservation(
    val symbol: String, val strategyId: String, val group: String, val timeframe: String,
    val admission: String, val barEndMs: Long?, val barStatus: String,
    val entry: ResearchVerdict, val exit: ResearchVerdict, val score: String?, val scoreReason: String?,
)
data class QuantResearchView(
    val supported: Boolean?, val loading: Boolean, val status: String, val error: String?,
    val scanId: String?, val decisionCutoffMs: Long?, val historical: Boolean,
    val rows: List<ResearchObservation>, val nextCursor: String?, val requestedScanId: String?,
)

fun WorkbenchProjection.quantResearch(): QuantResearchView {
    val command = "get_quant_observations"
    val supported = reads["get_status"]?.obj("protocol")?.array("supportedCommands")
        ?.any { it.jsonPrimitive.content == command }
    val intent = intents.lastOrNull { it.command == command }
    val loading = intent?.stage in setOf(DeliveryStage.PENDING, DeliveryStage.SENT,
        DeliveryStage.DELIVERED, DeliveryStage.ACCEPTED, DeliveryStage.WAITING_FOR_SNAPSHOT)
    val page = reads[command]
    val latestScan = listOfNotNull(reads["scan_result"], reads["scan_now"])
        .maxByOrNull { it.number("finishedAt") ?: Long.MIN_VALUE }
    val latest = latestScan?.text("scanId")
        ?: meta?.obj("analysis")?.text("lastScanId")
    val source = intents.lastOrNull { it.command == command && it.result == page }
    val explicit = source?.data?.text("scanId")
    val latestFinished = latestScan?.number("finishedAt")
    val newerCutoff = page?.number("decisionCutoffMs")?.let { cutoff -> latestFinished?.let { cutoff >= it } } == true
    val readAfterScan = source != null && source.data.text("cursor") == null &&
        latestFinished != null && source.at >= latestFinished
    val stale = explicit == null && latest != null && page != null && page.text("scanId") != latest &&
        !newerCutoff && !readAfterScan
    val rejected = intent?.stage == DeliveryStage.REJECTED
    val rows = if (stale || rejected) emptyList() else page?.array("observations").orEmpty().map { value ->
        val row = value.jsonObject
        fun verdict(key: String) = ResearchVerdict(row.obj(key)?.flag("value"), row.obj(key)?.text("reason"))
        ResearchObservation(row.text("symbol")!!, row.text("strategyId")!!, row.text("group")!!,
            row.text("timeframe")!!, row.text("admission")!!, row.obj("bar")?.number("endMs"),
            row.obj("bar")?.text("status") ?: "unknown", verdict("entry"), verdict("exit"),
            row.obj("score")?.text("value"), row.obj("score")?.text("reason"))
    }
    return QuantResearchView(supported, loading,
        when { rejected -> "failed"; stale -> "stale"; else -> page?.text("status") ?: "unread" },
        if (rejected) intent.errorCode else page?.text("error"), page?.text("scanId"),
        page?.number("decisionCutoffMs"), page?.flag("historical") == true, rows,
        if (stale || rejected || page?.flag("hasMore") != true) null else page.text("nextCursor"), explicit)
}
