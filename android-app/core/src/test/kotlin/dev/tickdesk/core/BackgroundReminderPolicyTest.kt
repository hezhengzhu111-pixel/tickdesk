package dev.tickdesk.core

import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Test

class BackgroundReminderPolicyTest {
    private val ready = WorkbenchProjection(pairId = "pair", epoch = "epoch",
        connection = ConnectionState(true, true, true, true, false))

    @Test fun optInIsBoundToPairAndVisibleStartAndNotificationPermission() {
        assertTrue(BackgroundReminderPolicy.canStart("pair", ready, visible = true, notifications = true))
        assertFalse(BackgroundReminderPolicy.canStart(null, ready, true, true))
        assertFalse(BackgroundReminderPolicy.canStart("other", ready, true, true))
        assertFalse(BackgroundReminderPolicy.canStart("pair", ready, false, true))
        assertFalse(BackgroundReminderPolicy.canStart("pair", ready, true, false))
    }

    @Test fun everyNonRetryableRelayReasonStopsBackgroundService() {
        for (reason in listOf("FRAME_LIMIT", "BINARY_FRAME", "CREDENTIAL_REVOKED", "SESSION_REPLACED",
            "PROTOCOL_MISMATCH", "TLS_VALIDATION", "UNEXPECTED_REDIRECT", "PAIR_MISMATCH", "INVALID_RELAY_FRAME",
            "PAIRING_KEY_UNAVAILABLE")) {
            assertFalse(reason, BackgroundReminderPolicy.canKeepRunning("pair", ready.copy(connectionError = reason), true))
        }
        for (reason in listOf("DISCONNECTED", "HELLO_TIMEOUT", "RELAY_BUSY", "LOCAL_STORAGE_OR_SESSION")) {
            assertTrue(reason, BackgroundReminderPolicy.canKeepRunning("pair", ready.copy(connectionError = reason), true))
        }
    }

    @Test fun offlineConnectionCanReconnectButRecoveryAndUnpairStopTheService() {
        val offline = ready.copy(connection = ConnectionState(paired = true))
        assertTrue(BackgroundReminderPolicy.canKeepRunning("pair", offline, true))
        assertFalse(BackgroundReminderPolicy.canKeepRunning("pair", ready.copy(serviceRecoveryActive = true), true))
        assertFalse(BackgroundReminderPolicy.canKeepRunning("pair", ready.copy(requiresReview = true), true))
        assertFalse(BackgroundReminderPolicy.canKeepRunning("pair", ready.copy(pairId = null), true))
        assertFalse(BackgroundReminderPolicy.canKeepRunning("pair", ready, false))
        assertFalse(BackgroundReminderPolicy.canKeepRunning(null, ready, true))
        assertFalse(BackgroundReminderPolicy.canKeepRunning("pair", ready.copy(connectionError = "CREDENTIAL_REVOKED"), true))
        assertFalse(BackgroundReminderPolicy.canKeepRunning("pair", ready.copy(connectionError = "PAIRING_KEY_UNAVAILABLE"), true))
        assertTrue(BackgroundReminderPolicy.canKeepRunning("pair", offline.copy(connectionError = "DISCONNECTED"), true))
    }

    @Test fun reconnectOnlyConsidersCurrentUnexpiredRemindersFromFreshSnapshot() {
        val row = task()
        val state = ready.copy(records = mapOf("tasks" to listOf(row)))
        assertEquals("reminder", BackgroundReminderPolicy.currentReferences(state, 101).single().reminderId)
        assertTrue(BackgroundReminderPolicy.currentReferences(state, 200).isEmpty())
        assertTrue(BackgroundReminderPolicy.currentReferences(state.copy(connection = ConnectionState(paired = true)), 101).isEmpty())
        assertTrue(BackgroundReminderPolicy.currentReferences(state.copy(serviceRecoveryActive = true), 101).isEmpty())
        assertTrue(BackgroundReminderPolicy.currentReferences(state.copy(records = mapOf("tasks" to listOf(
            JsonObject(row + ("status" to JsonPrimitive("ignored")))))), 101).isEmpty())
    }

    @Test fun snapshotIncludesShortFutureReferenceButNotFarFutureOrExpired() {
        val task = JsonObject(task() + mapOf("validUntil" to JsonPrimitive(100_000),
            "currentReminder" to buildJsonObject {
                put("reminderId", "reminder"); put("scheduledAt", 61_000); put("displayUntil", 71_000)
            }))
        val state = ready.copy(records = mapOf("tasks" to listOf(task)))
        assertEquals("reminder", BackgroundReminderPolicy.currentReferences(state, 60_350).single().reminderId)
        assertEquals(1, BackgroundReminderPolicy.currentReferences(state, 1_000).size)
        assertTrue(BackgroundReminderPolicy.currentReferences(state, 999).isEmpty())
        assertTrue(BackgroundReminderPolicy.currentReferences(state, 71_000).isEmpty())
    }

    @Test fun clickingRequiresLiveSameEpochTaskAndCurrentReminderWithoutRevivingExpiredOnes() {
        val link = ReminderDestination("pair", "epoch", "task", 200)
        val result = buildJsonObject { put("serverEpoch", "epoch"); put("recoveryMode", false); put("task", task()) }
        assertTrue(link.verifyClick(result, "reminder", 101))
        assertFalse(link.verifyClick(result, "forged", 101))
        assertFalse(link.verifyClick(result, "reminder", 200))
        assertFalse(link.copy(epoch = "other").verifyClick(result, "reminder", 101))
        assertFalse(link.verifyClick(JsonObject(result + ("recoveryMode" to JsonPrimitive(true))), "reminder", 101))
        val filled = JsonObject(task() + ("status" to JsonPrimitive("filled")))
        assertFalse(link.verifyClick(JsonObject(result + ("task" to filled)), "reminder", 101))
    }

    private fun task() = buildJsonObject {
        put("taskId", "task"); put("version", 2); put("status", "reminded"); put("validUntil", 300)
        putJsonObject("currentReminder") { put("reminderId", "reminder"); put("scheduledAt", 100); put("displayUntil", 200) }
    }
}
