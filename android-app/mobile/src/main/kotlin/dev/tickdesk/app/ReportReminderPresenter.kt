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
import android.os.Build
import dev.tickdesk.core.*
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.first
import kotlinx.serialization.json.*

internal data class ReportDestination(val pair: String, val epoch: String, val reportId: String, val displayUntil: Long)

internal const val REPORT_PAIR = "tickdesk_report_pair"
internal const val REPORT_EPOCH = "tickdesk_report_epoch"
internal const val REPORT_ID = "tickdesk_report_id"

internal class ReportReminderPresenter(private val context: Context, private val repository: WorkbenchRepository) {
    private val manager = context.getSystemService(NotificationManager::class.java)

    suspend fun present(pair: String, epoch: String, notice: ReportNotice) {
        if (!canPost()) return
        try {
            val result = readReport(pair, epoch, notice) ?: return
            val claimed = repository.reportNotification(pair, epoch, notice.reportId, claim = true) ?: return
            if (result.number("validUntil")?.let { it <= System.currentTimeMillis() } != false) return
            withContext(Dispatchers.Main) {
                if (repository.state.value.canShow(pair, epoch, claimed)) post(pair, epoch, claimed)
            }
        } catch (cancel: CancellationException) {
            throw cancel
        } catch (_: Exception) {
            // Room transaction and immutable read guards prevent partially verified alerts from showing.
        }
    }

    suspend fun open(pair: String, epoch: String, id: String): ReportDestination? {
        val link = repository.reportNotification(pair, epoch, id, claim = false) ?: return null
        val ready = withTimeoutOrNull(minOf(12_000L, link.displayUntil - System.currentTimeMillis()).coerceAtLeast(1L)) {
            repository.state.first { it.pairId != pair || it.connection.recoveryRequired || it.connection.canWrite }
        } ?: return null
        if (!ready.canShow(pair, epoch, link) || readReport(pair, epoch, link) == null) return null
        val freshLink = repository.reportNotification(pair, epoch, id, claim = false) ?: return null
        return ReportDestination(pair, epoch, id, freshLink.displayUntil)
    }

    private suspend fun readReport(pair: String, epoch: String, notice: ReportNotice): JsonObject? {
        if (!repository.state.value.canShow(pair, epoch, notice)) return null
        val id = repository.enqueue("get_report", buildJsonObject { put("reportId", notice.reportId) }, pair, epoch)
        val timeout = minOf(6_000L, notice.displayUntil - System.currentTimeMillis()).coerceAtLeast(1L)
        val state = withTimeoutOrNull(timeout) {
            repository.state.first { projection ->
                !projection.canShow(pair, epoch, notice) ||
                    projection.intents.any { it.id == id && it.stage !in pending }
            }
        }
        if (state == null || !state.canShow(pair, epoch, notice)) {
            repository.cancelUnsent(id)
            return null
        }
        return state.intents.firstOrNull { it.id == id && it.stage == DeliveryStage.APPLIED }
            ?.result?.obj("header")?.takeIf {
                it.text("reportId") == notice.reportId && it.text("state") == "current" &&
                    it.number("validUntil")?.let { until -> until > System.currentTimeMillis() } == true
            }
    }

    private fun WorkbenchProjection.canShow(pair: String, epoch: String, notice: ReportNotice) =
        pairId == pair && this.epoch == epoch && connection.canWrite && System.currentTimeMillis() < notice.displayUntil

    private fun canPost() = manager.areNotificationsEnabled() && (Build.VERSION.SDK_INT < 33 ||
        context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED) &&
        manager.getNotificationChannel(CHANNEL)?.importance != NotificationManager.IMPORTANCE_NONE

    @SuppressLint("MissingPermission")
    private fun post(pair: String, epoch: String, notice: ReportNotice) {
        if (!canPost()) return
        val ttl = notice.displayUntil - System.currentTimeMillis()
        if (ttl <= 0) return
        manager.createNotificationChannel(NotificationChannel(CHANNEL, "工作台分析报告", NotificationManager.IMPORTANCE_LOW))
        val key = "$pair:$epoch:${notice.reportId}"
        val intent = Intent(context, MainActivity::class.java).setAction("dev.tickdesk.app.OPEN_REPORT")
            .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            .putExtra(REPORT_PAIR, pair).putExtra(REPORT_EPOCH, epoch).putExtra(REPORT_ID, notice.reportId)
        val pending = PendingIntent.getActivity(context, key.hashCode(), intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val title = "工作台有新分析报告"
        val publicVersion = Notification.Builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_alert)
            .setContentTitle(title)
            .setContentText("解锁后查看分析报告。")
            .setContentIntent(pending)
            .build()
        val notification = Notification.Builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_alert)
            .setContentTitle(title)
            .setContentText("打开核对结论、证据与有效期。")
            .setPublicVersion(publicVersion)
            .setVisibility(Notification.VISIBILITY_PRIVATE)
            .setOnlyAlertOnce(true)
            .setContentIntent(pending)
            .setTimeoutAfter(ttl)
            .setAutoCancel(true)
            .build()
        manager.notify(key, REPORT_NOTIFICATION, notification)
    }

    private companion object {
        const val CHANNEL = "tickdesk_analysis_report"
        const val REPORT_NOTIFICATION = 72
        val pending = setOf(DeliveryStage.PENDING, DeliveryStage.SENT, DeliveryStage.DELIVERED)
    }
}
