package dev.tickdesk.app

import androidx.test.platform.app.InstrumentationRegistry
import dev.tickdesk.core.DeliveryStage
import dev.tickdesk.core.number
import dev.tickdesk.core.text
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test

/** Test-only seed for the isolated acceptance pairing. */
class AcceptanceSeedTest {
    @Test(timeout = 60_000)
    fun seedHoldingForDeviceAcceptance() = runBlocking(Dispatchers.IO) {
        val args = InstrumentationRegistry.getArguments()
        assumeTrue(args.getString("seedHolding") == "true")
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val runtime = (context.applicationContext as TickDeskApplication).runtime
        val state = withTimeout(20_000) { runtime.state.first { it.canQueue && it.pairId != null && it.epoch != null } }
        val base = state.records["positions"].orEmpty().firstOrNull { it.text("symbol") == "300502.SZ" }
            ?.number("positionRevision") ?: 0
        val id = runtime.enqueue(
            "reconcile_holding",
            buildJsonObject {
                put("symbol", "300502.SZ")
                put("basePositionRevision", base)
                put("qty", 600)
                put("cost", "12")
                put("asOf", System.currentTimeMillis())
                put("reason", "device acceptance seed")
                put("resolvedExecutionIds", JsonArray(emptyList()))
                put("partitions", JsonArray(listOf(
                    buildJsonObject {
                        put("qty", 300)
                        put("restrictions", JsonArray(emptyList()))
                    },
                    buildJsonObject {
                        put("qty", 300)
                        put("restrictions", JsonArray(listOf(buildJsonObject {
                            put("kind", "freeze")
                            put("evidenceRef", "fixture:freeze")
                        })))
                    },
                )))
            },
            state.pairId,
            state.epoch,
        )
        val done = withTimeout(30_000) {
            runtime.state.first { current ->
                current.intents.any { it.id == id && it.stage in setOf(
                    DeliveryStage.APPLIED, DeliveryStage.REJECTED, DeliveryStage.QUARANTINED,
                ) }
            }
        }
        val intent = done.intents.single { it.id == id }
        assertEquals("seed rejected: ${intent.errorCode}", DeliveryStage.APPLIED, intent.stage)
        assertTrue(intent.result != null)
    }
}
