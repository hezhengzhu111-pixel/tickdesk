mod support;
use serde_json::{json, Value};
use support::{command, deliver, LOCAL_PAIR, PEER};
use tickdesk_local::storage::Storage;

fn run(db: &mut Storage, key: &str, cmd: &str, fields: Value, now: u64) -> Value {
    let input = command(db, key, key, cmd, fields);
    db.receive(&deliver(db, PEER, key, &input)).unwrap();
    db.process(db.job(key).unwrap().unwrap(), now)
        .unwrap()
        .unwrap()
}

#[test]
fn absent_market_input_is_explicit_and_failed_scan_is_latest_history() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let before = run(
        &mut db,
        "before",
        "get_candidates",
        json!({}),
        1794792300000,
    );
    assert_eq!(before["data"]["ok"], true);
    assert!(before["data"]["result"]["scan"].is_null());
    let scan = run(&mut db, "scan", "scan_now", json!({}), 1794792300000);
    assert_eq!(scan["data"]["ok"], false);
    assert!(scan["data"]["result"]["scanId"].is_string());
    let latest = run(
        &mut db,
        "latest",
        "get_candidates",
        json!({}),
        1794792300001,
    );
    assert_eq!(latest["data"]["result"]["scan"], scan["data"]["result"]);
    assert_eq!(latest["data"]["result"]["items"], json!([]));
    let chart = run(
        &mut db,
        "chart",
        "get_chart",
        json!({"symbol":"300502.SZ","adjustment":"none"}),
        1794792300001,
    );
    assert_eq!(chart["data"]["result"]["dataStatus"], "missing");
    assert_eq!(chart["data"]["result"]["candles"], json!([]));
}

fn prepared_run(
    db: &mut Storage,
    key: &str,
    cmd: &str,
    fields: Value,
    response: Value,
    now: u64,
) -> Value {
    let input = command(db, key, key, cmd, fields);
    let request = tickdesk_local::market::capture(db, &input["data"], now).unwrap();
    db.receive(&deliver(db, PEER, key, &input)).unwrap();
    db.process_prepared(
        db.job(key).unwrap().unwrap(),
        now,
        Some(json!({"request":request,"response":response})),
    )
    .unwrap()
    .unwrap()
}

#[test]
fn chart_pages_keep_the_requested_period_and_reject_wrong_period_data() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794816000000;
    let fields = json!({"symbol":"300502.SZ","timeframe":"1w","adjustment":"none","beforeDate":"2026-11-13","limit":1});
    let mut chart = json!({"symbol":"300502.SZ","timeframe":"1w","adjustment":"none","asOf":1793948400000u64,
        "dataStatus":"fresh","candles":[{"date":"2026-11-06","open":"11","high":"13","low":"10",
        "close":"12","volume":1000,"amount":null}],"nextBeforeDate":"2026-11-06","hasMore":true,
        "source":{"name":"fixture","ref":"test:closed-week","asOf":now}});
    let page = prepared_run(
        &mut db,
        "week",
        "get_chart",
        fields.clone(),
        json!({"chart":chart}),
        now,
    );
    assert_eq!(page["data"]["result"], chart);
    chart["timeframe"] = json!("1mo");
    let rejected = prepared_run(
        &mut db,
        "wrong-period",
        "get_chart",
        fields.clone(),
        json!({"chart":chart}),
        now,
    );
    assert_eq!(rejected["data"]["error"]["code"], "DATA_UNAVAILABLE");
    let missing = run(
        &mut db,
        "month",
        "get_chart",
        json!({"symbol":"300502.SZ","timeframe":"1mo","adjustment":"none"}),
        now,
    );
    assert_eq!(missing["data"]["result"]["timeframe"], "1mo");
    assert_eq!(missing["data"]["result"]["dataStatus"], "missing");
}

#[test]
fn minute_chart_requires_an_opaque_cursor_and_ordered_complete_bounds() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794816000000;
    let fields = json!({"symbol":"300502.SZ","timeframe":"1m","adjustment":"none","limit":2});
    let mut chart = json!({"symbol":"300502.SZ","timeframe":"1m","adjustment":"none","asOf":now - 60_000,
        "dataStatus":"fresh","candles":[
            {"date":"2026-11-16","open":"11","high":"13","low":"10","close":"12","volume":1000,"amount":null,
             "startAt":now - 180_000,"endMs":now - 120_000,"availableAtMs":now - 120_000,"complete":true,"interval":"1m"},
            {"date":"2026-11-16","open":"12","high":"14","low":"11","close":"13","volume":900,"amount":null,
             "startAt":now - 120_000,"endMs":now - 60_000,"availableAtMs":now - 60_000,"complete":true,"interval":"1m"}],
        "nextBeforeDate":null,"hasMore":true,"source":{"name":"local-minute-store","ref":"kernel:minute:300502.SZ:1m","asOf":now - 60_000},
        "nextCursor":"v2:2:2:fixture"});
    let page = prepared_run(
        &mut db,
        "minute",
        "get_chart",
        fields.clone(),
        json!({"chart":chart}),
        now,
    );
    assert_eq!(page["data"]["result"], chart);
    // `hasMore=false` may not keep a next cursor.
    chart["hasMore"] = json!(false);
    let rejected = prepared_run(
        &mut db,
        "minute-unfinished-page",
        "get_chart",
        fields.clone(),
        json!({"chart":chart}),
        now,
    );
    assert_eq!(rejected["data"]["error"]["code"], "DATA_UNAVAILABLE");
    chart["hasMore"] = json!(true);
    chart["candles"][1]["startAt"] = chart["candles"][0]["startAt"].clone();
    let unordered = prepared_run(
        &mut db,
        "minute-unordered",
        "get_chart",
        fields,
        json!({"chart":chart}),
        now,
    );
    assert_eq!(
        unordered["data"]["error"]["code"], "DATA_UNAVAILABLE",
        "{unordered}"
    );
    let expired = prepared_run(
        &mut db,
        "minute-cursor-expired",
        "get_chart",
        json!({"symbol":"300502.SZ","timeframe":"1m","adjustment":"none","limit":2,
            "cursor":"v2:2:2:stale"}),
        json!({"error":"CHART_CURSOR_EXPIRED"}),
        now,
    );
    assert_eq!(
        expired["data"]["error"]["code"], "CHART_CURSOR_EXPIRED",
        "{expired}"
    );
}

#[test]
fn incomplete_minute_candle_without_available_time_is_forwarded_without_panic() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794816000000;
    let fields = json!({"symbol":"300502.SZ","timeframe":"1m","adjustment":"none","limit":2});
    let chart = json!({"symbol":"300502.SZ","timeframe":"1m","adjustment":"none","asOf":null,
        "dataStatus":"partial","candles":[
            {"date":"2026-11-16","open":"11","high":"13","low":"10","close":"12","volume":1000,"amount":null,
             "startAt":now,"endMs":now + 60_000,"availableAtMs":null,"complete":false,"interval":"1m"}],
        "nextBeforeDate":null,"hasMore":false,"source":null,"nextCursor":null});
    let page = prepared_run(
        &mut db,
        "minute-incomplete",
        "get_chart",
        fields,
        json!({"chart":chart}),
        now,
    );
    assert_eq!(page["data"]["result"], chart);
    assert_eq!(
        page["data"]["result"]["candles"][0]["availableAtMs"],
        serde_json::Value::Null
    );
}

#[test]
fn minute_chart_rejects_a_future_start_and_forwards_a_missing_first_gap() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794816000000;
    let fields = json!({"symbol":"300502.SZ","timeframe":"5m","adjustment":"none","limit":2});
    let mut chart = json!({"symbol":"300502.SZ","timeframe":"5m","adjustment":"none","asOf":null,
        "dataStatus":"partial","candles":[
            {"date":"2026-11-16","open":"11","high":"13","low":"10","close":"12","volume":1000,"amount":null,
             "startAt":now + 300_000,"endMs":now + 600_000,"availableAtMs":null,"complete":false,"interval":"5m"}],
        "gaps":[{"startAt":now - 300_000,"endMs":now,"quality":"missing_first_minute",
            "missingMinutes":[now - 300_000],"observedMinutes":4}],
        "nextBeforeDate":null,"hasMore":false,"source":null,"nextCursor":null});
    let rejected = prepared_run(
        &mut db,
        "minute-future",
        "get_chart",
        fields.clone(),
        json!({"chart":chart}),
        now,
    );
    assert_eq!(rejected["data"]["error"]["code"], "DATA_UNAVAILABLE");
    chart["candles"] = json!([]);
    chart["gaps"][0]["startAt"] = json!(now - 300_000);
    chart["gaps"][0]["endMs"] = json!(now);
    let forwarded = prepared_run(
        &mut db,
        "minute-gap",
        "get_chart",
        fields,
        json!({"chart":chart}),
        now,
    );
    assert_eq!(
        forwarded["data"]["result"]["gaps"][0]["quality"],
        "missing_first_minute"
    );
    assert_eq!(forwarded["data"]["result"]["gaps"][0]["observedMinutes"], 4);
}

#[test]
fn closed_snapshot_and_rate_limit_are_visible_without_inventing_a_trade_clock() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794816000000u64;
    let symbol = "600000.SH";
    let fields = json!({"symbols":[symbol,"510300.SH"]});
    let response = json!({"quoteFeed":{"session":"closed","delivery":{symbol:"available","510300.SH":"rate_limited"}},
        "quotes":[{"symbol":symbol,"price":"10.78","asOf":now-100,"timeBasis":"snapshot",
            "source":{"name":"fixture","ref":"test:sample","asOf":now-100}}]});
    let ack = prepared_run(
        &mut db,
        "closed",
        "get_quote_status",
        fields.clone(),
        response,
        now,
    );
    let result = &ack["data"]["result"];
    assert_eq!(result["session"], "closed");
    assert_eq!(result["items"][0]["quality"], "stale");
    assert_eq!(result["items"][0]["timeBasis"], "snapshot");
    assert_eq!(result["items"][0]["asOf"], now - 100);
    assert_eq!(result["items"][1]["delivery"], "rate_limited");
    assert_eq!(result["items"][1]["quality"], "missing");
    let offline = run(&mut db, "offline", "get_quote_status", fields, now + 1);
    assert_eq!(offline["data"]["result"]["items"][0]["price"], "10.78");
    assert_eq!(offline["data"]["result"]["items"][0]["delivery"], "offline");
}

#[test]
fn fresh_batch_reference_cannot_price_the_ledger_and_old_feed_does_not_become_current() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794805230000u64; // 13:00:30 in Shanghai.
    let symbol = "600000.SH";
    let adjusted = run(
        &mut db,
        "holding",
        "adjust_position",
        json!({"symbol":symbol,"qty":100,"availableQty":100,
        "cost":"10","pendingLots":[],"asOf":now-5000,"basePositionRevision":0,"reason":"fixture","resolvedExecutionIds":[]}),
        now - 2000,
    );
    assert_eq!(adjusted["data"]["ok"], true);
    let fields = json!({"symbols":[symbol]});
    let response = |source: &str, at: u64| {
        json!({
            "quoteFeed":{"session":"open","delivery":{symbol:"available"}},
            "quotes":[{"symbol":symbol,"price":"10.78","asOf":at,"timeBasis":"snapshot",
                "source":{"name":source,"ref":"test:snapshot","asOf":at}}]
        })
    };
    let lunch = prepared_run(
        &mut db,
        "before-session",
        "get_quote_status",
        fields.clone(),
        response("older", now - 40_000),
        now,
    );
    assert_eq!(lunch["data"]["result"]["items"][0]["quality"], "stale");
    let current = prepared_run(
        &mut db,
        "same-session",
        "get_quote_status",
        fields.clone(),
        response("older", now - 1000),
        now,
    );
    assert_eq!(current["data"]["result"]["items"][0]["quality"], "fresh");
    let switched = prepared_run(
        &mut db,
        "new-feed",
        "get_quote_status",
        fields,
        response("new", now - 1200),
        now,
    );
    assert_eq!(
        switched["data"]["result"]["items"][0]["source"]["name"],
        "older"
    );
    assert_eq!(switched["data"]["result"]["items"][0]["quality"], "stale");
    let sync = run(&mut db, "portfolio", "sync", json!({}), now);
    let page = db
        .outbox(64)
        .unwrap()
        .into_iter()
        .find(|frame| frame["payload"]["id"] == sync["data"]["result"]["pageEventId"])
        .unwrap();
    assert_eq!(
        page["payload"]["data"]["positions"][0]["priceStatus"],
        "stale"
    );
    assert!(page["payload"]["data"]["meta"]["valuation"]["positionMarketValue"].is_null());
}

fn frozen(now: u64) -> Value {
    json!({"frozen":{"tradeDate":"2026-11-16","isTradingDay":true,"generation":"generation-a","generationAfter":"generation-a","asOf":now,
        "strategies":{"total":3,"succeeded":3,"failed":[]},"policy":{"minScore":"60"},
        "candidates":[{"candidate":{"symbol":"300502.SZ","name":"fixture stock","score":"88.123456789123456789","strategies":["a","b","c"],"changeRatio":null,"volumeRatio":null,"marketCap":null,"lastPrice":"12.25","dataAsOf":now,"riskStatus":"unknown"},
        "direction":"buy","approved":true,"supported":true,"liquidityPassed":true}]}})
}

