package dev.tickdesk.core

import kotlinx.coroutines.*
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

class ReminderRetryCoordinator(
    private val scope: CoroutineScope,
    private val now: () -> Long,
    private val wait: suspend (Long) -> Unit = { delay(it) },
    private val isCurrent: (String, ReminderReference) -> Boolean,
    private val resolve: suspend (String, ReminderReference) -> Boolean,
) {
    private data class Key(val pair: String, val epoch: String, val reminder: String)
    private val active = mutableMapOf<Key, Job>()

    /** Resolve returns true when done, false only for a transient failure. One loop owns each reminder. */
    fun submit(pair: String, reference: ReminderReference): Job = synchronized(active) {
        val key = Key(pair, reference.epoch, reference.reminderId)
        active[key]?.let { return@synchronized it }
        val job = scope.launch(start = CoroutineStart.LAZY) {
            // Small server/device clock skew may place a received reminder just ahead of the phone.
            // Poll eligibility while waiting, then perform the usual fresh query only at/after its start.
            var startWaitBudget = 60_000L
            var attempts = 0
            while (attempts < 60) {
                val at = now()
                if (!reference.canAwaitWindow(at) || !isCurrent(pair, reference)) return@launch
                if (at < reference.scheduledAt) {
                    val pause = minOf(1_000L, reference.scheduledAt - at, startWaitBudget)
                    if (pause <= 0) return@launch
                    wait(pause)
                    startWaitBudget -= pause
                    continue
                }
                attempts++
                if (resolve(pair, reference)) return@launch
                val remaining = reference.displayUntil - now()
                if (remaining <= 0 || !isCurrent(pair, reference)) return@launch
                wait(minOf(1_000L, remaining))
            }
        }
        active[key] = job
        job.invokeOnCompletion { synchronized(active) { if (active[key] === job) active.remove(key) } }
        job.start()
        job
    }
}

object ReminderRetryPolicy {
    fun isCurrent(state: WorkbenchProjection, pair: String, reference: ReminderReference, now: Long): Boolean {
        if (state.pairId != pair || state.epoch != reference.epoch || state.connection.recoveryRequired ||
            state.requiresReview || state.serviceRecoveryActive || ConnectionFailure.isTerminal(state.connectionError)) return false
        if (!reference.canAwaitWindow(now)) return false
        val task = state.records["tasks"]?.firstOrNull { it.text("taskId") == reference.taskId } ?: return false
        if ((task.number("snoozeUntil") ?: 0) > now) return false
        // This validates the cached reference for scheduling, never for display. resolve must query again.
        return reference.verify(buildJsonObject {
            put("serverEpoch", state.epoch); put("recoveryMode", false); put("task", task)
        }, maxOf(now, reference.scheduledAt)) != null
    }
}
