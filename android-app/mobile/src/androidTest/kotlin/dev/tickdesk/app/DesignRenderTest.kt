package dev.tickdesk.app

import android.graphics.Bitmap
import android.graphics.Rect
import android.os.Build
import android.os.Bundle
import android.os.SystemClock
import android.view.MotionEvent
import android.view.accessibility.AccessibilityNodeInfo
import androidx.activity.compose.setContent
import androidx.compose.foundation.ScrollState
import androidx.compose.foundation.layout.Box
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.test.platform.app.InstrumentationRegistry
import dev.tickdesk.app.ui.*
import dev.tickdesk.core.*
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Test
import java.io.File

class DesignRenderTest {
    private val instrument = InstrumentationRegistry.getInstrumentation()
    private val automation = instrument.uiAutomation
    private val cases = instrument.context.assets.open("v2/fixtures.json").bufferedReader().use { input ->
        Json.parseToJsonElement(input.readText()).jsonObject.array("cases")!!
            .associate { it.jsonObject.text("name")!! to it.jsonObject.obj("payload")!! }
    }
    private val now = System.currentTimeMillis()
    private val allScreens by lazy { projection() }
    private lateinit var bodyScroll: ScrollState

    @Test fun scheduledScanReasonsRemainVisibleWithoutCandidatesOrTasks() {
        val activity = launchResumedMainActivity()
        val model = WorkbenchViewModel(activity.application)
        val output = File(instrument.targetContext.filesDir, "q02-scan-results").apply { mkdirs() }
        val event = cases.getValue("scan_result")
        val original = event.obj("data")!!
        val empty = JsonArray(emptyList())
        val fixtureState = allScreens.copy(
            connection = allScreens.connection.copy(localOnline = false),
            canQueue = false,
            records = emptyMap(),
            reads = emptyMap(),
        )
        val projection = mutableStateOf(fixtureState)
        bodyScroll = ScrollState(0)
        fun render(
            index: Int, slot: String, status: String, code: String, message: String,
            reason: String? = null, trigger: String = "scheduled",
        ) {
            val missed = code == "SCAN_SCHEDULE_MISSED"
            val scheduledAt = java.time.OffsetDateTime.parse("2026-11-16T${slot}:00+08:00").toInstant().toEpochMilli()
            val finishedAt = scheduledAt + if (missed) 3_000L else 950L
            val scanId = "q02_scan_${index}"
            val scan = original.with(
                "scanId" to str(scanId), "trigger" to str(trigger),
                "slot" to if (trigger == "scheduled") str(slot) else JsonNull,
                "status" to str(status), "requestedAt" to long(scheduledAt),
                "startedAt" to if (missed) JsonNull else long(scheduledAt), "finishedAt" to long(finishedAt),
                "dataAsOf" to JsonNull, "dataStatus" to str("unknown"), "degraded" to bool(true),
                "strategy" to buildJsonObject { put("total", 0); put("succeeded", 0); put("failed", empty) },
                "publishedTaskIds" to empty,
                "error" to buildJsonObject { put("code", code); put("message", message) },
                "rejectionSummary" to if (reason == null) empty else buildJsonArray {
                    add(buildJsonObject { put("reasonCode", reason); put("count", 1) })
                },
            )
            val received = Protocol.business(event.with(
                "id" to str("q02_event_${index}"), "dedupeKey" to str("scan:${scanId}:result"),
                "ts" to long(finishedAt), "snapshotSeq" to long(42L + index), "data" to scan,
            ))
            val reads = if (missed) {
                // Restart/missed-slot ledger is also visible after get_candidates rehydrates the latest round.
                val candidates = result("ack_get_candidates").with(
                    "scan" to received.data, "items" to empty, "nextPageCursor" to JsonNull, "historical" to bool(false),
                )
                mapOf("get_candidates" to candidates)
            } else mapOf("scan_result" to received.data)
            onTestMainThread { projection.value = fixtureState.copy(reads = reads) }
            waitForNode(message)
            waitForNode(if (trigger == "scheduled") "定时扫描" else "手动扫描")
            val badge = when (status) {
                "skipped" -> "已跳过"
                "rejected" -> "已拒绝"
                "suppressed" -> "已抑制"
                else -> "扫描失败"
            }
            waitForNode(badge)
            assertNull("A failed or skipped scan cannot use the completion badge", find("全量完成"))
            val wasRecordedOnly = status in setOf("skipped", "rejected", "suppressed")
            if (wasRecordedOnly) {
                assertNull("$badge should not claim scan completion", find("扫描完成于"))
                assertNull("$badge should not claim the scan ended", find("扫描结束于"))
                waitForNode("记录于")
            } else {
                waitForNode("扫描结束于")
                assertNull("A scan failure should still name the scan end", find("记录于"))
            }
            screenshot(output, "q02-${index}")
            scrollTo("候选标的（按综合评分排序）")
            assertNull(find("查看核验事项"))
            assertNull(find("当前截面没有符合条件的候选标的"))
            runBlocking { withContext(Dispatchers.Main) { bodyScroll.scrollTo(0) } }
            val timeLabel = if (wasRecordedOnly) "记录于" else "扫描结束于"
            android.util.Log.i("TickDeskQ02", "${slot} ${status} ${code}: ${badge} · ${timeLabel} · ${message}")
        }
        try {
            onTestMainThread {
                activity.setContent {
                    TickDeskTheme { TickDeskScreen(model, projection.value, initialTab = Tab.DISCOVER, bodyScrollState = bodyScroll) }
                }
            }
            render(1, "09:25", "skipped", "SCAN_OUTSIDE_WINDOW", "已确认休市，本轮未扫描。")
            render(2, "09:30", "failed", "DATA_UNAVAILABLE", "交易日历待核验，本轮未扫描。", "TRADING_CALENDAR_UNAVAILABLE")
            render(3, "09:35", "failed", "DATA_UNAVAILABLE", "量化服务未通过连接验证。")
            render(6, "12:00", "rejected", "SCAN_OUTSIDE_WINDOW", "当前不在扫描时段。", trigger = "manual")
            render(7, "13:00", "suppressed", "SCAN_PAUSED", "SCAN_PAUSED")
            render(4, "14:35", "skipped", "SCAN_SCHEDULE_MISSED", "14:35 轮次错过计划时点，已跳过。")
            click("事项", exact = true)
            waitForNode("当前没有事项")
            assertNull(find("查看核验事项"))
            screenshot(output, "q02-5-empty-inbox")
            android.util.Log.i("TickDeskQ02", "跳过后事项页：当前没有事项")
        } finally {
            onTestMainThread { activity.finish() }
        }
    }

