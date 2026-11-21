use super::*;
use serde::Deserialize;

const DAILY_LIMIT: u64 = 24;
const RISK_RESERVE: u64 = 4;
const VALID_MS: u64 = 30 * 60_000;
const MINUTE_MS: u64 = 60_000;
const SHANGHAI_OFFSET_MS: u64 = 8 * 60 * MINUTE_MS;
const DAY_MS: u64 = 24 * 60 * MINUTE_MS;
const CUTOFF_TTL: u64 = 12_000;

#[derive(Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub(crate) struct Request {
    version: u8,
    server_epoch: String,
    as_of: u64,
    stamp: String,
    fingerprint: String,
    trigger: String,
    risk_versions: Vec<String>,
}

impl Request {
    fn valid(&self, now: u64, epoch: &str) -> bool {
        let sha = |s: &str| {
            s.len() == 64
                && s.bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        };
        self.version == 1
            && self.server_epoch == epoch
            && self.as_of <= now
            && now - self.as_of <= CUTOFF_TTL
            && matches!(
                self.trigger.as_str(),
                "initial" | "changed" | "risk" | "expiry"
            )
            && sha(&self.stamp)
            && sha(&self.fingerprint)
            && self.risk_versions.len() <= 12
            && self
                .risk_versions
                .iter()
                .all(|id| id.strip_prefix("fact-").is_some_and(sha))
    }
}

fn peer(tx: &rusqlite::Transaction<'_>, symbol: &str) -> anyhow::Result<Option<(String, i64)>> {
    Ok(tx.query_row(
        "SELECT pair_id,fence FROM peer WHERE singleton=1 AND NOT EXISTS(SELECT 1 FROM recovery)
         AND EXISTS(SELECT 1 FROM objects WHERE collection IN ('positions','watchlist','tasks')
            AND (collection!='tasks' OR json_extract(body,'$.status') IN ('pending','acted','snoozed'))
            AND json_extract(body,'$.symbol')=?1)",
        [symbol], |row| Ok((row.get(0)?, row.get(1)?)),
    ).optional()?)
}

fn stamp(evidence: &Value) -> anyhow::Result<String> {
    contract::digest(&json!({
        "epoch":evidence["serverEpoch"],"business":evidence["business"],
        "quant":decision_state(&evidence["quant"]),
    }))
}

fn latest(domain: &Domain<'_, '_>, symbol: &str, fence: i64) -> anyhow::Result<Option<Value>> {
    let row = domain.tx.query_row(
        "SELECT j.state,j.accepted_at,j.evidence,j.reply FROM objects o
         JOIN chat_jobs j ON j.job_id=json_extract(o.body,'$.jobId') WHERE o.collection='ai_schedule_head'
         AND o.object_id=?1 AND j.pair_id=?2 AND j.epoch=?3 AND j.peer_fence=?4",
        params![symbol,domain.pair,domain.epoch,fence],
        |row| Ok((row.get::<_,String>(0)?,row.get::<_,u64>(1)?,row.get::<_,String>(2)?,row.get::<_,Option<String>>(3)?)),
    ).optional()?;
    row.map(|(state, accepted, evidence, reply)| {
        let evidence: Value = serde_json::from_str(&evidence)?;
        let reply: Value = reply.map(|s| serde_json::from_str(&s)).transpose()?.unwrap_or(Value::Null);
        let scheduled = &evidence["scheduled"];
        Ok(json!({
            "state":state,"acceptedAt":accepted,"validUntil":evidence["asOf"].as_u64().unwrap_or(0)+VALID_MS,
            "fingerprint":scheduled["fingerprint"],"riskVersions":scheduled["riskVersions"],
            "errorCode":if state=="quarantined" {json!("AI_QUARANTINED")} else {reply["data"]["errorCode"].clone()},
        }))
    }).transpose()
}

