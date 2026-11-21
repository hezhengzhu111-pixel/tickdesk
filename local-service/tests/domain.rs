mod support;

use serde_json::{json, Value};
use support::{command, deliver, LOCAL_PAIR, PEER};
use tickdesk_local::storage::Storage;

fn setup(dir: &std::path::Path) -> Storage {
    let mut db = Storage::open(dir).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    db
}

fn run(db: &mut Storage, key: &str, name: &str, params: Value, now: u64) -> Value {
    let input = command(db, &format!("event-{key}"), key, name, params);
    db.receive(&deliver(db, PEER, key, &input)).unwrap();
    db.process(db.job(key).unwrap().unwrap(), now)
        .unwrap()
        .unwrap()
}

#[test]
fn message_cursor_reads_its_frozen_page_without_reloading_current_history() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = setup(dir.path());
    for (key, at) in [("first", 100), ("second", 101)] {
        assert_eq!(
            run(&mut db, key, "chat", json!({"text":key}), at)["data"]["ok"],
            true
        );
    }
    let page = run(&mut db, "page-one", "get_messages", json!({"limit":1}), 102);
    let cursor = page["data"]["result"]["nextCursor"].clone();
    assert!(cursor.is_string());
    // A frozen continuation is independent of newly added current-history rows.
    // An unreadable new row detects accidental re-querying without timing assertions.
    let connection = rusqlite::Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();
    connection
        .execute(
            "INSERT INTO objects VALUES ('messages','unreadable-later-row','not-json')",
            [],
        )
        .unwrap();
    let page = run(
        &mut db,
        "page-two",
        "get_messages",
        json!({"limit":1,"cursor":cursor}),
        103,
    );
    assert_eq!(page["data"]["ok"], true);
    assert_eq!(page["data"]["result"]["items"][0]["text"], "first");
    assert!(page["data"]["result"]["nextCursor"].is_null());
}

#[test]
fn business_writes_rollback_with_ack_and_concurrent_adjustments_require_current_revision() {
    let tmp = tempfile::tempdir().unwrap();
    let mut db = setup(tmp.path());
    let confirmation = json!({"symbol":"300502.SZ","basePositionRevision":0,"qty":100,
        "availableQty":90,"pendingLots":[{"qty":10,"sellableDate":"2026-11-17"}],
        "cost":"12.001","asOf":1794792300000_u64,"reason":"confirmed inventory","resolvedExecutionIds":[]});
    let input = command(
        &db,
        "first-adjustment",
        "adjustment",
        "adjust_position",
        confirmation.clone(),
    );
    let d = deliver(&db, PEER, "adjustment", &input);
    db.receive(&d).unwrap();
    let work = db.job("adjustment").unwrap().unwrap();
    let fault = rusqlite::Connection::open(tmp.path().join("tickdesk.sqlite3")).unwrap();
    fault.execute_batch("CREATE TRIGGER fail_business_outbox BEFORE INSERT ON transport_outbox BEGIN SELECT RAISE(ABORT,'fault'); END;").unwrap();
    assert!(db.process(work.clone(), 1794792300000).is_err());
    fault
        .execute_batch("DROP TRIGGER fail_business_outbox")
        .unwrap();
    let ack = db.process(work, 1794792300000).unwrap().unwrap();
    assert_eq!(ack["data"]["result"]["positionRevision"], 1);
    assert_eq!(ack["data"]["result"]["adjustment"]["before"], Value::Null);
    assert_eq!(
        ack["data"]["result"]["adjustment"]["after"]["cost"],
        "12.001"
    );
    let other = run(
        &mut db,
        "stale",
        "adjust_position",
        confirmation,
        1794792300001,
    );
    assert_eq!(other["data"]["error"]["code"], "POSITION_VERSION_CONFLICT");
    let replay = support::status_query(
        &mut db,
        PEER,
        "after-crash",
        input["serverEpoch"].as_str().unwrap(),
        &input,
        None,
    );
    assert_eq!(replay["data"]["result"]["ackEventId"], ack["id"]);
}

#[test]
fn oversell_records_fact_without_partial_position_change_and_sync_hashes_authoritative_view() {
    let tmp = tempfile::tempdir().unwrap();
    let mut db = setup(tmp.path());
    let now = 1794792300000_u64;
    let base = run(
        &mut db,
        "base",
        "adjust_position",
        json!({"symbol":"300502.SZ","basePositionRevision":0,
        "qty":100,"availableQty":50,"pendingLots":[{"qty":50,"sellableDate":"2026-11-17"}],"cost":"12.001",
        "asOf":now,"reason":"report","resolvedExecutionIds":[]}),
        now,
    );
    assert_eq!(base["data"]["ok"], true);
    let fill = run(
        &mut db,
        "fill",
        "record_execution",
        json!({"symbol":"300502.SZ","side":"sell",
       "price":"14.0000","qty":75,"executedAt":now,"priceBasis":"average"}),
        now,
    );
    assert_eq!(fill["data"]["ok"], true);
    let result = &fill["data"]["result"];
    assert_eq!(
        result["execution"]["positionEffect"],
        "needs_reconciliation"
    );
    assert_eq!(result["execution"]["price"], "14.0000");
    assert_eq!(result["position"]["qty"], 100);
    assert_eq!(result["position"]["availableQty"], 50);
    assert_eq!(result["execution"]["ruleSnapshot"], Value::Null);
    let ack = run(&mut db, "sync", "sync", json!({}), now);
    let page_id = &ack["data"]["result"]["pageEventId"];
    let page = db
        .outbox(16)
        .unwrap()
        .into_iter()
        .map(|d| d["payload"].clone())
        .find(|event| event["id"] == *page_id)
        .unwrap();
    assert_eq!(page["data"]["positions"][0]["reconciliationRequired"], true);
    for k in [
        "positions",
        "tasks",
        "executions",
        "watchlist",
        "imports",
        "adjustments",
        "valuationPoints",
        "meta",
    ] {
        let items = if k == "meta" {
            json!([page["data"][k]])
        } else {
            page["data"][k].clone()
        };
        assert_eq!(
            tickdesk_local::contract::digest(&items).unwrap(),
            page["data"]["manifest"][k]["sha256"],
            "{k}"
        );
    }
}

