mod support;

use serde_json::{json, Value};
use support::{command, deliver, status_query, LOCAL_PAIR, PEER};
use tickdesk_local::storage::Storage;

fn bound(directory: &std::path::Path) -> Storage {
    let mut storage = Storage::open(directory).unwrap();
    storage.bind_peer(LOCAL_PAIR, PEER).unwrap();
    storage
}

#[test]
fn inbox_and_first_ack_survive_restart_and_outbox_failure() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = bound(dir.path());
    let original = command(
        &db,
        "input-1",
        "command-1",
        "get_command_status",
        json!({"originalEpoch":db.identity.server_epoch,"targetClientMsgId":"missing","targetRequestHash":"0".repeat(64)}),
    );
    let frame = deliver(&db, PEER, "delivery-1", &original);
    let receipt = db.receive(&frame).unwrap();
    assert_eq!(receipt["type"], "receipt");
    assert_eq!(receipt["fromIncarnation"], frame["fromIncarnation"]);
    assert_eq!(receipt["toIncarnation"], frame["toIncarnation"]);
    assert_eq!(receipt["payloadHash"], frame["payloadHash"]);
    drop(db);

    let mut db = bound(dir.path());
    assert_eq!(db.receive(&frame).unwrap(), receipt);
    let job = db.pending().unwrap().unwrap();
    let fault = rusqlite::Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();
    fault.execute_batch("CREATE TRIGGER fail_send BEFORE INSERT ON transport_outbox BEGIN SELECT RAISE(ABORT,'injected fault'); END;").unwrap();
    assert!(db.process(job.clone(), 100).is_err());
    assert!(db.outbox(16).unwrap().is_empty());
    fault.execute_batch("DROP TRIGGER fail_send").unwrap();
    assert!(db.pending().unwrap().is_some());
    let first = db.process(job, 200).unwrap().unwrap();
    assert_eq!(first["data"]["refId"], "input-1");
    assert_eq!(first["data"]["result"]["state"], "not_found");
    drop(db);

    let mut db = bound(dir.path());
    let sent = db.outbox(16).unwrap().remove(0);
    assert_eq!(sent["payload"], first);
    let mut receipt = sent.clone();
    receipt["type"] = json!("receipt");
    receipt.as_object_mut().unwrap().remove("category");
    receipt.as_object_mut().unwrap().remove("payload");
    let mut wrong = receipt.clone();
    wrong["toIncarnation"] = json!("other-peer");
    assert!(!db.accept_receipt(&wrong).unwrap());
    assert!(db.accept_receipt(&receipt).unwrap());
    assert!(db.outbox(16).unwrap().is_empty());

    let mut duplicate = original.clone();
    duplicate["id"] = json!("input-2");
    duplicate["ts"] = json!(1000);
    db.receive(&deliver(&db, PEER, "delivery-2", &duplicate))
        .unwrap();
    let replay = db
        .process(db.pending().unwrap().unwrap(), 1000)
        .unwrap()
        .unwrap();
    assert_eq!(replay, first);

    let mut changed = original.clone();
    changed["data"]["targetClientMsgId"] = json!("another");
    assert!(db
        .receive(&deliver(&db, PEER, "delivery-1", &changed))
        .is_err());
    db.receive(&deliver(&db, PEER, "delivery-3", &changed))
        .unwrap();
    let conflict = db
        .process(db.pending().unwrap().unwrap(), 2000)
        .unwrap()
        .unwrap();
    assert_eq!(conflict["data"]["error"]["code"], "COMMAND_ID_REUSED");
    assert_ne!(conflict["dedupeKey"], first["dedupeKey"]);
}

