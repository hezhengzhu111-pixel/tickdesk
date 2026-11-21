mod support;
use serde_json::json;
use support::{command, deliver, LOCAL_PAIR, PEER};
use tickdesk_local::storage::Storage;

fn run(db: &mut Storage, key: &str, name: &str, fields: serde_json::Value) -> serde_json::Value {
    let input = command(db, key, key, name, fields);
    db.receive(&deliver(db, PEER, key, &input)).unwrap();
    db.process(db.job(key).unwrap().unwrap(), 1_794_792_300_000)
        .unwrap()
        .unwrap()
}

#[test]
fn storage_status_measures_the_authoritative_directories_and_last_successful_backup() {
    let temp = tempfile::tempdir().unwrap();
    let mut db = Storage::open(&temp.path().join("ledger")).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let first = run(&mut db, "storage-empty", "get_storage_status", json!({}));
    assert_eq!(first["data"]["ok"], true, "{first}");
    let status = &first["data"]["result"];
    assert_eq!(status["status"], "available");
    assert_eq!(status["originalsBytes"], 0);
    assert_eq!(status["quarantineBytes"], 0);
    assert_eq!(status["stagingBytes"], 0);
    assert!(status["availableBytes"].as_u64().unwrap() > 0);
    assert!(status["lastSuccessfulBackupAt"].is_null());

    let backup = temp.path().join("ledger/backups/2026-11-21");
    std::fs::create_dir_all(&backup).unwrap();
    std::fs::write(
        backup.join("manifest.json"),
        json!({"completedAt":1_794_792_000_000_u64}).to_string(),
    )
    .unwrap();
    let after = run(&mut db, "storage-backup", "get_storage_status", json!({}));
    assert_eq!(
        after["data"]["result"]["lastSuccessfulBackupAt"],
        1_794_792_000_000_u64
    );
}

#[test]
fn published_backup_is_self_contained_during_read_only_audit() {
    let temp = tempfile::tempdir().unwrap();
    let source = temp.path().join("ledger");
    let ledger = Storage::open(&source).unwrap();
    let backup = temp.path().join("backup");
    ledger.backup_to(&backup).unwrap();
    let inspect = |directory: &std::path::Path| {
        rusqlite::Connection::open_with_flags(
            directory.join("tickdesk.sqlite3"),
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
        )
        .unwrap()
    };
    let mode = |db: &rusqlite::Connection| {
        db.query_row("PRAGMA journal_mode", [], |row| row.get::<_, String>(0))
            .unwrap()
    };
    assert_eq!(mode(&inspect(&source)), "wal");
    let archived = inspect(&backup);
    assert_eq!(mode(&archived), "delete");
    assert_eq!(
        archived
            .query_row("PRAGMA quick_check", [], |row| row.get::<_, String>(0))
            .unwrap(),
        "ok"
    );
    assert!(!backup.join("tickdesk.sqlite3-wal").exists());
    assert!(!backup.join("tickdesk.sqlite3-shm").exists());
    Storage::restore_from(&backup, &temp.path().join("restored")).unwrap();
}

#[test]
fn restore_verifies_bytes_rotates_identity_and_isolates_old_inbox() {
    let temp = tempfile::tempdir().unwrap();
    let mut db = Storage::open(&temp.path().join("original")).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let old = db.identity.clone();
    let command = command(&db, "old", "old", "pause", json!({}));
    db.receive(&deliver(&db, PEER, "old", &command)).unwrap();
    let backup = temp.path().join("backup");
    db.backup_to(&backup).unwrap();
    let destination = temp.path().join("restored");
    Storage::restore_from(&backup, &destination).unwrap();
    let mut restored = Storage::open(&destination).unwrap();
    assert_ne!(restored.identity.incarnation, old.incarnation);
    assert_ne!(restored.identity.server_epoch, old.server_epoch);
    assert!(restored.pending().unwrap().is_none());
    assert!(restored.recovery_active().unwrap());
    let query = support::status_query(
        &mut restored,
        PEER,
        "lookup",
        &old.server_epoch,
        &command,
        None,
    );
    assert_eq!(query["data"]["result"]["state"], "quarantined");
    assert!(Storage::restore_from(&backup, &destination).is_err());
    drop(restored);
    std::fs::write(backup.join("tickdesk.sqlite3"), b"corrupt").unwrap();
    let failed = temp.path().join("corrupt-restore");
    assert!(Storage::restore_from(&backup, &failed).is_err());
    assert!(!failed.exists());
}

#[test]
fn restore_quarantines_original_and_requires_an_explicit_review() {
    let temp = tempfile::tempdir().unwrap();
    let dir = temp.path().join("source");
    let db = Storage::open(&dir).unwrap();
    let id = uuid::Uuid::new_v4().to_string();
    let original = b"original fixture bytes";
    let hash = tickdesk_local::contract::sha256(original);
    std::fs::write(dir.join("files").join(&id), original).unwrap();
    let sql = rusqlite::Connection::open(dir.join("tickdesk.sqlite3")).unwrap();
    sql.execute(
        "INSERT INTO files VALUES (?1,?2,1,'original.png','image/png',?3,?4,'available',1,NULL)",
        rusqlite::params![id, LOCAL_PAIR, original.len(), hash],
    )
    .unwrap();
    drop(sql);
    let backup = temp.path().join("backup");
    db.backup_to(&backup).unwrap();

    let restored = temp.path().join("isolated");
    Storage::restore_from(&backup, &restored).unwrap();
    assert!(!restored.join("files").join(&id).exists());
    assert_eq!(
        std::fs::read(restored.join("restore-quarantine").join(&id)).unwrap(),
        original
    );
    let mut db = Storage::open(&restored).unwrap();
    assert!(db.resolve_restored_original(&id, "missing").is_err());
    db.resolve_restored_original(&id, "keep").unwrap();
    assert_eq!(
        std::fs::read(restored.join("files").join(&id)).unwrap(),
        original
    );
    assert!(!restored.join("restore-quarantine").join(&id).exists());
}
