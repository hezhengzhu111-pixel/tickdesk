//! Durable reminder transitions for existing tasks. This module never creates tasks.
use crate::{commands, contract, storage::Storage};
use anyhow::Context;
use rusqlite::{params, Connection, Transaction, TransactionBehavior};
use serde_json::{json, Value};
use std::{
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::sync::watch;
use uuid::Uuid;

const WINDOW: u64 = 60_000;
const REPEAT: u64 = 180_000;

impl Storage {
    /// Returns the number of new reminder rounds, excluding expiry and housekeeping.
    /// The caller supplies wall time; no HTTP or other I/O occurs inside the transaction.
    pub fn advance_reminders(&mut self, now: u64) -> anyhow::Result<usize> {
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let count = advance(&tx, now, false)?;
        crate::budget::reconcile(&tx, now)?;
        tx.commit()?;
        Ok(count)
    }

    pub(crate) fn retire_obsolete_reminders(&mut self, now: u64) -> anyhow::Result<()> {
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        retire(&tx, now)?;
        tx.commit()?;
        Ok(())
    }
}

fn number(task: &Value, key: &str) -> anyhow::Result<u64> {
    task[key]
        .as_u64()
        .with_context(|| format!("invalid persisted task {key}"))
}

fn tasks(tx: &Transaction<'_>) -> anyhow::Result<Vec<Value>> {
    let mut statement =
        tx.prepare("SELECT body FROM objects WHERE collection='tasks' ORDER BY object_id")?;
    let rows = statement.query_map([], |r| r.get::<_, String>(0))?;
    rows.map(|body| Ok(serde_json::from_str(&body?)?)).collect()
}

fn save(tx: &Transaction<'_>, task: &mut Value) -> anyhow::Result<u64> {
    task["version"] = json!(number(task, "version")? + 1);
    contract::validate_definition("Task", task)?;
    tx.execute(
        "UPDATE objects SET body=?1 WHERE collection='tasks' AND object_id=?2",
        params![
            task.to_string(),
            task["taskId"].as_str().context("missing task ID")?
        ],
    )?;
    tx.execute(
        "UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='snapshot_seq'",
        [],
    )?;
    commands::snapshot_seq(tx)
}

fn record_action(tx: &Transaction<'_>, task: &Value, kind: &str, now: u64) -> anyhow::Result<()> {
    let id = Uuid::new_v4().to_string();
    let action = json!({"actionId":id,"taskId":task["taskId"],"type":kind,"at":now,
        "clientMsgId":null,"reason":null,"taskVersion":task["version"],"actionVersion":task["actionVersion"]});
    tx.execute(
        "INSERT INTO task_actions VALUES (?1,?2,?3,?4)",
        params![id, task["taskId"].as_str(), now, action.to_string()],
    )?;
    Ok(())
}

/// Used inside pause/resume's command transaction so no old active window survives an ack.
pub(crate) fn advance(
    tx: &Transaction<'_>,
    now: u64,
    force_suppressed: bool,
) -> anyhow::Result<usize> {
    let recovering: bool =
        tx.query_row("SELECT EXISTS(SELECT 1 FROM recovery)", [], |r| r.get(0))?;
    if recovering {
        return Ok(0);
    }
    let paired: bool = tx.query_row("SELECT EXISTS(SELECT 1 FROM peer)", [], |r| r.get(0))?;
    if !paired {
        return Ok(0);
    }
    let paused: bool = tx.query_row("SELECT paused FROM domain_state", [], |r| r.get(0))?;
    let suppressed = paused || force_suppressed;
    let epoch: String = tx.query_row(
        "SELECT value FROM metadata WHERE key='server_epoch'",
        [],
        |r| r.get(0),
    )?;
    // Retire stale alerts before reserving queue capacity for a new round.
    retire(tx, now)?;
    let mut emitted = 0;
    for mut task in tasks(tx)? {
        if task["status"] != "pending" && task["status"] != "reminded" {
            continue;
        }
        let original = task.clone();
        let until = number(&task, "validUntil")?;
        let mut action = None;
        let mut reminder = false;
        if now >= until {
            task["status"] = json!("expired");
            task["actionVersion"] = json!(number(&task, "actionVersion")? + 1);
            task["nextReminderAt"] = Value::Null;
            task["snoozeUntil"] = Value::Null;
            task["currentReminder"] = Value::Null;
            action = Some("expired");
        } else {
            let snoozed = task["snoozeUntil"]
                .as_u64()
                .is_some_and(|until| now < until);
            if suppressed
                || snoozed
                || task["currentReminder"]["displayUntil"]
                    .as_u64()
                    .is_some_and(|end| now >= end)
            {
                task["currentReminder"] = Value::Null;
            }
            if let Some(due) = task["nextReminderAt"].as_u64().filter(|due| *due <= now) {
                if !snoozed {
                    // A delayed worker only considers the latest scheduled round, never a burst.
                    let critical = task["level"] == "critical";
                    // A verified rule-only notice can be snoozed once per user action.
                    // It does not inherit the repeating legacy strategy reminder.
                    let repeating = critical && !task["score"].is_null();
                    let eligible = critical
                        || (task["level"] == "warn" && number(&task, "remindedCount")? < 2);
                    let scheduled = if repeating {
                        due + ((now - due) / REPEAT) * REPEAT
                    } else {
                        due
                    };
                    let display_until = until.min(scheduled.saturating_add(WINDOW));
                    if eligible && !suppressed && now < display_until {
                        task["remindedCount"] = json!(number(&task, "remindedCount")? + 1);
                        task["status"] = json!("reminded");
                        task["lastRemindedAt"] = json!(scheduled);
                        task["currentReminder"] = json!({"reminderId":Uuid::new_v4().to_string(),"scheduledAt":scheduled,"displayUntil":display_until});
                        action = Some("reminded");
                        reminder = true;
                    }
                    task["snoozeUntil"] = Value::Null;
                    let next = scheduled.saturating_add(REPEAT);
                    task["nextReminderAt"] = if repeating && next < until {
                        json!(next)
                    } else {
                        Value::Null
                    };
                }
            }
        }
        if task != original {
            let seq = save(tx, &mut task)?;
            if let Some(action) = action {
                record_action(tx, &task, action, now)?;
            }
            let kind = if reminder { "reminder" } else { "task_update" };
            let dedupe = if reminder {
                format!(
                    "reminder:{}:{}",
                    task["taskId"].as_str().unwrap(),
                    task["remindedCount"]
                )
            } else {
                format!(
                    "task:{}:{}",
                    task["taskId"].as_str().unwrap(),
                    task["version"]
                )
            };
            let event = json!({"v":2,"kind":kind,"id":Uuid::new_v4().to_string(),"dedupeKey":dedupe,
                "ts":now,"serverEpoch":epoch,"snapshotSeq":seq,"data":task});
            commands::enqueue_current(tx, &event)?;
            emitted += usize::from(reminder);
        }
    }
    retire(tx, now)?;
    Ok(emitted)
}

/// Preserve immutable event evidence and delivery IDs, but stop retrying obsolete alerts.
pub(crate) fn retire(db: &Connection, now: u64) -> anyhow::Result<()> {
    let blocked: bool = db.query_row(
        "SELECT paused OR EXISTS(SELECT 1 FROM recovery) FROM domain_state",
        [],
        |r| r.get(0),
    )?;
    let epoch: String = db.query_row(
        "SELECT value FROM metadata WHERE key='server_epoch'",
        [],
        |r| r.get(0),
    )?;
    let mut statement = db.prepare("SELECT o.delivery_id,o.frame,t.body FROM transport_outbox o LEFT JOIN objects t ON t.collection='tasks' AND t.object_id=json_extract(o.frame,'$.payload.data.taskId') WHERE o.state='pending' AND json_extract(o.frame,'$.payload.kind')='reminder'")?;
    let rows = statement.query_map([], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, String>(1)?,
            r.get::<_, Option<String>>(2)?,
        ))
    })?;
    for row in rows {
        let (id, frame, task) = row?;
        let frame: Value = serde_json::from_str(&frame)?;
        let task: Value = task
            .map(|t| serde_json::from_str(&t))
            .transpose()?
            .unwrap_or(Value::Null);
        let reminder = &task["currentReminder"];
        let valid = !blocked
            && frame["payload"]["serverEpoch"] == epoch
            && (task["status"] == "pending" || task["status"] == "reminded")
            && task["validUntil"].as_u64().is_some_and(|end| now < end)
            && task["snoozeUntil"].as_u64().is_none_or(|end| now >= end)
            && reminder["scheduledAt"]
                .as_u64()
                .is_some_and(|start| now >= start)
            && reminder["displayUntil"]
                .as_u64()
                .is_some_and(|end| now < end)
            && reminder == &frame["payload"]["data"]["currentReminder"];
        if !valid {
            db.execute("UPDATE transport_outbox SET state='quarantined' WHERE delivery_id=?1 AND state='pending'",[id])?;
        }
    }
    Ok(())
}

