//! Exact valuation of the local recorded ledger. No cash, fees or external account
//! balances are inferred. Quotes may enrich a position but never change its quantity.
use crate::contract::{digest, validate_definition};
use num_bigint::BigInt;
use rusqlite::{params, Connection, OptionalExtension, Transaction};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
use uuid::Uuid;

const QUOTE_TTL: u64 = 90_000;

#[derive(Clone)]
struct Decimal {
    units: BigInt,
    scale: u32,
}
impl Decimal {
    fn zero() -> Self {
        Self {
            units: BigInt::ZERO,
            scale: 0,
        }
    }
    fn parse(raw: &str) -> Option<Self> {
        if raw.is_empty() || raw.len() > 48 {
            return None;
        }
        let (whole, fraction) = raw.split_once('.').unwrap_or((raw, ""));
        if whole.is_empty()
            || (whole.len() > 1 && whole.starts_with('0'))
            || !whole.bytes().all(|b| b.is_ascii_digit())
            || !fraction.bytes().all(|b| b.is_ascii_digit())
            || (raw.contains('.') && fraction.is_empty())
        {
            return None;
        }
        Some(Self {
            units: format!("{whole}{fraction}").parse().ok()?,
            scale: fraction.len() as u32,
        })
    }
    fn add(&self, other: &Self) -> Self {
        let scale = self.scale.max(other.scale);
        let ten = BigInt::from(10);
        Self {
            units: &self.units * ten.pow(scale - self.scale)
                + &other.units * ten.pow(scale - other.scale),
            scale,
        }
    }
    fn sub(&self, other: &Self) -> Self {
        let mut other = other.clone();
        other.units = -other.units;
        self.add(&other)
    }
    fn times(&self, qty: u64) -> Self {
        Self {
            units: &self.units * qty,
            scale: self.scale,
        }
    }
    fn text(&self) -> Option<String> {
        let negative = self.units < BigInt::ZERO;
        let digits = self.units.to_string();
        let digits = digits.trim_start_matches('-');
        let value = if self.scale == 0 {
            digits.to_owned()
        } else {
            let padded = format!("{digits:0>width$}", width = self.scale as usize + 1);
            let (whole, fraction) = padded.split_at(padded.len() - self.scale as usize);
            let fraction = fraction.trim_end_matches('0');
            if fraction.is_empty() {
                whole.to_owned()
            } else {
                format!("{whole}.{fraction}")
            }
        };
        let value = if negative { format!("-{value}") } else { value };
        (value.len() <= 48).then_some(value)
    }
    fn ratio(&self, denominator: &Self) -> Option<String> {
        if denominator.units == BigInt::ZERO {
            return None;
        }
        let ten = BigInt::from(10);
        let mut numerator = &self.units * ten.pow(denominator.scale);
        let denominator = &denominator.units * ten.pow(self.scale);
        for scale in 0..=46 {
            if &numerator % &denominator == BigInt::ZERO {
                return Self {
                    units: numerator / denominator,
                    scale,
                }
                .text();
            }
            numerator *= &ten;
        }
        None
    }
}

fn objects(db: &Connection, collection: &str) -> anyhow::Result<Vec<Value>> {
    let mut stmt = db.prepare("SELECT body FROM objects WHERE collection=?1 ORDER BY object_id")?;
    let rows = stmt.query_map([collection], |r| r.get::<_, String>(0))?;
    rows.map(|row| Ok(serde_json::from_str(&row?)?)).collect()
}
fn put(db: &Connection, collection: &str, key: &str, value: &Value) -> anyhow::Result<()> {
    db.execute("INSERT INTO objects VALUES (?1,?2,?3) ON CONFLICT(collection,object_id) DO UPDATE SET body=excluded.body",params![collection,key,serde_json::to_string(value)?])?;
    Ok(())
}

struct Evaluation {
    positions: Vec<Value>,
    result: Value,
    evidence: Value,
    cutoff: u64,
    changes: BTreeSet<String>,
}