    @Test fun reviewedScanFailureShowsChineseReasonCounts() {
        val activity = launchResumedMainActivity()
        val model = WorkbenchViewModel(activity.application)
        val output = File(instrument.targetContext.filesDir, "v3-scan-results").apply { mkdirs() }
        val reasons = listOf(
            "EXPECTED_DECISION_BAR_UNAVAILABLE" to "已收盘日线待核验",
            "REVIEWED_UNIVERSE_INCOMPLETE" to "审阅证券资料不齐",
            "REVIEWED_DIRECTION_UNAVAILABLE" to "买卖条件待核验",
            "CURRENT_EXECUTION_QUOTE_UNAVAILABLE" to "逐证券实时报价待核验",
            "CURRENT_TURNOVER_UNAVAILABLE" to "当日成交额待核验",
            "EXIT_SCORE_UNVERIFIED" to "卖出评分待核验",
            "ENTRY_SCORE_UNVERIFIED" to "买入评分待核验",
        )
        val event = cases.getValue("scan_result")
        val failureMessage = "正式策略资料待核验。"
        val result = Protocol.business(event.with("data" to data("scan_result").with(
            "status" to str("failed"), "degraded" to bool(true),
            "dataStatus" to str("unknown"),
            "strategy" to buildJsonObject {
                put("total", reasons.size)
                put("succeeded", 0)
                putJsonArray("failed") { reasons.indices.forEach { add("reviewed_${it + 1}") } }
            },
            "error" to buildJsonObject { put("code", "SCAN_FAILED"); put("message", failureMessage) },
            "rejectionSummary" to buildJsonArray {
                reasons.forEachIndexed { index, (code, _) ->
                    add(buildJsonObject { put("reasonCode", code); put("count", index + 1) })
                }
            },
        )))
        val projection = allScreens.copy(
            connection = allScreens.connection.copy(localOnline = false), canQueue = false,
            records = emptyMap(), reads = mapOf("scan_result" to result.data),
        )
        bodyScroll = ScrollState(0)
        try {
            onTestMainThread {
                activity.setContent { TickDeskTheme {
                    TickDeskScreen(model, projection, initialTab = Tab.DISCOVER, bodyScrollState = bodyScroll)
                } }
            }
            waitForNode("扫描失败")
            waitForNode(failureMessage)
            assertNull(find("全量完成"))
            reasons.forEachIndexed { index, (code, label) ->
                val expected = "$label · ${index + 1}"
                assertNotNull("Failed scan must show '$expected'", find(expected, exact = true))
                assertNull(find(code))
                android.util.Log.i("TickDeskV3Scan", "扫描失败 · $expected")
            }
            screenshot(output, "failed-reviewed-scan")
        } finally {
            onTestMainThread { activity.finish() }
        }
    }

    @Test fun fullReplyLinkRequiresBothArchiveMarkerAndCurrentCapability() {
        val activity = launchResumedMainActivity()
        val model = WorkbenchViewModel(activity.application)
        val status = result("ack_get_status")
        val protocol = status.obj("protocol")!!
        val commands = protocol.array("supportedCommands")!!.filterNot { it == str("get_reply_page") }
        fun statusWith(commands: List<JsonElement>) = status.with("protocol" to protocol.with("supportedCommands" to JsonArray(commands)))
        val legacy = statusWith(commands)
        val capable = statusWith(commands + str("get_reply_page"))
        val reply = data("reply").with("text" to str("已经保存的答复节选"), "sources" to JsonArray(listOf(buildJsonObject {
            put("name", "本地原文"); put("ref", "local:chat-output:job_fixture"); put("asOf", now)
        })))
        val original = allScreens.copy(peerIncarnation = "fixture-local", messages = emptyList(), replies = listOf(reply),
            reads = allScreens.reads + ("get_status" to legacy))
        val projection = mutableStateOf(original)
        try {
            onTestMainThread {
                activity.setContent { TickDeskTheme { TickDeskScreen(model, projection.value, initialTab = Tab.CHAT) } }
            }
            waitForNode("已经保存的答复节选")
            assertNull(find("查看全文"))
            onTestMainThread { projection.value = original.copy(reads = original.reads + ("get_status" to capable)) }
            waitForNode("查看全文")
            onTestMainThread {
                projection.value = projection.value.copy(replies = listOf(reply.with(
                    "text" to str("正文提及 local:chat-output:job_fixture"), "sources" to JsonArray(emptyList()),
                )))
            }
            waitForNode("正文提及 local:chat-output:job_fixture")
            assertNull(find("查看全文"))
        } finally {
            onTestMainThread { activity.finish() }
        }
    }

    @Test fun activeBuyOpensDetailsBeforeExecution() {
        val activity = launchResumedMainActivity()
        val model = WorkbenchViewModel(activity.application)
        bodyScroll = ScrollState(0)
        try {
            onTestMainThread {
                activity.setContent {
                    TickDeskTheme { TickDeskScreen(model, allScreens, bodyScrollState = bodyScroll) }
                }
            }
            waitForNode("贵州茅台")
            scrollTo("中际旭创")
            tap("中际旭创")
            waitForNode("刷新事项")
            assertNull("Opening an active buy card must not open the execution form", find("成交台账录入"))
            scrollTo("忽略本次")
            waitForNode("稍后 5 分钟")
            scrollTo("契约测试行情")
        } finally {
            onTestMainThread { activity.finish() }
        }
    }

