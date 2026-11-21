package dev.tickdesk.app

import android.app.NotificationManager
import android.graphics.Bitmap
import android.util.Log
import androidx.test.platform.app.InstrumentationRegistry
import dev.tickdesk.app.data.RelayClient
import dev.tickdesk.app.data.RoomWorkbenchStore
import dev.tickdesk.core.*
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.selects.select
import kotlinx.serialization.json.*
import okhttp3.CertificatePinner
import okhttp3.OkHttpClient
import okhttp3.tls.HandshakeCertificates
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.ByteArrayOutputStream
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate
import java.util.Base64
import java.util.UUID
import kotlin.random.Random

/** Uses a separate Room database and temporary service pair; never writes the installed pairing. */
class ThreeTierIntegrationTest {
    // WorkbenchRuntime performs repository and relay work on IO. Keep the test
    // dispatcher identical so synchronous protocol work cannot starve its timer loop.
    @Test fun realAndroidRelayAndLocalBusinessRoundTrip() = runBlocking(Dispatchers.IO) {
        val instrument = InstrumentationRegistry.getInstrumentation()
        val args = InstrumentationRegistry.getArguments()
        assumeTrue("Run with scripts/integration_android.py", args.getString("threeTierTest") == "true")
        val context = instrument.targetContext
        val cert = Base64.getDecoder().decode(requireNotNull(args.getString("caDer"))).inputStream().use {
            CertificateFactory.getInstance("X.509").generateCertificate(it) as X509Certificate
        }
        val tls = HandshakeCertificates.Builder().addTrustedCertificate(cert).build()
        val client = OkHttpClient.Builder().sslSocketFactory(tls.sslSocketFactory(), tls.trustManager).build()
        val config = Protocol.pairing(buildJsonObject {
            put("protocol", 2); put("pairId", requireNotNull(args.getString("pairId")))
            put("appToken", requireNotNull(args.getString("appBearer")))
            put("relayUrl", "wss://127.0.0.1:8443/relay/v2/connect")
            put("pins", JsonArray(listOf(JsonPrimitive(CertificatePinner.pin(cert)))))
        }.toString())
        val name = "three-tier-${UUID.randomUUID()}.db"
        val connectionScope = this
        val notifications = context.getSystemService(NotificationManager::class.java)
        val ownNotificationIds = mutableSetOf<Int>()
        val presentationScope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
        // MIUI may suspend a headless instrumentation process even while Android
        // reports an instrumentation FGS. Exercise foreground business flows visibly;
        // BackgroundReminderServiceTest separately verifies the supported background path.
        val activity = launchResumedMainActivity()
        try {
            RoomWorkbenchStore.open(context, name).use { store ->
                val repo = WorkbenchRepository(store)
                val presenter = ReminderPresenter(context, repo, presentationScope) { false }
                val relay = RelayClient(repo, client) { event ->
                    Log.i("TickDeskThreeTier", "wire kind=${event.kind} cmd=${event.command} seq=${event.sequence}" +
                        " ok=${event.data.flag("ok")} error=${event.data.obj("error")?.text("code")}" +
                        " page=${event.data.number("pageIndex")} more=${event.data.flag("hasMore")}")
                    presenter.receive(event)
                }
                repo.selectPair(config.pairId)
                var socket = connectionScope.async { relay.run(config) }
                suspend fun await(label: String, predicate: (WorkbenchProjection) -> Boolean): WorkbenchProjection {
                    val waiting = async { repo.state.first(predicate) }
                    return try {
                        withTimeout(20_000) { select {
                            waiting.onAwait { it }
                            socket.onAwait { error("$label: relay ended ${it.reason}") }
                        } }
                    } catch (failure: TimeoutCancellationException) {
                        error("$label timed out; connection=${repo.state.value.connection}, error=${repo.state.value.connectionError}, syncError=${repo.state.value.syncError}")
                    } finally { waiting.cancelAndJoin() }
                }
                suspend fun command(cmd: String, data: JsonObject = buildJsonObject {}): IntentView {
                    val id = repo.enqueue(cmd, data)
                    return await(cmd) { state -> state.intents.any {
                        it.id == id && it.stage in setOf(DeliveryStage.APPLIED, DeliveryStage.REJECTED)
                    } }.intents.single { it.id == id }
                }
                fun success(intent: IntentView): JsonObject {
                    assertEquals("${intent.command}: ${intent.errorCode}", DeliveryStage.APPLIED, intent.stage)
                    return requireNotNull(intent.result)
                }
                try {
                    withTimeout(120_000) {
                        val initial = await("bootstrap") { it.connection.canWrite }
                        assertEquals(23, initial.records["tasks"]?.size)
                        assertFalse(initial.requiresReview)
                        val initialEpoch = initial.epoch
                        val initialIdentity = repo.hello()
                        // WorkbenchRuntime also rechecks active reminders after full snapshot publication.
                        BackgroundReminderPolicy.currentReferences(initial, System.currentTimeMillis())
                            .forEach { presenter.receive(it, config.pairId) }
                        stage("paged_sync_23_tasks")

                        // Scheduler -> local outbox -> relay -> Android verification -> actual notification.
                        assertTrue("Notifications must already be allowed", notificationsAvailable(context))
                        stage("notification_permission_checked")
                        val reminderTask = await("scheduled reminder") { state ->
                            state.records["tasks"]?.any { it.text("taskId") == "integration_reminder" && it.obj("currentReminder") != null } == true
                        }.records.getValue("tasks").single { it.text("taskId") == "integration_reminder" }
                        val reference = BackgroundReminderPolicy.currentReferences(repo.state.value, System.currentTimeMillis())
                            .single { it.taskId == "integration_reminder" }
                        val notificationId = "${config.pairId}:${reference.epoch}:${reference.reminderId}".hashCode()
                        ownNotificationIds += notificationId
                        // The presenter waits for the server's scheduledAt when the emulator clock lags.
                        val clockLead = (reference.scheduledAt - System.currentTimeMillis()).coerceIn(0, 60_000)
                        withTimeout(clockLead + 10_000) {
                            while (notifications.activeNotifications.none { it.id == notificationId }) delay(50)
                        }
                        val posted = notifications.activeNotifications.single { it.id == notificationId }.postTime
                        assertEquals(ReminderOutcome.DONE, presenter.resolve(reference, config.pairId, fromPush = false))
                        assertEquals(posted, notifications.activeNotifications.single { it.id == notificationId }.postTime)
                        assertNotNull(presenter.open(config.pairId, reference.epoch, reference.reminderId))
                        stage("scheduled_notification_verified_and_deduplicated")

                        success(command("ignore", buildJsonObject {
                            put("taskId", reference.taskId); put("baseActionVersion", reminderTask.number("actionVersion")!!)
                        }))
                        assertNull(presenter.open(config.pairId, reference.epoch, reference.reminderId))
                        notifications.cancel(notificationId)
                        stage("terminal_notification_click_rejected")

                        val status = success(command("get_status"))
                        assertNotNull(status.obj("protocol"))
                        val settings = repo.state.value.meta!!.obj("settings")!!
                        val update = buildJsonObject {
                            put("baseSettingsRevision", settings.number("settingsRevision")!!)
                            put("reminderModes", settings.obj("reminderModes")!!)
                            put("nightStart", "00:00"); put("nightEnd", "23:59")
                            put("criticalAtNight", false); put("pushEnabled", false)
                        }
                        val changedSettings = success(command("set_settings", update)).obj("settings")!!
                        assertEquals(changedSettings, repo.state.value.meta!!.obj("settings"))
                        val staleSettings = command("set_settings", update)
                        assertEquals(DeliveryStage.REJECTED, staleSettings.stage)
                        assertEquals("SETTINGS_VERSION_CONFLICT", staleSettings.errorCode)
                        stage("settings_ack_sync_and_conflict")

                        val revision = repo.state.value.meta!!.number("positionRevision")!!
                        success(command("adjust_position", buildJsonObject {
                            put("symbol", "300502.SZ"); put("basePositionRevision", revision)
                            put("qty", 100); put("availableQty", 100); put("cost", "10.00")
                            put("asOf", System.currentTimeMillis() - 120_000); put("reason", "isolated three-tier test")
                            put("resolvedExecutionIds", JsonArray(emptyList())); put("pendingLots", JsonArray(emptyList()))
                        }))
                        assertEquals(100L, repo.state.value.records.getValue("positions").single().number("qty"))
                        val execution = success(command("record_execution", buildJsonObject {
                            put("symbol", "300502.SZ"); put("side", "buy"); put("price", "10.00")
                            put("qty", 100); put("executedAt", System.currentTimeMillis() - 60_000)
                        }))
                        assertNotNull(execution.obj("execution"))
                        assertEquals(1, repo.state.value.records["executions"]?.size)
                        stage("position_and_execution_ack_sync")

                        val actionTask = repo.state.value.records.getValue("tasks").single { it.text("taskId") == "integration_action" }
                        success(command("snooze", buildJsonObject {
                            put("taskId", "integration_action"); put("baseActionVersion", actionTask.number("actionVersion")!!)
                        }))
                        val snoozed = success(command("get_task", buildJsonObject { put("taskId", "integration_action") })).obj("task")!!
                        assertTrue(snoozed.number("snoozeUntil")!! > System.currentTimeMillis())
                        val staleAction = command("ignore", buildJsonObject {
                            put("taskId", "integration_action"); put("baseActionVersion", actionTask.number("actionVersion")!!)
                        })
                        assertEquals(DeliveryStage.REJECTED, staleAction.stage)
                        success(command("ignore", buildJsonObject {
                            put("taskId", "integration_action"); put("baseActionVersion", snoozed.number("actionVersion")!!)
                        }))
                        val history = success(command("get_task_actions", buildJsonObject { put("taskId", "integration_action") }))
                        assertTrue(history.array("items")!!.size >= 2)
                        stage("snooze_version_conflict_ignore_history")

                        assertEquals(true, success(command("pause")).flag("paused"))
                        assertEquals(true, repo.state.value.meta!!.obj("analysis")!!.flag("paused"))
                        assertEquals(false, success(command("resume")).flag("paused"))
                        assertEquals(false, repo.state.value.meta!!.obj("analysis")!!.flag("paused"))
                        stage("pause_resume_ack_sync")

                        socket.cancelAndJoin()
                        repo.disconnect()
                        val queued = repo.enqueue("set_profile", buildJsonObject { put("profile", "conservative") })
                        assertEquals(DeliveryStage.PENDING, repo.state.value.intents.single { it.id == queued }.stage)
                        socket = connectionScope.async { relay.run(config) }
                        val resumed = await("offline command") { state -> state.connection.canWrite && state.intents.any {
                            it.id == queued && it.stage == DeliveryStage.APPLIED
                        } }
                        assertEquals(initialEpoch, resumed.epoch)
                        assertEquals(initialIdentity, repo.hello())
                        assertEquals(1, resumed.intents.count { it.id == queued })
                        assertEquals("conservative", resumed.meta!!.obj("analysis")!!.text("profile"))
                        stage("offline_queue_reconnect_same_identity")

                        if (args.getString("completeFeatures") == "true") {
                            val search = success(command("search_symbols", buildJsonObject { put("query", "300502") }))
                            assertEquals("300502.SZ", search.array("items")!!.single().jsonObject.text("symbol"))
                            success(command("watch", buildJsonObject { putJsonArray("symbols") { add(JsonPrimitive("300502.SZ")) } }))
                            assertTrue(repo.state.value.records.getValue("watchlist").any { it.text("symbol") == "300502.SZ" })
                            val firstChart = success(command("get_chart", buildJsonObject {
                                put("symbol", "300502.SZ"); put("adjustment", "none"); put("limit", 2)
                            }))
                            assertEquals(2, firstChart.array("candles")!!.size)
                            val olderChart = success(command("get_chart", buildJsonObject {
                                put("symbol", "300502.SZ"); put("adjustment", "none")
                                put("beforeDate", firstChart.text("nextBeforeDate")!!)
                            }))
                            assertEquals(1, olderChart.array("candles")!!.size)
                            success(command("unwatch", buildJsonObject { putJsonArray("symbols") { add(JsonPrimitive("300502.SZ")) } }))
                            assertTrue(repo.state.value.records.getValue("watchlist").isEmpty())
                            val failedScan = command("scan_now")
                            assertEquals(DeliveryStage.REJECTED, failedScan.stage)
                            assertEquals("rejected", failedScan.result?.text("status"))
                            val latest = success(command("get_candidates"))
                            assertEquals(failedScan.result?.text("scanId"), latest.obj("scan")?.text("scanId"))
                            assertTrue(latest.array("items")!!.isEmpty())
                            stage("market_search_watch_chart_pages_failed_round")
                            val chat = command("chat", buildJsonObject { put("text", "说明未知现金。") })
                            success(chat)
                            val reply = repo.state.value.replies.single { it.text("requestId") == chat.id }
                            assertEquals("completed", reply.text("status"))
                            assertEquals(true, reply.flag("unverified"))
                            val messages = success(command("get_messages"))
                            assertTrue(messages.array("items")!!.any { it.jsonObject.obj("reply") == JsonObject(reply - "receivedAt") })
                            stage("chat_accepted_complete_full_history")
                            if (args.getString("pythonAssistant") == "true") {
                                suspend fun ask(text: String): Pair<IntentView, JsonObject> {
                                    val request = command("chat", buildJsonObject { put("text", text) })
                                    success(request)
                                    val answer = await("AI reply") { state -> state.replies.any { it.text("requestId") == request.id } }
                                        .replies.single { it.text("requestId") == request.id }
                                    assertEquals("completed", answer.text("status"))
                                    return request to answer
                                }
                                val ledgerBefore = repo.state.value.records.getValue("positions")
                                fun stableLedger(rows: List<JsonObject>) = rows.map { row ->
                                    JsonObject(row.filterKeys {
                                        it in setOf("symbol", "name", "qty", "availableQty", "cost",
                                            "positionRevision", "pendingLots", "reconciliationRequired")
                                    })
                                }
                                val (reportRequest, report) = ask("/分析 300502.SZ")
                                assertTrue(report.text("text")!!.contains("信息不足"))
                                assertTrue(report.text("text")!!.contains("量化比较"))
                                val prefix = "【TickDesk 报告追问】${reportRequest.id}\n追问：\n"
                                val (_, historical) = ask(prefix + "原报告为什么信息不足？")
                                assertTrue(historical.text("text")!!.contains("依据原报告冻结证据"))
                                val (_, current) = ask(prefix + "现在还有哪些资料缺口？")
                                assertFalse(current.text("text")!!.contains("依据原报告冻结证据"))
                                assertEquals(stableLedger(ledgerBefore),
                                    stableLedger(repo.state.value.records.getValue("positions")))
                                val history = success(command("get_messages"))
                                assertTrue(history.array("items")!!.any {
                                    it.jsonObject.obj("reply")?.text("requestId") == reportRequest.id
                                })
                                stage("python_analysis_report_historical_current_followup_ledger_unchanged")
                            }
                        }

                        val png = samplePng()
                        assertTrue(png.size > Protocol.CHUNK_SIZE)
                        val upload = repo.startUpload("three-tier.png", "image/png", png)
                        val saved = await("upload") { it.transfers.any { transfer -> transfer.id == upload && !transfer.needsDelivery } }
                            .transfers.single { it.id == upload }
                        assertEquals(saved.errorCode, FileTransferStatus.COMPLETE, saved.status)
                        val fileId = requireNotNull(saved.fileId)
                        val listed = success(command("list_files"))
                        assertTrue(listed.array("items")!!.any { it.jsonObject.text("fileId") == fileId })
                        val download = repo.startDownload(fileId)
                        val loaded = await("download") { it.transfers.any { transfer -> transfer.id == download && !transfer.needsDelivery } }
                            .transfers.single { it.id == download }
                        assertEquals(loaded.errorCode, FileTransferStatus.COMPLETE, loaded.status)
                        assertArrayEquals(png, repo.readDownloaded(download))
                        await("file receipts") { !it.hasPendingReceipts }
                        relay.flushWrites()
                        if (args.getString("completeFeatures") == "true") {
                            val noticeVersion = repo.state.value.meta!!.obj("visionNotice")!!.text("noticeVersion")!!
                            success(command("accept_vision_notice", buildJsonObject { put("noticeVersion", noticeVersion); put("accepted", true) }))
                            success(command("register_image", buildJsonObject { put("importId", "complete_import"); put("fileId", fileId) }))
                            val recognized = repo.state.value.records.getValue("imports").single { it.text("importId") == "complete_import" }
                            assertEquals("recognized", recognized.text("status"))
                            assertEquals("300502.SZ", recognized.array("recognizedRows")!!.single().jsonObject.obj("raw")!!.text("symbol"))
                            val unresolvedExecutions = repo.state.value.records.getValue("executions")
                                .filter { it.text("positionEffect") == "needs_reconciliation" }.map { JsonPrimitive(it.text("executionId")!!) }
                            val preview = success(command("apply_import", buildJsonObject {
                                // Host and phone clocks can differ by a small amount; this is still a
                                // real observed time, not a fabricated future timestamp.
                                put("importId", "complete_import"); put("phase", "preview"); put("snapshotAsOf", System.currentTimeMillis() - 5_000)
                                putJsonArray("rows") { add(buildJsonObject {
                                    put("rowId", "row1"); put("symbol", "300502.SZ"); put("name", "隔离联调证券")
                                    put("qty", 200); put("availableQty", 200); put("cost", "10.00")
                                    put("pendingLots", JsonArray(emptyList())); put("resolvedExecutionIds", JsonArray(unresolvedExecutions))
                                }) }
                            }))
                            assertEquals(true, preview.flag("readyForConfirm"))
                            val confirm = buildJsonObject {
                                put("importId", "complete_import"); put("phase", "confirm")
                                put("importVersion", preview.number("importVersion")!!); put("diffHash", preview.text("diffHash")!!)
                            }
                            success(command("apply_import", confirm))
                            assertEquals(200L, repo.state.value.records.getValue("positions").single().number("qty"))
                            assertEquals(true, success(command("apply_import", confirm)).flag("duplicate"))
                            val pendingId = repo.enqueue("register_image", buildJsonObject { put("importId", "cancel_import"); put("fileId", fileId) })
                            await("recognition accepted") { it.intents.any { item -> item.id == pendingId && item.stage == DeliveryStage.ACCEPTED } }
                            assertEquals("cancelled", success(command("cancel_import", buildJsonObject { put("importId", "cancel_import") })).text("status"))
                            val revoked = success(command("accept_vision_notice", buildJsonObject { put("noticeVersion", noticeVersion); put("accepted", false) }))
                            assertNull(revoked.number("consentedAt"))
                            val denied = command("register_image", buildJsonObject { put("importId", "denied_import"); put("fileId", fileId) })
                            assertEquals("VISION_CONSENT_REQUIRED", denied.errorCode)
                            stage("vision_consent_recognition_preview_commit_idempotency_cancel_revoke")
                        }
                        val tombstone = success(command("delete_file", buildJsonObject {
                            put("fileId", fileId); put("baseVersion", requireNotNull(saved.fileVersion)); put("confirm", true)
                        }))
                        assertEquals("deleted", tombstone.obj("file")!!.text("status"))
                        stage("file_upload_list_download_hash_delete")
                        if (args.getString("completeFeatures") == "true") {
                            // Real command/event chain, with only the market input supplied by fixtures.
                            success(command("set_profile", buildJsonObject { put("profile", "aggressive") }))
                            val scan = success(command("scan_now"))
                            val taskId = scan.array("publishedTaskIds")!!.single().jsonPrimitive.content
                            val candidates = success(command("get_candidates", buildJsonObject { put("scanId", scan.text("scanId")) }))
                            assertEquals(scan.text("scanId"), candidates.obj("scan")!!.text("scanId"))
                            assertEquals("300502.SZ", candidates.array("items")!!.single().jsonObject.text("symbol"))
                            val task = success(command("get_task", buildJsonObject { put("taskId", taskId) })).obj("task")!!
                            val actData = buildJsonObject {
                                put("taskId", taskId); put("baseActionVersion", task.number("actionVersion")!!)
                                put("side", "buy"); put("price", "12.25"); put("qty", 100)
                                put("executedAt", System.currentTimeMillis() - 5_000)
                            }
                            val acted = success(command("act", actData))
                            assertEquals("acted", acted.obj("task")!!.text("status"))
                            assertEquals(taskId, acted.obj("execution")!!.text("taskId"))
                            assertEquals(DeliveryStage.REJECTED, command("act", actData).stage)
                            assertEquals(2, repo.state.value.records.getValue("executions").size)
                            val unresolved = repo.state.value.records.getValue("executions")
                                .filter { it.text("positionEffect") == "needs_reconciliation" }
                                .map { JsonPrimitive(it.text("executionId")!!) }
                            success(command("adjust_position", buildJsonObject {
                                put("symbol", "300502.SZ"); put("basePositionRevision", repo.state.value.meta!!.number("positionRevision")!!)
                                put("qty", 300); put("availableQty", 300); put("cost", "10.75")
                                put("asOf", System.currentTimeMillis() - 5_000); put("reason", "隔离假数据成交后的人工对账")
                                put("resolvedExecutionIds", JsonArray(unresolved)); put("pendingLots", JsonArray(emptyList()))
                            }))
                            val finalState = repo.state.value
                            assertEquals(300L, finalState.records.getValue("positions").single().number("qty"))
                            assertEquals(false, finalState.records.getValue("positions").single().flag("reconciliationRequired"))
                            assertEquals("3675", finalState.meta!!.obj("valuation")!!.text("positionMarketValue"))
                            assertTrue(finalState.records.getValue("valuationPoints").isNotEmpty())
                            val actions = success(command("get_task_actions", buildJsonObject { put("taskId", taskId) }))
                            assertTrue(actions.array("items")!!.any { it.jsonObject.text("type") == "acted" })
                            stage("scan_task_act_execution_reconcile_position_valuation_closed_loop")
                        }
                        assertEquals(initialEpoch, repo.state.value.epoch)
                        stage("PASS_ALL")
                    }
                } finally {
                    stage("socket_cleanup_begin")
                    socket.cancelAndJoin()
                    stage("socket_cleanup_complete")
                    presentationScope.coroutineContext[Job]!!.cancelAndJoin()
                    presenter.dismiss()
                    stage("presenter_cleanup_complete")
                }
            }
        } finally {
            presentationScope.cancel()
            ownNotificationIds.forEach(notifications::cancel)
            context.deleteDatabase(name)
            onTestMainThread { activity.finish() }
            stage("isolated_database_removed")
        }
        Unit
    }

    private fun samplePng(): ByteArray {
        val bitmap = Bitmap.createBitmap(128, 128, Bitmap.Config.ARGB_8888)
        try {
            val random = Random(4817)
            bitmap.setPixels(IntArray(128 * 128) { random.nextInt(0x1000000) or 0xff000000.toInt() }, 0, 128, 0, 0, 128, 128)
            return ByteArrayOutputStream().use { out ->
                check(bitmap.compress(Bitmap.CompressFormat.PNG, 100, out)); out.toByteArray()
            }
        } finally { bitmap.recycle() }
    }

    private fun stage(value: String) { Log.i("TickDeskThreeTier", value); println("THREE_TIER: $value") }
}
