mod support;

use base64::{engine::general_purpose::STANDARD, Engine};
use image::{codecs::png::PngEncoder, ExtendedColorType, ImageEncoder};
use serde_json::{json, Value};
use support::{command, deliver, LOCAL_PAIR, PEER};
use tickdesk_local::{commands::now_ms, contract::sha256, storage::Storage};

fn issue(db: &mut Storage, id: &str, name: &str, fields: Value, now: u64) -> Value {
    let body = command(db, id, id, name, fields);
    let delivery_id = uuid::Uuid::new_v4().to_string();
    db.receive(&deliver(db, PEER, &delivery_id, &body)).unwrap();
    let job = db.job(&delivery_id).unwrap().unwrap();
    db.process(job, now).unwrap().unwrap()["data"].clone()
}

fn setup() -> (tempfile::TempDir, Storage, String, Vec<u8>) {
    let directory = tempfile::tempdir().unwrap();
    let mut db = Storage::open(directory.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let file_id = uuid::Uuid::new_v4().to_string();
    // Model an already archived original large enough to span more than one send window.
    let mut state = 0x1234_5678_u32;
    let pixels: Vec<_> = (0..256 * 256 * 3)
        .map(|_| {
            state ^= state << 13;
            state ^= state >> 17;
            state ^= state << 5;
            state as u8
        })
        .collect();
    let mut bytes = Vec::new();
    PngEncoder::new(&mut bytes)
        .write_image(&pixels, 256, 256, ExtendedColorType::Rgb8)
        .unwrap();
    std::fs::write(directory.path().join("files").join(&file_id), &bytes).unwrap();
    let conn = rusqlite::Connection::open(directory.path().join("tickdesk.sqlite3")).unwrap();
    conn.execute(
        "INSERT INTO files(file_id,pair_id,version,name,mime,size_bytes,sha256,state,created_at)
        VALUES (?1,?2,1,'test.png','image/png',?3,?4,'available',1)",
        rusqlite::params![file_id, LOCAL_PAIR, bytes.len(), sha256(&bytes)],
    )
    .unwrap();
    (directory, db, file_id, bytes)
}

fn begin(db: &mut Storage, file_id: &str, now: u64) -> Value {
    let ack = issue(db, "read", "read_file", json!({"fileId":file_id}), now);
    assert_eq!(ack["ok"], true, "{ack}");
    ack["result"].clone()
}

fn receipt(frame: &Value) -> Value {
    json!({
        "v": 2, "type": "receipt", "deliveryId": frame["deliveryId"],
        "fromIncarnation": frame["fromIncarnation"], "toIncarnation": frame["toIncarnation"],
        "payloadHash": frame["payloadHash"]
    })
}

#[test]
fn reading_an_available_original_returns_a_persistent_download_manifest() {
    let (directory, mut db, file_id, bytes) = setup();
    let now = now_ms();
    let manifest = begin(&mut db, &file_id, now);
    assert_eq!(manifest["file"]["fileId"], file_id);
    assert_eq!(manifest["file"]["sha256"], sha256(&bytes));
    assert_eq!(manifest["chunkSize"], 32768);
    assert_eq!(manifest["totalChunks"], 7);
    assert_eq!(manifest["expiresAt"], now + 86_400_000);
    drop(db);

    let mut db = Storage::open(directory.path()).unwrap();
    assert_eq!(begin(&mut db, &file_id, now + 1000), manifest);
    let id = &manifest["downloadId"];
    let cancelled = issue(
        &mut db,
        "cancel",
        "cancel_transfer",
        json!({"transferId":id,"kind":"download"}),
        now + 2000,
    );
    assert_eq!(
        cancelled["result"],
        json!({"transferId":id,"status":"cancelled"})
    );
    assert_eq!(
        std::fs::read(directory.path().join("files").join(&file_id)).unwrap(),
        bytes
    );
}

#[test]
fn restart_preserves_the_window_and_the_last_matching_receipt_releases_the_lease() {
    let (directory, mut db, file_id, bytes) = setup();
    let now = now_ms();
    let manifest = begin(&mut db, &file_id, now);
    db.pump_downloads(now).unwrap();
    let first = db.file_outbox().unwrap();
    assert_eq!(first.len(), 4);
    let mut wrong = receipt(&first[0]);
    wrong["payloadHash"] = json!("0".repeat(64));
    assert!(!db.accept_receipt(&wrong).unwrap());
    drop(db);
    let mut db = Storage::open(directory.path()).unwrap();
    db.pump_downloads(now + 1).unwrap();
    assert_eq!(db.file_outbox().unwrap(), first);

    let delete = json!({"fileId":file_id,"baseVersion":1,"confirm":true});
    let busy = issue(
        &mut db,
        "delete-busy",
        "delete_file",
        delete.clone(),
        now + 2,
    );
    assert_eq!(busy["error"]["code"], "FILE_BUSY");
    let mut parts = Vec::new();
    loop {
        let frames = db.file_outbox().unwrap();
        if frames.is_empty() {
            break;
        }
        for frame in frames {
            let payload = &frame["payload"];
            assert_eq!(frame["category"], "file_chunk");
            assert_eq!(payload["transferId"], manifest["downloadId"]);
            assert_eq!(payload["totalChunks"], manifest["totalChunks"]);
            assert_eq!(payload["index"], parts.len());
            let part = STANDARD.decode(payload["bytes"].as_str().unwrap()).unwrap();
            assert_eq!(payload["chunkHash"], sha256(&part));
            parts.push(part);
            let confirmed = receipt(&frame);
            assert!(db.accept_receipt(&confirmed).unwrap());
            assert!(db.accept_receipt(&confirmed).unwrap());
        }
        db.pump_downloads(now + 3).unwrap();
    }
    assert_eq!(parts.concat(), bytes);
    let result = issue(
        &mut db,
        "complete",
        "cancel_transfer",
        json!({"kind":"download","transferId":manifest["downloadId"]}),
        now + 4,
    );
    assert_eq!(result["result"]["status"], "already_complete");
    let deleted = issue(&mut db, "delete-after", "delete_file", delete, now + 5);
    assert_eq!(deleted["ok"], true, "{deleted}");
}

#[test]
fn cancelled_and_expired_downloads_stop_retries_and_late_receipts() {
    let (directory, mut db, file_id, bytes) = setup();
    let now = now_ms();
    let manifest = begin(&mut db, &file_id, now);
    db.pump_downloads(now).unwrap();
    let before_cancel = db.file_outbox().unwrap();
    issue(
        &mut db,
        "cancel",
        "cancel_transfer",
        json!({"kind":"download","transferId":manifest["downloadId"]}),
        now + 1,
    );
    assert!(db.file_outbox().unwrap().is_empty());
    assert!(!db.accept_receipt(&receipt(&before_cancel[0])).unwrap());

    let next = issue(
        &mut db,
        "read-again",
        "read_file",
        json!({"fileId":file_id}),
        now + 2,
    );
    assert_eq!(next["ok"], true, "{next}");
    db.pump_downloads(now + 3).unwrap();
    let before_expiry = db.file_outbox().unwrap();
    assert_eq!(before_expiry.len(), 4);
    db.pump_downloads(now + 2 + 86_400_000).unwrap();
    assert!(db.file_outbox().unwrap().is_empty());
    assert!(!db.accept_receipt(&receipt(&before_expiry[0])).unwrap());
    assert_eq!(
        std::fs::read(directory.path().join("files").join(&file_id)).unwrap(),
        bytes
    );
}

#[test]
fn replacing_the_app_cancels_the_old_download_without_modifying_the_original() {
    let (directory, mut db, file_id, bytes) = setup();
    let now = now_ms();
    begin(&mut db, &file_id, now);
    db.pump_downloads(now).unwrap();
    let frame = db.file_outbox().unwrap().remove(0);
    db.bind_peer(LOCAL_PAIR, "replacement-app").unwrap();
    db.pump_downloads(now + 1).unwrap();
    assert!(db.file_outbox().unwrap().is_empty());
    assert!(!db.accept_receipt(&receipt(&frame)).unwrap());
    assert_eq!(
        std::fs::read(directory.path().join("files").join(&file_id)).unwrap(),
        bytes
    );
}

#[test]
fn permanent_rejection_of_one_chunk_releases_the_transfer_and_stops_the_rest() {
    let (_directory, mut db, file_id, _) = setup();
    let now = now_ms();
    begin(&mut db, &file_id, now);
    db.pump_downloads(now).unwrap();
    let sent = db.file_outbox().unwrap();
    assert_eq!(sent.len(), 4);
    db.quarantine_delivery(sent[0]["deliveryId"].as_str().unwrap())
        .unwrap();
    db.pump_downloads(now + 1).unwrap();
    assert!(db.file_outbox().unwrap().is_empty());
    assert!(!db.accept_receipt(&receipt(&sent[1])).unwrap());
    let deleted = issue(
        &mut db,
        "delete",
        "delete_file",
        json!({"fileId":file_id,"baseVersion":1,"confirm":true}),
        now + 2,
    );
    assert_eq!(deleted["ok"], true, "{deleted}");
}

#[test]
fn late_transport_error_cannot_cancel_a_confirmed_chunk() {
    let (_directory, mut db, file_id, _) = setup();
    let now = now_ms();
    begin(&mut db, &file_id, now);
    db.pump_downloads(now).unwrap();
    let first = db.file_outbox().unwrap().remove(0);
    assert!(db.accept_receipt(&receipt(&first)).unwrap());
    db.quarantine_delivery(first["deliveryId"].as_str().unwrap())
        .unwrap();
    db.pump_downloads(now + 1).unwrap();
    assert_eq!(db.file_outbox().unwrap().len(), 4);
}
