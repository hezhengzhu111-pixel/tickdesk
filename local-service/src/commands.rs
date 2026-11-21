use crate::{
    contract::{canonical, digest, parse_frame, policy, validate_business, BUSINESS_LIMIT},
    domain::Domain,
    storage::{Identity, Storage},
};
use anyhow::{bail, ensure, Context};
use rusqlite::{params, Connection, OptionalExtension, Row, Transaction, TransactionBehavior};
use serde_json::{json, Value};
use uuid::Uuid;

const MAX_PENDING_INBOX: i64 = 1024;
const MAX_PENDING_OUTBOX: i64 = 2048;

#[derive(Clone)]
struct Peer {
    pair: String,
    incarnation: String,
    fence: i64,
}

#[derive(Clone)]
pub struct Job {
    pub(crate) id: i64,
    fence: i64,
}

pub fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

pub fn valid_id(id: &str) -> bool {
    (1..=64).contains(&id.len())
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

fn text(value: &Value) -> &str {
    value.as_str().expect("validated contract string")
}

fn peer(db: &Connection) -> anyhow::Result<Option<Peer>> {
    Ok(db
        .query_row(
            "SELECT pair_id,incarnation,fence FROM peer WHERE singleton=1",
            [],
            |r| {
                Ok(Peer {
                    pair: r.get(0)?,
                    incarnation: r.get(1)?,
                    fence: r.get(2)?,
                })
            },
        )
        .optional()?)
}

impl Storage {
    pub fn bind_peer(&mut self, pair_id: &str, incarnation: &str) -> anyhow::Result<()> {
        ensure!(
            valid_id(pair_id) && valid_id(incarnation),
            "invalid peer identity"
        );
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        match peer(&tx)? {
            None => {
                tx.execute(
                    "INSERT INTO peer VALUES (1,?1,?2,1)",
                    params![pair_id, incarnation],
                )?;
            }
            Some(old) => {
                ensure!(
                    old.pair == pair_id,
                    "data directory belongs to another pairing"
                );
                if old.incarnation != incarnation {
                    let retired = tx.query_row("SELECT EXISTS(SELECT 1 FROM retired_peers WHERE pair_id=?1 AND incarnation=?2)",
                        params![pair_id, incarnation], |r| r.get::<_, bool>(0))?;
                    ensure!(!retired, "peer incarnation was retired");
                    tx.execute(
                        "INSERT INTO retired_peers VALUES (?1,?2)",
                        params![old.pair, old.incarnation],
                    )?;
                    tx.execute(
                        "UPDATE peer SET incarnation=?1,fence=fence+1 WHERE singleton=1",
                        [incarnation],
                    )?;
                    quarantine(&tx)?;
                }
            }
        }
        tx.commit()?;
        Ok(())
    }

    pub fn enter_recovery(&mut self) -> anyhow::Result<()> {
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        ensure!(!recovery_active(&tx)?, "recovery is already active");
        let incarnation = Uuid::new_v4().to_string();
        let epoch = Uuid::new_v4().to_string();
        tx.execute(
            "UPDATE metadata SET value=?1 WHERE key='incarnation'",
            [&incarnation],
        )?;
        tx.execute(
            "UPDATE metadata SET value=?1 WHERE key='server_epoch'",
            [&epoch],
        )?;
        tx.execute(
            "INSERT INTO recovery VALUES (1,?1,?2,?3)",
            params![
                Uuid::new_v4().to_string(),
                self.identity.server_epoch,
                snapshot_seq(&tx)?
            ],
        )?;
        tx.execute("INSERT INTO recovery_original_reviews(session_id,file_id)
            SELECT (SELECT session_id FROM recovery),file_id FROM files WHERE state IN ('available','missing','deleting')",[])?;
        tx.execute(
            "UPDATE files SET state='missing',version=version+1 WHERE state='available'",
            [],
        )?;
        tx.execute("UPDATE peer SET fence=fence+1 WHERE singleton=1", [])?;
        quarantine(&tx)?;
        tx.execute("UPDATE recovery SET reviewed_seq=?1", [snapshot_seq(&tx)?])?;
        tx.commit()?;
        self.identity = Identity {
            incarnation,
            server_epoch: epoch,
        };
        Ok(())
    }

    pub fn recovery_active(&self) -> anyhow::Result<bool> {
        recovery_active(&self.connection)
    }

    pub fn receive(&mut self, frame: &Value) -> anyhow::Result<Value> {
        parse_frame(&canonical(frame)?)?;
        if frame["type"] == "deliver" && frame["category"] == "file_chunk" {
            return self.receive_file_chunk(frame);
        }
        ensure!(
            frame["type"] == "deliver"
                && frame["category"] == "business"
                && frame["payload"]["kind"] == "command",
            "INVALID_FRAME"
        );
        ensure!(
            frame["toIncarnation"] == self.identity.incarnation,
            "INCARNATION_MISMATCH"
        );
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let sender = peer(&tx)?.context("INCARNATION_MISMATCH")?;
        ensure!(
            frame["fromIncarnation"] == sender.incarnation,
            "INCARNATION_MISMATCH"
        );
        let id = text(&frame["deliveryId"]);
        let hash = text(&frame["payloadHash"]);
        let file_delivery: bool = tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM file_chunk_receipts
            WHERE pair_id=?1 AND peer=?2 AND delivery_id=?3)",
            params![sender.pair, sender.incarnation, id],
            |r| r.get(0),
        )?;
        ensure!(!file_delivery, "DELIVERY_ID_REUSED");
        let existing: Option<(String, i64)> = tx.query_row(
            "SELECT payload_hash,fence FROM transport_inbox WHERE pair_id=?1 AND peer=?2 AND delivery_id=?3",
            params![sender.pair, sender.incarnation, id], |r| Ok((r.get(0)?, r.get(1)?))).optional()?;
        if let Some((original, fence)) = existing {
            ensure!(fence == sender.fence, "INCARNATION_MISMATCH");
            if original != hash {
                tx.execute(
                    "INSERT OR IGNORE INTO delivery_conflicts VALUES (?1,?2,?3,?4,?5)",
                    params![sender.pair, sender.incarnation, id, original, hash],
                )?;
                tx.commit()?;
                bail!("DELIVERY_ID_REUSED");
            }
        } else {
            let count: i64 = tx.query_row(
                "SELECT count(*) FROM transport_inbox WHERE state='pending'",
                [],
                |row| row.get(0),
            )?;
            ensure!(count < MAX_PENDING_INBOX, "DISK_FULL");
            let body = &frame["payload"];
            let data = &body["data"];
            let command_epoch = body["serverEpoch"]
                .as_str()
                .unwrap_or(&self.identity.server_epoch);
            tx.execute("INSERT INTO transport_inbox(pair_id,peer,fence,delivery_id,payload_hash,received_epoch,command_epoch,client_msg_id,request_hash,body)
                VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)",
                params![sender.pair, sender.incarnation, sender.fence, id, hash, self.identity.server_epoch, command_epoch,
                    text(&data["clientMsgId"]), digest(data)?, serde_json::to_string(body)?])?;
        }
        tx.commit()?;
        Ok(
            json!({"v":2,"type":"receipt","deliveryId":id,"payloadHash":hash,
            "fromIncarnation":sender.incarnation,"toIncarnation":self.identity.incarnation}),
        )
    }

    pub fn pending(&self) -> anyhow::Result<Option<Job>> {
        Ok(self
            .connection
            .query_row(
                "SELECT id,fence FROM transport_inbox WHERE state='pending' ORDER BY id LIMIT 1",
                [],
                read_job,
            )
            .optional()?)
    }

    pub fn job(&self, delivery_id: &str) -> anyhow::Result<Option<Job>> {
        Ok(self.connection.query_row(
            "SELECT i.id,i.fence FROM transport_inbox i JOIN peer p ON i.pair_id=p.pair_id AND i.peer=p.incarnation
            WHERE i.state='pending' AND i.fence=p.fence AND i.delivery_id=?1",
            [delivery_id], read_job).optional()?)
    }

    pub fn process(&mut self, job: Job, now: u64) -> anyhow::Result<Option<Value>> {
        // The Storage owner serializes all file operations and peer transitions.
        // Run archive I/O before opening the write transaction; recheck the fence below.
        let preparation = self.prepare_pending_file(&job, now)?;
        self.process_prepared(job, now, preparation)
    }

    pub fn process_prepared(
        &mut self,
        job: Job,
        now: u64,
        preparation: Option<Value>,
    ) -> anyhow::Result<Option<Value>> {
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let Some(sender) = peer(&tx)? else {
            return Ok(None);
        };
        let command: Option<(String, String)> = tx.query_row(
            "SELECT body,request_hash FROM transport_inbox
            WHERE id=?1 AND fence=?2 AND fence=?3 AND received_epoch=?4 AND pair_id=?5 AND peer=?6 AND state='pending'",
            params![job.id, job.fence, sender.fence, self.identity.server_epoch, sender.pair, sender.incarnation],
            |r| Ok((r.get(0)?, r.get(1)?))).optional()?;
        let Some((body, hash)) = command else {
            return Ok(None);
        };
        let body: Value = serde_json::from_str(&body)?;
        let context = Command {
            identity: &self.identity,
            sender: &sender,
            body: &body,
            hash: &hash,
            now,
        };
        let ack = context.apply(&tx, preparation.as_ref())?;
        enqueue(&tx, &self.identity, &sender, &ack)?;
        let state = if ack["dedupeKey"]
            .as_str()
            .is_some_and(|k| k.starts_with("ack_reject:"))
        {
            "rejected"
        } else {
            "processed"
        };
        tx.execute(
            "UPDATE transport_inbox SET state=?1 WHERE id=?2",
            params![state, job.id],
        )?;
        if ack["data"]["ok"] == true
            || (ack["data"]["cmd"] == "scan_now" && ack["data"]["result"]["scanId"].is_string())
        {
            crate::budget::reconcile(&tx, now)?;
        }
        tx.commit()?;
        Ok(Some(ack))
    }

    /// Returns only current, unexecuted commands. Network preparation is never done under the database mutex.
    pub fn pending_job_data(&self, job: &Job) -> anyhow::Result<Option<Value>> {
        let body:Option<String>=self.connection.query_row("SELECT i.body FROM transport_inbox i JOIN peer p ON i.pair_id=p.pair_id AND i.peer=p.incarnation AND i.fence=p.fence WHERE i.id=?1 AND i.fence=?2 AND i.received_epoch=?3 AND i.state='pending' AND NOT EXISTS(SELECT 1 FROM command_results r WHERE r.pair_id=i.pair_id AND r.epoch=i.command_epoch AND r.client_msg_id=i.client_msg_id) AND NOT EXISTS(SELECT 1 FROM recovery)",params![job.id,job.fence,self.identity.server_epoch],|r|r.get(0)).optional()?;
        body.map(|body| Ok(serde_json::from_str::<Value>(&body)?["data"].clone()))
            .transpose()
    }

    pub(crate) fn next_job(&self, after: i64) -> anyhow::Result<Option<Job>> {
        Ok(self.connection.query_row("SELECT id,fence FROM transport_inbox WHERE state='pending' AND id>?1 ORDER BY id LIMIT 1",[after],read_job).optional()?)
    }

    fn prepare_pending_file(&mut self, job: &Job, now: u64) -> anyhow::Result<Option<Value>> {
        let candidate: Option<String> = self.connection.query_row(
            "SELECT i.body FROM transport_inbox i JOIN peer p ON i.pair_id=p.pair_id AND i.peer=p.incarnation AND i.fence=p.fence
             WHERE i.id=?1 AND i.fence=?2 AND i.received_epoch=?3 AND i.command_epoch=?3 AND i.state='pending'
             AND json_extract(i.body,'$.data.cmd') IN ('finalize_upload','delete_file')
             AND NOT EXISTS (SELECT 1 FROM recovery)
             AND NOT EXISTS (SELECT 1 FROM command_results r WHERE r.pair_id=i.pair_id AND r.epoch=i.command_epoch AND r.client_msg_id=i.client_msg_id)
             AND NOT EXISTS (SELECT 1 FROM transport_inbox q WHERE q.pair_id=i.pair_id AND q.command_epoch=i.command_epoch AND q.client_msg_id=i.client_msg_id AND q.state='quarantined')",
            params![job.id,job.fence,self.identity.server_epoch],|r|r.get(0)).optional()?;
        let Some(candidate) = candidate else {
            return Ok(None);
        };
        let body: Value = serde_json::from_str(&candidate)?;
        if body["data"]["cmd"] == "delete_file" {
            self.prepare_delete(&body["data"], now)
        } else {
            self.prepare_file_command(&body["data"], now)
        }
    }

    pub fn outbox(&self, limit: u32) -> anyhow::Result<Vec<Value>> {
        self.pending_outbox(limit.min(16), false)
    }

    pub fn file_outbox(&self) -> anyhow::Result<Vec<Value>> {
        self.pending_outbox(4, true)
    }

    fn pending_outbox(&self, limit: u32, files: bool) -> anyhow::Result<Vec<Value>> {
        let mut query = self.connection.prepare(
            "SELECT o.frame FROM transport_outbox o JOIN peer p ON o.pair_id=p.pair_id AND o.recipient=p.incarnation
            WHERE o.state='pending' AND o.sender=?1 AND
            (EXISTS (SELECT 1 FROM file_download_chunks c WHERE c.delivery_id=o.delivery_id))=?3
            ORDER BY o.id LIMIT ?2")?;
        let mut rows = query.query(params![self.identity.incarnation, limit, files])?;
        let mut frames = Vec::new();
        while let Some(row) = rows.next()? {
            frames.push(serde_json::from_str(&row.get::<_, String>(0)?)?);
        }
        Ok(frames)
    }

    pub fn accept_receipt(&mut self, receipt: &Value) -> anyhow::Result<bool> {
        parse_frame(&canonical(receipt)?)?;
        ensure!(receipt["type"] == "receipt", "INVALID_FRAME");
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let Some(sender) = peer(&tx)? else {
            return Ok(false);
        };
        if receipt["fromIncarnation"] != self.identity.incarnation
            || receipt["toIncarnation"] != sender.incarnation
        {
            return Ok(false);
        }
        crate::download::expire_downloads(&tx, now_ms())?;
        let count = tx.execute(
            "UPDATE transport_outbox SET state='delivered' WHERE state IN ('pending','delivered')
            AND pair_id=?1 AND sender=?2 AND recipient=?3 AND delivery_id=?4 AND payload_hash=?5",
            params![
                sender.pair,
                self.identity.incarnation,
                sender.incarnation,
                text(&receipt["deliveryId"]),
                text(&receipt["payloadHash"])
            ],
        )?;
        if count == 1 {
            crate::download::receive_download_receipt(&tx, text(&receipt["deliveryId"]))?;
        }
        tx.commit()?;
        Ok(count == 1)
    }

    pub fn quarantine_delivery(&mut self, delivery_id: &str) -> anyhow::Result<()> {
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        crate::download::quarantine_download_delivery(&tx, delivery_id)?;
        tx.execute("UPDATE transport_outbox SET state='quarantined' WHERE state='pending' AND delivery_id=?1",[delivery_id])?;
        tx.commit()?;
        Ok(())
    }

    pub fn record_startup(&self) -> anyhow::Result<()> {
        self.connection.execute(
            "INSERT INTO diagnostics(at,level,code,text)
            VALUES (?1,'INFO','LOCAL_STARTED','TickDesk 本地服务已启动。')",
            [now_ms()],
        )?;
        self.connection.execute(
            "DELETE FROM diagnostics WHERE id < (SELECT max(id)-10000 FROM diagnostics)",
            [],
        )?;
        Ok(())
    }
}

