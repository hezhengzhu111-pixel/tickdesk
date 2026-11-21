package dev.tickdesk.core

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject

data class ReportCatalogueQuery(val symbol: String = "", val cursor: String? = null, val limit: Long = 10)

data class ReportReads(
    val catalogues: Map<ReportCatalogueQuery, JsonObject> = emptyMap(),
    val headers: Map<String, JsonObject> = emptyMap(),
)

internal fun reportReads(epoch: String?, intents: Collection<JsonObject>): ReportReads {
    if (epoch == null) return ReportReads()
    val reports = intents.mapNotNull { row ->
        val request = row.obj("request")?.obj("data") ?: return@mapNotNull null
        val command = request.text("cmd")
        if ((command != "list_reports" && command != "get_report") || request.text("serverEpoch") != epoch) {
            return@mapNotNull null
        }
        val ack = row.obj("ack") ?: return@mapNotNull null
        if (ack.text("serverEpoch") != epoch || ack.obj("data")?.flag("ok") != true) return@mapNotNull null
        request to ack
    }.sortedWith(compareBy({ it.second.number("snapshotSeq") }, { it.second.number("ts") }, { it.second.text("id") }))

    val catalogues = mutableMapOf<ReportCatalogueQuery, JsonObject>()
    val headers = mutableMapOf<String, JsonObject>()
    for ((request, ack) in reports) {
        val result = ack.obj("data")?.obj("result") ?: continue
        if (request.text("cmd") == "list_reports") {
            val query = ReportCatalogueQuery(request.text("symbol").orEmpty(), request.text("cursor"), request.number("limit") ?: 10)
            catalogues[query] = result
            for (item in result.array("items").orEmpty()) {
                val header = item.jsonObject
                header.text("reportId")?.let { headers[it] = header }
            }
        } else {
            val id = request.text("reportId") ?: continue
            val header = result.obj("header") ?: continue
            if (header.text("reportId") == id) headers[id] = header
        }
    }
    return ReportReads(catalogues, headers)
}
