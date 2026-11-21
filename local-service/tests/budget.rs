mod support;

use rusqlite::{params, Connection, TransactionBehavior};
use serde_json::{json, Value};
use support::{command, deliver, LOCAL_PAIR, PEER};
use tickdesk_local::{
    budget::{self, BudgetConfig},
    storage::Storage,
};

const NOW: u64 = 1_794_878_400_000;

fn run(
    db: &mut Storage,
    key: &str,
    cmd: &str,
    args: Value,
    now: u64,
    reply: Option<Value>,
) -> Value {
    let wire = command(db, key, key, cmd, args);
    let prepared = reply.map(|response| {
        let request = tickdesk_local::market::capture(db, &wire["data"], now).unwrap();
        json!({"request": request, "response": response})
    });
    db.receive(&deliver(db, PEER, key, &wire)).unwrap();
    db.process_prepared(db.job(key).unwrap().unwrap(), now, prepared)
        .unwrap()
        .unwrap()
}

#[test]
fn reconciled_partial_fill_keeps_remainder_reserved_after_card_expiry_and_restart() {
    let (dir, mut db, conn) = setup();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let now = NOW + 600_000;
    let mut cfg = config();
    cfg.version = 2;
    cfg.per_symbol_notional = "10000".into();
    cfg.per_strategy_notional = "10000".into();
    *cfg.group_limits.get_mut("tech").unwrap() = "10000".into();
    *cfg.industry_limits.get_mut("software").unwrap() = "10000".into();
    cfg.symbols
        .get_mut("300502.SZ")
        .unwrap()
        .strategies
        .push("s3".into());
    db.configure_budget(Some(&cfg), now).unwrap();

    let mut input = raw("300502.SZ");
    input["candidate"] = json!({"symbol":"300502.SZ","name":"fixture","score":"80",
        "strategies":["s1","s2","s3"],"changeRatio":null,"volumeRatio":null,
        "marketCap":null,"lastPrice":"10","dataAsOf":now,"riskStatus":"unknown"});
    input["approved"] = json!(true);
    input["supported"] = json!(true);
    input["liquidityPassed"] = json!(true);
    let scan = run(
        &mut db,
        "scan-partial",
        "scan_now",
        json!({}),
        now,
        Some(
            json!({"frozen":{"tradeDate":"2026-11-17","isTradingDay":true,"generation":"fixture",
            "generationAfter":"fixture","asOf":now,"strategies":{"total":3,"succeeded":3,"failed":[]},
            "policy":{"minScore":"60"},"candidates":[input]}}),
        ),
    );
    assert_eq!(scan["data"]["ok"], true, "{scan}");
    let task = scan["data"]["result"]["publishedTaskIds"][0]
        .as_str()
        .unwrap();
    let reservation = || {
        conn.query_row(
            "SELECT qty,state FROM budget_reservations WHERE task_id=?1",
            [task],
            |r| Ok((r.get::<_, u64>(0)?, r.get::<_, String>(1)?)),
        )
        .unwrap()
    };
    assert_eq!(reservation(), (1000, "held".into()));
    let fill = run(
        &mut db,
        "fill-300",
        "record_execution",
        json!({"taskId":task,"symbol":"300502.SZ",
        "side":"buy","price":"10.000","qty":300,"orderQty":1000,"executedAt":now+1}),
        now + 1,
        None,
    );
    let execution = &fill["data"]["result"]["execution"];
    assert_eq!(execution["qty"], 300, "{fill}");
    assert_eq!(execution["positionEffect"], "needs_reconciliation");
    assert_eq!(reservation(), (1000, "reconciliation".into()));
    let adjusted = run(
        &mut db,
        "confirm-300",
        "adjust_position",
        json!({"symbol":"300502.SZ",
        "basePositionRevision":0,"qty":300,"availableQty":0,"pendingLots":null,"cost":"10",
        "asOf":now+2,"reason":"confirmed fixture fill","resolvedExecutionIds":[execution["executionId"]]}),
        now + 2,
        None,
    );
    assert_eq!(adjusted["data"]["ok"], true, "{adjusted}");
    assert_eq!(reservation(), (1000, "held".into()));
    let processing = run(
        &mut db,
        "confirm-processing",
        "set_execution_progress",
        json!({"taskId":task,"baseVersion":0,
        "state":"partially_filled","reportedRemainingQty":700,"expectedConfirmedQty":null,"asOf":now+3}),
        now + 3,
        None,
    );
    assert_eq!(processing["data"]["ok"], true, "{processing}");
    assert_eq!(
        processing["data"]["result"]["progress"]["reportedRemainingQty"],
        700
    );
    drop(db);
    let mut db = Storage::open(dir.path()).unwrap();
    db.advance_reminders(now + 1_800_001).unwrap();
    assert_eq!(reservation(), (1000, "held".into()));

    let at = now + 1_800_002;
    let progress = run(
        &mut db,
        "get-remainder",
        "get_execution_progress",
        json!({"taskId":task}),
        at,
        None,
    );
    assert_eq!(progress["data"]["result"]["version"], 3);
    let view = &progress["data"]["result"]["progress"];
    assert_eq!(view["state"], "partially_filled");
    assert_eq!(view["confirmedFillQty"], 300);
    assert_eq!(view["reservedQty"], 700);

    let conflict = run(
        &mut db,
        "old-version",
        "set_execution_progress",
        json!({"taskId":task,"baseVersion":0,
        "state":"processing","reportedRemainingQty":700,"expectedConfirmedQty":null,"asOf":at}),
        at,
        None,
    );
    assert_eq!(conflict["data"]["ok"], false, "{conflict}");
    assert_eq!(
        conflict["data"]["error"]["code"], "PROGRESS_VERSION_CONFLICT",
        "{conflict}"
    );
    assert_eq!(
        conflict["data"]["error"]["details"]["currentVersion"], 1,
        "{conflict}"
    );
    let stale = run(
        &mut db,
        "wrong-end",
        "set_execution_progress",
        json!({"taskId":task,"baseVersion":1,
        "state":"ended","reportedRemainingQty":0,"expectedConfirmedQty":0,"asOf":at}),
        at,
        None,
    );
    assert_eq!(stale["data"]["ok"], false);
    assert_eq!(stale["data"]["error"]["code"], "INVALID_PARAMS", "{stale}");
    assert_eq!(reservation(), (1000, "held".into()));
    let ended = run(
        &mut db,
        "confirm-end",
        "set_execution_progress",
        json!({"taskId":task,"baseVersion":1,
        "state":"ended","reportedRemainingQty":0,"expectedConfirmedQty":300,"asOf":at}),
        at,
        None,
    );
    assert_eq!(ended["data"]["ok"], true, "{ended}");
    assert_eq!(ended["data"]["result"]["progress"]["reservedQty"], 0);
    assert_eq!(reservation(), (1000, "released".into()));

    let old_pending = command(
        &db,
        "buffered-before-restore",
        "buffered-before-restore",
        "pause",
        json!({}),
    );
    db.receive(&deliver(&db, PEER, "old-pending", &old_pending))
        .unwrap();
    let recovery = tempfile::tempdir().unwrap();
    let backup = recovery.path().join("backup");
    let restored_path = recovery.path().join("restored");
    db.backup_to(&backup).unwrap();
    Storage::restore_from(&backup, &restored_path).unwrap();
    let mut restored = Storage::open(&restored_path).unwrap();
    assert_ne!(restored.identity.server_epoch, db.identity.server_epoch);
    let restored_view = run(
        &mut restored,
        "restored-progress",
        "get_execution_progress",
        json!({"taskId":task}),
        at,
        None,
    );
    let historical = &restored_view["data"]["result"]["progress"];
    assert_eq!(historical["state"], "unknown");
    assert_eq!(historical["reservedQty"], 1000);
    assert_eq!(historical["confirmedFillQty"], 300);
    let blocked = run(
        &mut restored,
        "restore-cannot-confirm-progress",
        "set_execution_progress",
        json!({
        "taskId":task,"baseVersion":2,"state":"ended","reportedRemainingQty":0,
        "expectedConfirmedQty":300,"asOf":at}),
        at,
        None,
    );
    assert_eq!(blocked["data"]["error"]["code"], "RECOVERY_REQUIRED");

    use base64::{engine::general_purpose::STANDARD, Engine};
    use tickdesk_local::contract::{canonical, digest};
    let session = run(
        &mut restored,
        "progress-recovery",
        "get_recovery",
        json!({}),
        at,
        None,
    );
    let session = &session["data"]["result"]["reconciliationId"];
    let hash = digest(&old_pending).unwrap();
    let proof = run(
        &mut restored,
        "old-pending-proof",
        "recovery_evidence",
        json!({"reconciliationId":session,
        "recordId":"buffered-proof","sourceType":"command","payloadHash":hash,"partIndex":0,"partCount":1,
        "bytesBase64":STANDARD.encode(canonical(&old_pending).unwrap()),"evidenceEpoch":db.identity.server_epoch,
        "capturedAt":at,"observedSnapshotSeq":null}),
        at,
        None,
    );
    assert_eq!(
        proof["data"]["result"]["record"]["status"], "verified",
        "{proof}"
    );
    let items = run(
        &mut restored,
        "pending-review",
        "get_recovery",
        json!({}),
        at,
        None,
    );
    let items = &items["data"]["result"];
    let archived = run(
        &mut restored,
        "review-buffered-intent",
        "recovery_reconcile",
        json!({"reconciliationId":session,
        "reviewedSnapshotSeq":items["reviewedSnapshotSeq"],"positions":[],"decisions":[{
            "itemId":items["items"][0]["itemId"],"evidenceRecordIds":["buffered-proof"],"disposition":"archive_operation",
            "note":"Checked broker records and archived the buffered non-trade operation"}]}),
        at,
        None,
    );
    assert_eq!(
        archived["data"]["result"]["remainingCount"], 0,
        "{archived}"
    );
    let seq = &archived["data"]["result"]["reviewedSnapshotSeq"];
    let manifest =
        digest(&json!([{"recordId":"buffered-proof","sourceType":"command","payloadHash":hash}]))
            .unwrap();
    let seal = run(
        &mut restored,
        "seal-buffered-recovery",
        "recovery_seal_evidence",
        json!({"reconciliationId":session,
        "reviewedSnapshotSeq":seq,"recordCount":1,"manifestHash":manifest}),
        at,
        None,
    );
    assert_eq!(seal["data"]["result"]["evidenceStatus"], "sealed", "{seal}");
    let confirm = run(
        &mut restored,
        "confirm-buffered-recovery",
        "recovery_confirm",
        json!({"reconciliationId":session,
        "reviewedSnapshotSeq":seq}),
        at,
        None,
    );
    assert_eq!(
        confirm["data"]["result"]["recoveryMode"], false,
        "{confirm}"
    );
    let fresh_end = run(
        &mut restored,
        "current-epoch-end",
        "set_execution_progress",
        json!({"taskId":task,
        "baseVersion":2,"state":"ended","reportedRemainingQty":0,"expectedConfirmedQty":300,"asOf":at+1}),
        at + 1,
        None,
    );
    assert_eq!(
        fresh_end["data"]["result"]["progress"]["reservedQty"], 0,
        "{fresh_end}"
    );

    let late = run(
        &mut db,
        "late-fill",
        "record_execution",
        json!({"taskId":task,"symbol":"300502.SZ","side":"buy",
        "price":"10.0150","qty":100,"executedAt":now+1}),
        at + 1,
        None,
    );
    assert_eq!(late["data"]["result"]["execution"]["qty"], 100);
    assert_eq!(late["data"]["result"]["execution"]["price"], "10.0150");
    let review = run(
        &mut db,
        "review-late",
        "get_execution_progress",
        json!({"taskId":task}),
        at + 2,
        None,
    );
    assert_eq!(review["data"]["result"]["progress"]["state"], "unknown");
    assert_eq!(
        review["data"]["result"]["progress"]["reconciliationRequired"],
        true
    );
}

