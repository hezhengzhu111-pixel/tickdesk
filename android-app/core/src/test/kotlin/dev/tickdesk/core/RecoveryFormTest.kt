package dev.tickdesk.core

import kotlinx.serialization.json.*
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class RecoveryFormTest {
    @Test
    fun includedReadsJsonNumbersAsNumbersAndKeepsUnknownAvailableNull() {
        val draft = buildJsonObject {
            put("positionSymbol", "300502.SZ")
            put("basePositionRevision", 3L)
            put("positionQty", 600L)
            put("availableQty", 250L)
            put("cost", "0")
            put("positionAsOf", 1_000_000L)
        }
        val parsed = RecoveryForms.included(draft, 3, setOf("execution_1"))
        assertTrue("${parsed.errors}", parsed.ok)
        val value = parsed.value!!
        assertEquals(3L, value.basePositionRevision)
        assertEquals(600L, value.qty)
        assertEquals(250L, value.availableQty)
        assertEquals("0", value.cost)
        assertEquals(1_000_000L, value.asOf)
        assertEquals(listOf("execution_1"), value.resolvedExecutionIds)

        val unknown = RecoveryForms.included(
            JsonObject(draft.toMutableMap().also { it.remove("availableQty") }),
            3,
            emptySet(),
        )
        assertTrue("${unknown.errors}", unknown.ok)
        assertNull(unknown.value!!.availableQty)
    }

    @Test
    fun missingFieldsStayFieldErrorsAndAreNeverReplacedByZeroOrNow() {
        val parsed = RecoveryForms.included(buildJsonObject { put("positionSymbol", "300502.SZ") }, 7, emptySet())
        assertEquals(false, parsed.ok)
        assertTrue("basePositionRevision" in parsed.errors)
        assertTrue("positionQty" in parsed.errors)
        assertTrue("cost" in parsed.errors)
        assertTrue("positionAsOf" in parsed.errors)
        assertTrue("availableQty" !in parsed.errors)
    }

    @Test
    fun numberTypedFieldsAreNotReadAsText() {
        val draft = buildJsonObject {
            put("positionSymbol", "300502.SZ")
            put("basePositionRevision", 3L)
            put("positionQty", 600L)
            put("availableQty", 250L)
            put("cost", "12")
            put("positionAsOf", 1_000_000L)
        }
        assertNull(draft.text("basePositionRevision"))
        val parsed = RecoveryForms.included(draft, 3, emptySet())
        assertEquals(3L, parsed.value?.basePositionRevision)
    }

    @Test
    fun staleGlobalBaselineIsAFieldError() {
        val draft = buildIncluded()
        val parsed = RecoveryForms.included(draft, 4, emptySet())
        assertEquals(false, parsed.ok)
        assertTrue(parsed.errors["basePositionRevision"]!!.contains("4"))
    }

    @Test
    fun recordBatchKeepsThePreallocatedExecutionIdAndCanCarryTheSameBatchBaseline() {
        val record = buildJsonObject {
            put("symbol", "300502.SZ")
            put("side", "sell")
            put("price", "11.25")
            put("qty", 250L)
            put("executedAt", 2_000_000L)
            put("fees", "0")
        }
        val parsed = RecoveryForms.recordBatch(record, "recovery_exec_1", buildIncluded(), 3, setOf("recovery_exec_1"))
        assertTrue("${parsed.errors}", parsed.ok)
        assertEquals("recovery_exec_1", parsed.value!!.record!!.newExecutionId)
        assertEquals(listOf("recovery_exec_1"), parsed.value!!.included!!.resolvedExecutionIds)
        assertEquals(250L, parsed.value!!.record!!.qty)
        assertEquals("0", parsed.value!!.record!!.fees)
    }

    @Test
    fun invalidDecimalsAndIdentityAreReportedBeforeSubmit() {
        val record = buildJsonObject {
            put("symbol", "300502")
            put("side", "hold")
            put("price", "-1")
            put("qty", 0L)
            put("executedAt", 0L)
        }
        val parsed = RecoveryForms.record(record, "bad id with spaces")
        assertEquals(false, parsed.ok)
        for (key in listOf("symbol", "side", "price", "qty", "executedAt", "newExecutionId")) {
            assertTrue("missing $key: ${parsed.errors}", key in parsed.errors)
        }
    }

    private fun buildIncluded() = buildJsonObject {
        put("positionSymbol", "300502.SZ")
        put("basePositionRevision", 3L)
        put("positionQty", 600L)
        put("availableQty", 250L)
        put("cost", "12")
        put("positionAsOf", 1_000_000L)
    }
}
