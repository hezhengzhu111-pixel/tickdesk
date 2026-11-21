mod support;

use serde_json::{json, Value};
use support::{command, deliver, LOCAL_PAIR, PEER};
use tickdesk_local::{commands::now_ms, storage::Storage};

fn run(db: &mut Storage, key: &str, name: &str, fields: Value) -> Value {
    command_event(db, key, name, fields)["data"].clone()
}

fn command_event(db: &mut Storage, key: &str, name: &str, fields: Value) -> Value {
    let payload = command(db, key, key, name, fields);
    db.receive(&deliver(db, PEER, key, &payload)).unwrap();
    let job = db.job(key).unwrap().unwrap();
    db.process(job, now_ms()).unwrap().unwrap()
}

fn setup() -> (tempfile::TempDir, Storage) {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    (dir, db)
}

#[test]
fn automatic_registration_freezes_apply_intent() {
    let (dir, mut db) = setup();
    seed_file(dir.path());
    assert_eq!(
        run(
            &mut db,
            "accept",
            "accept_vision_notice",
            json!({
                "noticeVersion":tickdesk_local::vision::NOTICE_VERSION,"accepted":true
            })
        )["ok"],
        true
    );
    let registered = run(
        &mut db,
        "automatic",
        "register_image",
        json!({
            "importId":"automatic","fileId":"00000000-0000-4000-8000-000000000001","autoApply":true
        }),
    );
    assert_eq!(registered["ok"], true, "{registered}");
    let conflicting = run(
        &mut db,
        "changed-intent",
        "register_image",
        json!({
            "importId":"automatic","fileId":"00000000-0000-4000-8000-000000000001"
        }),
    );
    assert_eq!(conflicting["error"]["code"], "IMPORT_STATE_CONFLICT");
}

#[test]
fn manual_adjustment_preserves_selected_display_name() {
    let (_dir, mut db) = setup();
    let adjusted = run(
        &mut db,
        "named",
        "adjust_position",
        json!({
            "symbol":"600000.SH","name":"浦发银行","qty":100,"availableQty":null,
            "cost":"10.00","pendingLots":null,"asOf":now_ms(),"reason":"手动添加持仓",
            "resolvedExecutionIds":[],"basePositionRevision":0
        }),
    );
    assert_eq!(adjusted["ok"], true, "{adjusted}");
    assert_eq!(
        adjusted["result"]["adjustment"]["after"]["name"],
        "浦发银行"
    );
}

fn automatic_rows() -> Value {
    let mut rows = recognition();
    rows[0]["raw"] = json!({"name":"浦发银行","symbol":"600000","qty":"200",
        "availableQty":"100","cost":"10.00","lastPrice":null,"marketValue":null,"pnl":null});
    rows[0]["issues"] = json!([]);
    rows[0]["confidence"] = json!({"name":"1","symbol":"1","qty":"1","availableQty":"1",
        "cost":"1","lastPrice":null,"marketValue":null,"pnl":null});
    rows
}

#[test]
fn automatic_import_requires_confident_unambiguous_essential_fields() {
    let mut cases = Vec::new();
    for field in ["qty", "cost", "symbol", "availableQty"] {
        for confidence in [json!("0.899999999"), Value::Null] {
            let mut rows = automatic_rows();
            rows[0]["confidence"][field] = confidence;
            cases.push(rows);
        }
        let mut rows = automatic_rows();
        rows[0]["issues"] = json!([{
            "field":field,"code":"UNCERTAIN","message":"模糊字段","severity":"warning"
        }]);
        cases.push(rows);
    }
    let mut by_name = automatic_rows();
    by_name[0]["raw"]["symbol"] = Value::Null;
    by_name[0]["confidence"]["name"] = json!("0.8");
    cases.push(by_name);
    for rows in cases {
        let (dir, mut db) = setup();
        seed_file(dir.path());
        register_automatic(&mut db);
        db.claim_vision(now_ms()).unwrap().unwrap();
        stage_automatic(dir.path(), rows.clone(), automatic_master());
        db.recover_vision(now_ms()).unwrap();
        assert_eq!(
            object(dir.path(), "imports", "automatic")["errorCode"],
            "IMPORT_CONTENT_INVALID",
            "{rows}"
        );
    }
    let (dir, mut db) = setup();
    seed_file(dir.path());
    register_automatic(&mut db);
    db.claim_vision(now_ms()).unwrap().unwrap();
    let mut rows = automatic_rows();
    rows[0]["confidence"]["qty"] = json!("0.9");
    rows[0]["confidence"]["name"] = Value::Null;
    rows[0]["raw"]["availableQty"] = Value::Null;
    rows[0]["confidence"]["availableQty"] = Value::Null;
    rows[0]["issues"] =
        json!([{"field":"lastPrice","code":"MISSING","message":"无现价","severity":"warning"}]);
    stage_automatic(dir.path(), rows, automatic_master());
    db.recover_vision(now_ms()).unwrap();
    assert_eq!(
        object(dir.path(), "imports", "automatic")["status"],
        "confirmed"
    );
}

