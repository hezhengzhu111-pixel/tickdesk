package dev.tickdesk.core

import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Test
import java.time.Instant

class ReminderVerificationTest {
    private val pushText = javaClass.getResource("/v2/push.example.json")!!.readText()
    private val hint = ReminderReference.fromPush(pushText)
    private val matchingTime = hint.scheduledAt + 1
    private val task = buildJsonObject {
        put("taskId", hint.taskId); put("version", hint.taskVersion); put("status", "reminded")
        put("validUntil", hint.displayUntil + 30_000); put("snoozeUntil", JsonNull)
        putJsonObject("currentReminder") {
            put("reminderId", hint.reminderId); put("scheduledAt", hint.scheduledAt); put("displayUntil", hint.displayUntil)
        }
    }

    @Test fun matchingFreshTaskRequiredAndExpiredHintsStaySilent() {
        assertEquals(task, hint.verify(result(), matchingTime))
        assertNull(hint.verify(result(epoch = "another_epoch"), matchingTime))
        assertNull(hint.verify(result(recovery = true), matchingTime))
        assertNull(hint.verify(result(), hint.displayUntil))
        assertNull(hint.verify(result(JsonObject(task + ("status" to JsonPrimitive("ignored")))), matchingTime))
        assertNull(hint.verify(result(JsonObject(task + ("snoozeUntil" to JsonPrimitive(hint.displayUntil)))), matchingTime))
        assertNull(hint.verify(result(JsonObject(task + ("version" to JsonPrimitive(hint.taskVersion - 1)))), matchingTime))
    }

    @Test fun malformedOrRedirectedPushIsRejected() {
        val payload = Json.parseToJsonElement(pushText).jsonObject
        assertThrows(ProtocolException::class.java) {
            ReminderReference.fromPush(JsonObject(payload + ("jump" to JsonPrimitive("task/some_other_task"))).toString())
        }
        assertThrows(ProtocolException::class.java) {
            ReminderReference.fromPush(JsonObject(payload + ("displayUntil" to JsonPrimitive(hint.scheduledAt + 60_001))).toString())
        }
        assertThrows(ProtocolException::class.java) {
            ReminderReference.fromPush("{\"v\":2," + pushText.trim().removePrefix("{"))
        }
    }

    @Test fun quietHoursCrossMidnightInShanghaiAndCriticalOptInIsRespected() {
        val settings = buildJsonObject { put("nightStart", "22:00"); put("nightEnd", "07:00"); put("criticalAtNight", true) }
        val midnight = Instant.parse("2026-11-15T16:30:00Z").toEpochMilli()
        val morning = Instant.parse("2026-11-15T23:00:00Z").toEpochMilli()
        assertTrue(ReminderQuietHours.silence(settings, "warn", midnight))
        assertFalse(ReminderQuietHours.silence(settings, "critical", midnight))
        assertFalse(ReminderQuietHours.silence(settings, "warn", morning))
        assertTrue(ReminderQuietHours.silence(JsonObject(settings + ("criticalAtNight" to JsonPrimitive(false))), "critical", midnight))
    }

    private fun result(row: JsonObject = task, epoch: String = hint.epoch, recovery: Boolean = false) = buildJsonObject {
        put("serverEpoch", epoch)
        put("recoveryMode", recovery)
        put("task", row)
    }
}
