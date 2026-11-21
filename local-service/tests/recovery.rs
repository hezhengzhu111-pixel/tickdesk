mod support;
use base64::{engine::general_purpose::STANDARD, Engine};
use serde_json::{json, Value};
use support::{command, deliver, LOCAL_PAIR, PEER};
use tickdesk_local::{
    contract::{canonical, digest},
    storage::Storage,
};

fn call(db: &mut Storage, key: &str, name: &str, data: Value) -> Value {
    let input = command(db, key, key, name, data);
    db.receive(&deliver(db, PEER, key, &input)).unwrap();
    db.process(db.job(key).unwrap().unwrap(), 1794792300100)
        .unwrap()
        .unwrap()["data"]
        .clone()
}

#[test]
fn unknown_buffered_trade_cannot_be_defaulted_to_discarded() {
    let tmp = tempfile::tempdir().unwrap();
    let source = tmp.path().join("source");
    let backup = tmp.path().join("backup");
    let restored = tmp.path().join("restored");
    let mut db = Storage::open(&source).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    db.backup_to(&backup).unwrap();
    let original = command(
        &db,
        "unknown-fill",
        "unknown-fill",
        "record_execution",
        json!({"symbol":"300502.SZ","side":"buy","price":"10","qty":10,
            "executedAt":1794792300000_u64}),
    );
    let old_epoch = db.identity.server_epoch.clone();
    db.receive(&deliver(&db, PEER, "unknown-fill-delivery", &original))
        .unwrap();
    drop(db);
    Storage::restore_from(&backup, &restored).unwrap();
    let mut db = Storage::open(&restored).unwrap();
    let session = call(&mut db, "unknown-session", "get_recovery", json!({}));
    let id = session["result"]["reconciliationId"].clone();
    let evidence = call(
        &mut db,
        "unknown-evidence",
        "recovery_evidence",
        json!({
        "reconciliationId":id,"recordId":"unknown-command","sourceType":"command",
        "payloadHash":digest(&original).unwrap(),"partIndex":0,"partCount":1,
        "bytesBase64":STANDARD.encode(canonical(&original).unwrap()),
        "evidenceEpoch":old_epoch,"capturedAt":1794792300100_u64,"observedSnapshotSeq":0}),
    );
    assert_eq!(
        evidence["result"]["record"]["status"], "verified",
        "{evidence}"
    );
    let review = call(&mut db, "unknown-review", "get_recovery", json!({}));
    let item = &review["result"]["items"][0];
    assert!(item["allowedDecisions"]
        .as_array()
        .unwrap()
        .contains(&json!("discard_intent")));
    let discarded = call(
        &mut db,
        "unknown-discard",
        "recovery_reconcile",
        json!({
        "reconciliationId":id,"reviewedSnapshotSeq":review["result"]["reviewedSnapshotSeq"],
        "positions":[],"decisions":[{
            "itemId":item["itemId"],"evidenceRecordIds":["unknown-command"],
            "disposition":"discard_intent","note":"not_found is not proof"
        }]}),
    );
    assert_eq!(discarded["ok"], false, "{discarded}");
    assert_eq!(discarded["error"]["code"], "RECOVERY_EVIDENCE_INCOMPLETE");
}

