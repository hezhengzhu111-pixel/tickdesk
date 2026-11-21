package dev.tickdesk.app

import android.Manifest
import android.annotation.SuppressLint
import android.app.*
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import dev.tickdesk.core.BackgroundReminderPolicy
import dev.tickdesk.core.WorkbenchProjection
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.*

internal data class BackgroundReminderState(val enabledPair: String? = null, val running: Boolean = false, val error: String? = null)

/** Consent belongs to one pairing. Only a visible Activity may start or resume the service. */
internal class BackgroundReminderController(private val context: Context, private val runtime: WorkbenchRuntime) {
    private val preferences = context.getSharedPreferences("background-reminders", Context.MODE_PRIVATE)
    private val mutableState = MutableStateFlow(BackgroundReminderState(preferences.getString("pair", null)))
    val state = mutableState.asStateFlow()

    fun setEnabled(enabled: Boolean) {
        if (!enabled) {
            savePair(null)
            context.stopService(Intent(context, BackgroundReminderService::class.java))
            return
        }
        val current = runtime.state.value
        if (!BackgroundReminderPolicy.canStart(current.pairId, current, runtime.isVisible, notificationsAvailable(context))) {
            failed("请先配对并退出恢复模式，允许通知及后台连接通知通道，然后在 App 内开启。")
            return
        }
        if (savePair(current.pairId)) resumeVisible()
    }

    fun resumeVisible() {
        val current = runtime.state.value
        if (state.value.enabledPair != null && state.value.enabledPair != current.pairId) {
            setEnabled(false)
            return
        }
        if (state.value.running || state.value.enabledPair == null) return
        if (!BackgroundReminderPolicy.canStart(state.value.enabledPair, current, runtime.isVisible, notificationsAvailable(context))) return
        try {
            context.startForegroundService(Intent(context, BackgroundReminderService::class.java).putExtra("pair", current.pairId))
        } catch (_: RuntimeException) {
            failed("系统未允许启动后台提醒，请回到 App 后重试。")
        }
    }

    fun running(value: Boolean) { mutableState.value = mutableState.value.copy(running = value, error = if (value) null else state.value.error) }
    fun failed(message: String) { mutableState.value = mutableState.value.copy(running = false, error = message) }

    @SuppressLint("UseKtx") // commit's Boolean is required; KTX edit discards the durable-write result.
    private fun savePair(pair: String?): Boolean {
        if (!preferences.edit().putString("pair", pair).commit()) {
            failed("后台提醒偏好未能保存，请重试。")
            return false
        }
        mutableState.value = mutableState.value.copy(enabledPair = pair, error = null)
        return true
    }
}

/** User-visible, opt-in workstation monitoring. No boot receiver, sticky restart or background auto-start. */
class BackgroundReminderService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val runtime get() = (application as TickDeskApplication).runtime
    private var monitor: Job? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            runtime.backgroundReminders.setEnabled(false)
            stopSelf()
            return START_NOT_STICKY
        }
        // startForegroundService already created a platform obligation. Pairing/connection/consent can
        // change between that call and this callback; satisfy promotion before rejecting a stale start.
        try {
            val notification = statusNotification(runtime.state.value)
            if (Build.VERSION.SDK_INT >= 34) startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
            else startForeground(NOTIFICATION_ID, notification)
        } catch (_: RuntimeException) {
            runtime.backgroundReminders.failed("系统拒绝后台提醒服务，请检查通知权限后在 App 内重试。")
            stopSelf()
            return START_NOT_STICKY
        }
        val pair = intent?.getStringExtra("pair")
        if (pair == null || pair != runtime.backgroundReminders.state.value.enabledPair ||
            !BackgroundReminderPolicy.canKeepRunning(pair, runtime.state.value, notificationsAvailable(this))) {
            stopSelf()
            return START_NOT_STICKY
        }
        runtime.backgroundReminders.running(true)
        if (monitor == null) monitor = scope.launch {
            runtime.withReminderConnection {
                val permissions = flow {
                    while (currentCoroutineContext().isActive) { emit(notificationsAvailable(this@BackgroundReminderService)); delay(15_000) }
                }
                combine(runtime.state, runtime.backgroundReminders.state, permissions) { projection, preference, allowed ->
                    Triple(projection, preference.enabledPair, allowed)
                }.distinctUntilChanged().collect { (projection, enabledPair, allowed) ->
                    if (!BackgroundReminderPolicy.canKeepRunning(enabledPair, projection, allowed)) {
                        if (projection.pairId != enabledPair) runtime.backgroundReminders.setEnabled(false)
                        else if (!allowed) runtime.backgroundReminders.failed("通知权限或后台连接通道已关闭，后台提醒已停止。")
                        else if (projection.connectionError != null) runtime.backgroundReminders.failed("连接无法继续，请在 App 中检查配对及连接状态后重试。")
                        stopSelf()
                    } else {
                        getSystemService(NotificationManager::class.java).notify(NOTIFICATION_ID, statusNotification(projection))
                    }
                }
            }
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        scope.cancel()
        runtime.backgroundReminders.running(false)
        stopForeground(STOP_FOREGROUND_REMOVE)
        super.onDestroy()
    }

    private fun statusNotification(state: WorkbenchProjection): Notification {
        ensureStatusChannel(this)
        val open = PendingIntent.getActivity(this, 70, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val stop = PendingIntent.getService(this, 71, Intent(this, BackgroundReminderService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val message = when {
            state.connection.recoveryRequired || state.serviceRecoveryActive -> "恢复核对中 · 提醒暂停"
            state.connectionError != null -> "连接中断 · 打开 App 查看状态"
            !state.connection.relayOnline -> "中继离线 · 正在重连"
            !state.connection.localOnline -> "本地工作台离线 · 等待连接"
            !state.connection.synced -> "正在同步 · 暂不发出提醒"
            else -> "工作台已连接 · 等待有效事项提醒"
        }
        return Notification.Builder(this, CHANNEL_ID).setSmallIcon(R.drawable.ic_alert)
            .setContentTitle("TickDesk 后台提醒").setContentText(message).setContentIntent(open)
            .setOngoing(true).setOnlyAlertOnce(true).setCategory(Notification.CATEGORY_SERVICE)
            .setVisibility(Notification.VISIBILITY_PUBLIC)
            .addAction(Notification.Action.Builder(null, "关闭后台提醒", stop).build()).build()
    }

    companion object {
        internal const val CHANNEL_ID = "tickdesk_background_connection"
        internal const val NOTIFICATION_ID = 746201
        internal const val ACTION_STOP = "dev.tickdesk.app.STOP_BACKGROUND_REMINDERS"

        internal fun ensureStatusChannel(context: Context) {
            context.getSystemService(NotificationManager::class.java).createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "后台提醒连接状态", NotificationManager.IMPORTANCE_LOW)
                    .apply { description = "显示后台提醒连接状态，可随时关闭" })
        }
    }
}

internal fun notificationsAvailable(context: Context): Boolean {
    val manager = context.getSystemService(NotificationManager::class.java)
    return manager.areNotificationsEnabled() && (Build.VERSION.SDK_INT < 33 ||
        context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED) &&
        manager.getNotificationChannel(BackgroundReminderService.CHANNEL_ID)?.importance != NotificationManager.IMPORTANCE_NONE
}