#[test]
fn prior_session_sellability_cannot_apply_a_fill_without_current_evidence() {
    let tmp = tempfile::tempdir().unwrap();
    let mut db = setup(tmp.path());
    let previous_session = 1_794_792_300_000_u64;
    let current_session = previous_session + 86_400_000;
    let base = run(
        &mut db,
        "base",
        "adjust_position",
        json!({"symbol":"300502.SZ","basePositionRevision":0,"qty":1500,
            "availableQty":700,"pendingLots":[{"qty":800,"sellableDate":"2026-11-17"}],
            "cost":"12.001","asOf":previous_session,"reason":"confirmed inventory",
            "resolvedExecutionIds":[]}),
        previous_session,
    );
    assert_eq!(base["data"]["ok"], true);
    let submission = command(
        &db,
        "fill-event",
        "fill",
        "record_execution",
        json!({"symbol":"300502.SZ","side":"sell","price":"14.0010","qty":200,
            "executedAt":current_session}),
    );
    db.receive(&deliver(&db, PEER, "fill", &submission))
        .unwrap();
    let ack = db
        .process(db.job("fill").unwrap().unwrap(), current_session)
        .unwrap()
        .unwrap();
    let result = &ack["data"]["result"];
    assert_eq!(
        result["execution"]["positionEffect"],
        "needs_reconciliation"
    );
    assert_eq!(result["execution"]["executedAt"], current_session);
    assert_eq!(result["execution"]["price"], "14.0010");
    assert_eq!(result["execution"]["qty"], 200);
    assert_eq!(result["position"]["qty"], 1500);
    assert_eq!(result["position"]["reconciliationRequired"], true);

    drop(db);
    let mut db = setup(tmp.path());
    let duplicate = db
        .receive(&deliver(&db, PEER, "fill-retry", &submission))
        .unwrap();
    let retry = db
        .process(
            db.job(duplicate["deliveryId"].as_str().unwrap())
                .unwrap()
                .unwrap(),
            current_session,
        )
        .unwrap()
        .unwrap();
    assert_eq!(retry, ack);
    let conn = rusqlite::Connection::open(tmp.path().join("tickdesk.sqlite3")).unwrap();
    let count: u64 = conn
        .query_row(
            "SELECT count(*) FROM objects WHERE collection='executions'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(count, 1);
}

const FRIDAY: u64 = 1_795_138_200_000;
const MONDAY: u64 = 1_795_397_400_000;

fn holding_rule(at: u64, date: &str) -> Value {
    json!({"ruleAvailableAtMs":at,"rule":{
        "ruleSnapshotId":format!("synthetic-{date}"),"symbol":"300502.SZ","tradeDate":date,
        "channel":"auction","exchange":"SZSE","instrumentType":"stock","board":"chinext",
        "dayTrading":"NO","settlement":"T+1","quantityUnit":100,"maxOrderQty":100000,
        "minPriceTick":"0.01","limitRatio":"0.2","adjustedPrevClose":"10","referencePrice":"10",
        "upperLimit":"12","lowerLimit":"8","riskStatus":"normal","tradingStatus":"trading",
        "ruleVersion":"synthetic-rules-v1","sources":[{"name":"合成","ref":"test:effective-rule","asOf":at}],
        "sourceTime":at,"dataStatus":"complete","issueFields":[]
    },"calendar":{
        "revisionId":"synthetic-calendar-v1","source":{"name":"合成","ref":"test:calendar","asOf":FRIDAY-900000},
        "availableAtMs":FRIDAY-900000,"coverageStart":"2026-11-20","coverageEnd":"2026-11-23",
        "sessions":[{"date":"2026-11-20","firstExecutionAtMs":FRIDAY-900000},
            {"date":"2026-11-23","firstExecutionAtMs":MONDAY-900000}]
    },"companyActionsCheckedThrough":date})
}

fn prepare_holding_rule(db: &mut Storage, key: &str, rule: Value, now: u64) {
    prepare_holding_rule_batch(db, key, json!({"version":3,"rules":[rule]}), now);
}

#[test]
fn reads_missing_history_without_claiming_zero_market_coverage() {
    let temp = tempfile::tempdir().unwrap();
    let mut db = setup(temp.path());
    let ack = run(
        &mut db,
        "history-coverage",
        "get_history_coverage",
        json!({}),
        FRIDAY,
    );
    assert_eq!(ack["data"]["ok"], true);
    assert_eq!(
        ack["data"]["result"],
        json!({"status":"unavailable","checkedAt":null,
        "coverage":null,"reason":"history_pending"})
    );
}

#[test]
fn aging_history_is_labelled_stale_and_scoped_to_the_current_epoch() {
    let temp = tempfile::tempdir().unwrap();
    let mut db = setup(temp.path());
    let sql = rusqlite::Connection::open(temp.path().join("tickdesk.sqlite3")).unwrap();
    let snapshot = json!({"status":"partial","checkedAt":FRIDAY,"reason":null,"coverage":{
        "source":"fuyao","start":"2026-01-20","through":"2026-11-19","expectedThrough":"2026-11-19",
        "updatedAt":FRIDAY-1,"rows":11,"observedSymbols":5,"requestedSymbols":6,"conflictingRows":0,
        "latestDay":{"observed":4,"expected":6}}});
    let store = |epoch: &str| {
        sql.execute("INSERT OR REPLACE INTO objects(collection,object_id,body) VALUES ('market_meta','history_coverage',?1)",
                    [json!({"epoch":epoch,"result":snapshot}).to_string()]).unwrap();
    };
    store(&db.identity.server_epoch);
    let ack = run(
        &mut db,
        "fresh-data",
        "get_history_coverage",
        json!({}),
        FRIDAY + 90_000,
    );
    assert_eq!(ack["data"]["result"], snapshot);
    let ack = run(
        &mut db,
        "stale-data",
        "get_history_coverage",
        json!({}),
        FRIDAY + 90_001,
    );
    assert_eq!(ack["data"]["result"]["status"], "unavailable");
    assert_eq!(ack["data"]["result"]["coverage"], snapshot["coverage"]);
    store("old-epoch");
    let ack = run(
        &mut db,
        "old-data",
        "get_history_coverage",
        json!({}),
        FRIDAY + 90_002,
    );
    assert_eq!(ack["data"]["result"]["reason"], "history_pending");
    assert!(ack["data"]["result"]["coverage"].is_null());
}

fn prepare_holding_rule_batch(db: &mut Storage, key: &str, batch: Value, now: u64) {
    let cmd = command(db, key, key, "scan_now", json!({}));
    let request = tickdesk_local::market::capture(db, &cmd["data"], now).unwrap();
    db.receive(&deliver(db, PEER, key, &cmd)).unwrap();
    db.process_prepared(
        db.job(key).unwrap().unwrap(),
        now,
        Some(json!({"request":request,"response":{"holdingRuleBatch":batch}})),
    )
    .unwrap()
    .unwrap();
}

#[test]
fn sourced_suspension_opens_one_risk_episode_even_without_quant_signals() {
    let temp = tempfile::tempdir().unwrap();
    let mut db = setup(temp.path());
    confirm_inventory(&mut db, 100, 100, FRIDAY - 2_000);
    let verified = |at, status| {
        let mut rule = holding_rule(at, "2026-11-20");
        rule["rule"]["ruleSnapshotId"] = json!(format!("fixture-{at}"));
        rule["rule"]["tradingStatus"] = json!(status);
        rule
    };
    let current_task = |db: &mut Storage, key: &str, event: &Value, at| {
        run(db, key, "get_task", json!({"taskId":event["taskId"]}), at)["data"]["result"]["task"]
            .clone()
    };
    let persisted_risks = || {
        let reader = rusqlite::Connection::open(temp.path().join("tickdesk.sqlite3")).unwrap();
        let mut statement = reader
            .prepare("SELECT body FROM objects WHERE collection='tasks' AND json_extract(body,'$.direction')='risk' ORDER BY json_extract(body,'$.createdAt')")
            .unwrap();
        statement
            .query_map([], |row| row.get::<_, String>(0))
            .unwrap()
            .map(|body| serde_json::from_str::<Value>(&body.unwrap()).unwrap())
            .collect::<Vec<_>>()
    };

    prepare_holding_rule(
        &mut db,
        "confirmed-suspension",
        verified(FRIDAY, "suspended"),
        FRIDAY,
    );
    let risks = persisted_risks();
    assert_eq!(risks.len(), 1);
    let first = &risks[0];
    assert!(first["riskEventKey"].is_string());
    assert_eq!(first["score"], Value::Null);
    assert_eq!(first["reasons"]["verificationStatus"], "verified");
    assert_eq!(
        first["reasons"]["riskEvidence"][0]["sourceRef"],
        "test:effective-rule"
    );
    assert!(first["nextReminderAt"].is_null());
    assert!(db
        .outbox(32)
        .unwrap()
        .iter()
        .any(|row| row["payload"]["kind"] == "reminder"
            && row["payload"]["data"]["taskId"] == first["taskId"]));
    let ignored = run(
        &mut db,
        "ignore-suspension",
        "ignore",
        json!({"taskId":first["taskId"],"baseActionVersion":1}),
        FRIDAY + 500,
    );
    assert_eq!(ignored["data"]["ok"], true, "{ignored}");
    drop(db);
    let mut db = setup(temp.path());
    prepare_holding_rule(
        &mut db,
        "still-suspended",
        verified(FRIDAY + 1_000, "suspended"),
        FRIDAY + 1_000,
    );
    assert_eq!(persisted_risks().len(), 1);
    assert_eq!(
        current_task(&mut db, "ignored-task-still-closed", first, FRIDAY + 1_001)["status"],
        "ignored"
    );

    prepare_holding_rule(
        &mut db,
        "resumed",
        verified(FRIDAY + 2_000, "trading"),
        FRIDAY + 2_000,
    );
    prepare_holding_rule(
        &mut db,
        "suspension-recurred",
        verified(FRIDAY + 3_000, "suspended"),
        FRIDAY + 3_000,
    );
    let rows = persisted_risks();
    assert_eq!(rows.len(), 2);
    assert_ne!(rows[1]["riskEventKey"], first["riskEventKey"]);
    prepare_holding_rule_batch(
        &mut db,
        "withdraw-suspension",
        json!({
            "version":3,"rules":[],"unavailable":[{"symbol":"300502.SZ","tradeDate":"2026-11-20","availableAtMs":FRIDAY+3_100,"reason":"withdrawn"}]
        }),
        FRIDAY + 3_100,
    );
    let withdrawn = current_task(&mut db, "expired-by-withdrawal", &rows[1], FRIDAY + 3_101);
    assert_eq!(withdrawn["status"], "expired");
    assert!(withdrawn["currentReminder"].is_null());
    prepare_holding_rule(
        &mut db,
        "resumed-again",
        verified(FRIDAY + 4_000, "trading"),
        FRIDAY + 4_000,
    );
    let closed = current_task(&mut db, "expired-by-resume", &rows[1], FRIDAY + 4_001);
    assert_eq!(closed["status"], "expired");
    assert!(closed["currentReminder"].is_null());
    let episodes = rusqlite::Connection::open(temp.path().join("tickdesk.sqlite3")).unwrap();
    assert_eq!(episodes.query_row("SELECT COUNT(*) FROM objects WHERE collection='market_suspension_heads' AND json_extract(body,'$.active')=0", [], |row| row.get::<_,u32>(0)).unwrap(), 1);
    let reader = rusqlite::Connection::open(temp.path().join("tickdesk.sqlite3")).unwrap();
    assert_eq!(
        reader
            .query_row(
                "SELECT COUNT(*) FROM objects WHERE collection='executions'",
                [],
                |row| row.get::<_, u32>(0)
            )
            .unwrap(),
        0
    );
}

#[test]
fn official_suspension_is_usable_without_guessing_any_other_rule_axis() {
    let temp = tempfile::tempdir().unwrap();
    let mut db = setup(temp.path());
    let symbol = "605255.SH";
    let inventory = run(
        &mut db,
        "shanghai-inventory",
        "adjust_position",
        json!({
            "symbol":symbol,"basePositionRevision":0,"qty":100,"availableQty":100,
            "pendingLots":[],"cost":"10","asOf":FRIDAY-1_000,"reason":"fixture holding","resolvedExecutionIds":[],
        }),
        FRIDAY - 1_000,
    );
    assert_eq!(inventory["data"]["ok"], true);
    let observed = FRIDAY + 1_000;
    let mut partial = holding_rule(observed, "2026-11-20");
    let risk = partial["rule"].as_object_mut().unwrap();
    risk.extend(json!({
        "symbol":symbol,"exchange":"SSE","board":"main",
        "settlement":"UNKNOWN","dayTrading":"UNKNOWN","quantityUnit":null,"maxOrderQty":null,
        "minPriceTick":null,"limitRatio":null,"adjustedPrevClose":null,"referencePrice":null,
        "upperLimit":null,"lowerLimit":null,"riskStatus":"unknown","tradingStatus":"suspended",
        "dataStatus":"missing","issueFields":["settlement","priceLimits","riskStatus"],
            "sources":[{"name":"上交所停复牌信息","ref":"https://www.sse.com.cn/disclosure/dealinstruc/suspension/stock/#605255.SH-20261116","asOf":observed}],
    }).as_object().unwrap().clone());
    partial["companyActionsCheckedThrough"] = Value::Null;
    let mut invalid = partial.clone();
    invalid["rule"]["sources"][0]["ref"] = json!("https://unverified.invalid/605255.SH-20261116");
    prepare_holding_rule(&mut db, "unsupported-partial", invalid, observed);
    let reader = rusqlite::Connection::open(temp.path().join("tickdesk.sqlite3")).unwrap();
    let count_risks = || {
        reader
            .query_row(
                "SELECT COUNT(*) FROM objects WHERE collection='tasks'",
                [],
                |row| row.get::<_, i32>(0),
            )
            .unwrap()
    };
    assert_eq!(count_risks(), 0);
    prepare_holding_rule(&mut db, "sse-suspension", partial, observed + 1);
    assert_eq!(count_risks(), 1);
    let declared = run(
        &mut db,
        "declared-sell",
        "record_execution",
        json!({
            "symbol":symbol,"side":"sell","qty":50,"price":"9","executedAt":FRIDAY+2,"channel":"auction",
        }),
        observed + 3,
    );
    let execution = &declared["data"]["result"]["execution"];
    assert_eq!(execution["positionEffect"], "needs_reconciliation");
    assert_eq!(
        execution["warnings"][0]["code"],
        "EXECUTION_DURING_SUSPENSION"
    );
    assert_eq!(execution["ruleSnapshot"]["dataStatus"], "missing");
    assert_eq!(execution["settlement"], Value::Null);
    assert_eq!(declared["data"]["result"]["position"]["qty"], 100);
}

#[test]
fn shenzhen_cancellation_retires_the_partial_suspension_without_claiming_resumption() {
    let temp = tempfile::tempdir().unwrap();
    let mut db = setup(temp.path());
    confirm_inventory(&mut db, 100, 100, FRIDAY - 1000);
    let symbol = "300502.SZ";
    let read_status = |db: &mut Storage, key, at| {
        run(db, key, "get_quote_status", json!({"symbols":[symbol]}), at)["data"]["result"]["items"]
            [0]["tradingStatus"]
            .clone()
    };
    let partial = |at: u64, action: &str, day: &str| {
        let mut evidence = holding_rule(at, "2026-11-20");
        let withdrawing = action == "withdrawal";
        evidence["rule"].as_object_mut().unwrap().extend(json!({
            "ruleSnapshotId":format!("sz-memo-{action}-{at}"), "settlement":"UNKNOWN","dayTrading":"UNKNOWN",
            "quantityUnit":null,"maxOrderQty":null,"minPriceTick":null,"limitRatio":null,"adjustedPrevClose":null,
            "referencePrice":null,"upperLimit":null,"lowerLimit":null,"riskStatus":"unknown",
            "dataStatus":"missing", "tradingStatus":if withdrawing {"unknown"} else {"suspended"},
            "issueFields":if withdrawing {json!(["settlement","priceLimits","riskStatus","tradingStatus"])}
               else {json!(["settlement","priceLimits","riskStatus"])},
            "sources":[{"name":"深圳证券交易所停复牌提示",
                "ref":format!("https://www.szse.cn/disclosure/memo/index.html#{action}-{symbol}-{day}"),"asOf":at}],
        }).as_object().unwrap().clone());
        evidence["companyActionsCheckedThrough"] = Value::Null;
        evidence
    };
    let early = partial(FRIDAY - 600_000, "suspension", "20261120");
    prepare_holding_rule(&mut db, "early", early, FRIDAY);
    assert_eq!(read_status(&mut db, "before-valid", FRIDAY), "unknown");
    let rejected = partial(FRIDAY + 1, "suspension", "20261119");
    prepare_holding_rule(&mut db, "wrong-event-day", rejected, FRIDAY + 1);
    assert_eq!(read_status(&mut db, "bad-ref-day", FRIDAY + 1), "unknown");
    let old = holding_rule(FRIDAY + 1, "2026-11-20");
    prepare_holding_rule(&mut db, "prior-full-trading", old, FRIDAY + 1);
    let valid = partial(FRIDAY + 2, "suspension", "20261120");
    prepare_holding_rule(&mut db, "memo-suspension", valid.clone(), FRIDAY + 2);
    assert_eq!(
        read_status(&mut db, "physical-quote-suspended", FRIDAY + 3),
        "suspended"
    );
    let conflicting = partial(FRIDAY + 2, "withdrawal", "20261120");
    prepare_holding_rule(&mut db, "conflicting-memo", conflicting, FRIDAY + 3);
    assert_eq!(
        read_status(&mut db, "conflicting-is-unknown", FRIDAY + 3),
        "unknown"
    );
    let marker = partial(FRIDAY + 4, "withdrawal", "20261120");
    prepare_holding_rule(&mut db, "cancelled-memo", marker, FRIDAY + 4);
    assert_eq!(
        read_status(&mut db, "cancelled-is-unknown", FRIDAY + 5),
        "unknown"
    );
    prepare_holding_rule(&mut db, "late-old-memo", valid, FRIDAY + 6);
    assert_eq!(
        read_status(&mut db, "no-old-warning", FRIDAY + 6),
        "unknown"
    );
    let sql = rusqlite::Connection::open(temp.path().join("tickdesk.sqlite3")).unwrap();
    let tasks: (i64, i64) = sql
        .query_row(
            "SELECT count(*),sum(json_extract(body,'$.status')='expired' AND
        json_type(body,'$.currentReminder')='null') FROM objects WHERE collection='tasks'",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    assert_eq!(tasks, (1, 1));
    let mut delayed_full = holding_rule(FRIDAY + 1, "2026-11-20");
    delayed_full["rule"]["ruleSnapshotId"] = json!("late-older-full-trading");
    delayed_full["ruleAvailableAtMs"] = json!(FRIDAY + 7);
    prepare_holding_rule(&mut db, "late-full-trading", delayed_full, FRIDAY + 7);
    assert_eq!(
        read_status(&mut db, "late-full-is-stale", FRIDAY + 7),
        "unknown"
    );
    let mut resumed = holding_rule(FRIDAY + 8, "2026-11-20");
    resumed["rule"]["ruleSnapshotId"] = json!("verified-after-cancellation");
    prepare_holding_rule(&mut db, "newer-confirmed-trading", resumed, FRIDAY + 8);
    assert_eq!(
        read_status(&mut db, "current-confirmed-trading", FRIDAY + 8),
        "trading"
    );
}

#[test]
fn auction_conflict_leaves_independent_settlement_usable() {
    let temp = tempfile::tempdir().unwrap();
    let mut db = setup(temp.path());
    confirm_inventory(&mut db, 100, 100, FRIDAY - 1000);
    let auction = holding_rule(FRIDAY, "2026-11-20");
    let mut fixed = auction.clone();
    fixed["rule"]["channel"] = json!("after_hours_fixed");
    prepare_holding_rule_batch(
        &mut db,
        "independent-full-rules",
        json!({"version":3,"rules":[auction,fixed]}),
        FRIDAY,
    );
    prepare_holding_rule_batch(
        &mut db,
        "auction-only-conflict",
        json!({"version":3,"rules":[],"unavailable":[{
            "symbol":"300502.SZ","tradeDate":"2026-11-20","availableAtMs":FRIDAY+1,"reason":"conflict","scope":"auctionStatus"
        }]}),
        FRIDAY + 1,
    );
    let quote = run(
        &mut db,
        "auction-status-after-conflict",
        "get_quote_status",
        json!({"symbols":["300502.SZ"]}),
        FRIDAY + 2,
    );
    assert_eq!(
        quote["data"]["result"]["items"][0]["tradingStatus"],
        "unknown"
    );
    let fill = run(
        &mut db,
        "fixed-price-fill",
        "record_execution",
        json!({
            "symbol":"300502.SZ","side":"buy","qty":100,"price":"10","executedAt":FRIDAY+3,
            "channel":"after_hours_fixed"
        }),
        FRIDAY + 3,
    );
    assert_eq!(fill["data"]["result"]["execution"]["settlement"], "T+1");
    assert_eq!(fill["data"]["result"]["position"]["qty"], 200);
}

fn confirm_inventory(db: &mut Storage, quantity: u64, available: u64, at: u64) {
    let confirmation = run(
        db,
        "baseline",
        "adjust_position",
        json!({"symbol":"300502.SZ","basePositionRevision":0,"qty":quantity,"availableQty":available,
            "pendingLots":(quantity == available).then(Vec::<Value>::new),"cost":"10","asOf":at,
            "confirmZero":quantity == 0,"reason":"confirmed inventory","resolvedExecutionIds":[]}),
        at,
    );
    assert_eq!(confirmation["data"]["ok"], true);
}

fn trade(db: &mut Storage, key: &str, side: &str, qty: u64, price: &str, now: u64) -> Value {
    let ack = run(
        db,
        key,
        "record_execution",
        json!({"symbol":"300502.SZ","side":side,"qty":qty,"price":price,"executedAt":now,"channel":"auction"}),
        now,
    );
    assert_eq!(ack["data"]["ok"], true);
    ack["data"]["result"].clone()
}

#[test]
fn verified_friday_buy_releases_only_on_rechecked_monday_session_after_restart() {
    let tmp = tempfile::tempdir().unwrap();
    let mut db = setup(tmp.path());
    confirm_inventory(&mut db, 1000, 1000, FRIDAY);
    prepare_holding_rule(
        &mut db,
        "friday-rule",
        holding_rule(FRIDAY, "2026-11-20"),
        FRIDAY,
    );
    let buy = trade(&mut db, "buy", "buy", 500, "10", FRIDAY + 1000);
    assert_eq!(buy["execution"]["positionEffect"], "applied");
    assert_eq!(buy["execution"]["settlement"], "T+1");
    assert_eq!(buy["execution"]["settlementSource"], "snapshot");
    assert_eq!(buy["execution"]["sellableDate"], "2026-11-23");
    assert_eq!(buy["position"]["qty"], 1500);
    assert_eq!(buy["position"]["availableQty"], 1000);
    assert_eq!(
        buy["position"]["pendingLots"],
        json!([{"qty":500,"sellableDate":"2026-11-23"}])
    );
    let sell_old = trade(&mut db, "old-sale", "sell", 300, "10", FRIDAY + 2000);
    assert_eq!(sell_old["position"]["availableQty"], 700);
    assert_eq!(sell_old["position"]["qty"], 1200);

    drop(db);
    let mut db = setup(tmp.path());
    prepare_holding_rule(
        &mut db,
        "monday-rule",
        holding_rule(MONDAY, "2026-11-23"),
        MONDAY,
    );
    let rechecked = trade(
        &mut db,
        "post-settlement",
        "sell",
        1000,
        "10",
        MONDAY + 1000,
    );
    assert_eq!(rechecked["execution"]["positionEffect"], "applied");
    assert_eq!(rechecked["position"]["qty"], 200);
    assert_eq!(rechecked["position"]["availableQty"], 200);
    assert_eq!(rechecked["position"]["pendingLots"], json!([]));
}

#[test]
fn actual_fill_outside_verified_price_band_keeps_price_and_baseline() {
    let tmp = tempfile::tempdir().unwrap();
    let mut db = setup(tmp.path());
    confirm_inventory(&mut db, 1000, 1000, FRIDAY);
    prepare_holding_rule(
        &mut db,
        "friday-rule",
        holding_rule(FRIDAY, "2026-11-20"),
        FRIDAY,
    );
    let reported = trade(
        &mut db,
        "quote-conflict",
        "sell",
        200,
        "7.999",
        FRIDAY + 1000,
    );
    assert_eq!(
        reported["execution"]["positionEffect"],
        "needs_reconciliation"
    );
    assert_eq!(reported["execution"]["ruleStatus"], "conflict");
    assert_eq!(reported["execution"]["price"], "7.999");
    assert_eq!(reported["execution"]["qty"], 200);
    assert_eq!(reported["position"]["qty"], 1000);
}

#[test]
fn later_rule_availability_is_not_used_to_apply_an_earlier_reported_fill() {
    let tmp = tempfile::tempdir().unwrap();
    let mut db = setup(tmp.path());
    confirm_inventory(&mut db, 1000, 1000, FRIDAY);
    let mut late_rule = holding_rule(FRIDAY, "2026-11-20");
    late_rule["ruleAvailableAtMs"] = json!(FRIDAY + 2000);
    prepare_holding_rule(&mut db, "late-rules", late_rule, FRIDAY + 2000);
    let ack = run(
        &mut db,
        "reported-buy",
        "record_execution",
        json!({"symbol":"300502.SZ","side":"buy","qty":500,"price":"10.000",
            "executedAt":FRIDAY+1000,"channel":"auction"}),
        FRIDAY + 3000,
    );
    let result = &ack["data"]["result"];
    assert_eq!(result["execution"]["price"], "10.000");
    assert_eq!(result["execution"]["qty"], 500);
    assert_eq!(
        result["execution"]["positionEffect"],
        "needs_reconciliation"
    );
    assert_eq!(result["execution"]["ruleStatus"], "partial");
    assert_eq!(result["execution"]["sellableDate"], Value::Null);
    assert_eq!(result["position"]["qty"], 1000);
}

#[test]
fn dated_rule_withdrawal_invalidates_same_day_cache_but_not_a_newer_revision() {
    let tmp = tempfile::tempdir().unwrap();
    let mut db = setup(tmp.path());
    confirm_inventory(&mut db, 1000, 1000, FRIDAY);
    prepare_holding_rule(
        &mut db,
        "friday-rule",
        holding_rule(FRIDAY, "2026-11-20"),
        FRIDAY,
    );
    let withdrawal = json!({"version":3,"rules":[],"unavailable":[{"symbol":"300502.SZ",
        "tradeDate":"2026-11-20","availableAtMs":FRIDAY+1000,"reason":"withdrawn"}]});
    prepare_holding_rule_batch(&mut db, "withdrawn", withdrawal.clone(), FRIDAY + 1000);
    let detail = holding_details(&mut db, "withdrawn-details", FRIDAY + 1000);
    assert_eq!(detail["availableQty"], Value::Null);

    let mut restored = holding_rule(FRIDAY + 2000, "2026-11-20");
    restored["rule"]["ruleSnapshotId"] = json!("new-effective-rules");
    prepare_holding_rule(&mut db, "restored-rule", restored, FRIDAY + 2000);
    prepare_holding_rule_batch(&mut db, "late-withdrawal", withdrawal, FRIDAY + 3000);
    let buy = trade(
        &mut db,
        "buy-after-restoration",
        "buy",
        500,
        "10",
        FRIDAY + 3000,
    );
    assert_eq!(buy["execution"]["positionEffect"], "applied");
    assert_eq!(buy["position"]["qty"], 1500);
}

fn reconcile_partitions(
    db: &mut Storage,
    base_revision: u64,
    quantity: u64,
    partitions: Value,
) -> Value {
    run(
        db,
        "reconcile-v3",
        "reconcile_holding",
        json!({"symbol":"300502.SZ","basePositionRevision":base_revision,"qty":quantity,"cost":"10",
            "asOf":FRIDAY,"reason":"确认券商持仓和限制","resolvedExecutionIds":[],"partitions":partitions}),
        FRIDAY,
    )
}

fn holding_details(db: &mut Storage, key: &str, now: u64) -> Value {
    let ack = run(
        db,
        key,
        "get_holding_details",
        json!({"symbol":"300502.SZ"}),
        now,
    );
    assert_eq!(ack["data"]["ok"], true);
    ack["data"]["result"]["holding"].clone()
}

#[test]
fn ordinary_cost_correction_preserves_broker_restrictions_origin_and_snapshot_clock() {
    let tmp = tempfile::tempdir().unwrap();
    let mut db = setup(tmp.path());
    let origin = json!({"group":"daily_swing","strategyId":"first",
        "codeHash":"a".repeat(64),"parametersHash":"b".repeat(64),"dependenciesHash":"c".repeat(64)});
    let partitions = json!([
        {"qty":700,"restrictions":[],"origin":origin},
        {"qty":300,"restrictions":[{"kind":"freeze","evidenceRef":"broker:exact-freeze"}],"origin":origin},
        {"qty":500,"restrictions":[{"kind":"settlement","acquiredOn":"2026-11-20",
            "earliestSession":"2026-11-23","ruleSnapshotId":"synthetic-2026-11-20"}],"origin":origin},
    ]);
    confirm_inventory(&mut db, 1500, 700, FRIDAY);
    prepare_holding_rule(
        &mut db,
        "friday-rule",
        holding_rule(FRIDAY, "2026-11-20"),
        FRIDAY,
    );
    let reconciled = reconcile_partitions(&mut db, 1, 1500, partitions.clone());
    assert_eq!(reconciled["data"]["ok"], true, "{reconciled}");
    let prior = holding_details(&mut db, "before-correction", FRIDAY);
    assert_eq!(prior["partitions"], partitions);
    assert_eq!(prior["sellableBasis"], "confirmed_snapshot");
    assert_eq!(prior["snapshotAsOf"], FRIDAY);
    let sql = rusqlite::Connection::open(tmp.path().join("tickdesk.sqlite3")).unwrap();
    assert_eq!(
        sql.execute(
            "UPDATE objects SET body=json_remove(body,'$.brokerReconciled') \
            WHERE collection='holding_partitions' AND object_id='300502.SZ'",
            [],
        )
        .unwrap(),
        1
    );
    drop(sql);

    for (key, rev, at) in [
        ("change-cost", 2, FRIDAY + 1000),
        ("check-again", 3, MONDAY),
    ] {
        let ack = run(
            &mut db,
            key,
            "adjust_position",
            json!({"symbol":"300502.SZ","basePositionRevision":rev,"qty":1500,"availableQty":700,
                "cost":"10.123","pendingLots":null,"asOf":at,"reason":"手动核对持仓","resolvedExecutionIds":[]}),
            at,
        );
        assert_eq!(ack["data"]["ok"], true);
        let adjustment = &ack["data"]["result"]["adjustment"];
        assert_eq!(adjustment["after"]["positionRevision"], rev + 1);
        assert_eq!(adjustment["after"]["asOf"], at);
        assert_eq!(adjustment["after"]["cost"], "10.123");
        assert_eq!(adjustment["after"]["pendingLots"], Value::Null);
        let holding = holding_details(&mut db, &format!("details-{key}"), at);
        assert_eq!(holding["snapshotAsOf"], FRIDAY);
        assert_eq!(holding["positionAsOf"], at);
        assert_eq!(holding["positionRevision"], rev + 1);
        assert_eq!(holding["originStatus"], "assigned");
        assert_eq!(holding["partitions"], partitions);
        if at == MONDAY {
            assert_eq!(holding["sellableBasis"], "unknown");
            assert_eq!(holding["availableQty"], Value::Null);
        } else {
            assert_eq!(holding["sellableBasis"], "confirmed_snapshot");
            assert_eq!(holding["availableQty"], 700);
        }
    }

    let now = MONDAY + 1000;
    prepare_holding_rule(&mut db, "monday-rule", holding_rule(now, "2026-11-23"), now);
    let after_open = holding_details(&mut db, "after-open", now);
    assert_eq!(after_open["positionRevision"], 4);
    assert_eq!(after_open["snapshotAsOf"], FRIDAY);
    assert_eq!(after_open["sellableBasis"], "local_inferred");
    assert_eq!(after_open["availableQty"], 1200);
    assert_eq!(after_open["partitions"][1], partitions[1]);
    assert_eq!(
        after_open["partitions"][2],
        json!({"qty":500,"restrictions":[],"origin":origin})
    );
}

#[test]
fn ordinary_correction_keeps_pending_lots_until_quantity_or_explicit_lots_change() {
    let tmp = tempfile::tempdir().unwrap();
    let mut db = setup(tmp.path());
    confirm_inventory(&mut db, 1000, 700, FRIDAY);
    prepare_holding_rule(
        &mut db,
        "friday-rule",
        holding_rule(FRIDAY, "2026-11-20"),
        FRIDAY,
    );
    let reconciled = reconcile_partitions(
        &mut db,
        1,
        1000,
        json!([
            {"qty":700,"restrictions":[]},
            {"qty":300,"restrictions":[{"kind":"settlement","acquiredOn":"2026-11-20",
                "earliestSession":"2026-11-23","ruleSnapshotId":"synthetic-2026-11-20"}]},
        ]),
    );
    assert_eq!(reconciled["data"]["ok"], true, "{reconciled}");
    let pending = json!([{"qty":300,"sellableDate":"2026-11-23"}]);
    assert_eq!(
        reconciled["data"]["result"]["adjustment"]["after"]["pendingLots"],
        pending
    );

    let check_at = FRIDAY + 1000;
    let checked = run(
        &mut db,
        "review-unchanged",
        "adjust_position",
        json!({
            "symbol":"300502.SZ","basePositionRevision":2,"qty":1000,"availableQty":700,
            "cost":"10","pendingLots":null,"asOf":check_at,"reason":"手动核对持仓","resolvedExecutionIds":[]
        }),
        check_at,
    );
    assert_eq!(checked["data"]["ok"], true);
    assert_eq!(
        checked["data"]["result"]["adjustment"]["after"]["pendingLots"],
        pending
    );

    let change_at = FRIDAY + 2000;
    let changed = run(
        &mut db,
        "change-available",
        "adjust_position",
        json!({
            "symbol":"300502.SZ","basePositionRevision":3,"qty":1000,"availableQty":800,
            "cost":"10","pendingLots":null,"asOf":change_at,"reason":"确认可用数量","resolvedExecutionIds":[]
        }),
        change_at,
    );
    assert_eq!(changed["data"]["ok"], true);
    assert_eq!(
        changed["data"]["result"]["adjustment"]["after"]["pendingLots"],
        Value::Null
    );
    let changed_holding = holding_details(&mut db, "changed-holding", change_at);
    assert_eq!(changed_holding["snapshotAsOf"], change_at);
    assert_eq!(changed_holding["availableQty"], 800);
    assert_eq!(
        changed_holding["partitions"][1]["restrictions"],
        json!([{"kind":"unknown"}])
    );

    let lots_at = FRIDAY + 3000;
    let explicit = json!([{"qty":200,"sellableDate":"2026-11-23"}]);
    let supplied = run(
        &mut db,
        "supply-lots",
        "adjust_position",
        json!({
            "symbol":"300502.SZ","basePositionRevision":4,"qty":1000,"availableQty":800,
            "cost":"10","pendingLots":explicit,"asOf":lots_at,"reason":"录入批次数量","resolvedExecutionIds":[]
        }),
        lots_at,
    );
    assert_eq!(supplied["data"]["ok"], true);
    assert_eq!(
        supplied["data"]["result"]["adjustment"]["after"]["pendingLots"],
        explicit
    );
    assert_eq!(
        holding_details(&mut db, "explicit-lots-details", lots_at)["snapshotAsOf"],
        lots_at
    );
}

#[test]
fn ordinary_correction_can_still_confirm_a_legacy_quantity_snapshot() {
    let tmp = tempfile::tempdir().unwrap();
    let mut db = setup(tmp.path());
    confirm_inventory(&mut db, 1000, 700, FRIDAY);
    let checked = run(
        &mut db,
        "review-legacy",
        "adjust_position",
        json!({
            "symbol":"300502.SZ","basePositionRevision":1,"qty":1000,"availableQty":700,
            "cost":"10.123","pendingLots":null,"asOf":MONDAY,"reason":"确认券商可用数量","resolvedExecutionIds":[]
        }),
        MONDAY,
    );
    assert_eq!(checked["data"]["ok"], true);
    let holding = holding_details(&mut db, "legacy-details", MONDAY);
    assert_eq!(holding["snapshotAsOf"], MONDAY);
    assert_eq!(holding["sellableBasis"], "confirmed_snapshot");
    assert_eq!(holding["availableQty"], 700);
}

#[test]
fn monday_releases_settlement_quantity_and_preserves_other_freezes() {
    let tmp = tempfile::tempdir().unwrap();
    let mut db = setup(tmp.path());
    let reconcile = reconcile_partitions(
        &mut db,
        0,
        1000,
        json!([
            {"qty":700,"restrictions":[]},
            {"qty":300,"restrictions":[{"kind":"freeze","evidenceRef":"broker:training-01"}]},
        ]),
    );
    assert_eq!(reconcile["data"]["ok"], true);
    prepare_holding_rule(
        &mut db,
        "friday-rule",
        holding_rule(FRIDAY, "2026-11-20"),
        FRIDAY,
    );
    let buy = trade(&mut db, "buy", "buy", 500, "10", FRIDAY + 1000);
    assert_eq!(buy["position"]["qty"], 1500);
    assert_eq!(buy["position"]["availableQty"], 700);
    assert_eq!(buy["position"]["pendingLots"], Value::Null);
    let before_weekend = holding_details(&mut db, "friday-details", FRIDAY + 1000);
    assert_eq!(before_weekend["availableQty"], 700);

    let saturday = holding_details(&mut db, "saturday-details", FRIDAY + 86_400_000);
    assert_eq!(saturday["availableQty"], Value::Null);
    let midnight = MONDAY - 34_200_000;
    prepare_holding_rule(
        &mut db,
        "overnight-rule",
        holding_rule(midnight, "2026-11-23"),
        midnight,
    );
    let before_open = holding_details(&mut db, "overnight-details", midnight);
    assert_eq!(before_open["availableQty"], Value::Null);
    let mut unchecked = holding_rule(MONDAY, "2026-11-23");
    unchecked["companyActionsCheckedThrough"] = Value::Null;
    unchecked["rule"]["ruleSnapshotId"] = json!("synthetic-monday-unchecked");
    prepare_holding_rule(&mut db, "unchecked-rule", unchecked, MONDAY);
    let awaiting_actions = holding_details(&mut db, "awaiting-actions", MONDAY);
    assert_eq!(awaiting_actions["availableQty"], Value::Null);

    let verified = holding_rule(MONDAY + 1000, "2026-11-23");
    prepare_holding_rule(&mut db, "monday-rule", verified, MONDAY + 1000);
    let after_open = holding_details(&mut db, "monday-details", MONDAY + 1000);
    assert_eq!(after_open["availableQty"], 1200);
    assert_eq!(after_open["sellableBasis"], "local_inferred");
    let sold = trade(&mut db, "sell", "sell", 1200, "10", MONDAY + 2000);
    assert_eq!(sold["position"]["qty"], 300);
    assert_eq!(sold["position"]["availableQty"], 0);
}

#[test]
fn unassigned_sale_does_not_guess_which_strategy_holding_remains() {
    let tmp = tempfile::tempdir().unwrap();
    let mut db = setup(tmp.path());
    let origin = |group: &str, strategy: &str| {
        json!({"group":group,"strategyId":strategy,
        "codeHash":"a".repeat(64),"parametersHash":"b".repeat(64),"dependenciesHash":"c".repeat(64)})
    };
    let reconcile = reconcile_partitions(
        &mut db,
        0,
        1500,
        json!([
            {"qty":1000,"restrictions":[],"origin":origin("daily_swing", "first")},
            {"qty":500,"restrictions":[],"origin":origin("short_term", "second")}
        ]),
    );
    assert_eq!(reconcile["data"]["ok"], true);
    let confirmed = holding_details(&mut db, "assigned-details", FRIDAY);
    assert_eq!(confirmed["originStatus"], "assigned");
    assert_eq!(confirmed["partitions"].as_array().unwrap().len(), 2);

    let sale = trade(&mut db, "sell", "sell", 700, "10", FRIDAY + 1000);
    assert_eq!(sale["execution"]["positionEffect"], "applied");
    assert_eq!(sale["position"]["qty"], 800);
    let remaining = holding_details(&mut db, "unassigned-details", FRIDAY + 1000);
    assert_eq!(remaining["qty"], 800);
    assert_eq!(remaining["originStatus"], "unknown");
    assert_eq!(
        remaining["partitions"],
        json!([{"qty":800,"restrictions":[],"origin":null}])
    );
    let closing = trade(&mut db, "close", "sell", 800, "10", FRIDAY + 2000);
    assert_eq!(closing["position"]["qty"], 0);
    prepare_holding_rule(
        &mut db,
        "new-rule",
        holding_rule(FRIDAY + 2000, "2026-11-20"),
        FRIDAY + 2000,
    );
    let reopened = trade(&mut db, "new-buy", "buy", 200, "10", FRIDAY + 3000);
    assert_eq!(reopened["position"]["qty"], 200);
    let new_holding = holding_details(&mut db, "new-holding", FRIDAY + 3000);
    assert_eq!(new_holding["originStatus"], "unallocated");
}

#[test]
fn unreported_available_quantity_stays_unknown_in_holding_details() {
    let tmp = tempfile::tempdir().unwrap();
    let mut db = setup(tmp.path());
    let baseline = run(
        &mut db,
        "unknown-baseline",
        "adjust_position",
        json!({"symbol":"300502.SZ","basePositionRevision":0,"qty":1000,"availableQty":null,
            "cost":"10","pendingLots":null,"asOf":FRIDAY,"reason":"仅确认持仓总数","resolvedExecutionIds":[]}),
        FRIDAY,
    );
    assert_eq!(baseline["data"]["ok"], true);
    let holding = holding_details(&mut db, "unknown-details", FRIDAY);
    assert_eq!(holding["qty"], 1000);
    assert_eq!(holding["availableQty"], Value::Null);
    assert_eq!(holding["sellableBasis"], "unknown");
}

#[test]
fn fill_at_snapshot_timestamp_does_not_turn_a_projection_into_a_confirmed_snapshot() {
    let tmp = tempfile::tempdir().unwrap();
    let mut db = setup(tmp.path());
    confirm_inventory(&mut db, 1000, 1000, FRIDAY);
    let sale = trade(&mut db, "sale", "sell", 100, "10", FRIDAY);
    assert_eq!(sale["position"]["qty"], 900);
    let holding = holding_details(&mut db, "same-timestamp-details", FRIDAY);
    assert_eq!(holding["availableQty"], Value::Null);
    assert_eq!(holding["sellableBasis"], "unknown");
}

#[test]
fn shares_with_both_settlement_and_freeze_stay_locked_after_settlement() {
    let tmp = tempfile::tempdir().unwrap();
    let mut db = setup(tmp.path());
    confirm_inventory(&mut db, 1200, 700, FRIDAY);
    prepare_holding_rule(
        &mut db,
        "friday-rule",
        holding_rule(FRIDAY, "2026-11-20"),
        FRIDAY,
    );
    let reconcile = reconcile_partitions(
        &mut db,
        1,
        1200,
        json!([
            {"qty":700,"restrictions":[]},
            {"qty":500,"restrictions":[
                {"kind":"settlement","acquiredOn":"2026-11-20","earliestSession":"2026-11-23",
                    "ruleSnapshotId":"synthetic-2026-11-20"},
                {"kind":"freeze","evidenceRef":"broker:training-overlap"}
            ]},
        ]),
    );
    assert_eq!(reconcile["data"]["ok"], true);
    prepare_holding_rule(
        &mut db,
        "monday-rule",
        holding_rule(MONDAY, "2026-11-23"),
        MONDAY,
    );
    let details = holding_details(&mut db, "monday-details", MONDAY);
    assert_eq!(details["qty"], 1200);
    assert_eq!(details["availableQty"], 700);
    assert_eq!(details["partitions"][1]["qty"], 500);
    assert_eq!(
        details["partitions"][1]["restrictions"],
        json!([{"kind":"freeze","evidenceRef":"broker:training-overlap"}])
    );
}

#[test]
fn settings_and_pause_are_real_state_and_not_advertised_as_scan_readiness() {
    let tmp = tempfile::tempdir().unwrap();
    let mut db = setup(tmp.path());
    let paused = run(&mut db, "pause", "pause", json!({}), 1);
    assert_eq!(paused["data"]["result"]["paused"], true);
    let updated = run(
        &mut db,
        "settings",
        "set_settings",
        json!({"baseSettingsRevision":0,
        "reminderModes":{"critical":"full_screen","warn":"sound","info":"visual"},
        "nightStart":"23:00","nightEnd":"07:00","criticalAtNight":false,"pushEnabled":true}),
        2,
    );
    assert_eq!(updated["data"]["result"]["settings"]["settingsRevision"], 1);
    drop(db);
    let mut db = setup(tmp.path());
    let status = run(&mut db, "status", "get_status", json!({}), 3);
    let result = &status["data"]["result"];
    assert_eq!(result["analysis"]["paused"], true);
    assert_eq!(result["analysis"]["kernelStatus"], "offline");
    assert!(result["protocol"]["supportedCommands"]
        .as_array()
        .unwrap()
        .contains(&json!("scan_now")));
}

#[test]
fn mcp_status_uses_real_probe_and_expires_without_polling() {
    let tmp = tempfile::tempdir().unwrap();
    let mut db = setup(tmp.path());
    db.record_ai_health("mcp", "online", None, 1000).unwrap();
    let result = run(&mut db, "mcp-fresh", "get_status", json!({}), 1100);
    let deps = result["data"]["result"]["dependencies"].as_array().unwrap();
    let mcp = deps.iter().find(|row| row["name"] == "mcp").unwrap();
    assert_eq!(mcp["status"], "online");
    assert_eq!(mcp["checkedAt"], 1000);
    let result = run(&mut db, "mcp-stale", "get_status", json!({}), 700000);
    let deps = result["data"]["result"]["dependencies"].as_array().unwrap();
    let mcp = deps.iter().find(|row| row["name"] == "mcp").unwrap();
    assert_eq!(mcp["status"], "unknown");
    assert_eq!(mcp["errorCode"], "MCP_PROBE_STALE");
}

#[test]
fn kernel_without_approved_strategies_remains_gated_with_specific_reason() {
    let tmp = tempfile::tempdir().unwrap();
    let mut db = setup(tmp.path());
    let sql = rusqlite::Connection::open(tmp.path().join("tickdesk.sqlite3")).unwrap();
    sql.execute(
        "INSERT INTO kernel_probe VALUES (1,1000,'degraded','0.3.37','[\"daily\"]','[]',0)",
        [],
    )
    .unwrap();
    let response = run(&mut db, "no-strategies", "get_status", json!({}), 1100);
    let result = &response["data"]["result"];
    assert_eq!(result["analysis"]["kernelStatus"], "offline");
    let kernel = result["dependencies"]
        .as_array()
        .unwrap()
        .iter()
        .find(|d| d["name"] == "kernel")
        .unwrap();
    assert_eq!(kernel["status"], "degraded");
    assert_eq!(kernel["errorCode"], "KERNEL_NO_PRODUCTION_STRATEGIES");
}

#[test]
fn task_actions_separate_from_display_version_and_terminal_state_wins() {
    let tmp = tempfile::tempdir().unwrap();
    let mut db = setup(tmp.path());
    let fixtures: Value =
        serde_json::from_str(include_str!("../contracts/v2/fixtures.json")).unwrap();
    let mut task = fixtures["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|fixture| fixture["name"] == "task")
        .unwrap()["payload"]["data"]
        .clone();
    // In the initial screen the reminder alone has moved the display version.
    task["version"] = json!(3);
    let task_id = task["taskId"].as_str().unwrap();
    let local = rusqlite::Connection::open(tmp.path().join("tickdesk.sqlite3")).unwrap();
    local
        .execute(
            "INSERT INTO objects VALUES ('tasks',?1,?2)",
            rusqlite::params![task_id, serde_json::to_string(&task).unwrap()],
        )
        .unwrap();
    drop(local);
    let now = task["createdAt"].as_u64().unwrap() + 100;
    let snoozed = run(
        &mut db,
        "snooze",
        "snooze",
        json!({"taskId":task_id,"baseActionVersion":1,"minutes":2}),
        now,
    );
    assert_eq!(snoozed["data"]["result"]["task"]["version"], 4);
    assert_eq!(snoozed["data"]["result"]["task"]["actionVersion"], 2);
    let stale = run(
        &mut db,
        "stale-ignore",
        "ignore",
        json!({"taskId":task_id,"baseActionVersion":1}),
        now + 1,
    );
    assert_eq!(stale["data"]["error"]["code"], "TASK_VERSION_CONFLICT");
    let ignored = run(
        &mut db,
        "ignore",
        "ignore",
        json!({"taskId":task_id,"baseActionVersion":2}),
        now + 2,
    );
    assert_eq!(ignored["data"]["result"]["task"]["status"], "ignored");
    let terminal = run(
        &mut db,
        "terminal",
        "snooze",
        json!({"taskId":task_id,"baseActionVersion":3,"minutes":1}),
        now + 3,
    );
    assert_eq!(terminal["data"]["error"]["code"], "TASK_TERMINAL");
    let history = run(
        &mut db,
        "history",
        "get_task_actions",
        json!({"taskId":task_id}),
        now + 3,
    );
    assert_eq!(history["data"]["result"]["items"][0]["type"], "ignored");
    assert_eq!(history["data"]["result"]["items"][1]["type"], "snoozed");
}

#[test]
fn sync_continuation_ack_tracks_current_request_while_page_remains_immutable() {
    let tmp = tempfile::tempdir().unwrap();
    let mut db = setup(tmp.path());
    let now = 1794792300000_u64;
    for n in 0..21 {
        let ack = run(
            &mut db,
            &format!("adjust-{n}"),
            "adjust_position",
            json!({
                "symbol":format!("{:06}.SZ",300500+n),"basePositionRevision":n,"qty":10,
                "availableQty":10,"pendingLots":[],"cost":"12.5","asOf":now,"reason":"confirmed position",
                "resolvedExecutionIds":[]
            }),
            now,
        );
        assert_eq!(ack["data"]["ok"], true);
    }
    let first = run(&mut db, "page0", "sync", json!({}), now);
    let head = &first["data"]["result"];
    assert_eq!(head["hasMore"], true);
    assert_eq!(head["pageIndex"], 0);
    assert_eq!(head["requestId"], "page0");
    let more = json!({"snapshotId":head["snapshotId"],"pageCursor":head["nextPageCursor"]});
    let page1 = run(&mut db, "page1", "sync", more.clone(), now);
    assert_eq!(page1["data"]["clientMsgId"], "page1");
    assert_eq!(page1["data"]["result"]["requestId"], "page1");
    let sql = rusqlite::Connection::open(tmp.path().join("tickdesk.sqlite3")).unwrap();
    let read_frame = |event_id: &Value| -> Value {
        let body: String = sql
            .query_row(
                "SELECT frame FROM transport_outbox WHERE event_id=?1 ORDER BY id DESC LIMIT 1",
                [event_id.as_str().unwrap()],
                |r| r.get(0),
            )
            .unwrap();
        serde_json::from_str(&body).unwrap()
    };
    let page_frame = read_frame(&page1["data"]["result"]["pageEventId"]);
    assert_eq!(page_frame["payload"]["data"]["requestId"], "page0");
    assert!(db
        .accept_receipt(
            &json!({"v":2,"type":"receipt","deliveryId":page_frame["deliveryId"],
        "payloadHash":page_frame["payloadHash"],"fromIncarnation":page_frame["fromIncarnation"],
        "toIncarnation":page_frame["toIncarnation"]})
        )
        .unwrap());
    let page_retry = run(&mut db, "page1-again", "sync", more.clone(), now + 1);
    assert_eq!(page_retry["data"]["clientMsgId"], "page1-again");
    let mut expected_result = page1["data"]["result"].clone();
    expected_result["requestId"] = json!("page1-again");
    assert_eq!(page_retry["data"]["result"], expected_result);
    let retry_frame = read_frame(&page_retry["data"]["result"]["pageEventId"]);
    assert_eq!(retry_frame["payload"], page_frame["payload"]);
    assert_eq!(retry_frame["payloadHash"], page_frame["payloadHash"]);
    let expired = run(&mut db, "late", "sync", more, now + 600_000);
    assert_eq!(expired["data"]["error"]["code"], "SNAPSHOT_EXPIRED");
}
