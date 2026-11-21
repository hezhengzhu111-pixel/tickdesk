package dev.tickdesk.app.ui

import android.Manifest
import android.app.NotificationManager
import android.content.Intent
import android.os.Build
import android.provider.Settings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LifecycleEventEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import dev.tickdesk.app.BackgroundReminderService
import dev.tickdesk.app.notificationsAvailable
import dev.tickdesk.core.WorkbenchProjection

@Composable
internal fun BackgroundReminderCard(state: WorkbenchProjection, model: WorkbenchViewModel) {
    val context = LocalContext.current
    val preference by model.backgroundReminders.collectAsStateWithLifecycle()
    var notifications by remember { mutableStateOf(notificationsAvailable(context)) }
    val permission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) {
        notifications = notificationsAvailable(context)
    }
    LifecycleEventEffect(Lifecycle.Event.ON_RESUME) { notifications = notificationsAvailable(context) }
    DesignCard {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Column(Modifier.weight(1f)) {
                SectionTitle("后台提醒")
                BackgroundCaption(when {
                    preference.enabledPair == null -> "已关闭 · 离开 App 后不保持提醒连接"
                    !notifications -> "通知权限或连接状态通道已关闭"
                    state.connection.recoveryRequired || state.serviceRecoveryActive -> "恢复核对中 · 后台提醒暂停"
                    !preference.running -> "服务未运行 · 回到 App 后可恢复"
                    !state.connection.relayOnline -> "服务运行中 · 中继离线，正在重连"
                    !state.connection.localOnline -> "服务运行中 · 等待本地工作台"
                    !state.connection.synced -> "服务运行中 · 正在同步"
                    else -> "已连接 · 等待有效事项提醒"
                })
            }
            Switch(checked = preference.enabledPair != null, onCheckedChange = model::setBackgroundReminders,
                modifier = Modifier.sizeIn(minWidth = 48.dp, minHeight = 48.dp))
        }
        BackgroundCaption("开启后保留状态栏通知并保持工作台连接，会增加耗电。系统停止、强制结束或重启后，请再次打开 App。网络断开时无法实时提醒；恢复后仅核验仍有效的提醒。")
        preference.error?.let { NoticeStrip(it, DeskColors.amber) }
        if (!notifications && Build.VERSION.SDK_INT >= 33) {
            OutlinedButton(onClick = { permission.launch(Manifest.permission.POST_NOTIFICATIONS) },
                modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) { Text("允许通知") }
        }
        if (preference.enabledPair != null && !preference.running) {
            OutlinedButton(onClick = { model.setBackgroundReminders(true) },
                modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) { Text("恢复后台提醒") }
        }
        TextButton(onClick = {
            BackgroundReminderService.ensureStatusChannel(context)
            context.startActivity(Intent(Settings.ACTION_CHANNEL_NOTIFICATION_SETTINGS)
                .putExtra(Settings.EXTRA_APP_PACKAGE, context.packageName)
                .putExtra(Settings.EXTRA_CHANNEL_ID, BackgroundReminderService.CHANNEL_ID))
        }, modifier = Modifier.heightIn(min = 48.dp)) { Text("查看后台连接通知通道") }
        val blockedReminder = context.getSystemService(NotificationManager::class.java)
            .getNotificationChannel("tickdesk_urgent")?.importance == NotificationManager.IMPORTANCE_NONE
        if (blockedReminder) NoticeStrip("重要事项通知通道已关闭，请在提醒与系统权限中重新允许。", DeskColors.amber)
    }
}

@Composable
private fun BackgroundCaption(text: String) {
    Text(text, color = DeskColors.muted, style = MaterialTheme.typography.bodyMedium)
}
