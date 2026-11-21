use super::{shanghai_date, Domain, SAFE_QTY};
use crate::{contract::validate_definition, market::decimal_cmp};
use anyhow::{ensure, Context};
use chrono::{DateTime, NaiveDate, NaiveTime};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{cmp::Ordering, collections::BTreeSet};

const COLLECTION: &str = "holding_rules";
const UNAVAILABLE: &str = "holding_rule_unavailability";
const AUCTION_STATUS_UNAVAILABLE: &str = "holding_auction_status_unavailability";
const OFFICIAL_SUSPENSIONS: &str = "holding_official_suspensions";
const SSE_SUSPENSION_PAGE: &str =
    "https://www.sse.com.cn/disclosure/dealinstruc/suspension/stock/#";
const SZSE_MEMO_PAGE: &str = "https://www.szse.cn/disclosure/memo/index.html#";

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct RuleBatch {
    version: u8,
    rules: Vec<RuleEvidence>,
    #[serde(default)]
    unavailable: Vec<RuleUnavailability>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(super) struct RuleEvidence {
    pub rule: Value,
    rule_available_at_ms: u64,
    calendar: Calendar,
    company_actions_checked_through: Option<String>,
    #[serde(skip)]
    unavailability: Option<RuleUnavailability>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct RuleUnavailability {
    symbol: String,
    trade_date: String,
    available_at_ms: u64,
    reason: String,
    #[serde(default)]
    scope: Option<UnavailabilityScope>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
enum UnavailabilityScope {
    AuctionStatus,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct Calendar {
    revision_id: String,
    source: Value,
    available_at_ms: u64,
    coverage_start: String,
    coverage_end: String,
    sessions: Vec<Session>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct Session {
    date: String,
    first_execution_at_ms: u64,
}

impl Domain<'_, '_> {
    pub(crate) fn persist_holding_rules(
        &self,
        raw: &Value,
        cutoff: u64,
        scope: &Value,
    ) -> anyhow::Result<BTreeSet<String>> {
        let Some(batch) = validated_rules(raw, cutoff, scope) else {
            return Ok(BTreeSet::new());
        };
        let mut auction_updates = BTreeSet::new();
        for (key, row) in batch.rules {
            let rule = &row["rule"];
            let symbol = rule["symbol"].as_str().expect("validated symbol");
            let collection = if rule["dataStatus"] == "missing" {
                if Self::official_suspension_ref(rule).is_none()
                    && !Self::official_szse_withdrawal(rule)
                {
                    continue;
                }
                OFFICIAL_SUSPENSIONS
            } else {
                COLLECTION
            };
            if rule["channel"] == "auction" {
                auction_updates.insert(symbol.to_owned());
            }
            if let Some(previous) = self.get(collection, &key)? {
                if previous["epoch"] == self.epoch {
                    let previous_rule = &previous["evidence"]["rule"];
                    let current_rule = &row["rule"];
                    let previous_at = previous["evidence"]["ruleAvailableAtMs"].as_u64();
                    let current_at = row["ruleAvailableAtMs"].as_u64();
                    let previous_clock = (previous_at, previous_rule["sourceTime"].as_u64());
                    let current_clock = (current_at, current_rule["sourceTime"].as_u64());
                    if previous_clock > current_clock {
                        continue;
                    }
                    if previous_rule != current_rule
                        && (previous_rule["ruleSnapshotId"] == current_rule["ruleSnapshotId"]
                            || previous_clock == current_clock)
                    {
                        // Equally current conflicting evidence must not remain usable.
                        self.put(
                            collection,
                            &key,
                            &json!({"epoch":self.epoch,
                            "evidence":row,"conflict":true}),
                        )?;
                        continue;
                    }
                    if previous["evidence"]["calendar"]["revisionId"]
                        == row["calendar"]["revisionId"]
                        && previous["evidence"]["calendar"] != row["calendar"]
                    {
                        self.put(
                            collection,
                            &key,
                            &json!({"epoch":self.epoch,
                            "evidence":row,"conflict":true}),
                        )?;
                        continue;
                    }
                    if previous["conflict"] == true && previous_clock == current_clock {
                        continue;
                    }
                    if previous["evidence"] == *row {
                        continue;
                    }
                }
            }
            self.put(
                collection,
                &key,
                &json!({"epoch":self.epoch,"evidence":row,"conflict":false}),
            )?;
        }
        for (collection, key, row) in batch.unavailable {
            if let Some(previous) = self.get(collection, &key)? {
                if previous["epoch"] == self.epoch
                    && previous["evidence"]["availableAtMs"].as_u64()
                        >= row["availableAtMs"].as_u64()
                {
                    continue;
                }
            }
            self.put(
                collection,
                &key,
                &json!({"epoch":self.epoch,"evidence":row}),
            )?;
            auction_updates.insert(row["symbol"].as_str().expect("validated symbol").to_owned());
        }
        Ok(auction_updates)
    }

    /// A positive exchange suspension may be proved independently of settlement
    /// or price bands. Keep that proof separate from the full ledger rule.
    pub(crate) fn official_suspension_ref(rule: &Value) -> Option<&str> {
        if rule["instrumentType"] != "stock"
            || rule["channel"] != "auction"
            || rule["tradingStatus"] != "suspended"
            || rule["issueFields"]
                .as_array()?
                .iter()
                .any(|field| field == "tradingStatus")
        {
            return None;
        }
        if rule["exchange"] == "SZSE" {
            return szse_memo_ref(rule, "suspension");
        }
        if rule["exchange"] != "SSE" {
            return None;
        }
        let trade_day = date(rule["tradeDate"].as_str()?).ok()?;
        let source_time = rule["sourceTime"].as_u64()?;
        if shanghai_date(source_time) != Some(trade_day) {
            return None;
        }
        let symbol = rule["symbol"].as_str()?;
        rule["sources"].as_array()?.iter().find_map(|source| {
            if source["name"] != "上交所停复牌信息" || source["asOf"].as_u64() != Some(source_time)
            {
                return None;
            }
            let reference = source["ref"].as_str()?;
            let (listed_symbol, start) = reference
                .strip_prefix(SSE_SUSPENSION_PAGE)?
                .split_once('-')?;
            let started = NaiveDate::parse_from_str(start, "%Y%m%d").ok()?;
            (listed_symbol == symbol
                && started <= trade_day
                && started.format("%Y%m%d").to_string() == start)
                .then_some(reference)
        })
    }

    fn official_szse_withdrawal(rule: &Value) -> bool {
        rule["exchange"] == "SZSE"
            && rule["instrumentType"] == "stock"
            && rule["channel"] == "auction"
            && rule["tradingStatus"] == "unknown"
            && rule["issueFields"]
                .as_array()
                .is_some_and(|issues| issues.iter().any(|field| field == "tradingStatus"))
            && szse_memo_ref(rule, "withdrawal").is_some()
    }

    pub(crate) fn verified_auction_status_at(
        &self,
        symbol: &str,
        date: &str,
        at: u64,
    ) -> anyhow::Result<Option<Value>> {
        let unavailable = self
            .read_unavailability(AUCTION_STATUS_UNAVAILABLE, symbol, date)?
            .filter(|state| state.available_at_ms <= at);
        let visible = |row: &RuleEvidence| {
            row.is_known_at(at)
                && unavailable.as_ref().is_none_or(|state| {
                    row.trading_evidence_newer_than(state.available_at_ms, state.available_at_ms)
                })
        };
        let full = self
            .holding_rule(symbol, date, Some("auction"))?
            .filter(|row| {
                row.rule["dataStatus"] == "complete"
                    && visible(row)
                    && matches!(
                        row.rule["tradingStatus"].as_str(),
                        Some("trading" | "suspended")
                    )
            });
        let partial = self
            .read_rule(OFFICIAL_SUSPENSIONS, symbol, date, "auction")?
            .filter(visible);
        if let Some(row) = partial.filter(|partial| {
            !full.as_ref().is_some_and(|full| {
                full.trading_evidence_newer_than(
                    partial.rule_available_at_ms,
                    partial.rule["sourceTime"].as_u64().expect("validated rule"),
                )
            })
        }) {
            // A current cancellation or conflict removes the trading proof.
            // The complete ledger snapshot still serves settlement separately.
            return Ok((row.rule["dataStatus"] == "missing"
                && Self::official_suspension_ref(&row.rule).is_some())
            .then_some(row.rule));
        }
        Ok(full.map(|row| row.rule))
    }

    pub(super) fn holding_rule(
        &self,
        symbol: &str,
        date: &str,
        channel: Option<&str>,
    ) -> anyhow::Result<Option<RuleEvidence>> {
        let Some(channel) = channel else {
            return Ok(None);
        };
        self.read_rule(COLLECTION, symbol, date, channel)
    }

    fn read_rule(
        &self,
        collection: &str,
        symbol: &str,
        date: &str,
        channel: &str,
    ) -> anyhow::Result<Option<RuleEvidence>> {
        let Some(row) = self.get(collection, &format!("{symbol}:{date}:{channel}"))? else {
            return Ok(None);
        };
        if row["epoch"] != self.epoch {
            return Ok(None);
        }
        let mut evidence: RuleEvidence = serde_json::from_value(row["evidence"].clone())?;
        if row["conflict"] == true {
            evidence.rule["dataStatus"] = json!("conflict");
        }
        evidence.unavailability = self
            .read_unavailability(UNAVAILABLE, symbol, date)?
            .filter(|state| state.available_at_ms >= evidence.rule_available_at_ms);
        Ok(Some(evidence))
    }

    fn read_unavailability(
        &self,
        collection: &str,
        symbol: &str,
        date: &str,
    ) -> anyhow::Result<Option<RuleUnavailability>> {
        let Some(row) = self.get(collection, &format!("{symbol}:{date}"))? else {
            return Ok(None);
        };
        if row["epoch"] != self.epoch {
            return Ok(None);
        }
        Ok(Some(serde_json::from_value(row["evidence"].clone())?))
    }
}

fn szse_memo_ref<'a>(rule: &'a Value, action: &str) -> Option<&'a str> {
    let symbol = rule["symbol"].as_str()?;
    if !symbol.ends_with(".SZ") || !matches!(rule["board"].as_str(), Some("main" | "chinext")) {
        return None;
    }
    let session = date(rule["tradeDate"].as_str()?).ok()?;
    let source_time = rule["sourceTime"].as_u64()?;
    let observed = DateTime::from_timestamp_millis(source_time.try_into().ok()?)?
        .with_timezone(&chrono_tz::Asia::Shanghai);
    if observed.date_naive() != session || observed.time() < NaiveTime::from_hms_opt(9, 25, 0)? {
        return None;
    }
    let anchor = format!("{action}-{symbol}-{}", session.format("%Y%m%d"));
    rule["sources"].as_array()?.iter().find_map(|source| {
        let reference = source["ref"].as_str()?;
        (source["name"] == "深圳证券交易所停复牌提示"
            && source["asOf"].as_u64() == Some(source_time)
            && reference.strip_prefix(SZSE_MEMO_PAGE) == Some(anchor.as_str()))
        .then_some(reference)
    })
}

fn date(value: &str) -> anyhow::Result<NaiveDate> {
    let parsed = NaiveDate::parse_from_str(value, "%Y-%m-%d")?;
    ensure!(parsed.to_string() == value, "noncanonical date");
    Ok(parsed)
}

struct ValidatedBatch<'a> {
    rules: Vec<(String, &'a Value)>,
    unavailable: Vec<(&'static str, String, &'a Value)>,
}

fn validated_rules<'a>(raw: &'a Value, cutoff: u64, scope: &Value) -> Option<ValidatedBatch<'a>> {
    let batch: RuleBatch = serde_json::from_value(raw.clone()).ok()?;
    if batch.version != 3
        || batch.rules.len().saturating_add(batch.unavailable.len()) > 500
        || cutoff > SAFE_QTY
    {
        return None;
    }
    let symbols: BTreeSet<_> = scope
        .as_array()?
        .iter()
        .filter_map(|item| item["symbol"].as_str())
        .collect();
    let trade_date = shanghai_date(cutoff)?.to_string();
    let mut keys = BTreeSet::new();
    let mut active_scopes = BTreeSet::new();
    let mut rules = Vec::new();
    for (index, row) in batch.rules.iter().enumerate() {
        let key = row.validate(cutoff).ok()?;
        if row.rule["tradeDate"] != trade_date || !symbols.contains(row.rule["symbol"].as_str()?) {
            return None;
        }
        if !keys.insert(key.clone()) {
            return None;
        }
        active_scopes.insert((
            row.rule["symbol"].as_str()?,
            row.rule["tradeDate"].as_str()?,
        ));
        rules.push((key, &raw["rules"][index]));
    }
    let mut unavailable = Vec::new();
    for (index, row) in batch.unavailable.iter().enumerate() {
        let key = format!("{}:{}", row.symbol, row.trade_date);
        let collection = match row.scope {
            Some(UnavailabilityScope::AuctionStatus) => AUCTION_STATUS_UNAVAILABLE,
            None => UNAVAILABLE,
        };
        if !keys.insert(format!("{collection}:{key}"))
            || (collection == UNAVAILABLE
                && active_scopes.contains(&(row.symbol.as_str(), row.trade_date.as_str())))
            || row.available_at_ms > cutoff
            || row.trade_date != trade_date
            || !symbols.contains(row.symbol.as_str())
            || !matches!(row.reason.as_str(), "conflict" | "withdrawn")
            || validate_definition("Symbol", &json!(row.symbol)).is_err()
        {
            return None;
        }
        unavailable.push((collection, key, &raw["unavailable"][index]));
    }
    Some(ValidatedBatch { rules, unavailable })
}

impl RuleEvidence {
    fn validate(&self, cutoff: u64) -> anyhow::Result<String> {
        let r = &self.rule;
        validate_definition("RuleSnapshot", r)?;
        let trade_date = r["tradeDate"].as_str().context("trade date")?;
        date(trade_date)?;
        let symbol = r["symbol"].as_str().context("rule symbol")?;
        let channel = r["channel"].as_str().context("rule channel")?;
        let sources = r["sources"].as_array().context("rule sources")?;
        ensure!(
            (symbol.ends_with(".SZ") && r["exchange"] == "SZSE"
                || symbol.ends_with(".SH") && r["exchange"] == "SSE")
                && !sources.is_empty()
                && self.rule_available_at_ms <= cutoff
                && sources
                    .iter()
                    .all(|s| valid_source(s, self.rule_available_at_ms))
                && r["sourceTime"]
                    .as_u64()
                    .is_some_and(|at| at <= self.rule_available_at_ms)
                && r["ruleVersion"]
                    .as_str()
                    .is_some_and(|s| !s.trim().is_empty()),
            "invalid rule provenance"
        );
        if let (Some(lower), Some(upper)) = (r["lowerLimit"].as_str(), r["upperLimit"].as_str()) {
            ensure!(
                decimal_cmp(lower, upper) != Ordering::Greater,
                "invalid price limits"
            );
        }
        self.calendar.validate(self.rule_available_at_ms)?;
        ensure!(
            self.calendar.session(trade_date).is_some(),
            "rule for closed session"
        );
        if let Some(through) = &self.company_actions_checked_through {
            date(through)?;
            ensure!(
                through.as_str() <= trade_date,
                "company actions cover a future session"
            );
        }
        Ok(format!("{symbol}:{trade_date}:{channel}"))
    }

    /// Reports a contradiction without discarding the declared execution.
    pub(super) fn conflict(&self, data: &Value, at: u64) -> Option<&'static str> {
        if self.rule_available_at_ms <= at
            && (self.rule["dataStatus"] == "conflict"
                || self
                    .unavailability
                    .as_ref()
                    .is_some_and(|state| at >= state.available_at_ms && state.reason == "conflict"))
        {
            return Some("RULE_SNAPSHOT_CONFLICT");
        }
        if !self.is_complete_at(at) {
            return None;
        }
        if self.rule["tradingStatus"] == "suspended" {
            return Some("EXECUTION_DURING_SUSPENSION");
        }
        if let (Some(claimed), Some(effective)) = (data["settlement"].as_str(), self.settlement(at))
        {
            if claimed != effective {
                return Some("SETTLEMENT_CONFLICT");
            }
        }
        let price = data["price"].as_str()?;
        if self.rule["lowerLimit"]
            .as_str()
            .is_some_and(|p| decimal_cmp(price, p) == Ordering::Less)
            || self.rule["upperLimit"]
                .as_str()
                .is_some_and(|p| decimal_cmp(price, p) == Ordering::Greater)
        {
            return Some("EXECUTION_PRICE_OUTSIDE_LIMITS");
        }
        None
    }

    fn is_complete_at(&self, at: u64) -> bool {
        self.rule["dataStatus"] == "complete" && self.is_known_at(at)
    }

    fn trading_evidence_newer_than(&self, available_at: u64, source_time: u64) -> bool {
        self.rule_available_at_ms > available_at
            && self.rule["sourceTime"]
                .as_u64()
                .is_some_and(|at| at > source_time)
    }

    fn is_known_at(&self, at: u64) -> bool {
        self.rule_available_at_ms <= at
            && self
                .unavailability
                .as_ref()
                .is_none_or(|state| at < state.available_at_ms)
            && self.rule["sourceTime"]
                .as_u64()
                .is_some_and(|source| source <= at)
            && self.calendar.available_at_ms <= at
    }

    pub(super) fn settlement(&self, at: u64) -> Option<&str> {
        if !self.is_complete_at(at) {
            return None;
        }
        match (
            self.rule["settlement"].as_str()?,
            self.rule["dayTrading"].as_str()?,
        ) {
            ("T+1", "NO") => Some("T+1"),
            ("T+0", "YES") if self.rule["instrumentType"] == "etf" => Some("T+0"),
            _ => None,
        }
    }

    pub(super) fn session_rechecked(&self, trade_date: &str, at: u64) -> bool {
        self.settlement(at).is_some()
            && self.rule["tradingStatus"] == "trading"
            && self
                .calendar
                .session(trade_date)
                .is_some_and(|s| s.first_execution_at_ms <= at)
    }

    pub(super) fn actions_checked(&self, trade_date: &str) -> bool {
        self.company_actions_checked_through.as_deref() == Some(trade_date)
    }

    pub(super) fn next_session(&self, trade_date: &str) -> Option<&str> {
        self.calendar
            .sessions
            .iter()
            .find(|s| s.date.as_str() > trade_date)
            .map(|s| s.date.as_str())
    }
}

