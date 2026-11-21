package dev.tickdesk.core

import kotlinx.coroutines.*
import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Test

class ReminderRetryTest {
    private val reference = ReminderReference("epoch", "task", "reminder", 1, 100, 10_100)

    @Test fun futureReferenceWaits650msWithoutResolvingEarlyAndDuplicatesShareWait() = runBlocking {
        val future = reference.copy(scheduledAt = 1_000, displayUntil = 11_000)
        val projection = projection(future)
        var now = 350L
        var calls = 0
        val waits = mutableListOf<Long>()
        val waiting = CompletableDeferred<Unit>()
        val release = CompletableDeferred<Unit>()
        val queue = ReminderRetryCoordinator(this, { now }, { milliseconds ->
            waits += milliseconds
            waiting.complete(Unit)
            release.await()
            now += milliseconds
        }, { pair, hint -> ReminderRetryPolicy.isCurrent(projection, pair, hint, now) }) { _, _ ->
            assertTrue("Must not query or display before scheduledAt", now >= future.scheduledAt)
            calls++; true
        }
        val first = queue.submit("pair", future)
        yield()
        assertFalse("A short future reference must remain waiting", first.isCompleted)
        waiting.await()
        assertEquals(0, calls)
        assertSame(first, queue.submit("pair", future.copy(taskVersion = 2)))
        release.complete(Unit)
        first.join()
        assertEquals(listOf(650L), waits)
        assertEquals(1, calls)
        assertEquals(11_000L, future.displayUntil)
    }

    @Test fun farFutureAndExpiredReferencesNeverWaitOrResolve() = runBlocking {
        val future = reference.copy(scheduledAt = 61_001, displayUntil = 71_001)
        var waits = 0
        var calls = 0
        val queue = ReminderRetryCoordinator(this, { 1_000L }, { waits++ }, { _, _ -> true }) { _, _ -> calls++; true }
        queue.submit("pair", future).join()
        queue.submit("pair", reference.copy(scheduledAt = 100, displayUntil = 1_000)).join()
        assertEquals(0, waits)
        assertEquals(0, calls)
    }

    @Test fun futureWaitStopsOnTerminalSnoozeRecoveryOrExpiryBeforeAnyQuery() = runBlocking {
        val future = reference.copy(scheduledAt = 1_000, displayUntil = 11_000)
        val active = projection(future)
        val task = active.records.getValue("tasks").single()
        val changed = listOf(active.copy(serviceRecoveryActive = true), active.copy(pairId = "other"),
            active.copy(records = mapOf("tasks" to listOf(JsonObject(task + ("status" to JsonPrimitive("ignored")))))),
            active.copy(records = mapOf("tasks" to listOf(JsonObject(task + ("snoozeUntil" to JsonPrimitive(9_000)))))),
            active)
        for (next in changed) {
            var now = 350L
            var state = active
            var calls = 0
            var waits = 0
            val queue = ReminderRetryCoordinator(this, { now }, {
                waits++; state = next; now = if (next == active) future.displayUntil else future.scheduledAt
            }, { pair, hint -> ReminderRetryPolicy.isCurrent(state, pair, hint, now) }) { _, _ -> calls++; true }
            queue.submit("pair", future).join()
            assertEquals(1, waits)
            assertEquals(0, calls)
        }
    }

    @Test fun firstQueryTimeoutRetriesWhileSocketRemainsSyncedThenCompletes() = runBlocking {
        var now = 101L
        var calls = 0
        val waits = mutableListOf<Long>()
        val queue = ReminderRetryCoordinator(this, { now }, { wait -> waits += wait; now += wait }, { _, _ -> true }) { _, _ ->
            calls++
            if (calls == 1) { now += 5_000; false } else true
        }
        queue.submit("pair", reference).join()
        assertEquals(2, calls)
        assertEquals(listOf(1_000L), waits)
    }

    @Test fun retryStopsAtExpiryWithoutIssuingAnotherQuery() = runBlocking {
        var now = 101L
        var calls = 0
        val queue = ReminderRetryCoordinator(this, { now }, { wait -> now += wait }, { _, _ -> true }) { _, _ ->
            calls++; now += 5_000; false
        }
        queue.submit("pair", reference).join()
        assertEquals(2, calls)
        assertTrue(now >= reference.displayUntil)
    }

    @Test fun concurrentEventAndSnapshotReferencesShareOneLoop() = runBlocking {
        val started = CompletableDeferred<Unit>()
        val release = CompletableDeferred<Unit>()
        var calls = 0
        val queue = ReminderRetryCoordinator(this, { 101L }, { yield() }, { _, _ -> true }) { _, _ ->
            calls++; started.complete(Unit); release.await(); true
        }
        val first = queue.submit("pair", reference)
        started.await()
        val duplicates = (1..20).map { async(Dispatchers.Default) { queue.submit("pair", reference.copy(taskVersion = 2)) } }.awaitAll()
        assertTrue(duplicates.all { it === first })
        release.complete(Unit)
        first.join()
        assertEquals(1, calls)
    }

    @Test fun terminalSnoozedRecoveryAndChangedIdentityStopPendingRetry() = runBlocking {
        val active = projection()
        val task = active.records.getValue("tasks").single()
        val stopped = listOf(active.copy(pairId = "other"), active.copy(epoch = "new_epoch"),
            active.copy(serviceRecoveryActive = true), active.copy(requiresReview = true),
            active.copy(connection = active.connection.copy(recoveryRequired = true)),
            active.copy(connectionError = "INVALID_RELAY_FRAME"),
            active.copy(records = emptyMap()),
            active.copy(records = mapOf("tasks" to listOf(JsonObject(task + ("status" to JsonPrimitive("ignored")))))),
            active.copy(records = mapOf("tasks" to listOf(JsonObject(task + ("snoozeUntil" to JsonPrimitive(9_000)))))),
        )
        assertTrue(ReminderRetryPolicy.isCurrent(active, "pair", reference, 101))
        assertTrue(ReminderRetryPolicy.isCurrent(active.copy(connection = ConnectionState(paired = true)), "pair", reference, 101))
        for (next in stopped) {
            assertFalse(ReminderRetryPolicy.isCurrent(next, "pair", reference, 101))
            var projection = active
            var calls = 0
            var waits = 0
            val queue = ReminderRetryCoordinator(this, { 101L }, { waits++ },
                { pair, hint -> ReminderRetryPolicy.isCurrent(projection, pair, hint, 101) }) { _, _ ->
                calls++; projection = next; false
            }
            queue.submit("pair", reference).join()
            assertEquals(1, calls)
            assertEquals(0, waits)
        }
    }

    private fun projection(hint: ReminderReference = reference) = WorkbenchProjection(pairId = "pair", epoch = "epoch",
        connection = ConnectionState(true, true, true, true, false), records = mapOf("tasks" to listOf(buildJsonObject {
            put("taskId", "task"); put("version", 1); put("status", "reminded"); put("validUntil", 20_000)
            putJsonObject("currentReminder") { put("reminderId", "reminder"); put("scheduledAt", hint.scheduledAt); put("displayUntil", hint.displayUntil) }
        })))
}
