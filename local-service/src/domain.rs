use crate::{
    commands::{failure, snapshot_seq, Outcome, SUPPORTED_COMMANDS},
    contract::{canonical, BUSINESS_LIMIT},
};
use rusqlite::{params, OptionalExtension, Transaction};
use serde_json::{json, Value};
use uuid::Uuid;

mod ledger;
mod recovery;
mod snapshot;
mod tasks;

pub(super) struct Domain<'a, 'db> {
    pub tx: &'a Transaction<'db>,
    pub data: &'a Value,
    pub pair: &'a str,
    pub epoch: &'a str,
    pub recovering: bool,
    pub now: u64,
}

struct KernelHealth {
    analysis: &'static str,
    dependency: &'static str,
    checked_at: Option<u64>,
    version: Option<String>,
    code: Option<&'static str>,
}

pub(super) const COLLECTIONS: [&str; 7] = [
    "tasks",
    "positions",
    "executions",
    "watchlist",
    "imports",
    "valuationPoints",
    "adjustments",
];
pub(super) const TTL: u64 = 600_000;

pub(super) fn str_at<'a>(value: &'a Value, key: &str) -> &'a str {
    value[key].as_str().expect("validated field")
}

pub(super) fn int(value: &Value, key: &str) -> u64 {
    value[key].as_u64().expect("validated integer")
}

pub(super) fn id() -> String {
    Uuid::new_v4().to_string()
}

fn directory_size(path: &std::path::Path) -> Option<u64> {
    let metadata = match std::fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Some(0),
        Err(_) => return None,
    };
    if crate::file_service::is_link(&metadata) {
        return None;
    }
    if !metadata.is_dir() {
        return metadata.is_file().then_some(metadata.len());
    }
    let mut total = 0u64;
    for entry in std::fs::read_dir(path).ok()? {
        let entry = entry.ok()?;
        let child = entry.path();
        let metadata = std::fs::symlink_metadata(&child).ok()?;
        if crate::file_service::is_link(&metadata) {
            continue;
        }
        total = total.checked_add(if metadata.is_dir() {
            directory_size(&child)?
        } else {
            metadata.len()
        })?;
    }
    Some(total)
}

fn latest_backup_at(path: &std::path::Path) -> Result<Option<u64>, ()> {
    let mut latest = None;
    let entries = match std::fs::read_dir(path) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err(()),
    };
    for entry in entries {
        let entry = entry.map_err(|_| ())?;
        if !entry.file_type().map_err(|_| ())?.is_dir() {
            continue;
        }
        let manifest = entry.path().join("manifest.json");
        let Ok(file) = std::fs::File::open(manifest) else {
            continue;
        };
        let Ok(value) =
            serde_json::from_reader::<_, Value>(std::io::Read::take(file, 16 * 1024 * 1024))
        else {
            continue;
        };
        let Some(completed) = value["completedAt"].as_u64() else {
            continue;
        };
        latest = latest.max(Some(completed));
    }
    Ok(latest)
}

