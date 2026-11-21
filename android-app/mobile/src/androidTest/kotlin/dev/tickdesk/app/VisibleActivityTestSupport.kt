package dev.tickdesk.app

import android.os.Handler
import android.os.Looper
import android.os.ParcelFileDescriptor
import android.os.SystemClock
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.runner.lifecycle.ActivityLifecycleMonitorRegistry
import androidx.test.runner.lifecycle.Stage
import dev.tickdesk.app.data.readBounded
import java.util.concurrent.Executors
import java.util.concurrent.FutureTask
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/** Wait for the state this test needs, not global UI idleness (which animated Compose UI may never reach). */
internal fun launchResumedMainActivity(): MainActivity {
    val instrument = InstrumentationRegistry.getInstrumentation()
    val context = instrument.targetContext
    // MIUI can reject applicationContext.startActivity from a background instrumentation process.
    // Shell represents the test operator opening the App; production startup policy is unchanged.
    val pipe = AtomicReference<ParcelFileDescriptor?>()
    val launcher = Executors.newSingleThreadExecutor { action -> Thread(action, "test-visible-launch").apply { isDaemon = true } }
    val result = launcher.submit<String> {
        val descriptor = instrument.uiAutomation.executeShellCommand("am start -W -n ${context.packageName}/dev.tickdesk.app.MainActivity")
        pipe.set(descriptor)
        ParcelFileDescriptor.AutoCloseInputStream(descriptor).use { it.readBounded(4096).toString(Charsets.UTF_8) }
    }
    val output = try { result.get(8, TimeUnit.SECONDS) }
    finally { pipe.get()?.close(); result.cancel(true); launcher.shutdownNow() }
    check(!output.contains("Error:") && !output.contains("Status: timeout")) { "Shell could not launch MainActivity: $output" }
    val deadline = SystemClock.elapsedRealtime() + 10_000
    while (SystemClock.elapsedRealtime() < deadline) {
        val activity = onTestMainThread {
            ActivityLifecycleMonitorRegistry.getInstance().getActivitiesInStage(Stage.RESUMED)
                .filterIsInstance<MainActivity>().firstOrNull { !it.isFinishing && !it.isDestroyed }
        }
        if (activity != null && (context.applicationContext as TickDeskApplication).runtime.isVisible) return activity
        SystemClock.sleep(50)
    }
    throw AssertionError("MainActivity did not become RESUMED and runtime-visible within 10 seconds")
}

internal fun <T> onTestMainThread(block: () -> T): T {
    if (Looper.myLooper() == Looper.getMainLooper()) return block()
    val task = FutureTask(block)
    check(Handler(Looper.getMainLooper()).post(task)) { "Main looper rejected test action" }
    return try { task.get(5, TimeUnit.SECONDS) }
    finally { if (!task.isDone) task.cancel(false) }
}
