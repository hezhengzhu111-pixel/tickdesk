use crate::{contract::validate_definition, domain::Domain, market::decimal_cmp, storage::Storage};
use anyhow::{ensure, Context};
use rusqlite::OptionalExtension;
use serde::Deserialize;
use serde_json::{json, Map, Value};
use std::{cmp::Ordering, collections::BTreeSet};

pub(crate) const VERSION: u64 = 3;

/// Public projection deliberately excludes evidence, quotes and internal event/holding data.
pub(crate) fn read(d: &Domain<'_, '_>) -> anyhow::Result<crate::commands::Outcome> {
    let requested = d.data["scanId"].as_str();
    let latest = d.get("market_meta", "latest_scan")?.unwrap_or(Value::Null);
    let key = requested.or_else(|| latest["scanId"].as_str());
    let stored = key
        .map(|key| d.get("market_quant_observations", key))
        .transpose()?
        .flatten()
        .filter(|row| row["serverEpoch"] == d.epoch && !d.recovering);
    let mut body = json!({"version":VERSION,"scanId":key,"decisionCutoffMs":null,
        "status":"missing","error":null,"observations":[],
        "historical":requested.is_some_and(|key| latest["scanId"] != key)});
    if let Some(row) = stored {
        body["status"] = row["status"].clone();
        body["error"] = row["error"].clone();
        body["decisionCutoffMs"] = row["batch"]["decisionCutoffMs"].clone();
        if let Some(observations) = row["batch"]["observations"].as_array() {
            let mut rows = Vec::new();
            for item in observations {
                let summary: Value = [
                    "symbol",
                    "strategyId",
                    "group",
                    "timeframe",
                    "admission",
                    "bar",
                    "entry",
                    "exit",
                    "score",
                ]
                .iter()
                .map(|key| ((*key).to_owned(), item[*key].clone()))
                .collect();
                validate_definition("QuantObservationV3", &summary)?;
                rows.push(summary);
            }
            body["observations"] = json!(rows);
        }
    }
    crate::market::query_snapshot(
        d,
        &format!("quant:{}", requested.unwrap_or("latest")),
        body,
        "cursor",
        "nextCursor",
        "observations",
    )
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct Batch {
    version: u64,
    decision_cutoff_ms: u64,
    observations: Vec<Observation>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct Observation {
    symbol: String,
    strategy_id: String,
    group: String,
    timeframe: String,
    code_hash: String,
    parameters_hash: String,
    dependencies_hash: String,
    input_hash: String,
    rules_hash: Option<String>,
    admission: String,
    bar: Bar,
    entry: Verdict,
    exit: Verdict,
    score: Score,
    quote: Option<Quote>,
    #[serde(rename = "evidence")]
    _evidence: Map<String, Value>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct Bar {
    status: String,
    end_ms: Option<u64>,
    available_at_ms: Option<u64>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Verdict {
    value: Option<bool>,
    reason: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Score {
    value: Option<String>,
    reason: Option<String>,
    scope: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct Quote {
    price: String,
    as_of: u64,
    source: Value,
}

fn validate(raw: &Value, cutoff: u64) -> anyhow::Result<()> {
    let batch = Batch::deserialize(raw).context("observation batch shape")?;
    ensure!(
        batch.version == VERSION && batch.decision_cutoff_ms == cutoff,
        "observation binding"
    );
    ensure!(batch.observations.len() <= 500, "observation count");
    let mut keys = BTreeSet::new();
    for row in &batch.observations {
        validate_definition("Symbol", &json!(row.symbol))?;
        validate_definition("Id", &json!(row.strategy_id))?;
        ensure!(
            keys.insert((&row.symbol, &row.strategy_id, &row.timeframe)),
            "duplicate observation"
        );
        ensure!(
            matches!(
                row.group.as_str(),
                "daily_swing" | "short_term" | "long_term"
            ),
            "strategy group"
        );
        ensure!(
            matches!(row.timeframe.as_str(), "1d" | "1m" | "5m" | "1w" | "1mo"),
            "decision timeframe"
        );
        ensure!(
            matches!(
                row.admission.as_str(),
                "research" | "causal_checked" | "oos_checked" | "shadow"
            ),
            "admission"
        );
        let required = [
            &row.code_hash,
            &row.parameters_hash,
            &row.dependencies_hash,
            &row.input_hash,
        ];
        ensure!(
            required
                .into_iter()
                .chain(row.rules_hash.iter())
                .all(|hash| {
                    hash.len() == 64
                        && hash
                            .bytes()
                            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
                }),
            "observation digest"
        );

        let any_value =
            row.entry.value.is_some() || row.exit.value.is_some() || row.score.value.is_some();
        match (
            row.bar.status.as_str(),
            row.bar.end_ms,
            row.bar.available_at_ms,
        ) {
            ("complete", Some(end), Some(available)) if end <= available && available <= cutoff => {
            }
            ("unknown", end, None) if !any_value && end.is_none_or(|end| end <= cutoff) => {}
            _ => anyhow::bail!("decision bar unavailable at cutoff"),
        }
        validate_reason(row.entry.value.is_some(), row.entry.reason.as_deref())?;
        validate_reason(row.exit.value.is_some(), row.exit.reason.as_deref())?;
        validate_reason(row.score.value.is_some(), row.score.reason.as_deref())?;
        ensure!(
            matches!(
                row.score.scope.as_str(),
                "entry_candidates" | "eligible_universe"
            ),
            "scoring scope"
        );
        if let Some(score) = &row.score.value {
            validate_definition("Decimal", &json!(score))?;
            ensure!(
                !score.starts_with('-') && decimal_cmp(score, "100") != Ordering::Greater,
                "score bounds"
            );
        }
        if let Some(quote) = &row.quote {
            validate_definition("Decimal", &json!(quote.price))?;
            validate_definition("Source", &quote.source)?;
            ensure!(
                !quote.price.starts_with('-')
                    && decimal_cmp(&quote.price, "0") == Ordering::Greater,
                "quote bounds"
            );
            ensure!(
                quote.as_of <= cutoff && quote.source["asOf"] == quote.as_of,
                "quote time binding"
            );
        }
    }
    Ok(())
}

fn validate_reason(available: bool, reason: Option<&str>) -> anyhow::Result<()> {
    ensure!(
        if available {
            reason.is_none()
        } else {
            reason.is_some_and(|r| !r.trim().is_empty() && r.len() <= 256)
        },
        "observation availability"
    );
    Ok(())
}

fn advance_events(d: &Domain<'_, '_>, scan_id: &str, batch: &Value) -> anyhow::Result<Vec<Value>> {
    let mut events = Vec::new();
    for row in batch["observations"]
        .as_array()
        .expect("validated observations")
    {
        for phase in ["entry", "exit"] {
            let key = crate::contract::digest(&json!([
                d.epoch,
                row["symbol"],
                row["strategyId"],
                row["group"],
                row["timeframe"],
                row["codeHash"],
                row["parametersHash"],
                row["dependenciesHash"],
                phase
            ]))?;
            let head = d.get("quant_event_heads", &key)?.unwrap_or(Value::Null);
            let prior = head["eventId"].as_str();
            let mut event = prior
                .map(|id| d.get("quant_events", id))
                .transpose()?
                .flatten();
            let active = event
                .as_ref()
                .is_some_and(|event| event["state"] == "active");
            let closed_at = event
                .as_ref()
                .and_then(|event| event["holdingClosedAt"].as_u64());
            let reopened = if let (Some(at), Some(symbol)) = (closed_at, row["symbol"].as_str()) {
                d.get("positions", symbol)?.is_some_and(|position| {
                    position["qty"].as_u64().is_some_and(|qty| qty > 0)
                        && position["asOf"].as_u64().is_some_and(|as_of| as_of >= at)
                })
            } else {
                false
            };
            let latest_bar = head["latestBarEndMs"]
                .as_u64()
                .or_else(|| head["lastDecisionEndMs"].as_u64());
            let complete_bar = row["bar"]["endMs"]
                .as_u64()
                .filter(|_| row["bar"]["status"] == "complete");
            let decision =
                complete_bar
                    .zip(row[phase]["value"].as_bool())
                    .filter(|(end, matched)| {
                        latest_bar.is_none_or(|latest| *end >= latest)
                            && (closed_at.is_none() || reopened)
                            && (head["lastDecisionEndMs"]
                                .as_u64()
                                .is_none_or(|last| *end > last)
                                || (reopened && *matched))
                    });
            let Some((end, matched)) = decision else {
                if complete_bar.is_some_and(|end| latest_bar.is_none_or(|latest| end > latest)) {
                    d.put(
                        "quant_event_heads",
                        &key,
                        &json!({"lastDecisionEndMs":head["lastDecisionEndMs"],
                        "latestBarEndMs":complete_bar,"eventId":head["eventId"]}),
                    )?;
                }
                if let Some(event) = event.filter(|_| active || (closed_at.is_some() && !reopened))
                {
                    events.push(event_view(d, event, row, phase)?);
                }
                continue;
            };

            let mut event_id = head["eventId"].clone();
            if matched && !active {
                let id = crate::domain::id();
                let next = json!({"eventId":id,"previousEventId":prior,"state":"active","phase":phase,
                    "serverEpoch":d.epoch,
                    "symbol":row["symbol"],"strategyId":row["strategyId"],"group":row["group"],"timeframe":row["timeframe"],
                    "startedScanId":scan_id,"startedBarEndMs":end,"resolvedScanId":null,"resolvedBarEndMs":null});
                d.put("quant_events", &id, &next)?;
                event_id = json!(id);
                event = Some(next);
            } else if let Some(existing) = event.as_mut().filter(|_| active && !matched) {
                existing["state"] = json!("resolved");
                existing["resolvedScanId"] = json!(scan_id);
                existing["resolvedBarEndMs"] = json!(end);
                d.put("quant_events", prior.expect("active event ID"), existing)?;
            }
            d.put(
                "quant_event_heads",
                &key,
                &json!({"lastDecisionEndMs":end,"latestBarEndMs":end,"eventId":event_id}),
            )?;
            if let Some(event) = event.filter(|_| matched || active) {
                events.push(event_view(d, event, row, phase)?);
            }
        }
    }
    Ok(events)
}

fn event_view(
    d: &Domain<'_, '_>,
    mut event: Value,
    row: &Value,
    phase: &str,
) -> anyhow::Result<Value> {
    if phase != "exit" || (event["state"] != "active" && event["holdingClosedAt"].is_null()) {
        return Ok(event);
    }
    let symbol = row["symbol"].as_str().expect("validated symbol");
    let holding = d.holding_details_for(symbol)?;
    if holding.is_null() {
        return Ok(event);
    }
    let mut allocated = None;
    let mut sellable = None;
    let state = if holding["qty"] == 0 {
        allocated = Some(0);
        sellable = Some(0);
        "closed"
    } else if holding["reconciliationRequired"] == true {
        "pending_reconciliation"
    } else if let Some(partitions) = holding["partitions"].as_array() {
        let matching = partitions.iter().filter(|part| {
            let origin = &part["origin"];
            [
                "strategyId",
                "group",
                "codeHash",
                "parametersHash",
                "dependenciesHash",
            ]
            .iter()
            .all(|field| origin[*field] == row[*field])
        });
        let known = matching
            .clone()
            .filter_map(|part| part["qty"].as_u64())
            .sum();
        if known == 0 {
            "origin_unverified"
        } else {
            allocated = Some(known);
            sellable = holding["availableQty"].as_u64().map(|total| {
                let usable = matching
                    .filter(|part| part["restrictions"].as_array().is_some_and(Vec::is_empty))
                    .filter_map(|part| part["qty"].as_u64())
                    .sum::<u64>();
                usable.min(total)
            });
            if sellable.is_none_or(|qty| qty < known) {
                "exit_restricted"
            } else {
                "exit_observed"
            }
        }
    } else {
        "origin_unverified"
    };
    event["holdingExit"] = json!({"state":state,"allocatedQty":allocated,"allocatedSellableQty":sellable,
        "plannedQty":null,"positionRevision":holding["positionRevision"],"sellableBasis":holding["sellableBasis"],
        "admission":row["admission"]});
    Ok(event)
}

pub(crate) fn close_holding_events(
    d: &Domain<'_, '_>,
    symbol: &str,
    at: u64,
) -> anyhow::Result<()> {
    d.tx.execute(
        "UPDATE objects SET body=json_set(body,'$.state','resolved','$.holdingClosedAt',?3)
         WHERE collection='quant_events' AND json_extract(body,'$.symbol')=?1
         AND json_extract(body,'$.serverEpoch')=?2 AND json_extract(body,'$.phase')='exit'
         AND json_extract(body,'$.state')='active'",
        rusqlite::params![symbol, d.epoch, at],
    )?;
    d.tx.execute(
        "UPDATE objects SET body=json_set(body,'$.state','resolved','$.resolvedAt',?3,
            '$.resolution','holding_closed')
         WHERE collection='formal_exit_events' AND json_extract(body,'$.symbol')=?1
         AND json_extract(body,'$.serverEpoch')=?2 AND json_extract(body,'$.state')='active'",
        rusqlite::params![symbol, d.epoch, at],
    )?;
    Ok(())
}

pub(crate) fn persist(
    d: &Domain<'_, '_>,
    scan_id: &str,
    request: &Value,
    reply: &Value,
) -> anyhow::Result<BTreeSet<String>> {
    let (Some(cutoff), Some(token)) = (
        request["requestedAt"].as_u64(),
        request["scanLease"].as_str(),
    ) else {
        return Ok(BTreeSet::new());
    };
    if request["quantObservationVersion"] != VERSION || request["leaseAcquired"] != true {
        return Ok(BTreeSet::new());
    }
    let live = d.get("market_meta", "scan_lease")?.is_some_and(|lease| {
        lease["token"] == token
            && lease["epoch"] == d.epoch
            && lease["expiresAt"].as_u64().is_some_and(|at| at >= d.now)
    });
    if !live {
        return Ok(BTreeSet::new());
    }
    let rule_updates = reply
        .get("holdingRuleBatch")
        .map(|rules| d.persist_holding_rules(rules, cutoff, &request["objects"]))
        .transpose()?
        .unwrap_or_default();
    if let Some(raw) = reply
        .get("quantObservationBatch")
        .filter(|raw| validate(raw, cutoff).is_ok())
    {
        let events = advance_events(d, scan_id, raw)?;
        d.put(
            "market_quant_observations",
            scan_id,
            &json!({"scanId":scan_id,"serverEpoch":d.epoch,"status":"available","error":null,"batch":raw,"events":events}),
        )?;
    } else if reply["quantObservationError"] == "DATA_UNAVAILABLE" {
        d.put(
            "market_quant_observations",
            scan_id,
            &json!({"scanId":scan_id,"serverEpoch":d.epoch,
            "status":"failed","error":"DATA_UNAVAILABLE","batch":null,"events":[]}),
        )?;
    }
    Ok(rule_updates)
}

pub(crate) fn record_analysis_scan(
    d: &Domain<'_, '_>,
    scan_id: &str,
    preflight_rejection: bool,
) -> anyhow::Result<()> {
    let scan = if preflight_rejection {
        if d.get("market_meta", "latest_analysis_scan")?
            .is_some_and(|head| head["serverEpoch"] == d.epoch)
        {
            return Ok(());
        }
        // Preserve the previous attempt on first use after an upgrade. A
        // missing or failed previous attempt stays missing or failed.
        d.get("market_meta", "latest_scan")?.unwrap_or(Value::Null)["scanId"].clone()
    } else {
        json!(scan_id)
    };
    d.put(
        "market_meta",
        "latest_analysis_scan",
        &json!({"scanId":scan,"serverEpoch":d.epoch}),
    )
}

pub(crate) fn analysis_observations(
    d: &Domain<'_, '_>,
    symbol: Option<&str>,
) -> anyhow::Result<Option<Value>> {
    if d.recovering {
        return Ok(None);
    }
    let head = match d.get("market_meta", "latest_analysis_scan")? {
        Some(head) if head["serverEpoch"] == d.epoch => Some(head),
        _ => d.get("market_meta", "latest_scan")?,
    };
    let Some(head) = head else { return Ok(None) };
    let Some(scan_id) = head["scanId"].as_str() else {
        return Ok(None);
    };
    let observations = d
        .get("market_quant_observations", scan_id)?
        .filter(|saved| saved["serverEpoch"] == d.epoch);
    let Some(record) = d.get("market_scans", scan_id)?.filter(|saved| {
        saved["serverEpoch"]
            .as_str()
            .or_else(|| saved["evidence"]["request"]["epoch"].as_str())
            == Some(d.epoch)
            && saved["scan"]["finishedAt"]
                .as_u64()
                .is_some_and(|at| at <= d.now)
    }) else {
        return Ok(observations);
    };
    let scan = &record["scan"];
    let mut candidates = Vec::new();
    if matches!(scan["status"].as_str(), Some("succeeded" | "partial")) {
        for id in scan["publishedTaskIds"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
        {
            let Some(task) = d.get("tasks", id)?.filter(|row| {
                row["scanId"] == scan_id
                    && row["riskEventKey"].is_null()
                    && row["score"].is_string()
                    && symbol.is_none_or(|symbol| row["symbol"] == symbol)
            }) else {
                continue;
            };
            candidates.push(
                json!({"taskId":id,"symbol":task["symbol"],"direction":task["direction"],
                "score":task["score"],"strategies":task["strategies"],"dataAsOf":task["dataAsOf"]}),
            );
            if candidates.len() == 10 {
                break;
            }
        }
    }
    let reason = record["evidence"]["response"]["productionReason"]
        .as_str()
        .filter(|reason| {
            matches!(
                *reason,
                "NO_PRODUCTION_APPROVAL"
                    | "PRODUCTION_EVALUATION_FAILED"
                    | "TRADING_CALENDAR_UNAVAILABLE"
            )
        })
        .or_else(|| scan["error"]["code"].as_str());
    let mut combined = observations.unwrap_or_else(|| {
        json!({
            "scanId":scan_id,"serverEpoch":d.epoch,"status":"missing","error":null,"batch":null,
        })
    });
    combined["production"] = json!({"scanId":scan_id,"status":scan["status"],"reason":reason,
        "finishedAt":scan["finishedAt"],"candidates":candidates});
    Ok(Some(combined))
}

impl Storage {
    pub(crate) fn quant_observations(&self, scan_id: &str) -> anyhow::Result<Option<Value>> {
        let row: Option<String> = self.connection.query_row(
            "SELECT body FROM objects WHERE collection='market_quant_observations' AND object_id=?1",
            [scan_id], |row| row.get(0),
        ).optional()?;
        row.map(|body| Ok(serde_json::from_str(&body)?)).transpose()
    }
}