fn config() -> BudgetConfig {
    serde_json::from_value(json!({"version":1,"valid_from":NOW-1000,"expires_at":NOW+86_400_000,
        "total_notional":"10000","total_risk":"1000","per_symbol_notional":"5000","per_strategy_notional":"7000",
        "group_limits":{"tech":"8000"},"industry_limits":{"software":"8000"},"daily_notional":"10000","max_active":3,
        "fee_buffer_rate":"0","slippage_buffer_rate":"0","symbols":{"300502.SZ":{"group":"tech","industry":"software","strategies":["s1","s2"],"stop_price":"9"},"300503.SZ":{"group":"tech","industry":"software","strategies":["s1","s2"],"stop_price":"9"}}})).unwrap()
}

fn raw(symbol: &str) -> Value {
    json!({"direction":"buy","candidate":{"symbol":symbol,"strategies":["s1","s2"],"lastPrice":"10","dataAsOf":NOW},
        "sizing":{"stopPrice":"9","unitQty":100,"priceBasis":"raw","ruleSource":"local:verified-rule","ruleAsOf":NOW,"effectiveDate":"2026-11-17"}})
}

fn setup() -> (tempfile::TempDir, Storage, Connection) {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.configure_budget(Some(&config()), NOW).unwrap();
    let connection = Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();
    (dir, db, connection)
}

