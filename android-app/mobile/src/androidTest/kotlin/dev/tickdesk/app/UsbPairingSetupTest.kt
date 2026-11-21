package dev.tickdesk.app

import android.os.ParcelFileDescriptor
import android.util.Log
import androidx.test.platform.app.InstrumentationRegistry
import dev.tickdesk.app.data.PairingVault
import dev.tickdesk.app.data.readBounded
import dev.tickdesk.core.Protocol
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/** Explicit developer setup, never replaces a pairing; token data stays on device and out of logs. */
class UsbPairingSetupTest {
    @Test(timeout = 60_000) fun pairUnpairedUsbDeviceToKnownBackend() = runBlocking(Dispatchers.IO) {
        val instrument = InstrumentationRegistry.getInstrumentation()
        val args = InstrumentationRegistry.getArguments()
        assumeTrue(args.getString("usbPairingSetup") == "true")
        val context = instrument.targetContext
        stage("vault_check_begin")
        assumeTrue("Device already paired; refusing replacement", PairingVault(context).read() == null)
        launchResumedMainActivity()
        stage("vault_unpaired")
        val expectedPin = requireNotNull(args.getString("expectedPin"))
        stage("shell_read_begin")
        val pipe = AtomicReference<ParcelFileDescriptor?>()
        val reader = Executors.newSingleThreadExecutor { action -> Thread(action, "usb-pairing-read").apply { isDaemon = true } }
        val future = reader.submit<String> {
            val descriptor = instrument.uiAutomation.executeShellCommand("cat /data/local/tmp/tickdesk-usb-pairing.json")
            pipe.set(descriptor)
            stage("shell_pipe_opened")
            ParcelFileDescriptor.AutoCloseInputStream(descriptor).use { input ->
                val bytes = input.readBounded(8192)
                try { bytes.toString(Charsets.UTF_8) } finally { bytes.fill(0) }
            }
        }
        val raw = try { future.get(5, TimeUnit.SECONDS) }
        finally { pipe.get()?.close(); future.cancel(true); reader.shutdownNow() }
        stage("shell_read_complete")
        val config = Protocol.pairing(raw)
        require(config.relayUrl == "wss://127.0.0.1:8443/relay/v2/connect") { "Unexpected USB relay endpoint" }
        require(config.pins == listOf(expectedPin)) { "Unexpected USB relay pin" }
        stage("configuration_validated")
        stage("runtime_init_begin")
        val runtime = (context.applicationContext as TickDeskApplication).runtime
        stage("paired_scope_begin")
        require(withTimeout(10_000) { runtime.pairedScope() } == null) { "Pairing changed before setup" }
        stage("paired_scope_unpaired")
        withTimeout(30_000) {
            runtime.withReminderConnection {
                stage("temporary_connection_lease_acquired")
                runtime.pair(config)
                stage("pair_saved_waiting_for_sync")
                runtime.state.first { it.connection.canWrite }
                assertTrue(runtime.state.value.connection.synced)
                stage("synced")
            }
        }
        stage("temporary_connection_lease_released")
    }

    private fun stage(value: String) { Log.i("TickDeskUsbSetup", value); println("TickDeskUsbSetup: $value") }
}
