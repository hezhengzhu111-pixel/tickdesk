#[allow(dead_code)]
mod support;

use base64::{engine::general_purpose::STANDARD, Engine};
use serde_json::{json, Value};
use support::{command, deliver, LOCAL_PAIR, PEER};
use tickdesk_local::{
    contract::{digest, sha256},
    storage::Storage,
};

fn issue(db: &mut Storage, id: &str, cmd: &str, fields: Value) -> Value {
    let body = command(db, id, id, cmd, fields);
    db.receive(&deliver(db, PEER, id, &body)).unwrap();
    let job = db.job(id).unwrap().unwrap();
    db.process(job, tickdesk_local::commands::now_ms())
        .unwrap()
        .unwrap()["data"]
        .clone()
}

fn begin(db: &mut Storage, hash: String, bytes: &[u8]) -> Value {
    let result = issue(
        db,
        "begin",
        "begin_upload",
        json!({"name":"../../original.png","mime":"image/png","sizeBytes":bytes.len(),"sha256":hash}),
    );
    assert_eq!(result["ok"], true, "{result}");
    result["result"]["upload"].clone()
}

fn chunk(db: &Storage, upload: &Value, id: &str, bytes: &[u8]) -> Value {
    let payload = json!({
        "transferId":upload["uploadId"],"fileId":upload["fileId"],"index":0,
        "totalChunks":upload["totalChunks"],"chunkHash":sha256(bytes),"bytes":STANDARD.encode(bytes),
    });
    json!({"v":2,"type":"deliver","category":"file_chunk","deliveryId":id,
        "fromIncarnation":PEER,"toIncarnation":db.identity.incarnation,"payloadHash":digest(&payload).unwrap(),"payload":payload})
}

fn png() -> Vec<u8> {
    STANDARD
        .decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGMQ0bABAADMAHm9FcOvAAAAAElFTkSuQmCC")
        .unwrap()
}

