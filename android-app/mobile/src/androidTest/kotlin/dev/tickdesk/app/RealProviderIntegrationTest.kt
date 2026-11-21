package dev.tickdesk.app

import androidx.test.platform.app.InstrumentationRegistry
import dev.tickdesk.core.*
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test

/** Opt-in: uses the existing pairing and paid provider, and leaves the replies in App history. */
class RealProviderIntegrationTest {
    @Test fun deployedPythonProviderAnswersAndPersistsWithoutChangingHoldings() = runBlocking {
        assumeTrue(InstrumentationRegistry.getArguments().getString("realProviderTest") == "true")
        launchResumedMainActivity()
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val runtime = (context.applicationContext as TickDeskApplication).runtime
        val pair = requireNotNull(runtime.pairedScope())
        runtime.withReminderConnection {
            withTimeout(25_000) { runtime.state.first { it.connection.canWrite } }
            val holdings = runtime.state.value.records["positions"]
            for (question in listOf(
                "这是手机真实 AI 联调测试。请简述现有资料是否足以判断买卖；如果资料不足，请明确说明，不要补造数据。",
                "/分析 600000.SH",
            )) {
                val id = runtime.enqueue("chat", buildJsonObject { put("text", question) })
                val terminal = withTimeout(100_000) {
                    runtime.state.first { state -> state.intents.any {
                        it.id == id && it.stage in setOf(DeliveryStage.APPLIED, DeliveryStage.REJECTED)
                    } }
                }.intents.single { it.id == id }
                assertEquals("chat ${terminal.errorCode}", DeliveryStage.APPLIED, terminal.stage)
                val reply = withTimeout(10_000) {
                    runtime.state.first { state -> state.replies.any { it.text("requestId") == id } }
                }.replies.single { it.text("requestId") == id }
                assertEquals(reply.toString(), "completed", reply.text("status"))
                assertTrue(reply.text("text").orEmpty().isNotBlank())
                if (question.startsWith("/分析")) {
                    assertTrue(reply.text("text")!!.contains("量化比较"))
                    assertTrue(reply.text("text")!!.contains("财报"))
                }
                val historyId = runtime.enqueue("get_messages", buildJsonObject {})
                val history = withTimeout(15_000) {
                    runtime.state.first { state -> state.intents.any {
                        it.id == historyId && it.stage in setOf(DeliveryStage.APPLIED, DeliveryStage.REJECTED)
                    } }
                }.intents.single { it.id == historyId }
                assertEquals(DeliveryStage.APPLIED, history.stage)
                assertTrue(history.result!!.array("items")!!.any {
                    it.jsonObject.obj("reply")?.text("requestId") == id
                })
                assertEquals(holdings, runtime.state.value.records["positions"])
                println("REAL_PROVIDER_PASS: ${if (question.startsWith("/分析")) "analysis" else "chat"} request=$id")
            }
            assertEquals(pair, runtime.state.value.pairId)
        }
    }
}
