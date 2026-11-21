//! Market inputs cross this boundary only after bounded I/O, before a write transaction.
mod history;
mod quotes;
mod risks;

pub(crate) use history::read as read_history_coverage;

use crate::{
    commands::{failure, Outcome},
    contract::validate_definition,
    domain::{id, Domain},
    kernel::KernelConfig,
    storage::Storage,
};
use anyhow::{ensure, Context};
use chrono::{DateTime, Datelike, Timelike, Utc};
use rusqlite::{params, OptionalExtension};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    cmp::Ordering,
    collections::{BTreeMap, BTreeSet},
    future::Future,
    sync::{Arc, Mutex},
    time::Duration,
};

const SCAN_INTERVAL_MS: u64 = 300_000;
const HOLDING_RULE_VERSION: u64 = 3;

pub fn needs_preparation(data: &Value) -> bool {
    matches!(
        data["cmd"].as_str(),
        Some("watch" | "search_symbols" | "get_chart" | "get_quote_status" | "scan_now" | "resume")
    ) && data["pageCursor"].is_null()
}

pub fn capture(db: &Storage, data: &Value, now: u64) -> anyhow::Result<Value> {
    let (paused, control, profile, position): (bool, u64, String, u64) = db.connection.query_row(
        "SELECT paused,control_version,profile,position_revision FROM domain_state",
        [],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
    )?;
    let mut stmt = db.connection.prepare("SELECT body FROM objects WHERE collection IN ('positions','watchlist') ORDER BY collection,object_id")?;
    let objects = stmt
        .query_map([], |r| r.get::<_, String>(0))?
        .map(|r| Ok(serde_json::from_str::<Value>(&r?)?))
        .collect::<anyhow::Result<Vec<_>>>()?;
    let mut lease = Value::Null;
    let mut acquired = true;
    if data["cmd"] == "resume" || (data["cmd"] == "scan_now" && !paused) {
        lease = json!(id());
        let value = json!({"token":lease,"expiresAt":now+8_000,"epoch":db.identity.server_epoch});
        acquired=db.connection.execute("INSERT INTO objects(collection,object_id,body) VALUES ('market_meta','scan_lease',?1) ON CONFLICT(collection,object_id) DO UPDATE SET body=excluded.body WHERE json_extract(objects.body,'$.expiresAt')<=?2 OR json_extract(objects.body,'$.epoch')!=?3",params![value.to_string(),now,db.identity.server_epoch])?==1;
    }
    let mut request = json!({"data":data,"requestedAt":now,"epoch":db.identity.server_epoch,"controlVersion":control,
        "positionRevision":position,"paused":paused,"profile":profile,"objects":objects,"scanLease":lease,"leaseAcquired":acquired});
    if !request["scanLease"].is_null() {
        request["quantObservationVersion"] = json!(crate::quant_observations::VERSION);
    }
    if data["cmd"] == "quotes" {
        request["holdingRuleVersion"] = json!(HOLDING_RULE_VERSION);
        request["historyCoverageVersion"] = json!(history::VERSION);
    }
    Ok(request)
}

pub async fn prepare(config: Option<&KernelConfig>, request: Value) -> Value {
    if request["paused"] == true && request["data"]["cmd"] == "scan_now" {
        return json!({"request":request,"response":{"error":"SCAN_PAUSED"}});
    }
    if !cfg!(feature = "integration-fixtures")
        && matches!(request["data"]["cmd"].as_str(), Some("scan_now" | "resume"))
        && request["requestedAt"]
            .as_u64()
            .is_some_and(|at| !market_time(at).1)
    {
        return json!({"request":request,"response":{"error":"SCAN_OUTSIDE_WINDOW"}});
    }
    if request["leaseAcquired"] == false {
        return json!({"request":request,"response":{"error":"SCAN_IN_PROGRESS"}});
    }
    let _lease = if matches!(request["data"]["cmd"].as_str(), Some("scan_now" | "resume")) {
        static SCANS: std::sync::OnceLock<std::sync::Arc<tokio::sync::Semaphore>> =
            std::sync::OnceLock::new();
        match SCANS
            .get_or_init(|| std::sync::Arc::new(tokio::sync::Semaphore::new(1)))
            .clone()
            .try_acquire_owned()
        {
            Ok(lease) => Some(lease),
            Err(_) => return json!({"request":request,"response":{"error":"SCAN_IN_PROGRESS"}}),
        }
    } else {
        None
    };
    let response = fetch(config, &request)
        .await
        .unwrap_or_else(|reason| json!({"error":"DATA_UNAVAILABLE","prepareReason":reason}));
    json!({"request":request,"response":response})
}

fn prepare_transport_error(error: reqwest::Error) -> &'static str {
    if error.is_timeout() {
        "KERNEL_PREPARE_TIMEOUT"
    } else {
        "KERNEL_PREPARE_UNREACHABLE"
    }
}

async fn fetch(config: Option<&KernelConfig>, request: &Value) -> Result<Value, &'static str> {
    const LIMIT: usize = 4 * 1024 * 1024;
    let config = config.ok_or("KERNEL_PREPARE_NOT_CONFIGURED")?;
    config
        .validate()
        .map_err(|_| "KERNEL_PREPARE_CONFIGURATION")?;
    let _ = rustls::crypto::ring::default_provider().install_default();
    let client = reqwest::Client::builder()
        .default_headers(config.headers().map_err(|_| "KERNEL_PREPARE_AUTH")?)
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(7))
        .build()
        .map_err(|_| "KERNEL_PREPARE_CONFIGURATION")?;
    let response = client
        .post(format!("http://{}/api/tickdesk/prepare", config.address))
        .json(request)
        .send()
        .await
        .map_err(prepare_transport_error)?;
    let status = response.status();
    if matches!(status.as_u16(), 401 | 403) {
        return Err("KERNEL_PREPARE_AUTH");
    }
    if !status.is_success() {
        return Err("KERNEL_PREPARE_HTTP_ERROR");
    }
    let bytes = crate::transport::read_limited(response, LIMIT)
        .await
        .map_err(|error| match error {
            crate::transport::BodyError::TooLarge => "KERNEL_PREPARE_INVALID_RESPONSE",
            crate::transport::BodyError::Transport(error) => prepare_transport_error(error),
        })?;
    let body: Value =
        serde_json::from_slice(&bytes).map_err(|_| "KERNEL_PREPARE_INVALID_RESPONSE")?;
    if !body.is_object() {
        return Err("KERNEL_PREPARE_INVALID_RESPONSE");
    }
    Ok(body)
}

pub(crate) fn command(
    d: &Domain<'_, '_>,
    events: &mut Vec<Value>,
    prepared: Option<&Value>,
) -> anyhow::Result<Outcome> {
    if let Some(response) = response(d, prepared) {
        persist_quotes(d.tx, response, d.now)?;
    }
    match d.data["cmd"].as_str().unwrap_or("") {
        "watch" | "unwatch" => watch(d, prepared),
        "search_symbols" => search(d, prepared),
        "get_chart" => chart(d, prepared),
        "get_quote_status" => quotes::read(d, response(d, prepared)),
        "get_candidates" => candidates(d),
        "scan_now" => scan(d, events, prepared, "manual"),
        _ => Ok(failure("INVALID_PARAMS")),
    }
}

fn response<'a>(d: &Domain<'_, '_>, p: Option<&'a Value>) -> Option<&'a Value> {
    p.filter(|p| p["request"]["data"] == *d.data && p["request"]["epoch"] == d.epoch)
        .map(|p| &p["response"])
}

fn watch(d: &Domain<'_, '_>, prepared: Option<&Value>) -> anyhow::Result<Outcome> {
    let requested = d.data["symbols"].as_array().unwrap();
    let adding = d.data["cmd"] == "watch";
    let mut listings = std::collections::BTreeMap::new();
    if adding {
        let Some(items) = response(d, prepared).and_then(|r| r["symbols"].as_array()) else {
            return Ok(failure("DATA_UNAVAILABLE"));
        };
        for item in items {
            validate_definition("SymbolInfo", item)?;
            listings.insert(item["symbol"].as_str().unwrap(), item);
        }
        let invalid: Vec<_> = requested
            .iter()
            .filter(|s| !listings.contains_key(s.as_str().unwrap()))
            .cloned()
            .collect();
        if !invalid.is_empty() {
            return Ok(Err(("INVALID_SYMBOL", json!({"invalidSymbols":invalid}))));
        }
        if requested.iter().any(|s| {
            listings[s.as_str().unwrap()]["source"]["asOf"]
                .as_u64()
                .is_none_or(|at| at > d.now)
        }) {
            return Ok(failure("UNSUPPORTED_MARKET"));
        }
    }
    let mut added = vec![];
    let mut removed = vec![];
    let mut unchanged = vec![];
    let existing = d.list("watchlist")?;
    let new_count = requested
        .iter()
        .filter(|s| !existing.iter().any(|i| i["symbol"] == **s))
        .count();
    if adding && existing.len() + new_count > 500 {
        return Ok(failure("INVALID_PARAMS"));
    }
    for symbol in requested {
        let key = symbol.as_str().unwrap();
        let present = existing.iter().any(|i| i["symbol"] == *symbol);
        if adding && !present {
            let master = listings[key];
            let quote = d.get("market_quotes", key)?.unwrap_or(Value::Null);
            d.put("watchlist",key,&json!({"symbol":symbol,"name":master["name"],"addedAt":d.now,"dataAsOf":quote["asOf"],"riskStatus":"unknown"}))?;
            added.push(symbol);
        } else if !adding && present {
            d.tx.execute(
                "DELETE FROM objects WHERE collection='watchlist' AND object_id=?1",
                [key],
            )?;
            removed.push(symbol);
        } else {
            unchanged.push(symbol);
        }
    }
    let seq = if added.is_empty() && removed.is_empty() {
        d.seq()?
    } else {
        d.bump()?
    };
    Ok(Ok(
        json!({"addedSymbols":added,"removedSymbols":removed,"unchangedSymbols":unchanged,"watchlist":d.list("watchlist")?,"snapshotSeq":seq}),
    ))
}

fn page(d: &Domain<'_, '_>, query: &str, initial: Value) -> anyhow::Result<Outcome> {
    query_snapshot(d, query, initial, "pageCursor", "nextPageCursor", "items")
}