impl Domain<'_, '_> {
    pub fn run(
        &self,
        events: &mut Vec<Value>,
        prepared: Option<&Value>,
    ) -> anyhow::Result<Outcome> {
        match str_at(self.data, "cmd") {
            "get_status" => self.status(),
            "get_storage_status" => self.storage_status(),
            "get_history_coverage" => crate::market::read_history_coverage(self),
            "get_quant_observations" => crate::quant_observations::read(self),
            "read_file" => crate::download::download_command(self),
            "cancel_transfer" if self.data["kind"] == "download" => {
                crate::download::download_command(self)
            }
            "cancel_transfer" => crate::file_service::file_command(self, prepared),
            "delete_file" => crate::deletion::delete_command(self, prepared),
            "begin_upload" | "get_upload" | "finalize_upload" | "list_files" => {
                crate::file_service::file_command(self, prepared)
            }
            "get_recovery"
            | "recovery_evidence"
            | "recovery_seal_evidence"
            | "recovery_reconcile"
            | "recovery_confirm" => self.recovery(),
            "pause" | "set_profile" => self.control(),
            "resume" => crate::market::resume(self, events, prepared),
            "watch" | "unwatch" | "get_candidates" | "search_symbols" | "get_chart"
            | "get_quote_status" | "scan_now" => crate::market::command(self, events, prepared),
            "chat" => crate::chat::chat_command(self),
            "accept_vision_notice" | "register_image" | "cancel_import" | "apply_import" => {
                crate::vision::command(self, events)
            }
            "set_settings" => self.set_settings(),
            "register_push" => self.register_push(),
            "adjust_position" => self.adjust(),
            "reconcile_holding" => self.reconcile_holding(),
            "get_holding_details" => self.get_holding_details(),
            "record_execution" => self.record_execution(),
            "get_trade_review" => self.trade_review(),
            "record_trade_review_note"
            | "withdraw_trade_review_note"
            | "get_trade_review_notes" => self.manual_trade_review(),
            "get_execution_progress" | "set_execution_progress" => {
                crate::budget::progress_command(self)
            }
            "sync" => self.sync(events),
            "act" | "ignore" | "snooze" => self.change_task(events),
            "get_task" => self.get_task(),
            "get_task_actions" => self.get_task_actions(),
            "get_logs" => self.get_logs(),
            "get_messages" => self.get_messages(),
            "list_reports" | "get_report" => crate::chat::reports::command(self),
            "get_reply_page" => crate::chat::reply_pages::command(self),
            _ => Ok(failure("DATA_UNAVAILABLE")),
        }
    }

    pub fn get(&self, collection: &str, key: &str) -> anyhow::Result<Option<Value>> {
        let item: Option<String> = self
            .tx
            .query_row(
                "SELECT body FROM objects WHERE collection=?1 AND object_id=?2",
                params![collection, key],
                |r| r.get(0),
            )
            .optional()?;
        Ok(item.map(|body| serde_json::from_str(&body)).transpose()?)
    }

    pub fn put(&self, collection: &str, key: &str, body: &Value) -> anyhow::Result<()> {
        self.tx.execute("INSERT INTO objects VALUES (?1,?2,?3) ON CONFLICT(collection,object_id) DO UPDATE SET body=excluded.body",
            params![collection, key, serde_json::to_string(body)?])?;
        Ok(())
    }

    pub fn list(&self, collection: &str) -> anyhow::Result<Vec<Value>> {
        self.read_bodies(
            "SELECT body FROM objects WHERE collection=?1 ORDER BY object_id",
            [collection],
        )
    }

    fn read_bodies(&self, sql: &str, params: impl rusqlite::Params) -> anyhow::Result<Vec<Value>> {
        let mut stmt = self.tx.prepare(sql)?;
        let data = stmt.query_map(params, |r| r.get::<_, String>(0))?;
        data.map(|item| Ok(serde_json::from_str(&item?)?)).collect()
    }

    pub fn bump(&self) -> anyhow::Result<u64> {
        self.tx.execute(
            "UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='snapshot_seq'",
            [],
        )?;
        snapshot_seq(self.tx)
    }

    pub fn seq(&self) -> anyhow::Result<u64> {
        snapshot_seq(self.tx)
    }

    fn kernel_health(&self) -> anyhow::Result<KernelHealth> {
        let mut health = KernelHealth {
            analysis: "offline",
            dependency: "unknown",
            checked_at: None,
            version: None,
            code: None,
        };
        let probe: Option<(u64, String, Option<String>, bool)> = self
            .tx
            .query_row(
                "SELECT checked_at,status,version,
                 strategy_ids='[]' AND strategy_load_errors=0 AND EXISTS (
                   SELECT 1 FROM json_each(usable_capabilities) WHERE value='daily')
                 FROM kernel_probe WHERE singleton=1",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
            )
            .optional()?;
        if let Some((at, state, version, no_strategies)) = probe {
            health.checked_at = Some(at);
            if self.now < at || self.now - at > 45_000 {
                health.code = Some("KERNEL_PROBE_STALE");
            } else {
                health.version = version;
                let (status, code) = match state.as_str() {
                    "available" => ("online", None),
                    "degraded" if no_strategies => {
                        ("degraded", Some("KERNEL_NO_PRODUCTION_STRATEGIES"))
                    }
                    "degraded" => ("degraded", Some("KERNEL_CAPABILITIES")),
                    "auth_required" => ("offline", Some("KERNEL_AUTH_REQUIRED")),
                    _ => ("offline", Some("KERNEL_UNREACHABLE")),
                };
                health.dependency = status;
                health.code = code;
                if status == "online" {
                    health.analysis = "online";
                }
            }
        }
        Ok(health)
    }