/// Recovery disarms old-epoch schedules. A new phone only retires the old active window.
pub(crate) fn disarm(tx: &Transaction<'_>, recovering: bool) -> anyhow::Result<()> {
    for mut task in tasks(tx)? {
        if !task["currentReminder"].is_null() || (recovering && !task["nextReminderAt"].is_null()) {
            task["currentReminder"] = Value::Null;
            if recovering {
                task["nextReminderAt"] = Value::Null;
            }
            save(tx, &mut task)?;
        }
    }
    Ok(())
}

pub async fn run(
    storage: Arc<Mutex<Storage>>,
    mut shutdown: watch::Receiver<bool>,
) -> anyhow::Result<()> {
    let mut tick = tokio::time::interval(Duration::from_secs(1));
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut failed = false;
    while !*shutdown.borrow() {
        tokio::select! {
            _ = shutdown.changed() => break,
            _ = tick.tick() => {
                let storage = storage.clone();
                let result = tokio::task::spawn_blocking(move || {
                    let mut storage = storage.lock().map_err(|_| anyhow::anyhow!("storage lock poisoned"))?;
                    storage.advance_reminders(commands::now_ms())
                }).await?;
                if result.is_err() && !failed {tracing::warn!("reminder transaction failed; retrying on next tick");}
                failed = result.is_err();
            }
        }
    }
    Ok(())
}