fn read_job(row: &Row<'_>) -> rusqlite::Result<Job> {
    Ok(Job {
        id: row.get(0)?,
        fence: row.get(1)?,
    })
}

fn quarantine(tx: &Transaction<'_>) -> anyhow::Result<()> {
    crate::reminders::disarm(tx, recovery_active(tx)?)?;
    crate::download::cancel_all_downloads(tx)?;
    tx.execute(
        "UPDATE file_uploads SET state='cancelled' WHERE state='receiving'",
        [],
    )?;
    tx.execute("DELETE FROM file_upload_chunks WHERE upload_id IN (SELECT upload_id FROM file_uploads WHERE state='cancelled')",[])?;
    tx.execute(
        "UPDATE transport_inbox SET state='quarantined' WHERE state='pending'",
        [],
    )?;
    tx.execute(
        "UPDATE transport_outbox SET state='quarantined' WHERE state='pending'",
        [],
    )?;
    Ok(())
}

fn recovery_active(db: &Connection) -> anyhow::Result<bool> {
    Ok(db.query_row("SELECT EXISTS(SELECT 1 FROM recovery)", [], |r| r.get(0))?)
}

pub(crate) fn snapshot_seq(db: &Connection) -> anyhow::Result<u64> {
    Ok(db.query_row(
        "SELECT CAST(value AS INTEGER) FROM metadata WHERE key='snapshot_seq'",
        [],
        |r| r.get(0),
    )?)
}