    @Test fun reportDirectoryOpensPersistedDetailsWithCoverageSourcesAndFollowUpModes() {
        val activity = launchResumedMainActivity()
        val model = WorkbenchViewModel(activity.application)
        bodyScroll = ScrollState(0)
        val detail = result("ack_get_report")
        val header = detail.obj("header")!!.with(
            "coverage" to buildJsonObject { put("business", "available"); put("quant", "partial"); put("external", "unavailable") },
            "summary" to str("量化结论不可用\n" + "经营与财报仍需结合最新证据核对。\n".repeat(22)),
        )
        val list = result("ack_list_reports").with("items" to JsonArray(listOf(header)))
        val report = detail.with("header" to header, "sources" to JsonArray(listOf(buildJsonObject {
            put("name", "隔离报告证据"); put("ref", "https://example.org/fact"); put("asOf", now)
        })))
        val id = report.obj("header")!!.text("reportId")!!
        val projection = allScreens.copy(
            connection = allScreens.connection.copy(localOnline = false), intents = listOf(
                IntentView("list", "list_reports", buildJsonObject { put("serverEpoch", allScreens.epoch!!) },
                    DeliveryStage.APPLIED, null, list, now, null, false),
                IntentView("detail", "get_report", buildJsonObject { put("reportId", id); put("serverEpoch", allScreens.epoch!!) },
                    DeliveryStage.APPLIED, null, report, now, null, false),
                IntentView("restore-old", "get_report", buildJsonObject { put("reportId", id); put("serverEpoch", "prior_epoch") },
                    DeliveryStage.APPLIED, null, report.with("text" to str("恢复前的报告正文"), "sources" to JsonArray(emptyList())), now, null, false),
            ),
            reportReads = ReportReads(
                catalogues = mapOf(ReportCatalogueQuery() to list),
                headers = mapOf(id to header),
            ),
        )
        try {
            onTestMainThread {
                activity.setContent {
                    TickDeskTheme { TickDeskScreen(model, projection, initialTab = Tab.CHAT, bodyScrollState = bodyScroll) }
                }
            }
            click("分析报告")
            waitForNode("按证券代码筛选")
            waitForNode("量化 部分覆盖")
            click("查看报告")
            waitForNode("报告详情")
            waitForNode("报告摘要")
            assertTrue("Frozen quant coverage must precede the report summary",
                bounds(waitForNode("量化 部分覆盖")).top < bounds(waitForNode("量化结论不可用")).top)
            assertNull("An archive read from a prior epoch may not appear", find("恢复前的报告正文"))
            scrollTo("证据截至")
            scrollTo("外部事实")
            scrollTo("有效至")
            scrollTo("隔离报告证据")
            click("隔离报告证据")
            waitForNode("打开来源网页")
            click("关闭")
            waitForNode("按原报告追问")
            click("按当前情况追问")
        } finally {
            onTestMainThread { activity.finish() }
        }
    }

    @Test fun failedRichReplyOffersConversationRecoveryAndDisablesReportFollowUp() {
        val activity = launchResumedMainActivity()
        val model = WorkbenchViewModel(activity.application)
        val id = "isolated_failed_reply"
        val failed = data("reply").with("requestId" to str(id), "status" to str("failed"),
            "errorCode" to str("AI_UNVERIFIED_SOURCE"), "text" to str("本次来源未通过核验"),
            "attachments" to JsonArray(emptyList()), "sources" to JsonArray(emptyList()))
        val projection = allScreens.copy(messages = emptyList(), replies = listOf(failed))
        try {
            onTestMainThread {
                activity.setContent {
                    TickDeskTheme { TickDeskScreen(model, projection, initialTab = Tab.CHAT, initialPage = "rich", initialTarget = id) }
                }
            }
            waitForNode("答复失败")
            setText(waitForNode("继续追问"), "检查原始证据")
            assertFalse("Failed AI output may not be used as evidence for another report query",
                generateSequence(waitForNode("发送消息")) { it.parent }.any { it.isEnabled && it.isClickable })
            waitForNode("原答复失败 · 可返回对话重新提问")
            click("返回对话重新提问")
            waitForNode("输入消息")
        } finally {
            onTestMainThread { activity.finish() }
        }
    }

    @Test fun ruleOnlySuspensionShowsSourceWithoutQuantScore() {
        val activity = launchResumedMainActivity()
        val model = WorkbenchViewModel(activity.application)
        bodyScroll = ScrollState(0)
        val evidence = Protocol.business(cases.getValue("task_verified_risk_without_score")).data
        val task = evidence.with("createdAt" to long(now - 60_000), "validUntil" to long(now + 1_200_000),
            "strategies" to JsonArray(emptyList()))
        val state = allScreens.copy(records = allScreens.records + ("tasks" to listOf(task)))
        try {
            onTestMainThread {
                activity.setContent {
                    TickDeskTheme {
                        TickDeskScreen(model, state, initialTab = Tab.INBOX, initialPage = "task",
                            initialTarget = task.text("taskId")!!, bodyScrollState = bodyScroll)
                    }
                }
            }
            waitForNode("刷新事项")
            scrollTo("规则证据 · 无量化评分")
            scrollTo("交易停牌")
            scrollTo("test://suspension")
        } finally {
            onTestMainThread { activity.finish() }
        }
    }

    @Test fun chartDisplaysOnlyResultsForSelectedPeriodAdjustmentAndPage() {
        val activity = launchResumedMainActivity()
        val model = WorkbenchViewModel(activity.application)
        bodyScroll = ScrollState(0)
        val daily = result("ack_get_chart")
        val symbol = daily.text("symbol")!!
        fun receipt(name: String, period: String?, returnedPeriod: String? = period,
                    adjustment: String = "none", before: String? = null, code: String = symbol,
                    epoch: String = allScreens.epoch!!): IntentView {
            val query = buildJsonObject {
                put("symbol", code); put("adjustment", adjustment)
                put("serverEpoch", epoch)
                period?.let { put("timeframe", it) }
                before?.let { put("beforeDate", it) }
            }
            val response = daily.with("symbol" to str(code), "adjustment" to str(adjustment), "source" to buildJsonObject {
                put("name", name); put("ref", "fixture:chart"); put("asOf", now)
            })
            return IntentView(name, "get_chart", query, DeliveryStage.APPLIED, null,
                JsonObject((response - "timeframe") + (returnedPeriod?.let { mapOf("timeframe" to str(it)) }.orEmpty())),
                now, null, false)
        }
        val status = buildJsonObject { putJsonObject("protocol") { putJsonArray("supportedCommands") { add("get_quote_status") } } }
        val state = allScreens.copy(connection = allScreens.connection.copy(localOnline = false),
            reads = allScreens.reads + ("get_status" to status), intents = listOf(
            receipt("日线基准数据", null),
            receipt("周线基准数据", "1w"),
            receipt("月线基准数据", "1mo"),
            receipt("前复权周线数据", "1w", adjustment = "forward"),
            receipt("更早周线数据", "1w", before = "2026-11-13"),
            receipt("其他证券周线数据", "1w", code = "600519.SH"),
            receipt("错误周期的迟到数据", "1d", returnedPeriod = "1mo"),
            receipt("未注明周期的周线回执", "1w", returnedPeriod = null),
            receipt("恢复前的旧纪元日线", "1d", epoch = "prior_epoch"),
        ))
        try {
            onTestMainThread {
                activity.setContent {
                    TickDeskTheme { TickDeskScreen(model, state, initialTab = Tab.DISCOVER, initialPage = "chart",
                        initialTarget = symbol, bodyScrollState = bodyScroll) }
                }
            }
            waitForNode("日线基准数据")
            click("周线")
            waitForNode("周线基准数据")
            click("前复权")
            waitForNode("前复权周线数据")
            click("月线")
            waitForGone("前复权周线数据")
            click("不复权")
            waitForNode("月线基准数据")
            click("日线")
            waitForNode("日线基准数据")
        } finally {
            onTestMainThread { activity.finish() }
        }
    }