fn v3_frozen(now: u64, exit: bool, buy: bool) -> Value {
    let mut specs = vec![
        json!({"id":"s","direction":"sell","symbols":["300502.SZ"],"weight":"1",
        "group":"daily_swing","codeHash":"a".repeat(64),"parametersHash":"b".repeat(64),
        "dependenciesHash":"c".repeat(64)}),
    ];
    let mut outcomes = json!({"s":{"status":if exit {"matched"} else {"no_match"},"reasons":[]}});
    let mut candidates = Vec::new();
    if buy {
        for id in ["b1", "b2", "b3"] {
            specs.push(
                json!({"id":id,"direction":"buy","symbols":["300502.SZ"],"weight":"1",
                "group":"daily_swing","codeHash":"d".repeat(64),"parametersHash":"e".repeat(64),
                "dependenciesHash":"f".repeat(64)}),
            );
            outcomes[id] = json!({"status":"matched","reasons":[]});
        }
        candidates.push(
            json!({"candidate":{"symbol":"300502.SZ","name":"fixture stock",
            "score":"88","strategies":["b1","b2","b3"],"changeRatio":null,"volumeRatio":null,
            "marketCap":null,"lastPrice":"12.25","dataAsOf":now,"riskStatus":"unknown"},
            "direction":"buy","approved":true,"supported":true,"liquidityPassed":true}),
        );
    }
    let exits = if exit {
        json!([{"symbol":"300502.SZ","name":"fixture stock","strategyId":"s","strategyIds":["s"],
            "ruleId":"matrix_daily_exit","ruleVersion":1,"condition":"已批准日线策略的独立退出条件成立",
            "decisionClock":"completed_daily_bar","barEndMs":now-60_000,"barAvailableAtMs":now-30_000,
            "quote":{"price":"12.25","asOf":now,"source":{"name":"fixture","ref":"fixture:quote","asOf":now}}}])
    } else {
        json!([])
    };
    json!({"frozen":{"tradeDate":"2026-11-16","isTradingDay":true,"generation":"generation-v3",
        "generationAfter":"generation-v3","asOf":now,
        "strategies":{"total":specs.len(),"succeeded":specs.len(),"failed":[]},
        "policy":{"minScore":"0","minimumAmount":"1"},"candidates":candidates,"exits":exits,
        "manifest":{"schemaVersion":3,"inputs":{},"approvalHash":"a".repeat(64),
            "parametersHash":"b".repeat(64),"strategyHashes":specs,
            "strategyOutcomes":outcomes,"researchDecisions":[]}}})
}

fn observation_batch(now: u64) -> Value {
    json!({"version":3,"decisionCutoffMs":now,"observations":[{
        "symbol":"300502.SZ","strategyId":"s","group":"daily_swing","timeframe":"1d",
        "codeHash":"a".repeat(64),"parametersHash":"b".repeat(64),"dependenciesHash":"c".repeat(64),
        "inputHash":"d".repeat(64),"rulesHash":null,"admission":"research",
        "bar":{"status":"complete","endMs":now-1000,"availableAtMs":now-500},
        "entry":{"value":null,"reason":"missing_fundamental"},"exit":{"value":true,"reason":null},
        "score":{"value":"0","reason":null,"scope":"eligible_universe"},"quote":null,"evidence":{}
    }]})
}

#[cfg(not(feature = "integration-fixtures"))]
#[tokio::test]
async fn closed_scan_and_resume_are_rejected_before_kernel_preparation() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let closed_at = 1794818718480;
    for (key, cmd, at) in [
        ("after-close-scan", "scan_now", closed_at),
        ("after-close-resume", "resume", closed_at + 2),
    ] {
        if cmd == "resume" {
            run(&mut db, "pause-before-resume", "pause", json!({}), at - 1);
        }
        let input = command(&db, key, key, cmd, json!({}));
        let request = tickdesk_local::market::capture(&db, &input["data"], at).unwrap();
        let prepared = tickdesk_local::market::prepare(None, request).await;
        assert_eq!(prepared["response"], json!({"error":"SCAN_OUTSIDE_WINDOW"}));
        db.receive(&deliver(&db, PEER, key, &input)).unwrap();
        let result = db
            .process_prepared(db.job(key).unwrap().unwrap(), at, Some(prepared))
            .unwrap()
            .unwrap();
        if cmd == "scan_now" {
            assert_eq!(result["data"]["error"]["code"], "SCAN_OUTSIDE_WINDOW");
        } else {
            assert_eq!(result["data"]["result"]["paused"], false);
            assert_eq!(
                result["data"]["result"]["scan"]["code"],
                "SCAN_OUTSIDE_WINDOW"
            );
        }
    }
    let open_request = tickdesk_local::market::capture(
        &db,
        &json!({"cmd":"scan_now"}),
        1794794400000 + 24 * 60 * 60_000,
    )
    .unwrap();
    let normal = tickdesk_local::market::prepare(None, open_request).await;
    assert_eq!(
        normal["response"],
        json!({"error":"DATA_UNAVAILABLE","prepareReason":"KERNEL_PREPARE_NOT_CONFIGURED"})
    );
}

#[test]
fn chat_freezes_published_quant_and_never_uses_a_previous_success_for_a_failed_round() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794794400000;
    let scan = prepared_run(
        &mut db,
        "formal-scan",
        "scan_now",
        json!({}),
        frozen(now),
        now,
    );
    assert_eq!(scan["data"]["ok"], true);
    let task = &scan["data"]["result"]["publishedTaskIds"][0];
    assert!(task.is_string());
    run(
        &mut db,
        "formal-report",
        "chat",
        json!({"text":"/分析 300502.SZ"}),
        now + 1,
    );
    let job = db.claim_chat(now + 1).unwrap().unwrap();
    let production = &job.evidence["quant"]["production"];
    assert_eq!(production["scanId"], scan["data"]["result"]["scanId"]);
    assert_eq!(production["status"], "succeeded");
    assert_eq!(production["candidates"][0]["taskId"], *task);
    assert_eq!(production["candidates"][0]["symbol"], "300502.SZ");
    assert_eq!(production["candidates"][0]["direction"], "buy");
    assert_eq!(
        production["candidates"][0]["score"],
        "88.123456789123456789"
    );
    assert!(job.evidence["quant"]["batch"].is_null());
    db.finish_chat(
        &job,
        Ok(json!({"text":"隔离验证","sourceRefs":[]}).to_string()),
        now + 2,
    )
    .unwrap()
    .unwrap();

    prepared_run(
        &mut db,
        "formal-failed",
        "scan_now",
        json!({}),
        json!({"error":"DATA_UNAVAILABLE","productionReason":"NO_PRODUCTION_APPROVAL"}),
        now + 300_000,
    );
    run(
        &mut db,
        "failed-report",
        "chat",
        json!({"text":"/分析 300502.SZ"}),
        now + 300_001,
    );
    let later = db.claim_chat(now + 300_001).unwrap().unwrap();
    assert_eq!(later.evidence["quant"]["production"]["status"], "failed");
    assert_eq!(
        later.evidence["quant"]["production"]["reason"],
        "NO_PRODUCTION_APPROVAL"
    );
    assert_eq!(
        later.evidence["quant"]["production"]["candidates"],
        json!([])
    );
}

#[test]
#[ignore = "requires the Python assistant environment for the exact Rust-to-Python report payload"]
fn published_quant_reaches_report_body_with_clickable_task_and_archived_score() {
    use std::{
        io::Write,
        process::{Command, Stdio},
    };

    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794794400000;
    let scan = prepared_run(
        &mut db,
        "combined-scan",
        "scan_now",
        json!({}),
        frozen(now),
        now,
    );
    assert_eq!(scan["data"]["ok"], true);
    let task = scan["data"]["result"]["publishedTaskIds"][0]
        .as_str()
        .unwrap();
    let scan_id = scan["data"]["result"]["scanId"].as_str().unwrap();
    let reference = format!("local:scan:{scan_id}:task:{task}");
    run(
        &mut db,
        "combined-report",
        "chat",
        json!({"text":"/分析 300502.SZ"}),
        now + 1,
    );
    let job = db.claim_chat(now + 1).unwrap().unwrap();
    let connection = rusqlite::Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();
    connection
        .execute(
            "UPDATE chat_jobs SET prompt_version='tickdesk-assistant-v1' WHERE job_id=?1",
            [&job.job_id],
        )
        .unwrap();
    let workspace = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap();
    let mut child =
        Command::new(workspace.join("tick-stock-panel/backend/.venv/Scripts/python.exe"))
            .arg("-X")
            .arg("utf8")
            .arg(
                workspace
                    .join(".superpowers/full-business-closure/render_published_quant_report.py"),
            )
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
    child.stdin.take().unwrap().write_all(json!({
        "version":1,"jobId":job.job_id,"requestId":job.request_id,"deadline":job.deadline,"text":job.text,
        "evidence":job.evidence,
    }).to_string().as_bytes()).unwrap();
    let output = child.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let raw = String::from_utf8(output.stdout).unwrap();
    let reply = db.finish_chat(&job, Ok(raw), now + 2).unwrap().unwrap();
    assert_eq!(reply["data"]["status"], "completed");
    assert_eq!(reply["data"]["sources"][0]["ref"], reference);
    assert_eq!(reply["data"]["attachments"][0]["ids"], json!([task]));
    let report = run(
        &mut db,
        "combined-detail",
        "get_report",
        json!({"reportId":job.request_id}),
        now + 3,
    );
    let detail = &report["data"]["result"];
    assert_eq!(detail["header"]["coverage"]["quant"], "available");
    assert_eq!(detail["text"], reply["data"]["text"]);
    assert!(detail["text"].as_str().unwrap().contains("正式买入候选"));
    assert!(detail["text"]
        .as_str()
        .unwrap()
        .contains("88.123456789123456789"));
    let saved: String = connection
        .query_row(
            "SELECT raw_output FROM chat_jobs WHERE job_id=?1",
            [&job.job_id],
            |row| row.get(0),
        )
        .unwrap();
    let artifact: Value = serde_json::from_str(&saved).unwrap();
    assert_eq!(
        artifact["artifact"]["report"]["productionQuant"]["scanId"],
        scan_id
    );
    assert_eq!(
        artifact["artifact"]["report"]["productionQuant"]["candidates"][0]["score"],
        "88.123456789123456789"
    );

    prepared_run(
        &mut db,
        "combined-failure",
        "scan_now",
        json!({}),
        json!({"error":"DATA_UNAVAILABLE","productionReason":"NO_PRODUCTION_APPROVAL"}),
        now + 300_000,
    );
    let old = run(
        &mut db,
        "combined-history",
        "get_report",
        json!({"reportId":job.request_id}),
        now + 300_001,
    );
    assert_eq!(old["data"]["result"]["header"]["state"], "historical");
    assert_eq!(old["data"]["result"]["text"], detail["text"]);
    println!(
        "{}",
        json!({"capture":"isolated published candidate","body":detail["text"],
        "source":detail["sources"][0],"attachedTask":task,"archivedMatches":true,"lostApprovalKeepsHistorical":true})
    );
}

#[test]
fn chat_keeps_frozen_observations_through_a_preflight_close_but_not_data_changes() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let market_open = 1794794400000;
    let market_closed = market_open + 2 * 60 * 60_000;
    let batch = observation_batch(market_open);
    let first = prepared_run(
        &mut db,
        "first-observations",
        "scan_now",
        json!({}),
        json!({"error":"DATA_UNAVAILABLE","quantObservationBatch":batch}),
        market_open,
    );
    let original = &first["data"]["result"]["scanId"];
    let connection = rusqlite::Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();
    // Simulate an upgrade from a workspace with captured observations before
    // the separate analysis pointer was introduced.
    connection
        .execute(
            "DELETE FROM objects WHERE collection='market_meta' AND object_id='latest_analysis_scan'",
            [],
        )
        .unwrap();
    let capture = |db: &mut Storage, key, at| {
        assert_eq!(
            run(db, key, "chat", json!({"text":"300502.SZ"}), at)["data"]["ok"],
            true
        );
        let job = db.claim_chat(at).unwrap().unwrap();
        connection
            .execute(
                "UPDATE chat_jobs SET prompt_version='tickdesk-assistant-v1' WHERE job_id=?",
                [&job.job_id],
            )
            .unwrap();
        job
    };
    let raw = json!({"text":"隔离验证","sourceRefs":[]}).to_string();
    let inflight = capture(&mut db, "before-rejection", market_closed - 2);
    assert_eq!(inflight.evidence["quant"]["scanId"], *original);
    let rejected = run(
        &mut db,
        "closed-attempt",
        "scan_now",
        json!({}),
        market_closed,
    );
    assert_eq!(rejected["data"]["error"]["code"], "SCAN_OUTSIDE_WINDOW");
    assert_eq!(rejected["data"]["result"]["status"], "rejected");
    let candidates = run(
        &mut db,
        "latest-candidates",
        "get_candidates",
        json!({}),
        market_closed + 1,
    );
    assert_eq!(
        candidates["data"]["result"]["scan"],
        rejected["data"]["result"]
    );
    let public_quant = run(
        &mut db,
        "latest-quant",
        "get_quant_observations",
        json!({}),
        market_closed + 2,
    );
    assert_eq!(
        public_quant["data"]["result"]["scanId"],
        rejected["data"]["result"]["scanId"]
    );
    assert_eq!(public_quant["data"]["result"]["status"], "missing");
    let completed = db
        .finish_chat(&inflight, Ok(raw.clone()), market_closed + 3)
        .unwrap()
        .unwrap();
    assert_eq!(completed["data"]["status"], "completed");

    let revised = capture(&mut db, "before-material-update", market_closed + 4);
    assert_eq!(revised.evidence["quant"]["scanId"], *original);
    let mut new_batch = observation_batch(market_closed + 5);
    new_batch["observations"][0]["score"]["value"] = json!("1");
    let updated = prepared_run(
        &mut db,
        "revised-observations",
        "scan_now",
        json!({}),
        json!({"error":"SCAN_OUTSIDE_WINDOW","quantObservationBatch":new_batch}),
        market_closed + 5,
    );
    assert_eq!(updated["data"]["error"]["code"], "SCAN_OUTSIDE_WINDOW");
    let stale = db
        .finish_chat(&revised, Ok(raw.clone()), market_closed + 6)
        .unwrap()
        .unwrap();
    assert_eq!(stale["data"]["errorCode"], "AI_STALE_CONTEXT");

    run(
        &mut db,
        "pause-chat-quant",
        "pause",
        json!({}),
        market_closed + 7,
    );
    let paused = capture(&mut db, "while-paused", market_closed + 8);
    let skipped = run(
        &mut db,
        "paused-attempt",
        "scan_now",
        json!({}),
        market_closed + 9,
    );
    assert_eq!(skipped["data"]["error"]["code"], "SCAN_PAUSED");
    let answered = db
        .finish_chat(&paused, Ok(raw.clone()), market_closed + 10)
        .unwrap()
        .unwrap();
    assert_eq!(answered["data"]["status"], "completed");
    let resume = run(
        &mut db,
        "resume-chat-quant",
        "resume",
        json!({}),
        market_closed + 11,
    );
    assert_eq!(resume["data"]["result"]["paused"], false);

    let profile_check = capture(&mut db, "before-profile-change", market_closed + 12);
    run(
        &mut db,
        "switch-profile",
        "set_profile",
        json!({"profile":"aggressive"}),
        market_closed + 13,
    );
    let stale = db
        .finish_chat(&profile_check, Ok(raw.clone()), market_closed + 14)
        .unwrap()
        .unwrap();
    assert_eq!(stale["data"]["errorCode"], "AI_STALE_CONTEXT");

    let in_window_again = market_closed + 60 * 60_000;
    let failed = capture(&mut db, "before-data-failure", in_window_again - 1);
    assert_eq!(
        failed.evidence["quant"]["scanId"],
        updated["data"]["result"]["scanId"]
    );
    prepared_run(
        &mut db,
        "unavailable-observations",
        "scan_now",
        json!({}),
        json!({"error":"DATA_UNAVAILABLE","quantObservationError":"DATA_UNAVAILABLE"}),
        in_window_again,
    );
    let stale = db
        .finish_chat(&failed, Ok(raw), in_window_again + 1)
        .unwrap()
        .unwrap();
    assert_eq!(stale["data"]["errorCode"], "AI_STALE_CONTEXT");
    // The real data failure must clear analysis observations even for a
    // later preflight rejection.
    run(
        &mut db,
        "later-closed",
        "scan_now",
        json!({}),
        market_closed + 4 * 60 * 60_000,
    );
    let later = capture(
        &mut db,
        "after-data-failure",
        market_closed + 4 * 60 * 60_000 + 1,
    );
    assert_eq!(later.evidence["quant"]["status"], "failed");
    assert!(later.evidence["quant"]["batch"].is_null());
}