pub(crate) fn query_snapshot(
    d: &Domain<'_, '_>,
    query: &str,
    initial: Value,
    input: &str,
    output: &str,
    rows: &str,
) -> anyhow::Result<Outcome> {
    let (mut body, expires) = if let Some(cursor) = d.data[input].as_str() {
        let row:Option<(String,u64)>=d.tx.query_row("SELECT body,expires_at FROM read_cursors WHERE cursor=?1 AND pair_id=?2 AND epoch=?3 AND query=?4",params![cursor,d.pair,d.epoch,query],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
        let Some((body, expires)) = row else {
            return Ok(failure("INVALID_PARAMS"));
        };
        if expires <= d.now {
            return Ok(failure("SNAPSHOT_EXPIRED"));
        }
        (serde_json::from_str::<Value>(&body)?, expires)
    } else {
        (initial, d.now + 600_000)
    };
    let items = body[rows].as_array().context("invalid market page")?;
    let limit = d.data["limit"].as_u64().unwrap_or(20) as usize;
    let take = items.len().min(limit);
    let more = items.len() > take;
    let visible = items[..take].to_vec();
    let cursor = if more {
        let cursor = id();
        let mut tail = body.clone();
        tail[rows] = json!(items[take..]);
        d.tx.execute(
            "INSERT INTO read_cursors VALUES (?1,?2,?3,?4,?5,?6)",
            params![cursor, d.pair, d.epoch, query, expires, tail.to_string()],
        )?;
        json!(cursor)
    } else {
        Value::Null
    };
    body[rows] = json!(visible);
    body[output] = cursor;
    body["hasMore"] = json!(more);
    Ok(Ok(body))
}

fn search(d: &Domain<'_, '_>, p: Option<&Value>) -> anyhow::Result<Outcome> {
    let query = d.data["query"].as_str().unwrap_or("");
    let mut items = response(d, p)
        .and_then(|r| r["symbols"].as_array())
        .cloned()
        .unwrap_or_default();
    if d.data["pageCursor"].is_null()
        && response(d, p).is_none_or(|r| r["symbols"].as_array().is_none())
    {
        return Ok(failure("DATA_UNAVAILABLE"));
    }
    for item in &items {
        validate_definition("SymbolInfo", item)?;
    }
    items.retain(|i| {
        i["symbol"].as_str().unwrap().contains(query) || i["name"].as_str().unwrap().contains(query)
    });
    items.sort_by(|a, b| a["symbol"].as_str().cmp(&b["symbol"].as_str()));
    page(d, &format!("symbols:{query}"), json!({"items":items}))
}

fn chart(d: &Domain<'_, '_>, p: Option<&Value>) -> anyhow::Result<Outcome> {
    if let Some(result) = response(d, p) {
        match result["chartPreparation"].as_str() {
            Some("CHART_PREPARING") => return Ok(failure("CHART_PREPARING")),
            Some("CHART_PREPARATION_FAILED") => return Ok(failure("CHART_PREPARATION_FAILED")),
            _ if result["error"] == "DATA_UNAVAILABLE" => return Ok(failure("DATA_UNAVAILABLE")),
            _ if result["error"] == "CHART_CURSOR_EXPIRED" => {
                return Ok(failure("CHART_CURSOR_EXPIRED"));
            }
            _ => {}
        }
    }
    let timeframe = d.data["timeframe"].as_str().unwrap_or("1d");
    let minute = matches!(timeframe, "1m" | "5m");
    let mut absent = json!({"symbol":d.data["symbol"],"adjustment":d.data["adjustment"],"asOf":null,"dataStatus":"missing","candles":[],"nextBeforeDate":null,"hasMore":false,"source":null,"nextCursor":null});
    if timeframe != "1d" {
        absent["timeframe"] = json!(timeframe);
    }
    let value = response(d, p)
        .and_then(|r| r.get("chart"))
        .unwrap_or(&absent);
    if validate_definition("result_get_chart", value).is_err()
        || value["symbol"] != d.data["symbol"]
        || value["adjustment"] != d.data["adjustment"]
        || value["timeframe"].as_str().unwrap_or("1d") != timeframe
    {
        return Ok(failure("DATA_UNAVAILABLE"));
    }
    let candles = value["candles"].as_array().unwrap();
    let page_valid = if minute {
        (value["hasMore"] == true
            && value["nextCursor"]
                .as_str()
                .is_none_or(|cursor| !cursor.starts_with("v2:")))
            || (value["hasMore"] == false && !value["nextCursor"].is_null())
    } else {
        (value["hasMore"] == true
            && candles
                .first()
                .is_none_or(|bar| bar["date"] != value["nextBeforeDate"]))
            || (value["hasMore"] == false && !value["nextBeforeDate"].is_null())
    };
    if candles.len() as u64 > d.data["limit"].as_u64().unwrap_or(20) || page_valid {
        return Ok(failure("DATA_UNAVAILABLE"));
    }
    let mut prev = "";
    let mut prev_start = None;
    for candle in candles {
        let Some(date) = candle["date"].as_str() else {
            return Ok(failure("DATA_UNAVAILABLE"));
        };
        if minute {
            let (Some(start), Some(end), Some(interval), Some(complete)) = (
                candle["startAt"].as_u64(),
                candle["endMs"].as_u64(),
                candle["interval"].as_str(),
                candle["complete"].as_bool(),
            ) else {
                return Ok(failure("DATA_UNAVAILABLE"));
            };
            let step = if timeframe == "1m" { 60_000 } else { 300_000 };
            if interval != timeframe
                || start % 60_000 != 0
                || start > d.now
                || end != start + step
                || prev_start.is_some_and(|prior| start <= prior)
            {
                return Ok(failure("DATA_UNAVAILABLE"));
            }
            if complete {
                if candle["availableAtMs"]
                    .as_u64()
                    .is_none_or(|at| at < end || at > d.now)
                {
                    return Ok(failure("DATA_UNAVAILABLE"));
                }
            } else if !candle["availableAtMs"].is_null() {
                return Ok(failure("DATA_UNAVAILABLE"));
            }
            prev_start = Some(start);
        } else if !(date > prev
            && d.data["beforeDate"]
                .as_str()
                .is_none_or(|before| date < before))
        {
            return Ok(failure("DATA_UNAVAILABLE"));
        }
        prev = date;
        let Some(high) = candle["high"].as_str() else {
            return Ok(failure("DATA_UNAVAILABLE"));
        };
        let Some(low) = candle["low"].as_str() else {
            return Ok(failure("DATA_UNAVAILABLE"));
        };
        for key in ["open", "close"] {
            let Some(v) = candle[key].as_str() else {
                return Ok(failure("DATA_UNAVAILABLE"));
            };
            if decimal_cmp(high, v) == Ordering::Less || decimal_cmp(low, v) == Ordering::Greater {
                return Ok(failure("DATA_UNAVAILABLE"));
            }
        }
    }
    if let Some(gaps) = value["gaps"].as_array() {
        if gaps.len() > 200 {
            return Ok(failure("DATA_UNAVAILABLE"));
        }
        let step = if timeframe == "1m" { 60_000 } else { 300_000 };
        for gap in gaps {
            let (Some(start), Some(end)) = (gap["startAt"].as_u64(), gap["endMs"].as_u64()) else {
                return Ok(failure("DATA_UNAVAILABLE"));
            };
            let missing = gap["missingMinutes"].as_array();
            if start % 60_000 != 0
                || start > d.now
                || end != start + step
                || gap["quality"] != "missing_first_minute"
                || missing.is_none_or(|rows| rows.is_empty() || rows.len() > 5)
                || gap["observedMinutes"]
                    .as_u64()
                    .is_none_or(|count| count == 0 || count >= step / 60_000)
            {
                return Ok(failure("DATA_UNAVAILABLE"));
            }
        }
    }
    Ok(Ok(value.clone()))
}

fn candidates(d: &Domain<'_, '_>) -> anyhow::Result<Outcome> {
    let latest = d.get("market_meta", "latest_scan")?.unwrap_or(Value::Null);
    let requested = d.data["scanId"].as_str();
    let key = requested.or_else(|| latest["scanId"].as_str());
    let stored = key
        .map(|key| d.get("market_scans", key))
        .transpose()?
        .flatten();
    if requested.is_some() && stored.is_none() {
        return Ok(failure("SCAN_NOT_FOUND"));
    }
    let body = stored.unwrap_or(json!({"scan":null,"items":[]}));
    let historical = requested.is_some_and(|key| latest["scanId"] != key);
    // Query identity retains whether the caller requested latest; cursor stores the original scan.
    page(
        d,
        &format!("candidates:{}", requested.unwrap_or("latest")),
        json!({"scan":body["scan"],"items":body["items"],"historical":historical}),
    )
}

pub(crate) fn resume(
    d: &Domain<'_, '_>,
    events: &mut Vec<Value>,
    p: Option<&Value>,
) -> anyhow::Result<Outcome> {
    if let Some(response) = response(d, p) {
        persist_quotes(d.tx, response, d.now)?;
    }
    if p.is_some_and(|p| {
        p["request"]["controlVersion"] != d.analysis().unwrap_or(Value::Null)["controlVersion"]
    }) {
        return Ok(failure("SCAN_INTERRUPTED"));
    }
    crate::reminders::advance(d.tx, d.now, true)?;
    d.tx.execute(
        "UPDATE domain_state SET paused=0,control_version=control_version+1",
        [],
    )?;
    d.bump()?;
    let result = scan(d, events, p, "resume")?;
    let (accepted, scan) = match result {
        Ok(scan) => (true, scan),
        Err(("SCAN_PARTIAL", details)) => (true, details["scan"].clone()),
        Err((_, details)) => (false, details["scan"].clone()),
    };
    Ok(Ok(
        json!({"paused":false,"controlVersion":d.analysis()?["controlVersion"],"scan":{"status":if accepted{"accepted"}else{"rejected"},"scanId":if accepted{scan["scanId"].clone()}else{Value::Null},"code":if accepted{Value::Null}else{scan["error"]["code"].clone()}},"snapshotSeq":d.seq()?}),
    ))
}

fn market_time(now: u64) -> (String, bool) {
    let time = DateTime::<Utc>::from_timestamp_millis(now as i64)
        .unwrap()
        .with_timezone(&chrono_tz::Asia::Shanghai);
    let minutes = time.hour() * 60 + time.minute();
    (
        time.format("%Y-%m-%d").to_string(),
        time.weekday().num_days_from_monday() < 5
            && ((565..=695).contains(&minutes) || (775..=905).contains(&minutes)),
    )
}

fn tradable_time(now: u64) -> bool {
    let time = DateTime::<Utc>::from_timestamp_millis(now as i64)
        .unwrap()
        .with_timezone(&chrono_tz::Asia::Shanghai);
    let minutes = time.hour() * 60 + time.minute();
    time.weekday().num_days_from_monday() < 5
        && ((570..690).contains(&minutes) || (780..900).contains(&minutes))
}

fn frozen_strategy_summary(frozen: &Value) -> Option<&Value> {
    let strategy = &frozen["strategies"];
    let total = strategy["total"].as_u64().filter(|total| *total <= 100)?;
    let succeeded = strategy["succeeded"]
        .as_u64()
        .filter(|count| *count <= total)?;
    let failed = strategy["failed"].as_array()?;
    if succeeded + failed.len() as u64 != total
        || strategy.as_object()?.len() != 3
        || failed.iter().any(|id| {
            id.as_str()
                .is_none_or(|s| s.is_empty() || s.chars().count() > 80)
        })
        || failed
            .iter()
            .filter_map(Value::as_str)
            .collect::<BTreeSet<_>>()
            .len()
            != failed.len()
    {
        return None;
    }
    Some(strategy)
}

fn valid_frozen_candidates(frozen: &Value, summary: &Value, now: u64) -> bool {
    let minimum = &frozen["policy"]["minScore"];
    if validate_definition("Decimal", minimum).is_err()
        || minimum.as_str().is_none_or(|score| {
            score.starts_with('-') || decimal_cmp(score, "100") == Ordering::Greater
        })
    {
        return false;
    }
    let failed: BTreeSet<&str> = summary["failed"]
        .as_array()
        .expect("validated strategy summary")
        .iter()
        .filter_map(Value::as_str)
        .collect();
    let mut contributors = BTreeSet::<&str>::new();
    let Some(candidates) = frozen["candidates"]
        .as_array()
        .filter(|rows| rows.len() <= 500)
    else {
        return false;
    };
    for raw in candidates {
        let candidate = &raw["candidate"];
        if validate_definition("Candidate", candidate).is_err()
            || raw
                .get("source")
                .is_some_and(|source| validate_definition("Source", source).is_err())
        {
            return false;
        }
        contributors.extend(
            candidate["strategies"]
                .as_array()
                .expect("validated candidate")
                .iter()
                .filter_map(Value::as_str),
        );
    }
    if frozen["manifest"]["schemaVersion"] == 3
        && !valid_v3_outcomes(frozen, summary, &contributors, candidates, now)
    {
        return false;
    }
    !contributors
        .iter()
        .any(|strategy| failed.contains(strategy))
        && contributors.len()
            <= summary["succeeded"]
                .as_u64()
                .expect("validated strategy summary") as usize
}

const V3_EVIDENCE_REASONS: [&str; 8] = [
    "EXPECTED_DECISION_BAR_UNAVAILABLE",
    "REVIEWED_UNIVERSE_INCOMPLETE",
    "REVIEWED_DIRECTION_UNAVAILABLE",
    "CURRENT_EXECUTION_QUOTE_UNAVAILABLE",
    "CURRENT_TURNOVER_UNAVAILABLE",
    "EXIT_CONDITION_UNAVAILABLE",
    // Accepted only for artifacts produced before exits had their own rule.
    "EXIT_SCORE_UNVERIFIED",
    "ENTRY_SCORE_UNVERIFIED",
];

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct ExitSignal {
    symbol: String,
    name: String,
    strategy_id: String,
    strategy_ids: Vec<String>,
    rule_id: String,
    rule_version: u64,
    condition: String,
    decision_clock: String,
    bar_end_ms: u64,
    bar_available_at_ms: u64,
    quote: Option<ExitQuote>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct ExitQuote {
    price: String,
    as_of: u64,
    source: Value,
}

#[derive(Clone)]
pub(crate) struct StrategySpec {
    id: String,
    direction: String,
    symbols: Vec<Value>,
    group: String,
    code_hash: String,
    parameters_hash: String,
    dependencies_hash: String,
}

impl StrategySpec {
    fn matches_origin(&self, origin: &Value) -> bool {
        origin["group"] == json!(self.group)
            && origin["strategyId"] == json!(self.id)
            && origin["codeHash"] == json!(self.code_hash)
            && origin["parametersHash"] == json!(self.parameters_hash)
            && origin["dependenciesHash"] == json!(self.dependencies_hash)
    }
}

fn valid_hash(value: &Value) -> Option<&str> {
    value
        .as_str()
        .filter(|hash| hash.len() == 64 && hash.bytes().all(|byte| byte.is_ascii_hexdigit()))
}

fn strategy_specs(manifest: &Value) -> Option<std::collections::BTreeMap<String, StrategySpec>> {
    let specs = manifest["strategyHashes"].as_array()?;
    specs
        .iter()
        .map(|spec| {
            let id = spec["id"]
                .as_str()
                .filter(|id| !id.is_empty() && id.len() <= 64)?;
            let direction = spec["direction"]
                .as_str()
                .filter(|side| matches!(*side, "buy" | "sell"))?;
            let symbols = spec["symbols"]
                .as_array()
                .filter(|symbols| !symbols.is_empty())?;
            let group = spec["group"]
                .as_str()
                .filter(|group| matches!(*group, "daily_swing" | "short_term" | "long_term"))?;
            Some((
                id.to_owned(),
                StrategySpec {
                    id: id.to_owned(),
                    direction: direction.to_owned(),
                    symbols: symbols.clone(),
                    group: group.to_owned(),
                    code_hash: valid_hash(&spec["codeHash"])?.to_owned(),
                    parameters_hash: valid_hash(&spec["parametersHash"])?.to_owned(),
                    dependencies_hash: valid_hash(&spec["dependenciesHash"])?.to_owned(),
                },
            ))
        })
        .collect()
}

/// Parse the versioned exit signals. A signal is usable only when it belongs
/// to an approved sell scope, names a completed bar available at this cutoff
/// and (when present) a quote whose source clock matches.
fn parse_exits(
    frozen: &Value,
    specs: &std::collections::BTreeMap<String, StrategySpec>,
    now: u64,
) -> Option<Vec<ExitSignal>> {
    let raw: &[Value] = match frozen.get("exits") {
        // An artifact from a kernel that predates exit rules has no exits.
        None => &[],
        Some(Value::Array(rows)) => rows.as_slice(),
        Some(_) => return None,
    };
    if raw.len() > 500 {
        return None;
    }
    let mut seen = BTreeSet::new();
    let mut signals = Vec::new();
    for value in raw {
        let signal: ExitSignal = serde_json::from_value(value.clone()).ok()?;
        validate_definition("Symbol", &json!(signal.symbol)).ok()?;
        if signal.name.is_empty() || signal.name.chars().count() > 100 {
            return None;
        }
        let spec = specs.get(&signal.strategy_id)?;
        if spec.direction != "sell" || !spec.symbols.contains(&json!(signal.symbol)) {
            return None;
        }
        if signal.strategy_ids.is_empty()
            || signal.strategy_ids.len() > 100
            || signal
                .strategy_ids
                .windows(2)
                .any(|pair| pair[0] >= pair[1])
            || !signal.strategy_ids.contains(&signal.strategy_id)
        {
            return None;
        }
        for id in &signal.strategy_ids {
            let spec = specs.get(id)?;
            if spec.direction != "sell" || !spec.symbols.contains(&json!(signal.symbol)) {
                return None;
            }
        }
        if signal.rule_id.trim().is_empty()
            || signal.rule_id.len() > 80
            || signal.condition.trim().is_empty()
            || signal.condition.chars().count() > 200
            || signal.decision_clock != "completed_daily_bar"
            || signal.rule_version == 0
            || signal.rule_version > 1000
            || signal.bar_end_ms == 0
            || signal.bar_end_ms > signal.bar_available_at_ms
            || signal.bar_available_at_ms > now
            || !seen.insert(signal.symbol.clone())
        {
            return None;
        }
        if let Some(quote) = &signal.quote {
            validate_definition("Source", &quote.source).ok()?;
            if quote.price.len() > 48
                || quote.price.starts_with('-')
                || decimal_cmp(&quote.price, "0") != Ordering::Greater
                || quote.as_of > now
                || quote.source["asOf"] != json!(quote.as_of)
            {
                return None;
            }
        }
        signals.push(signal);
    }
    Some(signals)
}

fn valid_v3_outcomes(
    frozen: &Value,
    summary: &Value,
    contributors: &BTreeSet<&str>,
    candidates: &[Value],
    now: u64,
) -> bool {
    let manifest = &frozen["manifest"];
    let Some(outcomes) = manifest["strategyOutcomes"].as_object() else {
        return false;
    };
    let Some(specs) = strategy_specs(manifest) else {
        return false;
    };
    if outcomes.len() as u64 != summary["total"].as_u64().unwrap_or(u64::MAX)
        || specs.len() != outcomes.len()
    {
        return false;
    }
    if outcomes.keys().any(|id| !specs.contains_key(id.as_str())) {
        return false;
    }
    let Some(exits) = parse_exits(frozen, &specs, now) else {
        return false;
    };
    let mut exit_strategies: std::collections::BTreeMap<&str, BTreeSet<&str>> =
        std::collections::BTreeMap::new();
    for signal in &exits {
        for id in &signal.strategy_ids {
            exit_strategies
                .entry(id.as_str())
                .or_default()
                .insert(signal.symbol.as_str());
        }
    }
    let failed = summary["failed"]
        .as_array()
        .expect("validated strategy summary");
    for (id, outcome) in outcomes {
        let Some(reasons) = outcome["reasons"].as_array() else {
            return false;
        };
        let has_candidate = contributors.contains(id.as_str());
        let is_failed = failed.iter().any(|value| value == id);
        let is_sell = specs
            .get(id.as_str())
            .is_some_and(|spec| spec.direction == "sell");
        let valid = match outcome["status"].as_str() {
            Some("matched") if is_sell => {
                !has_candidate
                    && !is_failed
                    && reasons.is_empty()
                    && exit_strategies
                        .get(id.as_str())
                        .is_some_and(|symbols| !symbols.is_empty())
            }
            Some("matched") => has_candidate && !is_failed && reasons.is_empty(),
            Some("no_match") => !has_candidate && !is_failed && reasons.is_empty(),
            Some("not_computable") => {
                !has_candidate
                    && is_failed
                    && !reasons.is_empty()
                    && reasons.len() <= V3_EVIDENCE_REASONS.len()
                    && reasons.iter().all(|reason| {
                        reason
                            .as_str()
                            .is_some_and(|code| V3_EVIDENCE_REASONS.contains(&code))
                    })
            }
            _ => false,
        };
        if !valid {
            return false;
        }
    }
    candidates.iter().all(|raw| {
        let candidate = &raw["candidate"];
        candidate["strategies"]
            .as_array()
            .expect("validated candidate")
            .iter()
            .all(|id| {
                id.as_str()
                    .and_then(|id| specs.get(id))
                    .is_some_and(|spec| {
                        raw["direction"] == spec.direction
                            && spec.direction == "buy"
                            && spec.symbols.contains(&candidate["symbol"])
                    })
            })
    })
}

fn scan(
    d: &Domain<'_, '_>,
    events: &mut Vec<Value>,
    p: Option<&Value>,
    trigger: &str,
) -> anyhow::Result<Outcome> {
    let key = id();
    let (date, in_window) = market_time(d.now);
    let analysis = d.analysis()?;
    let baseline = p.map(|p| &p["request"]);
    let supplied = response(d, p);
    let fixture_session = cfg!(feature = "integration-fixtures")
        && supplied.is_some_and(|value| value["fixture"] == "tickdesk-integration-v1");
    let calendar_error = match supplied.and_then(|r| r["tradingCalendar"]["state"].as_str()) {
        Some("closed") => Some("SCAN_OUTSIDE_WINDOW"),
        Some("unknown") => Some("DATA_UNAVAILABLE"),
        _ => None,
    };
    let frozen = supplied
        .filter(|_| calendar_error.is_none())
        .and_then(|r| r.get("frozen"));
    let frozen_summary = frozen.and_then(frozen_strategy_summary);
    let v3 = frozen.is_some_and(|f| f["manifest"]["schemaVersion"] == 3);
    let mut valid_v3 = false;
    let requested = baseline
        .and_then(|v| v["requestedAt"].as_u64())
        .unwrap_or(d.now);
    let scheduled_slot = (trigger == "scheduled").then_some(requested / SCAN_INTERVAL_MS);
    if let Some(slot) = scheduled_slot {
        if scheduled_slot_finished(d.tx, d.epoch, slot)? {
            return Ok(failure("SCAN_IN_PROGRESS"));
        }
    }
    let changed = baseline.is_some_and(|b| {
        b["positionRevision"] != d.position_revision().unwrap_or(u64::MAX)
            || b["controlVersion"]
                .as_u64()
                .unwrap_or(u64::MAX)
                .checked_add(u64::from(trigger == "resume"))
                != analysis["controlVersion"].as_u64()
    });
    let error = if analysis["paused"] == true {
        Some("SCAN_PAUSED")
    } else if !in_window && !fixture_session {
        Some("SCAN_OUTSIDE_WINDOW")
    } else if d.now < requested || d.now - requested > 8_000 {
        Some("SCAN_DEADLINE_EXCEEDED")
    } else if changed {
        Some("SCAN_INTERRUPTED")
    } else if calendar_error.is_some() {
        calendar_error
    } else if let Some(f) = frozen {
        if f["tradeDate"] != date || f["isTradingDay"] != true {
            Some("SCAN_OUTSIDE_WINDOW")
        } else if f["generation"].as_str().is_none_or(str::is_empty)
            || f["generation"] != f["generationAfter"]
            || frozen_summary.is_none_or(|summary| !valid_frozen_candidates(f, summary, d.now))
        {
            Some("SCAN_FAILED")
        } else if v3 && f["strategies"]["succeeded"] == 0 {
            valid_v3 = true;
            Some("SCAN_FAILED")
        } else if f["asOf"]
            .as_u64()
            .is_none_or(|at| at > d.now || d.now - at > 90_000)
        {
            Some("SCAN_DATA_STALE")
        } else {
            valid_v3 = v3;
            None
        }
    } else {
        Some(match supplied.and_then(|r| r["error"].as_str()) {
            Some("SCAN_IN_PROGRESS") => "SCAN_IN_PROGRESS",
            Some("SCAN_OUTSIDE_WINDOW") => "SCAN_OUTSIDE_WINDOW",
            Some("SCAN_DEADLINE_EXCEEDED") => "SCAN_DEADLINE_EXCEEDED",
            _ => "DATA_UNAVAILABLE",
        })
    };
    // The same transaction owns evidence and the business decisions that consume it.
    let mut rule_updates = BTreeSet::new();
    if analysis["paused"] == false
        && !d.recovering
        && !changed
        && calendar_error.is_none()
        && d.now >= requested
        && d.now - requested <= 8_000
    {
        if let (Some(request), Some(reply)) = (baseline, supplied) {
            rule_updates = crate::quant_observations::persist(d, &key, request, reply)?;
        }
    }
    let mut items = vec![];
    let mut published = if (in_window || fixture_session) && error != Some("SCAN_OUTSIDE_WINDOW") {
        risks::advance(d, &key, &date, rule_updates, events)?
    } else {
        vec![]
    };
    let mut rejects = std::collections::BTreeMap::<String, u64>::new();
    if valid_v3 {
        let outcomes = frozen.expect("validated v3 scan")["manifest"]["strategyOutcomes"]
            .as_object()
            .expect("validated strategy outcomes");
        for outcome in outcomes.values() {
            let reasons: BTreeSet<_> = outcome["reasons"]
                .as_array()
                .expect("validated strategy reasons")
                .iter()
                .filter_map(Value::as_str)
                .collect();
            for reason in reasons {
                *rejects.entry(reason.to_owned()).or_default() += 1;
            }
        }
    }
    if error == Some("DATA_UNAVAILABLE") {
        if let Some(
            reason @ ("NO_PRODUCTION_APPROVAL"
            | "TRADING_CALENDAR_UNAVAILABLE"
            | "PRODUCTION_EVALUATION_FAILED"),
        ) = supplied.and_then(|r| r["productionReason"].as_str())
        {
            rejects.insert(reason.to_owned(), 1);
        }
    }
    let strategy = frozen_summary
        .cloned()
        .unwrap_or(json!({"total":0,"succeeded":0,"failed":[]}));
    let partial = strategy["failed"].as_array().is_some_and(|v| !v.is_empty());
    let mut error = error;
    if error.is_none() && strategy["succeeded"].as_u64().unwrap_or(0) == 0 {
        error = Some("SCAN_FAILED");
    }
    if error.is_none() {
        let f = frozen.unwrap();
        let raw = f["candidates"].as_array().context("frozen candidates")?;
        let mut ordered = raw.clone();
        ordered.sort_by(|a, b| {
            decimal_cmp(
                b["candidate"]["score"].as_str().unwrap_or("0"),
                a["candidate"]["score"].as_str().unwrap_or("0"),
            )
            .then(
                a["candidate"]["symbol"]
                    .as_str()
                    .cmp(&b["candidate"]["symbol"].as_str()),
            )
        });
        let conservative = analysis["profile"] == "conservative";
        let min_hits = if conservative { 3 } else { 2 };
        let daily_limit = if conservative { 3 } else { 8 };
        let cooldown = if conservative { 3_600_000 } else { 1_800_000 };
        let tasks = d.list("tasks")?;
        let specs_needed = f["manifest"]["schemaVersion"] == 3;
        // Only a formal approved exit can suppress production buys. A pending
        // sell task card is a reminder, not a durable exit latch.
        let mut exit_symbols: BTreeSet<String> = BTreeSet::new();
        // A complete, later trusted bar that disproves the exit rule closes
        // the formal latch before this scan decides whether a buy is allowed.
        resolve_refuted_formal_exits(d, f)?;
        // Production buy suppression is driven only by formal approved exits,
        // never by research observations. The formal event is independent of
        // the 30-minute task card.
        let formal = formal_exits(d)?;
        for event in formal.values().filter(|event| event["state"] == "active") {
            if let Some(symbol) = event["symbol"].as_str() {
                exit_symbols.insert(symbol.to_owned());
            }
        }
        if specs_needed {
            let specs = strategy_specs(&f["manifest"]).context("validated strategy specs")?;
            for signal in parse_exits(f, &specs, d.now).context("validated exits")? {
                let symbol = signal.symbol.clone();
                let pos = d.get("positions", &symbol)?;
                if pos
                    .as_ref()
                    .is_none_or(|p| p["qty"].as_u64().is_none_or(|qty| qty == 0))
                {
                    *rejects
                        .entry("EXIT_WITHOUT_HOLDING".to_owned())
                        .or_default() += 1;
                    resolve_formal_exits_for_symbol(d, events, &symbol, "holding_ended")?;
                    continue;
                }
                if pos
                    .as_ref()
                    .is_some_and(|p| p["reconciliationRequired"] == true)
                {
                    *rejects
                        .entry("POSITION_RECONCILIATION".to_owned())
                        .or_default() += 1;
                    continue;
                }
                let holding = d.holding_details_for(&symbol)?;
                let (mut restriction, available) =
                    exit_restriction(&holding, &signal.strategy_ids, &specs);
                if let Some(reason) = risks::trading_constraint(d, &symbol, &date, "sell")? {
                    restriction = if reason == "TRADING_SUSPENDED" {
                        "trading_suspended"
                    } else {
                        "sellable_unknown"
                    };
                }
                let executable = matches!(restriction, "" | "partially_sellable");
                let quote_fresh = signal
                    .quote
                    .as_ref()
                    .is_some_and(|quote| quote.as_of <= d.now && d.now - quote.as_of <= 90_000);
                // The five-minute scan window is wider than the continuous
                // auction. Executability uses the actual tradable session.
                let executable = executable && quote_fresh && tradable_time(d.now);
                if !executable && restriction.is_empty() {
                    restriction = if !quote_fresh {
                        "quote_unavailable"
                    } else {
                        "market_closed"
                    };
                }
                let event_key = formal_exit_key(d, &signal, f)?;
                let existing = formal.get(&event_key).cloned();
                let previous_task = existing
                    .as_ref()
                    .and_then(|event| event["lastTaskId"].as_str())
                    .map(str::to_owned);
                let transition = existing.as_ref().is_some_and(|event| {
                    event["state"] == "active" && executable && event["executable"] != true
                });
                // The rule fact is durable, but executability, quantity,
                // restriction and the visible card are facts of this scan.
                // Refresh all of them before deciding whether a new reminder
                // stage was reached.
                persist_formal_exit(
                    d,
                    &event_key,
                    &key,
                    &signal,
                    &holding,
                    restriction,
                    available,
                    executable,
                    previous_task.as_deref(),
                )?;
                if existing
                    .as_ref()
                    .is_some_and(|event| event["state"] == "active")
                    && !transition
                {
                    *rejects.entry("FORMAL_EXIT_LATCHED".to_owned()).or_default() += 1;
                    exit_symbols.insert(symbol);
                    if let Some(task_id) = previous_task.as_deref() {
                        if let Some(task) = update_exit_task(
                            d,
                            task_id,
                            &signal,
                            restriction,
                            available,
                            executable,
                            false,
                        )? {
                            events.push(event(d, "task_update", task)?);
                        }
                    }
                    continue;
                }
                if transition {
                    if let Some(task_id) = previous_task.as_deref() {
                        if let Some(task) = update_exit_task(
                            d,
                            task_id,
                            &signal,
                            restriction,
                            available,
                            executable,
                            true,
                        )? {
                            events.push(event(d, "task_update", task)?);
                        }
                    }
                }
                if !transition
                    && tasks.iter().any(|t| {
                        t["symbol"] == symbol
                            && t["direction"] == "sell"
                            && t["createdAt"]
                                .as_u64()
                                .is_some_and(|at| d.now.saturating_sub(at) < cooldown)
                    })
                {
                    *rejects.entry("COOLDOWN_SUPPRESSED".to_owned()).or_default() += 1;
                    exit_symbols.insert(symbol);
                    continue;
                }
                if !transition
                    && tasks.iter().any(|t| {
                        t["symbol"] == symbol
                            && t["direction"] == "sell"
                            && matches!(t["status"].as_str(), Some("pending" | "reminded"))
                            && t["validUntil"].as_u64().is_some_and(|at| at > d.now)
                    })
                {
                    *rejects.entry("ACTIVE_TASK_EXISTS".to_owned()).or_default() += 1;
                    exit_symbols.insert(symbol);
                    continue;
                }
                if published.len() >= 500 {
                    *rejects.entry("PUBLISH_CAPACITY".to_owned()).or_default() += 1;
                    exit_symbols.insert(symbol);
                    continue;
                }
                let task_key = id();
                let task = create_exit_task(
                    d,
                    &key,
                    &task_key,
                    &signal,
                    restriction,
                    available,
                    executable,
                    partial,
                )?;
                persist_formal_exit(
                    d,
                    &event_key,
                    &key,
                    &signal,
                    &holding,
                    restriction,
                    available,
                    executable,
                    Some(&task_key),
                )?;
                exit_symbols.insert(symbol);
                published.push(task["taskId"].clone());
                events.push(event(d, "task_update", task.clone())?);
                // A restricted exit is visible on the holding but must not nag
                // for shares that cannot be sold yet.
                if executable {
                    events.push(event(d, "reminder", task)?);
                }
            }
        }
        let mut count = tasks
            .iter()
            .filter(|t| {
                t["direction"] == "buy"
                    && t["createdAt"]
                        .as_u64()
                        .is_some_and(|at| market_time(at).0 == date)
            })
            .count();
        let mut seen = BTreeSet::new();
        for raw in ordered {
            let candidate = &raw["candidate"];
            let symbol = candidate["symbol"].as_str().unwrap();
            let direction = raw["direction"].as_str().unwrap_or("");
            let strategies = candidate["strategies"]
                .as_array()
                .unwrap()
                .iter()
                .map(Value::to_string)
                .collect::<BTreeSet<_>>();
            let pos = d.get("positions", symbol)?;
            let reason = if raw["approved"] != true {
                "STRATEGY_NOT_APPROVED"
            } else if raw["supported"] != true {
                "UNSUPPORTED_MARKET"
            } else if !["buy", "sell", "risk"].contains(&direction) {
                "INVALID_DIRECTION"
            } else if !seen.insert((symbol.to_owned(), direction.to_owned())) {
                "DUPLICATE_SIGNAL"
            } else if candidate["dataAsOf"]
                .as_u64()
                .is_none_or(|at| at > d.now || d.now - at > 90_000)
            {
                "SCAN_DATA_STALE"
            } else if decimal_cmp(candidate["score"].as_str().unwrap(), "100") == Ordering::Greater
            {
                "INVALID_SCORE"
            } else if raw["liquidityPassed"] != true {
                "LIQUIDITY_UNVERIFIED"
            } else if direction != "risk" && strategies.len() < min_hits {
                "RESONANCE_INSUFFICIENT"
            } else if f["policy"]["minScore"].as_str().is_none_or(|min| {
                decimal_cmp(candidate["score"].as_str().unwrap(), min) == Ordering::Less
            }) {
                "SCORE_THRESHOLD"
            } else if pos
                .as_ref()
                .is_some_and(|p| p["reconciliationRequired"] == true)
            {
                "POSITION_RECONCILIATION"
            } else if direction == "buy" && exit_symbols.contains(symbol) {
                "ACTIVE_EXIT_EXISTS"
            } else if direction == "buy" && candidate["riskStatus"] == "risk" {
                "HARD_RISK_VETO"
            } else if let Some(reason) = risks::trading_constraint(d, symbol, &date, direction)? {
                reason
            } else if direction == "sell"
                && d.holding_details_for(symbol)?["availableQty"]
                    .as_u64()
                    .unwrap_or(0)
                    == 0
            {
                "SELLABLE_UNKNOWN"
            } else if tasks.iter().any(|t| {
                t["symbol"] == symbol
                    && t["direction"] == direction
                    && t["createdAt"]
                        .as_u64()
                        .is_some_and(|at| d.now.saturating_sub(at) < cooldown)
            }) {
                "COOLDOWN_SUPPRESSED"
            } else if tasks.iter().any(|t| {
                t["symbol"] == symbol
                    && t["direction"] == direction
                    && matches!(t["status"].as_str(), Some("pending" | "reminded"))
                    && t["validUntil"].as_u64().is_some_and(|at| at > d.now)
            }) {
                "ACTIVE_TASK_EXISTS"
            } else if direction == "buy" && count >= daily_limit {
                "DAILY_LIMIT"
            } else if published.len() >= 500 {
                "PUBLISH_CAPACITY"
            } else {
                ""
            };
            if !reason.is_empty() {
                *rejects.entry(reason.to_owned()).or_default() += 1;
                continue;
            }
            let task_key = id();
            let budget = crate::budget::reserve(d.tx, &task_key, &raw, d.now)?;
            if budget.blocked {
                *rejects.entry(budget.reason.to_owned()).or_default() += 1;
                continue;
            }
            items.push(candidate.clone());
            let task = create_task(d, &key, &task_key, &raw, partial, budget.quantity)?;
            published.push(task["taskId"].clone());
            if direction == "buy" {
                count += 1;
            }
            events.push(event(d, "task_update", task.clone())?);
            events.push(event(d, "reminder", task)?);
        }
    }
    let status = match error {
        Some("SCAN_PAUSED") => "suppressed",
        Some("SCAN_IN_PROGRESS" | "SCAN_OUTSIDE_WINDOW") if trigger == "scheduled" => "skipped",
        Some("SCAN_IN_PROGRESS" | "SCAN_OUTSIDE_WINDOW") => "rejected",
        Some(_) => "failed",
        None if partial => "partial",
        None => "succeeded",
    };
    let slot_label = scheduled_slot.map(scheduled_slot_label);
    let data_as_of = frozen
        .and_then(|f| f["asOf"].as_u64())
        .filter(|at| *at <= d.now);
    let scan = json!({"scanId":key,"trigger":trigger,"status":status,"profile":analysis["profile"],"tradeDate":date,"slot":slot_label,"requestedAt":requested,"startedAt":requested,"finishedAt":d.now,"dataAsOf":data_as_of,"dataStatus":if error.is_none(){"fresh"}else{"unknown"},"strategy":strategy,"degraded":partial||error.is_some(),"publishedTaskIds":published,"error":error.map(|code|json!({"code":code,"message":scan_error_message(code,supplied)})),"rejectionSummary":rejects.into_iter().map(|(reason,count)|json!({"reasonCode":reason,"count":count})).collect::<Vec<_>>()});
    let preflight_rejection = matches!(
        error,
        Some("SCAN_OUTSIDE_WINDOW" | "SCAN_IN_PROGRESS" | "SCAN_PAUSED")
    ) && supplied.is_none_or(|reply| {
        reply["error"]
            .as_str()
            .is_none_or(|code| Some(code) == error)
            && (reply["quotes"].is_null() || reply["quotes"].as_array().is_some_and(Vec::is_empty))
            && [
                "frozen",
                "quoteFeed",
                "quantObservationBatch",
                "quantObservationError",
                "holdingRuleBatch",
            ]
            .into_iter()
            .all(|field| reply.get(field).is_none())
    });
    if let Some(token) = baseline.and_then(|b| b["scanLease"].as_str()) {
        d.tx.execute("DELETE FROM objects WHERE collection='market_meta' AND object_id='scan_lease' AND json_extract(body,'$.token')=?1",[token])?;
    }
    publish_scan(d, events, &scan, items, p.cloned(), preflight_rejection)?;
    if let Some(code) = error {
        Ok(Err((code, json!({"scan":scan}))))
    } else if partial {
        Ok(Err(("SCAN_PARTIAL", json!({"scan":scan}))))
    } else {
        Ok(Ok(scan))
    }
}

fn scan_error_message(code: &'static str, supplied: Option<&Value>) -> &'static str {
    let response = supplied.unwrap_or(&Value::Null);
    match code {
        "SCAN_FAILED" if response["frozen"]["manifest"]["schemaVersion"] == 3 => {
            "正式策略资料待核验。"
        }
        "SCAN_OUTSIDE_WINDOW" if response["tradingCalendar"]["state"] == "closed" => {
            "已确认休市，本轮未扫描。"
        }
        "SCAN_OUTSIDE_WINDOW" => "当前不在扫描时段。",
        "DATA_UNAVAILABLE" => match (
            response["productionReason"].as_str(),
            response["prepareReason"].as_str(),
        ) {
            (Some("TRADING_CALENDAR_UNAVAILABLE"), _) => "交易日历待核验，本轮未扫描。",
            (Some("NO_PRODUCTION_APPROVAL"), _) => "暂无已准入的正式策略。",
            (Some("PRODUCTION_EVALUATION_FAILED"), _) => "正式策略计算失败。",
            (_, Some("KERNEL_PREPARE_AUTH")) => "量化服务未通过连接验证。",
            (_, Some("KERNEL_PREPARE_TIMEOUT")) => "量化服务准备超时。",
            (_, Some("KERNEL_PREPARE_INVALID_RESPONSE")) => "量化服务返回的数据无法读取。",
            (_, Some("KERNEL_PREPARE_HTTP_ERROR")) => "量化服务未能准备本轮扫描。",
            (_, Some("KERNEL_PREPARE_UNREACHABLE")) => "无法连接量化服务。",
            (_, Some("KERNEL_PREPARE_NOT_CONFIGURED" | "KERNEL_PREPARE_CONFIGURATION")) => {
                "量化服务连接配置不可用。"
            }
            _ => "本轮数据不可用。",
        },
        _ => code,
    }
}

fn scheduled_slot_label(slot: u64) -> String {
    DateTime::<Utc>::from_timestamp_millis((slot * SCAN_INTERVAL_MS) as i64)
        .expect("scheduled slot fits the system clock")
        .with_timezone(&chrono_tz::Asia::Shanghai)
        .format("%H:%M")
        .to_string()
}

fn publish_scan(
    d: &Domain<'_, '_>,
    events: &mut Vec<Value>,
    scan: &Value,
    items: Vec<Value>,
    mut evidence: Option<Value>,
    preflight: bool,
) -> anyhow::Result<()> {
    validate_definition("Scan", scan)?;
    let key = scan["scanId"].as_str().context("scan ID")?;
    if let Some(reply) = evidence
        .as_mut()
        .and_then(|value| value.get_mut("response"))
        .and_then(Value::as_object_mut)
    {
        reply.remove("quantObservationBatch");
        reply.remove("quantObservationError");
        reply.remove("holdingRuleBatch");
    }
    d.put(
        "market_scans",
        key,
        &json!({"scan":scan,"items":items,"evidence":evidence,"serverEpoch":d.epoch}),
    )?;
    crate::quant_observations::record_analysis_scan(d, key, preflight)?;
    d.put("market_meta", "latest_scan", &json!({"scanId":key}))?;
    if let Some(requested) = scan["requestedAt"]
        .as_u64()
        .filter(|_| scan["trigger"] == "scheduled")
    {
        d.put(
            "market_meta",
            "last_scheduled_scan_slot",
            &json!({"slot":requested / SCAN_INTERVAL_MS,"epoch":d.epoch}),
        )?;
    }
    d.bump()?;
    events.push(event(d, "scan_result", scan.clone())?);
    Ok(())
}

fn persist_quotes(
    tx: &rusqlite::Transaction<'_>,
    response: &Value,
    now: u64,
) -> anyhow::Result<()> {
    let delivery = response["quoteFeed"]["delivery"].as_object();
    if let Some(delivery) = delivery {
        for symbol in delivery.keys() {
            tx.execute("UPDATE objects SET body=json_set(body,'$.status','stale') WHERE collection='market_quotes'
                AND object_id=?1 AND json_extract(body,'$.status')='fresh'", [symbol])?;
        }
    } else if response["error"] == "DATA_UNAVAILABLE" {
        tx.execute("UPDATE objects SET body=json_set(body,'$.status','stale') WHERE collection='market_quotes'
            AND json_extract(body,'$.timeBasis') IS NOT NULL AND json_extract(body,'$.status')='fresh'", [])?;
    }
    let Some(quotes) = response["quotes"].as_array() else {
        return Ok(());
    };
    ensure!(quotes.len() <= 1000, "quote limit");
    let mut watch_updates = 0;
    for quote in quotes {
        let symbol = quote["symbol"].as_str().context("quote symbol")?;
        validate_definition("Symbol", &quote["symbol"])?;
        validate_definition("Source", &quote["source"])?;
        let price = quote["price"].as_str().context("quote price")?;
        let valid_price = price.len() <= 48
            && !price.is_empty()
            && price.split('.').count() <= 2
            && price.bytes().all(|c| c.is_ascii_digit() || c == b'.')
            && price.bytes().any(|c| matches!(c, b'1'..=b'9'))
            && !price.starts_with('.')
            && !price.ends_with('.');
        ensure!(valid_price, "invalid quote price");
        let at = quote["asOf"].as_u64().context("quote timestamp")?;
        ensure!(
            at <= now && quote["source"]["asOf"] == at,
            "invalid quote time"
        );
        let mut quote = quote.clone();
        ensure!(
            quote["timeBasis"].is_null()
                || matches!(
                    quote["timeBasis"].as_str(),
                    Some("snapshot" | "trade" | "settlement")
                ),
            "invalid quote time basis"
        );
        // The upstream batch clock can timestamp a displayable reference price.
        // An exact per-security trade clock is required to use it in valuation.
        let fresh = if delivery.is_some() || !quote["timeBasis"].is_null() {
            quote["timeBasis"] == "trade"
                && quotes::session(response) == "open"
                && response["quoteFeed"]["delivery"][symbol] == "available"
                && quotes::current_in_session(at, now)
                && !quotes::verified_suspension(tx, symbol, now)?
        } else {
            now - at <= quotes::QUOTE_TTL_MS
        };
        quote["status"] = json!(if fresh { "fresh" } else { "stale" });
        let stored = tx.execute("INSERT INTO objects VALUES ('market_quotes',?1,?2) ON CONFLICT(collection,object_id) DO UPDATE SET body=excluded.body WHERE json_extract(objects.body,'$.asOf')<=?3",params![symbol,quote.to_string(),at])?;
        if stored > 0 {
            watch_updates += tx.execute(
                "UPDATE objects SET body=json_set(body,'$.dataAsOf',?2)
                 WHERE collection='watchlist' AND object_id=?1 AND coalesce(json_extract(body,'$.dataAsOf'),-1)<?2",
                params![symbol, at],
            )?;
        }
    }
    if watch_updates > 0 {
        tx.execute(
            "UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='snapshot_seq'",
            [],
        )?;
    }
    Ok(())
}

fn event(d: &Domain<'_, '_>, kind: &str, data: Value) -> anyhow::Result<Value> {
    let key = id();
    Ok(
        json!({"v":2,"kind":kind,"id":key,"dedupeKey":format!("{kind}:{key}"),"ts":d.now,"serverEpoch":d.epoch,"snapshotSeq":d.seq()?,"data":data}),
    )
}

fn create_task(
    d: &Domain<'_, '_>,
    scan: &str,
    key: &str,
    raw: &Value,
    degraded: bool,
    quantity: Option<u64>,
) -> anyhow::Result<Value> {
    let c = &raw["candidate"];
    let critical = raw["direction"] == "sell" || raw["direction"] == "risk";
    let level = if critical { "critical" } else { "info" };
    let source = raw.get("source").cloned();
    if let Some(s) = &source {
        validate_definition("Source", s)?;
    }
    let task = json!({"taskId":key,"scanId":scan,"sourceEventKey":id(),"riskEventKey":null,"eventRevision":null,"symbol":c["symbol"],"name":c["name"],"direction":raw["direction"],"level":level,"status":"pending","version":1,"actionVersion":1,"score":c["score"],"confidence":"low","degraded":degraded,"dataAsOf":c["dataAsOf"],"strategies":c["strategies"],"reasons":{"quant":"已通过固定版本量化规则；未配置计划资金时仅作无数量观察。","ai":null,"verificationStatus":"unverified","sources":source.into_iter().collect::<Vec<_>>(),"riskEvidence":[],"risks":["外部事实尚未核验；账户现金未知，不提供可执行数量。"]},"stopPrice":null,"targetPrice":null,"entryZone":null,"createdAt":d.now,"validUntil":d.now+1_800_000,"snoozeUntil":null,"nextReminderAt":if critical{json!(d.now+300_000)}else{Value::Null},"remindedCount":1,"lastRemindedAt":d.now,"levelReason":"固定量化规则","currentReminder":{"reminderId":id(),"scheduledAt":d.now,"displayUntil":d.now+60_000}});
    let mut task = task;
    if let Some(quantity) = quantity {
        task["reasons"]["quant"]=json!(format!("固定版本量化信号；本地计划风险预算允许 {quantity} 单位。这是计划预算，不代表账户现金或券商可买量。"));
        task["stopPrice"] = raw["sizing"]["stopPrice"].clone();
    }
    store_new_task(d, &task)?;
    Ok(task)
}

fn formal_exits(d: &Domain<'_, '_>) -> anyhow::Result<BTreeMap<String, Value>> {
    Ok(d.list("formal_exit_events")?
        .into_iter()
        .filter(|event| event["serverEpoch"] == d.epoch && event["state"] == "active")
        .filter_map(|event| {
            event["eventId"]
                .as_str()
                .map(|id| (id.to_owned(), event.clone()))
        })
        .collect())
}

fn formal_exit_key(
    d: &Domain<'_, '_>,
    signal: &ExitSignal,
    frozen: &Value,
) -> anyhow::Result<String> {
    let specs = frozen["manifest"]["strategyHashes"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|spec| {
            spec["id"]
                .as_str()
                .is_some_and(|id| signal.strategy_ids.iter().any(|item| item == id))
        })
        .map(|spec| {
            json!({
                "id":spec["id"],"codeHash":spec["codeHash"],
                "parametersHash":spec["parametersHash"],"dependenciesHash":spec["dependenciesHash"]
            })
        })
        .collect::<Vec<_>>();
    crate::contract::digest(&json!([
        d.epoch,
        signal.symbol,
        signal.strategy_ids,
        specs,
        signal.rule_id,
        signal.rule_version
    ]))
}

#[allow(clippy::too_many_arguments)]
fn persist_formal_exit(
    d: &Domain<'_, '_>,
    key: &str,
    scan: &str,
    signal: &ExitSignal,
    holding: &Value,
    restriction: &str,
    available: Option<u64>,
    executable: bool,
    last_task_id: Option<&str>,
) -> anyhow::Result<()> {
    let previous = d.get("formal_exit_events", key)?;
    let first_seen = previous
        .as_ref()
        .and_then(|event| event["firstSeenAt"].as_u64())
        .unwrap_or(d.now);
    let last_task_id = last_task_id
        .map(|id| json!(id))
        .or_else(|| {
            previous
                .as_ref()
                .and_then(|event| event.get("lastTaskId").cloned())
        })
        .unwrap_or(Value::Null);
    let event = json!({
        "eventId":key,"serverEpoch":d.epoch,"symbol":signal.symbol,
        "strategyIds":signal.strategy_ids,"ruleId":signal.rule_id,"ruleVersion":signal.rule_version,
        "condition":signal.condition,"state":"active","latch":false,
        "barEndMs":signal.bar_end_ms,"barAvailableAtMs":signal.bar_available_at_ms,
        "firstSeenAt":first_seen,"lastSeenAt":d.now,"lastScanId":scan,
        "restriction":if restriction.is_empty() {Value::Null} else {json!(restriction)},
        "availableQty":available,"executable":executable,
        "holdingRevision":holding["positionRevision"],"holdingQty":holding["qty"],
        "lastTaskId":last_task_id,
    });
    d.put("formal_exit_events", key, &event)?;
    Ok(())
}

fn exit_risks(executable: bool, restriction: &str) -> Vec<String> {
    if executable {
        let mut risks =
            vec!["退出条件成立；请核对可卖数量、价格与执行条件。本应用不会自动下单。".to_owned()];
        if !restriction.is_empty() {
            risks.insert(0, exit_restriction_text(restriction).to_owned());
        }
        risks
    } else {
        vec![
            exit_restriction_text(restriction).to_owned(),
            "退出条件成立但当前不可执行；受限数量不催卖。".to_owned(),
        ]
    }
}

fn exit_evidence(
    d: &Domain<'_, '_>,
    signal: &ExitSignal,
    restriction: &str,
    available: Option<u64>,
    executable: bool,
) -> Value {
    json!({
        "ruleId":signal.rule_id, "ruleVersion":signal.rule_version, "condition":signal.condition,
        "decisionClock":signal.decision_clock, "barEndMs":signal.bar_end_ms,
        "barAvailableAtMs":signal.bar_available_at_ms, "strategyId":signal.strategy_id,
        "executable":executable,
        "restriction":if restriction.is_empty() {Value::Null} else {json!(restriction)},
        "availableQty":available, "reviewAt":d.exit_review_at(&signal.symbol, d.now),
    })
}

fn update_exit_task(
    d: &Domain<'_, '_>,
    task_id: &str,
    signal: &ExitSignal,
    restriction: &str,
    available: Option<u64>,
    executable: bool,
    expire: bool,
) -> anyhow::Result<Option<Value>> {
    let Some(mut task) = d.get("tasks", task_id)? else {
        return Ok(None);
    };
    task["exit"] = exit_evidence(d, signal, restriction, available, executable);
    task["dataAsOf"] = signal
        .quote
        .as_ref()
        .map_or(json!(signal.bar_available_at_ms), |quote| {
            json!(quote.as_of)
        });
    task["reasons"]["risks"] = json!(exit_risks(executable, restriction));
    task["level"] = json!(if executable { "critical" } else { "warn" });
    task["levelReason"] = json!(if executable {
        "独立退出条件成立"
    } else {
        "退出受限，待复核"
    });
    if !executable || expire {
        task["status"] = json!("expired");
        task["currentReminder"] = Value::Null;
        task["nextReminderAt"] = Value::Null;
        task["snoozeUntil"] = Value::Null;
    }
    task["version"] = json!(task["version"].as_u64().unwrap_or(1) + 1);
    task["actionVersion"] = json!(task["actionVersion"].as_u64().unwrap_or(1) + 1);
    d.put("tasks", task_id, &task)?;
    Ok(Some(task))
}

fn resolve_formal_exits_for_symbol(
    d: &Domain<'_, '_>,
    events: &mut Vec<Value>,
    symbol: &str,
    resolution: &str,
) -> anyhow::Result<()> {
    for (id, mut row) in formal_exits(d)? {
        if row["symbol"].as_str() != Some(symbol) || row["latch"] == true {
            continue;
        }
        row["state"] = json!("resolved");
        row["executable"] = json!(false);
        row["availableQty"] = json!(0);
        row["resolvedAt"] = json!(d.now);
        row["resolution"] = json!(resolution);
        d.put("formal_exit_events", &id, &row)?;
        if let Some(task_id) = row["lastTaskId"].as_str() {
            if let Some(task) = d.get("tasks", task_id)? {
                let mut task = task;
                task["exit"]["executable"] = json!(false);
                task["exit"]["availableQty"] = json!(0);
                task["status"] = json!("expired");
                task["currentReminder"] = Value::Null;
                task["nextReminderAt"] = Value::Null;
                task["snoozeUntil"] = Value::Null;
                task["version"] = json!(task["version"].as_u64().unwrap_or(1) + 1);
                task["actionVersion"] = json!(task["actionVersion"].as_u64().unwrap_or(1) + 1);
                d.put("tasks", task_id, &task)?;
                events.push(event(d, "task_update", task)?);
            }
        }
    }
    Ok(())
}

fn resolve_refuted_formal_exits(d: &Domain<'_, '_>, frozen: &Value) -> anyhow::Result<()> {
    let Some(rows) = frozen["manifest"]["researchDecisions"].as_array() else {
        return Ok(());
    };
    let Some(specs) = strategy_specs(&frozen["manifest"]) else {
        return Ok(());
    };
    let mut refuted = std::collections::BTreeMap::new();
    for row in rows {
        let (Some(symbol), Some(strategy)) = (row["symbol"].as_str(), row["strategyId"].as_str())
        else {
            continue;
        };
        let Some(spec) = specs.get(strategy) else {
            continue;
        };
        let Some(end) = row["bar"]["endMs"].as_u64() else {
            continue;
        };
        let available = row["bar"]["availableAtMs"].as_u64();
        // A later bar may only refute an exit when the bar is complete, was
        // actually available at this scan, and still carries the verified
        // strategy version whose rule fired.
        if row["bar"]["status"] != "complete"
            || row["exit"]["value"] != false
            || available.is_none_or(|at| at > d.now || at < end)
            || spec.code_hash != row["codeHash"].as_str().unwrap_or_default()
            || spec.parameters_hash != row["parametersHash"].as_str().unwrap_or_default()
            || spec.dependencies_hash != row["dependenciesHash"].as_str().unwrap_or_default()
        {
            continue;
        }
        refuted
            .entry((symbol.to_owned(), strategy.to_owned()))
            .and_modify(|prior: &mut u64| *prior = (*prior).max(end))
            .or_insert(end);
    }
    for (id, mut event) in formal_exits(d)? {
        if event["latch"] == true {
            continue;
        }
        let symbol = event["symbol"].as_str().unwrap_or_default();
        let strategies = event["strategyIds"].as_array().cloned().unwrap_or_default();
        let ended = event["barEndMs"].as_u64().unwrap_or(0);
        // Every verified reason must be refuted by a later trusted bar. One
        // strategy turning false must not release another still-valid reason.
        let all_refuted = !strategies.is_empty()
            && strategies.iter().filter_map(Value::as_str).all(|strategy| {
                refuted
                    .get(&(symbol.to_owned(), strategy.to_owned()))
                    .is_some_and(|end| *end > ended)
            });
        if all_refuted {
            event["state"] = json!("resolved");
            event["executable"] = json!(false);
            event["availableQty"] = json!(0);
            event["resolvedAt"] = json!(d.now);
            event["resolution"] = json!("trusted_rule_disproved");
            d.put("formal_exit_events", &id, &event)?;
        }
    }
    Ok(())
}

fn exit_restriction(
    holding: &Value,
    strategies: &[String],
    specs: &std::collections::BTreeMap<String, StrategySpec>,
) -> (&'static str, Option<u64>) {
    let Some(available) = holding["availableQty"].as_u64() else {
        return ("sellable_unknown", None);
    };
    // A security-level available quantity is not evidence that these shares
    // belong to the strategy whose rule fired. Require every referenced
    // strategy version to be present in the verified manifest and require an
    // exact origin match (group, ID and all three hashes) on the partition.
    let verified = strategies
        .iter()
        .map(|id| specs.get(id))
        .collect::<Option<Vec<_>>>();
    let Some(verified) = verified else {
        return ("strategy_origin_unverified", None);
    };
    let Some(partitions) = holding["partitions"]
        .as_array()
        .filter(|partitions| !partitions.is_empty())
    else {
        return ("strategy_origin_unverified", None);
    };
    let matching = partitions
        .iter()
        .filter(|partition| {
            verified
                .iter()
                .any(|spec| spec.matches_origin(&partition["origin"]))
        })
        .collect::<Vec<_>>();
    let assigned = matching
        .iter()
        .filter_map(|partition| partition["qty"].as_u64())
        .sum::<u64>();
    if assigned == 0 {
        // The exit belongs to a strategy version, but no confirmed partition
        // carries that exact version or a verified origin. Never fall back to
        // the whole account position.
        return ("strategy_origin_unverified", None);
    }
    let sellable = matching
        .iter()
        .filter(|partition| {
            partition["restrictions"]
                .as_array()
                .is_some_and(Vec::is_empty)
        })
        .filter_map(|partition| partition["qty"].as_u64())
        .sum::<u64>()
        .min(available);
    if sellable == 0 {
        let mut freeze = false;
        let mut settlement = false;
        for partition in &matching {
            for restriction in partition["restrictions"].as_array().into_iter().flatten() {
                match restriction["kind"].as_str() {
                    Some("freeze") => freeze = true,
                    Some("settlement") => settlement = true,
                    _ => {}
                }
            }
        }
        return if freeze {
            ("frozen", Some(0))
        } else if settlement {
            ("t_plus_one", Some(0))
        } else {
            ("sellable_unknown", Some(0))
        };
    }
    if sellable < assigned {
        ("partially_sellable", Some(sellable))
    } else {
        ("", Some(sellable))
    }
}

fn exit_restriction_text(restriction: &str) -> &'static str {
    match restriction {
        "t_plus_one" => "今日新增部分受 T+1 限制，当前不可卖的数量不催卖；请在下一实际交易日复核。",
        "frozen" => "部分数量存在其他冻结，未解除前不催卖该数量。",
        "trading_suspended" => "该证券当前交易受限，退出条件仍保留，待停复牌与可卖状态核实后复核。",
        "partially_sellable" => "仅部分数量可卖；退出条件覆盖整体持仓，受限部分不催卖。",
        "strategy_origin_unverified" => "该退出规则对应的策略持仓归属无法核实；不使用其他策略或未分配的数量，暂不提供可执行卖出数量。",
        "quote_unavailable" => "退出条件成立，但没有可核验的当前原始报价；只保留退出事实，不催卖。",
        "market_closed" => "当前不在可执行交易时段；退出条件仍保留，待交易时段重新核验报价和可卖量。",
        _ => "可卖数量或交易状态尚未核实，暂不提供可执行卖出数量。",
    }
}

#[allow(clippy::too_many_arguments)]
fn create_exit_task(
    d: &Domain<'_, '_>,
    scan: &str,
    key: &str,
    signal: &ExitSignal,
    restriction: &str,
    available: Option<u64>,
    executable: bool,
    degraded: bool,
) -> anyhow::Result<Value> {
    let sources: Vec<Value> = signal
        .quote
        .as_ref()
        .map(|quote| {
            json!({"name":quote.source["name"],"ref":quote.source["ref"],"asOf":quote.as_of})
        })
        .into_iter()
        .collect();
    let risks = if executable {
        let mut risks =
            vec!["退出条件成立；请核对可卖数量、价格与执行条件。本应用不会自动下单。".to_owned()];
        if !restriction.is_empty() {
            risks.insert(0, exit_restriction_text(restriction).to_owned());
        }
        risks
    } else {
        vec![
            exit_restriction_text(restriction).to_owned(),
            "退出条件成立但当前不可执行；受限数量不催卖。".to_owned(),
        ]
    };
    let data_as_of = signal
        .quote
        .as_ref()
        .map_or(signal.bar_available_at_ms, |quote| quote.as_of);
    let task = json!({
        "taskId":key, "scanId":scan, "sourceEventKey":id(), "riskEventKey":null, "eventRevision":null,
        "symbol":signal.symbol, "name":signal.name, "direction":"sell",
        "level":if executable {"critical"} else {"warn"}, "status":"pending",
        "version":1, "actionVersion":1, "score":null, "confidence":"medium", "degraded":degraded,
        "dataAsOf":data_as_of, "strategies":signal.strategy_ids,
        "reasons":{
            "quant":format!("已批准日线策略的独立退出条件成立（{} v{}，策略 {}）。本提示只说明退出条件成立，不代表已经卖出。",
                signal.rule_id, signal.rule_version, signal.strategy_ids.join("、")),
            "ai":null, "verificationStatus":"verified", "sources":sources, "riskEvidence":[], "risks":risks,
        },
        "exit":{
            "ruleId":signal.rule_id, "ruleVersion":signal.rule_version, "condition":signal.condition,
            "decisionClock":signal.decision_clock, "barEndMs":signal.bar_end_ms,
            "barAvailableAtMs":signal.bar_available_at_ms, "strategyId":signal.strategy_id,
            "executable":executable,
            "restriction":if restriction.is_empty() {Value::Null} else {json!(restriction)},
            "availableQty":available, "reviewAt":d.exit_review_at(&signal.symbol, d.now),
        },
        "stopPrice":null, "targetPrice":null, "entryZone":null,
        "createdAt":d.now, "validUntil":d.now+1_800_000, "snoozeUntil":null,
        // An ordinary exit sends at most the creation reminder; the app or a
        // new verified decision stage is required for anything further.
        "nextReminderAt":Value::Null,
        "remindedCount":if executable {1} else {0},
        "lastRemindedAt":if executable {json!(d.now)} else {Value::Null},
        "levelReason":if executable {"独立退出条件成立"} else {"退出受限，待复核"},
        "currentReminder":if executable {
            json!({"reminderId":id(),"scheduledAt":d.now,"displayUntil":d.now+60_000})
        } else {Value::Null},
    });
    store_new_task(d, &task)?;
    Ok(task)
}

fn store_new_task(d: &Domain<'_, '_>, task: &Value) -> anyhow::Result<()> {
    validate_definition("Task", task)?;
    let key = task["taskId"].as_str().context("task ID")?;
    d.put("tasks", key, task)?;
    d.bump()?;
    let action_id = id();
    let action = json!({"actionId":action_id,"taskId":key,"type":"created","at":d.now,"clientMsgId":null,"reason":null,"taskVersion":1,"actionVersion":1});
    d.tx.execute(
        "INSERT INTO task_actions VALUES (?1,?2,?3,?4)",
        params![action_id, key, d.now, action.to_string()],
    )?;
    Ok(())
}

pub(crate) fn decimal_cmp(a: &str, b: &str) -> Ordering {
    let (ai, af) = a.split_once('.').unwrap_or((a, ""));
    let (bi, bf) = b.split_once('.').unwrap_or((b, ""));
    ai.len().cmp(&bi.len()).then(ai.cmp(bi)).then_with(|| {
        let width = af.len().max(bf.len());
        af.bytes()
            .chain(std::iter::repeat(b'0'))
            .take(width)
            .cmp(bf.bytes().chain(std::iter::repeat(b'0')).take(width))
    })
}

fn scheduled_slot_finished(
    db: &rusqlite::Connection,
    epoch: &str,
    slot: u64,
) -> anyhow::Result<bool> {
    Ok(db.query_row(
        "SELECT EXISTS(
           SELECT 1 FROM objects
           WHERE collection='market_meta' AND object_id='last_scheduled_scan_slot'
             AND json_extract(body,'$.epoch')=?1 AND json_extract(body,'$.slot')>=?2
         )",
        params![epoch, slot],
        |row| row.get(0),
    )?)
}

fn paired_scan_scope(db: &rusqlite::Connection) -> anyhow::Result<Option<(String, u64)>> {
    Ok(db
        .query_row("SELECT pair_id,fence FROM peer", [], |row| {
            Ok((row.get(0)?, row.get(1)?))
        })
        .optional()?)
}

fn scheduled_tick(now: u64) -> Option<(u64, bool)> {
    let at = now / SCAN_INTERVAL_MS * SCAN_INTERVAL_MS;
    market_time(at).1.then_some((at, now - at < 2_000))
}

fn record_missed_slot(
    d: &Domain<'_, '_>,
    events: &mut Vec<Value>,
    scheduled_at: u64,
) -> anyhow::Result<()> {
    let slot = scheduled_at / SCAN_INTERVAL_MS;
    let analysis = d.analysis()?;
    if analysis["paused"] == true || scheduled_slot_finished(d.tx, d.epoch, slot)? {
        return Ok(());
    }
    let label = scheduled_slot_label(slot);
    let scan = json!({
        "scanId":id(),"trigger":"scheduled","status":"skipped","profile":analysis["profile"],
        "tradeDate":market_time(scheduled_at).0,"slot":label,
        "requestedAt":scheduled_at,"startedAt":null,"finishedAt":d.now,
        "dataAsOf":null,"dataStatus":"unknown","strategy":{"total":0,"succeeded":0,"failed":[]},
        "degraded":true,"publishedTaskIds":[],
        "error":{"code":"SCAN_SCHEDULE_MISSED","message":format!("{label} 轮次错过计划时点，已跳过。")},
        "rejectionSummary":[]
    });
    publish_scan(
        d,
        events,
        &scan,
        vec![],
        Some(json!({"response":{"schedulerReason":"SLOT_START_NOT_OBSERVED"}})),
        true,
    )
}

pub async fn run(
    db: Arc<Mutex<Storage>>,
    config: Option<KernelConfig>,
    shutdown: tokio::sync::watch::Receiver<bool>,
) -> anyhow::Result<()> {
    tokio::join!(
        maintain_worker(
            "quotes",
            db.clone(),
            config.clone(),
            shutdown.clone(),
            run_quotes
        ),
        maintain_worker("scheduled_scan", db, config, shutdown, run_scheduled),
    );
    Ok(())
}

async fn maintain_worker<Fut>(
    worker: &'static str,
    db: Arc<Mutex<Storage>>,
    config: Option<KernelConfig>,
    mut shutdown: tokio::sync::watch::Receiver<bool>,
    run: impl Fn(Arc<Mutex<Storage>>, Option<KernelConfig>, tokio::sync::watch::Receiver<bool>) -> Fut,
) where
    Fut: Future<Output = anyhow::Result<()>> + Send,
{
    while !*shutdown.borrow() {
        if run(db.clone(), config.clone(), shutdown.clone())
            .await
            .is_ok()
        {
            return;
        }
        tracing::warn!(worker, "market worker encountered a local error; retrying");
        tokio::select! {
            _ = shutdown.changed() => return,
            _ = tokio::time::sleep(Duration::from_secs(1)) => {}
        }
    }
}

async fn run_quotes(
    db: Arc<Mutex<Storage>>,
    config: Option<KernelConfig>,
    mut shutdown: tokio::sync::watch::Receiver<bool>,
) -> anyhow::Result<()> {
    let mut quotes = tokio::time::interval(Duration::from_secs(30));
    quotes.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    loop {
        tokio::select! {_=shutdown.changed()=>return Ok(()),_=quotes.tick()=>{
            let now=crate::commands::now_ms();
            let request={let db=db.lock().map_err(|_|anyhow::anyhow!("storage mutex"))?;capture(&db,&json!({"cmd":"quotes"}),now)?};
            let prepared=tokio::select!{_=shutdown.changed()=>return Ok(()),p=prepare(config.as_ref(),request)=>p};
            let mut db=db.lock().map_err(|_|anyhow::anyhow!("storage mutex"))?;
            if db.recovery_active()? || prepared["request"]["epoch"] != db.identity.server_epoch {continue;}
            let now=crate::commands::now_ms();
            persist_quote_cycle_rules(&mut db, &prepared, now)?;
            history::persist(&db.connection, &db.identity.server_epoch, &prepared["response"], now)?;
            let tx=db.connection.transaction()?;
            if persist_quotes(&tx,&prepared["response"],now).is_ok(){tx.commit()?;}
        }}
    }
}

fn persist_quote_cycle_rules(db: &mut Storage, prepared: &Value, now: u64) -> anyhow::Result<()> {
    let request = &prepared["request"];
    let (Some(batch), Some(cutoff)) = (
        prepared["response"].get("holdingRuleBatch"),
        request["requestedAt"].as_u64(),
    ) else {
        return Ok(());
    };
    if request["holdingRuleVersion"] != HOLDING_RULE_VERSION || request["data"]["cmd"] != "quotes" {
        return Ok(());
    }
    let epoch = db.identity.server_epoch.clone();
    let tx = db.connection.transaction()?;
    let pair: Option<String> = tx
        .query_row("SELECT pair_id FROM peer", [], |r| r.get(0))
        .optional()?;
    let Some(pair) = pair else {
        return Ok(());
    };
    let position_revision: u64 =
        tx.query_row("SELECT position_revision FROM domain_state", [], |row| {
            row.get(0)
        })?;
    if request["positionRevision"] != position_revision {
        return Ok(());
    }
    let domain = Domain {
        tx: &tx,
        data: &request["data"],
        pair: &pair,
        epoch: &epoch,
        recovering: false,
        now,
    };
    let updated = domain.persist_holding_rules(batch, cutoff, &request["objects"])?;
    let mut events = Vec::new();
    risks::retire_superseded(&domain, &market_time(now).0, &updated, &mut events)?;
    for event in events {
        crate::commands::enqueue_current(&tx, &event)?;
    }
    tx.commit()?;
    Ok(())
}

/// Observe the current slot without replaying older work. Quotes use a separate clock.
async fn run_scheduled(
    db: Arc<Mutex<Storage>>,
    config: Option<KernelConfig>,
    mut shutdown: tokio::sync::watch::Receiver<bool>,
) -> anyhow::Result<()> {
    let mut tick = tokio::time::interval(Duration::from_secs(1));
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut last_slot = None;
    loop {
        tokio::select! {
            _ = shutdown.changed() => return Ok(()),
            _ = tick.tick() => {}
        }
        let now = crate::commands::now_ms();
        let Some((at, on_time)) = scheduled_tick(now) else {
            continue;
        };
        if last_slot == Some(at) {
            continue;
        }
        last_slot = Some(at);
        let (scope, epoch, request) = {
            let db = db.lock().map_err(|_| anyhow::anyhow!("storage mutex"))?;
            let epoch = db.identity.server_epoch.clone();
            if db.recovery_active()?
                || scheduled_slot_finished(&db.connection, &epoch, at / SCAN_INTERVAL_MS)?
            {
                continue;
            }
            let Some(scope) = paired_scan_scope(&db.connection)? else {
                continue;
            };
            let request = if on_time {
                let data = json!({"cmd":"scan_now","clientMsgId":id(),"serverEpoch":epoch});
                let request = capture(&db, &data, now)?;
                if request["paused"] == true {
                    continue;
                }
                Some(request)
            } else {
                None
            };
            (scope, epoch, request)
        };
        let prepared = if let Some(request) = request {
            Some(tokio::select! {
                _ = shutdown.changed() => return Ok(()),
                prepared = prepare(config.as_ref(), request) => prepared
            })
        } else {
            None
        };
        let mut db = db.lock().map_err(|_| anyhow::anyhow!("storage mutex"))?;
        if db.identity.server_epoch != epoch
            || db.recovery_active()?
            || paired_scan_scope(&db.connection)?.as_ref() != Some(&scope)
        {
            continue;
        }
        let tx = db.connection.transaction()?;
        let data = prepared
            .as_ref()
            .map(|p| &p["request"]["data"])
            .unwrap_or(&Value::Null);
        let domain = Domain {
            tx: &tx,
            data,
            pair: &scope.0,
            epoch: &epoch,
            recovering: false,
            now: crate::commands::now_ms(),
        };
        let mut events = vec![];
        if let Some(prepared) = prepared.as_ref() {
            let _ = scan(&domain, &mut events, Some(prepared), "scheduled")?;
        } else {
            record_missed_slot(&domain, &mut events, at)?;
        }
        for event in events {
            crate::commands::enqueue_current(&tx, &event)?;
        }
        tx.commit()?;
    }
}

#[cfg(test)]
mod scheduled_tests {
    use super::*;

    #[test]
    fn weekend_is_outside_the_market_window() -> anyhow::Result<()> {
        let saturday = DateTime::parse_from_rfc3339("2026-11-14T14:35:00+08:00")?;
        let sunday = DateTime::parse_from_rfc3339("2026-11-15T14:35:00+08:00")?;
        let monday = DateTime::parse_from_rfc3339("2026-11-16T14:35:00+08:00")?;
        assert!(!market_time(saturday.timestamp_millis() as u64).1);
        assert!(!market_time(sunday.timestamp_millis() as u64).1);
        assert!(market_time(monday.timestamp_millis() as u64).1);
        Ok(())
    }

    #[tokio::test]
    async fn scheduled_prepare_keeps_an_auth_failure_distinct_from_unavailable_calendar(
    ) -> anyhow::Result<()> {
        use axum::{http::StatusCode, routing::post, Router};
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let config = KernelConfig {
            address: listener.local_addr()?,
            token_file: None,
        };
        let server = tokio::spawn(async move {
            axum::serve(
                listener,
                Router::new().route(
                    "/api/tickdesk/prepare",
                    post(|| async { StatusCode::UNAUTHORIZED }),
                ),
            )
            .await
        });
        let now =
            DateTime::parse_from_rfc3339("2026-11-16T09:30:00+08:00")?.timestamp_millis() as u64;
        let result = prepare(
            Some(&config),
            json!({"data":{"cmd":"scan_now"},"requestedAt":now}),
        )
        .await;
        server.abort();
        assert_eq!(result["response"]["error"], "DATA_UNAVAILABLE");
        assert_eq!(result["response"]["prepareReason"], "KERNEL_PREPARE_AUTH");
        Ok(())
    }

    fn run_slot(db: &mut Storage, now: u64, response: Value) -> anyhow::Result<Vec<Value>> {
        let data = json!({"cmd":"scan_now"});
        let request = capture(db, &data, now)?;
        let prepared = json!({"request":request,"response":response});
        let epoch = db.identity.server_epoch.clone();
        let tx = db.connection.transaction()?;
        let domain = Domain {
            tx: &tx,
            data: &data,
            pair: "fixture-pair",
            epoch: &epoch,
            recovering: false,
            now,
        };
        let mut events = vec![];
        let _ = scan(&domain, &mut events, Some(&prepared), "scheduled")?;
        tx.commit()?;
        Ok(events)
    }

    #[test]
    #[ignore = "requires Python scan fixture exported by verify_v3_frozen_wire.py"]
    fn v3_python_wire_keeps_matched_empty_and_unknown_distinct() -> anyhow::Result<()> {
        let fixture: Value =
            serde_json::from_slice(&std::fs::read(std::env::var("TICKDESK_V3_FROZEN_WIRE")?)?)?;
        let at = fixture["cutoff"].as_u64().context("fixture cutoff")?;
        for (name, terminal, reason) in [
            ("matched", "succeeded", Some("RESONANCE_INSUFFICIENT")),
            ("empty", "succeeded", None),
            (
                "missing",
                "failed",
                Some("CURRENT_EXECUTION_QUOTE_UNAVAILABLE"),
            ),
            ("wrong_direction", "failed", None),
        ] {
            let dir = tempfile::tempdir()?;
            let mut db = Storage::open(dir.path())?;
            let mut frozen = fixture[name].clone();
            if name == "wrong_direction" {
                frozen = fixture["matched"].clone();
                frozen["candidates"][0]["direction"] = json!("sell");
            }
            let result = run_slot(&mut db, at, json!({"frozen":frozen}))?;
            let scan = &result[0]["data"];
            assert_eq!(scan["status"], terminal, "{name} {scan}");
            let reasons = scan["rejectionSummary"].as_array().unwrap();
            if let Some(reason) = reason {
                assert!(
                    reasons.iter().any(|row| row["reasonCode"] == reason),
                    "{name}"
                );
            } else {
                assert!(reasons.is_empty(), "{name}");
            }
            assert_eq!(scan["publishedTaskIds"], json!([]), "{name}");
        }
        Ok(())
    }

    #[test]
    fn v3_missing_execution_quote_keeps_strategy_failure_and_its_reason() -> anyhow::Result<()> {
        let dir = tempfile::tempdir()?;
        let mut db = Storage::open(dir.path())?;
        let at =
            DateTime::parse_from_rfc3339("2026-11-16T10:00:00+08:00")?.timestamp_millis() as u64;
        let result = run_slot(
            &mut db,
            at,
            json!({"frozen":{
                "generation":"v3-fixture","generationAfter":"v3-fixture",
                "tradeDate":"2026-11-16","isTradingDay":true,"asOf":null,
                "strategies":{"total":1,"succeeded":0,"failed":["v3-fixture"]},
                "policy":{"minScore":"50"},"candidates":[],
                "manifest":{"schemaVersion":3,"strategyHashes":[{"id":"v3-fixture","direction":"buy","symbols":["000001.SZ"],
                    "group":"daily_swing","codeHash":"a".repeat(64),"parametersHash":"b".repeat(64),
                    "dependenciesHash":"c".repeat(64)}],
                    "strategyOutcomes":{"v3-fixture":{"status":"not_computable",
                        "reasons":["CURRENT_EXECUTION_QUOTE_UNAVAILABLE"]}}}
            }}),
        )?;
        let scan = &result[0]["data"];
        assert_eq!(scan["status"], "failed");
        assert_eq!(scan["error"]["code"], "SCAN_FAILED");
        assert!(scan["dataAsOf"].is_null());
        assert_eq!(
            scan["rejectionSummary"],
            json!([{"reasonCode":"CURRENT_EXECUTION_QUOTE_UNAVAILABLE","count":1}])
        );
        assert_eq!(scan["publishedTaskIds"], json!([]));
        Ok(())
    }

    #[test]
    fn scheduled_failure_records_slot_once_across_restart() -> anyhow::Result<()> {
        let dir = tempfile::tempdir()?;
        let at =
            DateTime::parse_from_rfc3339("2026-11-16T09:30:00+08:00")?.timestamp_millis() as u64;
        let mut db = Storage::open(dir.path())?;
        let unavailable = json!({"error":"DATA_UNAVAILABLE"});
        let first = run_slot(&mut db, at, unavailable.clone())?;
        assert_eq!(first.len(), 1);
        assert_eq!(first[0]["data"]["slot"], "09:30");
        assert_eq!(first[0]["data"]["status"], "failed");
        drop(db);
        let mut db = Storage::open(dir.path())?;
        assert!(run_slot(&mut db, at + 1_000, unavailable.clone())?.is_empty());
        let next = run_slot(&mut db, at + 300_000, unavailable)?;
        assert_eq!(next.len(), 1);
        assert_eq!(next[0]["data"]["slot"], "09:35");
        Ok(())
    }

    #[test]
    fn scan_ledger_and_event_distinguish_closed_calendar_from_missing_evidence(
    ) -> anyhow::Result<()> {
        let dir = tempfile::tempdir()?;
        let at =
            DateTime::parse_from_rfc3339("2026-11-16T09:25:00+08:00")?.timestamp_millis() as u64;
        let mut db = Storage::open(dir.path())?;
        let rows = [
            (
                at,
                json!({"state":"closed","date":"2026-11-16","observedAt":at-1,
                        "through":"2026-11-16","source":"fuyao"}),
                "skipped",
                "SCAN_OUTSIDE_WINDOW",
                "已确认休市，本轮未扫描。",
                None,
            ),
            (
                at + SCAN_INTERVAL_MS,
                json!({"state":"unknown","date":"2026-11-16","observedAt":null,
                                        "through":null,"source":null}),
                "failed",
                "DATA_UNAVAILABLE",
                "交易日历待核验，本轮未扫描。",
                Some("TRADING_CALENDAR_UNAVAILABLE"),
            ),
        ];
        for (now, calendar, status, code, message, reason) in rows {
            let mut response = json!({"error":code,"tradingCalendar":calendar});
            if let Some(reason) = reason {
                response["productionReason"] = json!(reason);
            }
            let events = run_slot(&mut db, now, response.clone())?;
            assert_eq!(events.len(), 1);
            let scan = &events[0]["data"];
            assert_eq!(scan["status"], status);
            assert_eq!(scan["error"], json!({"code":code,"message":message}));
            assert_eq!(scan["publishedTaskIds"], json!([]));
            let body: String = db.connection.query_row(
                "SELECT body FROM objects WHERE collection='market_scans' AND object_id=?1",
                [scan["scanId"].as_str().unwrap()],
                |row| row.get(0),
            )?;
            let body: Value = serde_json::from_str(&body)?;
            assert_eq!(body["evidence"]["response"], response);
        }
        Ok(())
    }

    #[test]
    fn restart_in_the_current_slot_records_a_skip_with_no_scan_start() -> anyhow::Result<()> {
        let dir = tempfile::tempdir()?;
        let at =
            DateTime::parse_from_rfc3339("2026-11-16T14:35:00+08:00")?.timestamp_millis() as u64;
        let mut db = Storage::open(dir.path())?;
        let now = at + 5_687;
        assert_eq!(scheduled_tick(at + 1_999), Some((at, true)));
        assert_eq!(scheduled_tick(now), Some((at, false)));
        let record = |db: &mut Storage| -> anyhow::Result<Vec<Value>> {
            let epoch = db.identity.server_epoch.clone();
            let tx = db.connection.transaction()?;
            let domain = Domain {
                tx: &tx,
                data: &Value::Null,
                pair: "fixture-pair",
                epoch: &epoch,
                recovering: false,
                now,
            };
            let mut events = vec![];
            record_missed_slot(&domain, &mut events, at)?;
            tx.commit()?;
            Ok(events)
        };
        let events = record(&mut db)?;
        assert_eq!(events.len(), 1);
        let shown = &events[0]["data"];
        assert_eq!(shown["status"], "skipped");
        assert_eq!(shown["slot"], "14:35");
        assert_eq!(shown["requestedAt"], at);
        assert_eq!(shown["finishedAt"], now);
        assert!(shown["startedAt"].is_null());
        assert_eq!(shown["error"]["code"], "SCAN_SCHEDULE_MISSED");
        assert_eq!(shown["publishedTaskIds"], json!([]));
        drop(db);
        let mut db = Storage::open(dir.path())?;
        assert!(record(&mut db)?.is_empty());
        let stored: String = db.connection.query_row(
            "SELECT body FROM objects WHERE collection='market_scans'",
            [],
            |row| row.get(0),
        )?;
        let stored: Value = serde_json::from_str(&stored)?;
        assert_eq!(&stored["scan"], shown);
        assert_eq!(
            stored["evidence"]["response"]["schedulerReason"],
            "SLOT_START_NOT_OBSERVED"
        );
        let next = run_slot(
            &mut db,
            at + SCAN_INTERVAL_MS,
            json!({"error":"DATA_UNAVAILABLE"}),
        )?;
        assert_eq!(next[0]["data"]["slot"], "14:40");
        Ok(())
    }

    #[tokio::test]
    async fn temporary_storage_failure_keeps_market_worker_running() -> anyhow::Result<()> {
        use axum::{routing::post, Json, Router};
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let config = KernelConfig {
            address: listener.local_addr()?,
            token_file: None,
        };
        let received = Arc::new(tokio::sync::Notify::new());
        let signal_quote = received.clone();
        let server = tokio::spawn(async move {
            let app = Router::new().route(
                "/api/tickdesk/prepare",
                post(move || async move {
                    signal_quote.notify_one();
                    Json(json!({"quotes":[]}))
                }),
            );
            axum::serve(listener, app).await
        });
        let directory = tempfile::tempdir()?;
        let storage = Arc::new(Mutex::new(Storage::open(directory.path())?));
        storage
            .lock()
            .unwrap()
            .connection
            .execute_batch("ALTER TABLE domain_state RENAME TO inaccessible_state")?;
        let (signal, shutdown) = tokio::sync::watch::channel(false);
        let worker = tokio::spawn(run(storage.clone(), Some(config), shutdown));
        tokio::time::sleep(Duration::from_millis(100)).await;
        assert!(
            !worker.is_finished(),
            "temporary read failure stopped the market worker"
        );
        storage
            .lock()
            .unwrap()
            .connection
            .execute_batch("ALTER TABLE inaccessible_state RENAME TO domain_state")?;
        tokio::time::timeout(Duration::from_secs(4), received.notified()).await?;
        signal.send(true)?;
        worker.await??;
        server.abort();
        Ok(())
    }
}
