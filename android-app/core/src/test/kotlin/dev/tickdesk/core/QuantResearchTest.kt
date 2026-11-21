package dev.tickdesk.core

import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Test

class QuantResearchTest {
    private fun page(): JsonObject = contractResource("fixtures").array("cases")!!
        .map { it.jsonObject }.single { it.text("name") == "ack_get_quant_observations" }
        .obj("payload")!!.obj("data")!!.obj("result")!!
    private fun state(result: JsonObject = page()) = WorkbenchProjection(reads = mapOf(
        "get_status" to buildJsonObject { putJsonObject("protocol") {
            putJsonArray("supportedCommands") { add("get_quant_observations") }
        } }, "get_quant_observations" to result,
    ))

    @Test fun preservesFalseZeroAndUnknownSeparately() {
        val view = state().quantResearch()
        assertEquals("available", view.status)
        assertEquals(false, view.rows.single().exit.value)
        assertNull(view.rows.single().entry.value)
        assertEquals("missing_fundamental", view.rows.single().entry.reason)
        assertEquals("0", view.rows.single().score)
        assertNull(view.rows.single().scoreReason)
    }

    @Test fun newFailedScanCannotMakeOldObservationLookCurrent() {
        val state = state()
        val view = state.copy(reads = state.reads + ("scan_result" to buildJsonObject {
            put("scanId", "new-failed-scan"); put("status", "failed"); put("finishedAt", 9999999999999)
        })).quantResearch()
        assertEquals("stale", view.status)
        assertTrue(view.rows.isEmpty())
        assertNull(view.nextCursor)
    }

    @Test fun distinguishesUnknownCapabilityUnsupportedMissingAndFailed() {
        assertNull(WorkbenchProjection().quantResearch().supported)
        assertEquals(false, WorkbenchProjection(reads = mapOf("get_status" to buildJsonObject {
            putJsonObject("protocol") { putJsonArray("supportedCommands") {} }
        })).quantResearch().supported)
        for (status in listOf("missing", "failed")) {
            val result = JsonObject(page() + mapOf("status" to JsonPrimitive(status),
                "observations" to JsonArray(emptyList()), "error" to if (status == "failed") JsonPrimitive("DATA_UNAVAILABLE") else JsonNull))
            val view = state(result).quantResearch()
            assertEquals(status, view.status)
            assertTrue(view.rows.isEmpty())
        }
    }

    @Test fun freshReadMayBeNewerThanTheLastReceivedScanEvent() {
        val state = state()
        val view = state.copy(reads = state.reads + ("scan_result" to buildJsonObject {
            put("scanId", "older-event"); put("finishedAt", 1000)
        })).quantResearch()
        assertEquals("available", view.status)
        assertEquals(1, view.rows.size)
    }
}