fn evaluate(db: &Connection, now: u64) -> anyhow::Result<Evaluation> {
    let mut positions = objects(db, "positions")?;
    let quotes: BTreeMap<String, Value> = objects(db, "market_quotes")?
        .into_iter()
        .filter_map(|q| q["symbol"].as_str().map(str::to_owned).map(|key| (key, q)))
        .collect();
    let executions = objects(db, "executions")?;
    let mut reconciliation: BTreeSet<String> = executions
        .iter()
        .filter(|e| e["reconciliationRequired"] == true)
        .filter_map(|e| e["symbol"].as_str().map(str::to_owned))
        .collect();
    let mut missing = BTreeSet::new();
    let mut subtotal = Decimal::zero();
    let mut pnl = Decimal::zero();
    let mut covered = 0;
    let mut total = 0;
    let mut price_asof = None::<u64>;
    let mut cutoff = 0;
    let mut used_quotes = Vec::new();
    for position in &mut positions {
        let symbol = position["symbol"].as_str().unwrap().to_owned();
        let qty = position["qty"].as_u64().unwrap();
        cutoff = cutoff.max(position["asOf"].as_u64().unwrap_or(0));
        if position["reconciliationRequired"] == true {
            reconciliation.insert(symbol.clone());
        }
        for key in [
            "lastPrice",
            "priceAsOf",
            "marketValue",
            "unrealizedPnl",
            "unrealizedPnlRatio",
        ] {
            position[key] = Value::Null;
        }
        position["priceStatus"] = json!("missing");
        let quote = quotes.get(&symbol);
        if let Some(quote) = quote {
            used_quotes.push(quote.clone());
        }
        let parsed = quote.and_then(|quote| {
            let price = Decimal::parse(quote["price"].as_str()?)?;
            let at = quote["asOf"].as_u64()?;
            let source = &quote["source"];
            if price.units <= BigInt::ZERO
                || at > now
                || source["name"].as_str().is_none_or(str::is_empty)
                || source["ref"].as_str().is_none_or(str::is_empty)
                || source["asOf"].as_u64().is_none_or(|time| time > now)
                || !matches!(quote["status"].as_str(), Some("fresh" | "stale"))
            {
                return None;
            }
            Some((
                price,
                at,
                quote["status"] == "fresh" && now - at <= QUOTE_TTL,
            ))
        });
        if let Some((_, at, fresh)) = &parsed {
            position["lastPrice"] = quote.unwrap()["price"].clone();
            position["priceAsOf"] = json!(at);
            position["priceStatus"] = json!(if *fresh { "fresh" } else { "stale" });
        }
        if qty == 0 {
            continue;
        }
        total += 1;
        let valued = parsed
            .filter(|(_, _, fresh)| *fresh)
            .and_then(|(price, at, _)| {
                let cost = Decimal::parse(position["cost"].as_str()?)?;
                let market = price.times(qty);
                let profit = price.sub(&cost).times(qty);
                let market_text = market.text()?;
                let profit_text = profit.text()?;
                let next_subtotal = subtotal.add(&market);
                next_subtotal.text()?;
                let next_pnl = pnl.add(&profit);
                next_pnl.text()?;
                Some((
                    market_text,
                    profit_text,
                    price.sub(&cost).ratio(&cost),
                    next_subtotal,
                    next_pnl,
                    at,
                ))
            });
        if let Some((market, profit, ratio, next_subtotal, next_pnl, at)) = valued {
            position["marketValue"] = json!(market);
            position["unrealizedPnl"] = json!(profit);
            position["unrealizedPnlRatio"] = json!(ratio);
            subtotal = next_subtotal;
            pnl = next_pnl;
            covered += 1;
            price_asof = Some(price_asof.map_or(at, |old| old.min(at)));
            cutoff = cutoff.max(at);
        } else {
            missing.insert(symbol);
        }
    }
    let complete = missing.is_empty() && reconciliation.is_empty();
    let result = json!({"scope":"recorded_positions","asOf":price_asof,"positionMarketValue":if complete{json!(subtotal.text())}else{Value::Null},
        "pricedSubtotal":subtotal.text().unwrap(),"unrealizedPnl":if complete{json!(pnl.text())}else{Value::Null},
        "coveredCount":covered,"totalCount":total,"unpricedSymbols":missing,"reconciliationSymbols":reconciliation,
        "includesCash":false,"includesFees":false,"feePolicy":"not_estimated"});
    let mut changes = BTreeSet::new();
    for adjustment in objects(db, "adjustments")? {
        let before = adjustment["before"]["qty"].as_u64().unwrap_or(0);
        if adjustment["after"]["qty"].as_u64() != Some(before) {
            if let Some(id) = adjustment["adjustmentId"].as_str() {
                changes.insert(id.to_owned());
            }
        }
    }
    for execution in &executions {
        if execution["positionEffect"] == "applied" {
            if let Some(id) = execution["executionId"].as_str() {
                changes.insert(id.to_owned());
            }
        }
    }
    let revision: u64 = db.query_row("SELECT position_revision FROM domain_state", [], |r| {
        r.get(0)
    })?;
    let evidence = json!({"positions":positions,"quotes":used_quotes,"positionRevision":revision,"calculationVersion":"recorded-decimal-v1","summary":result,"quantityChangeIds":changes});
    if !complete || cutoff == 0 {
        cutoff = now;
    }
    Ok(Evaluation {
        positions,
        result,
        evidence,
        cutoff,
        changes,
    })
}

