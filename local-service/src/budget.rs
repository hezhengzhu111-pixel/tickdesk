//! Planned-risk limits, independent of account cash. All arithmetic is decimal/integer.
use crate::{
    commands::{failure, Outcome},
    contract,
    domain::Domain,
    storage::Storage,
};
use anyhow::{ensure, Context};
use num_bigint::BigUint;
use rusqlite::{params, OptionalExtension, Transaction};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    io::Read,
    path::Path,
};

const SAFE: u64 = 9_007_199_254_740_991;
const PRICE_MAX_AGE: u64 = 90_000;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct BudgetSymbol {
    pub group: String,
    pub industry: String,
    pub strategies: Vec<String>,
    pub stop_price: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct BudgetConfig {
    pub version: u64,
    pub valid_from: u64,
    pub expires_at: u64,
    pub total_notional: String,
    pub total_risk: String,
    pub per_symbol_notional: String,
    pub per_strategy_notional: String,
    pub group_limits: BTreeMap<String, String>,
    pub industry_limits: BTreeMap<String, String>,
    pub daily_notional: String,
    pub max_active: usize,
    pub fee_buffer_rate: String,
    pub slippage_buffer_rate: String,
    pub symbols: BTreeMap<String, BudgetSymbol>,
}

impl BudgetConfig {
    pub fn load(path: &Path) -> anyhow::Result<Self> {
        let mut bytes = Vec::new();
        std::fs::File::open(path)
            .context("open budget configuration")?
            .take(262145)
            .read_to_end(&mut bytes)?;
        ensure!(bytes.len() <= 262144, "budget configuration exceeds 256KiB");
        let config: Self = serde_json::from_slice(&bytes).context("invalid budget JSON")?;
        config.validate()?;
        Ok(config)
    }
    pub fn validate(&self) -> anyhow::Result<()> {
        ensure!(
            self.version > 0
                && self.version <= SAFE
                && self.valid_from < self.expires_at
                && self.expires_at <= SAFE,
            "invalid budget version or validity interval"
        );
        ensure!(
            self.max_active > 0 && self.max_active <= 1000 && self.symbols.len() <= 2000,
            "invalid budget capacity"
        );
        for amount in [
            &self.total_notional,
            &self.total_risk,
            &self.per_symbol_notional,
            &self.per_strategy_notional,
            &self.daily_notional,
        ]
        .into_iter()
        .chain(self.group_limits.values())
        .chain(self.industry_limits.values())
        {
            external(amount)?;
        }
        for rate in [&self.fee_buffer_rate, &self.slippage_buffer_rate] {
            ensure!(
                external(rate)?.0 <= scale(),
                "budget pressure rate must be <=1"
            );
        }
        for (symbol, assignment) in &self.symbols {
            ensure!(
                symbol.len() == 9
                    && symbol.as_bytes()[..6].iter().all(u8::is_ascii_digit)
                    && (symbol.ends_with(".SH") || symbol.ends_with(".SZ")),
                "invalid budget symbol"
            );
            ensure!(
                self.group_limits.contains_key(&assignment.group)
                    && self.industry_limits.contains_key(&assignment.industry),
                "budget symbol requires configured group and industry caps"
            );
            ensure!(
                !assignment.strategies.is_empty()
                    && assignment.strategies.len() <= 100
                    && assignment
                        .strategies
                        .iter()
                        .all(|s| !s.trim().is_empty() && s.len() <= 80),
                "budget strategy assignment invalid"
            );
            if let Some(stop) = &assignment.stop_price {
                external(stop)?;
            }
        }
        Ok(())
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BudgetDecision {
    pub quantity: Option<u64>,
    pub reason: &'static str,
    pub blocked: bool,
}
fn observe(reason: &'static str) -> BudgetDecision {
    BudgetDecision {
        quantity: None,
        reason,
        blocked: false,
    }
}
fn reject(reason: &'static str) -> BudgetDecision {
    BudgetDecision {
        quantity: None,
        reason,
        blocked: true,
    }
}

impl Storage {
    pub fn configure_budget(
        &mut self,
        config: Option<&BudgetConfig>,
        now: u64,
    ) -> anyhow::Result<()> {
        if let Some(config) = config {
            config.validate()?;
        }
        let tx = self.connection.transaction()?;
        if let Some(config) = config {
            let body = serde_json::to_value(config)?;
            // The local configuration is not a v2 business object: group labels can
            // be Unicode. Hash its deterministic JSON text as a canonical string.
            let hash = contract::digest(&json!({"document":body.to_string()}))?;
            let existing: Option<String> = tx
                .query_row(
                    "SELECT body_hash FROM budget_configs WHERE version=?1",
                    [config.version],
                    |r| r.get(0),
                )
                .optional()?;
            if let Some(existing) = existing {
                ensure!(
                    existing == hash,
                    "budget version is immutable; increment version for a changed plan"
                );
            } else {
                tx.execute(
                    "INSERT INTO budget_configs VALUES (?1,?2,?3,?4)",
                    params![config.version, body.to_string(), hash, now],
                )?;
            }
            tx.execute(
                "UPDATE budget_state SET active_version=?1",
                [config.version],
            )?;
        } else {
            tx.execute("UPDATE budget_state SET active_version=NULL", [])?;
        }
        tx.commit()?;
        Ok(())
    }
}

#[derive(Clone, Debug, Default)]
struct Amount(BigUint);
fn scale() -> BigUint {
    BigUint::from(10u8).pow(48)
}
fn external(s: &str) -> anyhow::Result<Amount> {
    ensure!(s.len() <= 48, "decimal exceeds 48 characters");
    Amount::parse(s)
}
impl Amount {
    fn parse(s: &str) -> anyhow::Result<Self> {
        let (whole, fraction) = s.split_once('.').unwrap_or((s, ""));
        ensure!(
            !whole.is_empty()
                && whole.bytes().all(|c| c.is_ascii_digit())
                && fraction.bytes().all(|c| c.is_ascii_digit())
                && fraction.len() <= 48
                && s.len() <= 128
                && (!s.contains('.') || !fraction.is_empty()),
            "invalid decimal"
        );
        ensure!(
            whole == "0" || !whole.starts_with('0'),
            "noncanonical decimal integer"
        );
        let digits = format!("{whole}{fraction}").parse::<BigUint>()?;
        Ok(Self(
            digits * BigUint::from(10u8).pow((48 - fraction.len()) as u32),
        ))
    }
    fn text(&self) -> String {
        let digits = format!("{:0>49}", self.0.to_string());
        let (whole, fraction) = digits.split_at(digits.len() - 48);
        let fraction = fraction.trim_end_matches('0');
        if fraction.is_empty() {
            whole.to_owned()
        } else {
            format!("{whole}.{fraction}")
        }
    }
    fn add(&mut self, other: &Self) {
        self.0 += &other.0;
    }
    fn times(&self, qty: u64) -> Self {
        Self(&self.0 * qty)
    }
    fn left(&self, used: &Self) -> Self {
        Self(if self.0 > used.0 {
            &self.0 - &used.0
        } else {
            BigUint::ZERO
        })
    }
    fn pressured(&self, rate: &Self) -> Self {
        let numerator = &self.0 * (scale() + &rate.0);
        Self((numerator + scale() - BigUint::from(1u8)) / scale())
    }
    fn units(&self, per: &Self) -> u64 {
        if per.0 == BigUint::ZERO {
            return 0;
        }
        let digits = (&self.0 / &per.0).to_u64_digits();
        if digits.len() > 1 {
            SAFE
        } else {
            digits.first().copied().unwrap_or(0).min(SAFE)
        }
    }
}

#[derive(Default)]
struct Usage {
    total: Amount,
    risk: Amount,
    daily: Amount,
    active: usize,
    symbols: BTreeMap<String, Amount>,
    strategies: BTreeMap<String, Amount>,
    groups: BTreeMap<String, Amount>,
    industries: BTreeMap<String, Amount>,
}
impl Usage {
    fn add(
        &mut self,
        symbol: &str,
        strategies: &[String],
        group: &str,
        industry: &str,
        notional: &Amount,
        risk: &Amount,
    ) {
        self.total.add(notional);
        self.risk.add(risk);
        self.symbols.entry(symbol.into()).or_default().add(notional);
        for strategy in strategies.iter().collect::<BTreeSet<_>>() {
            self.strategies
                .entry(strategy.clone())
                .or_default()
                .add(notional);
        }
        self.groups.entry(group.into()).or_default().add(notional);
        self.industries
            .entry(industry.into())
            .or_default()
            .add(notional);
    }
}

fn date(now: u64) -> anyhow::Result<String> {
    Ok(chrono::DateTime::from_timestamp_millis(now.try_into()?)
        .context("invalid budget time")?
        .with_timezone(&chrono_tz::Asia::Shanghai)
        .format("%Y-%m-%d")
        .to_string())
}

fn objects(tx: &Transaction<'_>, collection: &str) -> anyhow::Result<Vec<Value>> {
    let mut statement =
        tx.prepare("SELECT body FROM objects WHERE collection=?1 ORDER BY object_id")?;
    let rows = statement.query_map([collection], |r| r.get::<_, String>(0))?;
    rows.map(|r| Ok(serde_json::from_str(&r?)?)).collect()
}

/// Releases only proven unused reservations. Unknown outcomes keep capacity held.
pub fn reconcile(tx: &Transaction<'_>, now: u64) -> anyhow::Result<()> {
    reconcile_remaining(tx, now).map(|_| ())
}

struct Progress {
    summary: Value,
    remaining: u64,
    verified_fills: Option<u64>,
    latest_fill: Option<u64>,
    fill_hash: Option<String>,
}

fn reconcile_remaining(
    tx: &Transaction<'_>,
    now: u64,
) -> anyhow::Result<BTreeMap<String, Progress>> {
    let mut statement = tx.prepare("SELECT task_id,symbol,qty,state FROM budget_reservations")?;
    let reservations = statement
        .query_map([], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, u64>(2)?,
                r.get::<_, String>(3)?,
            ))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    if reservations.is_empty() {
        return Ok(BTreeMap::new());
    }
    let epoch: String = tx.query_row(
        "SELECT value FROM metadata WHERE key='server_epoch'",
        [],
        |r| r.get(0),
    )?;
    let mut statements =
        tx.prepare("SELECT object_id,body FROM objects WHERE collection='execution_progress'")?;
    let reports = statements
        .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?
        .map(|row| {
            let (task, body) = row?;
            Ok((task, serde_json::from_str::<Value>(&body)?))
        })
        .collect::<anyhow::Result<BTreeMap<_, _>>>()?;
    let executions = objects(tx, "executions")?;
    let mut by_task = BTreeMap::<&str, Vec<&Value>>::new();
    for execution in &executions {
        if let Some(task_id) = execution["taskId"].as_str() {
            by_task.entry(task_id).or_default().push(execution);
        }
    }
    let mut progress = BTreeMap::new();
    for (task_id, symbol, planned, previous) in reservations {
        let linked = by_task.get(task_id.as_str()).map_or(&[][..], Vec::as_slice);
        let report = reports.get(&task_id).unwrap_or(&Value::Null);
        let report_current = report["epoch"] == epoch;
        let report_stale = !report.is_null() && !report_current;
        let pending: bool = tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM transport_inbox i
             WHERE i.state IN ('pending','quarantined') AND json_extract(i.body,'$.data.taskId')=?1
             AND (json_extract(i.body,'$.data.cmd')='record_execution'
                  OR (json_extract(i.body,'$.data.cmd')='act' AND json_extract(i.body,'$.data.side') IS NOT NULL))
             AND (i.state='pending' OR NOT EXISTS(
                SELECT 1 FROM recovery_items item JOIN recovery_completed completed USING(session_id)
                WHERE completed.original_epoch=i.command_epoch AND item.decision IS NOT NULL
                AND item.subject=i.command_epoch||':'||i.client_msg_id||':'||i.request_hash)))",
            [&task_id], |r| r.get(0),
        )?;
        let counted = linked.iter().any(|e| e["positionEffect"] != "superseded");
        let filled = linked
            .iter()
            .filter(|e| e["positionEffect"] != "superseded")
            .try_fold(0_u64, |total, execution| {
                if execution["symbol"] != symbol
                    || execution["side"] != "buy"
                    || !matches!(
                        execution["positionEffect"].as_str(),
                        Some("applied" | "reconciled")
                    )
                {
                    return None;
                }
                total
                    .checked_add(
                        execution["qty"]
                            .as_u64()
                            .filter(|qty| *qty > 0 && *qty <= SAFE)?,
                    )
                    .filter(|qty| *qty <= SAFE)
            })
            .filter(|_| linked.is_empty() || counted);
        let verified_fills = filled.filter(|_| !pending);
        let fill_hash = verified_fills
            .map(|_| {
                let facts = linked
                    .iter()
                    .filter(|fill| fill["positionEffect"] != "superseded")
                    .map(|fill| &fill["executionId"])
                    .collect::<Vec<_>>();
                contract::digest(&json!(facts))
            })
            .transpose()?;
        let latest_fill = linked
            .iter()
            .filter_map(|fill| fill["executedAt"].as_u64())
            .max();
        let ended = report_current && report["state"] == "ended";
        let end_matches = ended
            && verified_fills.is_some_and(|qty| report["expectedConfirmedQty"] == qty)
            && fill_hash
                .as_deref()
                .is_some_and(|hash| report["fillHash"] == hash);
        let (state, reported_state, remaining) = if end_matches {
            (
                if filled == Some(planned) {
                    "converted"
                } else {
                    "released"
                },
                "ended",
                0,
            )
        } else if report_stale || ended || verified_fills.is_none_or(|qty| qty > planned) {
            ("reconciliation", "unknown", planned)
        } else if report["state"] == "unknown" {
            ("reconciliation", "unknown", planned - filled.unwrap())
        } else if filled == Some(planned) {
            ("converted", "ended", 0)
        } else if !linked.is_empty() {
            // Lifecycle actions on the notification card do not settle an order remainder.
            ("held", "partially_filled", planned - filled.unwrap())
        } else if !report.is_null() {
            ("held", "processing", planned)
        } else if matches!(previous.as_str(), "released" | "converted") {
            (previous.as_str(), "not_started", 0)
        } else {
            let body: Option<String> = tx
                .query_row(
                    "SELECT body FROM objects WHERE collection='tasks' AND object_id=?1",
                    [&task_id],
                    |r| r.get(0),
                )
                .optional()?;
            let terminal = body
                .map(|b| serde_json::from_str::<Value>(&b))
                .transpose()?
                .is_some_and(|t| t["status"] == "ignored" || t["status"] == "expired");
            if terminal {
                ("released", "not_started", 0)
            } else {
                ("held", "not_started", planned)
            }
        };
        if state != previous {
            tx.execute(
                "UPDATE budget_reservations SET state=?1,updated_at=?2 WHERE task_id=?3",
                params![state, now, task_id],
            )?;
        }
        let summary = json!({"taskId":task_id,"symbol":symbol,"version":report["version"].as_u64().unwrap_or(0),
            "state":reported_state,"plannedQty":planned,"confirmedFillQty":verified_fills,"reservedQty":remaining,
            "reportedRemainingQty":report["reportedRemainingQty"],"reportedAt":report["asOf"],
            "reconciliationRequired":state=="reconciliation"});
        progress.insert(
            task_id,
            Progress {
                summary,
                remaining,
                verified_fills,
                latest_fill,
                fill_hash,
            },
        );
    }
    Ok(progress)
}