#[test]
fn status_reports_pending_and_quarantined_and_requeues_original_ack() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = bound(dir.path());
    let epoch = db.identity.server_epoch.clone();
    let done = command(&db, "first", "done", "get_recovery", json!({}));
    db.receive(&deliver(&db, PEER, "d1", &done)).unwrap();
    let ack = db
        .process(db.pending().unwrap().unwrap(), 1)
        .unwrap()
        .unwrap();
    let pending = command(&db, "second", "pending", "get_recovery", json!({}));
    db.receive(&deliver(&db, PEER, "d2", &pending)).unwrap();
    let stale = db.pending().unwrap().unwrap();
    let result = |v: Value| v["data"]["result"].clone();
    let query = status_query(&mut db, PEER, "processing", &epoch, &pending, None);
    assert_eq!(
        result(query),
        json!({"state":"processing","ackEventId":null})
    );
    db.bind_peer(LOCAL_PAIR, "reinstalled").unwrap();
    assert!(db.process(stale, 20).unwrap().is_none());
    assert!(db.outbox(16).unwrap().is_empty());
    let query = status_query(
        &mut db,
        "reinstalled",
        "quarantined",
        &epoch,
        &pending,
        None,
    );
    assert_eq!(result(query)["state"], "quarantined");
    let query = status_query(
        &mut db,
        "reinstalled",
        "hash-check",
        &epoch,
        &pending,
        Some("0".repeat(64)),
    );
    assert_eq!(result(query)["state"], "hash_mismatch");
    let query = status_query(&mut db, "reinstalled", "found", &epoch, &done, None);
    assert_eq!(
        result(query),
        json!({"state":"found","ackEventId":ack["id"]})
    );
    assert!(db
        .outbox(16)
        .unwrap()
        .iter()
        .any(|row| row["payload"] == ack));
}

#[test]
fn explicit_recovery_rotates_both_ids_and_enforces_catalog_gate_after_restart() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = bound(dir.path());
    let old = db.identity.clone();
    let pending = command(&db, "pre-restore", "original", "pause", json!({}));
    db.receive(&deliver(&db, PEER, "old-delivery", &pending))
        .unwrap();
    db.enter_recovery().unwrap();
    assert_ne!(old.server_epoch, db.identity.server_epoch);
    assert_ne!(old.incarnation, db.identity.incarnation);
    let current = db.identity.clone();
    drop(db);

    let mut db = bound(dir.path());
    assert_eq!(db.identity, current);
    assert!(db.enter_recovery().is_err());
    let rejected = command(&db, "post-restore", "post-restore", "pause", json!({}));
    db.receive(&deliver(&db, PEER, "new-delivery", &rejected))
        .unwrap();
    let ack = db
        .process(db.pending().unwrap().unwrap(), 1)
        .unwrap()
        .unwrap();
    assert_eq!(ack["data"]["error"]["code"], "RECOVERY_REQUIRED");
    let query = status_query(&mut db, PEER, "lookup", &old.server_epoch, &pending, None);
    assert_eq!(query["data"]["result"]["state"], "quarantined");
}

#[test]
fn replacement_peer_cannot_rewrap_quarantined_intent_and_stale_epoch_query_does_not_block() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = bound(dir.path());
    let pending = command(&db, "old-pause", "old-pause", "pause", json!({}));
    let epoch = db.identity.server_epoch.clone();
    db.receive(&deliver(&db, PEER, "old-pause-frame", &pending))
        .unwrap();
    db.bind_peer(LOCAL_PAIR, "replacement").unwrap();
    db.receive(&deliver(&db, "replacement", "rewrapped", &pending))
        .unwrap();
    let ack = db
        .process(db.pending().unwrap().unwrap(), 20)
        .unwrap()
        .unwrap();
    assert_eq!(ack["data"]["error"]["code"], "RECOVERY_REQUIRED");
    let query = status_query(
        &mut db,
        "replacement",
        "quarantine-status",
        &epoch,
        &pending,
        None,
    );
    assert_eq!(query["data"]["result"]["state"], "quarantined");

    let mut late = command(&db, "late", "late", "pause", json!({}));
    late["serverEpoch"] = json!("older-epoch");
    late["data"]["serverEpoch"] = json!("older-epoch");
    db.receive(&deliver(&db, "replacement", "late-frame", &late))
        .unwrap();
    let ack = db
        .process(db.pending().unwrap().unwrap(), 20)
        .unwrap()
        .unwrap();
    assert_eq!(ack["data"]["error"]["code"], "SERVER_EPOCH_MISMATCH");
    let query = status_query(
        &mut db,
        "replacement",
        "older-status",
        "older-epoch",
        &late,
        None,
    );
    assert_eq!(query["data"]["result"]["state"], "not_found");
}

