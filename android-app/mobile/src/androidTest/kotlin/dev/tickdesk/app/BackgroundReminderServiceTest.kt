package dev.tickdesk.app

import android.Manifest
import android.app.ActivityManager
import android.app.NotificationManager
import android.content.ComponentName
import android.content.ContextWrapper
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.SystemClock
import androidx.test.platform.app.InstrumentationRegistry
import dev.tickdesk.core.DeliveryStage
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.JsonObject
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test

/** Opt-in real service check: uses existing pairing and never clears or replaces user data. */
class BackgroundReminderServiceTest {
    private val instrument = InstrumentationRegistry.getInstrumentation()
    private val context = instrument.targetContext

    @Test fun staleVisibleForegroundStartPromotesThenStopsWithoutCrashOrResidue() = runBlocking {
        assumeTrue(InstrumentationRegistry.getArguments().getString("backgroundReminderTest") == "true")
        val runtime = (context.applicationContext as TickDeskApplication).runtime
        val pair = requireNotNull(runtime.pairedScope()) { "Existing pairing required" }
        assertTrue("Grant notifications before this opt-in test", notificationsAvailable(context))
        val initiallyEnabled = runtime.backgroundReminders.state.value.enabledPair != null
        val activity = launchResumedMainActivity()
        try {
            onTestMainThread { runtime.backgroundReminders.setEnabled(true) }
            waitUntil { runtime.backgroundReminders.state.value.running }
            // Capture the same valid start request as the controller, then revoke consent before its delivery.
            // This models a queued start arriving after the state that authorized it has become stale.
            val staleStart = Intent(context, BackgroundReminderService::class.java).putExtra("pair", pair)
            onTestMainThread { runtime.backgroundReminders.setEnabled(false) }
            waitUntil { !runtime.backgroundReminders.state.value.running && !reminderServiceExists() }
            onTestMainThread {
                assertTrue(runtime.isVisible)
                context.startForegroundService(staleStart)
            }
            // Old code crashes on stopSelf before startForeground (immediately on MIUI, timeout elsewhere).
            delay(12_000)
            assertTrue(runtime.isVisible)
            assertNull(runtime.backgroundReminders.state.value.enabledPair)
            assertFalse(runtime.backgroundReminders.state.value.running)
            assertFalse(reminderServiceExists())
            assertTrue(context.getSystemService(NotificationManager::class.java).activeNotifications
                .none { it.id == BackgroundReminderService.NOTIFICATION_ID })
        } finally {
            onTestMainThread { runtime.backgroundReminders.setEnabled(initiallyEnabled); activity.finish() }
        }
    }

    @Test fun visibleOptInKeepsServiceAfterHomeAndStopLeavesVisibleConnectionAlone() = runBlocking {
        assumeTrue(InstrumentationRegistry.getArguments().getString("backgroundReminderTest") == "true")
        val runtime = (context.applicationContext as TickDeskApplication).runtime
        assertNotNull("Opt-in test requires existing pairing; run UsbPairingSetupTest first", runtime.pairedScope())
        val initiallyEnabled = runtime.backgroundReminders.state.value.enabledPair != null
        assertTrue("Grant notifications before this opt-in test", notificationsAvailable(context))
        var activity = launchResumedMainActivity()
        try {
            waitUntil { runtime.isVisible }
            onTestMainThread { runtime.backgroundReminders.setEnabled(true) }
            waitUntil { runtime.backgroundReminders.state.value.running }
            assertTrue(context.getSystemService(NotificationManager::class.java).activeNotifications
                .any { it.id == BackgroundReminderService.NOTIFICATION_ID })
            onTestMainThread { activity.moveTaskToBack(true) }
            waitUntil { !runtime.isVisible }
            assertTrue(runtime.backgroundReminders.state.value.running)
            val backgroundRead = runtime.enqueue("get_status", JsonObject(emptyMap()))
            withTimeout(15_000) { runtime.state.first { it.intents.any { intent -> intent.id == backgroundRead && intent.stage == DeliveryStage.APPLIED } } }
            assertFalse(runtime.isVisible)
            // A read ACK can arrive one sync behind the writable projection;
            // wait for the durable background connection instead of racing it.
            withTimeout(15_000) { runtime.state.first { it.connection.canWrite } }
            activity = launchResumedMainActivity()
            waitUntil { runtime.isVisible }
            val pair = runtime.state.value.pairId
            context.startService(Intent(context, BackgroundReminderService::class.java).setAction(BackgroundReminderService.ACTION_STOP))
            waitUntil { !runtime.backgroundReminders.state.value.running }
            assertNull(runtime.backgroundReminders.state.value.enabledPair)
            assertTrue(runtime.isVisible)
            assertEquals(pair, runtime.state.value.pairId)
            val foregroundRead = runtime.enqueue("get_status", JsonObject(emptyMap()))
            withTimeout(15_000) { runtime.state.first { it.intents.any { intent -> intent.id == foregroundRead && intent.stage == DeliveryStage.APPLIED } } }
            withTimeout(15_000) { runtime.state.first { it.connection.canWrite } }
            waitUntil { context.getSystemService(NotificationManager::class.java).activeNotifications
                .none { it.id == BackgroundReminderService.NOTIFICATION_ID } }
        } finally {
            onTestMainThread { runtime.backgroundReminders.setEnabled(initiallyEnabled); activity.finish() }
        }
    }

    @Test fun deniedNotificationsNeverStartAServiceOrPersistConsent() = runBlocking {
        assumeTrue(Build.VERSION.SDK_INT >= 33)
        val runtime = (context.applicationContext as TickDeskApplication).runtime
        runtime.pairedScope()
        val before = runtime.backgroundReminders.state.value.enabledPair
        var starts = 0
        val denied = object : ContextWrapper(context) {
            override fun checkSelfPermission(permission: String): Int =
                if (permission == Manifest.permission.POST_NOTIFICATIONS) PackageManager.PERMISSION_DENIED else super.checkSelfPermission(permission)
            override fun startForegroundService(service: Intent): ComponentName? { starts++; return null }
        }
        assertFalse(notificationsAvailable(denied))
        val controller = BackgroundReminderController(denied, runtime)
        controller.setEnabled(true)
        assertEquals(0, starts)
        assertEquals(before, controller.state.value.enabledPair)
        assertNotNull(controller.state.value.error)
    }

    private fun waitUntil(predicate: () -> Boolean) {
        val deadline = SystemClock.elapsedRealtime() + 10_000
        while (!predicate() && SystemClock.elapsedRealtime() < deadline) SystemClock.sleep(50)
        assertTrue("Timed out waiting for service lifecycle", predicate())
    }

    @Suppress("DEPRECATION") // Modern Android still returns this app's own services to its instrumentation.
    private fun reminderServiceExists(): Boolean = context.getSystemService(ActivityManager::class.java)
        .getRunningServices(Int.MAX_VALUE).any { it.service.className == BackgroundReminderService::class.java.name }
}