impl Domain<'_, '_> {
    /// The next legal review point for a daily exit: after the next verified
    /// trading session closes. Unknown calendar evidence stays unknown.
    pub(crate) fn exit_review_at(&self, symbol: &str, now: u64) -> Option<u64> {
        let day = shanghai_date(now)?.to_string();
        let rule = self
            .holding_rule(symbol, &day, Some("auction"))
            .ok()
            .flatten()?;
        let next = rule.next_session(&day)?;
        let date = NaiveDate::parse_from_str(next, "%Y-%m-%d").ok()?;
        let stamp = date
            .and_hms_opt(15, 35, 0)?
            .and_local_timezone(chrono_tz::Asia::Shanghai)
            .single()?;
        u64::try_from(stamp.timestamp_millis()).ok()
    }
}

impl Calendar {
    fn validate(&self, cutoff: u64) -> anyhow::Result<()> {
        validate_definition("Source", &self.source)?;
        let start = date(&self.coverage_start)?;
        let end = date(&self.coverage_end)?;
        ensure!(
            start <= end
                && !self.revision_id.trim().is_empty()
                && self.revision_id.len() <= 128
                && valid_source(&self.source, self.available_at_ms)
                && self.available_at_ms <= cutoff
                && self.sessions.len() <= 370,
            "invalid trading calendar provenance"
        );
        let mut previous = None;
        for session in &self.sessions {
            let day = date(&session.date)?;
            ensure!(
                (start..=end).contains(&day)
                    && previous.is_none_or(|prev| day > prev)
                    && session.first_execution_at_ms <= SAFE_QTY
                    && shanghai_date(session.first_execution_at_ms) == Some(day),
                "invalid calendar session"
            );
            previous = Some(day);
        }
        Ok(())
    }

    fn session(&self, trade_date: &str) -> Option<&Session> {
        self.sessions.iter().find(|s| s.date == trade_date)
    }
}

fn valid_source(value: &Value, cutoff: u64) -> bool {
    value["asOf"].as_u64().is_some_and(|at| at <= cutoff)
        && ["name", "ref"]
            .iter()
            .all(|key| value[key].as_str().is_some_and(|s| !s.trim().is_empty()))
}