#[test]
fn claimed_job_and_peer_transition_compete_at_the_commit_boundary() {
    use std::sync::{Arc, Barrier, Mutex};
    for change_first in [true, false] {
        let dir = tempfile::tempdir().unwrap();
        let mut db = bound(dir.path());
        let pending = command(&db, "command", "pause", "pause", json!({}));
        let epoch = db.identity.server_epoch.clone();
        db.receive(&deliver(&db, PEER, "delivery", &pending))
            .unwrap();
        let job = db.pending().unwrap().unwrap();
        let db = Arc::new(Mutex::new(db));
        let order = Barrier::new(2);
        std::thread::scope(|scope| {
            let updater = scope.spawn(|| {
                if !change_first {
                    order.wait();
                }
                db.lock()
                    .unwrap()
                    .bind_peer(LOCAL_PAIR, "replacement")
                    .unwrap();
                if change_first {
                    order.wait();
                }
            });
            if change_first {
                order.wait();
            }
            let outcome = db.lock().unwrap().process(job, 1).unwrap();
            if !change_first {
                order.wait();
            }
            assert_eq!(outcome.is_none(), change_first);
            updater.join().unwrap();
        });
        let result = status_query(
            &mut db.lock().unwrap(),
            "replacement",
            "lookup",
            &epoch,
            &pending,
            None,
        );
        assert_eq!(
            result["data"]["result"]["state"],
            if change_first { "quarantined" } else { "found" }
        );
    }
}

#[test]
fn hard_process_stop_between_receipt_and_business_and_after_ack_commit() {
    use std::{
        process::{Command, Stdio},
        thread,
        time::{Duration, Instant},
    };
    let temp = tempfile::tempdir().unwrap();
    let spawn = |stage: &str| {
        Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "crash_child"])
            .env("TICKDESK_TEST_CRASH_DIR", temp.path())
            .env("TICKDESK_TEST_CRASH_BOUNDARY", stage)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap()
    };
    for stage in ["after-receipt", "after-commit"] {
        let mut child = spawn(stage);
        let until = Instant::now() + Duration::from_secs(15);
        let marker = temp.path().join(stage);
        while !marker.exists() && child.try_wait().unwrap().is_none() && Instant::now() < until {
            thread::sleep(Duration::from_millis(25));
        }
        let reached = marker.exists();
        let _ = child.kill();
        child.wait().unwrap();
        assert!(reached, "child did not reach {stage}");
    }
    let mut db = bound(temp.path());
    assert!(db.pending().unwrap().is_none());
    let frame = db.outbox(16).unwrap().remove(0);
    assert_eq!(frame["payload"]["data"]["cmd"], "pause");
    assert_eq!(frame["payload"]["data"]["result"]["controlVersion"], 1);

    let input = command(&db, "logical-retry", "pause-test", "pause", json!({}));
    db.receive(&deliver(&db, PEER, "replayed", &input)).unwrap();
    let ack = db
        .process(db.pending().unwrap().unwrap(), 250)
        .unwrap()
        .unwrap();
    assert_eq!(ack, frame["payload"]);
}

#[test]
fn crash_child() {
    let Some(directory) = std::env::var_os("TICKDESK_TEST_CRASH_DIR") else {
        return;
    };
    let stage = std::env::var("TICKDESK_TEST_CRASH_BOUNDARY").unwrap();
    let directory = std::path::PathBuf::from(directory);
    let mut db = bound(&directory);
    if stage == "after-receipt" {
        let input = command(&db, "logical-first", "pause-test", "pause", json!({}));
        db.receive(&deliver(&db, PEER, "received", &input)).unwrap();
    } else {
        let ack = db
            .process(db.pending().unwrap().unwrap(), 150)
            .unwrap()
            .unwrap();
        assert_eq!(ack["data"]["ok"], true);
    }
    std::fs::write(directory.join(stage), b"committed").unwrap();
    loop {
        std::thread::park();
    }
}