#[test]
fn quant_read_is_bounded_frozen_and_never_reuses_a_previous_scan() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794794400000;
    let missing = run(&mut db, "q-empty", "get_quant_observations", json!({}), now);
    assert_eq!(missing["data"]["result"]["status"], "missing");
    assert!(missing["data"]["result"]["scanId"].is_null());
    let mut batch = observation_batch(now);
    let mut second = batch["observations"][0].clone();
    second["strategyId"] = json!("second-strategy");
    batch["observations"].as_array_mut().unwrap().push(second);
    let scan = prepared_run(
        &mut db,
        "q-scan",
        "scan_now",
        json!({}),
        json!({"error":"DATA_UNAVAILABLE","quantObservationBatch":batch}),
        now,
    );
    let scan_id = scan["data"]["result"]["scanId"].clone();
    let first = run(
        &mut db,
        "q-first",
        "get_quant_observations",
        json!({"limit":1}),
        now + 1,
    );
    let page = &first["data"]["result"];
    assert_eq!(page["scanId"], scan_id);
    assert_eq!(page["status"], "available");
    assert_eq!(page["observations"].as_array().unwrap().len(), 1);
    assert_eq!(page["observations"][0]["exit"]["value"], true);
    assert!(page["observations"][0].get("evidence").is_none());
    let cursor = page["nextCursor"].clone();
    let failed = run(&mut db, "q-new", "scan_now", json!({}), now + 2);
    let next = run(
        &mut db,
        "q-next",
        "get_quant_observations",
        json!({"cursor":cursor,"limit":1}),
        now + 3,
    );
    assert_eq!(next["data"]["result"]["scanId"], scan_id);
    assert_eq!(
        next["data"]["result"]["observations"][0]["strategyId"],
        "second-strategy"
    );
    assert_eq!(next["data"]["result"]["hasMore"], false);
    let latest = run(
        &mut db,
        "q-latest",
        "get_quant_observations",
        json!({}),
        now + 4,
    );
    assert_eq!(
        latest["data"]["result"]["scanId"],
        failed["data"]["result"]["scanId"]
    );
    assert_eq!(latest["data"]["result"]["status"], "missing");
    assert_eq!(latest["data"]["result"]["observations"], json!([]));
    let wrong_query = run(
        &mut db,
        "q-wrong",
        "get_quant_observations",
        json!({"cursor":cursor,"scanId":scan_id}),
        now + 5,
    );
    assert_eq!(wrong_query["data"]["error"]["code"], "INVALID_PARAMS");
    let expired = run(
        &mut db,
        "q-expired",
        "get_quant_observations",
        json!({"cursor":cursor}),
        now + 600_001,
    );
    assert_eq!(expired["data"]["error"]["code"], "SNAPSHOT_EXPIRED");
}

#[test]
fn quant_read_reports_failure_and_isolates_restored_epoch() {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("source");
    let mut db = Storage::open(&source).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794794400000;
    let first = prepared_run(
        &mut db,
        "qr-first",
        "scan_now",
        json!({}),
        json!({"quantObservationBatch":observation_batch(now)}),
        now,
    );
    let scan_id = first["data"]["result"]["scanId"].clone();
    let failed = prepared_run(
        &mut db,
        "qr-failed",
        "scan_now",
        json!({}),
        json!({"quantObservationError":"DATA_UNAVAILABLE"}),
        now + 1,
    );
    let read = run(
        &mut db,
        "qr-read",
        "get_quant_observations",
        json!({}),
        now + 2,
    );
    assert_eq!(
        read["data"]["result"]["scanId"],
        failed["data"]["result"]["scanId"]
    );
    assert_eq!(read["data"]["result"]["status"], "failed");
    assert_eq!(read["data"]["result"]["error"], "DATA_UNAVAILABLE");
    let history = run(
        &mut db,
        "qr-history",
        "get_quant_observations",
        json!({"scanId":scan_id}),
        now + 3,
    );
    assert_eq!(history["data"]["result"]["historical"], true);
    assert_eq!(history["data"]["result"]["status"], "available");
    let backup = dir.path().join("backup");
    db.backup_to(&backup).unwrap();
    drop(db);
    let restored = dir.path().join("restored");
    Storage::restore_from(&backup, &restored).unwrap();
    let mut db = Storage::open(&restored).unwrap();
    let isolated = run(
        &mut db,
        "qr-restored",
        "get_quant_observations",
        json!({"scanId":scan_id}),
        now + 4,
    );
    assert_eq!(isolated["data"]["ok"], true, "{isolated}");
    assert_eq!(isolated["data"]["result"]["status"], "missing");
    assert_eq!(isolated["data"]["result"]["observations"], json!([]));
}

