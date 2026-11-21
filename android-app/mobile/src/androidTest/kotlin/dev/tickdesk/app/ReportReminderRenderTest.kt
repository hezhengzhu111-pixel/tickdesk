package dev.tickdesk.app

import android.accessibilityservice.AccessibilityService
import android.app.KeyguardManager
import android.app.NotificationManager
import android.graphics.Bitmap
import android.os.ParcelFileDescriptor
import android.view.accessibility.AccessibilityNodeInfo
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.runner.lifecycle.ActivityLifecycleMonitorRegistry
import androidx.test.runner.lifecycle.Stage
import dev.tickdesk.core.*
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.first
import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File
import java.util.UUID

/** Android system notification backed by an isolated Room store and canonical WSS replay. */
class ReportReminderRenderTest {
    @Test fun verifiedRiskReportPostsOnceAndTapRevalidatesWhileExpiredReportStops() = runBlocking {
        assumeTrue("Use the demo package to isolate permissions and notifications", BuildConfig.MOCK_DEMO)
        val instrument = InstrumentationRegistry.getInstrumentation()
        val context = instrument.targetContext
        val manager = context.getSystemService(NotificationManager::class.java)
        val keyguard = context.getSystemService(KeyguardManager::class.java)
        val permissionDenied = InstrumentationRegistry.getArguments().getString("reportPermission") == "denied"
        val lockscreen = InstrumentationRegistry.getArguments().getString("reportSurface") == "lockscreen"
        assertEquals("Set the selected notification permission for the demo package", !permissionDenied, manager.areNotificationsEnabled())
        val runtime = (context.applicationContext as TickDeskApplication).runtime
        assumeTrue("Use an unpaired demo package", runtime.pairedScope() == null)
        val repo = runtime.repository
        val cases = instrument.context.assets.open("v2/fixtures.json").bufferedReader().use {
            Json.parseToJsonElement(it.readText()).jsonObject.array("cases")!!.associate { row ->
                row.jsonObject.text("name")!! to row.jsonObject.obj("payload")!!
            }
        }
        val openedAt = System.currentTimeMillis()
        val until = openedAt + 60_000
        val pair = "isolated-report-${UUID.randomUUID()}"
        val epoch = "epoch_demo_a"
        val reportId = "android_report_fixture"
        val shade = instrument.uiAutomation
        fun deviceCommand(command: String) = ParcelFileDescriptor.AutoCloseInputStream(shade.executeShellCommand(command))
            .use { it.readBytes().decodeToString().trim() }
        suspend fun visible(label: String): AccessibilityNodeInfo = withTimeout(9_000) {
            var node = shade.rootInActiveWindow?.let { findText(it, label) }
            while (node == null) {
                delay(100)
                node = shade.rootInActiveWindow?.let { findText(it, label) }
            }
            node
        }
        manager.cancelAll()
        try {
            repo.selectPair(pair)
            repo.peerOnline("local", epoch)
            val incarnation = Protocol.frame(repo.hello()).json.text("incarnation")!!
            fun deliver(body: JsonObject) = Protocol.deliver(Protocol.business(body), body.text("id")!!, "local", incarnation)
            fun ack(template: String, request: BusinessMessage, result: JsonObject): RelayFrame {
                val id = request.clientMsgId!!
                return deliver(JsonObject(cases.getValue(template) + mapOf(
                    "id" to JsonPrimitive("ack_$id"), "dedupeKey" to JsonPrimitive("ack:$id"),
                    "data" to buildJsonObject {
                        put("cmd", request.command!!); put("ok", true); put("error", JsonNull)
                        put("refId", request.id); put("clientMsgId", id)
                        put("requestHash", CanonicalJson.hash(request.data)); put("result", result)
                    }
                )))
            }
            for (name in listOf("snapshot_page_0", "snapshot_page_1")) {
                val sync = repo.nextFrames().first { it.message?.command == "sync" }.message!!
                val templatePage = cases.getValue(name)
                val data = JsonObject(templatePage.obj("data")!! + mapOf(
                    "generatedAt" to JsonPrimitive(openedAt), "expiresAt" to JsonPrimitive(openedAt + 600_000),
                ))
                val page = JsonObject(templatePage + mapOf("ts" to JsonPrimitive(openedAt), "data" to data))
                val result = buildJsonObject {
                    put("requestId", sync.clientMsgId!!); put("pageEventId", page.text("id")!!)
                    for (key in listOf("serverEpoch", "snapshotId", "snapshotSeq", "pageIndex", "nextPageCursor", "hasMore")) {
                        put(key, data.getValue(key))
                    }
                }
                repo.receive(deliver(page))
                repo.receive(ack("ack_sync", sync, result))
            }
            assertTrue(repo.state.value.connection.canWrite)
            repo.requestAuxiliary("list_reports", pair)
            val subscribe = repo.nextFrames().first { it.message?.command == "list_reports" }.message!!
            assertEquals(true, subscribe.data.flag("subscribe"))
            repo.receive(ack("ack_list_reports", subscribe, buildJsonObject {
                putJsonArray("items") {}; put("nextCursor", JsonNull)
            }))

            val template = cases.getValue("analysis_report_publication")
            val header = JsonObject(template.obj("data")!!.obj("header")!! + mapOf(
                "reportId" to JsonPrimitive(reportId), "generatedAt" to JsonPrimitive(openedAt),
                "validUntil" to JsonPrimitive(until), "stance" to JsonPrimitive("restricted"),
            ))
            val publication = JsonObject(template + mapOf(
                "ts" to JsonPrimitive(openedAt), "data" to buildJsonObject {
                    put("header", header); put("notifyUntil", until)
                }
            ))
            if (lockscreen) {
                assertEquals("1", deviceCommand("getprop ro.kernel.qemu"))
                assertEquals("1", deviceCommand("settings get secure lock_screen_show_notifications"))
                assertEquals("Enable the emulator swipe lockscreen for this test", "false", deviceCommand("cmd lock_settings get-disabled"))
                assertFalse(keyguard.isDeviceSecure)
                deviceCommand("input keyevent KEYCODE_SLEEP")
                delay(550)
                deviceCommand("input keyevent KEYCODE_WAKEUP")
                withTimeout(5_000) { while (!keyguard.isKeyguardLocked) delay(90) }
            }
            repo.receive(deliver(publication))
            if (permissionDenied) {
                val queried = withTimeoutOrNull(1_000) {
                    repo.state.first { state -> state.intents.any { it.command == "get_report" } }
                }
                assertNull("Denied notifications should not send a report revalidation request", queried)
                assertTrue(manager.activeNotifications.isEmpty())
                assertNull(runtime.reportReminders.open(pair, epoch, reportId))
                return@runBlocking
            }
            val evidence = buildJsonObject {
                put("header", header); put("text", "隔离持仓风险报告")
                putJsonArray("sources") { add(buildJsonObject {
                    put("name", "隔离持仓证据"); put("ref", "fixture:position"); put("asOf", openedAt)
                }) }
            }
            suspend fun answerReport() {
                withTimeout(6_000) {
                    repo.state.first { it.intents.any { intent ->
                        intent.command == "get_report" && intent.stage == DeliveryStage.PENDING
                    } }
                }
                val request = repo.nextFrames().first { it.message?.command == "get_report" }.message!!
                assertEquals(reportId, request.data.text("reportId"))
                repo.receive(ack("ack_get_report", request, evidence))
            }
            answerReport()
            val posted = withTimeout(6_000) {
                var active = manager.activeNotifications.firstOrNull { it.tag?.endsWith(reportId) == true }
                while (active == null) {
                    delay(100)
                    active = manager.activeNotifications.firstOrNull { it.tag?.endsWith(reportId) == true }
                }
                active
            }
            assertEquals("工作台有新分析报告", posted.notification.extras.getCharSequence("android.title"))
            assertNotNull(posted.notification.contentIntent)
            repo.receive(deliver(publication))
            assertTrue(repo.state.value.reportNotices.isEmpty())
            assertEquals(1, manager.activeNotifications.count { it.tag?.endsWith(reportId) == true })

            if (lockscreen) {
                assertTrue("The report arrived while Android was locked", keyguard.isKeyguardLocked)
                fun snapshot(name: String) {
                    val bitmap = checkNotNull(shade.takeScreenshot())
                    try {
                        File(context.filesDir, name).outputStream().use {
                            assertTrue(bitmap.compress(Bitmap.CompressFormat.PNG, 100, it))
                        }
                    } finally {
                        bitmap.recycle()
                    }
                }
                snapshot("report-lockscreen-collapsed.png")
                val lockedNotice = withTimeoutOrNull(2_500) { visible("工作台有新分析报告") } ?: run {
                    assertTrue("The system notification shade must be reachable on the keyguard",
                        shade.performGlobalAction(AccessibilityService.GLOBAL_ACTION_NOTIFICATIONS))
                    visible("工作台有新分析报告")
                }
                assertTrue("Showing the notification must leave the device locked", keyguard.isKeyguardLocked)
                assertEquals("com.android.systemui", lockedNotice.packageName?.toString())
                val root = checkNotNull(shade.rootInActiveWindow)
                assertNull("Report content must remain inside the app", findText(root, "隔离持仓风险报告"))
                snapshot("report-lockscreen-evidence.png")
                deviceCommand("wm dismiss-keyguard")
                withTimeout(5_000) { while (keyguard.isKeyguardLocked) delay(90) }
            }
            assertTrue("Notification shade should open", shade.performGlobalAction(AccessibilityService.GLOBAL_ACTION_NOTIFICATIONS))
            val shown = visible("工作台有新分析报告")
            assertTrue("Tapping the system alert must launch its real PendingIntent",
                generateSequence(shown) { it.parent }.any { it.performAction(AccessibilityNodeInfo.ACTION_CLICK) })
            answerReport()
            visible("报告详情")
            visible("隔离持仓风险报告")
            assertNull(runtime.reportReminders.open("other-pair", epoch, reportId))

            val expired = JsonObject(publication + mapOf(
                "id" to JsonPrimitive("expired_report_event"), "dedupeKey" to JsonPrimitive("report:expired"),
                "data" to buildJsonObject {
                    put("header", JsonObject(header + ("reportId" to JsonPrimitive("expired"))))
                    put("notifyUntil", openedAt)
                }
            ))
            repo.receive(deliver(expired))
            assertTrue(repo.state.value.reportNotices.isEmpty())
            assertNull(runtime.reportReminders.open(pair, epoch, "expired"))
            assertTrue(manager.activeNotifications.none { it.tag?.endsWith("expired") == true })
        } finally {
            if (lockscreen) deviceCommand("wm dismiss-keyguard")
            manager.cancelAll()
            repo.selectPair(null)
            onTestMainThread {
                ActivityLifecycleMonitorRegistry.getInstance().getActivitiesInStage(Stage.RESUMED)
                    .filterIsInstance<MainActivity>().forEach { it.finish() }
            }
        }
    }

    private fun findText(root: AccessibilityNodeInfo, label: String): AccessibilityNodeInfo? {
        if (root.isVisibleToUser && (root.text?.toString()?.contains(label) == true ||
                root.contentDescription?.toString()?.contains(label) == true)) return root
        for (index in 0 until root.childCount) {
            root.getChild(index)?.let { findText(it, label) }?.let { return it }
        }
        return null
    }
}