pub(crate) fn enqueue_current(tx: &Transaction<'_>, event: &Value) -> anyhow::Result<()> {
    let sender = peer(tx)?.context("reminder requires a paired endpoint")?;
    let identity = Identity {
        incarnation: tx.query_row(
            "SELECT value FROM metadata WHERE key='incarnation'",
            [],
            |r| r.get(0),
        )?,
        server_epoch: tx.query_row(
            "SELECT value FROM metadata WHERE key='server_epoch'",
            [],
            |r| r.get(0),
        )?,
    };
    enqueue(tx, &identity, &sender, event)
}

fn enqueue(
    tx: &Transaction<'_>,
    identity: &Identity,
    peer: &Peer,
    event: &Value,
) -> anyhow::Result<()> {
    validate_business(event)?;
    let bytes = canonical(event)?;
    ensure!(
        bytes.len() <= BUSINESS_LIMIT,
        "response exceeds v2 message limit"
    );
    let event_id = text(&event["id"]);
    let hash = crate::contract::sha256(&bytes);
    let existing: Option<String> = tx.query_row("SELECT payload_hash FROM transport_outbox WHERE pair_id=?1 AND sender=?2 AND recipient=?3 AND event_id=?4 AND state='pending'",
        params![peer.pair, identity.incarnation, peer.incarnation, event_id], |r| r.get(0)).optional()?;
    if let Some(existing) = existing {
        ensure!(existing == hash, "immutable event mismatch");
        return Ok(());
    }
    let count: i64 = tx.query_row(
        "SELECT count(*) FROM transport_outbox WHERE state='pending'",
        [],
        |r| r.get(0),
    )?;
    ensure!(count < MAX_PENDING_OUTBOX, "DISK_FULL");
    let id = Uuid::new_v4().to_string();
    let frame = json!({"v":2,"type":"deliver","deliveryId":id,"fromIncarnation":identity.incarnation,
        "toIncarnation":peer.incarnation,"category":"business","payloadHash":hash,"payload":event});
    tx.execute("INSERT INTO transport_outbox(delivery_id,pair_id,sender,recipient,payload_hash,event_id,frame)
        VALUES (?1,?2,?3,?4,?5,?6,?7)",
        params![id, peer.pair, identity.incarnation, peer.incarnation, hash, event_id, serde_json::to_string(&frame)?])?;
    Ok(())
}

