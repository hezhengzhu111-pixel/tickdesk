use super::*;
use crate::contract::{digest, sha256, strict_json, validate_definition};
use base64::{engine::general_purpose::STANDARD, Engine};
use std::collections::BTreeSet;

struct Session {
    id: String,
    seq: u64,
}

#[derive(Clone)]
struct Record {
    key: String,
    source: String,
    hash: String,
    count: u64,
    epoch: String,
    captured: u64,
    observed: Option<u64>,
    status: String,
    body: Option<String>,
}

const RECORD_FIELDS: &str =
    "record_id,source,payload_hash,part_count,evidence_epoch,captured_at,observed_seq,status,body";

fn non_trade_operation(value: &Value) -> bool {
    matches!(value["kind"].as_str(), Some("command" | "ack"))
        && matches!(
            value["data"]["cmd"].as_str(),
            Some(
                "pause"
                    | "resume"
                    | "set_profile"
                    | "set_settings"
                    | "register_push"
                    | "watch"
                    | "unwatch"
                    | "scan_now"
                    | "chat"
                    | "accept_vision_notice"
                    | "register_image"
                    | "cancel_import"
                    | "begin_upload"
                    | "finalize_upload"
                    | "delete_file"
                    | "cancel_transfer"
                    | "ignore"
                    | "snooze"
                    | "reconcile_holding"
                    | "set_execution_progress"
            )
        )
}

fn fill_evidence(value: &Value) -> bool {
    (value["kind"] == "command"
        && (value["data"]["cmd"] == "record_execution"
            || (value["data"]["cmd"] == "act" && value["data"]["side"].is_string())))
        || (value["kind"] == "ack"
            && value["data"]["result"]["execution"]["executionId"].is_string())
}

impl Record {
    fn read(r: &rusqlite::Row<'_>) -> rusqlite::Result<Self> {
        Ok(Self {
            key: r.get(0)?,
            source: r.get(1)?,
            hash: r.get(2)?,
            count: r.get(3)?,
            epoch: r.get(4)?,
            captured: r.get(5)?,
            observed: r.get(6)?,
            status: r.get(7)?,
            body: r.get(8)?,
        })
    }
}

fn deterministic_rejection(ack: &Value) -> bool {
    ack["data"]["ok"] == false
        && matches!(
            ack["data"]["error"]["code"].as_str(),
            Some(
                "INVALID_PARAMS"
                    | "TASK_VERSION_CONFLICT"
                    | "TASK_TERMINAL"
                    | "TASK_EXPIRED"
                    | "TASK_STATE_CONFLICT"
                    | "INVALID_SELLABLE_QTY"
                    | "COMMAND_ID_REUSED"
                    | "RECOVERY_REQUIRED"
            )
        )
}

impl Domain<'_, '_> {
    pub(super) fn recovery(&self) -> anyhow::Result<Outcome> {
        let session = self
            .tx
            .query_row("SELECT session_id,reviewed_seq FROM recovery", [], |r| {
                Ok(Session {
                    id: r.get(0)?,
                    seq: r.get(1)?,
                })
            })
            .optional()?;
        let Some(session) = session else {
            return Ok(failure("RECOVERY_NOT_ACTIVE"));
        };
        if self.data["reconciliationId"]
            .as_str()
            .is_some_and(|id| id != session.id)
        {
            return Ok(failure("INVALID_PARAMS"));
        }
        let name = str_at(self.data, "cmd");
        if matches!(
            name,
            "recovery_seal_evidence" | "recovery_reconcile" | "recovery_confirm"
        ) && self.data["reviewedSnapshotSeq"] != session.seq
        {
            return Ok(Err((
                "RECOVERY_VERSION_CONFLICT",
                json!({"reviewedSnapshotSeq":session.seq}),
            )));
        }
        match name {
            "get_recovery" => self.recovery_status(&session),
            "recovery_evidence" => self.evidence(&session),
            "recovery_seal_evidence" => self.seal(&session),
            "recovery_reconcile" => self.reconcile(&session),
            "recovery_confirm" => self.confirm(&session),
            _ => unreachable!(),
        }
    }

    fn records(&self, session: &Session) -> anyhow::Result<Vec<Record>> {
        let mut q = self.tx.prepare(&format!(
            "SELECT {RECORD_FIELDS} FROM recovery_records WHERE session_id=?1 ORDER BY record_id"
        ))?;
        let rows = q.query_map([&session.id], Record::read)?;
        Ok(rows.collect::<rusqlite::Result<_>>()?)
    }