async fn observations(db: Storage, scan_id: &str) -> (axum::http::StatusCode, Value) {
    use http_body_util::BodyExt;
    use std::sync::{Arc, Mutex};
    use tower::ServiceExt;

    let response = tickdesk_local::http::router_with_storage(Arc::new(Mutex::new(db)))
        .oneshot(
            axum::http::Request::builder()
                .uri(format!("/quant/observations/{scan_id}"))
                .body(axum::body::Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let status = response.status();
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

#[tokio::test]
async fn quant_scan_persists_independent_verdicts_across_restart() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794794400000;
    let batch = observation_batch(now);
    let ack = prepared_run(
        &mut db,
        "quant",
        "scan_now",
        json!({}),
        json!({"error":"DATA_UNAVAILABLE","quantObservationBatch":batch}),
        now,
    );
    let scan = &ack["data"]["result"];
    assert_eq!(scan["publishedTaskIds"], json!([]));
    assert_eq!(ack["data"]["error"]["code"], "DATA_UNAVAILABLE");
    assert!(db
        .outbox(16)
        .unwrap()
        .iter()
        .all(|e| e["payload"]["kind"] != "reminder"));
    let epoch = db.identity.server_epoch.clone();
    drop(db);

    let db = Storage::open(dir.path()).unwrap();
    let scan_id = scan["scanId"].as_str().unwrap();
    let (status, result) = observations(db, scan_id).await;
    assert_eq!(status, axum::http::StatusCode::OK);
    assert_eq!(result["scanId"], scan_id);
    assert_eq!(result["serverEpoch"], epoch);
    assert_eq!(result["batch"], batch);
}

#[tokio::test]
async fn quant_scan_rejects_future_input_and_checks_pause_at_commit() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794794400000;
    let mut future = observation_batch(now);
    future["observations"][0]["bar"]["availableAtMs"] = json!(now + 1);
    let mut reply = frozen(now);
    reply["quantObservationBatch"] = future;
    let legacy = prepared_run(&mut db, "future", "scan_now", json!({}), reply, now);
    assert_eq!(legacy["data"]["ok"], true);
    assert_eq!(
        legacy["data"]["result"]["publishedTaskIds"]
            .as_array()
            .unwrap()
            .len(),
        1
    );

    let input = command(&db, "paused-quant", "paused-quant", "scan_now", json!({}));
    let request = tickdesk_local::market::capture(&db, &input["data"], now + 1).unwrap();
    db.receive(&deliver(&db, PEER, "paused-quant", &input))
        .unwrap();
    run(&mut db, "pause-quant", "pause", json!({}), now + 2);
    let paused = db.process_prepared(
        db.job("paused-quant").unwrap().unwrap(), now + 3,
        Some(json!({"request":request,"response":{"quantObservationBatch":observation_batch(now+1)}})),
    ).unwrap().unwrap();
    assert_eq!(paused["data"]["error"]["code"], "SCAN_PAUSED");
    drop(db);
    for ack in [legacy, paused] {
        let (status, _) = observations(
            Storage::open(dir.path()).unwrap(),
            ack["data"]["result"]["scanId"].as_str().unwrap(),
        )
        .await;
        assert_eq!(status, axum::http::StatusCode::NOT_FOUND);
    }
}

async fn scan_over_http(
    db: &mut Storage,
    config: &tickdesk_local::kernel::KernelConfig,
    key: &str,
    now: u64,
) -> (Value, Value) {
    use tickdesk_local::market;
    let input = command(db, key, key, "scan_now", json!({}));
    let delivery = deliver(db, PEER, key, &input);
    db.receive(&delivery).unwrap();
    let request = market::capture(db, &input["data"], now).unwrap();
    let start = std::time::Instant::now();
    let prepared = market::prepare(Some(config), request).await;
    let finish = now + u64::try_from(start.elapsed().as_millis()).unwrap();
    assert_eq!(
        prepared["response"]["quantObservationBatch"]["version"], 3,
        "{prepared}"
    );
    let rules = prepared["response"]["holdingRuleBatch"]["rules"].clone();
    let ack = db
        .process_prepared(db.job(key).unwrap().unwrap(), finish, Some(prepared))
        .unwrap()
        .unwrap();
    db.receive(&delivery).unwrap();
    assert!(db.job(key).unwrap().is_none());
    (ack["data"]["result"].clone(), rules)
}

#[tokio::test]
#[ignore = "requires isolated Python quant fixture on TICKDESK_QUANT_VERIFY_ADDRESS"]
async fn quant_kernel_http_to_durable_scan() {
    use tickdesk_local::kernel::KernelConfig;

    let address = std::env::var("TICKDESK_QUANT_VERIFY_ADDRESS")
        .unwrap()
        .parse()
        .unwrap();
    let config = KernelConfig {
        address,
        token_file: None,
    };
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1767342600000;
    let (scan, _) = scan_over_http(&mut db, &config, "live-quant", now).await;
    assert_eq!(scan["publishedTaskIds"], json!([]));
    assert!(db
        .outbox(16)
        .unwrap()
        .iter()
        .any(|e| e["payload"]["kind"] == "scan_result" && e["payload"]["data"] == scan));
    assert!(db
        .outbox(16)
        .unwrap()
        .iter()
        .all(|e| e["payload"]["kind"] != "reminder"));
    drop(db);

    let (status, stored) = observations(
        Storage::open(dir.path()).unwrap(),
        scan["scanId"].as_str().unwrap(),
    )
    .await;
    assert_eq!(status, axum::http::StatusCode::OK);
    assert_eq!(stored["batch"]["decisionCutoffMs"], now);
    let rows = stored["batch"]["observations"].as_array().unwrap();
    assert_eq!(rows.len(), 4);
    for (symbol, entry, exit, score) in [
        ("000001.SZ", json!(true), json!(false), json!("0")),
        ("000002.SZ", json!(true), json!(false), json!("100")),
        ("000003.SZ", json!(false), json!(false), Value::Null),
        ("000004.SZ", Value::Null, json!(true), Value::Null),
    ] {
        let row = rows.iter().find(|row| row["symbol"] == symbol).unwrap();
        assert_eq!(row["entry"]["value"], entry, "{symbol} entry");
        assert_eq!(row["exit"]["value"], exit, "{symbol} exit");
        assert_eq!(row["score"]["value"], score, "{symbol} score");
        assert_eq!(row["admission"], "research");
        assert_eq!(row["bar"]["status"], "complete");
    }

    let symbol = "000004.SZ";
    let row = rows.iter().find(|row| row["symbol"] == symbol).unwrap();
    let origin = json!({"group":row["group"],"strategyId":row["strategyId"],"codeHash":row["codeHash"],
        "parametersHash":row["parametersHash"],"dependenciesHash":row["dependenciesHash"]});
    let mut db = Storage::open(dir.path()).unwrap();
    let base = run(
        &mut db,
        "live-hold",
        "reconcile_holding",
        json!({"symbol":symbol,"basePositionRevision":0,
        "qty":300,"cost":"10","asOf":now-5_700_000,"reason":"synthetic baseline","resolvedExecutionIds":[],
        "partitions":[{"qty":300,"restrictions":[],"origin":origin}]}),
        now + 10_000,
    );
    assert_eq!(base["data"]["ok"], true, "{base}");
    let (_, rules) = scan_over_http(&mut db, &config, "live-verify-rule", now + 11_000).await;
    let rules = rules.as_array().expect("real held symbol rules");
    assert!(rules
        .iter()
        .any(|rule| rule["rule"]["symbol"] == symbol && rule["rule"]["settlement"] == "T+1"));
    let buy = run(
        &mut db,
        "live-buy",
        "record_execution",
        json!({"symbol":symbol,"side":"buy","qty":200,
        "price":"10.0000","channel":"auction","executedAt":now-5_460_000}),
        now + 20_000,
    );
    assert_eq!(
        buy["data"]["result"]["execution"]["positionEffect"], "applied",
        "{buy}"
    );
    assert_eq!(
        buy["data"]["result"]["execution"]["sellableDate"],
        "2026-01-05"
    );
    assert_eq!(buy["data"]["result"]["position"]["qty"], 500);
    assert_eq!(buy["data"]["result"]["position"]["availableQty"], 300);

    let sell = run(
        &mut db,
        "live-reduce",
        "record_execution",
        json!({"symbol":symbol,"side":"sell","qty":100,
        "price":"10.0100","channel":"auction","executedAt":now-5_459_999}),
        now + 21_000,
    );
    assert_eq!(sell["data"]["result"]["execution"]["price"], "10.0100");
    assert_eq!(
        sell["data"]["result"]["execution"]["positionEffect"], "applied",
        "{sell}"
    );
    assert_eq!(sell["data"]["result"]["position"]["qty"], 400);
    assert_eq!(sell["data"]["result"]["position"]["availableQty"], 200);
    assert!(sell["data"]["result"]["execution"]["realizedPnl"].is_null());
    let snapshot = run(&mut db, "live-valuation", "sync", json!({}), now + 21_001);
    let page_id = &snapshot["data"]["result"]["pageEventId"];
    let page = db
        .outbox(64)
        .unwrap()
        .into_iter()
        .map(|row| row["payload"].clone())
        .find(|row| row["id"] == *page_id)
        .unwrap();
    assert_eq!(page["data"]["positions"][0]["qty"], 400);
    assert_eq!(page["data"]["positions"][0]["priceStatus"], "missing");
    assert!(page["data"]["positions"][0]["marketValue"].is_null());
    let (review, _) = scan_over_http(&mut db, &config, "live-held-exit", now + 22_000).await;
    drop(db);
    let (_, review) = observations(
        Storage::open(dir.path()).unwrap(),
        review["scanId"].as_str().unwrap(),
    )
    .await;
    let event = review["events"]
        .as_array()
        .unwrap()
        .iter()
        .find(|event| event["symbol"] == symbol && event["phase"] == "exit")
        .unwrap();
    assert_eq!(event["state"], "active");
    assert_eq!(event["holdingExit"]["allocatedQty"], 200);
    assert_eq!(event["holdingExit"]["allocatedSellableQty"], 200);
    assert!(event["holdingExit"]["plannedQty"].is_null());

    let monday = 1767576600000;
    let mut db = Storage::open(dir.path()).unwrap();
    scan_over_http(&mut db, &config, "live-next-session", monday).await;
    let hold = run(
        &mut db,
        "live-maturity",
        "get_holding_details",
        json!({"symbol":symbol}),
        monday + 8_000,
    );
    assert_eq!(
        hold["data"]["result"]["holding"]["availableQty"], 400,
        "{hold}"
    );
    let close = run(
        &mut db,
        "live-close",
        "record_execution",
        json!({"symbol":symbol,"side":"sell","qty":400,
        "price":"10.0000","channel":"auction","executedAt":monday+8_001}),
        monday + 8_001,
    );
    assert_eq!(
        close["data"]["result"]["execution"]["positionEffect"], "applied",
        "{close}"
    );
    assert_eq!(close["data"]["result"]["position"]["qty"], 0);
    assert!(close["data"]["result"]["execution"]["realizedPnl"].is_null());
    let (closed, _) = scan_over_http(&mut db, &config, "live-closed-holding", monday + 9_000).await;
    drop(db);
    let (_, closed) = observations(
        Storage::open(dir.path()).unwrap(),
        closed["scanId"].as_str().unwrap(),
    )
    .await;
    assert!(closed["events"].as_array().unwrap().iter().any(|row| {
        row["eventId"] == event["eventId"]
            && row["holdingExit"]["state"] == "closed"
            && row["state"] == "resolved"
    }));
    let mut db = Storage::open(dir.path()).unwrap();
    let reentry = run(
        &mut db,
        "live-new-holding-cycle",
        "reconcile_holding",
        json!({"symbol":symbol,
        "basePositionRevision":close["data"]["result"]["position"]["positionRevision"],"qty":100,"cost":"10",
        "asOf":monday+18_000,"reason":"new verified holding cycle","resolvedExecutionIds":[],
        "partitions":[{"qty":100,"restrictions":[],"origin":origin}]}),
        monday + 18_000,
    );
    assert_eq!(reentry["data"]["ok"], true, "{reentry}");
    let (renewed, _) = scan_over_http(&mut db, &config, "live-renewed-exit", monday + 19_000).await;
    drop(db);
    let (_, renewed) = observations(
        Storage::open(dir.path()).unwrap(),
        renewed["scanId"].as_str().unwrap(),
    )
    .await;
    assert!(renewed["events"].as_array().unwrap().iter().any(|row| {
        row["previousEventId"] == event["eventId"]
            && row["eventId"] != event["eventId"]
            && row["holdingExit"]["allocatedQty"] == 100
            && row["state"] == "active"
    }));
}

#[tokio::test]
async fn quant_condition_survives_twelve_slots_missing_data_and_same_bar_revisions() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794794400000;
    let bar = observation_batch(now)["observations"][0]["bar"].clone();
    let scan = |db: &mut Storage, key: &str, now, bar: Value, exit: Value| {
        let mut batch = observation_batch(now);
        batch["observations"][0]["bar"] = bar;
        batch["observations"][0]["exit"] = exit;
        let ack = prepared_run(
            db,
            key,
            "scan_now",
            json!({}),
            json!({"quantObservationBatch":batch}),
            now,
        );
        assert_eq!(ack["data"]["result"]["publishedTaskIds"], json!([]));
        ack["data"]["result"]["scanId"].as_str().unwrap().to_owned()
    };
    let active = json!({"value":true,"reason":null});
    let first = scan(&mut db, "first-risk", now, bar.clone(), active.clone());
    for slot in 1..=11 {
        let result = match slot {
            5 => json!({"value":null,"reason":"missing"}),
            8 => json!({"value":false,"reason":null}),
            _ => active.clone(),
        };
        scan(
            &mut db,
            &format!("slot-{slot}"),
            now + slot * 300_000,
            bar.clone(),
            result,
        );
    }
    let connection = rusqlite::Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();
    let events = || {
        connection
            .query_row(
                "SELECT count(*) FROM objects WHERE collection='quant_events'",
                [],
                |row| row.get::<_, u64>(0),
            )
            .unwrap()
    };
    assert_eq!(events(), 1);
    assert!(db
        .outbox(16)
        .unwrap()
        .iter()
        .all(|e| e["payload"]["kind"] != "reminder"));
    drop(db);
    let (status, original) = observations(Storage::open(dir.path()).unwrap(), &first).await;
    assert_eq!(status, axum::http::StatusCode::OK);
    assert_eq!(original["events"][0]["state"], "active");

    let mut db = Storage::open(dir.path()).unwrap();
    let unknown_time = now + 2 * 86_400_000;
    let unknown_bar = observation_batch(unknown_time)["observations"][0]["bar"].clone();
    scan(
        &mut db,
        "newer-input-missing",
        unknown_time,
        unknown_bar,
        json!({"value":null,"reason":"missing_factor"}),
    );
    let stale_bar = observation_batch(now + 86_400_000)["observations"][0]["bar"].clone();
    let stale = scan(
        &mut db,
        "late-older-false",
        unknown_time + 1,
        stale_bar,
        json!({"value":false,"reason":null}),
    );
    let next = unknown_time + 86_400_000;
    let next_bar = observation_batch(next)["observations"][0]["bar"].clone();
    let cleared = scan(
        &mut db,
        "resolved-risk",
        next,
        next_bar,
        json!({"value":false,"reason":null}),
    );
    let third = next + 86_400_000;
    let third_bar = observation_batch(third)["observations"][0]["bar"].clone();
    let reopened = scan(&mut db, "new-risk", third, third_bar, active);
    assert_eq!(events(), 2);
    drop(db);
    let (_, carried) = observations(Storage::open(dir.path()).unwrap(), &stale).await;
    assert_eq!(carried["events"][0]["state"], "active");
    assert_eq!(
        carried["events"][0]["eventId"],
        original["events"][0]["eventId"]
    );
    let (_, resolved) = observations(Storage::open(dir.path()).unwrap(), &cleared).await;
    let (_, renewed) = observations(Storage::open(dir.path()).unwrap(), &reopened).await;
    assert_eq!(resolved["events"][0]["state"], "resolved");
    assert_eq!(
        resolved["events"][0]["eventId"],
        original["events"][0]["eventId"]
    );
    assert_ne!(
        renewed["events"][0]["eventId"],
        original["events"][0]["eventId"]
    );
    assert_eq!(
        renewed["events"][0]["previousEventId"],
        original["events"][0]["eventId"]
    );
}

#[test]
fn legacy_sell_scan_rechecks_current_sellable_evidence() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794794400000;
    let old = run(
        &mut db,
        "old-available",
        "adjust_position",
        json!({"symbol":"300502.SZ","basePositionRevision":0,
        "qty":100,"availableQty":100,"pendingLots":[],"asOf":now-86_400_000,"cost":"10",
        "resolvedExecutionIds":[],"reason":"previous session snapshot"}),
        now - 86_400_000,
    );
    assert_eq!(old["data"]["ok"], true);
    let mut reply = frozen(now);
    reply["frozen"]["candidates"][0]["direction"] = json!("sell");
    let sales = prepared_run(
        &mut db,
        "verify-selling-rules",
        "scan_now",
        json!({}),
        reply,
        now,
    );
    let result = &sales["data"]["result"];
    assert_eq!(result["publishedTaskIds"], json!([]));
    assert_eq!(
        result["rejectionSummary"],
        json!([{"reasonCode":"SELLABLE_UNKNOWN","count":1}])
    );
}

#[tokio::test]
async fn held_exit_review_uses_matching_allocation_and_survives_a_partial_sale() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794794400000;
    let symbol = "300502.SZ";
    let origin = json!({"group":"daily_swing","strategyId":"s","codeHash":"a".repeat(64),
        "parametersHash":"b".repeat(64),"dependenciesHash":"c".repeat(64)});
    let confirmed = run(
        &mut db,
        "reconcile-held",
        "reconcile_holding",
        json!({"symbol":symbol,
        "basePositionRevision":0,"qty":1000,"cost":"12","asOf":now,"reason":"fixture confirmation",
        "resolvedExecutionIds":[],"partitions":[{"qty":400,"restrictions":[],"origin":origin},
        {"qty":600,"restrictions":[{"kind":"freeze","evidenceRef":"fixture:freeze"}],"origin":origin}]}),
        now,
    );
    assert_eq!(confirmed["data"]["ok"], true, "{confirmed}");
    let first = prepared_run(
        &mut db,
        "exit-review",
        "scan_now",
        json!({}),
        json!({"quantObservationBatch":observation_batch(now+1)}),
        now + 1,
    );
    let fill = run(
        &mut db,
        "reduce-holding",
        "record_execution",
        json!({"symbol":symbol,
        "side":"sell","qty":250,"price":"11.2500","executedAt":now+2}),
        now + 2,
    );
    assert_eq!(
        fill["data"]["result"]["execution"]["positionEffect"], "applied",
        "{fill}"
    );
    assert_eq!(fill["data"]["result"]["position"]["availableQty"], 150);
    let mut batch = observation_batch(now + 1);
    batch["decisionCutoffMs"] = json!(now + 3);
    batch["observations"][0]["exit"] = json!({"value":null,"reason":"missing_price"});
    let next = prepared_run(
        &mut db,
        "exit-review-after-fill",
        "scan_now",
        json!({}),
        json!({"quantObservationBatch":batch}),
        now + 3,
    );
    drop(db);

    let mut event_id = None;
    for (ack, qty, available) in [(first, 1000, json!(400)), (next, 750, Value::Null)] {
        let (_, result) = observations(
            Storage::open(dir.path()).unwrap(),
            ack["data"]["result"]["scanId"].as_str().unwrap(),
        )
        .await;
        let event = &result["events"][0];
        assert_eq!(event["state"], "active");
        assert_eq!(event["holdingExit"]["allocatedQty"], qty);
        assert_eq!(event["holdingExit"]["allocatedSellableQty"], available);
        assert_eq!(event["holdingExit"]["state"], "exit_restricted");
        assert!(event["holdingExit"]["plannedQty"].is_null());
        if let Some(previous) = event_id {
            assert_eq!(event["eventId"], previous);
        }
        event_id = Some(event["eventId"].clone());
    }
}

