package dev.tickdesk.app

import android.Manifest
import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.Ringtone
import android.media.RingtoneManager
import android.os.Build
import android.os.SystemClock
import dev.tickdesk.core.*
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.serialization.json.*
import java.util.concurrent.ConcurrentHashMap

internal data class VerifiedReminder(val task: JsonObject, val expiresAt: Long, val id: String)

internal const val REMINDER_PAIR = "tickdesk_reminder_pair"
internal const val REMINDER_EPOCH = "tickdesk_reminder_epoch"
internal const val REMINDER_ID = "tickdesk_reminder_id"

internal enum class ReminderOutcome { DONE, RETRY }

internal class ReminderPresenter(
    private val context: Context,
    private val repository: WorkbenchRepository,
    private val scope: CoroutineScope,
    private val isForeground: () -> Boolean,
) {
    private val verifying = ConcurrentHashMap.newKeySet<String>()
    private val mutableAlert = MutableStateFlow<VerifiedReminder?>(null)
    val alert = mutableAlert.asStateFlow()
    private var sound: Ringtone? = null
    private val retries = ReminderRetryCoordinator(scope, System::currentTimeMillis,
        isCurrent = { pair, reference -> ReminderRetryPolicy.isCurrent(repository.state.value, pair, reference, System.currentTimeMillis()) },
        resolve = { pair, reference -> resolve(reference, pair, fromPush = false) == ReminderOutcome.DONE })

    fun receive(event: BusinessMessage) {
        val reference = ReminderReference.fromBusiness(event) ?: return
        val pair = repository.state.value.pairId ?: return
        receive(reference, pair)
    }

    fun receive(reference: ReminderReference, pair: String) { retries.submit(pair, reference) }

    suspend fun resolve(reference: ReminderReference, pair: String, fromPush: Boolean): ReminderOutcome {
        val ttl = reference.displayUntil - System.currentTimeMillis()
        if (!reference.inWindow(System.currentTimeMillis())) return ReminderOutcome.DONE
        val key = "$pair:${reference.epoch}:${reference.reminderId}"
        if (!verifying.add(key)) return if (fromPush) ReminderOutcome.DONE else ReminderOutcome.RETRY
        return try {
            val ready = withTimeoutOrNull(minOf(12_000L, ttl).coerceAtLeast(1)) {
                repository.state.first {
                    it.pairId != pair || it.connection.recoveryRequired || it.connection.canWrite
                }
            } ?: return ReminderOutcome.RETRY
            if (ready.pairId != pair || !ready.connection.canWrite || ready.epoch != reference.epoch ||
                !reference.inWindow(System.currentTimeMillis())) return ReminderOutcome.DONE
            if (fromPush && ready.meta?.obj("settings")?.flag("pushEnabled") != true) return ReminderOutcome.DONE

            val result = queryTask(reference.taskId, pair) ?: return ReminderOutcome.RETRY
            val now = System.currentTimeMillis()
            val task = reference.verify(result, now) ?: return ReminderOutcome.DONE
            val latest = repository.state.value
            val projected = latest.records["tasks"]?.firstOrNull { it.text("taskId") == reference.taskId }
            if (latest.pairId != pair || latest.epoch != reference.epoch || !latest.connection.canWrite ||
                (projected?.number("version") ?: 0) > (task.number("version") ?: 0)) return ReminderOutcome.DONE

            val settings = latest.meta?.obj("settings")
            if (fromPush && settings?.flag("pushEnabled") != true) return ReminderOutcome.DONE
            val quiet = ReminderQuietHours.silence(settings, task.text("level"), now)
            val fullscreen = isForeground() && task.text("level") == "critical" && !quiet
            if (!fullscreen && !canNotify()) return ReminderOutcome.DONE
            val stopAt = minOf(reference.displayUntil, task.number("validUntil")!!)
            val reminder = VerifiedReminder(task, stopAt, reference.reminderId)
            if (!repository.claimReminder(reference.epoch, reference.reminderId, reference.taskId, stopAt, pair)) {
                return ReminderOutcome.DONE
            }
            withContext(Dispatchers.Main) {
                val current = repository.state.value
                if (current.pairId == pair && current.epoch == reference.epoch && current.connection.canWrite &&
                    System.currentTimeMillis() < stopAt) {
                    if (fullscreen && isForeground()) present(reminder)
                    else if (canNotify()) post(reminder, quiet, pair, reference.epoch)
                }
            }
            ReminderOutcome.DONE
        } catch (cancel: CancellationException) {
            throw cancel
        } catch (_: Exception) {
            ReminderOutcome.RETRY
        } finally {
            verifying.remove(key)
        }
    }

    private fun present(reminder: VerifiedReminder) {
        dismiss()
        mutableAlert.value = reminder
        sound = runCatching {
            RingtoneManager.getRingtone(context, RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION))
                ?.also { it.play() }
        }.getOrNull()
        scope.launch {
            delay((reminder.expiresAt - System.currentTimeMillis()).coerceAtLeast(0))
            withContext(Dispatchers.Main) { if (mutableAlert.value?.id == reminder.id) dismiss() }
        }
    }

    fun dismiss() {
        sound?.stop()
        sound = null
        mutableAlert.value = null
    }

    /** A tap is navigation only; it must never ring or post another notification. */
    suspend fun open(pair: String, epoch: String, reminderId: String): ReminderDestination? {
        val link = repository.reminderDestination(pair, epoch, reminderId) ?: return null
        val ready = withTimeoutOrNull(minOf(12_000L, link.displayUntil - System.currentTimeMillis()).coerceAtLeast(1)) {
            repository.state.first { it.pairId != pair || it.connection.recoveryRequired || it.connection.canWrite }
        } ?: return null
        if (ready.pairId != pair || ready.epoch != epoch || !ready.connection.canWrite) return null
        val result = queryTask(link.taskId, pair) ?: return null
        val latest = repository.state.value
        val projected = latest.records["tasks"]?.firstOrNull { it.text("taskId") == link.taskId }
        if (latest.pairId != pair || latest.epoch != epoch || !latest.connection.canWrite ||
            (projected?.number("version") ?: 0) > (result.obj("task")?.number("version") ?: 0) ||
            !link.verifyClick(result, reminderId, System.currentTimeMillis())) return null
        return repository.reminderDestination(pair, epoch, reminderId)?.takeIf { it == link }
    }

    private suspend fun queryTask(taskId: String, pair: String): JsonObject? {
        val id = repository.enqueue("get_task", buildJsonObject { put("taskId", taskId) }, expectedPair = pair)
        val deadline = SystemClock.elapsedRealtime() + 5_000
        val intent = withTimeoutOrNull(5000) {
            repository.state.first { projection ->
                projection.pairId != pair || projection.connection.recoveryRequired ||
                    projection.intents.any { it.id == id && it.stage !in queryPending }
            }.takeIf { it.pairId == pair && !it.connection.recoveryRequired }?.intents?.firstOrNull { it.id == id }
        }
        if (intent?.stage != DeliveryStage.APPLIED || SystemClock.elapsedRealtime() > deadline) {
            // If it never left the device it no longer needs to be sent once the display window closes.
            repository.cancelUnsent(id)
            return null
        }
        return intent.result
    }

    private fun canNotify(): Boolean {
        val nm = context.getSystemService(NotificationManager::class.java)
        return nm.areNotificationsEnabled() && (Build.VERSION.SDK_INT < 33 ||
            context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED)
    }

    @SuppressLint("MissingPermission")
    private fun post(reminder: VerifiedReminder, quiet: Boolean, pair: String, epoch: String) {
        val nm = context.getSystemService(NotificationManager::class.java)
        val level = reminder.task.text("level")
        val important = level in setOf("critical", "warn")
        val channel = when { quiet -> "tickdesk_quiet"; important -> "tickdesk_urgent"; else -> "tickdesk_info" }
        val channelName = when { quiet -> "工作台夜间提醒"; important -> "工作台重要事项"; else -> "工作台信息提醒" }
        nm.createNotificationChannel(NotificationChannel(channel, channelName,
            if (important && !quiet) NotificationManager.IMPORTANCE_HIGH else NotificationManager.IMPORTANCE_LOW))
        if (nm.getNotificationChannel(channel)?.importance == NotificationManager.IMPORTANCE_NONE) return
        val notificationId = "$pair:$epoch:${reminder.id}".hashCode()
        val intent = Intent(context, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            .putExtra(REMINDER_PAIR, pair)
            .putExtra(REMINDER_EPOCH, epoch)
            .putExtra(REMINDER_ID, reminder.id)
        val pending = PendingIntent.getActivity(context, notificationId, intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val ttl = reminder.expiresAt - System.currentTimeMillis()
        if (ttl <= 0) return
        val headline = when (level) {
            "critical" -> "紧急事项待核对"
            "warn" -> "重要事项待核对"
            else -> "工作台有待处理事项"
        }
        val until = dev.tickdesk.app.ui.timeText(reminder.expiresAt)
        val publicVersion = Notification.Builder(context, channel)
            .setSmallIcon(R.drawable.ic_alert)
            .setContentTitle(headline)
            .setContentText("解锁后查看 · 有效至 $until")
            .setContentIntent(pending)
            .build()
        val direction = when (reminder.task.text("direction")) {
            "buy" -> "量化买点"
            "sell" -> "持仓卖点"
            else -> "风险核对"
        }
        val security = listOfNotNull(reminder.task.text("name"), reminder.task.text("symbol")).joinToString(" · ")
        val title = if (security.isBlank()) headline else security
        val explanation = "$direction · 有效至 $until"
        val accent = when (level) {
            "critical" -> 0xFFFF4D5E.toInt()
            "warn" -> 0xFFF5A623.toInt()
            else -> 0xFF60A5FA.toInt()
        }
        val notification = Notification.Builder(context, channel)
            .setSmallIcon(R.drawable.ic_alert)
            .setColor(accent)
            .setContentTitle(title)
            .setContentText(explanation)
            .setSubText(level?.uppercase())
            .setStyle(Notification.BigTextStyle().bigText(explanation))
            .setCategory(Notification.CATEGORY_REMINDER)
            .setPublicVersion(publicVersion)
            .setVisibility(Notification.VISIBILITY_PRIVATE)
            .setOnlyAlertOnce(true)
            .setContentIntent(pending)
            .setTimeoutAfter(ttl)
            .setAutoCancel(true)
            .build()
        nm.notify(notificationId, notification)
    }

    private companion object {
        val queryPending = setOf(DeliveryStage.PENDING, DeliveryStage.SENT, DeliveryStage.DELIVERED)
    }
}