#[test]
fn uploaded_chunks_survive_restart_and_finalize_only_once() {
    let directory = tempfile::tempdir().unwrap();
    let mut db = Storage::open(directory.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let bytes = png();
    let upload = begin(&mut db, sha256(&bytes), &bytes);
    let args = json!({"uploadId":upload["uploadId"]});
    let incomplete = issue(&mut db, "missing", "finalize_upload", args.clone());
    assert_eq!(incomplete["error"]["code"], "UPLOAD_INCOMPLETE");

    let frame = chunk(&db, &upload, "first-chunk", &bytes);
    let receipt = db.receive(&frame).unwrap();
    assert_eq!(receipt["type"], "receipt");
    drop(db);

    let mut db = Storage::open(directory.path()).unwrap();
    assert_eq!(db.receive(&frame).unwrap(), receipt);
    let duplicate = chunk(&db, &upload, "duplicate", &bytes);
    assert_eq!(db.receive(&duplicate).unwrap()["type"], "receipt");
    let conflicting = chunk(&db, &upload, "different", &vec![0; bytes.len()]);
    assert_eq!(
        db.receive(&conflicting).unwrap_err().to_string(),
        "CHUNK_CONFLICT"
    );
    let status = issue(&mut db, "status", "get_upload", args.clone());
    assert_eq!(status["result"]["upload"]["receivedChunks"], json!([0]));
    let first = issue(&mut db, "finalize1", "finalize_upload", args.clone());
    assert_eq!(first["ok"], true, "{first}");
    let second = issue(&mut db, "finalize2", "finalize_upload", args);
    assert_eq!(first["result"], second["result"]);
    assert_eq!(first["result"]["file"]["fileId"], upload["fileId"]);
    let files = std::fs::read_dir(directory.path().join("files"))
        .unwrap()
        .collect::<Result<Vec<_>, _>>()
        .unwrap();
    assert_eq!(files.len(), 1);
    assert_eq!(std::fs::read(files[0].path()).unwrap(), bytes);
    let listed = issue(&mut db, "files", "list_files", json!({}));
    assert_eq!(listed["result"]["items"], json!([first["result"]["file"]]));
}

#[test]
fn refuses_to_archive_a_complete_upload_with_a_mismatched_hash() {
    let directory = tempfile::tempdir().unwrap();
    let mut db = Storage::open(directory.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let bytes = png();
    let upload = begin(&mut db, "0".repeat(64), &bytes);
    let mut frame = chunk(&db, &upload, "invalid", &bytes);
    frame["payload"]["chunkHash"] = json!("0".repeat(64));
    frame["payloadHash"] = json!(digest(&frame["payload"]).unwrap());
    assert!(db.receive(&frame).is_err());
    let frame = chunk(&db, &upload, "valid", &bytes);
    db.receive(&frame).unwrap();
    let ack = issue(
        &mut db,
        "finalize",
        "finalize_upload",
        json!({"uploadId":upload["uploadId"]}),
    );
    assert_eq!(ack["error"]["code"], "FILE_HASH_MISMATCH");
    assert_eq!(
        std::fs::read_dir(directory.path().join("files"))
            .unwrap()
            .count(),
        0
    );
}

#[test]
fn rejects_undecodable_bytes_even_when_the_hash_matches() {
    let directory = tempfile::tempdir().unwrap();
    let mut db = Storage::open(directory.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let bytes = &png()[..41];
    let upload = begin(&mut db, sha256(bytes), bytes);
    let frame = chunk(&db, &upload, "chunk", bytes);
    db.receive(&frame).unwrap();
    let ack = issue(
        &mut db,
        "finalize",
        "finalize_upload",
        json!({"uploadId":upload["uploadId"]}),
    );
    assert_eq!(ack["error"]["code"], "FILE_TYPE_UNSUPPORTED");
    assert_eq!(
        std::fs::read_dir(directory.path().join("files"))
            .unwrap()
            .count(),
        0
    );
}

#[test]
fn cancelled_upload_cannot_be_recreated_by_a_late_chunk() {
    let directory = tempfile::tempdir().unwrap();
    let mut db = Storage::open(directory.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let bytes = png();
    let upload = begin(&mut db, sha256(&bytes), &bytes);
    let cancelled = issue(
        &mut db,
        "cancel",
        "cancel_transfer",
        json!({"transferId":upload["uploadId"],"kind":"upload"}),
    );
    assert_eq!(cancelled["result"]["status"], "cancelled");
    let frame = chunk(&db, &upload, "late", &bytes);
    drop(db);
    let mut db = Storage::open(directory.path()).unwrap();
    assert_eq!(
        db.receive(&frame).unwrap_err().to_string(),
        "UPLOAD_NOT_FOUND"
    );
    let status = issue(
        &mut db,
        "status",
        "get_upload",
        json!({"uploadId":upload["uploadId"]}),
    );
    assert_eq!(status["result"]["upload"]["receivedChunks"], json!([]));
    assert_eq!(status["result"]["upload"]["status"], "cancelled");
}

#[test]
fn backup_recovery_fences_receipts_for_the_old_upload_epoch() {
    let directory = tempfile::tempdir().unwrap();
    let mut db = Storage::open(directory.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let bytes = png();
    let upload = begin(&mut db, sha256(&bytes), &bytes);
    let mut frame = chunk(&db, &upload, "chunk", &bytes);
    assert_eq!(db.receive(&frame).unwrap()["type"], "receipt");
    db.enter_recovery().unwrap();
    frame["toIncarnation"] = json!(db.identity.incarnation);
    assert_eq!(
        db.receive(&frame).unwrap_err().to_string(),
        "UPLOAD_NOT_FOUND"
    );
}

#[test]
fn archive_survives_an_interruption_before_the_finalization_ack_commits() {
    let directory = tempfile::tempdir().unwrap();
    let mut db = Storage::open(directory.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let bytes = png();
    let upload = begin(&mut db, sha256(&bytes), &bytes);
    let frame = chunk(&db, &upload, "chunk", &bytes);
    db.receive(&frame).unwrap();
    let body = command(
        &db,
        "finish",
        "finish",
        "finalize_upload",
        json!({"uploadId":upload["uploadId"]}),
    );
    db.receive(&deliver(&db, PEER, "finish", &body)).unwrap();
    let diagnostic = rusqlite::Connection::open(directory.path().join("tickdesk.sqlite3")).unwrap();
    diagnostic
        .execute_batch(
            "CREATE TRIGGER abort_finalize_ack BEFORE INSERT ON command_results
        WHEN NEW.client_msg_id='finish' BEGIN SELECT RAISE(ABORT,'simulated failure'); END;",
        )
        .unwrap();
    let job = db.job("finish").unwrap().unwrap();
    assert!(db.process(job, tickdesk_local::commands::now_ms()).is_err());
    let listed = issue(&mut db, "files", "list_files", json!({}));
    assert_eq!(listed["result"]["items"], json!([]));
    let path = directory
        .path()
        .join("files")
        .join(upload["fileId"].as_str().unwrap());
    assert_eq!(std::fs::read(&path).unwrap(), bytes);
    diagnostic
        .execute_batch("DROP TRIGGER abort_finalize_ack")
        .unwrap();
    drop(diagnostic);
    drop(db);
    let mut db = Storage::open(directory.path()).unwrap();
    let job = db.job("finish").unwrap().unwrap();
    let ack = db
        .process(job, tickdesk_local::commands::now_ms())
        .unwrap()
        .unwrap();
    assert_eq!(ack["data"]["ok"], true, "{ack}");
    assert_eq!(ack["data"]["result"]["file"]["fileId"], upload["fileId"]);
    assert_eq!(std::fs::read(&path).unwrap(), bytes);
}

#[test]
fn abandoned_uncommitted_archives_are_cleaned_without_deleting_completed_originals() {
    for transition in ["cancel", "expire", "replace", "recover"] {
        let directory = tempfile::tempdir().unwrap();
        let mut db = Storage::open(directory.path()).unwrap();
        db.bind_peer(LOCAL_PAIR, PEER).unwrap();
        let bytes = png();
        let upload = begin(&mut db, sha256(&bytes), &bytes);
        db.receive(&chunk(&db, &upload, "chunk", &bytes)).unwrap();
        let body = command(
            &db,
            "finish",
            "finish",
            "finalize_upload",
            json!({"uploadId":upload["uploadId"]}),
        );
        db.receive(&deliver(&db, PEER, "finish", &body)).unwrap();
        let diagnostic =
            rusqlite::Connection::open(directory.path().join("tickdesk.sqlite3")).unwrap();
        diagnostic.execute_batch("CREATE TRIGGER fail_finalize BEFORE INSERT ON command_results WHEN NEW.client_msg_id='finish' BEGIN SELECT RAISE(ABORT,'fault'); END;").unwrap();
        assert!(db
            .process(
                db.job("finish").unwrap().unwrap(),
                tickdesk_local::commands::now_ms()
            )
            .is_err());
        diagnostic
            .execute_batch("DROP TRIGGER fail_finalize")
            .unwrap();
        let path = directory
            .path()
            .join("files")
            .join(upload["fileId"].as_str().unwrap());
        assert!(path.is_file());
        match transition {
            "cancel" => {
                let ack = issue(
                    &mut db,
                    "cancel",
                    "cancel_transfer",
                    json!({"transferId":upload["uploadId"],"kind":"upload"}),
                );
                assert_eq!(ack["ok"], true);
            }
            "expire" => {
                diagnostic
                    .execute("UPDATE file_uploads SET expires_at=0", [])
                    .unwrap();
            }
            "replace" => db.bind_peer(LOCAL_PAIR, "replacement-app").unwrap(),
            "recover" => db.enter_recovery().unwrap(),
            _ => unreachable!(),
        }
        drop(diagnostic);
        drop(db);
        let _db = Storage::open(directory.path()).unwrap();
        assert!(!path.exists(), "abandoned original survived {transition}");
    }
}