    @Test fun chartQuoteLabelsClosedSessionRateLimitSnapshotTimeAndSuspension() {
        val activity = launchResumedMainActivity()
        val model = WorkbenchViewModel(activity.application)
        bodyScroll = ScrollState(0)
        val result = result("ack_get_quote_status")
        val item = result.objects("items").single()
        val symbol = item.text("symbol")!!
        fun receipt(price: JsonObject, marketSession: String, epoch: String = allScreens.epoch!!): IntentView = IntentView(
            "quote_${price.text("symbol")}", "get_quote_status", buildJsonObject {
                putJsonArray("symbols") { add(price.text("symbol")!!) }
                put("serverEpoch", epoch)
            }, DeliveryStage.APPLIED, null, buildJsonObject {
                put("session", marketSession); putJsonArray("items") { add(price) }
            }, now, null, false,
        )
        val status = result("ack_get_status").let { legacy ->
            val protocol = legacy.obj("protocol")!!
            legacy.with("protocol" to protocol.with("supportedCommands" to JsonArray(
                protocol.array("supportedCommands")!!.toList() + str("get_quote_status"),
            )))
        }
        val original = allScreens.copy(connection = allScreens.connection.copy(localOnline = false),
            reads = allScreens.reads + ("get_status" to status), intents = listOf(
                receipt(item.with("delivery" to str("rate_limited")), "closed"),
                receipt(item.with("symbol" to str("300502.SZ"), "price" to str("99.99")), "open"),
                receipt(item.with("price" to str("88.88")), "open", epoch = "prior_epoch"),
            ))
        val projection = mutableStateOf(original)
        try {
            onTestMainThread {
                activity.setContent {
                    TickDeskTheme { TickDeskScreen(model, projection.value, initialTab = Tab.DISCOVER, initialPage = "chart",
                        initialTarget = symbol, bodyScrollState = bodyScroll) }
                }
            }
            waitForNode("休市")
            waitForNode("数据源限流")
            waitForNode("快照时间")
            waitForNode("10.78")
            assertNull("Quote for a different security leaked into the screen", find("99.99"))
            assertNull("Quote from the pre-restore workspace leaked into the screen", find("88.88"))
            onTestMainThread {
                projection.value = original.copy(intents = listOf(receipt(item.with(
                    "tradingStatus" to str("suspended"), "timeBasis" to str("trade"), "delivery" to str("offline"),
                ), "open")))
            }
            waitForNode("停牌")
            waitForNode("数据源离线")
            waitForNode("成交时间")
            assertNull(find("快照时间"))
        } finally {
            onTestMainThread { activity.finish() }
        }
    }

    @Test fun portfolioShowsSnapshotEstimateOnlyWhenEveryHoldingHasAPrice() {
        val activity = launchResumedMainActivity()
        val model = WorkbenchViewModel(activity.application)
        bodyScroll = ScrollState(0)
        val evidence = File(instrument.targetContext.filesDir, "intraday-reference-valuation").apply { mkdirs() }
        val estimate = allScreens.records.getValue("positions").first().with(
            "name" to str("沪深300ETF"), "symbol" to str("510300.SH"), "qty" to long(1000),
            "availableQty" to long(1000), "cost" to str("4.123"),
            "lastPrice" to str("4.045"), "priceAsOf" to long(now - 30_000), "priceStatus" to str("stale"),
            "marketValue" to JsonNull, "unrealizedPnl" to JsonNull, "unrealizedPnlRatio" to JsonNull,
            "reconciliationRequired" to JsonPrimitive(false),
        )
        val unavailable = estimate.with("name" to str("浦发银行"), "symbol" to str("600000.SH"),
            "lastPrice" to JsonNull, "priceAsOf" to JsonNull, "priceStatus" to str("missing"))
        val base = allScreens.copy(meta = allScreens.meta!!.with("valuation" to buildJsonObject {
            put("coveredCount", 0); put("totalCount", 1); put("positionMarketValue", JsonNull)
            put("unrealizedPnl", JsonNull); put("pricedSubtotal", "0")
            putJsonArray("unpricedSymbols") { add("510300.SH") }
        }), records = allScreens.records + ("positions" to listOf(estimate)))
        val projection = mutableStateOf(base)
        try {
            onTestMainThread {
                activity.setContent {
                    TickDeskTheme { TickDeskScreen(model, projection.value, initialTab = Tab.PORTFOLIO, bodyScrollState = bodyScroll) }
                }
            }
            waitForNode("参考持仓市值（CNY）")
            waitForNode("¥ 4,045.00", exact = true)
            waitForNode("实时估值 0 / 1", exact = true)
            waitForNode("参考行情最早")
            screenshot(evidence, "reference-estimate")
            scrollTo("参考市值")
            onTestMainThread {
                val partial = base.meta!!.obj("valuation")!!.with("totalCount" to long(2),
                    "unpricedSymbols" to JsonArray(listOf(str("510300.SH"), str("600000.SH"))))
                projection.value = base.copy(
                    meta = base.meta!!.with("valuation" to partial),
                    records = base.records + ("positions" to listOf(estimate, unavailable)),
                )
            }
            scrollTo("当前持仓明细 · 2 只")
            onTestMainThread { bodyScroll.dispatchRawDelta(-bodyScroll.value.toFloat()) }
            waitForNode("持仓市值估算（CNY）")
            assertNull(find("参考持仓市值（CNY）"))
            assertNull(find("¥ 4,045.00", exact = true))
            screenshot(evidence, "partial-coverage")
        } finally {
            onTestMainThread { activity.finish() }
        }
    }