pub(crate) fn progress_command(d: &Domain<'_, '_>) -> anyhow::Result<Outcome> {
    let writing = d.data["cmd"] == "set_execution_progress";
    let Some(task) = d.data["taskId"].as_str() else {
        return Ok(Ok(json!({"version":3,"progress":null})));
    };
    let mut progress = reconcile_remaining(d.tx, d.now)?;
    let Some(current) = progress.remove(task) else {
        return Ok(if writing {
            failure("TASK_NOT_FOUND")
        } else {
            Ok(json!({"version":3,"progress":null}))
        });
    };
    if !writing {
        return Ok(Ok(json!({"version":3,"progress":current.summary})));
    }
    let report = d.data;
    let version = current.summary["version"].as_u64().unwrap();
    let as_of = report["asOf"].as_u64().unwrap();
    let amount = report["reportedRemainingQty"].as_u64();
    let expected = report["expectedConfirmedQty"].as_u64();
    let planned = current.summary["plannedQty"].as_u64().unwrap();
    let symbol_review = report["state"] == "ended"
        && d.tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM objects WHERE collection='executions'
         AND json_extract(body,'$.symbol')=?1 AND json_extract(body,'$.reconciliationRequired')=1)",
            [current.summary["symbol"].as_str().unwrap()],
            |row| row.get::<_, bool>(0),
        )?;
    let valid = match report["state"].as_str().unwrap() {
        "ended" => {
            !symbol_review
                && amount == Some(0)
                && expected.is_some()
                && expected == current.verified_fills
                && current.latest_fill.is_none_or(|at| at <= as_of)
        }
        "unknown" => amount.is_none() && expected.is_none(),
        "processing" | "partially_filled" => {
            expected.is_none()
                && amount.is_some_and(|qty| {
                    qty > 0 && qty <= planned.saturating_sub(current.verified_fills.unwrap_or(0))
                })
                && (report["state"] == "processing"
                    || current.verified_fills.is_some_and(|qty| qty > 0))
        }
        _ => false,
    };
    if report["baseVersion"] != version {
        return Ok(Err((
            "PROGRESS_VERSION_CONFLICT",
            json!({"currentVersion":version}),
        )));
    }
    if !valid
        || version >= SAFE
        || as_of > d.now
        || current.summary["reportedAt"]
            .as_u64()
            .is_some_and(|at| at > as_of)
    {
        return Ok(failure("INVALID_PARAMS"));
    }
    let body = json!({"epoch":d.epoch,"version":version+1,"state":report["state"],"reportedRemainingQty":amount,
        "expectedConfirmedQty":expected,"fillHash":current.fill_hash,"asOf":as_of,"clientMsgId":report["clientMsgId"],"taskId":task});
    d.put("execution_progress", task, &body)?;
    d.put("execution_progress_history", &crate::domain::id(), &body)?;
    d.bump()?;
    let mut updated = reconcile_remaining(d.tx, d.now)?;
    let updated = updated
        .remove(task)
        .context("reserved intent missing after write")?;
    Ok(Ok(json!({"version":3,"progress":updated.summary})))
}