    pub fn analysis(&self) -> anyhow::Result<Value> {
        let kernel = self.kernel_health()?.analysis;
        let last_scan = self
            .get("market_meta", "latest_scan")?
            .map(|scan| scan["scanId"].clone())
            .unwrap_or(Value::Null);
        Ok(self.tx.query_row(
            "SELECT paused,control_version,profile FROM domain_state",
            [],
            |r| {
                Ok(
                    json!({"paused":r.get::<_,bool>(0)?,"controlVersion":r.get::<_,u64>(1)?,
                "profile":r.get::<_,String>(2)?,"lastScanId":last_scan,"kernelStatus":kernel}),
                )
            },
        )?)
    }

    pub fn settings(&self) -> anyhow::Result<Value> {
        let body: String = self
            .tx
            .query_row("SELECT settings FROM domain_state", [], |r| r.get(0))?;
        Ok(serde_json::from_str(&body)?)
    }

    pub fn position_revision(&self) -> anyhow::Result<u64> {
        Ok(self
            .tx
            .query_row("SELECT position_revision FROM domain_state", [], |r| {
                r.get(0)
            })?)
    }

    pub fn bump_position(&self) -> anyhow::Result<u64> {
        self.tx.execute(
            "UPDATE domain_state SET position_revision=position_revision+1",
            [],
        )?;
        self.position_revision()
    }

    fn storage_status(&self) -> anyhow::Result<Outcome> {
        let path: String = self.tx.query_row(
            "SELECT file FROM pragma_database_list WHERE name='main'",
            [],
            |row| row.get(0),
        )?;
        let Some(root) = std::path::Path::new(&path).parent() else {
            return Ok(Ok(json!({
                "status":"unknown","reason":"DATA_DIRECTORY_UNKNOWN",
                "originalsBytes":null,"quarantineBytes":null,"stagingBytes":null,
                "availableBytes":null,"lastSuccessfulBackupAt":null,
            })));
        };
        let originals = directory_size(&root.join("files"));
        let quarantine = directory_size(&root.join("restore-quarantine"));
        let staging = directory_size(&root.join("tmp"));
        let available = fs2::available_space(root).ok();
        let backup = latest_backup_at(&root.join("backups"));
        let complete = originals.is_some()
            && quarantine.is_some()
            && staging.is_some()
            && available.is_some()
            && backup.is_ok();
        let backup = backup.unwrap_or(None);
        Ok(Ok(json!({
            "status": if complete {"available"} else {"unknown"},
            "reason": if complete {Value::Null} else {json!("STORAGE_MEASUREMENT_FAILED")},
            "originalsBytes":originals,"quarantineBytes":quarantine,"stagingBytes":staging,
            "availableBytes":available,"lastSuccessfulBackupAt":backup,
        })))
    }