    @Test fun settingsSeparatesHistoryCoverageFromHoldingsAndHidesOldEpochResults() {
        val activity = launchResumedMainActivity()
        val model = WorkbenchViewModel(activity.application)
        bodyScroll = ScrollState(0)
        val legacy = result("ack_get_status")
        val protocol = legacy.obj("protocol")!!
        val capable = legacy.with("protocol" to protocol.with("supportedCommands" to JsonArray(
            protocol.array("supportedCommands")!!.toList() + str("get_history_coverage"),
        )))
        fun history(epoch: String, provider: String, recorded: Int) = IntentView(
            "coverage_$epoch", "get_history_coverage", buildJsonObject { put("serverEpoch", epoch) },
            DeliveryStage.APPLIED, null, buildJsonObject {
                put("status", "partial"); put("checkedAt", now); put("reason", "history_pending")
                putJsonObject("coverage") {
                    put("source", provider); put("start", "2026-01-05"); put("through", "2026-11-13")
                    put("expectedThrough", "2026-11-16"); put("updatedAt", now - 60_000)
                    put("rows", 15_000); put("observedSymbols", 99); put("requestedSymbols", 110)
                    putJsonObject("latestDay") { put("observed", recorded); put("expected", 110) }
                    put("conflictingRows", 3)
                }
            }, now, null, false,
        )
        val original = allScreens.copy(connection = allScreens.connection.copy(localOnline = false),
            reads = allScreens.reads + ("get_status" to capable), intents = listOf(
                history(allScreens.epoch!!, "隔离历史源", 90), history("prior_epoch", "旧纪元行情源", 12),
            ))
        val projection = mutableStateOf(original)
        try {
            onTestMainThread {
                activity.setContent {
                    val rendered = projection.value
                    TickDeskTheme {
                        Box(Modifier.semantics {
                            contentDescription = if (rendered.reads["get_status"] == legacy) "legacy-capability-rendered" else "history-capability-rendered"
                        }) {
                            TickDeskScreen(model, rendered, initialTab = Tab.SETTINGS, bodyScrollState = bodyScroll)
                        }
                    }
                }
            }
            waitForNode("配对与连接")
            scrollTo("工作台服务状态")
            click("工作台服务状态")
            waitForNode("AI 分析")
            scrollTo("持仓行情覆盖 1 / 1")
            scrollTo("全市场历史覆盖")
            scrollTo("沪深主板与创业板 · 不复权日线")
            scrollTo("隔离历史源")
            scrollTo("应至 2026-11-16")
            scrollTo("最近交易日已观察 90 / 110")
            scrollTo("缺 20")
            assertNull(find("旧纪元行情源"))
            onTestMainThread { projection.value = original.copy(reads = original.reads + ("get_status" to legacy)) }
            waitForNode("legacy-capability-rendered", exact = true)
            assertNull(find("隔离历史源"))
        } finally {
            onTestMainThread { activity.finish() }
        }
    }

    @Test fun advancedOcrJumpAndDraftSurviveReturningAcrossTabs() {
        val activity = launchResumedMainActivity()
        val model = WorkbenchViewModel(activity.application)
        bodyScroll = ScrollState(0)
        try {
            onTestMainThread {
                activity.setContent { TickDeskTheme { TickDeskScreen(model, allScreens,
                    initialTab = Tab.SETTINGS, bodyScrollState = bodyScroll) } }
            }
            waitForNode("配对与连接")
            scrollTo("原图与高级导入管理")
            click("原图与高级导入管理")
            click("跳到识别结果")
            waitForNode("OCR 识别结果")
            setText(waitForNode("示例标的 名称"), "联调修正标的")
            pressBack()
            runBlocking { withContext(Dispatchers.Main) { bodyScroll.scrollTo(0) } }
            waitForNode("配对与连接")
            click("选股")
            waitForNode("截屏导入持仓")
            click("设置")
            runBlocking { withContext(Dispatchers.Main) { bodyScroll.scrollTo(0) } }
            waitForNode("配对与连接")
            scrollTo("原图与高级导入管理")
            click("原图与高级导入管理")
            if (find("联调修正标的 名称") == null && find("跳到识别结果") != null) click("跳到识别结果")
            val restored = waitForNode("联调修正标的 名称")
            assertEquals("联调修正标的", restored.text?.toString() ?: restored.contentDescription?.toString()?.removeSuffix(" 名称"))
        } finally {
            onTestMainThread { activity.finish() }
        }
    }

