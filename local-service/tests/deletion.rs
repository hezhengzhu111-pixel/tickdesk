#[allow(dead_code)]
mod support;

use base64::{engine::general_purpose::STANDARD, Engine};
use serde_json::{json, Value};
use support::{command, deliver, LOCAL_PAIR, PEER};
use tickdesk_local::{
    commands::now_ms,
    contract::{digest, sha256},
    storage::Storage,
};

fn send(db: &mut Storage, id: &str, name: &str, fields: Value) {
    let payload = command(db, id, id, name, fields);
    db.receive(&deliver(db, PEER, id, &payload)).unwrap();
}

fn execute(db: &mut Storage, id: &str) -> anyhow::Result<Value> {
    let job = db.job(id)?.unwrap();
    Ok(db.process(job, now_ms())?.unwrap()["data"].clone())
}

fn create_file(db: &mut Storage) -> Value {
    let b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGMQ0bABAADMAHm9FcOvAAAAAElFTkSuQmCC";
    let bytes = STANDARD.decode(b64).unwrap();
    let hash = sha256(&bytes);
    send(
        db,
        "begin",
        "begin_upload",
        json!({"name":"sample.png","mime":"image/png","sizeBytes":bytes.len(),"sha256":hash}),
    );
    let result = execute(db, "begin").unwrap();
    assert_eq!(result["ok"], true, "{result}");
    let upload = &result["result"]["upload"];
    let payload = json!({"transferId":upload["uploadId"],"fileId":upload["fileId"],
        "index":0,"totalChunks":1,"chunkHash":hash,"bytes":b64});
    db.receive(&json!({"v":2,"type":"deliver","category":"file_chunk","deliveryId":"chunk",
        "fromIncarnation":PEER,"toIncarnation":db.identity.incarnation,"payloadHash":digest(&payload).unwrap(),"payload":payload})).unwrap();
    send(
        db,
        "finish",
        "finalize_upload",
        json!({"uploadId":upload["uploadId"]}),
    );
    let result = execute(db, "finish").unwrap();
    assert_eq!(result["ok"], true, "{result}");
    result["result"]["file"].clone()
}

#[test]
fn deletion_resumes_after_unlink_when_business_commit_was_interrupted() {
    let directory = tempfile::tempdir().unwrap();
    let mut db = Storage::open(directory.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let file = create_file(&mut db);
    let path = directory
        .path()
        .join("files")
        .join(file["fileId"].as_str().unwrap());
    send(
        &mut db,
        "delete",
        "delete_file",
        json!({"fileId":file["fileId"],"baseVersion":file["version"],"confirm":true}),
    );
    let diagnostics =
        rusqlite::Connection::open(directory.path().join("tickdesk.sqlite3")).unwrap();
    diagnostics
        .execute_batch(
            "CREATE TRIGGER abort_delete_ack BEFORE INSERT ON command_results
        WHEN NEW.client_msg_id='delete' BEGIN SELECT RAISE(ABORT, 'injected failure'); END;",
        )
        .unwrap();
    assert!(execute(&mut db, "delete").is_err());
    assert!(!path.exists());
    let state: String = diagnostics
        .query_row("SELECT state FROM files", [], |row| row.get(0))
        .unwrap();
    assert_eq!(state, "deleting");
    diagnostics
        .execute_batch("DROP TRIGGER abort_delete_ack")
        .unwrap();
    drop(diagnostics);
    drop(db);
    let mut db = Storage::open(directory.path()).unwrap();
    let result = execute(&mut db, "delete").unwrap();
    assert_eq!(result["ok"], true, "{result}");
    let tombstone = &result["result"]["file"];
    assert_eq!(tombstone["fileId"], file["fileId"]);
    assert_eq!(tombstone["status"], "deleted");
    assert!(tombstone["deletedAt"].is_u64());
    assert!(!path.exists());
}

#[test]
fn deletion_with_a_stale_file_version_preserves_the_original() {
    let directory = tempfile::tempdir().unwrap();
    let mut db = Storage::open(directory.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let file = create_file(&mut db);
    send(
        &mut db,
        "stale-delete",
        "delete_file",
        json!({"fileId":file["fileId"],"baseVersion":0,"confirm":true}),
    );
    let result = execute(&mut db, "stale-delete").unwrap();
    assert_eq!(result["error"]["code"], "FILE_VERSION_CONFLICT");
    assert!(directory
        .path()
        .join("files")
        .join(file["fileId"].as_str().unwrap())
        .exists());
}

#[test]
fn deletion_waits_for_an_active_download_lease_to_release() {
    let directory = tempfile::tempdir().unwrap();
    let mut db = Storage::open(directory.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let file = create_file(&mut db);
    send(
        &mut db,
        "read",
        "read_file",
        json!({"fileId":file["fileId"]}),
    );
    let download = execute(&mut db, "read").unwrap();
    assert_eq!(download["ok"], true, "{download}");
    let args = json!({"fileId":file["fileId"],"baseVersion":file["version"],"confirm":true});
    send(&mut db, "busy", "delete_file", args.clone());
    assert_eq!(
        execute(&mut db, "busy").unwrap()["error"]["code"],
        "FILE_BUSY"
    );
    send(
        &mut db,
        "cancel",
        "cancel_transfer",
        json!({"transferId":download["result"]["downloadId"],"kind":"download"}),
    );
    assert_eq!(
        execute(&mut db, "cancel").unwrap()["result"]["status"],
        "cancelled"
    );
    send(&mut db, "delete", "delete_file", args);
    assert_eq!(
        execute(&mut db, "delete").unwrap()["result"]["file"]["status"],
        "deleted"
    );
}

#[test]
fn replacement_app_must_confirm_a_stranded_deletion_with_the_current_version() {
    let directory = tempfile::tempdir().unwrap();
    let mut db = Storage::open(directory.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let file = create_file(&mut db);
    let args = json!({"fileId":file["fileId"],"baseVersion":file["version"],"confirm":true});
    send(&mut db, "old-delete", "delete_file", args.clone());
    let diagnostics =
        rusqlite::Connection::open(directory.path().join("tickdesk.sqlite3")).unwrap();
    diagnostics
        .execute_batch(
            "CREATE TRIGGER abort_old_delete BEFORE INSERT ON command_results
        WHEN NEW.client_msg_id='old-delete' BEGIN SELECT RAISE(ABORT, 'injected failure'); END;",
        )
        .unwrap();
    assert!(execute(&mut db, "old-delete").is_err());
    let deleting_version: u64 = diagnostics
        .query_row("SELECT version FROM files", [], |r| r.get(0))
        .unwrap();

    let new_app = "replacement";
    db.bind_peer(LOCAL_PAIR, new_app).unwrap();
    assert!(db.job("old-delete").unwrap().is_none());
    let stale = command(&db, "stale", "stale", "delete_file", args);
    db.receive(&deliver(&db, new_app, "stale", &stale)).unwrap();
    assert_eq!(
        execute(&mut db, "stale").unwrap()["error"]["code"],
        "FILE_VERSION_CONFLICT"
    );
    let new = command(
        &db,
        "confirmed",
        "confirmed",
        "delete_file",
        json!({"fileId":file["fileId"],"baseVersion":deleting_version,"confirm":true}),
    );
    db.receive(&deliver(&db, new_app, "confirmed", &new))
        .unwrap();
    assert_eq!(
        execute(&mut db, "confirmed").unwrap()["result"]["file"]["status"],
        "deleted"
    );
}