    fn record_summary(&self, session: &Session, r: &Record) -> anyhow::Result<Value> {
        let received: u64 = self.tx.query_row(
            "SELECT count(*) FROM recovery_parts WHERE session_id=?1 AND record_id=?2",
            params![session.id, r.key],
            |q| q.get(0),
        )?;
        Ok(
            json!({"recordId":r.key,"sourceType":r.source,"payloadHash":r.hash,"partCount":r.count,
            "receivedParts":received,"status":r.status}),
        )
    }

    fn sealed(&self, session: &Session) -> anyhow::Result<bool> {
        Ok(self.tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM recovery_seals WHERE session_id=?1 AND reviewed_seq=?2)",
            params![session.id, session.seq],
            |r| r.get(0),
        )?)
    }

    fn recovery_status(&self, session: &Session) -> anyhow::Result<Outcome> {
        let records = self.records(session)?;
        let count = |status: &str| records.iter().filter(|r| r.status == status).count();
        let result = self.page(&format!("recovery:{}:{}",session.id,session.seq),"pageCursor",|| {
            let rows = self.read_bodies(
                "SELECT body FROM recovery_items WHERE session_id=?1 ORDER BY item_id",
                [&session.id],
            )?;
            let mut entries = records
                .iter()
                .map(|r| Ok(json!({"record":self.record_summary(session,r)?})))
                .collect::<anyhow::Result<Vec<_>>>()?;
            entries.extend(rows.into_iter().map(|row| json!({"item":row})));
            Ok(entries)
        },
            json!({"reconciliationId":session.id,"reviewedSnapshotSeq":session.seq,"recoveryMode":true,
                "verifiedCount":count("verified"),"receivingCount":count("receiving"),"invalidCount":count("invalid"),
                "evidenceStatus":if self.sealed(session)? {"sealed"} else {"receiving"}}),"nextPageCursor",true)?;
        Ok(result.map(|mut page| {
            let entries = page.as_object_mut().unwrap().remove("items").unwrap();
            for (tag, key) in [("record", "records"), ("item", "items")] {
                page[key] = json!(entries
                    .as_array()
                    .unwrap()
                    .iter()
                    .filter_map(|v| v.get(tag))
                    .collect::<Vec<_>>());
            }
            page
        }))
    }

    fn advance(&self, session: &Session) -> anyhow::Result<u64> {
        let seq = self.bump()?;
        self.tx.execute(
            "UPDATE recovery SET reviewed_seq=?1 WHERE session_id=?2",
            params![seq, session.id],
        )?;
        self.tx.execute(
            "DELETE FROM recovery_seals WHERE session_id=?1",
            [&session.id],
        )?;
        Ok(seq)
    }

    fn evidence(&self, session: &Session) -> anyhow::Result<Outcome> {
        let data = self.data;
        let key = str_at(data, "recordId");
        let index = int(data, "partIndex");
        let count = int(data, "partCount");
        let Ok(bytes) = STANDARD.decode(str_at(data, "bytesBase64")) else {
            return Ok(failure("INVALID_PARAMS"));
        };
        if index >= count
            || bytes.is_empty()
            || bytes.len() > 4096
            || data["evidenceEpoch"] == self.epoch
        {
            return Ok(failure("INVALID_PARAMS"));
        }
        let existing = self.records(session)?.into_iter().find(|r| r.key == key);
        if let Some(r) = existing {
            if r.hash != str_at(data, "payloadHash")
                || r.source != str_at(data, "sourceType")
                || r.count != count
                || r.epoch != str_at(data, "evidenceEpoch")
                || r.captured != int(data, "capturedAt")
                || r.observed != data["observedSnapshotSeq"].as_u64()
            {
                return Ok(failure("INVALID_PARAMS"));
            }
            let old:Option<Vec<u8>>=self.tx.query_row("SELECT bytes FROM recovery_parts WHERE session_id=?1 AND record_id=?2 AND part_index=?3",
                params![session.id,key,index], |row|row.get(0)).optional()?;
            if let Some(old) = old {
                if old != bytes {
                    return Ok(failure("INVALID_PARAMS"));
                }
                return Ok(Ok(
                    json!({"reconciliationId":session.id,"record":self.record_summary(session,&r)?,
                    "reviewedSnapshotSeq":session.seq}),
                ));
            }
        } else {
            self.tx.execute("INSERT INTO recovery_records(session_id,record_id,source,payload_hash,part_count,evidence_epoch,captured_at,observed_seq)
                VALUES (?1,?2,?3,?4,?5,?6,?7,?8)",params![session.id,key,str_at(data,"sourceType"),str_at(data,"payloadHash"),
                count,str_at(data,"evidenceEpoch"),int(data,"capturedAt"),data["observedSnapshotSeq"].as_u64()])?;
        }
        self.tx.execute(
            "INSERT INTO recovery_parts VALUES (?1,?2,?3,?4)",
            params![session.id, key, index, bytes],
        )?;
        let all = self.records(session)?;
        let record = all
            .iter()
            .find(|r| r.key == key)
            .expect("inserted recovery record");
        let mut q=self.tx.prepare("SELECT bytes FROM recovery_parts WHERE session_id=?1 AND record_id=?2 ORDER BY part_index")?;
        let parts = q
            .query_map(params![session.id, key], |r| r.get::<_, Vec<u8>>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        if parts.len() as u64 == count {
            let bytes: Vec<u8> = parts.into_iter().flatten().collect();
            let verified = self.parse_record(record, &bytes)?;
            if let Some(value) = verified {
                // A well formed ACK is retained pending proof of its matching original command.
                let state = if record.source == "ack" {
                    "receiving"
                } else {
                    "verified"
                };
                self.tx.execute("UPDATE recovery_records SET status=?1,body=?2 WHERE session_id=?3 AND record_id=?4",
                    params![state,serde_json::to_string(&value)?,session.id,key])?;
                if state == "verified" {
                    self.add_item(session, record, &value)?;
                }
            } else {
                self.tx.execute("UPDATE recovery_records SET status='invalid' WHERE session_id=?1 AND record_id=?2",params![session.id,key])?;
            }
            self.match_acks(session)?;
        }
        let seq = self.advance(session)?;
        let record = self
            .records(session)?
            .into_iter()
            .find(|r| r.key == key)
            .unwrap();
        Ok(Ok(
            json!({"reconciliationId":session.id,"record":self.record_summary(session,&record)?,"reviewedSnapshotSeq":seq}),
        ))
    }

    fn parse_record(&self, record: &Record, bytes: &[u8]) -> anyhow::Result<Option<Value>> {
        if sha256(bytes) != record.hash {
            return Ok(None);
        }
        let Ok(value) = strict_json(bytes) else {
            return Ok(None);
        };
        if canonical(&value)? != bytes
            || value["serverEpoch"] != record.epoch
            || validate_definition(&format!("Evidence_{}", record.source), &value).is_err()
        {
            return Ok(None);
        }
        let valid = match record.source.as_str() {
            "command" => value["data"]["serverEpoch"] == value["serverEpoch"],
            "ack" => record
                .observed
                .is_some_and(|seq| int(&value, "snapshotSeq") <= seq),
            _ => {
                value["snapshotSeq"].as_u64() == record.observed
                    && value["capturedAt"] == record.captured
            }
        };
        Ok(valid.then_some(value))
    }

    fn match_acks(&self, session: &Session) -> anyhow::Result<()> {
        let records = self.records(session)?;
        let mut commands = std::collections::BTreeSet::new();
        for r in &records {
            if r.source == "command" && r.status == "verified" {
                let value: Value = serde_json::from_str(r.body.as_deref().unwrap())?;
                commands.insert((
                    r.epoch.clone(),
                    str_at(&value["data"], "clientMsgId").to_owned(),
                    digest(&value["data"])?,
                ));
            }
        }
        for r in records
            .iter()
            .filter(|r| r.source == "ack" && r.status == "receiving" && r.body.is_some())
        {
            let ack: Value = serde_json::from_str(r.body.as_deref().unwrap())?;
            let key = str_at(&ack["data"], "clientMsgId");
            let hash = str_at(&ack["data"], "requestHash");
            let local:bool=self.tx.query_row(
                "SELECT EXISTS(SELECT 1 FROM command_results WHERE pair_id=?1 AND epoch=?2 AND client_msg_id=?3 AND request_hash=?4
                 UNION ALL SELECT 1 FROM transport_inbox WHERE pair_id=?1 AND command_epoch=?2 AND client_msg_id=?3 AND request_hash=?4)",
                params![self.pair,r.epoch,key,hash],|v|v.get(0))?;
            if local || commands.contains(&(r.epoch.clone(), key.to_owned(), hash.to_owned())) {
                self.tx.execute("UPDATE recovery_records SET status='verified' WHERE session_id=?1 AND record_id=?2",
                    params![session.id,r.key])?;
                self.add_item(session, r, &ack)?;
            }
        }
        Ok(())
    }

    /// A discarded intent needs a durable, deterministic rejection. A missing
    /// ack, `not_found`, an unknown outcome, or a successful trade ack is not
    /// proof that no fill exists.
    fn definitively_not_executed(&self, session: &Session, item: &Value) -> anyhow::Result<bool> {
        let (Some(epoch), Some(client), Some(hash)) = (
            item["oldEpoch"].as_str(),
            item["oldClientMsgId"].as_str(),
            item["requestHash"].as_str(),
        ) else {
            return Ok(false);
        };
        let body: Option<String> = self
            .tx
            .query_row(
                "SELECT body FROM command_results WHERE pair_id=?1 AND epoch=?2
                 AND client_msg_id=?3 AND request_hash=?4",
                params![self.pair, epoch, client, hash],
                |row| row.get(0),
            )
            .optional()?;
        if let Some(body) = body {
            let ack: Value = serde_json::from_str(&body)?;
            return Ok(deterministic_rejection(&ack));
        }
        // A backup can predate the rejection ACK, so the restored ledger has no
        // command_result row. The uploaded evidence may still prove the exact
        // intent: one verified original command whose canonical data hashes to
        // this item, plus one verified ACK carrying the same request hash and a
        // deterministic validation rejection. Success, not_found, an unknown
        // outcome or a missing ACK never qualifies.
        let refs = item["evidenceRecordIds"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        let mut command_matched = false;
        let mut ack_matched = false;
        for record in self.records(session)? {
            if record.status != "verified" || !refs.contains(&json!(record.key)) {
                continue;
            }
            let Some(body) = record.body.as_deref() else {
                continue;
            };
            let value: Value = serde_json::from_str(body)?;
            match record.source.as_str() {
                "command" => {
                    let data = &value["data"];
                    if str_at(data, "clientMsgId") == client
                        && value["serverEpoch"] == record.epoch
                        && digest(data)? == hash
                    {
                        command_matched = true;
                    }
                }
                "ack" => {
                    let data = &value["data"];
                    if str_at(data, "clientMsgId") == client
                        && str_at(data, "requestHash") == hash
                        && deterministic_rejection(&value)
                    {
                        ack_matched = true;
                    }
                }
                _ => {}
            }
        }
        Ok(command_matched && ack_matched)
    }

    fn add_item(&self, session: &Session, r: &Record, value: &Value) -> anyhow::Result<()> {
        let (subject, client, hash, allowed) = if matches!(r.source.as_str(), "command" | "ack") {
            let d = &value["data"];
            let client = str_at(d, "clientMsgId");
            let hash = if r.source == "command" {
                digest(d)?
            } else {
                str_at(d, "requestHash").to_owned()
            };
            let db_ack:Option<String>=self.tx.query_row("SELECT body FROM command_results WHERE pair_id=?1 AND epoch=?2 AND client_msg_id=?3 AND request_hash=?4",
                params![self.pair,r.epoch,client,hash],|row|row.get(0)).optional()?;
            if let Some(local) = db_ack {
                if r.source == "command" || serde_json::from_str::<Value>(&local)? == *value {
                    return Ok(());
                }
            }
            if crate::contract::policy(str_at(d, "cmd"))?.mode == "read" {
                return Ok(());
            }
            (
                format!("{}:{client}:{hash}", r.epoch),
                json!(client),
                json!(hash),
                if non_trade_operation(value) {
                    json!(["archive_operation"])
                } else if r.source == "ack" && d["ok"] == true {
                    json!(["included", "record"])
                } else {
                    json!(["included", "record", "discard_intent"])
                },
            )
        } else {
            let (collection, key) = if r.source == "position_snapshot" {
                ("positions", "symbol")
            } else {
                ("tasks", "taskId")
            };
            let record = &value["record"];
            if self.get(collection, str_at(record, key))?.as_ref() == Some(record) {
                return Ok(());
            }
            (
                format!("{}:{}:{}", r.epoch, r.source, r.key),
                Value::Null,
                Value::Null,
                json!(["included"]),
            )
        };
        let existing: Option<String> = self
            .tx
            .query_row(
                "SELECT body FROM recovery_items WHERE session_id=?1 AND subject=?2",
                params![session.id, subject],
                |r| r.get(0),
            )
            .optional()?;
        let mut item = if let Some(existing) = existing {
            serde_json::from_str::<Value>(&existing)?
        } else {
            json!({"itemId":id(),"oldEpoch":r.epoch,"oldClientMsgId":client,"requestHash":hash,
                "evidenceRecordIds":[],"summary":"恢复的本地状态与手机证据需核对。","allowedDecisions":allowed,"disposition":null})
        };
        let refs = item["evidenceRecordIds"].as_array_mut().unwrap();
        if !refs.iter().any(|key| key == &r.key) && refs.len() < 100 {
            refs.push(json!(r.key));
        }
        if r.source == "ack" && value["data"]["ok"] == true {
            item["allowedDecisions"] = allowed;
        }
        item["disposition"] = Value::Null;
        self.tx.execute(
            "INSERT INTO recovery_items(session_id,item_id,subject,body) VALUES (?1,?2,?3,?4)
            ON CONFLICT(session_id,subject) DO UPDATE SET body=excluded.body,decision=NULL",
            params![
                session.id,
                str_at(&item, "itemId"),
                subject,
                serde_json::to_string(&item)?
            ],
        )?;
        Ok(())
    }

    fn seal(&self, session: &Session) -> anyhow::Result<Outcome> {
        let records = self.records(session)?;
        if records.iter().any(|r| r.status != "verified")
            || self.data["recordCount"] != records.len()
        {
            return Ok(failure("RECOVERY_EVIDENCE_INCOMPLETE"));
        }
        let manifest: Vec<_> = records
            .into_iter()
            .map(|r| json!({"recordId":r.key,"sourceType":r.source,"payloadHash":r.hash}))
            .collect();
        let hash = digest(&json!(manifest))?;
        if self.data["manifestHash"] != hash {
            return Ok(Err((
                "RECOVERY_EVIDENCE_INCOMPLETE",
                json!({"expectedHash":hash}),
            )));
        }
        self.tx.execute("INSERT INTO recovery_seals VALUES (?1,?2,?3,?4)
            ON CONFLICT(session_id) DO UPDATE SET reviewed_seq=excluded.reviewed_seq,record_count=excluded.record_count,manifest_hash=excluded.manifest_hash",
            params![session.id,session.seq,manifest.len(),hash])?;
        Ok(Ok(
            json!({"reconciliationId":session.id,"reviewedSnapshotSeq":session.seq,"evidenceStatus":"sealed"}),
        ))
    }

    fn reconcile(&self, session: &Session) -> anyhow::Result<Outcome> {
        let mut seen = BTreeSet::new();
        let mut closed = Vec::new();
        let mut recorded = Vec::new();
        let positions = self.data["positions"].as_array().unwrap();
        let initial_revision = self.position_revision()?;
        for position in positions {
            if position["basePositionRevision"] != initial_revision
                || !seen.insert(str_at(position, "symbol"))
            {
                return Ok(failure("POSITION_VERSION_CONFLICT"));
            }
        }
        seen.clear();
        for decision in self.data["decisions"].as_array().unwrap() {
            let key = str_at(decision, "itemId");
            let disposition = str_at(decision, "disposition");
            if !seen.insert(key) {
                return Ok(failure("INVALID_PARAMS"));
            }
            let item:Option<String>=self.tx.query_row("SELECT body FROM recovery_items WHERE session_id=?1 AND item_id=?2 AND decision IS NULL",
                params![session.id,key],|r|r.get(0)).optional()?;
            let Some(item) = item else {
                return Ok(failure("INVALID_PARAMS"));
            };
            let mut item: Value = serde_json::from_str(&item)?;
            let refs = decision["evidenceRecordIds"].as_array().unwrap();
            if !item["allowedDecisions"]
                .as_array()
                .unwrap()
                .contains(&json!(disposition))
                || refs.is_empty()
                || !refs.iter().all(|entry| {
                    item["evidenceRecordIds"]
                        .as_array()
                        .unwrap()
                        .contains(entry)
                })
            {
                return Ok(failure("INVALID_PARAMS"));
            }
            let sources = self
                .records(session)?
                .into_iter()
                .filter(|r| refs.contains(&json!(r.key)))
                .collect::<Vec<_>>();
            let payloads = sources
                .iter()
                .map(|r| serde_json::from_str::<Value>(r.body.as_deref().unwrap()))
                .collect::<serde_json::Result<Vec<_>>>()?;
            let linked_symbol = payloads.iter().find_map(|body| {
                body["record"]["symbol"]
                    .as_str()
                    .or_else(|| body["data"]["symbol"].as_str())
                    .or_else(|| body["data"]["result"]["execution"]["symbol"].as_str())
            });
            let linked_task = payloads.iter().find_map(|body| {
                body["record"]["taskId"]
                    .as_str()
                    .or_else(|| body["data"]["taskId"].as_str())
                    .or_else(|| body["data"]["result"]["task"]["taskId"].as_str())
            });
            if let Some(task) = decision.get("taskResolution") {
                if task["taskId"].as_str() != linked_task {
                    return Ok(failure("INVALID_PARAMS"));
                }
                let task_id = str_at(task, "taskId");
                let Some(mut current) = self.get("tasks", task_id)? else {
                    return Ok(failure("TASK_NOT_FOUND"));
                };
                if matches!(str_at(&current, "status"), "acted" | "ignored" | "expired")
                    && current["status"] != task["status"]
                {
                    return Ok(failure("TASK_TERMINAL"));
                }
                current["status"] = task["status"].clone();
                for k in ["currentReminder", "nextReminderAt", "snoozeUntil"] {
                    current[k] = Value::Null;
                }
                for k in ["version", "actionVersion"] {
                    current[k] = json!(int(&current, k) + 1);
                }
                self.put("tasks", task_id, &current)?;
            }
            match disposition {
                "record" => {
                    if !payloads.iter().any(fill_evidence) {
                        return Ok(failure("INVALID_PARAMS"));
                    }
                    let input = &decision["execution"];
                    let symbol = str_at(input, "symbol");
                    let new_key = str_at(decision, "newExecutionId");
                    if linked_symbol.is_some_and(|expected| expected != symbol) {
                        return Ok(failure("INVALID_PARAMS"));
                    }
                    if self.get("executions", new_key)?.is_some() {
                        return Ok(failure("RECOVERY_EXECUTION_ID_CONFLICT"));
                    }
                    let record = self.record_recovered(input, symbol, new_key)?;
                    if let Err(error) = record {
                        return Ok(Err(error));
                    }
                    recorded.push(json!({"itemId":key,"executionId":new_key}));
                }
                "included" => {
                    let fill = payloads
                        .iter()
                        .map(|body| &body["data"]["result"]["execution"])
                        .find(|execution| execution["executionId"].is_string());
                    let has_fill_intent = payloads.iter().any(|body| {
                        body["kind"] == "command"
                            && (body["data"]["cmd"] == "record_execution"
                                || (body["data"]["cmd"] == "act"
                                    && body["data"]["side"].is_string()))
                    });
                    let included = if let Some(fill) = fill {
                        let execution_id = str_at(fill, "executionId");
                        self.get("executions", execution_id)?.is_some_and(|local| {
                            [
                                "executionId",
                                "symbol",
                                "qty",
                                "price",
                                "executedAt",
                                "side",
                            ]
                            .iter()
                            .all(|&field| local[field] == fill[field])
                                && (local["reconciliationRequired"] == false
                                    || positions.iter().any(|position| {
                                        position["symbol"] == fill["symbol"]
                                            && position["resolvedExecutionIds"]
                                                .as_array()
                                                .is_some_and(|ids| {
                                                    ids.contains(&json!(execution_id))
                                                })
                                    }))
                        })
                    } else if has_fill_intent {
                        false
                    } else if payloads
                        .iter()
                        .any(|body| body["data"]["cmd"] == "adjust_position")
                    {
                        // Closing an old adjustment requires a new explicit, versioned
                        // baseline; the historical ack is evidence, never a projection.
                        let baselines: Vec<_> = payloads
                            .iter()
                            .filter_map(|body| {
                                if body["data"]["cmd"] != "adjust_position" {
                                    return None;
                                }
                                if body["kind"] == "command" {
                                    Some(&body["data"])
                                } else if body["data"]["ok"] == true {
                                    Some(&body["data"]["result"]["adjustment"]["after"])
                                } else {
                                    None
                                }
                            })
                            .collect();
                        !baselines.is_empty()
                            && positions.iter().any(|position| {
                                baselines.iter().all(|old| {
                                    old["symbol"] == position["symbol"]
                                        && old["asOf"]
                                            .as_u64()
                                            .is_some_and(|at| int(position, "asOf") >= at)
                                })
                            })
                    } else if sources.iter().any(|r| r.source == "position_snapshot") {
                        linked_symbol.is_some_and(|symbol| {
                            positions.iter().any(|p| {
                                p["symbol"] == symbol
                                    && payloads.iter().all(|old| {
                                        old["record"]["asOf"]
                                            .as_u64()
                                            .is_none_or(|at| int(p, "asOf") >= at)
                                    })
                            })
                        })
                    } else {
                        linked_task.is_some() && decision.get("taskResolution").is_some()
                    };
                    if !included {
                        return Ok(failure("INVALID_PARAMS"));
                    }
                }
                "archive_operation" => {
                    // Require all evidence attached to this item, not a convenient subset.
                    // Persist the decision only; no historical ack changes current state.
                    if refs.len() != item["evidenceRecordIds"].as_array().unwrap().len()
                        || refs
                            .iter()
                            .filter_map(Value::as_str)
                            .collect::<BTreeSet<_>>()
                            .len()
                            != refs.len()
                        || !payloads.iter().all(non_trade_operation)
                        || str_at(decision, "note").trim().is_empty()
                        || decision.get("taskResolution").is_some()
                    {
                        return Ok(failure("INVALID_PARAMS"));
                    }
                }
                "discard_intent" => {
                    if !sources.iter().any(|r| r.source == "command")
                        || str_at(decision, "note").trim().is_empty()
                        || decision.get("taskResolution").is_some()
                        || !self.definitively_not_executed(session, &item)?
                    {
                        return Ok(failure("RECOVERY_EVIDENCE_INCOMPLETE"));
                    }
                }
                _ => unreachable!(),
            }
            item["disposition"] = json!(disposition);
            self.tx.execute(
                "UPDATE recovery_items SET body=?1,decision=?2 WHERE session_id=?3 AND item_id=?4",
                params![
                    serde_json::to_string(&item)?,
                    serde_json::to_string(decision)?,
                    session.id,
                    key
                ],
            )?;
            closed.push(key);
        }
        for position in positions {
            let mut input = position.clone();
            input["basePositionRevision"] = json!(self.position_revision()?);
            let domain = Domain {
                data: &input,
                ..*self
            };
            if let Err(error) = domain.adjust()? {
                return Ok(Err(error));
            }
        }
        let seq = if closed.is_empty() && positions.is_empty() {
            session.seq
        } else {
            self.advance(session)?
        };
        Ok(Ok(
            json!({"reconciliationId":session.id,"reviewedSnapshotSeq":seq,
            "closedItemIds":closed,"recordedExecutions":recorded,"remainingCount":self.open_items(session)?}),
        ))
    }

    fn open_items(&self, session: &Session) -> anyhow::Result<u64> {
        Ok(self.tx.query_row(
            "SELECT count(*) FROM recovery_items WHERE session_id=?1 AND decision IS NULL",
            [&session.id],
            |r| r.get(0),
        )?)
    }

    fn confirm(&self, session: &Session) -> anyhow::Result<Outcome> {
        if !self.sealed(session)?
            || self.open_items(session)? != 0
            || self
                .records(session)?
                .iter()
                .any(|r| r.status != "verified")
        {
            return Ok(failure("RECOVERY_EVIDENCE_INCOMPLETE"));
        }
        let original_files = self.tx.query_row("SELECT count(*) FROM recovery_original_reviews WHERE session_id=?1 AND decision IS NULL",
            [&session.id],|r|r.get::<_,u64>(0))?;
        let unreconciled = self.tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM objects WHERE collection='executions'
            AND json_extract(body,'$.reconciliationRequired')=1)",
            [],
            |r| r.get::<_, bool>(0),
        )?;
        if original_files != 0 || unreconciled {
            return Ok(failure("RECOVERY_EVIDENCE_INCOMPLETE"));
        }
        self.tx.execute("INSERT INTO recovery_completed SELECT session_id,original_epoch,?1,reviewed_seq,?2 FROM recovery",
            params![self.epoch,self.now])?;
        self.tx
            .execute("DELETE FROM recovery WHERE session_id=?1", [&session.id])?;
        Ok(Ok(
            json!({"reconciliationId":session.id,"serverEpoch":self.epoch,"recoveryMode":false,"snapshotSeq":self.bump()?}),
        ))
    }
}