pub fn summary(db: &Connection, now: u64) -> anyhow::Result<Value> {
    Ok(evaluate(db, now)?.result)
}

pub fn refresh(tx: &Transaction<'_>, now: u64) -> anyhow::Result<()> {
    let value = evaluate(tx, now)?;
    let fingerprint = digest(&value.evidence)?;
    let state: Option<String> = tx
        .query_row(
            "SELECT body FROM objects WHERE collection='valuationState' AND object_id='current'",
            [],
            |r| r.get(0),
        )
        .optional()?;
    let state: Value = state
        .map(|s| serde_json::from_str(&s))
        .transpose()?
        .unwrap_or(json!({}));
    if state["fingerprint"].is_null()
        && value.positions.is_empty()
        && value.changes.is_empty()
        && value.result["reconciliationSymbols"]
            .as_array()
            .unwrap()
            .is_empty()
    {
        return Ok(());
    }
    if state["fingerprint"] == fingerprint {
        return Ok(());
    }
    for position in &value.positions {
        put(
            tx,
            "positions",
            position["symbol"].as_str().unwrap(),
            position,
        )?;
    }
    let seen: BTreeSet<String> = state["seenChanges"]
        .as_array()
        .map(|ids| {
            ids.iter()
                .filter_map(|id| id.as_str().map(str::to_owned))
                .collect()
        })
        .unwrap_or_default();
    let changes: Vec<_> = value.changes.difference(&seen).cloned().collect();
    let date = chrono::DateTime::from_timestamp_millis(value.cutoff as i64)
        .ok_or_else(|| anyhow::anyhow!("invalid valuation cutoff"))?
        .with_timezone(&chrono_tz::Asia::Shanghai)
        .date_naive()
        .to_string();
    let previous = objects(tx, "valuationPoints")?
        .into_iter()
        .filter(|p| p["asOf"] == value.cutoff)
        .max_by_key(|p| p["revision"].as_u64());
    let mut supersedes = previous
        .as_ref()
        .map(|p| p["pointId"].clone())
        .unwrap_or(Value::Null);
    let first_revision = previous
        .as_ref()
        .and_then(|p| p["revision"].as_u64())
        .unwrap_or(0)
        + 1;
    let missing: BTreeSet<String> = value.result["unpricedSymbols"]
        .as_array()
        .unwrap()
        .iter()
        .chain(value.result["reconciliationSymbols"].as_array().unwrap())
        .filter_map(|v| v.as_str().map(str::to_owned))
        .collect();
    // Chunk change annotations without dropping them when many ledger entries arrive
    // between snapshots. Each immutable point retains the same valuation and cutoff.
    let batches = changes.len().div_ceil(500).max(1);
    for batch in 0..batches {
        let revision = first_revision + batch as u64;
        let ids =
            &changes[(batch * 500).min(changes.len())..((batch + 1) * 500).min(changes.len())];
        let key = Uuid::new_v4().to_string();
        let point = json!({"pointId":key,"tradeDate":date,"asOf":value.cutoff,"marketValue":value.result["positionMarketValue"],"complete":value.result["positionMarketValue"].is_string(),"missingSymbols":missing,"supersedesPointId":supersedes,"revision":revision,"quantityChangeIds":ids});
        validate_definition("ValuationPoint", &point)?;
        put(tx, "valuationPoints", &key, &point)?;
        let mut evidence = value.evidence.clone();
        evidence["cutoff"] = json!(value.cutoff);
        evidence["generatedAt"] = json!(now);
        put(tx, "valuationEvidence", &key, &evidence)?;
        supersedes = json!(key);
    }
    put(
        tx,
        "valuationState",
        "current",
        &json!({"fingerprint":fingerprint,"seenChanges":value.changes,"pointId":supersedes}),
    )?;
    tx.execute(
        "UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='snapshot_seq'",
        [],
    )?;
    Ok(())
}
