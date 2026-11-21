package dev.tickdesk.core

data class ConnectionState(
    val paired: Boolean = false,
    val relayOnline: Boolean = false,
    val localOnline: Boolean = false,
    val synced: Boolean = false,
    val recoveryRequired: Boolean = false,
) {
    val canWrite: Boolean get() = paired && relayOnline && localOnline && synced && !recoveryRequired
}

enum class DeliveryStage {
    PENDING, SENT, DELIVERED, ACCEPTED, WAITING_FOR_SNAPSHOT, APPLIED, REJECTED,
    NEEDS_RECONCILIATION, QUARANTINED, CANCELLED;

    val businessSucceeded: Boolean get() = this == APPLIED
}