    @Test fun populatedReferenceScreensRenderAndActionDocksStayVisible() {
        val context = instrument.targetContext
        val compact = context.resources.configuration.screenWidthDp < 350
        val output = File(context.filesDir, "ui-design").apply { mkdirs() }
        output.listFiles { file -> file.isFile && file.extension == "png" }?.forEach(File::delete)
        val activity = launchResumedMainActivity()
        val model = WorkbenchViewModel(activity.application)
        try {
            fun render(id: String, tab: Tab, page: String = "", target: String = "", expect: String,
                       state: WorkbenchProjection = allScreens, alert: VerifiedReminder? = null, differentFrom: String? = null) {
                val scroll = ScrollState(0)
                bodyScroll = scroll
                instrument.runOnMainSync {
                    activity.setContent {
                        key(id) {
                            Box(Modifier.semantics { contentDescription = "test-render-$id" }) {
                                TickDeskTheme { TickDeskScreen(model, state, tab, page, target, alert, bodyScrollState = scroll) }
                            }
                        }
                    }
                }
                // Consecutive screens can share the same label. Wait for this
                // composition's accessibility tree, not a matching old screen.
                instrument.waitForIdleSync()
                // A reminder opens its own Dialog window; the activity marker
                // is then intentionally hidden from the active accessibility root.
                // That case waits for its unique dialog label below instead.
                if (alert == null) waitForNode("test-render-$id")
                waitForNode(expect)
                screenshot(output, id, differentFrom)
            }

            render("01-inbox", Tab.INBOX, expect = "紧急")
            if (!compact) {
                scrollTo("宁德时代")
                click("宁德时代")
                waitForNode("刷新事项")
                screenshot(output, "01-risk-detail")
                pressBack()
                waitForNode("实时同步")

                render("01-inbox-buy-route", Tab.INBOX, expect = "紧急")
                scrollTo("中际旭创")
                tap("中际旭创")
                waitForNode("刷新事项")
                assertNull("Opening a buy card must start in task details", find("成交台账录入"))
                pressBack()
                waitForNode("实时同步")
            }

            render("02-task", Tab.INBOX, "task", "task_demo", "卖出回填")
            verifyControlsFixed("卖出回填", "刷新事项")
            screenshot(output, "02-task-scrolled")
            click("卖出回填")
            waitForNode("卖出成交回填")
            screenshot(output, "02-sell-sheet")
            click("返回核对")
            waitForNode("刷新事项")

            render("03-execution", Tab.INBOX, "execution", "fixture_buy", "确认写入")
            verifyControlsFixed("确认写入", "成交台账录入")
            scrollTo(if (compact) "保存成交记录 · 非下单" else "实际成交价")
            screenshot(output, "03-execution-fields")

            render("04-discover", Tab.DISCOVER, expect = "截屏导入持仓", differentFrom = "03-execution-fields")
            scrollTo(if (compact) "买入成交记录 / 回填" else "量价突破")
            screenshot(output, "04-discover-candidates")
            click("截屏导入持仓")
            val pickerDeadline = SystemClock.uptimeMillis() + 6_000
            fun pickerVisible() = automation.rootInActiveWindow?.packageName?.toString()
                ?.let { it != context.packageName && it != "com.android.systemui" } == true
            while (!pickerVisible() && SystemClock.uptimeMillis() < pickerDeadline) SystemClock.sleep(80)
            assertTrue("Screenshot shortcut should open the system picker", pickerVisible())
            pressBack()
            waitForNode("截屏导入持仓")
            assertNull("Picker cancellation must leave the discover page visible", find("原图保留与外部识别"))
            if (!compact) {
                click("设置")
                waitForNode("原图与高级导入管理")
                click("原图与高级导入管理")
                waitForNode("原图保留与外部识别")
                click("跳到识别结果")
                waitForNode("OCR 识别结果")
                val field = waitForNode("示例标的 名称")
                setText(field, "联调修正标的")
                pressBack()
                runBlocking { withContext(Dispatchers.Main) { bodyScroll.scrollTo(0) } }
                waitForNode("配对与连接")
                click("选股")
                waitForNode("截屏导入持仓")
                click("设置")
                waitForNode("配对与连接")
                // This reference test injects one viewport for all destinations.
                runBlocking { withContext(Dispatchers.Main) { bodyScroll.scrollTo(0) } }
                waitForNode("原图与高级导入管理")
                click("原图与高级导入管理")
                if (find("联调修正标的") == null && find("跳到识别结果") != null) click("跳到识别结果")
                waitForNode("联调修正标的")
                scrollTo("核对内容已修改")
            }

            val offline = allScreens.connection.copy(localOnline = false)
            val chart = result("ack_get_chart")
            val chartIntent = IntentView("ui_chart", "get_chart", buildJsonObject {
                put("symbol", chart.text("symbol")!!); put("adjustment", chart.text("adjustment")!!)
                put("serverEpoch", allScreens.epoch!!)
            }, DeliveryStage.APPLIED, null, chart, now, null, false)
            render("14-chart", Tab.DISCOVER, "chart", chart.text("symbol")!!, "不复权",
                state = allScreens.copy(connection = offline, intents = listOf(chartIntent)))
            render("15-symbol-search", Tab.DISCOVER, "symbols", expect = "查询证券",
                state = allScreens.copy(connection = offline))

            render("05-portfolio", Tab.PORTFOLIO, expect = "持仓市值估算（CNY）")
            render("06-settings", Tab.SETTINGS, expect = "配对与连接")
            val meta = allScreens.meta!!
            val paused = JsonObject(meta + ("analysis" to JsonObject(meta.obj("analysis")!! + ("paused" to JsonPrimitive(true)))))
            val pausedState = allScreens.copy(meta = paused)
            render("07-analysis-running", Tab.SETTINGS, expect = "分析运行中", page = "analysis")
            render("07-pause", Tab.SETTINGS, expect = "盘中量化扫描已暂停", page = "analysis", state = pausedState)
            render("04-discover-paused", Tab.DISCOVER, expect = "截屏导入持仓", state = pausedState)
            if (compact) {
                waitForNode("分析已暂停")
            } else {
                scrollTo("引擎已暂停 · 无法扫描")
                assertTrue("Paused scanning control must be disabled", generateSequence(
                    waitForNode("引擎已暂停 · 无法扫描")
                ) { it.parent }.any { !it.isEnabled })
                screenshot(output, "04-paused-scan-gate")
            }

            render("09-foreground", Tab.INBOX, expect = "前台强提醒",
                alert = VerifiedReminder(allScreens.records.getValue("tasks").first(), System.currentTimeMillis() + 60_000, "fixture_reminder"))
            waitForNode("返回应用")
            val quickAction = bounds(waitForNode("查看事项详情"))
            val reminderBody = descendants(checkNotNull(automation.rootInActiveWindow)).firstOrNull { it.isScrollable }
            if (reminderBody?.performAction(AccessibilityNodeInfo.ACTION_SCROLL_FORWARD) == true) {
                automation.waitForIdle(120, 2_000)
                assertEquals(quickAction, bounds(waitForNode("查看事项详情")))
                screenshot(output, "09-foreground-scrolled")
            }

            render("11-chat", Tab.CHAT, expect = "输入消息")
            tapFieldAndType("输入消息", "tickdesk_qa_draft")
            val keyboardVisible = onTestMainThread {
                ViewCompat.getRootWindowInsets(activity.window.decorView)?.isVisible(WindowInsetsCompat.Type.ime()) == true
            }
            if (keyboardVisible) pressBack()
            waitForNode("tickdesk_qa_draft")
            if (!compact) {
                click("立即扫描")
                waitForNode("请求工作台按当前档案扫描全市场")
                click("取消")
                waitForNode("tickdesk_qa_draft")
            }

            render("12-rich", Tab.CHAT, "rich", "client_chat", "结构化投研卡片")
            scrollTo("展开市值采样表")
            click("展开市值采样表")
            scrollTo("point_3")
            screenshot(output, "12-rich-attachment")

            render("13-import", Tab.PORTFOLIO, "import", expect = "原图保留与外部识别")
            scrollTo("提交对账确认", maxSwipes = 95)
            screenshot(output, "13-import-diff")

            val recovering = allScreens.copy(connection = ConnectionState(paired = true, recoveryRequired = true),
                canQueue = false, serviceRecoveryActive = true)
            render("recovery", Tab.SETTINGS, "recovery", expect = "恢复对账", state = recovering)
            scrollTo("等待读取当前恢复会话")
            scrollTo("刷新恢复状态")
            screenshot(output, "recovery-offline-controls")
        } finally {
            instrument.runOnMainSync { activity.finish() }
        }
    }