impl Storage {
    pub(crate) fn analysis_snapshot(
        &mut self,
        symbol: &str,
        now: u64,
    ) -> anyhow::Result<Option<Value>> {
        let tx = self.connection.transaction()?;
        let Some((pair, fence)) = peer(&tx, symbol)? else {
            return Ok(None);
        };
        let domain = Domain {
            tx: &tx,
            data: &Value::Null,
            pair: &pair,
            epoch: &self.identity.server_epoch,
            recovering: false,
            now,
        };
        let evidence = capture_evidence(&domain, "schedule", symbol, Some(symbol))?;
        let version = stamp(&evidence)?;
        let last = latest(&domain, symbol, fence)?;
        Ok(Some(
            json!({"version":1,"evidence":evidence,"stamp":version,"latest":last}),
        ))
    }

    pub(crate) fn queue_analysis(
        &mut self,
        symbol: &str,
        request: Request,
        now: u64,
    ) -> anyhow::Result<Option<Value>> {
        if !request.valid(now, &self.identity.server_epoch) {
            return Ok(None);
        }
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let Some((pair, fence)) = peer(&tx, symbol)? else {
            return Ok(None);
        };
        let domain = Domain {
            tx: &tx,
            data: &Value::Null,
            pair: &pair,
            epoch: &self.identity.server_epoch,
            recovering: false,
            now,
        };
        let head = latest(&domain, symbol, fence)?;
        if head.as_ref().is_some_and(|head| {
            matches!(head["state"].as_str(), Some("pending" | "running"))
                || (head["fingerprint"] == request.fingerprint
                    && (!head["errorCode"].is_null()
                        || head["validUntil"].as_u64().unwrap_or(0) > now))
        }) {
            return Ok(Some(json!({"version":1,"status":"deduplicated"})));
        }
        let day_start =
            ((now + SHANGHAI_OFFSET_MS) / DAY_MS * DAY_MS).saturating_sub(SHANGHAI_OFFSET_MS);
        let risk = request.trigger == "risk";
        let interval = if risk { 15_000 } else { MINUTE_MS };
        let (daily, recent, running) = tx.query_row(
            "SELECT count(*),
                coalesce(sum(accepted_at>?2 AND (json_extract(evidence,'$.scheduled.trigger')='risk')=?4),0),
                coalesce(sum(state IN ('pending','running') AND (json_extract(evidence,'$.scheduled.trigger')='risk')=?4),0)
               FROM chat_jobs WHERE epoch=?3 AND accepted_at>=?1 AND json_extract(evidence,'$.scheduled') IS NOT NULL",
            params![day_start, now.saturating_sub(interval), domain.epoch,risk],
            |row| Ok((row.get::<_,u64>(0)?,row.get::<_,u64>(1)?,row.get::<_,u64>(2)?)),
        )?;
        let cap = if risk {
            DAILY_LIMIT
        } else {
            DAILY_LIMIT - RISK_RESERVE
        };
        if daily >= cap || recent >= 1 || running >= 1 {
            return Ok(Some(
                json!({"version":1,"status":"budget_or_busy","daily":daily,"dailyLimit":DAILY_LIMIT}),
            ));
        }
        let text = format!("/分析 {symbol}");
        let id = Uuid::new_v4().to_string();
        let capture = Domain {
            now: request.as_of,
            ..domain
        };
        let mut evidence = capture_evidence(&capture, &id, &text, Some(symbol))?;
        if evidence["business"]["paused"] == true || stamp(&evidence)? != request.stamp {
            return Ok(None);
        }
        if evidence["quant"]["batch"]["decisionCutoffMs"]
            .as_u64()
            .is_some_and(|cutoff| cutoff > request.as_of)
        {
            return Ok(None);
        }
        evidence["asOf"] = json!(request.as_of);
        evidence["scheduled"] = json!({
            "symbol":symbol,"fingerprint":request.fingerprint,"riskVersions":request.risk_versions,"trigger":request.trigger
        });
        tx.execute(
            "INSERT INTO chat_jobs(job_id,request_id,pair_id,epoch,peer_fence,state,accepted_at,deadline,text,evidence,prompt_version,request_hash)
             VALUES (?1,?1,?2,?3,?4,'pending',?5,?6,?7,?8,'tickdesk-assistant-v1',?9)",
            params![id,pair,domain.epoch,fence,now,now+DEADLINE,text,evidence.to_string(),request.fingerprint],
        )?;
        domain.put("ai_schedule_head", symbol, &json!({"jobId":id}))?;
        tx.commit()?;
        Ok(Some(json!({"version":1,"status":"queued","jobId":id})))
    }