    fn status(&self) -> anyhow::Result<Outcome> {
        let device: Option<String> = self
            .tx
            .query_row("SELECT device_id FROM device_registration", [], |r| {
                r.get(0)
            })
            .optional()?;
        let queued: u64 = self.tx.query_row(
            "SELECT count(*) FROM transport_outbox WHERE state='pending'",
            [],
            |r| r.get(0),
        )?;
        let kernel = self.kernel_health()?;
        let mut dependencies: Vec<_> = ["im", "deepseek", "mcp", "mipush"]
            .iter()
            .map(|name| json!({"name":name,"status":"unknown","checkedAt":null,"errorCode":null}))
            .collect();
        for (index, name) in [(1, "deepseek"), (2, "mcp")] {
            if let Some((mut status, at, mut code)) = self
                .tx
                .query_row(
                    "SELECT status,checked_at,error_code FROM provider_health WHERE name=?1",
                    [name],
                    |r| {
                        Ok((
                            r.get::<_, String>(0)?,
                            r.get::<_, u64>(1)?,
                            r.get::<_, Option<String>>(2)?,
                        ))
                    },
                )
                .optional()?
            {
                if name == "mcp" && (self.now < at || self.now - at > 660_000) {
                    status = "unknown".to_owned();
                    code = Some("MCP_PROBE_STALE".to_owned());
                }
                dependencies[index] =
                    json!({"name":name,"status":status,"checkedAt":at,"errorCode":code});
            }
        }
        dependencies
            .push(json!({"name":"kernel","status":kernel.dependency,"checkedAt":kernel.checked_at,"errorCode":kernel.code}));
        Ok(Ok(
            json!({"serverEpoch":self.epoch,"snapshotSeq":self.seq()?,"recoveryMode":self.recovering,
            "analysis":self.analysis()?,"protocol":{"major":2,"minor":0,"supportedCommands":SUPPORTED_COMMANDS},
            "dependencies":dependencies,"queues":{"imPending":queued,"pushPending":0,"fileCleanupPending":0},
            "device":{"deviceId":device,"pushEnabled":self.settings()?["pushEnabled"],"registrationValid":false},
            "versions":{"shell":env!("CARGO_PKG_VERSION"),"kernel":kernel.version}}),
        ))
    }

    fn control(&self) -> anyhow::Result<Outcome> {
        if str_at(self.data, "cmd") == "set_profile" {
            self.tx.execute(
                "UPDATE domain_state SET profile=?1,control_version=control_version+1",
                [str_at(self.data, "profile")],
            )?;
            return Ok(Ok(
                json!({"profile":self.data["profile"],"snapshotSeq":self.bump()?}),
            ));
        }
        crate::reminders::advance(self.tx, self.now, true)?;
        self.tx.execute(
            "UPDATE domain_state SET paused=1,control_version=control_version+1",
            [],
        )?;
        let seq = self.bump()?;
        let version: u64 =
            self.tx
                .query_row("SELECT control_version FROM domain_state", [], |row| {
                    row.get(0)
                })?;
        Ok(Ok(
            json!({"paused":true,"controlVersion":version,"snapshotSeq":seq,"suppressedScanIds":[]}),
        ))
    }

    fn set_settings(&self) -> anyhow::Result<Outcome> {
        let version = int(&self.settings()?, "settingsRevision");
        if self.data["baseSettingsRevision"] != version {
            return Ok(Err((
                "SETTINGS_VERSION_CONFLICT",
                json!({"currentRevision":version}),
            )));
        }
        let modes = json!({"critical":"full_screen","warn":"sound","info":"visual"});
        let time = |s: &str| {
            s.split_once(':').is_some_and(|(h, m)| {
                h.len() == 2
                    && m.len() == 2
                    && h.bytes().all(|b| b.is_ascii_digit())
                    && m.bytes().all(|b| b.is_ascii_digit())
                    && h.parse::<u8>().is_ok_and(|v| v < 24)
                    && m.parse::<u8>().is_ok_and(|v| v < 60)
            })
        };
        if self.data["reminderModes"] != modes
            || !time(str_at(self.data, "nightStart"))
            || !time(str_at(self.data, "nightEnd"))
        {
            return Ok(failure("INVALID_PARAMS"));
        }
        let value = json!({"settingsRevision":version+1,"reminderModes":modes,
            "nightStart":self.data["nightStart"],"nightEnd":self.data["nightEnd"],
            "criticalAtNight":self.data["criticalAtNight"],"pushEnabled":self.data["pushEnabled"]});
        self.tx.execute(
            "UPDATE domain_state SET settings=?1",
            [serde_json::to_string(&value)?],
        )?;
        Ok(Ok(json!({"settings":value,"snapshotSeq":self.bump()?})))
    }

