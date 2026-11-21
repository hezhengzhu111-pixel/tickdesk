package dev.tickdesk.app

import android.content.Context
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import dev.tickdesk.core.CanonicalJson
import dev.tickdesk.core.ProtocolException
import dev.tickdesk.core.ReminderReference

/** Call from a trusted SDK callback in the app's process. There is no exported push receiver here. */
internal object PushIngress {
    suspend fun accept(context: Context, payload: String): Boolean {
        val reference = try { ReminderReference.fromPush(payload) } catch (_: ProtocolException) { return false }
        if (!reference.inWindow(System.currentTimeMillis())) return false
        val app = context.applicationContext as TickDeskApplication
        val pair = app.runtime.pairedScope() ?: return false
        val request = OneTimeWorkRequestBuilder<PushReminderWorker>()
            .setInputData(workDataOf(
                "pair" to pair, "epoch" to reference.epoch, "task" to reference.taskId,
                "reminder" to reference.reminderId, "version" to reference.taskVersion,
                "scheduled" to reference.scheduledAt, "until" to reference.displayUntil,
            ))
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .build()
        val key = CanonicalJson.sha256("$pair:${reference.epoch}:${reference.reminderId}".toByteArray())
        WorkManager.getInstance(app).enqueueUniqueWork("tickdesk-reminder-$key", ExistingWorkPolicy.KEEP, request)
        return true
    }

    suspend fun register(context: Context, regId: String): String? {
        val app = context.applicationContext as TickDeskApplication
        return app.runtime.registerPush(regId)
    }
}

class PushReminderWorker(context: Context, parameters: WorkerParameters) : CoroutineWorker(context, parameters) {
    override suspend fun doWork(): Result {
        val pair = inputData.getString("pair") ?: return Result.failure()
        val reference = ReminderReference(
            inputData.getString("epoch") ?: return Result.failure(),
            inputData.getString("task") ?: return Result.failure(),
            inputData.getString("reminder") ?: return Result.failure(),
            inputData.getLong("version", -1), inputData.getLong("scheduled", -1), inputData.getLong("until", -1),
        )
        if (!reference.inWindow(System.currentTimeMillis()) || reference.taskVersion < 1) return Result.success()
        val runtime = (applicationContext as TickDeskApplication).runtime
        val outcome = runtime.backgroundReminder(reference, pair)
        return if (outcome == ReminderOutcome.RETRY && reference.inWindow(System.currentTimeMillis())) Result.retry()
        else Result.success()
    }
}