    private fun click(label: String, exact: Boolean = false) {
        val found = waitForNode(label, exact)
        assertTrue("$label should have an accessible click action",
            generateSequence(found) { it.parent }.any { it.performAction(AccessibilityNodeInfo.ACTION_CLICK) })
        automation.waitForIdle(120, 2_000)
    }

    private fun verifyControlsFixed(vararg labels: String) {
        val initial = labels.associateWith { bounds(waitForNode(it)) }
        val offset = bodyScroll.value
        scroll()
        instrument.waitForIdleSync()
        assertNotEquals("The body did not scroll", offset, bodyScroll.value)
        labels.forEach { label ->
            assertEquals("$label moved with scrolling body", initial.getValue(label), bounds(waitForNode(label)))
        }
    }

    private fun scrollTo(label: String, maxSwipes: Int = 70) {
        repeat(maxSwipes) {
            if (find(label) != null) return
            val root = automation.rootInActiveWindow
            if (root == null || descendants(root).none { it.isVisibleToUser && it.isScrollable }) {
                waitForNode(label)
                return
            }
            scroll(search = true)
            instrument.waitForIdleSync()
        }
        waitForNode(label)
    }

    private fun scroll(search: Boolean = false) {
        val root = automation.rootInActiveWindow ?: error("App window unavailable")
        val area = descendants(root).filter { it.isScrollable && it.isVisibleToUser }.map(::bounds)
            .maxByOrNull { it.width() * it.height() } ?: error("Expected a scrollable content area")
        val step = (area.height() / if (search) 5 else 3).coerceAtLeast(1)
        runBlocking { withContext(Dispatchers.Main) {
            bodyScroll.scrollTo((bodyScroll.value + step).coerceAtMost(bodyScroll.maxValue))
        } }
        automation.waitForIdle(80, 2_000)
    }

    private fun waitForNode(label: String, exact: Boolean = false): AccessibilityNodeInfo {
        val deadline = SystemClock.uptimeMillis() + 5_000L
        while (SystemClock.uptimeMillis() < deadline) {
            find(label, exact)?.let { return it }
            SystemClock.sleep(100)
        }
        val visible = automation.rootInActiveWindow?.let { descendants(it).mapNotNull { node -> node.text }.take(28).toList() }
        throw AssertionError("Expected visible '$label'. Found $visible")
    }

    private fun waitForGone(label: String, exact: Boolean = false) {
        val deadline = SystemClock.uptimeMillis() + 5_000L
        while (SystemClock.uptimeMillis() < deadline) {
            if (find(label, exact) == null) return
            SystemClock.sleep(100)
        }
        throw AssertionError("Expected '$label' to disappear after the selection changed")
    }

    private fun find(label: String, exact: Boolean = false): AccessibilityNodeInfo? {
        if (Build.VERSION.SDK_INT >= 34) automation.clearCache()
        fun matches(value: CharSequence?) = if (exact) value?.toString() == label else value?.contains(label) == true
        return automation.rootInActiveWindow?.let { root ->
            descendants(root).firstOrNull { it.isVisibleToUser &&
                (matches(it.text) || matches(it.contentDescription)) }
        }
    }

    private fun descendants(root: AccessibilityNodeInfo): Sequence<AccessibilityNodeInfo> = sequence {
        yield(root)
        for (index in 0 until root.childCount) {
            root.getChild(index)?.let { yieldAll(descendants(it)) }
        }
    }

    private fun bounds(node: AccessibilityNodeInfo) = Rect().also { node.getBoundsInScreen(it) }

    private fun tap(label: String) {
        val rect = bounds(waitForNode(label))
        val down = SystemClock.uptimeMillis()
        for (action in listOf(MotionEvent.ACTION_DOWN, MotionEvent.ACTION_UP)) {
            val event = MotionEvent.obtain(down, SystemClock.uptimeMillis(), action,
                rect.exactCenterX(), rect.exactCenterY(), 0)
            try {
                assertTrue("Could not tap $label", automation.injectInputEvent(event, true))
            } finally {
                event.recycle()
            }
        }
        automation.waitForIdle(180, 3_000)
    }

    private fun tapFieldAndType(label: String, text: String) {
        click(label)
        // Match OCR field editing so draft persistence does not depend on shell key injection.
        setText(waitForNode(label), text)
    }

