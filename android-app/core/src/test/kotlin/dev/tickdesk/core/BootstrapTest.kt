package dev.tickdesk.core

import org.junit.Assert.*
import org.junit.Test

class BootstrapTest {
    @Test fun connectionRequiresPairPeerAndSyncWithoutRecovery() {
        assertFalse(ConnectionState().canWrite)
        val online = ConnectionState(paired = true, relayOnline = true, localOnline = true, synced = true)
        assertTrue(online.canWrite)
        assertFalse(online.copy(paired = false).canWrite)
        assertFalse(online.copy(localOnline = false).canWrite)
        assertFalse(online.copy(relayOnline = false).canWrite)
        assertFalse(online.copy(synced = false).canWrite)
        assertFalse(online.copy(recoveryRequired = true).canWrite)
    }

    @Test fun receiptCannotBePresentedAsBusinessSuccess() {
        assertFalse(DeliveryStage.DELIVERED.businessSucceeded)
        assertFalse(DeliveryStage.NEEDS_RECONCILIATION.businessSucceeded)
        assertTrue(DeliveryStage.APPLIED.businessSucceeded)
    }

    @Test fun completeVendoredContractIsAvailableButNotAdvertisedAsImplemented() {
        assertTrue(ContractCatalog.definedCommands().size >= 42)
        assertTrue(ContractCatalog.definedCommands().containsAll(listOf("get_command_status", "delete_file", "record_execution", "get_execution_progress", "set_execution_progress")))
        assertTrue(ContractCatalog.implementedCommands.isEmpty())
    }
}
