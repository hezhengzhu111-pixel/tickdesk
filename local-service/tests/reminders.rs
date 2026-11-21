mod support;

use rusqlite::{params, Connection};
use serde_json::{json, Value};
use tickdesk_local::storage::Storage;

const START: u64 = 1_794_792_300_000;

fn setup() -> (tempfile::TempDir, Storage, Connection) {
    let tmp = tempfile::tempdir().unwrap();
    let mut db = Storage::open(tmp.path()).unwrap();
    db.bind_peer(support::LOCAL_PAIR, support::PEER).unwrap();
    let sql = Connection::open(tmp.path().join("tickdesk.sqlite3")).unwrap();
    (tmp, db, sql)
}

fn seed(sql: &Connection, key: &str, level: &str) {
    let fixtures: Value =
        serde_json::from_str(include_str!("../contracts/v2/fixtures.json")).unwrap();
    let mut task = fixtures["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|f| f["name"] == "task")
        .unwrap()["payload"]["data"]
        .clone();
    task["taskId"] = json!(key);
    task["level"] = json!(level);
    task["status"] = json!("pending");
    task["createdAt"] = json!(START);
    task["validUntil"] = json!(START + 1_800_000);
    task["version"] = json!(1);
    task["actionVersion"] = json!(1);
    task["remindedCount"] = json!(1);
    task["lastRemindedAt"] = json!(START);
    task["snoozeUntil"] = Value::Null;
    task["currentReminder"] = Value::Null;
    task["nextReminderAt"] = if level == "info" {
        Value::Null
    } else {
        json!(START + 300_000)
    };
    sql.execute(
        "INSERT INTO objects VALUES ('tasks',?1,?2)",
        params![key, task.to_string()],
    )
    .unwrap();
}

fn task(sql: &Connection, key: &str) -> Value {
    let body: String = sql
        .query_row(
            "SELECT body FROM objects WHERE collection='tasks' AND object_id=?1",
            [key],
            |r| r.get(0),
        )
        .unwrap();
    serde_json::from_str(&body).unwrap()
}

fn run(db: &mut Storage, key: &str, name: &str, fields: Value, now: u64) -> Value {
    let command = support::command(db, key, key, name, fields);
    db.receive(&support::deliver(db, support::PEER, key, &command))
        .unwrap();
    db.process(db.job(key).unwrap().unwrap(), now)
        .unwrap()
        .unwrap()
}

#[test]
fn snooze_without_minutes_defaults_to_five_with_atomic_ack_and_original_request_hash() {
    let (_tmp, mut db, sql) = setup();
    seed(&sql, "task", "warn");
    let before = task(&sql, "task");
    let now = START + 100_000;
    let input = support::command(
        &db,
        "default-snooze",
        "default-snooze",
        "snooze",
        json!({"taskId":"task","baseActionVersion":1}),
    );
    let request_hash = tickdesk_local::contract::digest(&input["data"]).unwrap();
    db.receive(&support::deliver(
        &db,
        support::PEER,
        "default-snooze",
        &input,
    ))
    .unwrap();
    let job = db.job("default-snooze").unwrap().unwrap();
    sql.execute_batch("CREATE TRIGGER reject_snooze_outbox BEFORE INSERT ON transport_outbox BEGIN SELECT RAISE(ABORT,'injected'); END;").unwrap();
    assert!(db.process(job.clone(), now).is_err());
    assert_eq!(task(&sql, "task"), before);
    assert_eq!(
        sql.query_row("SELECT COUNT(*) FROM task_actions", [], |r| r
            .get::<_, u64>(0))
            .unwrap(),
        0
    );
    assert_eq!(
        sql.query_row("SELECT COUNT(*) FROM command_results", [], |r| r
            .get::<_, u64>(0))
            .unwrap(),
        0
    );
    sql.execute_batch("DROP TRIGGER reject_snooze_outbox")
        .unwrap();
    let ack = db.process(job, now).unwrap().unwrap();
    assert_eq!(ack["data"]["ok"], true);
    assert_eq!(ack["data"]["requestHash"], request_hash);
    let updated = task(&sql, "task");
    assert_eq!(updated["snoozeUntil"], now + 300_000);
    assert_eq!(updated["nextReminderAt"], now + 300_000);
    assert_eq!(updated["remindedCount"], 1);
    assert_eq!(updated["version"], 2);
    assert_eq!(updated["actionVersion"], 2);
    assert_eq!(ack["data"]["result"]["task"], updated);
    let persisted: String = sql
        .query_row(
            "SELECT body FROM transport_inbox WHERE delivery_id='default-snooze'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    let persisted: Value = serde_json::from_str(&persisted).unwrap();
    assert!(persisted["data"].get("minutes").is_none());
    let mut replay = input.clone();
    replay["id"] = json!("replay-default-snooze");
    db.receive(&support::deliver(
        &db,
        support::PEER,
        "replay-default-snooze",
        &replay,
    ))
    .unwrap();
    let replayed = db
        .process(db.job("replay-default-snooze").unwrap().unwrap(), now + 1)
        .unwrap()
        .unwrap();
    assert_eq!(replayed, ack);
    assert_eq!(
        sql.query_row("SELECT COUNT(*) FROM task_actions", [], |r| r
            .get::<_, u64>(0))
            .unwrap(),
        1
    );
    assert_eq!(
        run(
            &mut db,
            "status-after-snooze",
            "get_status",
            json!({}),
            now + 2
        )["data"]["ok"],
        true
    );
    let ignored = run(
        &mut db,
        "ignore-after-snooze",
        "ignore",
        json!({"taskId":"task","baseActionVersion":2}),
        now + 3,
    );
    assert_eq!(ignored["data"]["ok"], true);
    assert_eq!(ignored["data"]["result"]["task"]["actionVersion"], 3);
}

#[test]
fn explicit_snooze_minutes_keep_the_requested_one_to_five_minute_delay() {
    let (_tmp, mut db, sql) = setup();
    for minutes in 1..=5 {
        let key = format!("task-{minutes}");
        seed(&sql, &key, "warn");
        let result = run(
            &mut db,
            &format!("snooze-{minutes}"),
            "snooze",
            json!({"taskId":key,"baseActionVersion":1,"minutes":minutes}),
            START + 100_000,
        );
        assert_eq!(result["data"]["ok"], true);
        assert_eq!(
            result["data"]["result"]["task"]["snoozeUntil"],
            START + 100_000 + minutes * 60_000
        );
    }
}

#[test]
fn levels_schedule_once_and_restart_preserves_dedupe_without_touching_action_version() {
    let (tmp, mut db, sql) = setup();
    for level in ["critical", "warn", "info"] {
        seed(&sql, level, level);
    }
    assert_eq!(db.advance_reminders(START + 299_999).unwrap(), 0);
    assert_eq!(db.advance_reminders(START + 300_000).unwrap(), 2);
    let first = task(&sql, "critical");
    assert_eq!(first["version"], 2);
    assert_eq!(first["actionVersion"], 1);
    assert_eq!(first["remindedCount"], 2);
    assert_eq!(first["status"], "reminded");
    assert_eq!(first["currentReminder"]["scheduledAt"], START + 300_000);
    assert_eq!(first["currentReminder"]["displayUntil"], START + 360_000);
    assert_eq!(task(&sql, "warn")["nextReminderAt"], Value::Null);
    assert_eq!(task(&sql, "info")["version"], 1);
    let frames = db.outbox(20).unwrap();
    assert_eq!(frames.len(), 2);
    for frame in &frames {
        assert_eq!(frame["payload"]["kind"], "reminder");
        tickdesk_local::contract::validate_business(&frame["payload"]).unwrap();
    }
    drop(db);
    let mut db = Storage::open(tmp.path()).unwrap();
    assert_eq!(db.advance_reminders(START + 300_001).unwrap(), 0);
    assert_eq!(task(&sql, "critical"), first);
    assert_eq!(db.advance_reminders(START + 480_000).unwrap(), 1);
    assert_eq!(task(&sql, "critical")["remindedCount"], 3);
    let actions: u64 = sql
        .query_row("SELECT COUNT(*) FROM task_actions", [], |r| r.get(0))
        .unwrap();
    assert_eq!(actions, 3);
}

#[test]
fn missed_windows_do_not_catch_up_and_expiry_wins_at_the_boundary() {
    let (_tmp, mut db, sql) = setup();
    seed(&sql, "critical", "critical");
    seed(&sql, "warn", "warn");
    assert_eq!(db.advance_reminders(START + 900_000).unwrap(), 0);
    assert_eq!(task(&sql, "critical")["nextReminderAt"], START + 1_020_000);
    assert_eq!(task(&sql, "critical")["remindedCount"], 1);
    assert_eq!(task(&sql, "warn")["nextReminderAt"], Value::Null);
    db.advance_reminders(START + 1_020_001).unwrap();
    assert_eq!(task(&sql, "critical")["remindedCount"], 2);
    db.advance_reminders(START + 1_800_000).unwrap();
    for key in ["critical", "warn"] {
        let t = task(&sql, key);
        assert_eq!(t["status"], "expired");
        assert_eq!(t["actionVersion"], 2);
        assert_eq!(t["currentReminder"], Value::Null);
        assert_eq!(t["nextReminderAt"], Value::Null);
    }
    assert_eq!(db.advance_reminders(START + 1_800_001).unwrap(), 0);
}

#[test]
fn pause_and_snooze_cancel_active_reminder_and_never_resurrect_terminal_tasks() {
    let (_tmp, mut db, sql) = setup();
    seed(&sql, "task", "critical");
    db.advance_reminders(START + 300_000).unwrap();
    assert_eq!(
        run(&mut db, "pause", "pause", json!({}), START + 300_001)["data"]["ok"],
        true
    );
    assert_eq!(task(&sql, "task")["currentReminder"], Value::Null);
    assert_eq!(db.advance_reminders(START + 480_000).unwrap(), 0);
    run(&mut db, "resume", "resume", json!({}), START + 480_001);
    assert_eq!(db.advance_reminders(START + 480_002).unwrap(), 0);
    let snoozed = run(
        &mut db,
        "snooze",
        "snooze",
        json!({"taskId":"task","baseActionVersion":1,"minutes":2}),
        START + 500_000,
    );
    assert_eq!(snoozed["data"]["ok"], true);
    assert_eq!(db.advance_reminders(START + 619_999).unwrap(), 0);
    assert_eq!(db.advance_reminders(START + 620_000).unwrap(), 1);
    assert_eq!(task(&sql, "task")["snoozeUntil"], Value::Null);
    run(
        &mut db,
        "ignore",
        "ignore",
        json!({"taskId":"task","baseActionVersion":2}),
        START + 620_001,
    );
    assert_eq!(db.advance_reminders(START + 800_000).unwrap(), 0);
    assert_eq!(task(&sql, "task")["status"], "ignored");
}

#[test]
fn reminder_and_history_rollback_when_outbox_cannot_commit() {
    let (_tmp, mut db, sql) = setup();
    seed(&sql, "task", "critical");
    let original = task(&sql, "task");
    sql.execute_batch("CREATE TRIGGER reject_reminder BEFORE INSERT ON transport_outbox BEGIN SELECT RAISE(ABORT,'injected'); END;").unwrap();
    assert!(db.advance_reminders(START + 300_000).is_err());
    assert_eq!(task(&sql, "task"), original);
    assert_eq!(
        sql.query_row("SELECT COUNT(*) FROM task_actions", [], |r| r
            .get::<_, u64>(0))
            .unwrap(),
        0
    );
    sql.execute_batch("DROP TRIGGER reject_reminder;").unwrap();
    assert_eq!(db.advance_reminders(START + 300_000).unwrap(), 1);
}

#[test]
fn recovery_quarantines_old_reminders_and_disarms_restored_task_schedules() {
    let (_tmp, mut db, sql) = setup();
    seed(&sql, "task", "critical");
    db.advance_reminders(START + 300_000).unwrap();
    db.enter_recovery().unwrap();
    assert_eq!(db.advance_reminders(START + 480_000).unwrap(), 0);
    assert_eq!(db.outbox(20).unwrap().len(), 0);
    assert_eq!(task(&sql, "task")["currentReminder"], Value::Null);
    assert_eq!(task(&sql, "task")["nextReminderAt"], Value::Null);
    // Simulate completed evidence reconciliation; old epoch work stays disarmed.
    sql.execute("DELETE FROM recovery", []).unwrap();
    assert_eq!(db.advance_reminders(START + 500_000).unwrap(), 0);
}

#[test]
fn obsolete_reminder_frames_remain_evidence_but_are_not_retried_to_reconnected_phone() {
    let (_tmp, mut db, sql) = setup();
    seed(&sql, "task", "critical");
    db.advance_reminders(START + 300_000).unwrap();
    assert_eq!(
        db.outbox(20)
            .unwrap()
            .iter()
            .filter(|f| f["payload"]["kind"] == "reminder")
            .count(),
        1
    );
    db.advance_reminders(START + 360_000).unwrap();
    assert_eq!(
        db.outbox(20)
            .unwrap()
            .iter()
            .filter(|f| f["payload"]["kind"] == "reminder")
            .count(),
        0
    );
    assert_eq!(
        sql.query_row(
            "SELECT COUNT(*) FROM transport_outbox WHERE state='quarantined'",
            [],
            |r| r.get::<_, u64>(0)
        )
        .unwrap(),
        1
    );
    db.advance_reminders(START + 480_000).unwrap();
    run(
        &mut db,
        "snooze",
        "snooze",
        json!({"taskId":"task","baseActionVersion":1,"minutes":1}),
        START + 480_001,
    );
    assert_eq!(
        db.outbox(20)
            .unwrap()
            .iter()
            .filter(|f| f["payload"]["kind"] == "reminder")
            .count(),
        0
    );
    let ack = run(&mut db, "sync", "sync", json!({}), START + 480_002);
    let page_id = &ack["data"]["result"]["pageEventId"];
    let page = db
        .outbox(20)
        .unwrap()
        .into_iter()
        .find(|f| f["payload"]["id"] == *page_id)
        .unwrap();
    assert_eq!(
        page["payload"]["data"]["tasks"][0]["currentReminder"],
        Value::Null
    );
}

#[test]
fn actual_backup_restore_never_reissues_backed_up_reminder() {
    let (tmp, mut db, sql) = setup();
    seed(&sql, "task", "critical");
    db.advance_reminders(START + 300_000).unwrap();
    let backup = tmp.path().join("backup");
    db.backup_to(&backup).unwrap();
    let restore = tmp.path().join("restored");
    Storage::restore_from(&backup, &restore).unwrap();
    let mut restored = Storage::open(&restore).unwrap();
    assert_ne!(restored.identity.server_epoch, db.identity.server_epoch);
    assert!(restored.recovery_active().unwrap());
    assert_eq!(restored.advance_reminders(START + 480_000).unwrap(), 0);
    let sql = Connection::open(restore.join("tickdesk.sqlite3")).unwrap();
    assert_eq!(task(&sql, "task")["nextReminderAt"], Value::Null);
    assert!(restored.outbox(20).unwrap().is_empty());
}

#[test]
fn replacing_phone_retires_old_window_but_preserves_future_authoritative_reminders() {
    let (_tmp, mut db, sql) = setup();
    seed(&sql, "task", "critical");
    db.advance_reminders(START + 300_000).unwrap();
    db.bind_peer(support::LOCAL_PAIR, "replacement-app")
        .unwrap();
    assert!(db.outbox(20).unwrap().is_empty());
    assert_eq!(task(&sql, "task")["currentReminder"], Value::Null);
    assert_eq!(task(&sql, "task")["nextReminderAt"], START + 480_000);
    assert_eq!(db.advance_reminders(START + 480_000).unwrap(), 1);
    let frames = db.outbox(20).unwrap();
    assert_eq!(frames.len(), 1);
    assert_eq!(frames[0]["toIncarnation"], "replacement-app");
    assert_eq!(frames[0]["payload"]["data"]["remindedCount"], 3);
}

#[test]
fn obsolete_alert_backlog_does_not_consume_the_capacity_needed_by_current_round() {
    let (_tmp, mut db, sql) = setup();
    seed(&sql, "task", "critical");
    db.advance_reminders(START + 300_000).unwrap();
    let original = db.outbox(1).unwrap().remove(0);
    // Model an offline backlog of distinct historical reminders, at the existing queue limit.
    for n in 1..2048 {
        let mut frame = original.clone();
        frame["deliveryId"] = json!(format!("historical-delivery-{n}"));
        frame["payload"]["id"] = json!(format!("historical-event-{n}"));
        frame["payload"]["data"]["currentReminder"]["reminderId"] =
            json!(format!("historical-round-{n}"));
        frame["payloadHash"] = json!(tickdesk_local::contract::digest(&frame["payload"]).unwrap());
        sql.execute("INSERT INTO transport_outbox(delivery_id,pair_id,sender,recipient,payload_hash,event_id,frame) VALUES (?1,?2,?3,?4,?5,?6,?7)",
            params![frame["deliveryId"].as_str(),support::LOCAL_PAIR,db.identity.incarnation,support::PEER,frame["payloadHash"].as_str(),frame["payload"]["id"].as_str(),frame.to_string()]).unwrap();
    }
    assert_eq!(db.advance_reminders(START + 480_000).unwrap(), 1);
    assert_eq!(db.outbox(20).unwrap().len(), 1);
    assert_eq!(
        sql.query_row(
            "SELECT COUNT(*) FROM transport_outbox WHERE state='quarantined'",
            [],
            |r| r.get::<_, u64>(0)
        )
        .unwrap(),
        2048
    );
}

#[tokio::test]
async fn runtime_worker_schedules_existing_due_task_and_stops_cleanly() {
    let (_tmp, db, sql) = setup();
    seed(&sql, "task", "critical");
    let now = tickdesk_local::commands::now_ms();
    let mut t = task(&sql, "task");
    t["createdAt"] = json!(now - 300_000);
    t["nextReminderAt"] = json!(now);
    t["validUntil"] = json!(now + 1_000_000);
    t["lastRemindedAt"] = json!(now - 300_000);
    sql.execute(
        "UPDATE objects SET body=?1 WHERE collection='tasks'",
        [t.to_string()],
    )
    .unwrap();
    let shared = std::sync::Arc::new(std::sync::Mutex::new(db));
    let (stop, shutdown) = tokio::sync::watch::channel(false);
    let worker = tokio::spawn(tickdesk_local::reminders::run(shared.clone(), shutdown));
    tokio::time::timeout(std::time::Duration::from_secs(4), async {
        loop {
            if task(&sql, "task")["remindedCount"] == 2 {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    stop.send(true).unwrap();
    tokio::time::timeout(std::time::Duration::from_secs(2), worker)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert_eq!(
        shared.lock().unwrap().outbox(20).unwrap()[0]["payload"]["kind"],
        "reminder"
    );
}