    private fun setText(node: AccessibilityNodeInfo, text: String) {
        val arguments = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        }
        assertTrue("Expected an editable field", generateSequence(node) { it.parent }.any {
            it.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, arguments)
        })
        waitForNode(text)
    }

    private fun pressBack() {
        assertTrue(automation.performGlobalAction(android.accessibilityservice.AccessibilityService.GLOBAL_ACTION_BACK))
        automation.waitForIdle(120, 2_000)
    }

    private fun screenshot(out: File, id: String, differentFrom: String? = null) {
        val previous = differentFrom?.let { File(out, "$it.png").readBytes() }
        val path = File(out, "$id.png")
        repeat(4) {
            instrument.waitForIdleSync()
            SystemClock.sleep(250)
            val bitmap = checkNotNull(automation.takeScreenshot())
            try {
                path.outputStream().use { assertTrue(bitmap.compress(Bitmap.CompressFormat.PNG, 100, it)) }
            } finally {
                bitmap.recycle()
            }
            if (previous == null || !path.readBytes().contentEquals(previous)) return
        }
        path.delete()
        fail("Screenshot $id repeated the preceding screen ($differentFrom)")
    }

    private fun data(name: String) = cases.getValue(name).obj("data")!!
    private fun result(name: String) = data(name).obj("result")!!
    private fun JsonObject.with(vararg values: Pair<String, JsonElement>) = JsonObject(this + values.toMap())
    private fun str(value: String) = JsonPrimitive(value)
    private fun long(value: Long) = JsonPrimitive(value)
    private fun bool(value: Boolean) = JsonPrimitive(value)

    private fun projection(): WorkbenchProjection {
        val task = data("task").with(
            "createdAt" to long(now - 180_000), "dataAsOf" to long(now - 8_000),
            "validUntil" to long(now + 1_020_000), "nextReminderAt" to long(now + 160_000),
            "status" to str("reminded"), "remindedCount" to long(2), "direction" to str("sell"),
            "symbol" to str("600519.SH"), "name" to str("贵州茅台"),
            "stopPrice" to str("1488.00"), "levelReason" to str("止损卖出信号 · 跌破 1,488.00 元"),
            "reasons" to buildJsonObject {
                put("quant", "现价 1,482.60，跌破已记录止损位 1,488.00 元，5 分钟成交放量至均值 1.85 倍。")
                put("ai", "本次结论来自量化规则。外部信息未核验。")
                put("verificationStatus", "partial")
                put("riskEvidence", JsonArray(emptyList())); put("risks", JsonArray(listOf(str("价格波动"))))
                put("sources", JsonArray(listOf(buildJsonObject {
                    put("name", "契约测试行情"); put("ref", "fixture_quote"); put("asOf", now - 8_000)
                })))
            },
        )
        val second = task.with("taskId" to str("fixture_buy"), "name" to str("中际旭创"),
            "symbol" to str("300308.SZ"), "direction" to str("buy"), "level" to str("warn"), "remindedCount" to long(1),
            "levelReason" to str("右侧突破加仓 · 3 策略共振"), "status" to str("pending"),
            "reasons" to task.obj("reasons")!!.with("quant" to str("突破 20 日箱体上沿 126.80 元，量比 2.18，行业资金净流入。")))
        val third = task.with("taskId" to str("fixture_risk"), "name" to str("宁德时代"),
            "symbol" to str("300750.SZ"), "direction" to str("risk"), "level" to str("info"), "remindedCount" to long(1),
            "status" to str("pending"), "levelReason" to str("盘中振幅扩大 · 行业舆情核验中"),
            "reasons" to task.obj("reasons")!!.with("quant" to str("盘中成交波动扩大，行业公开信息尚待核验。")))
        val position = data("snapshot_page_1").objects("positions").first().with(
            "symbol" to str("600519.SH"), "name" to str("贵州茅台"), "priceAsOf" to long(now - 8_000),
            "qty" to long(100), "availableQty" to long(100), "lastPrice" to str("1482.60"),
            "cost" to str("1532.80"), "marketValue" to str("148260.00"), "unrealizedPnl" to str("-5020.00"),
            "unrealizedPnlRatio" to str("-0.03275"),
        )
        val snapshotMeta = data("snapshot_page_0").obj("meta")!!
        val meta = snapshotMeta.with(
            "visionNotice" to snapshotMeta.obj("visionNotice")!!.with("consentedAt" to long(now - 60_000)),
            "valuation" to snapshotMeta.obj("valuation")!!.with(
            "positionMarketValue" to str("148260.00"), "pricedSubtotal" to str("148260.00"),
            "unrealizedPnl" to str("-5020.00"), "asOf" to long(now - 8_000),
            "coveredCount" to long(1), "totalCount" to long(1), "unpricedSymbols" to JsonArray(emptyList()),
        ))
        val candidate = buildJsonObject {
            put("symbol", "300308.SZ"); put("name", "中际旭创"); put("score", "86")
            put("strategies", JsonArray(listOf(str("量价突破"), str("流动性过滤"), str("资金流入"))))
            put("changeRatio", "0.0426"); put("volumeRatio", "2.18"); put("marketCap", "20000000000")
            put("lastPrice", "128.50"); put("dataAsOf", now - 8_000); put("riskStatus", "none")
        }
        val candidates = result("ack_get_candidates").with("items" to JsonArray(listOf(candidate)))
        val samples = (0..3).map { index ->
            buildJsonObject {
                put("pointId", "point_$index"); put("tradeDate", "2026-11-1$index")
                put("asOf", now - (4 - index) * 86_400_000L); put("marketValue", (1070 + index * 30).toString())
                put("complete", true); put("missingSymbols", JsonArray(emptyList()))
                put("supersedesPointId", JsonNull); put("revision", index + 1); put("quantityChangeIds", JsonArray(emptyList()))
            }
        }
        val reply = data("reply").with("receivedAt" to long(now - 35_000), "attachments" to JsonArray(listOf(buildJsonObject {
            put("type", "valuation"); put("ids", JsonArray(samples.map { str(it.text("pointId")!!) }))
            put("symbol", JsonNull); put("scanId", JsonNull)
        })))
        val upload = data("import_cost_only_diff")
        return WorkbenchProjection(
            connection = ConnectionState(paired = true, relayOnline = true, localOnline = true, synced = true),
            pairId = "fixture_workbench", epoch = "epoch_demo_a", canQueue = true, synchronizedAt = now - 10_000,
            sequence = 42, meta = meta, records = mapOf("tasks" to listOf(task, second, third), "positions" to listOf(position),
                "valuationPoints" to samples, "imports" to listOf(upload)),
            reads = mapOf("get_candidates" to candidates, "get_status" to result("ack_get_status"),
                "list_files" to result("ack_list_files")),
            messages = result("ack_get_messages").objects("items"), replies = listOf(reply),
        )
    }
}