#[test]
fn same_transaction_enforces_shared_symbol_strategy_group_and_global_caps() {
    let (_dir, _db, mut conn) = setup();
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .unwrap();
    let first = budget::reserve(&tx, "task-one", &raw("300502.SZ"), NOW).unwrap();
    assert_eq!(first.quantity, Some(500));
    let duplicate = budget::reserve(&tx, "task-one", &raw("300502.SZ"), NOW).unwrap();
    assert_eq!(duplicate.quantity, Some(500));
    let same = budget::reserve(&tx, "task-two", &raw("300502.SZ"), NOW).unwrap();
    assert!(same.blocked);
    let other = budget::reserve(&tx, "task-three", &raw("300503.SZ"), NOW).unwrap();
    assert_eq!(other.quantity, Some(200));
    let exhausted = budget::reserve(&tx, "task-four", &raw("300503.SZ"), NOW).unwrap();
    assert!(exhausted.blocked);
    tx.commit().unwrap();
}

#[test]
fn missing_stale_future_or_unverified_sizing_never_creates_executable_quantity() {
    let (_dir, _db, mut conn) = setup();
    for change in [
        json!({"stopPrice":null}),
        json!({"stopPrice":"0"}),
        json!({"unitQty":0}),
        json!({"priceBasis":"forward"}),
        json!({"ruleSource":""}),
        json!({"ruleAsOf":NOW+1}),
        json!({"effectiveDate":"2020-01-01"}),
    ] {
        let mut input = raw("300502.SZ");
        for (key, value) in change.as_object().unwrap() {
            input["sizing"][key] = value.clone();
        }
        let tx = conn.transaction().unwrap();
        assert_eq!(
            budget::reserve(&tx, "bad", &input, NOW).unwrap().quantity,
            None
        );
    }
    let mut input = raw("300502.SZ");
    input["candidate"]["dataAsOf"] = json!(NOW - 90_001);
    let tx = conn.transaction().unwrap();
    assert_eq!(
        budget::reserve(&tx, "stale", &input, NOW).unwrap().quantity,
        None
    );
}