/// Called inside the same immediate transaction as task creation and its outbox events.
pub fn reserve(
    tx: &Transaction<'_>,
    task_id: &str,
    raw: &Value,
    now: u64,
) -> anyhow::Result<BudgetDecision> {
    let hash = contract::digest(raw)?;
    let existing: Option<(String, u64)> = tx
        .query_row(
            "SELECT request_hash,qty FROM budget_reservations WHERE task_id=?1",
            [task_id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()?;
    if let Some((original, qty)) = existing {
        ensure!(
            original == hash,
            "budget task ID reused with different sizing input"
        );
        return Ok(BudgetDecision {
            quantity: Some(qty),
            reason: "BUDGET_RESERVED",
            blocked: false,
        });
    }
    if raw["direction"] != "buy" {
        return Ok(observe("BUDGET_NOT_BUY"));
    }
    let recovering: bool =
        tx.query_row("SELECT EXISTS(SELECT 1 FROM recovery)", [], |r| r.get(0))?;
    if recovering {
        return Ok(reject("RECOVERY_REQUIRED"));
    }
    let body: Option<String> = tx
        .query_row(
            "SELECT c.body FROM budget_configs c JOIN budget_state s ON s.active_version=c.version",
            [],
            |r| r.get(0),
        )
        .optional()?;
    let Some(body) = body else {
        return Ok(observe("BUDGET_NOT_CONFIGURED"));
    };
    let config: BudgetConfig = serde_json::from_str(&body)?;
    if now < config.valid_from || now >= config.expires_at {
        return Ok(observe("BUDGET_EXPIRED"));
    }
    let c = &raw["candidate"];
    let sizing = &raw["sizing"];
    let symbol = c["symbol"].as_str().unwrap_or("");
    let Some(assignment) = config.symbols.get(symbol) else {
        return Ok(observe("BUDGET_CLASSIFICATION_MISSING"));
    };
    let strategies = c["strategies"]
        .as_array()
        .map(|s| {
            s.iter()
                .filter_map(|v| v.as_str().map(str::to_owned))
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    if strategies.is_empty()
        || strategies
            .iter()
            .any(|s| !assignment.strategies.contains(s))
    {
        return Ok(observe("BUDGET_STRATEGY_MISSING"));
    }
    let trade_date = date(now)?;
    let fresh = |at: Option<u64>, age: u64| at.is_some_and(|at| at <= now && now - at <= age);
    let price = c["lastPrice"].as_str().and_then(|s| external(s).ok());
    let stop = sizing["stopPrice"].as_str().and_then(|s| external(s).ok());
    let unit = sizing["unitQty"].as_u64().filter(|q| *q > 0 && *q <= SAFE);
    if !fresh(c["dataAsOf"].as_u64(), PRICE_MAX_AGE)
        || !fresh(sizing["ruleAsOf"].as_u64(), 86_400_000)
        || sizing["effectiveDate"] != trade_date
        || sizing["priceBasis"] != "raw"
        || sizing["ruleSource"]
            .as_str()
            .is_none_or(|s| s.trim().is_empty() || s.len() > 512)
    {
        return Ok(observe("BUDGET_INPUT_UNVERIFIED"));
    }
    let (Some(price), Some(stop), Some(unit)) = (price, stop, unit) else {
        return Ok(observe("BUDGET_INPUT_MISSING"));
    };
    if stop.0 == BigUint::ZERO || price.0 <= stop.0 {
        return Ok(observe("BUDGET_STOP_INVALID"));
    }
    let mut rate = external(&config.fee_buffer_rate)?;
    rate.add(&external(&config.slippage_buffer_rate)?);
    let unit_notional = price.pressured(&rate);
    let unit_risk = Amount(&unit_notional.0 - &stop.0);
    let progress = reconcile_remaining(tx, now)?;
    let unresolved: bool = tx.query_row("SELECT EXISTS(SELECT 1 FROM objects WHERE collection='executions' AND json_extract(body,'$.positionEffect')='needs_reconciliation')", [], |r|r.get(0))?;
    if unresolved
        || progress
            .values()
            .any(|entry| entry.summary["reconciliationRequired"] == true)
    {
        return Ok(reject("BUDGET_EXECUTION_UNRESOLVED"));
    }
    let mut usage = Usage::default();
    for position in objects(tx, "positions")? {
        let Some(qty) = position["qty"].as_u64() else {
            return Ok(reject("BUDGET_POSITION_UNKNOWN"));
        };
        if qty == 0 {
            continue;
        }
        let symbol = position["symbol"].as_str().unwrap_or("");
        let Some(assignment) = config.symbols.get(symbol) else {
            return Ok(reject("BUDGET_POSITION_CLASSIFICATION"));
        };
        let position_price = position["lastPrice"]
            .as_str()
            .and_then(|s| external(s).ok());
        let position_stop = assignment
            .stop_price
            .as_deref()
            .and_then(|s| external(s).ok());
        let quote_changed: bool = tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM objects WHERE collection='market_quotes' AND object_id=?1
             AND (json_extract(body,'$.status') IS NOT 'fresh' OR json_extract(body,'$.asOf') IS NOT ?2
                  OR json_extract(body,'$.price') IS NOT ?3))",
            params![symbol, position["priceAsOf"].as_u64(), position["lastPrice"].as_str()],
            |row| row.get(0),
        )?;
        if position["reconciliationRequired"] == true
            || position["priceStatus"] != "fresh"
            || !fresh(position["priceAsOf"].as_u64(), PRICE_MAX_AGE)
            || quote_changed
        {
            return Ok(reject("BUDGET_POSITION_UNKNOWN"));
        }
        let (Some(position_price), Some(position_stop)) = (position_price, position_stop) else {
            return Ok(reject("BUDGET_POSITION_UNKNOWN"));
        };
        if position_stop.0 == BigUint::ZERO || position_price.0 <= position_stop.0 {
            return Ok(reject("BUDGET_POSITION_RISK_UNKNOWN"));
        }
        let notional = position_price.pressured(&rate);
        let risk = Amount(&notional.0 - &position_stop.0);
        usage.add(
            symbol,
            &assignment.strategies,
            &assignment.group,
            &assignment.industry,
            &notional.times(qty),
            &risk.times(qty),
        );
    }
    let mut statement = tx.prepare(
        "SELECT task_id,symbol,strategies,qty,notional,risk,trade_date FROM budget_reservations",
    )?;
    let rows = statement.query_map([], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, String>(1)?,
            r.get::<_, String>(2)?,
            r.get::<_, u64>(3)?,
            r.get::<_, String>(4)?,
            r.get::<_, String>(5)?,
            r.get::<_, String>(6)?,
        ))
    })?;
    for row in rows {
        let (task_id, symbol, strategies, planned, notional, risk, day) = row?;
        let notional = Amount::parse(&notional)?;
        let risk = Amount::parse(&risk)?;
        if day == trade_date {
            usage.daily.add(&notional);
        }
        let remaining = progress.get(&task_id).map_or(0, |entry| entry.remaining);
        if remaining > 0 {
            // Reclassification under a newer plan must count every existing reservation
            // against the current groups, while retaining its original audit assignment.
            let Some(assignment) = config.symbols.get(&symbol) else {
                return Ok(reject("BUDGET_POSITION_CLASSIFICATION"));
            };
            let mut current_strategies = serde_json::from_str::<Vec<String>>(&strategies)?;
            current_strategies.extend(assignment.strategies.iter().cloned());
            usage.add(
                &symbol,
                &current_strategies,
                &assignment.group,
                &assignment.industry,
                &Amount(notional.0 / planned).times(remaining),
                &Amount(risk.0 / planned).times(remaining),
            );
            usage.active += 1;
        }
    }
    if usage.active >= config.max_active {
        return Ok(reject("BUDGET_ACTIVE_LIMIT"));
    }
    let zero = Amount::default();
    let mut qty = external(&config.total_notional)?
        .left(&usage.total)
        .units(&unit_notional)
        .min(
            external(&config.total_risk)?
                .left(&usage.risk)
                .units(&unit_risk),
        )
        .min(
            external(&config.daily_notional)?
                .left(&usage.daily)
                .units(&unit_notional),
        )
        .min(
            external(&config.per_symbol_notional)?
                .left(usage.symbols.get(symbol).unwrap_or(&zero))
                .units(&unit_notional),
        )
        .min(
            external(&config.group_limits[&assignment.group])?
                .left(usage.groups.get(&assignment.group).unwrap_or(&zero))
                .units(&unit_notional),
        )
        .min(
            external(&config.industry_limits[&assignment.industry])?
                .left(usage.industries.get(&assignment.industry).unwrap_or(&zero))
                .units(&unit_notional),
        );
    for strategy in &strategies {
        qty = qty.min(
            external(&config.per_strategy_notional)?
                .left(usage.strategies.get(strategy).unwrap_or(&zero))
                .units(&unit_notional),
        );
    }
    qty = qty / unit * unit;
    if qty == 0 {
        return Ok(reject("BUDGET_EXHAUSTED"));
    }
    tx.execute("INSERT INTO budget_reservations VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,'held',?11,?12,?12)",params![task_id,config.version,hash,symbol,json!(strategies).to_string(),assignment.group,assignment.industry,qty,unit_notional.times(qty).text(),unit_risk.times(qty).text(),trade_date,now])?;
    Ok(BudgetDecision {
        quantity: Some(qty),
        reason: "BUDGET_RESERVED",
        blocked: false,
    })
}