#[test]
fn deterministic_rejection_after_the_backup_can_discard_the_old_intent() {
    let tmp = tempfile::tempdir().unwrap();
    let source = tmp.path().join("source");
    let backup = tmp.path().join("backup");
    let restored = tmp.path().join("restored");
    let mut db = Storage::open(&source).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    db.backup_to(&backup).unwrap();
    let original = command(
        &db,
        "rejected-fill",
        "rejected-fill",
        "record_execution",
        json!({"symbol":"300502.SZ","side":"buy","price":"10","qty":10,
            "executedAt":1794792300100_u64 + 60_000}),
    );
    let old_epoch = db.identity.server_epoch.clone();
    db.receive(&deliver(&db, PEER, "rejected-fill-delivery", &original))
        .unwrap();
    let ack = db
        .process(
            db.job("rejected-fill-delivery").unwrap().unwrap(),
            1794792300100,
        )
        .unwrap()
        .unwrap();
    assert_eq!(ack["data"]["ok"], false, "{ack}");
    assert_eq!(ack["data"]["error"]["code"], "INVALID_PARAMS", "{ack}");
    drop(db);
    Storage::restore_from(&backup, &restored).unwrap();
    let mut db = Storage::open(&restored).unwrap();
    let session = call(&mut db, "rejected-session", "get_recovery", json!({}));
    let id = session["result"]["reconciliationId"].clone();
    for (key, kind, body) in [
        ("rejected-command", "command", &original),
        ("rejected-ack", "ack", &ack),
    ] {
        let response = call(
            &mut db,
            key,
            "recovery_evidence",
            json!({
            "reconciliationId":id,"recordId":key,"sourceType":kind,"payloadHash":digest(body).unwrap(),
            "partIndex":0,"partCount":1,"bytesBase64":STANDARD.encode(canonical(body).unwrap()),
            "evidenceEpoch":old_epoch,"capturedAt":1794792300100_u64,
            "observedSnapshotSeq":ack["snapshotSeq"]}),
        );
        assert_eq!(
            response["result"]["record"]["status"], "verified",
            "{response}"
        );
    }
    let review = call(&mut db, "rejected-review", "get_recovery", json!({}));
    let item = &review["result"]["items"][0];
    assert!(item["allowedDecisions"]
        .as_array()
        .unwrap()
        .contains(&json!("discard_intent")));
    let discarded = call(
        &mut db,
        "rejected-discard",
        "recovery_reconcile",
        json!({
        "reconciliationId":id,"reviewedSnapshotSeq":review["result"]["reviewedSnapshotSeq"],
        "positions":[],"decisions":[{
            "itemId":item["itemId"],"evidenceRecordIds":["rejected-command","rejected-ack"],
            "disposition":"discard_intent","note":"Deterministic INVALID_PARAMS rejection matched by request hash."
        }]}),
    );
    assert_eq!(discarded["ok"], true, "{discarded}");
    assert_eq!(discarded["result"]["remainingCount"], 0);
}