#[test]
fn exact_decimal_fees_and_lot_rounding_are_conservative() {
    let (_dir, mut db, mut conn) = setup();
    let mut cfg = config();
    cfg.version = 2;
    cfg.total_notional = "1".into();
    cfg.per_symbol_notional = "1".into();
    cfg.fee_buffer_rate = "0.1".into();
    db.configure_budget(Some(&cfg), NOW).unwrap();
    let mut input = raw("300502.SZ");
    input["candidate"]["lastPrice"] = json!("0.1");
    input["sizing"]["stopPrice"] = json!("0.09");
    input["sizing"]["unitQty"] = json!(3);
    let tx = conn.transaction().unwrap();
    assert_eq!(
        budget::reserve(&tx, "decimal", &input, NOW)
            .unwrap()
            .quantity,
        Some(9)
    );
    let amount: String = tx
        .query_row(
            "SELECT notional FROM budget_reservations WHERE task_id='decimal'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(amount, "0.99");
}

#[test]
fn ignored_reservation_releases_only_without_execution_or_unresolved_command() {
    let (_dir, _db, mut conn) = setup();
    let tx = conn.transaction().unwrap();
    budget::reserve(&tx, "task-one", &raw("300502.SZ"), NOW).unwrap();
    tx.execute(
        "INSERT INTO objects VALUES ('tasks','task-one',?1)",
        [json!({"status":"ignored"}).to_string()],
    )
    .unwrap();
    budget::reconcile(&tx, NOW).unwrap();
    let state: String = tx
        .query_row("SELECT state FROM budget_reservations", [], |r| r.get(0))
        .unwrap();
    assert_eq!(state, "released");
    let next = budget::reserve(&tx, "task-two", &raw("300502.SZ"), NOW).unwrap();
    assert_eq!(next.quantity, Some(500));
    tx.execute(
        "INSERT INTO objects VALUES ('tasks','task-two',?1)",
        [json!({"status":"expired"}).to_string()],
    )
    .unwrap();
    tx.execute("INSERT INTO objects VALUES ('executions','fill-one',?1)",[json!({"executionId":"fill-one","taskId":"task-two","symbol":"300502.SZ","side":"buy","qty":500,"positionEffect":"needs_reconciliation","supersedesExecutionId":null}).to_string()]).unwrap();
    budget::reconcile(&tx, NOW).unwrap();
    let state: String = tx
        .query_row(
            "SELECT state FROM budget_reservations WHERE task_id='task-two'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(state, "reconciliation");
    tx.execute("UPDATE objects SET body=?1 WHERE object_id='fill-one'",[json!({"executionId":"fill-one","taskId":"task-two","symbol":"300502.SZ","side":"buy","qty":500,"positionEffect":"reconciled","supersedesExecutionId":null}).to_string()]).unwrap();
    budget::reconcile(&tx, NOW).unwrap();
    let state: String = tx
        .query_row(
            "SELECT state FROM budget_reservations WHERE task_id='task-two'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(state, "converted");
}

#[test]
fn immutable_config_versions_and_incomplete_holdings_fail_closed() {
    let (_dir, mut db, mut conn) = setup();
    let mut cfg = config();
    cfg.total_notional = "100000".into();
    assert!(db.configure_budget(Some(&cfg), NOW).is_err());
    conn.execute(
        "INSERT INTO objects VALUES ('positions','300502.SZ',?1)",
        params![
            json!({"symbol":"300502.SZ","qty":100,"lastPrice":null,"reconciliationRequired":false})
                .to_string()
        ],
    )
    .unwrap();
    let tx = conn.transaction().unwrap();
    let result = budget::reserve(&tx, "unsafe", &raw("300502.SZ"), NOW).unwrap();
    assert!(result.blocked);
    assert_eq!(result.quantity, None);
}

#[test]
fn corrections_without_a_position_baseline_block_new_risk() {
    let (_dir, _db, mut conn) = setup();
    conn.execute("INSERT INTO objects VALUES ('executions','unresolved',?1)",[json!({"taskId":null,"symbol":"300502.SZ","positionEffect":"needs_reconciliation","reconciliationRequired":true}).to_string()]).unwrap();
    let tx = conn.transaction().unwrap();
    assert!(
        budget::reserve(&tx, "new-risk", &raw("300503.SZ"), NOW)
            .unwrap()
            .blocked
    );
}

#[test]
fn changed_group_assignment_cannot_hide_existing_reservations() {
    let (_dir, mut db, mut conn) = setup();
    {
        let tx = conn.transaction().unwrap();
        budget::reserve(&tx, "first", &raw("300502.SZ"), NOW).unwrap();
        tx.commit().unwrap();
    }
    let mut cfg = config();
    cfg.version = 2;
    cfg.per_strategy_notional = "10000".into();
    cfg.group_limits.insert("merged".into(), "6000".into());
    for assignment in cfg.symbols.values_mut() {
        assignment.group = "merged".into();
    }
    db.configure_budget(Some(&cfg), NOW).unwrap();
    let tx = conn.transaction().unwrap();
    assert_eq!(
        budget::reserve(&tx, "second", &raw("300503.SZ"), NOW)
            .unwrap()
            .quantity,
        Some(100)
    );
}

#[test]
fn malformed_unicode_symbol_is_rejected_without_panicking() {
    let mut cfg = config();
    let assignment = cfg.symbols.values().next().unwrap().clone();
    cfg.symbols.insert("aaaa中xx".into(), assignment);
    assert!(cfg.validate().is_err());
}

#[test]
fn independent_writers_cannot_overreserve_shared_capacity() {
    let (dir, _db, _conn) = setup();
    let handles = ["300502.SZ", "300503.SZ"]
        .into_iter()
        .enumerate()
        .map(|(index, symbol)| {
            let path = dir.path().join("tickdesk.sqlite3");
            std::thread::spawn(move || {
                let mut conn = Connection::open(path).unwrap();
                conn.busy_timeout(std::time::Duration::from_secs(3))
                    .unwrap();
                let tx = conn
                    .transaction_with_behavior(TransactionBehavior::Immediate)
                    .unwrap();
                let decision =
                    budget::reserve(&tx, &format!("parallel-{index}"), &raw(symbol), NOW).unwrap();
                tx.commit().unwrap();
                decision.quantity.unwrap_or(0)
            })
        })
        .collect::<Vec<_>>();
    assert_eq!(
        handles.into_iter().map(|h| h.join().unwrap()).sum::<u64>(),
        700
    );
}

#[test]
fn every_budget_cap_independently_limits_quantity_and_expiry_disables_sizing() {
    for dimension in [
        "total", "risk", "symbol", "strategy", "group", "industry", "daily",
    ] {
        let (_dir, mut db, mut conn) = setup();
        let mut cfg = config();
        cfg.version = 2;
        match dimension {
            "total" => cfg.total_notional = "1500".into(),
            "risk" => cfg.total_risk = "150".into(),
            "symbol" => cfg.per_symbol_notional = "1500".into(),
            "strategy" => cfg.per_strategy_notional = "1500".into(),
            "group" => {
                cfg.group_limits.insert("tech".into(), "1500".into());
            }
            "industry" => {
                cfg.industry_limits.insert("software".into(), "1500".into());
            }
            _ => cfg.daily_notional = "1500".into(),
        }
        db.configure_budget(Some(&cfg), NOW).unwrap();
        let tx = conn.transaction().unwrap();
        assert_eq!(
            budget::reserve(&tx, "limited", &raw("300502.SZ"), NOW)
                .unwrap()
                .quantity,
            Some(100),
            "{dimension}"
        );
    }
    for active_at in [NOW - 1, NOW + 86_400_000] {
        let (_dir, _db, mut conn) = setup();
        let tx = conn.transaction().unwrap();
        let mut input = raw("300502.SZ");
        input["candidate"]["dataAsOf"] = json!(active_at);
        // Before validity starts or at expiry, a plan must not produce quantity.
        let at = if active_at < NOW {
            NOW - 1001
        } else {
            active_at
        };
        assert_eq!(
            budget::reserve(&tx, "expired", &input, at)
                .unwrap()
                .quantity,
            None
        );
    }
    let (_dir, mut db, mut conn) = setup();
    let mut cfg = config();
    cfg.version = 2;
    cfg.max_active = 1;
    db.configure_budget(Some(&cfg), NOW).unwrap();
    let tx = conn.transaction().unwrap();
    budget::reserve(&tx, "first", &raw("300502.SZ"), NOW).unwrap();
    assert_eq!(
        budget::reserve(&tx, "second", &raw("300503.SZ"), NOW)
            .unwrap()
            .reason,
        "BUDGET_ACTIVE_LIMIT"
    );
}

#[test]
fn explicit_unicode_group_names_are_supported_by_local_configuration() {
    let (_dir, mut db, _conn) = setup();
    let mut cfg = config();
    cfg.version = 2;
    cfg.group_limits.insert("科技".into(), "5000".into());
    for symbol in cfg.symbols.values_mut() {
        symbol.group = "科技".into();
    }
    db.configure_budget(Some(&cfg), NOW).unwrap();
}