fn automatic_master() -> Value {
    json!([{"symbol":"600000.SH","name":"浦发银行","exchange":"SSE","instrument":"stock",
        "board":"沪主板","supported":true,"source":{"name":"fixture","ref":"instruments:600000.SH","asOf":1}}])
}

fn register_automatic(db: &mut Storage) {
    assert_eq!(
        run(
            db,
            "accept",
            "accept_vision_notice",
            json!({
                "noticeVersion":tickdesk_local::vision::NOTICE_VERSION,"accepted":true
            })
        )["ok"],
        true
    );
    let registered = run(
        db,
        "automatic",
        "register_image",
        json!({
            "importId":"automatic","fileId":"00000000-0000-4000-8000-000000000001","autoApply":true
        }),
    );
    assert_eq!(registered["ok"], true, "{registered}");
}

fn stage_automatic(dir: &std::path::Path, rows: Value, symbols: Value) {
    let db = rusqlite::Connection::open(dir.join("tickdesk.sqlite3")).unwrap();
    db.execute(
        "INSERT INTO objects VALUES ('vision_received','automatic',?1)",
        [json!({"rows":rows,"symbols":symbols}).to_string()],
    )
    .unwrap();
}

#[test]
fn automatic_import_commits_once_preserves_other_holdings_and_unknown_lots() {
    let (dir, mut db) = setup();
    seed_file(dir.path());
    assert_eq!(
        run(
            &mut db,
            "existing",
            "adjust_position",
            json!({
                "symbol":"000001.SZ","qty":50,"availableQty":50,"cost":"12","pendingLots":[],
                "asOf":now_ms(),"basePositionRevision":0,"reason":"existing holding","resolvedExecutionIds":[]
            })
        )["ok"],
        true
    );
    let untouched = object(dir.path(), "positions", "000001.SZ");
    register_automatic(&mut db);
    let registered = object(dir.path(), "imports", "automatic");
    db.claim_vision(now_ms()).unwrap().unwrap();
    stage_automatic(dir.path(), automatic_rows(), automatic_master());
    drop(db);
    let mut db = Storage::open(dir.path()).unwrap();
    db.recover_vision(now_ms()).unwrap();
    let imported = object(dir.path(), "positions", "600000.SH");
    assert_eq!(imported["qty"], 200);
    assert_eq!(imported["availableQty"], 100);
    assert_eq!(imported["cost"], "10.00");
    assert_eq!(imported["pendingLots"], Value::Null);
    assert_eq!(imported["reconciliationRequired"], true);
    assert_eq!(imported["asOf"], registered["createdAt"]);
    assert_eq!(object(dir.path(), "positions", "000001.SZ"), untouched);
    assert_eq!(
        object(dir.path(), "imports", "automatic")["status"],
        "confirmed"
    );
    db.recover_vision(now_ms()).unwrap();
    assert_eq!(object(dir.path(), "positions", "600000.SH"), imported);
    let conn = rusqlite::Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();
    let count: u64 = conn
        .query_row(
            "SELECT count(*) FROM import_commits WHERE import_id='automatic'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(count, 1);
}

#[test]
fn automatic_import_rejects_any_invalid_row_without_partial_positions() {
    let mut cases = Vec::new();
    cases.push(json!([]));
    for (field, value) in [
        ("qty", json!(null)),
        ("qty", json!("0")),
        ("qty", json!("1.5")),
        ("qty", json!("9007199254740992")),
        ("cost", json!(null)),
        ("cost", json!("-1")),
        ("availableQty", json!("201")),
        ("availableQty", json!("bad")),
        ("symbol", json!("999999")),
        ("name", json!("矛盾名称")),
    ] {
        let mut rows = automatic_rows();
        rows[0]["raw"][field] = value;
        cases.push(rows);
    }
    let mut duplicate = automatic_rows();
    let mut other = duplicate[0].clone();
    other["rowId"] = json!("row2");
    duplicate.as_array_mut().unwrap().push(other);
    cases.push(duplicate);
    let mut partial = automatic_rows();
    let mut bad = partial[0].clone();
    bad["rowId"] = json!("row2");
    bad["raw"]["symbol"] = json!("999999");
    partial.as_array_mut().unwrap().push(bad);
    cases.push(partial);
    for rows in cases {
        let (dir, mut db) = setup();
        seed_file(dir.path());
        register_automatic(&mut db);
        db.claim_vision(now_ms()).unwrap().unwrap();
        stage_automatic(dir.path(), rows.clone(), automatic_master());
        db.recover_vision(now_ms()).unwrap();
        let item = object(dir.path(), "imports", "automatic");
        assert_eq!(item["status"], "failed", "{rows}: {item}");
        assert_eq!(
            item["errorCode"], "IMPORT_CONTENT_INVALID",
            "{rows}: {item}"
        );
        let conn = rusqlite::Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();
        let count: u64 = conn
            .query_row(
                "SELECT count(*) FROM objects WHERE collection='positions'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 0, "{rows}");
    }
}

#[test]
fn automatic_import_allows_unknown_available_but_rejects_concurrent_ledger_change() {
    for concurrent in [false, true] {
        let (dir, mut db) = setup();
        seed_file(dir.path());
        register_automatic(&mut db);
        db.claim_vision(now_ms()).unwrap().unwrap();
        if concurrent {
            assert_eq!(
                run(
                    &mut db,
                    "changed",
                    "adjust_position",
                    json!({
                        "symbol":"000001.SZ","qty":50,"availableQty":null,"cost":"12","pendingLots":null,
                        "asOf":now_ms(),"basePositionRevision":0,"reason":"concurrent change","resolvedExecutionIds":[]
                    })
                )["ok"],
                true
            );
        }
        let mut rows = automatic_rows();
        rows[0]["raw"]["availableQty"] = Value::Null;
        rows[0]["issues"] = json!([{"field":"availableQty","code":"MISSING","message":"截图未提供可用量","severity":"error"}]);
        stage_automatic(dir.path(), rows, automatic_master());
        db.recover_vision(now_ms()).unwrap();
        let item = object(dir.path(), "imports", "automatic");
        if concurrent {
            assert_eq!(item["errorCode"], "IMPORT_DIFF_STALE");
            assert_eq!(item["status"], "failed");
        } else {
            assert_eq!(item["status"], "confirmed");
            assert_eq!(
                object(dir.path(), "positions", "600000.SH")["availableQty"],
                Value::Null
            );
        }
    }
}

#[test]
fn old_draft_only_consent_does_not_authorize_direct_import() {
    let (dir, mut db) = setup();
    seed_file(dir.path());
    let conn = rusqlite::Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();
    conn.execute(
        "UPDATE vision_notice SET version='vision-v1',consented_at=1",
        [],
    )
    .unwrap();
    let result = run(
        &mut db,
        "old-consent",
        "register_image",
        json!({
            "importId":"automatic","fileId":"00000000-0000-4000-8000-000000000001","autoApply":true
        }),
    );
    assert_eq!(result["error"]["code"], "VISION_CONSENT_REQUIRED");
    assert!(db.claim_vision(now_ms()).unwrap().is_none());
    register_automatic(&mut db);
    assert!(db.claim_vision(now_ms()).unwrap().is_some());
}

#[test]
fn automatic_import_does_not_use_name_to_hide_an_ambiguous_security_code() {
    let (dir, mut db) = setup();
    seed_file(dir.path());
    register_automatic(&mut db);
    db.claim_vision(now_ms()).unwrap().unwrap();
    let mut master = automatic_master();
    let mut conflicting = master[0].clone();
    conflicting["symbol"] = json!("600000.SZ");
    conflicting["name"] = json!("另一证券");
    conflicting["exchange"] = json!("SZSE");
    master.as_array_mut().unwrap().push(conflicting);
    stage_automatic(dir.path(), automatic_rows(), master);
    db.recover_vision(now_ms()).unwrap();
    assert_eq!(
        object(dir.path(), "imports", "automatic")["errorCode"],
        "IMPORT_CONTENT_INVALID"
    );
}

#[tokio::test]
async fn automatic_worker_fetches_real_master_and_retries_only_local_atomic_commit() {
    use axum::{routing::post, Json, Router};
    use std::{
        sync::{
            atomic::{AtomicUsize, Ordering},
            Arc, Mutex,
        },
        time::Duration,
    };
    use tickdesk_local::{
        kernel::KernelConfig,
        provider::{AiProvider, AiProviderConfig},
    };
    let calls = Arc::new(AtomicUsize::new(0));
    let counter = calls.clone();
    let app=Router::new().route("/vision",post(move||{let counter=counter.clone();async move{
        counter.fetch_add(1,Ordering::SeqCst);
        Json(json!({"choices":[{"finish_reason":"stop","message":{"content":automatic_rows().to_string()}}]}))
    }})).route("/api/tickdesk/prepare",post(|Json(body):Json<Value>|async move{
        assert_eq!(body["data"]["cmd"],"search_symbols");
        assert_eq!(body["objects"],json!([]));
        let mut master=automatic_master();master[0]["supported"]=json!(false);
        Json(json!({"symbols":master}))
    }));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    let provider = AiProvider::new(&AiProviderConfig {
        endpoint: format!("http://{address}/vision"),
        model: "fixture".into(),
        key_file: None,
        timeout_ms: 1000,
        max_response_bytes: 262144,
    })
    .unwrap();
    let (dir, mut db) = setup();
    seed_file(dir.path());
    register_automatic(&mut db);
    let conn = rusqlite::Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();
    conn.execute_batch("CREATE TRIGGER reject_auto_result BEFORE INSERT ON transport_outbox WHEN json_extract(NEW.frame,'$.payload.kind')='import_result' BEGIN SELECT RAISE(ABORT,'injected failure'); END").unwrap();
    let (shutdown, rx) = tokio::sync::watch::channel(false);
    let worker = tokio::spawn(tickdesk_local::vision::run_with_kernel(
        Arc::new(Mutex::new(db)),
        Some(provider),
        Some(KernelConfig {
            address,
            token_file: None,
        }),
        rx,
    ));
    tokio::time::timeout(Duration::from_secs(3), async {
        while calls.load(Ordering::SeqCst) == 0 {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    tokio::time::sleep(Duration::from_millis(200)).await;
    let count: u64 = conn
        .query_row(
            "SELECT count(*) FROM objects WHERE collection IN ('positions','adjustments')",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(count, 0, "outbox failure must roll back all ledger changes");
    assert_eq!(
        object(dir.path(), "imports", "automatic")["status"],
        "pending"
    );
    conn.execute_batch("DROP TRIGGER reject_auto_result")
        .unwrap();
    tokio::time::timeout(Duration::from_secs(3), async {
        while object(dir.path(), "imports", "automatic")["status"] != "confirmed" {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    assert_eq!(
        object(dir.path(), "positions", "600000.SH")["name"],
        "浦发银行"
    );
    shutdown.send(true).unwrap();
    worker.await.unwrap().unwrap();
    server.abort();
}

#[test]
fn consent_is_durable_and_revocation_returns_null() {
    let (dir, mut db) = setup();
    let accepted = run(
        &mut db,
        "consent",
        "accept_vision_notice",
        json!({"noticeVersion":"vision-v2","accepted":true}),
    );
    assert_eq!(accepted["ok"], true, "{accepted}");
    assert!(accepted["result"]["consentedAt"].is_u64());
    drop(db);
    let mut db = Storage::open(dir.path()).unwrap();
    let revoked = run(
        &mut db,
        "revoke",
        "accept_vision_notice",
        json!({"noticeVersion":"vision-v2","accepted":false}),
    );
    assert_eq!(revoked["ok"], true, "{revoked}");
    assert_eq!(revoked["result"]["consentedAt"], Value::Null);
}

#[test]
fn registration_requires_current_notice_consent() {
    let (_dir, mut db) = setup();
    let result = run(
        &mut db,
        "register",
        "register_image",
        json!({"importId":"import1","fileId":"00000000-0000-4000-8000-000000000001"}),
    );
    assert_eq!(
        result["error"]["code"], "VISION_CONSENT_REQUIRED",
        "{result}"
    );
    let result = run(
        &mut db,
        "obsolete",
        "accept_vision_notice",
        json!({"noticeVersion":"obsolete","accepted":true}),
    );
    assert_eq!(result["error"]["code"], "INVALID_PARAMS", "{result}");
}

fn seed_file(dir: &std::path::Path) {
    use base64::{engine::general_purpose::STANDARD, Engine};
    let bytes=STANDARD.decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGMQ0bABAADMAHm9FcOvAAAAAElFTkSuQmCC").unwrap();
    std::fs::write(
        dir.join("files")
            .join("00000000-0000-4000-8000-000000000001"),
        &bytes,
    )
    .unwrap();
    let conn = rusqlite::Connection::open(dir.join("tickdesk.sqlite3")).unwrap();
    conn.execute("INSERT INTO files VALUES ('00000000-0000-4000-8000-000000000001',?1,1,'sample.png','image/png',?2,?3,'available',1,NULL)",rusqlite::params![LOCAL_PAIR,bytes.len(),tickdesk_local::contract::sha256(&bytes)]).unwrap();
}

fn register(db: &mut Storage) {
    assert_eq!(
        run(
            db,
            "accept",
            "accept_vision_notice",
            json!({"noticeVersion":"vision-v2","accepted":true})
        )["ok"],
        true
    );
    let result = run(
        db,
        "register",
        "register_image",
        json!({"importId":"import1","fileId":"00000000-0000-4000-8000-000000000001"}),
    );
    assert_eq!(result["ok"], true, "{result}");
}

fn recognition() -> Value {
    json!([{"rowId":"row1","raw":{"name":"测试","symbol":null,"qty":"unknown","availableQty":null,"cost":null,"lastPrice":null,"marketValue":null,"pnl":null},
        "confidence":{"name":"0.8","symbol":null,"qty":"0.2","availableQty":null,"cost":null,"lastPrice":null,"marketValue":null,"pnl":null},
        "issues":[{"field":"symbol","code":"MISSING","message":"缺代码，需人工修正","severity":"error"}]}])
}

fn row() -> Value {
    json!({"rowId":"row1","symbol":"600000.SH","name":"测试","qty":100,"availableQty":100,"cost":"12.3400","pendingLots":[],"resolvedExecutionIds":[]})
}

fn object(dir: &std::path::Path, collection: &str, key: &str) -> Value {
    let conn = rusqlite::Connection::open(dir.join("tickdesk.sqlite3")).unwrap();
    let text: String = conn
        .query_row(
            "SELECT body FROM objects WHERE collection=?1 AND object_id=?2",
            [collection, key],
            |r| r.get(0),
        )
        .unwrap();
    serde_json::from_str(&text).unwrap()
}

#[test]
fn cancellation_releases_lease_and_late_response_cannot_revive_import() {
    let (dir, mut db) = setup();
    seed_file(dir.path());
    register(&mut db);
    let request = db.claim_vision(now_ms()).unwrap().unwrap();
    let args =
        json!({"fileId":"00000000-0000-4000-8000-000000000001","baseVersion":1,"confirm":true});
    assert_eq!(
        run(&mut db, "busy", "delete_file", args.clone())["error"]["code"],
        "FILE_BUSY"
    );
    let cancelled = command_event(
        &mut db,
        "cancel",
        "cancel_import",
        json!({"importId":"import1"}),
    );
    assert_eq!(cancelled["data"]["result"]["status"], "cancelled");
    assert!(!db
        .complete_vision(&request, Ok(recognition()), now_ms())
        .unwrap());
    let cancelled_version = object(dir.path(), "imports", "import1")["importVersion"]
        .as_u64()
        .unwrap();
    let deleted = command_event(&mut db, "delete", "delete_file", args);
    assert_eq!(deleted["data"]["ok"], true);
    assert!(deleted["snapshotSeq"].as_u64().unwrap() > cancelled["snapshotSeq"].as_u64().unwrap());
    let item = object(dir.path(), "imports", "import1");
    assert_eq!(item["status"], "cancelled");
    assert_eq!(item["fileStatus"], "deleted");
    assert!(item["importVersion"].as_u64().unwrap() > cancelled_version);
    drop(db);
    let mut db = Storage::open(dir.path()).unwrap();
    let sync = run(&mut db, "sync-after-delete", "sync", json!({}));
    assert_eq!(sync["ok"], true);
    let page = db
        .outbox(64)
        .unwrap()
        .into_iter()
        .map(|frame| frame["payload"].clone())
        .find(|payload| payload["id"] == sync["result"]["pageEventId"])
        .expect("sync returns the authoritative page after restart");
    assert_eq!(page["data"]["imports"], json!([item]));
    assert_eq!(page["data"]["positions"], json!([]));
}

#[test]
fn recognized_rows_require_explicit_resolution_then_confirm_exact_saved_preview() {
    let (dir, mut db) = setup();
    seed_file(dir.path());
    register(&mut db);
    let request = db.claim_vision(now_ms()).unwrap().unwrap();
    db.complete_vision(&request, Ok(recognition()), now_ms())
        .unwrap();
    let result = run(
        &mut db,
        "unresolved",
        "apply_import",
        json!({"importId":"import1","phase":"preview","snapshotAsOf":now_ms(),"rows":[]}),
    );
    assert_eq!(result["error"]["code"], "IMPORT_ROWS_UNRESOLVED");
    let preview = run(
        &mut db,
        "preview",
        "apply_import",
        json!({"importId":"import1","phase":"preview","snapshotAsOf":now_ms(),"rows":[row()]}),
    );
    assert_eq!(preview["ok"], true, "{preview}");
    let item = &preview["result"];
    assert_eq!(item["recognizedRows"], recognition());
    assert_eq!(item["readyForConfirm"], true);
    let args = json!({"importId":"import1","phase":"confirm","importVersion":item["importVersion"],"diffHash":item["diffHash"]});
    let confirmed = run(&mut db, "confirm", "apply_import", args.clone());
    assert_eq!(confirmed["ok"], true, "{confirmed}");
    assert_eq!(confirmed["result"]["addedCount"], 1);
    assert_eq!(
        object(dir.path(), "positions", "600000.SH")["cost"],
        "12.3400"
    );
    let repeated = run(&mut db, "confirm-again", "apply_import", args);
    assert_eq!(repeated["result"]["duplicate"], true, "{repeated}");
    assert_eq!(
        repeated["result"]["positionRevision"],
        confirmed["result"]["positionRevision"]
    );
}

#[test]
fn restart_interrupts_external_request_and_deadline_rejects_late_results() {
    let (dir, mut db) = setup();
    seed_file(dir.path());
    register(&mut db);
    let request = db.claim_vision(now_ms()).unwrap().unwrap();
    drop(db);
    let mut db = Storage::open(dir.path()).unwrap();
    db.recover_vision(now_ms()).unwrap();
    assert!(!db
        .complete_vision(&request, Ok(recognition()), now_ms())
        .unwrap());
    assert_eq!(object(dir.path(), "imports", "import1")["status"], "failed");
    run(
        &mut db,
        "register2",
        "register_image",
        json!({"importId":"import2","fileId":"00000000-0000-4000-8000-000000000001"}),
    );
    let request = db.claim_vision(now_ms()).unwrap().unwrap();
    assert!(!db
        .complete_vision(&request, Ok(recognition()), request.deadline)
        .unwrap());
    assert_eq!(
        object(dir.path(), "imports", "import2")["errorCode"],
        "VISION_DEADLINE_EXCEEDED"
    );
}

#[test]
fn malformed_provider_rows_fail_without_publishing_a_preview() {
    let (dir, mut db) = setup();
    seed_file(dir.path());
    register(&mut db);
    let request = db.claim_vision(now_ms()).unwrap().unwrap();
    db.complete_vision(
        &request,
        Ok(json!([{"rowId":"row1","raw":{},"confidence":{},"issues":[]}])),
        now_ms(),
    )
    .unwrap();
    let item = object(dir.path(), "imports", "import1");
    assert_eq!(item["errorCode"], "VISION_INVALID_RESPONSE");
    assert_eq!(item["recognizedRows"], json!([]));
    assert_eq!(item["status"], "failed");
}

#[test]
fn revocation_cancels_pending_and_recognized_imports_and_blocks_late_completion() {
    let (dir, mut db) = setup();
    seed_file(dir.path());
    register(&mut db);
    let request = db.claim_vision(now_ms()).unwrap().unwrap();
    run(
        &mut db,
        "register2",
        "register_image",
        json!({"importId":"import2","fileId":"00000000-0000-4000-8000-000000000001"}),
    );
    let recognized = db.claim_vision(now_ms()).unwrap().unwrap();
    db.complete_vision(&recognized, Ok(recognition()), now_ms())
        .unwrap();
    assert_eq!(
        run(
            &mut db,
            "revoke",
            "accept_vision_notice",
            json!({"noticeVersion":"vision-v2","accepted":false})
        )["ok"],
        true
    );
    assert!(!db
        .complete_vision(&request, Ok(recognition()), now_ms())
        .unwrap());
    assert_eq!(
        object(dir.path(), "imports", "import1")["status"],
        "cancelled"
    );
    assert_eq!(
        object(dir.path(), "imports", "import2")["status"],
        "cancelled"
    );
    assert!(db.claim_vision(now_ms()).unwrap().is_none());
}

#[test]
fn stale_preview_cannot_write_and_removal_requires_separate_confirmation() {
    let (dir, mut db) = setup();
    seed_file(dir.path());
    register(&mut db);
    let request = db.claim_vision(now_ms()).unwrap().unwrap();
    db.complete_vision(&request, Ok(recognition()), now_ms())
        .unwrap();
    let preview = run(
        &mut db,
        "preview",
        "apply_import",
        json!({"importId":"import1","phase":"preview","snapshotAsOf":now_ms(),"rows":[row()]}),
    );
    let item = &preview["result"];
    assert_eq!(
        run(
            &mut db,
            "adjust",
            "adjust_position",
            json!({"symbol":"000001.SZ","basePositionRevision":0,"asOf":now_ms(),"qty":100,"availableQty":100,"cost":"1","pendingLots":[],"resolvedExecutionIds":[],"reason":"测试基线","confirmZero":false})
        )["ok"],
        true
    );
    let confirm = json!({"importId":"import1","phase":"confirm","importVersion":item["importVersion"],"diffHash":item["diffHash"]});
    assert_eq!(
        run(&mut db, "stale", "apply_import", confirm)["error"]["code"],
        "IMPORT_DIFF_STALE"
    );
    let preview = run(
        &mut db,
        "preview2",
        "apply_import",
        json!({"importId":"import1","phase":"preview","snapshotAsOf":now_ms(),"rows":[row()],"missingActions":[{"symbol":"000001.SZ","action":"delete"}]}),
    );
    let item = &preview["result"];
    let mut confirm = json!({"importId":"import1","phase":"confirm","importVersion":item["importVersion"],"diffHash":item["diffHash"]});
    assert_eq!(
        run(&mut db, "unconfirmed", "apply_import", confirm.clone())["error"]["code"],
        "IMPORT_REMOVALS_UNCONFIRMED"
    );
    assert_eq!(object(dir.path(), "positions", "000001.SZ")["qty"], 100);
    confirm["confirmRemovals"] = json!(true);
    let result = run(&mut db, "confirmed", "apply_import", confirm);
    assert_eq!(result["result"]["deletedCount"], 1, "{result}");
    assert_eq!(result["result"]["positionCount"], 1);
}

#[test]
fn failed_ack_commit_rolls_back_positions_audit_and_import_then_retries_once() {
    let (dir, mut db) = setup();
    seed_file(dir.path());
    register(&mut db);
    let request = db.claim_vision(now_ms()).unwrap().unwrap();
    db.complete_vision(&request, Ok(recognition()), now_ms())
        .unwrap();
    let preview = run(
        &mut db,
        "preview",
        "apply_import",
        json!({"importId":"import1","phase":"preview","snapshotAsOf":now_ms(),"rows":[row()]}),
    );
    let item = &preview["result"];
    let payload = command(
        &db,
        "confirm",
        "confirm",
        "apply_import",
        json!({"importId":"import1","phase":"confirm","importVersion":item["importVersion"],"diffHash":item["diffHash"]}),
    );
    db.receive(&deliver(&db, PEER, "confirm", &payload))
        .unwrap();
    let conn = rusqlite::Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();
    conn.execute_batch("CREATE TRIGGER fail_vision_ack BEFORE INSERT ON command_results WHEN NEW.client_msg_id='confirm' BEGIN SELECT RAISE(ABORT,'injected failure'); END").unwrap();
    assert!(db
        .process(db.job("confirm").unwrap().unwrap(), now_ms())
        .is_err());
    let count: u64 = conn
        .query_row(
            "SELECT count(*) FROM objects WHERE collection IN ('positions','adjustments')",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(count, 0);
    assert_eq!(
        object(dir.path(), "imports", "import1")["status"],
        "recognized"
    );
    conn.execute_batch("DROP TRIGGER fail_vision_ack").unwrap();
    assert_eq!(
        db.process(db.job("confirm").unwrap().unwrap(), now_ms())
            .unwrap()
            .unwrap()["data"]["ok"],
        true
    );
}

#[tokio::test]
async fn real_http_adapter_sends_only_consented_image_and_preserves_uncertain_transcription() {
    use axum::{routing::post, Json, Router};
    use tickdesk_local::{
        provider::{AiProvider, AiProviderConfig},
        vision,
    };
    let app=Router::new().route("/vision",post(|Json(body):Json<Value>|async move {
        assert_eq!(body["model"],"local-fixture");
        assert!(body["messages"][1]["content"][0]["image_url"]["url"].as_str().unwrap().starts_with("data:image/png;base64,"));
        Json(json!({"choices":[{"finish_reason":"stop","message":{"content":recognition().to_string()}}]}))
    }));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    let provider = AiProvider::new(&AiProviderConfig {
        endpoint: format!("http://{addr}/vision"),
        model: "local-fixture".into(),
        key_file: None,
        timeout_ms: 1000,
        max_response_bytes: 262144,
    })
    .unwrap();
    let (dir, mut db) = setup();
    seed_file(dir.path());
    register(&mut db);
    let request = db.claim_vision(now_ms()).unwrap().unwrap();
    let rows = vision::recognize(&provider, &request).await.unwrap();
    assert_eq!(rows, recognition());
    db.complete_vision(&request, Ok(rows), now_ms()).unwrap();
    assert_eq!(
        object(dir.path(), "imports", "import1")["readyForConfirm"],
        false
    );
    server.abort();
}

#[tokio::test]
async fn worker_without_credentials_finishes_with_explicit_unavailable_error() {
    use std::{
        sync::{Arc, Mutex},
        time::Duration,
    };
    let (dir, mut db) = setup();
    seed_file(dir.path());
    register(&mut db);
    let storage = Arc::new(Mutex::new(db));
    let (shutdown, rx) = tokio::sync::watch::channel(false);
    let worker = tokio::spawn(tickdesk_local::vision::run(storage.clone(), None, rx));
    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            if object(dir.path(), "imports", "import1")["status"] == "failed" {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    shutdown.send(true).unwrap();
    worker.await.unwrap().unwrap();
    let item = object(dir.path(), "imports", "import1");
    assert_eq!(item["errorCode"], "AI_NOT_CONFIGURED");
    assert_eq!(item["recognizedRows"], json!([]));
}

#[test]
fn peer_replacement_quarantines_running_recognition_and_releases_lease() {
    let (dir, mut db) = setup();
    seed_file(dir.path());
    register(&mut db);
    let request = db.claim_vision(now_ms()).unwrap().unwrap();
    db.bind_peer(LOCAL_PAIR, "replacement-peer").unwrap();
    assert!(!db
        .complete_vision(&request, Ok(recognition()), now_ms())
        .unwrap());
    let conn = rusqlite::Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();
    let state: String = conn
        .query_row(
            "SELECT state FROM vision_jobs WHERE import_id='import1'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(state, "failed");
    assert_eq!(object(dir.path(), "imports", "import1")["status"], "failed");
}

#[test]
fn unknown_lots_remain_unknown_and_flag_position_for_reconciliation() {
    let (dir, mut db) = setup();
    seed_file(dir.path());
    register(&mut db);
    let request = db.claim_vision(now_ms()).unwrap().unwrap();
    db.complete_vision(&request, Ok(recognition()), now_ms())
        .unwrap();
    let mut unknown = row();
    unknown["availableQty"] = Value::Null;
    unknown["pendingLots"] = Value::Null;
    let preview = run(
        &mut db,
        "preview",
        "apply_import",
        json!({"importId":"import1","phase":"preview","snapshotAsOf":now_ms(),"rows":[unknown]}),
    );
    let item = &preview["result"];
    let result = run(
        &mut db,
        "confirm",
        "apply_import",
        json!({"importId":"import1","phase":"confirm","importVersion":item["importVersion"],"diffHash":item["diffHash"]}),
    );
    assert_eq!(result["ok"], true, "{result}");
    let position = object(dir.path(), "positions", "600000.SH");
    assert_eq!(position["availableQty"], Value::Null);
    assert_eq!(position["pendingLots"], Value::Null);
    assert_eq!(position["reconciliationRequired"], true);
}

#[test]
fn confirmed_import_uses_current_verified_quote_for_valuation() {
    let (dir, mut db) = setup();
    seed_file(dir.path());
    register(&mut db);
    let request = db.claim_vision(now_ms()).unwrap().unwrap();
    db.complete_vision(&request, Ok(recognition()), now_ms())
        .unwrap();
    let preview = run(
        &mut db,
        "preview",
        "apply_import",
        json!({"importId":"import1","phase":"preview","snapshotAsOf":now_ms(),"rows":[row()]}),
    );
    let quote = json!({"symbol":"600000.SH","price":"13","asOf":now_ms(),"status":"fresh","source":{"name":"test-local","ref":"fixture","asOf":now_ms()}});
    let conn = rusqlite::Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();
    conn.execute(
        "INSERT INTO objects VALUES ('market_quotes','600000.SH',?1)",
        [quote.to_string()],
    )
    .unwrap();
    let item = &preview["result"];
    let result = run(
        &mut db,
        "confirm",
        "apply_import",
        json!({"importId":"import1","phase":"confirm","importVersion":item["importVersion"],"diffHash":item["diffHash"]}),
    );
    assert_eq!(result["result"]["valuation"]["positionMarketValue"], "1300");
}

#[tokio::test]
async fn worker_retries_completion_commit_without_repeating_external_request() {
    use axum::{routing::post, Json, Router};
    use std::{
        sync::{
            atomic::{AtomicUsize, Ordering},
            Arc, Mutex,
        },
        time::Duration,
    };
    use tickdesk_local::provider::{AiProvider, AiProviderConfig};
    let calls = Arc::new(AtomicUsize::new(0));
    let counter = calls.clone();
    let app=Router::new().route("/vision",post(move||{let counter=counter.clone();async move {
        counter.fetch_add(1,Ordering::SeqCst);
        Json(json!({"choices":[{"finish_reason":"stop","message":{"content":recognition().to_string()}}]}))
    }}));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    let provider = AiProvider::new(&AiProviderConfig {
        endpoint: format!("http://{addr}/vision"),
        model: "fixture".into(),
        key_file: None,
        timeout_ms: 1000,
        max_response_bytes: 262144,
    })
    .unwrap();
    let (dir, mut db) = setup();
    seed_file(dir.path());
    register(&mut db);
    let conn = rusqlite::Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();
    conn.execute_batch("CREATE TRIGGER reject_vision_result BEFORE INSERT ON transport_outbox WHEN json_extract(NEW.frame,'$.payload.kind')='import_result' BEGIN SELECT RAISE(ABORT,'injected failure'); END").unwrap();
    let storage = Arc::new(Mutex::new(db));
    let (shutdown, rx) = tokio::sync::watch::channel(false);
    let worker = tokio::spawn(tickdesk_local::vision::run(storage, Some(provider), rx));
    tokio::time::timeout(Duration::from_secs(2), async {
        while calls.load(Ordering::SeqCst) == 0 {
            tokio::time::sleep(Duration::from_millis(10)).await
        }
    })
    .await
    .unwrap();
    tokio::time::sleep(Duration::from_millis(150)).await;
    assert!(
        !worker.is_finished(),
        "transient completion failure must not stop worker"
    );
    conn.execute_batch("DROP TRIGGER reject_vision_result")
        .unwrap();
    tokio::time::timeout(Duration::from_secs(2), async {
        while object(dir.path(), "imports", "import1")["status"] != "recognized" {
            tokio::time::sleep(Duration::from_millis(10)).await
        }
    })
    .await
    .unwrap();
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    shutdown.send(true).unwrap();
    worker.await.unwrap().unwrap();
    server.abort();
}
