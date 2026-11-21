package dev.tickdesk.core

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import java.time.Instant
import java.time.LocalTime
import java.time.ZoneId

data class ReminderDestination(val pair: String, val epoch: String, val taskId: String, val displayUntil: Long)

/** A pointer to a reminder. Any display data must come from a fresh get_task result. */
data class ReminderReference(
    val epoch: String,
    val taskId: String,
    val reminderId: String,
    val taskVersion: Long,
    val scheduledAt: Long,
    val displayUntil: Long,
) {
    fun inWindow(now: Long): Boolean =
        scheduledAt >= 0 && displayUntil > scheduledAt &&
            displayUntil - scheduledAt <= MAX_WINDOW_MS && now >= scheduledAt && now < displayUntil

    /** Scheduling eligibility only: a received reference may wait up to one window for its start. */
    fun canAwaitWindow(now: Long): Boolean =
        inWindow(maxOf(now, scheduledAt)) && now >= scheduledAt - MAX_WINDOW_MS

    fun verify(result: JsonObject, now: Long): JsonObject? {
        if (!inWindow(now) || result.text("serverEpoch") != epoch || result.flag("recoveryMode") != false) return null
        val task = result.obj("task") ?: return null
        val reminder = task.obj("currentReminder") ?: return null
        if (task.text("taskId") != taskId || (task.number("version") ?: -1L) < taskVersion ||
            task.text("status") !in setOf("pending", "reminded") ||
            reminder.text("reminderId") != reminderId || reminder.number("scheduledAt") != scheduledAt ||
            reminder.number("displayUntil") != displayUntil || (task.number("validUntil") ?: 0L) <= now ||
            (task.number("snoozeUntil") ?: 0L) > now) return null
        return task
    }

    companion object {
        private const val MAX_WINDOW_MS = 60_000L

        fun fromPush(raw: String): ReminderReference {
            protocolCheck(raw.length <= 4096 && raw.toByteArray(Charsets.UTF_8).size <= 4096, "Push reference exceeds limit")
            val data = CanonicalJson.parse(raw)
            WireSchema.verify(data, WireSchema.business, "Push")
            val json = data.jsonObject
            val reference = ReminderReference(json.text("serverEpoch")!!, json.text("taskId")!!,
                json.text("reminderId")!!, json.number("taskVersion")!!, json.number("scheduledAt")!!,
                json.number("displayUntil")!!)
            protocolCheck(json.text("jump") == "task/${reference.taskId}" &&
                reference.inWindow(reference.scheduledAt) &&
                reference.displayUntil <= json.number("validUntil")!!, "Push reference window or target mismatch")
            return reference
        }

        fun fromBusiness(event: BusinessMessage): ReminderReference? {
            if (event.kind != "reminder") return null
            val epoch = event.epoch ?: return null
            val data = event.data
            val reminder = data.obj("currentReminder") ?: return null
            return ReminderReference(epoch, data.text("taskId")!!, reminder.text("reminderId")!!,
                data.number("version")!!, reminder.number("scheduledAt")!!, reminder.number("displayUntil")!!)
        }
    }
}

object ReminderQuietHours {
    private val shanghai = ZoneId.of("Asia/Shanghai")

    fun silence(settings: JsonObject?, level: String?, now: Long): Boolean {
        val start = settings?.text("nightStart")?.let { runCatching { LocalTime.parse(it) }.getOrNull() } ?: return false
        val end = settings.text("nightEnd")?.let { runCatching { LocalTime.parse(it) }.getOrNull() } ?: return false
        if (start == end) return false
        val localTime = Instant.ofEpochMilli(now).atZone(shanghai).toLocalTime()
        val atNight = if (start < end) localTime >= start && localTime < end else localTime >= start || localTime < end
        return atNight && !(level == "critical" && settings.flag("criticalAtNight") == true)
    }
}