#[test]
fn frozen_scan_publishes_atomic_tasks_and_enforces_cooldown_and_generation() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794794400000;
    let first = prepared_run(&mut db, "first", "scan_now", json!({}), frozen(now), now);
    assert_eq!(first["data"]["ok"], true, "{first}");
    let task = first["data"]["result"]["publishedTaskIds"][0]
        .as_str()
        .unwrap();
    let read = run(&mut db, "task", "get_task", json!({"taskId":task}), now);
    assert_eq!(
        read["data"]["result"]["task"]["score"],
        "88.123456789123456789"
    );
    assert_eq!(
        read["data"]["result"]["task"]["reasons"]["verificationStatus"],
        "unverified"
    );
    let second = prepared_run(
        &mut db,
        "second",
        "scan_now",
        json!({}),
        frozen(now + 1),
        now + 1,
    );
    assert_eq!(second["data"]["result"]["publishedTaskIds"], json!([]));
    let mut broken = frozen(now + 2);
    broken["frozen"]["generationAfter"] = json!("generation-b");
    let rejected = prepared_run(&mut db, "broken", "scan_now", json!({}), broken, now + 2);
    assert_eq!(rejected["data"]["error"]["code"], "SCAN_FAILED");
    let events = db.outbox(16).unwrap();
    assert!(events.iter().any(|e| e["payload"]["kind"] == "scan_result"
        && e["payload"]["data"] == first["data"]["result"]));
}

#[test]
fn malformed_frozen_batch_replaces_latest_success_with_durable_failure() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794794400000;
    let mut first = frozen(now);
    first["frozen"]["candidates"] = json!([]);
    let no_match = prepared_run(&mut db, "no-match", "scan_now", json!({}), first, now);
    assert_eq!(no_match["data"]["result"]["status"], "succeeded");
    let mut broken = frozen(now + 1);
    broken["frozen"]["candidates"]
        .as_array_mut()
        .unwrap()
        .push(json!({
            "direction":"buy","candidate":{"symbol":"300502.SZ"}
        }));
    let failed = prepared_run(&mut db, "malformed", "scan_now", json!({}), broken, now + 1);
    assert_eq!(failed["data"]["error"]["code"], "SCAN_FAILED");
    assert_eq!(failed["data"]["result"]["publishedTaskIds"], json!([]));
    drop(db);
    let mut db = Storage::open(dir.path()).unwrap();
    let latest = run(
        &mut db,
        "read-malformed",
        "get_candidates",
        json!({}),
        now + 2,
    );
    assert_eq!(latest["data"]["result"]["scan"], failed["data"]["result"]);
    assert_eq!(latest["data"]["result"]["items"], json!([]));
}

#[test]
fn failed_strategy_cannot_count_toward_task_resonance() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794794400000;
    let mut malformed = frozen(now);
    malformed["frozen"]["strategies"] = json!({"total":4,"succeeded":3,"failed":["a"]});
    let result = prepared_run(
        &mut db,
        "failed-attribution",
        "scan_now",
        json!({}),
        malformed,
        now,
    );
    assert_eq!(result["data"]["error"]["code"], "SCAN_FAILED");
    assert_eq!(result["data"]["result"]["publishedTaskIds"], json!([]));
    let reader = rusqlite::Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();
    assert_eq!(
        reader
            .query_row(
                "SELECT COUNT(*) FROM objects WHERE collection='tasks'",
                [],
                |row| row.get::<_, i32>(0)
            )
            .unwrap(),
        0
    );
}

#[test]
fn scan_preserves_confirmed_closure_and_missing_approval_as_different_outcomes() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794794400000;
    let closed = prepared_run(
        &mut db,
        "closed-day",
        "scan_now",
        json!({}),
        json!({"error":"SCAN_OUTSIDE_WINDOW"}),
        now,
    );
    assert_eq!(closed["data"]["result"]["status"], "rejected");
    let missing = prepared_run(
        &mut db,
        "missing-approval",
        "scan_now",
        json!({}),
        json!({"error":"DATA_UNAVAILABLE","productionReason":"NO_PRODUCTION_APPROVAL"}),
        now + 1,
    );
    assert_eq!(missing["data"]["result"]["status"], "failed");
    assert_eq!(
        missing["data"]["result"]["rejectionSummary"],
        json!([{"reasonCode":"NO_PRODUCTION_APPROVAL","count":1}])
    );
    assert_eq!(missing["data"]["result"]["publishedTaskIds"], json!([]));
}

#[test]
fn active_snoozed_risk_is_not_duplicated_after_cooldown() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let at = 1794794400000;
    let profile = run(
        &mut db,
        "aggressive",
        "set_profile",
        json!({"profile":"aggressive"}),
        at,
    );
    assert_eq!(profile["data"]["ok"], true);
    let risk = |now| {
        let mut response = frozen(now);
        response["frozen"]["candidates"][0]["direction"] = json!("risk");
        response
    };
    let scan = prepared_run(&mut db, "risk-first", "scan_now", json!({}), risk(at), at);
    let task_id = scan["data"]["result"]["publishedTaskIds"][0]
        .as_str()
        .unwrap();
    let later = run(
        &mut db,
        "risk-later",
        "snooze",
        json!({"taskId":task_id,"baseActionVersion":1,"minutes":5}),
        at + 29 * 60_000,
    );
    assert_eq!(later["data"]["ok"], true);
    let next_at = at + 31 * 60_000;
    let next = prepared_run(
        &mut db,
        "risk-repeat",
        "scan_now",
        json!({}),
        risk(next_at),
        next_at,
    );
    assert_eq!(next["data"]["result"]["publishedTaskIds"], json!([]));
    let current = run(
        &mut db,
        "verify-risk",
        "get_task",
        json!({"taskId":task_id}),
        next_at,
    );
    assert_eq!(
        current["data"]["result"]["task"],
        later["data"]["result"]["task"]
    );
}

#[test]
fn prior_suspension_without_current_evidence_is_unknown_for_new_operations() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let reader = rusqlite::Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();
    reader.execute(
        "INSERT INTO objects VALUES ('market_suspension_heads','300502.SZ',?1)",
        [json!({"epoch":db.identity.server_epoch,"active":true,"episode":"old","taskId":"old"}).to_string()],
    ).unwrap();
    let now = 1794794400000;
    let scan = prepared_run(
        &mut db,
        "suspension-status-unknown",
        "scan_now",
        json!({}),
        frozen(now),
        now,
    );
    assert_eq!(scan["data"]["result"]["publishedTaskIds"], json!([]));
    assert_eq!(
        scan["data"]["result"]["rejectionSummary"],
        json!([{"reasonCode":"TRADING_STATUS_UNCONFIRMED","count":1}])
    );
}

#[test]
fn cursor_freezes_original_scan_and_watch_scope_is_atomic() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794794400000;
    let master = json!({"symbols":[{"symbol":"300502.SZ","name":"test","exchange":"SZSE","instrument":"stock","board":"创业板","supported":true,"source":{"name":"fixture exchange","ref":"exchange:list","asOf":now-1}},{"symbol":"688001.SH","name":"unsupported","exchange":"SSE","instrument":"stock","board":"科创板","supported":false,"source":null}]});
    let bad = prepared_run(
        &mut db,
        "bad",
        "watch",
        json!({"symbols":["300502.SZ","688001.SH"]}),
        master.clone(),
        now,
    );
    assert_eq!(bad["data"]["error"]["code"], "UNSUPPORTED_MARKET");
    let good = prepared_run(
        &mut db,
        "good",
        "watch",
        json!({"symbols":["300502.SZ"]}),
        master.clone(),
        now,
    );
    assert_eq!(
        good["data"]["result"]["watchlist"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    let search = prepared_run(
        &mut db,
        "search",
        "search_symbols",
        json!({"query":"0","limit":1}),
        master,
        now,
    );
    let cursor = search["data"]["result"]["nextPageCursor"].as_str().unwrap();
    let tail = run(
        &mut db,
        "tail",
        "search_symbols",
        json!({"query":"0","pageCursor":cursor}),
        now + 1,
    );
    assert_eq!(tail["data"]["result"]["items"][0]["symbol"], "688001.SH");
    let wrong = run(
        &mut db,
        "wrong",
        "search_symbols",
        json!({"query":"different","pageCursor":cursor}),
        now + 1,
    );
    assert_eq!(wrong["data"]["error"]["code"], "INVALID_PARAMS");
}

#[test]
fn sourced_symbol_can_be_followed_without_analysis_eligibility_and_removed_offline() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794794400000;
    let watch = prepared_run(
        &mut db,
        "watch-outside-quant",
        "watch",
        json!({"symbols":["688001.SH"]}),
        json!({"symbols":[{"symbol":"688001.SH","name":"fixture","exchange":"SSE","instrument":"stock",
            "board":"科创板","supported":false,"source":{"name":"fixture exchange","ref":"exchange:list","asOf":now-1}}]}),
        now,
    );
    assert_eq!(
        watch["data"]["result"]["addedSymbols"],
        json!(["688001.SH"])
    );
    let remove = run(
        &mut db,
        "unwatch-without-master",
        "unwatch",
        json!({"symbols":["688001.SH"]}),
        now + 1,
    );
    assert_eq!(
        remove["data"]["result"]["removedSymbols"],
        json!(["688001.SH"])
    );
    assert_eq!(remove["data"]["result"]["watchlist"], json!([]));
}

#[test]
fn followed_quote_clock_is_persisted_and_does_not_roll_back() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794794400000;
    prepared_run(
        &mut db,
        "watch-quoted",
        "watch",
        json!({"symbols":["600000.SH"]}),
        json!({"symbols":[{"symbol":"600000.SH","name":"fixture","exchange":"SSE","instrument":"stock",
           "board":"沪主板","supported":true,"source":{"name":"fixture","ref":"exchange:list","asOf":now-1}}]}),
        now,
    );
    for (key, at) in [("current-quote", now), ("late-old-quote", now - 1)] {
        prepared_run(
            &mut db,
            key,
            "search_symbols",
            json!({"query":"600000"}),
            json!({"symbols":[],"quotes":[{"symbol":"600000.SH","price":"12.25","asOf":at,
                "source":{"name":"fixture feed","ref":"vendor:snapshot","asOf":at}}]}),
            now + 1,
        );
        let sqlite = rusqlite::Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();
        let raw: String = sqlite
            .query_row(
                "SELECT body FROM objects WHERE collection='watchlist' AND object_id='600000.SH'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(
            serde_json::from_str::<Value>(&raw).unwrap()["dataAsOf"],
            now
        );
    }
}