    fn register_push(&self) -> anyhow::Result<Outcome> {
        let reg = str_at(self.data, "regId");
        if reg.trim().is_empty() {
            return Ok(failure("INVALID_PARAMS"));
        }
        self.tx.execute("INSERT INTO device_registration VALUES (1,?1,?2)
            ON CONFLICT(singleton) DO UPDATE SET device_id=excluded.device_id,reg_id=excluded.reg_id",
            params![str_at(self.data,"deviceId"),reg])?;
        Ok(Ok(
            json!({"deviceId":self.data["deviceId"],"pushEnabled":self.settings()?["pushEnabled"],"snapshotSeq":self.bump()?}),
        ))
    }

    fn get_logs(&self) -> anyhow::Result<Outcome> {
        self.page(
            "logs",
            "pageCursor",
            || {
                let mut stmt = self.tx.prepare(
                    "SELECT at,level,code,text FROM diagnostics ORDER BY at DESC,id ASC",
                )?;
                let rows = stmt.query_map([], |r| Ok(json!({"at":r.get::<_,u64>(0)?,
                    "level":r.get::<_,String>(1)?,"code":r.get::<_,String>(2)?,"text":r.get::<_,String>(3)?})))?;
                Ok(rows.collect::<rusqlite::Result<_>>()?)
            },
            json!({}),
            "nextPageCursor",
            true,
        )
    }

    fn get_messages(&self) -> anyhow::Result<Outcome> {
        self.page("messages", "cursor", || self.read_bodies(
            "SELECT body FROM objects WHERE collection='messages' ORDER BY json_extract(body,'$.at') DESC,object_id", [],
        ), json!({}), "nextCursor", false)
    }

    fn page(
        &self,
        query: &str,
        input: &str,
        initial: impl FnOnce() -> anyhow::Result<Vec<Value>>,
        mut other: Value,
        output: &str,
        include_more: bool,
    ) -> anyhow::Result<Outcome> {
        let (items, expires): (Vec<Value>, u64) = if let Some(cursor) = self.data[input].as_str() {
            let row: Option<(String,u64)> = self.tx.query_row("SELECT body,expires_at FROM read_cursors WHERE cursor=?1 AND pair_id=?2 AND epoch=?3 AND query=?4",
                params![cursor,self.pair,self.epoch,query], |r|Ok((r.get(0)?,r.get(1)?))).optional()?;
            let Some((body, until)) = row else {
                return Ok(failure("INVALID_PARAMS"));
            };
            if until <= self.now {
                return Ok(failure("SNAPSHOT_EXPIRED"));
            }
            (serde_json::from_str(&body)?, until)
        } else {
            (initial()?, self.now + TTL)
        };
        let limit = self.data["limit"].as_u64().unwrap_or(20) as usize;
        let mut page = Vec::new();
        let mut bytes = 0;
        for item in items.iter().take(limit) {
            bytes += canonical(item)?.len() + 1;
            if bytes > BUSINESS_LIMIT - 2048 {
                break;
            }
            page.push(item);
        }
        if !items.is_empty() && page.is_empty() {
            return Ok(failure("MESSAGE_TOO_LARGE"));
        }
        let has_more = items.len() > page.len();
        let cursor = if has_more {
            let cursor = id();
            self.tx.execute(
                "INSERT INTO read_cursors VALUES (?1,?2,?3,?4,?5,?6)",
                params![
                    cursor,
                    self.pair,
                    self.epoch,
                    query,
                    expires,
                    serde_json::to_string(&items[page.len()..])?
                ],
            )?;
            json!(cursor)
        } else {
            Value::Null
        };
        other["items"] = json!(page);
        other[output] = cursor;
        if include_more {
            other["hasMore"] = json!(has_more);
        }
        Ok(Ok(other))
    }
}