struct Command<'a> {
    identity: &'a Identity,
    sender: &'a Peer,
    body: &'a Value,
    hash: &'a str,
    now: u64,
}

pub(crate) type Outcome = Result<Value, (&'static str, Value)>;

pub(crate) fn failure(code: &'static str) -> Outcome {
    Err((code, json!({})))
}

impl Command<'_> {
    fn apply(&self, tx: &Transaction<'_>, prepared: Option<&Value>) -> anyhow::Result<Value> {
        let data = &self.body["data"];
        let name = text(&data["cmd"]);
        let client_id = text(&data["clientMsgId"]);
        let epoch = &self.identity.server_epoch;
        if self.body["serverEpoch"]
            .as_str()
            .is_some_and(|received| received != epoch)
        {
            return self.reject(tx, "SERVER_EPOCH_MISMATCH", json!({"currentEpoch":epoch}));
        }
        let existing: Option<(String,String)> = tx.query_row(
            "SELECT request_hash,body FROM command_results WHERE pair_id=?1 AND epoch=?2 AND client_msg_id=?3",
            params![self.sender.pair, epoch, client_id], |r| Ok((r.get(0)?,r.get(1)?))).optional()?;
        if let Some((hash, ack)) = existing {
            if hash == self.hash {
                let ack: Value = serde_json::from_str(&ack)?;
                if !self.can_send_ack(tx, &ack)? {
                    return self.reject(tx, "AI_REPLY_UNAVAILABLE", json!({}));
                }
                let mut saved = tx
                    .prepare("SELECT body FROM command_events WHERE ack_id=?1 ORDER BY event_id")?;
                let rows = saved
                    .query_map([text(&ack["id"])], |row| row.get::<_, String>(0))?
                    .collect::<rusqlite::Result<Vec<_>>>()?;
                for item in rows {
                    let event = serde_json::from_str(&item)?;
                    if !self.can_send_ack(tx, &event)? {
                        return self.reject(tx, "AI_REPLY_UNAVAILABLE", json!({}));
                    }
                    enqueue(tx, self.identity, self.sender, &event)?;
                }
                return Ok(ack);
            }
            return self.reject(tx, "COMMAND_ID_REUSED", json!({}));
        }
        let quarantined: Option<String> = tx
            .query_row(
                "SELECT request_hash FROM transport_inbox
            WHERE pair_id=?1 AND command_epoch=?2 AND client_msg_id=?3 AND state='quarantined'
            ORDER BY id LIMIT 1",
                params![self.sender.pair, epoch, client_id],
                |row| row.get(0),
            )
            .optional()?;
        if let Some(hash) = quarantined {
            let code = if hash == self.hash {
                "RECOVERY_REQUIRED"
            } else {
                "COMMAND_ID_REUSED"
            };
            return self.reject(tx, code, json!({}));
        }
        let rule = policy(name)?;
        let recovering = recovery_active(tx)?;
        tx.execute_batch("SAVEPOINT dispatch")?;
        let mut events = Vec::new();
        let result = if recovering && !rule.allowed_during_recovery {
            failure("RECOVERY_REQUIRED")
        } else if !recovering && rule.only_during_recovery {
            failure("RECOVERY_NOT_ACTIVE")
        } else {
            match name {
                "get_command_status" => self.lookup(tx, &mut events)?,
                _ => Domain {
                    tx,
                    data,
                    pair: &self.sender.pair,
                    epoch,
                    recovering,
                    now: self.now,
                }
                .run(&mut events, prepared)?,
            }
        };
        let scan_result = name == "scan_now"
            && result
                .as_ref()
                .err()
                .is_some_and(|(_, details)| details.get("scan").is_some());
        if result.is_err() && !scan_result {
            tx.execute_batch("ROLLBACK TO dispatch")?;
            events.clear();
        }
        let key = format!("ack:{client_id}");
        let mut ack = self.ack(tx, key.clone(), result)?;
        if canonical(&ack)?.len() > BUSINESS_LIMIT {
            tx.execute_batch("ROLLBACK TO dispatch")?;
            events.clear();
            ack = self.ack(tx, key, failure("MESSAGE_TOO_LARGE"))?;
        }
        tx.execute_batch("RELEASE dispatch")?;
        for event in events {
            enqueue(tx, self.identity, self.sender, &event)?;
            tx.execute(
                "INSERT INTO command_events VALUES (?1,?2,?3)",
                params![
                    text(&ack["id"]),
                    text(&event["id"]),
                    serde_json::to_string(&event)?
                ],
            )?;
        }
        tx.execute(
            "INSERT INTO command_results VALUES (?1,?2,?3,?4,?5)",
            params![
                self.sender.pair,
                epoch,
                client_id,
                self.hash,
                serde_json::to_string(&ack)?
            ],
        )?;
        Ok(ack)
    }

    fn reject(
        &self,
        tx: &Transaction<'_>,
        code: &'static str,
        details: Value,
    ) -> anyhow::Result<Value> {
        let id = text(&self.body["data"]["clientMsgId"]);
        let epoch = &self.identity.server_epoch;
        let key = params![self.sender.pair, epoch, id, self.hash, code];
        let prior: Option<String> = tx.query_row("SELECT body FROM command_rejections WHERE pair_id=?1 AND epoch=?2 AND client_msg_id=?3 AND request_hash=?4 AND code=?5", key, |r|r.get(0)).optional()?;
        if let Some(body) = prior {
            return Ok(serde_json::from_str(&body)?);
        }
        let ack = self.ack(
            tx,
            format!("ack_reject:{epoch}:{id}:{}", self.hash),
            Err((code, details)),
        )?;
        tx.execute(
            "INSERT INTO command_rejections VALUES (?1,?2,?3,?4,?5,?6)",
            params![
                self.sender.pair,
                epoch,
                id,
                self.hash,
                code,
                serde_json::to_string(&ack)?
            ],
        )?;
        Ok(ack)
    }

    fn ack(&self, tx: &Transaction<'_>, key: String, result: Outcome) -> anyhow::Result<Value> {
        let (ok, result, error) = match result {
            Ok(result) => (true, result, Value::Null),
            Err((code, mut details)) => (
                false,
                if self.body["data"]["cmd"] == "scan_now" {
                    details
                        .as_object_mut()
                        .and_then(|d| d.remove("scan"))
                        .unwrap_or(Value::Null)
                } else {
                    Value::Null
                },
                json!({"code":code,"message":code,"retryable":false,"details":details}),
            ),
        };
        let data = &self.body["data"];
        Ok(
            json!({"v":2,"kind":"ack","id":Uuid::new_v4().to_string(),"dedupeKey":key,"ts":self.now,
            "serverEpoch":self.identity.server_epoch,"snapshotSeq":snapshot_seq(tx)?,"data":{
              "clientMsgId":data["clientMsgId"],"refId":self.body["id"],"cmd":data["cmd"],"requestHash":self.hash,
              "ok":ok,"result":result,"error":error
            }}),
        )
    }

    fn lookup(&self, tx: &Transaction<'_>, events: &mut Vec<Value>) -> anyhow::Result<Outcome> {
        let data = &self.body["data"];
        let epoch = text(&data["originalEpoch"]);
        let id = text(&data["targetClientMsgId"]);
        let hash = text(&data["targetRequestHash"]);
        let same_command = params![self.sender.pair, epoch, id];
        let result: Option<(String,String)> = tx.query_row("SELECT request_hash,body FROM command_results WHERE pair_id=?1 AND epoch=?2 AND client_msg_id=?3",
            same_command, |r|Ok((r.get(0)?,r.get(1)?))).optional()?;
        let (state, event_id) = if let Some((record_hash, body)) = result {
            if record_hash != hash {
                ("hash_mismatch", Value::Null)
            } else {
                let event: Value = serde_json::from_str(&body)?;
                ensure!(
                    event["kind"] == "ack"
                        && event["serverEpoch"] == epoch
                        && event["data"]["clientMsgId"] == id
                        && event["data"]["requestHash"] == hash,
                    "corrupt command result"
                );
                if !self.can_send_ack(tx, &event)? {
                    return Ok(Ok(json!({"state":"not_found","ackEventId":null})));
                }
                let id = event["id"].clone();
                events.push(event);
                ("found", id)
            }
        } else {
            let state: Option<i32> = tx.query_row(
                "SELECT max(CASE WHEN request_hash<>?4 THEN 0 WHEN state='quarantined' THEN 1 WHEN state='pending' THEN 2 ELSE 3 END)
                FROM transport_inbox WHERE pair_id=?1 AND command_epoch=?2 AND client_msg_id=?3 AND state<>'rejected'",
                params![self.sender.pair, epoch, id, hash], |r|r.get(0))?;
            (
                match state {
                    Some(0) => "hash_mismatch",
                    Some(1) => "quarantined",
                    Some(2) => "processing",
                    Some(3) => {
                        tracing::error!("committed inbox row has no matching command result");
                        return Ok(failure("INTERNAL_ERROR"));
                    }
                    _ => "not_found",
                },
                Value::Null,
            )
        };
        Ok(Ok(json!({"state":state,"ackEventId":event_id})))
    }

    fn can_send_ack(&self, tx: &Transaction<'_>, ack: &Value) -> anyhow::Result<bool> {
        if ack["data"]["cmd"] != "get_reply_page" || ack["data"]["ok"] != true {
            return Ok(true);
        }
        let result = &ack["data"]["result"];
        let Some((epoch, request_id)) = ack["serverEpoch"]
            .as_str()
            .zip(result["requestId"].as_str())
        else {
            return Ok(false);
        };
        crate::chat::reply_pages::can_read(tx, &self.sender.pair, epoch, request_id)
    }
}

