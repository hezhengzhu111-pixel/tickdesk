use super::*;

// A cache read or follow-up can carry the original artifact. An archive entry
// belongs to the job that produced it, within the current pair and fence.
const REPORTS: &str = "WITH reports AS (
    SELECT j.*, json_extract(j.raw_output,'$.artifact.report.asOf') AS as_of,
           json_extract(j.raw_output,'$.artifact.report.symbols') AS symbols
      FROM chat_jobs j JOIN peer p ON p.pair_id=j.pair_id AND p.fence=j.peer_fence
     WHERE j.pair_id=?1 AND j.epoch=?2 AND j.state='completed'
       AND json_extract(j.reply,'$.data.status')='completed'
       AND json_extract(j.raw_output,'$.artifact.report.reportId')=j.job_id
       AND json_type(j.raw_output,'$.artifact.report.asOf')='integer'
       AND json_extract(j.raw_output,'$.artifact.report.asOf')>=0
) SELECT job_id,request_id,deadline,text,evidence,raw_output,reply,as_of,
    NOT EXISTS (SELECT 1 FROM reports newer WHERE newer.symbols=r.symbols
                 AND (newer.as_of,newer.request_id)>(r.as_of,r.request_id))
    FROM reports r";

struct ArchivedReport {
    job: ChatJob,
    artifact: Value,
    reply: Value,
    as_of: u64,
    latest: bool,
}

impl ArchivedReport {
    fn from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<StoredReport> {
        let fields = job_from_row(row)?;
        Ok((
            fields,
            row.get::<_, String>(5)?,
            row.get::<_, String>(6)?,
            row.get::<_, u64>(7)?,
            row.get::<_, bool>(8)?,
        ))
    }

    fn parse(row: StoredReport) -> anyhow::Result<Self> {
        let ((job_id, request_id, deadline, text, evidence), artifact, reply, as_of, latest) = row;
        Ok(Self {
            job: ChatJob {
                job_id,
                request_id,
                deadline,
                text,
                evidence: serde_json::from_str(&evidence)?,
            },
            artifact: serde_json::from_str(&artifact)?,
            reply: serde_json::from_str(&reply)?,
            as_of,
            latest,
        })
    }

    fn header(&self, domain: &Domain<'_, '_>) -> anyhow::Result<Value> {
        let header = self.header_value(domain)?;
        contract::validate_definition("ReportHeader", &header)?;
        Ok(header)
    }

    fn header_value(&self, domain: &Domain<'_, '_>) -> anyhow::Result<Value> {
        let report = &self.artifact["artifact"]["report"];
        let context = &self.artifact["artifact"]["context"];
        let until = report["validUntil"].as_u64().unwrap_or(self.as_of);
        let state = if domain.now >= until {
            "expired"
        } else if self.latest
            && !domain.recovering
            && chat_context_current(domain.tx, &self.job, domain.now)?
        {
            "current"
        } else {
            "historical"
        };
        let quality = |name: &str| match context["quality"][name].as_str() {
            Some("partial") => "partial",
            Some("available") => "available",
            _ => "unavailable",
        };
        let summary = quant_summary(report).unwrap_or_else(|| {
            report["body"]["summary"]["text"]
                .as_str()
                .unwrap_or_default()
                .chars()
                .take(550)
                .collect()
        });
        Ok(json!({
            "reportId":self.job.request_id,"symbols":report["symbols"],"asOf":self.as_of,
            "generatedAt":self.reply["ts"],"validUntil":until,"state":state,
            "stance":report["body"]["stance"],
            "summary":summary,
            "origin":if self.job.evidence["scheduled"].is_object() {"automatic"} else {"manual"},
            "coverage":{"business":quality("business"),"quant":quality("quant"),"external":quality("external")},
        }))
    }
}

