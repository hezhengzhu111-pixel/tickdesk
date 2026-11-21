package dev.tickdesk.app

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.getValue
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.lifecycleScope
import dev.tickdesk.app.ui.TickDeskApp
import dev.tickdesk.app.ui.TickDeskTheme
import dev.tickdesk.core.ReminderDestination
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {
    private val runtime get() = (application as TickDeskApplication).runtime
    private val openedTask = MutableStateFlow<ReminderDestination?>(null)
    private val openedReport = MutableStateFlow<ReportDestination?>(null)
    private var taskLink: Job? = null
    private var reportLink: Job? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge(statusBarStyle = SystemBarStyle.dark(android.graphics.Color.TRANSPARENT),
            navigationBarStyle = SystemBarStyle.dark(android.graphics.Color.TRANSPARENT))
        readTaskLink(intent)
        readReportLink(intent)
        setContent {
            val destination by openedTask.collectAsStateWithLifecycle()
            val report by openedReport.collectAsStateWithLifecycle()
            TickDeskTheme {
                TickDeskApp(openTask = destination, onTaskOpened = { openedTask.value = null },
                    openReport = report, onReportOpened = { openedReport.value = null })
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        readTaskLink(intent)
        readReportLink(intent)
    }

    private fun readReportLink(intent: Intent) {
        val pair = intent.getStringExtra(REPORT_PAIR) ?: return
        val epoch = intent.getStringExtra(REPORT_EPOCH) ?: return
        val id = intent.getStringExtra(REPORT_ID) ?: return
        intent.removeExtra(REPORT_PAIR)
        intent.removeExtra(REPORT_EPOCH)
        intent.removeExtra(REPORT_ID)
        reportLink?.cancel()
        reportLink = lifecycleScope.launch {
            openedReport.value = try {
                if (runtime.pairedScope() == pair) runtime.reportReminders.open(pair, epoch, id) else null
            } catch (cancel: kotlinx.coroutines.CancellationException) { throw cancel }
            catch (_: Exception) { null }
        }
    }

    private fun readTaskLink(intent: Intent) {
        val pair = intent.getStringExtra(REMINDER_PAIR) ?: return
        val epoch = intent.getStringExtra(REMINDER_EPOCH) ?: return
        val reminder = intent.getStringExtra(REMINDER_ID) ?: return
        intent.removeExtra(REMINDER_PAIR)
        intent.removeExtra(REMINDER_EPOCH)
        intent.removeExtra(REMINDER_ID)
        taskLink?.cancel()
        taskLink = lifecycleScope.launch {
            openedTask.value = try {
                if (runtime.pairedScope() == pair) runtime.reminders.open(pair, epoch, reminder) else null
            } catch (cancel: kotlinx.coroutines.CancellationException) { throw cancel }
            catch (_: Exception) { null }
        }
    }

    override fun onStart() {
        super.onStart()
        runtime.foreground(true)
    }

    override fun onStop() {
        runtime.foreground(false)
        super.onStop()
    }

    override fun onResume() {
        super.onResume()
        runtime.resumeBackgroundReminders()
    }
}