#[test]
fn non_trade_success_is_manually_archived_without_replaying_or_creating_a_fill() {
    let tmp = tempfile::tempdir().unwrap();
    let source = tmp.path().join("source");
    let backup = tmp.path().join("backup");
    let restored = tmp.path().join("restored");
    let mut db = Storage::open(&source).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    db.backup_to(&backup).unwrap();
    let original = command(&db, "old-pause", "old-pause", "pause", json!({}));
    let old_epoch = db.identity.server_epoch.clone();
    db.receive(&deliver(&db, PEER, "old-delivery", &original))
        .unwrap();
    let ack = db
        .process(db.job("old-delivery").unwrap().unwrap(), 1794792300000)
        .unwrap()
        .unwrap();
    drop(db);
    Storage::restore_from(&backup, &restored).unwrap();
    let mut db = Storage::open(&restored).unwrap();
    let session = call(&mut db, "session", "get_recovery", json!({}));
    let id = session["result"]["reconciliationId"].clone();
    for (key, kind, body) in [("command", "command", &original), ("ack", "ack", &ack)] {
        let response = call(
            &mut db,
            key,
            "recovery_evidence",
            json!({
            "reconciliationId":id,"recordId":key,"sourceType":kind,"payloadHash":digest(body).unwrap(),
            "partIndex":0,"partCount":1,"bytesBase64":STANDARD.encode(canonical(body).unwrap()),
            "evidenceEpoch":old_epoch,"capturedAt":1794792300100_u64,"observedSnapshotSeq":ack["snapshotSeq"]}),
        );
        assert_eq!(
            response["result"]["record"]["status"], "verified",
            "{response}"
        );
    }
    let review = call(&mut db, "review", "get_recovery", json!({}));
    let item = &review["result"]["items"][0];
    let seq = review["result"]["reviewedSnapshotSeq"].clone();
    let bad = call(
        &mut db,
        "unrelated-fill",
        "recovery_reconcile",
        json!({
        "reconciliationId":id,"reviewedSnapshotSeq":seq,"positions":[],
        "decisions":[{"itemId":item["itemId"],"evidenceRecordIds":["command","ack"],"disposition":"record",
            "execution":{"symbol":"300502.SZ","side":"buy","price":"10","qty":10,"executedAt":1794792300000_u64,"settlement":"T+1"},
            "newExecutionId":"unrelated-fill","note":"must not invent a trade for pause"}]}),
    );
    assert_eq!(
        bad["ok"], false,
        "non-trade recovery must not create a fill: {bad}"
    );
    assert!(item["allowedDecisions"]
        .as_array()
        .unwrap()
        .contains(&json!("archive_operation")));
    let archived = call(
        &mut db,
        "archive",
        "recovery_reconcile",
        json!({
        "reconciliationId":id,"reviewedSnapshotSeq":seq,"positions":[],
        "decisions":[{"itemId":item["itemId"],"evidenceRecordIds":["command","ack"],"disposition":"archive_operation",
            "note":"Reviewed old pause; retain the restored state and change settings later if needed."}]}),
    );
    assert_eq!(archived["ok"], true, "{archived}");
    assert_eq!(archived["result"]["remainingCount"], 0);
    let status = call(&mut db, "current-state", "get_status", json!({}));
    assert_eq!(status["result"]["analysis"]["paused"], false);
    assert_eq!(status["result"]["analysis"]["controlVersion"], 0);
    let diagnostic = rusqlite::Connection::open(restored.join("tickdesk.sqlite3")).unwrap();
    let fills: i64 = diagnostic
        .query_row(
            "SELECT count(*) FROM objects WHERE collection='executions'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(fills, 0);
    let manifest = json!([
        {"recordId":"ack","sourceType":"ack","payloadHash":digest(&ack).unwrap()},
        {"recordId":"command","sourceType":"command","payloadHash":digest(&original).unwrap()}]);
    let seq = &archived["result"]["reviewedSnapshotSeq"];
    let sealed = call(
        &mut db,
        "seal",
        "recovery_seal_evidence",
        json!({"reconciliationId":id,
        "reviewedSnapshotSeq":seq,"recordCount":2,"manifestHash":digest(&manifest).unwrap()}),
    );
    assert_eq!(sealed["ok"], true, "{sealed}");
    let confirmed = call(
        &mut db,
        "confirm",
        "recovery_confirm",
        json!({"reconciliationId":id,"reviewedSnapshotSeq":seq}),
    );
    assert_eq!(confirmed["ok"], true, "{confirmed}");
    assert!(!db.recovery_active().unwrap());
}

#[test]
fn missing_parts_block_seal_and_restart_preserves_evidence_without_executing_original_command() {
    let tmp = tempfile::tempdir().unwrap();
    let mut db = Storage::open(tmp.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let original = command(&db, "old-pause", "old-pause", "pause", json!({}));
    let old_epoch = db.identity.server_epoch.clone();
    db.enter_recovery().unwrap();
    let recovery = call(&mut db, "status", "get_recovery", json!({}));
    let id = &recovery["result"]["reconciliationId"];
    let bytes = canonical(&original).unwrap();
    let hash = digest(&original).unwrap();
    let evidence = |index, part: &[u8]| {
        json!({
            "reconciliationId":id,"recordId":"r1","sourceType":"command","payloadHash":hash,
            "partIndex":index,"partCount":2,"bytesBase64":STANDARD.encode(part),"evidenceEpoch":old_epoch,
            "capturedAt":1794792300000_u64,"observedSnapshotSeq":null
        })
    };
    let half = bytes.len() / 2;
    let incomplete = call(
        &mut db,
        "first",
        "recovery_evidence",
        evidence(1, &bytes[half..]),
    );
    assert_eq!(incomplete["result"]["record"]["status"], "receiving");
    let empty_hash = digest(&json!([])).unwrap();
    let seal = call(
        &mut db,
        "seal-1",
        "recovery_seal_evidence",
        json!({"reconciliationId":id,
        "reviewedSnapshotSeq":incomplete["result"]["reviewedSnapshotSeq"],"recordCount":0,"manifestHash":empty_hash}),
    );
    assert_eq!(seal["error"]["code"], "RECOVERY_EVIDENCE_INCOMPLETE");
    drop(db);
    let mut db = Storage::open(tmp.path()).unwrap();
    let complete = call(
        &mut db,
        "second",
        "recovery_evidence",
        evidence(0, &bytes[..half]),
    );
    assert_eq!(complete["result"]["record"]["status"], "verified");
    assert_eq!(complete["result"]["record"]["receivedParts"], 2);
    let manifest =
        digest(&json!([{"recordId":"r1","sourceType":"command","payloadHash":hash}])).unwrap();
    let seal = call(
        &mut db,
        "seal-2",
        "recovery_seal_evidence",
        json!({"reconciliationId":id,
        "reviewedSnapshotSeq":complete["result"]["reviewedSnapshotSeq"],"recordCount":1,"manifestHash":manifest}),
    );
    assert_eq!(seal["result"]["evidenceStatus"], "sealed");
    let review = call(&mut db, "review", "get_recovery", json!({}));
    assert_eq!(review["result"]["items"].as_array().unwrap().len(), 1);
    assert!(db.recovery_active().unwrap());
    let status = call(&mut db, "domain", "get_status", json!({}));
    assert_eq!(status["result"]["analysis"]["paused"], false);
}

#[test]
fn same_batch_new_execution_can_be_resolved_before_seal_and_confirmation() {
    let tmp = tempfile::tempdir().unwrap();
    let mut db = Storage::open(tmp.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let old_epoch = db.identity.server_epoch.clone();
    let execution = json!({"symbol":"300502.SZ","side":"buy","price":"10.000","qty":10,
        "executedAt":1794792300000_u64,"settlement":"T+1"});
    let old = command(
        &db,
        "from-app",
        "original-fill",
        "record_execution",
        execution.clone(),
    );
    db.enter_recovery().unwrap();
    let status = call(&mut db, "session", "get_recovery", json!({}));
    let id = &status["result"]["reconciliationId"];
    let hash = digest(&old).unwrap();
    let proof = call(
        &mut db,
        "evidence",
        "recovery_evidence",
        json!({"reconciliationId":id,"recordId":"original","sourceType":"command",
        "payloadHash":hash,"partIndex":0,"partCount":1,"bytesBase64":STANDARD.encode(canonical(&old).unwrap()),
        "evidenceEpoch":old_epoch,"capturedAt":1794792300000_u64,"observedSnapshotSeq":null}),
    );
    assert_eq!(proof["result"]["record"]["status"], "verified");
    let status = call(&mut db, "items", "get_recovery", json!({}));
    let item = &status["result"]["items"][0]["itemId"];
    let archived = call(
        &mut db,
        "cannot-archive-fill",
        "recovery_reconcile",
        json!({
        "reconciliationId":id,"reviewedSnapshotSeq":status["result"]["reviewedSnapshotSeq"],"positions":[],
        "decisions":[{"itemId":item,"evidenceRecordIds":["original"],"disposition":"archive_operation","note":"must keep trade reconciliation"}]}),
    );
    assert_eq!(archived["ok"], false);
    let reconciled = call(
        &mut db,
        "decision",
        "recovery_reconcile",
        json!({
            "reconciliationId":id,"reviewedSnapshotSeq":status["result"]["reviewedSnapshotSeq"],
            "decisions":[{"itemId":item,"evidenceRecordIds":["original"],"disposition":"record","execution":execution,
                "newExecutionId":"created-during-recovery","note":"verified manual fill"}],
            "positions":[{"symbol":"300502.SZ","basePositionRevision":0,"qty":10,"availableQty":0,"cost":"10.000",
                "asOf":1794792300000_u64,"pendingLots":[{"qty":10,"sellableDate":"2026-11-17"}],
                "resolvedExecutionIds":["created-during-recovery"],"reason":"confirmed broker statement"}]
        }),
    );
    assert_eq!(reconciled["result"]["remainingCount"], 0);
    assert_eq!(
        reconciled["result"]["recordedExecutions"][0]["executionId"],
        "created-during-recovery"
    );
    let seq = &reconciled["result"]["reviewedSnapshotSeq"];
    let no_seal = call(
        &mut db,
        "without-seal",
        "recovery_confirm",
        json!({"reconciliationId":id,"reviewedSnapshotSeq":seq}),
    );
    assert_eq!(no_seal["error"]["code"], "RECOVERY_EVIDENCE_INCOMPLETE");
    let manifest =
        digest(&json!([{"recordId":"original","sourceType":"command","payloadHash":hash}]))
            .unwrap();
    let seal = call(
        &mut db,
        "seal",
        "recovery_seal_evidence",
        json!({"reconciliationId":id,"reviewedSnapshotSeq":seq,
        "recordCount":1,"manifestHash":manifest}),
    );
    assert_eq!(seal["result"]["evidenceStatus"], "sealed");
    let confirmed = call(
        &mut db,
        "confirmed",
        "recovery_confirm",
        json!({"reconciliationId":id,"reviewedSnapshotSeq":seq}),
    );
    assert_eq!(confirmed["result"]["recoveryMode"], false);
    assert!(!db.recovery_active().unwrap());
}

#[test]
fn restored_position_adjustment_requires_an_explicit_current_baseline() {
    let tmp = tempfile::tempdir().unwrap();
    let mut db = Storage::open(&tmp.path().join("source")).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let backup = tmp.path().join("backup");
    db.backup_to(&backup).unwrap();
    let baseline = json!({"symbol":"300502.SZ","basePositionRevision":0,"qty":10,"availableQty":10,
        "pendingLots":[],"cost":"10","asOf":1794792300000_u64,"reason":"manual baseline","resolvedExecutionIds":[]});
    let original = command(
        &db,
        "old-adjust",
        "old-adjust",
        "adjust_position",
        baseline.clone(),
    );
    let epoch = db.identity.server_epoch.clone();
    db.receive(&deliver(&db, PEER, "old-adjust", &original))
        .unwrap();
    let ack = db
        .process(db.job("old-adjust").unwrap().unwrap(), 1794792300000)
        .unwrap()
        .unwrap();
    assert_eq!(ack["data"]["ok"], true);
    drop(db);
    let restored = tmp.path().join("restored");
    Storage::restore_from(&backup, &restored).unwrap();
    let mut db = Storage::open(&restored).unwrap();
    let status = call(&mut db, "session", "get_recovery", json!({}));
    let id = status["result"]["reconciliationId"].clone();
    for (key, kind, body) in [("command", "command", &original), ("ack", "ack", &ack)] {
        let result = call(
            &mut db,
            key,
            "recovery_evidence",
            json!({"reconciliationId":id,"recordId":key,
            "sourceType":kind,"payloadHash":digest(body).unwrap(),"partIndex":0,"partCount":1,
            "bytesBase64":STANDARD.encode(canonical(body).unwrap()),"evidenceEpoch":epoch,
            "capturedAt":1794792300100_u64,"observedSnapshotSeq":ack["snapshotSeq"]}),
        );
        assert_eq!(result["result"]["record"]["status"], "verified");
    }
    let status = call(&mut db, "items", "get_recovery", json!({}));
    let decision = json!({"itemId":status["result"]["items"][0]["itemId"],
        "evidenceRecordIds":["command","ack"],"disposition":"included","note":"current position independently checked"});
    let mut request = json!({"reconciliationId":id,"reviewedSnapshotSeq":status["result"]["reviewedSnapshotSeq"],
        "decisions":[decision],"positions":[]});
    assert_eq!(
        call(
            &mut db,
            "no-baseline",
            "recovery_reconcile",
            request.clone()
        )["ok"],
        false
    );
    let mut wrong = baseline.clone();
    wrong["symbol"] = json!("000001.SZ");
    request["positions"] = json!([wrong]);
    assert_eq!(
        call(
            &mut db,
            "wrong-symbol",
            "recovery_reconcile",
            request.clone()
        )["ok"],
        false
    );
    let mut early = baseline.clone();
    early["asOf"] = json!(1794792299999_u64);
    request["positions"] = json!([early]);
    assert_eq!(
        call(
            &mut db,
            "old-baseline",
            "recovery_reconcile",
            request.clone()
        )["ok"],
        false
    );
    request["positions"] = json!([baseline]);
    let accepted = call(&mut db, "with-baseline", "recovery_reconcile", request);
    assert_eq!(accepted["ok"], true, "{accepted}");
    assert_eq!(accepted["result"]["remainingCount"], 0);
    let diagnostic = rusqlite::Connection::open(restored.join("tickdesk.sqlite3")).unwrap();
    let fills: i64 = diagnostic
        .query_row(
            "SELECT count(*) FROM objects WHERE collection='executions'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(fills, 0);
}