pub const SUPPORTED_COMMANDS: &[&str] = &[
    "watch",
    "unwatch",
    "get_candidates",
    "get_quant_observations",
    "search_symbols",
    "get_chart",
    "get_quote_status",
    "get_storage_status",
    "get_history_coverage",
    "scan_now",
    "chat",
    "accept_vision_notice",
    "register_image",
    "cancel_import",
    "apply_import",
    "act",
    "ignore",
    "snooze",
    "get_task",
    "get_task_actions",
    "record_execution",
    "get_trade_review",
    "record_trade_review_note",
    "withdraw_trade_review_note",
    "get_trade_review_notes",
    "get_execution_progress",
    "set_execution_progress",
    "adjust_position",
    "get_holding_details",
    "reconcile_holding",
    "pause",
    "resume",
    "set_profile",
    "set_settings",
    "register_push",
    "sync",
    "get_logs",
    "get_messages",
    "list_reports",
    "get_report",
    "get_reply_page",
    "get_status",
    "get_command_status",
    "get_recovery",
    "recovery_evidence",
    "recovery_seal_evidence",
    "recovery_reconcile",
    "recovery_confirm",
    "begin_upload",
    "get_upload",
    "finalize_upload",
    "list_files",
    "read_file",
    "cancel_transfer",
    "delete_file",
];