fn quant_summary(report: &Value) -> Option<String> {
    let mut sections = Vec::new();
    let production = &report["productionQuant"];
    if let Some(candidates) = production["candidates"]
        .as_array()
        .filter(|_| matches!(production["status"].as_str(), Some("succeeded" | "partial")))
    {
        let mut parts = Vec::new();
        for (direction, label) in [("buy", "买入"), ("sell", "卖出"), ("risk", "风险")] {
            let count = candidates
                .iter()
                .filter(|candidate| candidate["direction"] == direction)
                .count();
            if count > 0 {
                parts.push(format!("{label} {count} 项"));
            }
        }
        if !parts.is_empty() {
            sections.push(format!("正式候选 · {}", parts.join(" · ")));
        }
    }
    if let Some(rows) = report["quantOriginal"].as_array().filter(|rows| {
        !rows.is_empty()
            && rows.iter().all(|row| {
                ["entry", "exit"].iter().all(|field| {
                    row[*field]
                        .get("value")
                        .is_some_and(|value| value.is_boolean() || value.is_null())
                }) && row["score"]
                    .get("value")
                    .is_some_and(|value| value.is_string() || value.is_number() || value.is_null())
            })
    }) {
        let mut parts = Vec::new();
        for (field, side) in [("entry", "入场"), ("exit", "退出")] {
            for (value, label) in [
                (Some(true), "触发"),
                (Some(false), "未触发"),
                (None, "待核实"),
            ] {
                let count = rows
                    .iter()
                    .filter(|row| row[field]["value"].as_bool() == value)
                    .count();
                if count > 0 {
                    parts.push(format!("{side}{label} {count} 项"));
                }
            }
        }
        let scored = rows
            .iter()
            .filter(|row| !row["score"]["value"].is_null())
            .count();
        if scored > 0 {
            parts.push(format!("评分已计算 {scored} 项"));
        }
        if scored < rows.len() {
            parts.push(format!("评分待核实 {} 项", rows.len() - scored));
        }
        sections.push(format!("研究观察 · {}", parts.join(" · ")));
    }
    (!sections.is_empty()).then(|| sections.join("\n"))
}

type StoredJob = (String, String, u64, String, String);
type StoredReport = (StoredJob, String, String, u64, bool);

fn read_report(domain: &Domain<'_, '_>, id: &str) -> anyhow::Result<Option<ArchivedReport>> {
    let row = domain
        .tx
        .query_row(
            &format!("{REPORTS} WHERE r.request_id=?3"),
            params![domain.pair, domain.epoch, id],
            ArchivedReport::from_row,
        )
        .optional()?;
    row.map(ArchivedReport::parse).transpose()
}

pub(super) fn publish_manual(
    tx: &rusqlite::Transaction<'_>,
    job: &ChatJob,
    epoch: &str,
    now: u64,
) -> anyhow::Result<()> {
    let pair: Option<String> = tx
        .query_row(
            "SELECT pair_id FROM chat_jobs WHERE job_id=?1
         AND json_extract(raw_output,'$.artifact.report.reportId')=job_id",
            [&job.job_id],
            |row| row.get(0),
        )
        .optional()?;
    let Some(pair) = pair else { return Ok(()) };
    let domain = Domain {
        tx,
        data: &Value::Null,
        pair: &pair,
        epoch,
        recovering: false,
        now,
    };
    let Some(saved) = read_report(&domain, &job.request_id)? else {
        return Ok(());
    };
    let header = saved.header_value(&domain)?;
    if contract::validate_definition("ReportHeader", &header).is_err() {
        tracing::warn!("Skipping invalid manual report header");
        return Ok(());
    }
    enqueue_report(&domain, &job.request_id, header, None)
}

pub(super) fn publish_scheduled(
    domain: &Domain<'_, '_>,
    job: &ChatJob,
    prior: &Value,
) -> anyhow::Result<()> {
    let subscribed: bool = domain.tx.query_row(
        "SELECT EXISTS(SELECT 1 FROM peer p JOIN objects o ON o.object_id=p.pair_id
          WHERE p.pair_id=?1 AND o.collection='analysis_report_subscription'
          AND json_extract(o.body,'$.epoch')=?2 AND json_extract(o.body,'$.fence')=p.fence)",
        params![domain.pair, domain.epoch],
        |row| row.get(0),
    )?;
    if !subscribed {
        return Ok(());
    }
    let Some(saved) = read_report(domain, &job.request_id)? else {
        return Ok(());
    };
    let header = saved.header(domain)?;
    if header["state"] != "current" {
        return Ok(());
    }
    let report = &saved.artifact["artifact"]["report"]["body"];
    let notify_until = if material_risk_update(report, prior) {
        Some(
            domain
                .now
                .saturating_add(60_000)
                .min(header["validUntil"].as_u64().unwrap_or(domain.now)),
        )
    } else {
        None
    };
    enqueue_report(domain, &job.request_id, header, notify_until)
}

