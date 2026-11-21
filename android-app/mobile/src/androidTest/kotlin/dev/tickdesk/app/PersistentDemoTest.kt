package dev.tickdesk.app

import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import androidx.test.platform.app.InstrumentationRegistry
import dev.tickdesk.core.*
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.ByteArrayOutputStream

/** Explicitly populates the separate demo App through the real relay/local service. */
class PersistentDemoTest {
    @Test(timeout = 120_000) fun populateAndVerifyPersistentDemo() = runBlocking(Dispatchers.IO) {
        val instrument = InstrumentationRegistry.getInstrumentation()
        assumeTrue(InstrumentationRegistry.getArguments().getString("persistentDemo") == "true")
        val context = instrument.targetContext
        require(context.packageName == "dev.tickdesk.app.demo" && BuildConfig.MOCK_DEMO)
        launchResumedMainActivity()
        val runtime = (context.applicationContext as TickDeskApplication).runtime
        runtime.withReminderConnection {
            withTimeout(90_000) {
                val initial = runtime.state.first { it.connection.canWrite }
                require(initial.pairId!!.startsWith("demo_"))
                assertEquals(3, initial.records.getValue("positions").size)
                assertTrue(initial.records.getValue("tasks").isNotEmpty())
                val preferences = context.getSharedPreferences("demo-setup", 0)
                suspend fun command(cmd: String, data: JsonObject = buildJsonObject {}): JsonObject {
                    val id = runtime.enqueue(cmd, data)
                    val intent = runtime.state.first { state -> state.intents.any {
                        it.id == id && it.stage in setOf(DeliveryStage.APPLIED, DeliveryStage.REJECTED)
                    } }.intents.single { it.id == id }
                    assertEquals("$cmd: ${intent.errorCode}", DeliveryStage.APPLIED, intent.stage)
                    return requireNotNull(intent.result)
                }
                if (!preferences.getBoolean("populated-v1", false)) {
                    command("chat", buildJsonObject { put("text", "这是演示工作台，请说明可以验证哪些流程。") })
                    command("get_messages")
                    assertTrue(runtime.state.value.messages.any { it.text("role") == "assistant" })
                    val task = runtime.state.value.records.getValue("tasks").firstOrNull {
                        it.text("symbol") == "300502.SZ" && it.text("direction") == "buy" && it.text("status") == "pending"
                    }
                    if (task != null) {
                        command("act", buildJsonObject {
                            put("taskId", task.text("taskId")); put("baseActionVersion", task.number("actionVersion")!!)
                            put("side", "buy"); put("price", "12.25"); put("qty", 100)
                            put("executedAt", System.currentTimeMillis())
                        })
                        val unresolved = runtime.state.value.records.getValue("executions")
                            .filter { it.text("positionEffect") == "needs_reconciliation" }
                            .map { JsonPrimitive(it.text("executionId")!!) }
                        command("adjust_position", buildJsonObject {
                            put("symbol", "300502.SZ"); put("basePositionRevision", runtime.state.value.meta!!.number("positionRevision")!!)
                            put("qty", 300); put("availableQty", 300); put("cost", "10.75")
                            put("asOf", System.currentTimeMillis()); put("reason", "模拟成交后的持仓核对")
                            put("resolvedExecutionIds", JsonArray(unresolved)); put("pendingLots", JsonArray(emptyList()))
                        })
                    }
                    val state = runtime.state.value
                    val upload = runtime.startUpload("模拟持仓截图.png", "image/png", sampleImage(), state.pairId!!, state.epoch!!)
                    val transfer = runtime.state.first { it.transfers.any { row -> row.id == upload && !row.needsDelivery } }
                        .transfers.single { it.id == upload }
                    assertEquals(FileTransferStatus.COMPLETE, transfer.status)
                    val notice = runtime.state.value.meta!!.obj("visionNotice")!!.text("noticeVersion")!!
                    command("accept_vision_notice", buildJsonObject { put("noticeVersion", notice); put("accepted", true) })
                    command("register_image", buildJsonObject { put("importId", "demo_screenshot"); put("fileId", transfer.fileId) })
                    val recognized = runtime.state.value.records.getValue("imports").single { it.text("importId") == "demo_screenshot" }
                    assertEquals("recognized", recognized.text("status"))
                    val preview = command("apply_import", buildJsonObject {
                        put("importId", "demo_screenshot"); put("phase", "preview"); put("snapshotAsOf", System.currentTimeMillis())
                        putJsonArray("rows") {
                            for ((index, row) in runtime.state.value.records.getValue("positions").withIndex()) add(buildJsonObject {
                                put("rowId", "row${index + 1}"); put("symbol", row.text("symbol")); put("name", row.text("name"))
                                put("qty", row.number("qty")!!); put("availableQty", row.number("availableQty")!!); put("cost", row.text("cost"))
                                put("pendingLots", JsonArray(emptyList())); put("resolvedExecutionIds", JsonArray(emptyList()))
                            })
                        }
                    })
                    assertEquals(true, preview.flag("readyForConfirm"))
                    assertTrue(preferences.edit().putBoolean("populated-v1", true).commit())
                }
                command("get_messages")
                command("list_files")
                command("get_candidates")
                assertEquals(3, runtime.state.value.records.getValue("positions").size)
                assertTrue(runtime.state.value.records.getValue("imports").any { it.text("importId") == "demo_screenshot" })
                assertTrue(runtime.state.value.messages.any { it.text("role") == "assistant" })
                println("PASS: persistent demo has live synced tasks, 3 positions, chat history, saved screenshot and import preview")
            }
        }
        Unit
    }

    private fun sampleImage(): ByteArray {
        val bitmap = Bitmap.createBitmap(800, 480, Bitmap.Config.ARGB_8888)
        try {
            val canvas = Canvas(bitmap)
            canvas.drawColor(Color.rgb(18, 28, 46))
            val paint = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.WHITE; textSize = 28f }
            canvas.drawText("TickDesk 模拟持仓截图 · 联调专用", 32f, 60f, paint)
            canvas.drawText("证券代码         数量       可用       成本", 32f, 135f, paint)
            canvas.drawText("300502.SZ       300        300        10.75", 32f, 210f, paint)
            canvas.drawText("600000.SH      1000       1000       10.00", 32f, 285f, paint)
            canvas.drawText("000001.SZ       500        500        11.00", 32f, 360f, paint)
            return ByteArrayOutputStream().use { out ->
                check(bitmap.compress(Bitmap.CompressFormat.PNG, 100, out)); out.toByteArray()
            }
        } finally { bitmap.recycle() }
    }
}