#[test]
fn partial_scan_keeps_tasks_and_pause_wins_over_prepared_results() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794794400000;
    let mut partial = frozen(now);
    partial["frozen"]["strategies"] = json!({"total":4,"succeeded":3,"failed":["failed-strategy"]});
    let ack = prepared_run(&mut db, "partial", "scan_now", json!({}), partial, now);
    assert_eq!(ack["data"]["ok"], false);
    assert_eq!(ack["data"]["error"]["code"], "SCAN_PARTIAL");
    assert_eq!(
        ack["data"]["result"]["publishedTaskIds"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    let latest = run(
        &mut db,
        "last-partial",
        "get_candidates",
        json!({}),
        now + 1,
    );
    assert_eq!(latest["data"]["result"]["scan"], ack["data"]["result"]);
    let input = command(&db, "in-flight", "in-flight", "scan_now", json!({}));
    let request = tickdesk_local::market::capture(&db, &input["data"], now + 2).unwrap();
    let blocked = tickdesk_local::market::capture(&db, &input["data"], now + 3).unwrap();
    assert_eq!(blocked["leaseAcquired"], false);
    db.receive(&deliver(&db, PEER, "in-flight", &input))
        .unwrap();
    run(&mut db, "pause", "pause", json!({}), now + 4);
    let done = db
        .process_prepared(
            db.job("in-flight").unwrap().unwrap(),
            now + 5,
            Some(json!({"request":request,"response":frozen(now+2)})),
        )
        .unwrap()
        .unwrap();
    assert_eq!(done["data"]["error"]["code"], "SCAN_PAUSED");
    assert_eq!(done["data"]["result"]["publishedTaskIds"], json!([]));
}

#[test]
fn provider_http_boundary_fetches_actual_response_and_denies_redirect() {
    let rt = tokio::runtime::Runtime::new().unwrap();
    rt.block_on(async {
        let app = axum::Router::new().route(
            "/api/tickdesk/prepare",
            axum::routing::post(|| async { axum::Json(json!({"symbols":[]})) }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let task = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let cfg = tickdesk_local::kernel::KernelConfig {
            address,
            token_file: None,
        };
        let output = tickdesk_local::market::prepare(
            Some(&cfg),
            json!({"data":{"cmd":"search_symbols","query":"x"}}),
        )
        .await;
        assert_eq!(output["response"]["symbols"], json!([]));
        task.abort();
    });
}

#[test]
fn paused_scheduled_capture_does_not_lock_out_resume() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794794400000;
    run(&mut db, "pause-for-slot", "pause", json!({}), now);
    let ignored = tickdesk_local::market::capture(&db, &json!({"cmd":"scan_now"}), now).unwrap();
    assert_eq!(ignored["scanLease"], Value::Null);
    let resume = tickdesk_local::market::capture(&db, &json!({"cmd":"resume"}), now + 1).unwrap();
    assert_eq!(resume["leaseAcquired"], true);
}

#[test]
fn verified_exit_publishes_restriction_without_nagging_and_suppresses_new_buys() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794794400000;
    let symbol = "300502.SZ";
    let origin = json!({"group":"daily_swing","strategyId":"s","codeHash":"a".repeat(64),
        "parametersHash":"b".repeat(64),"dependenciesHash":"c".repeat(64)});
    let confirmed = run(
        &mut db,
        "exit-holding",
        "reconcile_holding",
        json!({"symbol":symbol,"basePositionRevision":0,"qty":600,"cost":"12","asOf":now,
        "reason":"fixture confirmation","resolvedExecutionIds":[],
        "partitions":[{"qty":600,"restrictions":[{"kind":"freeze","evidenceRef":"fixture:freeze"}],"origin":origin}]}),
        now,
    );
    assert_eq!(confirmed["data"]["ok"], true, "{confirmed}");

    let first = prepared_run(
        &mut db,
        "exit-restricted",
        "scan_now",
        json!({}),
        v3_frozen(now, true, false),
        now,
    );
    assert_eq!(first["data"]["ok"], true, "{first}");
    let [task_id] = first["data"]["result"]["publishedTaskIds"]
        .as_array()
        .unwrap()
        .as_slice()
    else {
        panic!("expected one exit task: {first}");
    };
    let task_id = task_id.as_str().unwrap();
    let read = run(
        &mut db,
        "exit-task",
        "get_task",
        json!({"taskId":task_id}),
        now + 1,
    );
    let task = &read["data"]["result"]["task"];
    assert_eq!(task["direction"], "sell");
    assert!(task["score"].is_null());
    assert_eq!(task["exit"]["executable"], false);
    assert_eq!(task["exit"]["restriction"], "frozen");
    assert_eq!(task["exit"]["availableQty"], 0);
    assert_eq!(task["exit"]["ruleId"], "matrix_daily_exit");
    assert!(task["currentReminder"].is_null());
    assert!(task["nextReminderAt"].is_null());
    assert!(db
        .outbox(64)
        .unwrap()
        .iter()
        .all(|event| event["payload"]["kind"] != "reminder"));

    let repeat = prepared_run(
        &mut db,
        "exit-repeat",
        "scan_now",
        json!({}),
        v3_frozen(now + 1000, true, false),
        now + 1000,
    );
    assert_eq!(repeat["data"]["result"]["publishedTaskIds"], json!([]));
    assert!(repeat["data"]["result"]["rejectionSummary"]
        .as_array()
        .unwrap()
        .iter()
        .any(|row| matches!(
            row["reasonCode"].as_str(),
            Some("COOLDOWN_SUPPRESSED" | "ACTIVE_TASK_EXISTS" | "FORMAL_EXIT_LATCHED")
        )));

    let with_buy = prepared_run(
        &mut db,
        "exit-blocks-buy",
        "scan_now",
        json!({}),
        v3_frozen(now + 2000, true, true),
        now + 2000,
    );
    assert_eq!(with_buy["data"]["result"]["publishedTaskIds"], json!([]));
    assert!(
        with_buy["data"]["result"]["rejectionSummary"]
            .as_array()
            .unwrap()
            .iter()
            .any(|row| row["reasonCode"] == "ACTIVE_EXIT_EXISTS"),
        "{with_buy}"
    );

    let details = run(
        &mut db,
        "exit-details",
        "get_holding_details",
        json!({"symbol":symbol}),
        now + 3000,
    );
    let revision = details["data"]["result"]["holding"]["positionRevision"]
        .as_u64()
        .unwrap();
    let released = run(
        &mut db,
        "exit-release",
        "reconcile_holding",
        json!({"symbol":symbol,"basePositionRevision":revision,"qty":600,"cost":"12",
        "asOf":now+3_000_001,"reason":"freeze evidence cleared","resolvedExecutionIds":[],
        "partitions":[{"qty":600,"restrictions":[],"origin":origin}]}),
        now + 3_000_001,
    );
    assert_eq!(released["data"]["ok"], true, "{released}");

    let renewed = prepared_run(
        &mut db,
        "exit-executable",
        "scan_now",
        json!({}),
        v3_frozen(now + 4_000_000, true, false),
        now + 4_000_000,
    );
    assert_eq!(renewed["data"]["ok"], true, "{renewed}");
    let [renewed_id] = renewed["data"]["result"]["publishedTaskIds"]
        .as_array()
        .unwrap()
        .as_slice()
    else {
        panic!("expected one executable exit: {renewed}");
    };
    let renewed_id = renewed_id.as_str().unwrap();
    let read = run(
        &mut db,
        "exit-task-2",
        "get_task",
        json!({"taskId":renewed_id}),
        now + 4_000_001,
    );
    let task = &read["data"]["result"]["task"];
    assert_eq!(task["exit"]["executable"], true, "{task}");
    assert!(task["exit"]["restriction"].is_null());
    assert_eq!(task["exit"]["availableQty"], 600);
    assert_eq!(task["currentReminder"]["scheduledAt"], now + 4_000_000);
    let reminders = db
        .outbox(256)
        .unwrap()
        .into_iter()
        .filter(|event| {
            event["payload"]["kind"] == "reminder"
                && event["payload"]["data"]["taskId"] == renewed_id
        })
        .count();
    assert_eq!(reminders, 1);
}

#[test]
fn exit_without_a_holding_stays_research_only() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794794400000;
    let scan = prepared_run(
        &mut db,
        "exit-no-holding",
        "scan_now",
        json!({}),
        v3_frozen(now, true, false),
        now,
    );
    assert_eq!(scan["data"]["ok"], true, "{scan}");
    assert_eq!(scan["data"]["result"]["publishedTaskIds"], json!([]));
    assert!(scan["data"]["result"]["rejectionSummary"]
        .as_array()
        .unwrap()
        .iter()
        .any(|row| row["reasonCode"] == "EXIT_WITHOUT_HOLDING"));
}

#[test]
fn research_exit_event_does_not_suppress_a_production_buy() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794794400000;
    let latched = prepared_run(
        &mut db,
        "research-exit",
        "scan_now",
        json!({}),
        json!({"quantObservationBatch":observation_batch(now)}),
        now,
    );
    assert_eq!(latched["data"]["result"]["publishedTaskIds"], json!([]));
    let after_expiry = now + 31 * 60_000;
    let scan = prepared_run(
        &mut db,
        "buy-with-research-only",
        "scan_now",
        json!({}),
        v3_frozen(after_expiry, true, true),
        after_expiry,
    );
    assert_eq!(
        scan["data"]["result"]["publishedTaskIds"]
            .as_array()
            .unwrap()
            .len(),
        1,
        "{scan}"
    );
    assert!(
        scan["data"]["result"]["rejectionSummary"]
            .as_array()
            .unwrap()
            .iter()
            .all(|row| row["reasonCode"] != "ACTIVE_EXIT_EXISTS"),
        "{scan}"
    );
}

#[test]
fn exit_without_a_fresh_quote_has_no_executable_quantity_or_reminder() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794794400000;
    let symbol = "300502.SZ";
    let origin = json!({"group":"daily_swing","strategyId":"s","codeHash":"a".repeat(64),
        "parametersHash":"b".repeat(64),"dependenciesHash":"c".repeat(64)});
    let confirmed = run(
        &mut db,
        "quote-holding",
        "reconcile_holding",
        json!({"symbol":symbol,"basePositionRevision":0,"qty":600,"cost":"12","asOf":now,
        "reason":"fixture confirmation","resolvedExecutionIds":[],
        "partitions":[{"qty":600,"restrictions":[],"origin":origin}]}),
        now,
    );
    assert_eq!(confirmed["data"]["ok"], true, "{confirmed}");
    let label = "missing";
    let quote = Value::Null;
    let mut frozen = v3_frozen(now, true, false);
    frozen["frozen"]["exits"][0]["quote"] = quote;
    let scan = prepared_run(
        &mut db,
        &format!("quote-{label}"),
        "scan_now",
        json!({}),
        frozen,
        now,
    );
    assert_eq!(scan["data"]["ok"], true, "{scan}");
    let [task_id] = scan["data"]["result"]["publishedTaskIds"]
        .as_array()
        .unwrap()
        .as_slice()
    else {
        panic!("expected one exit task: {scan}");
    };
    let task = run(
        &mut db,
        &format!("quote-task-{label}"),
        "get_task",
        json!({"taskId":task_id}),
        now + 1,
    );
    let task = &task["data"]["result"]["task"];
    assert_eq!(task["exit"]["executable"], false, "{task}");
    assert_eq!(task["exit"]["restriction"], "quote_unavailable", "{task}");
    assert_eq!(task["remindedCount"], 0, "{task}");
    assert!(task["currentReminder"].is_null(), "{task}");
}

#[test]
fn formal_exit_latch_suppresses_buy_after_task_card_expiry() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794794400000;
    let symbol = "300502.SZ";
    let origin = json!({"group":"daily_swing","strategyId":"s","codeHash":"a".repeat(64),
        "parametersHash":"b".repeat(64),"dependenciesHash":"c".repeat(64)});
    let confirmed = run(
        &mut db,
        "formal-holding",
        "reconcile_holding",
        json!({"symbol":symbol,"basePositionRevision":0,"qty":600,"cost":"12","asOf":now,
        "reason":"fixture confirmation","resolvedExecutionIds":[],
        "partitions":[{"qty":300,"restrictions":[],"origin":origin},
        {"qty":300,"restrictions":[{"kind":"freeze","evidenceRef":"fixture:freeze"}]}]}),
        now,
    );
    assert_eq!(confirmed["data"]["ok"], true, "{confirmed}");
    let first = prepared_run(
        &mut db,
        "formal-first",
        "scan_now",
        json!({}),
        v3_frozen(now, true, false),
        now,
    );
    assert_eq!(
        first["data"]["result"]["publishedTaskIds"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    let after_expiry = now + 31 * 60_000;
    let scan = prepared_run(
        &mut db,
        "formal-buy-after-expiry",
        "scan_now",
        json!({}),
        v3_frozen(after_expiry, true, true),
        after_expiry,
    );
    assert_eq!(scan["data"]["result"]["publishedTaskIds"], json!([]));
    assert!(
        scan["data"]["result"]["rejectionSummary"]
            .as_array()
            .unwrap()
            .iter()
            .any(|row| row["reasonCode"] == "ACTIVE_EXIT_EXISTS"),
        "{scan}"
    );
    let connection = rusqlite::Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();
    let active: u64 = connection
        .query_row(
            "SELECT count(*) FROM objects WHERE collection='formal_exit_events'
             AND json_extract(body,'$.state')='active'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(active, 1);
}

#[test]
fn restricted_exit_reports_no_reminder_and_no_invented_review_time() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794794400000;
    let symbol = "300502.SZ";
    let origin = json!({"group":"daily_swing","strategyId":"s","codeHash":"a".repeat(64),
        "parametersHash":"b".repeat(64),"dependenciesHash":"c".repeat(64)});
    let confirmed = run(
        &mut db,
        "restricted-holding",
        "reconcile_holding",
        json!({"symbol":symbol,"basePositionRevision":0,"qty":600,"cost":"12","asOf":now,
        "reason":"fixture confirmation","resolvedExecutionIds":[],
        "partitions":[{"qty":600,"restrictions":[{"kind":"freeze","evidenceRef":"fixture:freeze"}],"origin":origin}]}),
        now,
    );
    assert_eq!(confirmed["data"]["ok"], true, "{confirmed}");
    let scan = prepared_run(
        &mut db,
        "restricted-exit",
        "scan_now",
        json!({}),
        v3_frozen(now, true, false),
        now,
    );
    let [task_id] = scan["data"]["result"]["publishedTaskIds"]
        .as_array()
        .unwrap()
        .as_slice()
    else {
        panic!("expected one exit task: {scan}");
    };
    let task = run(
        &mut db,
        "restricted-task",
        "get_task",
        json!({"taskId":task_id}),
        now + 1,
    );
    let task = &task["data"]["result"]["task"];
    assert_eq!(task["exit"]["restriction"], "frozen");
    assert_eq!(task["remindedCount"], 0);
    assert!(task["lastRemindedAt"].is_null());
    assert!(task["currentReminder"].is_null());
    assert!(task["exit"]["reviewAt"].is_null());
    let reminders = db
        .outbox(256)
        .unwrap()
        .into_iter()
        .filter(|event| {
            event["payload"]["kind"] == "reminder"
                && event["payload"]["data"]["taskId"].as_str() == task_id.as_str()
        })
        .count();
    assert_eq!(reminders, 0);
}

#[test]
fn exit_from_another_strategy_origin_never_uses_the_whole_position() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794794400000;
    let symbol = "300502.SZ";
    let origin = json!({"group":"daily_swing","strategyId":"other","codeHash":"a".repeat(64),
        "parametersHash":"b".repeat(64),"dependenciesHash":"c".repeat(64)});
    let confirmed = run(
        &mut db,
        "other-origin-holding",
        "reconcile_holding",
        json!({"symbol":symbol,"basePositionRevision":0,"qty":600,"cost":"12","asOf":now,
        "reason":"fixture confirmation","resolvedExecutionIds":[],
        "partitions":[{"qty":600,"restrictions":[],"origin":origin}]}),
        now,
    );
    assert_eq!(confirmed["data"]["ok"], true, "{confirmed}");
    let scan = prepared_run(
        &mut db,
        "other-origin-exit",
        "scan_now",
        json!({}),
        v3_frozen(now, true, false),
        now,
    );
    let [task_id] = scan["data"]["result"]["publishedTaskIds"]
        .as_array()
        .unwrap()
        .as_slice()
    else {
        panic!("expected one restricted exit task: {scan}");
    };
    let task = run(
        &mut db,
        "other-origin-task",
        "get_task",
        json!({"taskId":task_id}),
        now + 1,
    );
    let task = &task["data"]["result"]["task"];
    assert_eq!(task["exit"]["restriction"], "strategy_origin_unverified");
    assert!(task["exit"]["availableQty"].is_null());
    assert_eq!(task["exit"]["executable"], false);
    assert_eq!(task["remindedCount"], 0);
}

