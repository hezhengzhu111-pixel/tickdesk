package dev.tickdesk.app

import android.os.SystemClock
import android.util.Log
import androidx.test.platform.app.InstrumentationRegistry
import dev.tickdesk.core.*
import kotlinx.serialization.json.*
import org.junit.Assert.assertTrue
import org.junit.Test

class ProtocolLoadTest {
    @Test fun twentyTaskSnapshotValidationHasBoundedDeviceCost() {
        val cases = InstrumentationRegistry.getInstrumentation().context.assets.open("v2/fixtures.json").bufferedReader().use {
            Json.parseToJsonElement(it.readText()).jsonObject.array("cases")!!
                .associate { row -> row.jsonObject.text("name")!! to row.jsonObject.obj("payload")!! }
        }
        val task = cases.getValue("task").obj("data")!!
        val original = cases.getValue("snapshot_page_0")
        val fields = original.obj("data")!!.toMutableMap()
        for (name in listOf("positions", "watchlist", "imports", "executions", "adjustments", "valuationPoints")) {
            fields[name] = JsonArray(emptyList())
        }
        fields["tasks"] = JsonArray((0..19).map { JsonObject(task + ("taskId" to JsonPrimitive("load_$it"))) })
        val body = JsonObject(fields)
        val page = JsonObject(original + ("data" to body))
        // Schema cost only; the modified manifest is intentionally not a sync test.
        val start = SystemClock.elapsedRealtime()
        repeat(10) { Protocol.business(page) }
        val elapsed = SystemClock.elapsedRealtime() - start
        Log.i("TickDeskThreeTier", "twenty_task_schema_10_iterations_ms=$elapsed")
        assertTrue("Ten 20-task validations took $elapsed ms", elapsed < 5_000)
    }
}