    pub(crate) fn latest_analysis(
        &mut self,
        symbol: &str,
        now: u64,
    ) -> anyhow::Result<Option<Value>> {
        let tx = self.connection.transaction()?;
        let Some((pair, fence)) = peer(&tx, symbol)? else {
            return Ok(None);
        };
        let row = tx.query_row(
            "SELECT j.job_id,j.request_id,j.deadline,j.text,j.evidence,j.raw_output,j.reply FROM objects o
               JOIN chat_jobs j ON j.job_id=json_extract(o.body,'$.jobId')
              WHERE o.collection='ai_report_head' AND o.object_id=?1
                AND j.pair_id=?2 AND j.epoch=?3 AND j.peer_fence=?4 AND j.state='completed'",
            params![symbol,pair,self.identity.server_epoch,fence],
            |row| Ok((job_from_row(row)?,row.get::<_,String>(5)?,row.get::<_,String>(6)?)),
        ).optional()?;
        row.map(
            |((job_id, request_id, deadline, text, evidence), raw, reply)| {
                let job = ChatJob {
                    job_id,
                    request_id,
                    deadline,
                    text,
                    evidence: serde_json::from_str(&evidence)?,
                };
                let raw: Value = serde_json::from_str(&raw)?;
                let reply: Value = serde_json::from_str(&reply)?;
                let report = &raw["artifact"]["report"];
                let expired = report["validUntil"]
                    .as_u64()
                    .is_none_or(|until| now >= until);
                let state = if expired {
                    "expired"
                } else if chat_context_current(&tx, &job, now)? {
                    "available"
                } else {
                    "historical"
                };
                Ok(
                    json!({"version":1,"serverEpoch":self.identity.server_epoch,"state":state,
                      "artifact":raw["artifact"],"reply":reply["data"]}),
                )
            },
        )
        .transpose()
    }
}

pub(super) fn complete(
    tx: &rusqlite::Transaction<'_>,
    job: &ChatJob,
    result: &Value,
    raw: Option<&str>,
) -> anyhow::Result<()> {
    tx.execute(
        "UPDATE chat_jobs SET state='completed',raw_output=?1,reply=?2 WHERE job_id=?3",
        params![raw, result.to_string(), job.job_id],
    )?;
    if result["data"]["status"] == "completed" {
        let symbol = str_at(&job.evidence["scheduled"], "symbol");
        let previous: Option<String> = tx
            .query_row(
                "SELECT j.raw_output FROM objects o
              JOIN chat_jobs j ON j.job_id=json_extract(o.body,'$.jobId')
              JOIN peer p ON p.pair_id=j.pair_id AND p.fence=j.peer_fence
             WHERE o.collection='ai_report_head' AND o.object_id=?1 AND j.epoch=?2",
                params![symbol, job.evidence["serverEpoch"].as_str()],
                |row| row.get(0),
            )
            .optional()?;
        let previous: Value = previous
            .map(|s| serde_json::from_str(&s))
            .transpose()?
            .unwrap_or(Value::Null);
        let body = json!({"jobId":job.job_id}).to_string();
        tx.execute(
            "INSERT INTO objects(collection,object_id,body) VALUES ('ai_report_head',?1,?2)
             ON CONFLICT(collection,object_id) DO UPDATE SET body=excluded.body",
            params![symbol, body],
        )?;
        let pair: String =
            tx.query_row("SELECT pair_id FROM peer WHERE singleton=1", [], |row| {
                row.get(0)
            })?;
        let domain = Domain {
            tx,
            data: &Value::Null,
            pair: &pair,
            epoch: str_at(&job.evidence, "serverEpoch"),
            recovering: false,
            now: result["ts"].as_u64().unwrap_or(0),
        };
        super::reports::publish_scheduled(&domain, job, &previous["artifact"]["report"]["body"])?;
    }
    Ok(())
}