#[test]
fn partially_sellable_exit_states_the_available_quantity_and_reminds_once() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794794400000;
    let symbol = "300502.SZ";
    let origin = json!({"group":"daily_swing","strategyId":"s","codeHash":"a".repeat(64),
        "parametersHash":"b".repeat(64),"dependenciesHash":"c".repeat(64)});
    let confirmed = run(
        &mut db,
        "partial-holding",
        "reconcile_holding",
        json!({"symbol":symbol,"basePositionRevision":0,"qty":600,"cost":"12","asOf":now,
        "reason":"fixture confirmation","resolvedExecutionIds":[],
        "partitions":[{"qty":300,"restrictions":[],"origin":origin},
        {"qty":300,"restrictions":[{"kind":"freeze","evidenceRef":"fixture:freeze"}],"origin":origin}]}),
        now,
    );
    assert_eq!(confirmed["data"]["ok"], true, "{confirmed}");
    let scan = prepared_run(
        &mut db,
        "partial-exit",
        "scan_now",
        json!({}),
        v3_frozen(now, true, false),
        now,
    );
    assert_eq!(scan["data"]["ok"], true, "{scan}");
    let [task_id] = scan["data"]["result"]["publishedTaskIds"]
        .as_array()
        .unwrap()
        .as_slice()
    else {
        panic!("expected one exit task: {scan}");
    };
    let task_id = task_id.as_str().unwrap();
    let read = run(
        &mut db,
        "partial-task",
        "get_task",
        json!({"taskId":task_id}),
        now + 1,
    );
    let task = &read["data"]["result"]["task"];
    assert_eq!(task["exit"]["executable"], true, "{task}");
    assert_eq!(task["exit"]["restriction"], "partially_sellable", "{task}");
    assert_eq!(task["exit"]["availableQty"], 300);
    assert!(task["currentReminder"]["reminderId"].is_string());
    assert!(task["reasons"]["risks"]
        .as_array()
        .unwrap()
        .iter()
        .any(|text| text.as_str().unwrap().contains("仅部分数量可卖")));
    let reminders = db
        .outbox(256)
        .unwrap()
        .into_iter()
        .filter(|event| {
            event["payload"]["kind"] == "reminder" && event["payload"]["data"]["taskId"] == task_id
        })
        .count();
    assert_eq!(reminders, 1);
    let details = run(
        &mut db,
        "partial-details",
        "get_holding_details",
        json!({"symbol":symbol}),
        now + 2,
    );
    assert_eq!(details["data"]["result"]["holding"]["qty"], 600);
    assert_eq!(details["data"]["result"]["holding"]["availableQty"], 300);
}

#[test]
fn formal_exit_never_uses_a_partition_from_a_different_code_version() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794794400000;
    let symbol = "300502.SZ";
    let origin = json!({"group":"daily_swing","strategyId":"s","codeHash":"a".repeat(64),
        "parametersHash":"b".repeat(64),"dependenciesHash":"c".repeat(64)});
    let confirmed = run(
        &mut db,
        "version-origin-holding",
        "reconcile_holding",
        json!({"symbol":symbol,"basePositionRevision":0,"qty":600,"cost":"12","asOf":now,
        "reason":"fixture confirmation","resolvedExecutionIds":[],
        "partitions":[{"qty":600,"restrictions":[],"origin":origin}]}),
        now,
    );
    assert_eq!(confirmed["data"]["ok"], true, "{confirmed}");

    let mut different = v3_frozen(now, true, false);
    different["frozen"]["manifest"]["strategyHashes"][0]["codeHash"] = json!("d".repeat(64));
    let scan = prepared_run(
        &mut db,
        "version-mismatch-exit",
        "scan_now",
        json!({}),
        different,
        now,
    );
    assert_eq!(scan["data"]["ok"], true, "{scan}");
    let [task_id] = scan["data"]["result"]["publishedTaskIds"]
        .as_array()
        .unwrap()
        .as_slice()
    else {
        panic!("expected one restricted exit task: {scan}");
    };
    let task_id = task_id.as_str().unwrap();
    let task = run(
        &mut db,
        "version-mismatch-task",
        "get_task",
        json!({"taskId":task_id}),
        now + 1,
    );
    let task = &task["data"]["result"]["task"];
    assert_eq!(task["exit"]["executable"], false, "{task}");
    assert_eq!(
        task["exit"]["restriction"], "strategy_origin_unverified",
        "{task}"
    );
    assert!(task["exit"]["availableQty"].is_null(), "{task}");
    assert!(task["currentReminder"].is_null(), "{task}");
    assert_eq!(task["remindedCount"], 0, "{task}");
    let reminders = db
        .outbox(256)
        .unwrap()
        .into_iter()
        .filter(|event| event["payload"]["kind"] == "reminder")
        .count();
    assert_eq!(reminders, 0);
    let details = run(
        &mut db,
        "version-mismatch-details",
        "get_holding_details",
        json!({"symbol":symbol}),
        now + 2,
    );
    assert_eq!(
        details["data"]["result"]["holding"]["availableQty"], 600,
        "{details}"
    );
}

