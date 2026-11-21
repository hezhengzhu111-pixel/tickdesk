package dev.tickdesk.core

import kotlinx.serialization.json.JsonObject

object BackgroundReminderPolicy {
    fun canStart(enabledPair: String?, state: WorkbenchProjection, visible: Boolean, notifications: Boolean) =
        visible && canKeepRunning(enabledPair, state, notifications)

    fun canKeepRunning(enabledPair: String?, state: WorkbenchProjection, notifications: Boolean) =
        enabledPair != null && enabledPair == state.pairId && notifications &&
            !state.connection.recoveryRequired && !state.requiresReview && !state.serviceRecoveryActive &&
            !ConnectionFailure.isTerminal(state.connectionError)

    /** Snapshot entries are only hints; the presenter must still perform a fresh get_task. */
    fun currentReferences(state: WorkbenchProjection, now: Long): List<ReminderReference> {
        if (!state.connection.canWrite || state.serviceRecoveryActive || state.requiresReview) return emptyList()
        val epoch = state.epoch ?: return emptyList()
        return state.records["tasks"].orEmpty().mapNotNull { task ->
            if (task.text("status") !in setOf("pending", "reminded") || (task.number("validUntil") ?: 0) <= now ||
                (task.number("snoozeUntil") ?: 0) > now) return@mapNotNull null
            val reminder = task.obj("currentReminder") ?: return@mapNotNull null
            ReminderReference(epoch, task.text("taskId") ?: return@mapNotNull null,
                reminder.text("reminderId") ?: return@mapNotNull null, task.number("version") ?: return@mapNotNull null,
                reminder.number("scheduledAt") ?: return@mapNotNull null,
                reminder.number("displayUntil") ?: return@mapNotNull null).takeIf { it.canAwaitWindow(now) }
        }
    }
}

fun ReminderDestination.verifyClick(result: JsonObject, reminderId: String, now: Long): Boolean {
    if (now >= displayUntil || result.text("serverEpoch") != epoch || result.flag("recoveryMode") != false) return false
    val task = result.obj("task") ?: return false
    val reminder = task.obj("currentReminder") ?: return false
    return task.text("taskId") == taskId && task.text("status") in setOf("pending", "reminded") &&
        (task.number("validUntil") ?: 0) > now && (task.number("snoozeUntil") ?: 0) <= now &&
        reminder.text("reminderId") == reminderId && reminder.number("displayUntil") == displayUntil &&
        (reminder.number("scheduledAt") ?: Long.MAX_VALUE) <= now
}