fn enqueue_report(
    domain: &Domain<'_, '_>,
    request_id: &str,
    header: Value,
    notify_until: Option<u64>,
) -> anyhow::Result<()> {
    let event_id = Uuid::new_v4().to_string();
    let seq = domain.bump()?;
    commands::enqueue_current(
        domain.tx,
        &json!({
            "v":2,"kind":"analysis_report","id":event_id,"dedupeKey":format!("report:{request_id}"),
            "ts":domain.now,"serverEpoch":domain.epoch,"snapshotSeq":seq,
            "data":{"header":header,"notifyUntil":notify_until},
        }),
    )?;
    Ok(())
}

fn material_risk_update(report: &Value, prior: &Value) -> bool {
    let stance = report["stance"].as_str();
    let refs = report["summary"]["sourceRefs"].as_array();
    if !matches!(stance, Some("negative" | "restricted")) || refs.is_none_or(Vec::is_empty) {
        return false;
    }
    if stance != prior["stance"].as_str() {
        return true;
    }
    let previous = prior["summary"]["sourceRefs"].as_array();
    refs.into_iter().flatten().any(|id| {
        id.as_str().is_some_and(|id| id.starts_with("fact-"))
            && previous.is_none_or(|list| !list.contains(id))
    })
}

pub(crate) fn command(domain: &Domain<'_, '_>) -> anyhow::Result<Outcome> {
    if let Some(id) = domain.data["reportId"].as_str() {
        let Some(saved) = read_report(domain, id)? else {
            return Ok(failure("AI_REPORT_UNAVAILABLE"));
        };
        let header = saved.header(domain)?;
        let mut reply = saved.reply["data"].clone();
        let detail = |reply: &Value| json!({"header":header,"text":reply["text"],"sources":reply["sources"]});
        let result = detail(&reply);
        if reply["text"]
            .as_str()
            .is_some_and(|text| text.chars().count() > 24_000)
            || contract::canonical(&result)?.len() > contract::BUSINESS_LIMIT - 2048
        {
            excerpt_reply(
                &mut reply,
                &saved.job.job_id,
                saved.reply["ts"].as_u64().unwrap_or(domain.now),
            );
            return Ok(Ok(detail(&reply)));
        }
        return Ok(Ok(result));
    }
    let symbol = domain.data["symbol"].as_str();
    let cursor = domain.data["cursor"].as_str();
    let parse_cursor = |cursor: &str| {
        let (filter, key) = cursor.split_once(':')?;
        if filter != symbol.unwrap_or("") {
            return None;
        }
        let (as_of, id) = key.split_once(':')?;
        let as_of: u64 = as_of.parse().ok()?;
        commands::valid_id(id).then(|| (as_of, id.to_owned()))
    };
    let boundary = cursor.and_then(parse_cursor);
    if cursor.is_some() && boundary.is_none() {
        return Ok(failure("INVALID_PARAMS"));
    }
    let (as_of, id) = boundary.unzip();
    let limit = domain.data["limit"].as_u64().unwrap_or(20) as usize;
    let mut sql = domain.tx.prepare(&format!(
        "{REPORTS}
        WHERE (?3 IS NULL OR EXISTS(SELECT 1 FROM json_each(r.symbols) WHERE value=?3))
          AND (?4 IS NULL OR (r.as_of,r.request_id)<(?4,?5))
        ORDER BY r.as_of DESC,r.request_id DESC LIMIT ?6"
    ))?;
    let records = sql
        .query_map(
            params![domain.pair, domain.epoch, symbol, as_of, id, limit + 1],
            ArchivedReport::from_row,
        )?
        .collect::<rusqlite::Result<Vec<StoredReport>>>()?;
    let next = if records.len() > limit {
        let (_, request_id, _, _, _) = &records[limit - 1].0;
        Some(format!(
            "{}:{}:{request_id}",
            symbol.unwrap_or(""),
            records[limit - 1].3
        ))
    } else {
        None
    };
    let items = records
        .into_iter()
        .take(limit)
        .map(|row| ArchivedReport::parse(row)?.header(domain))
        .collect::<anyhow::Result<Vec<_>>>()?;
    if domain.data["subscribe"] == true && cursor.is_none() && !domain.recovering {
        let fence: i64 = domain.tx.query_row(
            "SELECT fence FROM peer WHERE pair_id=?1",
            [domain.pair],
            |r| r.get(0),
        )?;
        domain.put(
            "analysis_report_subscription",
            domain.pair,
            &json!({"epoch":domain.epoch,"fence":fence}),
        )?;
    }
    Ok(Ok(json!({"items":items,"nextCursor":next})))
}