#[test]
fn formal_exit_rechecks_executability_quantity_and_reminders_across_restart() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794794400000;
    let symbol = "300502.SZ";
    let origin = json!({"group":"daily_swing","strategyId":"s","codeHash":"a".repeat(64),
        "parametersHash":"b".repeat(64),"dependenciesHash":"c".repeat(64)});
    let confirmed = run(
        &mut db,
        "cycle-holding",
        "reconcile_holding",
        json!({"symbol":symbol,"basePositionRevision":0,"qty":600,"cost":"12","asOf":now,
        "reason":"fixture confirmation","resolvedExecutionIds":[],
        "partitions":[{"qty":600,"restrictions":[],"origin":origin}]}),
        now,
    );
    assert_eq!(confirmed["data"]["ok"], true, "{confirmed}");

    let first = prepared_run(
        &mut db,
        "cycle-exit",
        "scan_now",
        json!({}),
        v3_frozen(now, true, false),
        now,
    );
    let [first_id] = first["data"]["result"]["publishedTaskIds"]
        .as_array()
        .unwrap()
        .as_slice()
    else {
        panic!("expected one executable exit: {first}");
    };
    let first_id = first_id.as_str().unwrap().to_owned();
    let first_task = run(
        &mut db,
        "cycle-task-1",
        "get_task",
        json!({"taskId":first_id}),
        now + 1,
    );
    assert_eq!(
        first_task["data"]["result"]["task"]["exit"]["availableQty"],
        600
    );
    assert_eq!(
        first_task["data"]["result"]["task"]["currentReminder"]["scheduledAt"],
        now
    );
    let reminder_count = |dir: &std::path::Path| {
        rusqlite::Connection::open(dir.join("tickdesk.sqlite3"))
            .unwrap()
            .query_row(
                "SELECT count(*) FROM transport_outbox
                 WHERE json_extract(frame,'$.payload.kind')='reminder'",
                [],
                |row| row.get::<_, i64>(0),
            )
            .unwrap()
    };
    assert_eq!(reminder_count(dir.path()), 1);

    let details = run(
        &mut db,
        "cycle-details-freeze",
        "get_holding_details",
        json!({"symbol":symbol}),
        now + 2,
    );
    let revision = details["data"]["result"]["holding"]["positionRevision"]
        .as_u64()
        .unwrap();
    let frozen = run(
        &mut db,
        "cycle-freeze",
        "reconcile_holding",
        json!({"symbol":symbol,"basePositionRevision":revision,"qty":600,"cost":"12",
        "asOf":now+1_000,"reason":"all shares frozen","resolvedExecutionIds":[],
        "partitions":[{"qty":600,"restrictions":[{"kind":"freeze","evidenceRef":"fixture:freeze"}],
        "origin":origin}]}),
        now + 1_000,
    );
    assert_eq!(frozen["data"]["ok"], true, "{frozen}");
    let restricted = prepared_run(
        &mut db,
        "cycle-restricted",
        "scan_now",
        json!({}),
        v3_frozen(now + 2_000, true, false),
        now + 2_000,
    );
    assert_eq!(restricted["data"]["result"]["publishedTaskIds"], json!([]));
    assert_eq!(reminder_count(dir.path()), 1);
    let task = run(
        &mut db,
        "cycle-task-restricted",
        "get_task",
        json!({"taskId":first_id}),
        now + 3_000,
    );
    let task = &task["data"]["result"]["task"];
    assert_eq!(task["exit"]["executable"], false, "{task}");
    assert_eq!(task["exit"]["restriction"], "frozen", "{task}");
    assert_eq!(task["exit"]["availableQty"], 0, "{task}");
    assert_eq!(task["status"], "expired", "{task}");
    let details = run(
        &mut db,
        "cycle-details-restricted",
        "get_holding_details",
        json!({"symbol":symbol}),
        now + 4_000,
    );
    assert_eq!(details["data"]["result"]["holding"]["availableQty"], 0);
    let connection = rusqlite::Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();
    let (state, executable, available): (String, bool, Option<u64>) = connection
        .query_row(
            "SELECT json_extract(body,'$.state'),json_extract(body,'$.executable'),
             json_extract(body,'$.availableQty') FROM objects WHERE collection='formal_exit_events'",
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .unwrap();
    assert_eq!(
        (state.as_str(), executable, available),
        ("active", false, Some(0))
    );
    drop(connection);
    drop(db);

    let mut db = Storage::open(dir.path()).unwrap();
    let repeated = prepared_run(
        &mut db,
        "cycle-restricted-after-restart",
        "scan_now",
        json!({}),
        v3_frozen(now + 5_000, true, false),
        now + 5_000,
    );
    assert_eq!(repeated["data"]["result"]["publishedTaskIds"], json!([]));
    assert_eq!(reminder_count(dir.path()), 1);
    let details = run(
        &mut db,
        "cycle-details-unfreeze",
        "get_holding_details",
        json!({"symbol":symbol}),
        now + 6_000,
    );
    let revision = details["data"]["result"]["holding"]["positionRevision"]
        .as_u64()
        .unwrap();
    let released = run(
        &mut db,
        "cycle-release-250",
        "reconcile_holding",
        json!({"symbol":symbol,"basePositionRevision":revision,"qty":600,"cost":"12",
        "asOf":now+4_000_000,"reason":"250 released, 350 still frozen","resolvedExecutionIds":[],
        "partitions":[{"qty":250,"restrictions":[],"origin":origin},
        {"qty":350,"restrictions":[{"kind":"freeze","evidenceRef":"fixture:freeze"}],"origin":origin}]}),
        now + 4_000_000,
    );
    assert_eq!(released["data"]["ok"], true, "{released}");
    let renewed = prepared_run(
        &mut db,
        "cycle-renewed",
        "scan_now",
        json!({}),
        v3_frozen(now + 4_000_001, true, false),
        now + 4_000_001,
    );
    let [renewed_id] = renewed["data"]["result"]["publishedTaskIds"]
        .as_array()
        .unwrap()
        .as_slice()
    else {
        panic!("expected one renewed executable exit: {renewed}");
    };
    let renewed_id = renewed_id.as_str().unwrap().to_owned();
    let renewed_task = run(
        &mut db,
        "cycle-task-2",
        "get_task",
        json!({"taskId":renewed_id}),
        now + 4_000_002,
    );
    let renewed_task = &renewed_task["data"]["result"]["task"];
    assert_eq!(renewed_task["exit"]["executable"], true, "{renewed_task}");
    assert_eq!(renewed_task["exit"]["restriction"], "partially_sellable");
    assert_eq!(renewed_task["exit"]["availableQty"], 250);
    assert_eq!(reminder_count(dir.path()), 2);

    let sale = run(
        &mut db,
        "cycle-partial-sale",
        "record_execution",
        json!({"symbol":symbol,"side":"sell","qty":250,"price":"11.25",
        "executedAt":now+4_100_000}),
        now + 4_100_000,
    );
    assert_eq!(
        sale["data"]["result"]["execution"]["positionEffect"], "applied",
        "{sale}"
    );
    let after_sale = prepared_run(
        &mut db,
        "cycle-after-sale",
        "scan_now",
        json!({}),
        v3_frozen(now + 4_200_000, true, false),
        now + 4_200_000,
    );
    assert_eq!(after_sale["data"]["result"]["publishedTaskIds"], json!([]));
    assert_eq!(reminder_count(dir.path()), 2);
    let task = run(
        &mut db,
        "cycle-task-after-sale",
        "get_task",
        json!({"taskId":renewed_id}),
        now + 4_200_001,
    );
    let task = &task["data"]["result"]["task"];
    assert_eq!(task["exit"]["executable"], false, "{task}");
    assert!(task["exit"]["availableQty"].is_null(), "{task}");
    assert_eq!(task["exit"]["restriction"], "sellable_unknown", "{task}");
    assert_eq!(task["status"], "expired", "{task}");

    let details = run(
        &mut db,
        "cycle-details-zero-baseline",
        "get_holding_details",
        json!({"symbol":symbol}),
        now + 4_300_000,
    );
    let revision = details["data"]["result"]["holding"]["positionRevision"]
        .as_u64()
        .unwrap();
    let released_all = run(
        &mut db,
        "cycle-release-all",
        "reconcile_holding",
        json!({"symbol":symbol,"basePositionRevision":revision,"qty":350,"cost":"12",
        "asOf":now+4_300_000,"reason":"remaining freeze cleared","resolvedExecutionIds":[],
        "partitions":[{"qty":350,"restrictions":[],"origin":origin}]}),
        now + 4_300_000,
    );
    assert_eq!(released_all["data"]["ok"], true, "{released_all}");
    let close = run(
        &mut db,
        "cycle-close",
        "record_execution",
        json!({"symbol":symbol,"side":"sell","qty":350,"price":"11.25",
        "executedAt":now+4_400_000}),
        now + 4_400_000,
    );
    assert_eq!(close["data"]["result"]["position"]["qty"], 0, "{close}");
    let closed = prepared_run(
        &mut db,
        "cycle-closed",
        "scan_now",
        json!({}),
        v3_frozen(now + 4_500_000, true, false),
        now + 4_500_000,
    );
    assert_eq!(closed["data"]["result"]["publishedTaskIds"], json!([]));
    assert_eq!(reminder_count(dir.path()), 2);
    let details = run(
        &mut db,
        "cycle-details-zero",
        "get_holding_details",
        json!({"symbol":symbol}),
        now + 4_500_001,
    );
    assert_eq!(details["data"]["result"]["holding"]["qty"], 0);
    let connection = rusqlite::Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();
    let (state, executable): (String, bool) = connection
        .query_row(
            "SELECT json_extract(body,'$.state'),json_extract(body,'$.executable')
             FROM objects WHERE collection='formal_exit_events'",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    assert_eq!((state.as_str(), executable), ("resolved", false));
}

#[test]
fn one_refuted_exit_strategy_does_not_release_another_verified_reason() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794794400000;
    let symbol = "300502.SZ";
    let origin = json!({"group":"daily_swing","strategyId":"s1","codeHash":"a".repeat(64),
        "parametersHash":"b".repeat(64),"dependenciesHash":"c".repeat(64)});
    let confirmed = run(
        &mut db,
        "multi-exit-holding",
        "reconcile_holding",
        json!({"symbol":symbol,"basePositionRevision":0,"qty":600,"cost":"12","asOf":now,
        "reason":"fixture confirmation","resolvedExecutionIds":[],
        "partitions":[{"qty":600,"restrictions":[],"origin":origin}]}),
        now,
    );
    assert_eq!(confirmed["data"]["ok"], true, "{confirmed}");
    let mut frozen = v3_frozen(now, true, false);
    frozen["frozen"]["manifest"]["strategyHashes"] = json!([
        {"id":"s1","direction":"sell","symbols":[symbol],"weight":"1","group":"daily_swing",
         "codeHash":"a".repeat(64),"parametersHash":"b".repeat(64),"dependenciesHash":"c".repeat(64)},
        {"id":"s2","direction":"sell","symbols":[symbol],"weight":"1","group":"daily_swing",
         "codeHash":"d".repeat(64),"parametersHash":"e".repeat(64),"dependenciesHash":"f".repeat(64)}
    ]);
    frozen["frozen"]["manifest"]["strategyOutcomes"] =
        json!({"s1":{"status":"matched","reasons":[]},"s2":{"status":"matched","reasons":[]}});
    frozen["frozen"]["strategies"] = json!({"total":2,"succeeded":2,"failed":[]});
    frozen["frozen"]["exits"][0]["strategyId"] = json!("s1");
    frozen["frozen"]["exits"][0]["strategyIds"] = json!(["s1", "s2"]);
    let first = prepared_run(&mut db, "multi-exit", "scan_now", json!({}), frozen, now);
    assert_eq!(
        first["data"]["result"]["publishedTaskIds"]
            .as_array()
            .unwrap()
            .len(),
        1,
        "{first}"
    );
    let later = now + 600_000;
    let refute_one = json!({"frozen":{
        "tradeDate":"2026-11-16","isTradingDay":true,"generation":"gen-one","generationAfter":"gen-one",
        "asOf":later,"strategies":{"total":2,"succeeded":2,"failed":[]},"policy":{"minScore":"0"},
        "candidates":[],"exits":[],
        "manifest":{"schemaVersion":3,"strategyHashes":[
            {"id":"s1","direction":"sell","symbols":[symbol],"weight":"1","group":"daily_swing",
             "codeHash":"a".repeat(64),"parametersHash":"b".repeat(64),"dependenciesHash":"c".repeat(64)},
            {"id":"s2","direction":"sell","symbols":[symbol],"weight":"1","group":"daily_swing",
             "codeHash":"d".repeat(64),"parametersHash":"e".repeat(64),"dependenciesHash":"f".repeat(64)}],
        "strategyOutcomes":{"s1":{"status":"no_match","reasons":[]},"s2":{"status":"matched","reasons":[]}},
        "researchDecisions":[{"symbol":symbol,"strategyId":"s1",
            "codeHash":"a".repeat(64),"parametersHash":"b".repeat(64),"dependenciesHash":"c".repeat(64),
            "bar":{"status":"complete","endMs":now+1_000,"availableAtMs":now+1_500},
            "exit":{"value":false,"reason":null}}]}
    }});
    let one = prepared_run(
        &mut db,
        "one-refuted",
        "scan_now",
        json!({}),
        refute_one,
        later,
    );
    assert_eq!(
        one["data"]["result"]["publishedTaskIds"],
        json!([]),
        "{one}"
    );
    let connection = rusqlite::Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();
    let state: String = connection
        .query_row(
            "SELECT json_extract(body,'$.state') FROM objects WHERE collection='formal_exit_events'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(state, "active");

    let refute_both = json!({"frozen":{
        "tradeDate":"2026-11-16","isTradingDay":true,"generation":"gen-both","generationAfter":"gen-both",
        "asOf":later,"strategies":{"total":2,"succeeded":2,"failed":[]},"policy":{"minScore":"0"},
        "candidates":[],"exits":[],
        "manifest":{"schemaVersion":3,"strategyHashes":[
            {"id":"s1","direction":"sell","symbols":[symbol],"weight":"1","group":"daily_swing",
             "codeHash":"a".repeat(64),"parametersHash":"b".repeat(64),"dependenciesHash":"c".repeat(64)},
            {"id":"s2","direction":"sell","symbols":[symbol],"weight":"1","group":"daily_swing",
             "codeHash":"d".repeat(64),"parametersHash":"e".repeat(64),"dependenciesHash":"f".repeat(64)}],
        "strategyOutcomes":{"s1":{"status":"no_match","reasons":[]},"s2":{"status":"no_match","reasons":[]}},
        "researchDecisions":[
            {"symbol":symbol,"strategyId":"s1",
             "codeHash":"a".repeat(64),"parametersHash":"b".repeat(64),"dependenciesHash":"c".repeat(64),
             "bar":{"status":"complete","endMs":now+1_000,"availableAtMs":now+1_500},
             "exit":{"value":false,"reason":null}},
            {"symbol":symbol,"strategyId":"s2",
             "codeHash":"d".repeat(64),"parametersHash":"e".repeat(64),"dependenciesHash":"f".repeat(64),
             "bar":{"status":"complete","endMs":now+1_000,"availableAtMs":now+1_500},
             "exit":{"value":false,"reason":null}}]}
    }});
    prepared_run(
        &mut db,
        "both-refuted",
        "scan_now",
        json!({}),
        refute_both,
        later + 1_000,
    );
    let state: String = connection
        .query_row(
            "SELECT json_extract(body,'$.state') FROM objects WHERE collection='formal_exit_events'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(state, "resolved");
}

#[test]
fn trusted_later_bar_resolves_the_formal_exit_and_publishes_a_buy() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794794400000;
    let symbol = "300502.SZ";
    let origin = json!({"group":"daily_swing","strategyId":"s","codeHash":"a".repeat(64),
        "parametersHash":"b".repeat(64),"dependenciesHash":"c".repeat(64)});
    let confirmed = run(
        &mut db,
        "refute-holding",
        "reconcile_holding",
        json!({"symbol":symbol,"basePositionRevision":0,"qty":600,"cost":"12","asOf":now,
        "reason":"fixture confirmation","resolvedExecutionIds":[],
        "partitions":[{"qty":600,"restrictions":[],"origin":origin}]}),
        now,
    );
    assert_eq!(confirmed["data"]["ok"], true, "{confirmed}");
    let first = prepared_run(
        &mut db,
        "exit-before-refute",
        "scan_now",
        json!({}),
        v3_frozen(now, true, false),
        now,
    );
    assert_eq!(
        first["data"]["result"]["publishedTaskIds"]
            .as_array()
            .unwrap()
            .len(),
        1,
        "{first}"
    );

    let later = now + 600_000;
    let mut refute = v3_frozen(later, false, true);
    refute["frozen"]["manifest"]["researchDecisions"] = json!([{
        "symbol":symbol,"strategyId":"s",
        "codeHash":"a".repeat(64),"parametersHash":"b".repeat(64),
        "dependenciesHash":"c".repeat(64),
        "bar":{"status":"complete","endMs":now+1_000,"availableAtMs":now+1_500},
        "exit":{"value":false,"reason":null}
    }]);
    let scan = prepared_run(
        &mut db,
        "buy-after-refute",
        "scan_now",
        json!({}),
        refute,
        later,
    );
    assert_eq!(scan["data"]["ok"], true, "{scan}");
    let [buy_id] = scan["data"]["result"]["publishedTaskIds"]
        .as_array()
        .unwrap()
        .as_slice()
    else {
        panic!("trusted later bar should release the buy latch: {scan}");
    };
    assert!(
        scan["data"]["result"]["rejectionSummary"]
            .as_array()
            .unwrap()
            .iter()
            .all(|row| row["reasonCode"] != "ACTIVE_EXIT_EXISTS"),
        "{scan}"
    );
    let read = run(
        &mut db,
        "buy-after-refute-task",
        "get_task",
        json!({"taskId":buy_id}),
        later + 1,
    );
    assert_eq!(read["data"]["result"]["task"]["direction"], "buy", "{read}");
}

#[test]
fn ignored_formal_exit_card_and_partial_sale_keep_the_buy_latch() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = 1794794400000;
    let symbol = "300502.SZ";
    let origin = json!({"group":"daily_swing","strategyId":"s","codeHash":"a".repeat(64),
        "parametersHash":"b".repeat(64),"dependenciesHash":"c".repeat(64)});
    let confirmed = run(
        &mut db,
        "latch-holding",
        "reconcile_holding",
        json!({"symbol":symbol,"basePositionRevision":0,"qty":1000,"cost":"12","asOf":now,
        "reason":"fixture confirmation","resolvedExecutionIds":[],
        "partitions":[{"qty":600,"restrictions":[],"origin":origin},
        {"qty":400,"restrictions":[{"kind":"freeze","evidenceRef":"fixture:freeze"}],"origin":origin}]}),
        now,
    );
    assert_eq!(confirmed["data"]["ok"], true, "{confirmed}");
    let first = prepared_run(
        &mut db,
        "latch-exit",
        "scan_now",
        json!({}),
        v3_frozen(now, true, false),
        now,
    );
    let [task_id] = first["data"]["result"]["publishedTaskIds"]
        .as_array()
        .unwrap()
        .as_slice()
    else {
        panic!("expected one restricted exit task: {first}");
    };
    let task_id = task_id.as_str().unwrap().to_owned();
    let ignored = run(
        &mut db,
        "ignore-exit-card",
        "ignore",
        json!({"taskId":task_id,"baseActionVersion":1,"reason":"fixture card ignored"}),
        now + 1_000,
    );
    assert_eq!(ignored["data"]["ok"], true, "{ignored}");
    assert_eq!(ignored["data"]["result"]["task"]["status"], "ignored");

    let after_ignore = prepared_run(
        &mut db,
        "buy-after-ignored-card",
        "scan_now",
        json!({}),
        v3_frozen(now + 2_000, false, true),
        now + 2_000,
    );
    assert_eq!(
        after_ignore["data"]["result"]["publishedTaskIds"],
        json!([]),
        "{after_ignore}"
    );
    assert!(
        after_ignore["data"]["result"]["rejectionSummary"]
            .as_array()
            .unwrap()
            .iter()
            .any(|row| row["reasonCode"] == "ACTIVE_EXIT_EXISTS"),
        "{after_ignore}"
    );

    let details = run(
        &mut db,
        "latch-details",
        "get_holding_details",
        json!({"symbol":symbol}),
        now + 3_000,
    );
    let revision = details["data"]["result"]["holding"]["positionRevision"]
        .as_u64()
        .unwrap();
    let cleared = run(
        &mut db,
        "clear-freeze",
        "reconcile_holding",
        json!({"symbol":symbol,"basePositionRevision":revision,"qty":1000,"cost":"12",
        "asOf":now+3_100,"reason":"freeze evidence cleared","resolvedExecutionIds":[],
        "partitions":[{"qty":1000,"restrictions":[],"origin":origin}]}),
        now + 3_100,
    );
    assert_eq!(cleared["data"]["ok"], true, "{cleared}");
    let sale = run(
        &mut db,
        "partial-sale",
        "record_execution",
        json!({"symbol":symbol,"side":"sell","qty":400,"price":"11.2500",
            "executedAt":now+4_000}),
        now + 4_000,
    );
    assert_eq!(
        sale["data"]["result"]["execution"]["positionEffect"], "applied",
        "{sale}"
    );
    assert_eq!(sale["data"]["result"]["position"]["qty"], 600);
    let after_sale = prepared_run(
        &mut db,
        "buy-after-partial-sale",
        "scan_now",
        json!({}),
        v3_frozen(now + 5_000, false, true),
        now + 5_000,
    );
    assert_eq!(
        after_sale["data"]["result"]["publishedTaskIds"],
        json!([]),
        "{after_sale}"
    );
    assert!(
        after_sale["data"]["result"]["rejectionSummary"]
            .as_array()
            .unwrap()
            .iter()
            .any(|row| row["reasonCode"] == "ACTIVE_EXIT_EXISTS"),
        "{after_sale}"
    );
}
