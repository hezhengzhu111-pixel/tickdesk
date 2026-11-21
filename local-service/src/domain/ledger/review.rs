//! Versioned actual-trade review: FIFO pairing over recorded executions.
//!
//! This is a reproducible candidate basis, not a broker cost-basis claim. Fees
//! exist only when the user declared them on the recorded execution.
//!
//! Amounts stay in a signed decimal type until the response is serialized.
//! Parsing a formatted negative amount back into an unsigned number used to
//! collapse losses to zero and turn "net known but negative" into "fee unknown".
//! Fee allocation keeps the original lot quantity and the original declared fee
//! so a fee is never re-allocated from an already reduced remaining quantity.
use super::{failure, str_at, Domain, Outcome};
use num_bigint::BigInt;
use serde_json::{json, Value};
use std::cmp::Ordering;
use std::collections::{BTreeMap, BTreeSet};

const VERSION: u64 = 1;
const PAIRING: &str = "fifo";
const MAX_ITEMS: usize = 200;
const DEFAULT_ITEMS: u64 = 50;
/// Fixed, explicit precision for fee slices. The final slice of a unit absorbs
/// the remainder so allocated fees always add back to the declared total.
const FEE_SCALE: u32 = 8;

#[derive(Clone, Debug, PartialEq, Eq)]
struct Dec {
    value: BigInt,
    scale: u32,
}

impl Dec {
    fn zero() -> Self {
        Self {
            value: BigInt::from(0_u32),
            scale: 0,
        }
    }

    fn parse(text: &str) -> Option<Self> {
        let (negative, text) = match text.strip_prefix('-') {
            Some(rest) => (true, rest),
            None => (false, text),
        };
        let (whole, fraction) = text.split_once('.').unwrap_or((text, ""));
        if whole.is_empty()
            || fraction.len() > 40
            || !whole.bytes().all(|b| b.is_ascii_digit())
            || !fraction.bytes().all(|b| b.is_ascii_digit())
        {
            return None;
        }
        let digits = format!("{whole}{fraction}").parse::<BigInt>().ok()?;
        Some(Self {
            value: if negative { -digits } else { digits },
            scale: fraction.len() as u32,
        })
    }

    fn align(&self, other: &Self) -> (BigInt, BigInt, u32) {
        let scale = self.scale.max(other.scale);
        let ten = BigInt::from(10_u32);
        (
            &self.value * ten.pow(scale - self.scale),
            &other.value * ten.pow(scale - other.scale),
            scale,
        )
    }

    fn add(&self, other: &Self) -> Self {
        let (a, b, scale) = self.align(other);
        Self {
            value: a + b,
            scale,
        }
    }

    fn sub(&self, other: &Self) -> Self {
        let (a, b, scale) = self.align(other);
        Self {
            value: a - b,
            scale,
        }
    }

    fn mul_int(&self, qty: u64) -> Self {
        Self {
            value: &self.value * BigInt::from(qty),
            scale: self.scale,
        }
    }

    fn cmp_zero(&self) -> Ordering {
        self.value.cmp(&BigInt::from(0_u32))
    }

    /// Round a non-negative cumulative share down to `FEE_SCALE` digits.
    fn share(&self, numerator: u64, denominator: u64) -> Self {
        if denominator == 0 {
            return Self::zero();
        }
        let ten = BigInt::from(10_u32);
        let value = &self.value * BigInt::from(numerator) * ten.pow(FEE_SCALE)
            / (BigInt::from(denominator) * ten.pow(self.scale));
        Self {
            value,
            scale: FEE_SCALE,
        }
    }

    fn format(&self) -> String {
        let negative = self.value.sign() == num_bigint::Sign::Minus;
        let digits = self.value.magnitude().to_string();
        let mut text = if self.scale == 0 {
            digits
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
        if text == "0" {
            return text;
        }
        if negative {
            text.insert(0, '-');
        }
        text
    }
}

/// Tracks cumulative allocation for one fee-bearing unit (a buy lot or one sell
/// execution). `original_qty` never changes; only `allocated_qty` advances.
struct FeeAllocation {
    total: Option<Dec>,
    allocated: Dec,
    allocated_qty: u64,
    original_qty: u64,
}

impl FeeAllocation {
    fn new(total: Option<Dec>, original_qty: u64) -> Self {
        Self {
            total,
            allocated: Dec::zero(),
            allocated_qty: 0,
            original_qty,
        }
    }

    fn next(&mut self, used: u64) -> Option<Dec> {
        let total = self.total.as_ref()?;
        let cumulative_qty = self.allocated_qty.checked_add(used)?;
        let cumulative = if cumulative_qty >= self.original_qty {
            total.clone()
        } else {
            total.share(cumulative_qty, self.original_qty)
        };
        let slice = cumulative.sub(&self.allocated);
        self.allocated_qty = cumulative_qty;
        self.allocated = cumulative;
        Some(slice)
    }
}

struct Lot {
    remaining_qty: u64,
    price: Dec,
    fee: FeeAllocation,
    at: u64,
    execution_id: String,
    task_id: Value,
    entry_key: String,
}

#[derive(Default)]
struct EntryGroup {
    remaining_qty: u64,
    buy_execution_ids: BTreeSet<String>,
    opened_at: u64,
    task_id: Value,
    symbol: String,
}

struct EntrySample {
    entry_key: String,
    symbol: String,
    buy_execution_ids: Vec<String>,
    task_id: Value,
    qty: u64,
    gross: Dec,
    net: Option<Dec>,
    opened_at: u64,
    closed_at: u64,
}

#[derive(Clone)]
struct MatchSlice {
    symbol: String,
    entry_key: String,
    task_id: Value,
    buy_execution_id: String,
    sell_execution_id: String,
    qty: u64,
    buy_price: Dec,
    sell_price: Dec,
    gross: Dec,
    buy_fee: Option<Dec>,
    sell_fee: Option<Dec>,
    net: Option<Dec>,
    opened_at: u64,
    closed_at: u64,
    in_period: bool,
}

impl Domain<'_, '_> {
    pub(crate) fn trade_review(&self) -> anyhow::Result<Outcome> {
        let symbol = self.data["symbol"].as_str();
        let from = self.data["from"].as_u64();
        let to = self.data["to"].as_u64();
        if from.zip(to).is_some_and(|(from, to)| from > to) {
            return Ok(failure("INVALID_PARAMS"));
        }
        let limit = self.data["limit"]
            .as_u64()
            .unwrap_or(DEFAULT_ITEMS)
            .clamp(1, MAX_ITEMS as u64) as usize;
        // History before `from` still has to feed FIFO: it owns the opening
        // cost and the quantity already consumed. Only the closing fragment's
        // own timestamp decides whether it belongs to the reporting period.
        // `to` is an exclusive upper bound, so later fills cannot move the
        // period-end open lots.
        let cutoff = to.unwrap_or(u64::MAX);
        let mut executions = self
            .list("executions")?
            .into_iter()
            .filter(|row| {
                symbol.is_none_or(|symbol| row["symbol"] == symbol)
                    && row["executedAt"].as_u64().is_some_and(|at| at < cutoff)
            })
            .collect::<Vec<_>>();
        executions.sort_by(|a, b| {
            a["executedAt"]
                .as_u64()
                .cmp(&b["executedAt"].as_u64())
                .then_with(|| a["executionId"].as_str().cmp(&b["executionId"].as_str()))
        });

        let in_period = |at: u64| from.is_none_or(|from| at >= from) && to.is_none_or(|to| at < to);
        let mut lots: BTreeMap<String, Vec<Lot>> = Default::default();
        let mut groups: BTreeMap<String, EntryGroup> = Default::default();
        let mut slices: Vec<MatchSlice> = Vec::new();
        let mut exclusions = Vec::new();
        for execution in &executions {
            let id = execution["executionId"].as_str().unwrap_or_default();
            if execution["positionEffect"] == "superseded" {
                if in_period(execution["executedAt"].as_u64().unwrap_or(0)) {
                    exclusions.push(json!({"executionId":id,"reason":"superseded"}));
                }
                continue;
            }
            // `needs_reconciliation` describes the projected position snapshot,
            // not whether the user actually declared the fill. The review basis
            // is the recorded execution, so the fill still pairs here; position
            // reconciliation remains a separate gate.
            let Some(qty) = execution["qty"].as_u64().filter(|qty| *qty > 0) else {
                continue;
            };
            let Some(price) = Dec::parse(str_at(execution, "price")) else {
                continue;
            };
            let fees = execution["fees"]
                .as_str()
                .and_then(Dec::parse)
                .filter(|fee| fee.cmp_zero() != Ordering::Less)
                .filter(|_| execution["feeSource"] == "user");
            let Some(at) = execution["executedAt"].as_u64() else {
                continue;
            };
            let execution_symbol = str_at(execution, "symbol").to_owned();
            let task_id = execution["taskId"].clone();
            let entry_key = task_id
                .as_str()
                .map(|task| format!("task:{task}"))
                .unwrap_or_else(|| format!("buy:{id}"));
            let bucket = lots.entry(execution_symbol.clone()).or_default();
            if execution["side"] == "buy" {
                let group = groups
                    .entry(entry_key.clone())
                    .or_insert_with(|| EntryGroup {
                        opened_at: at,
                        task_id: task_id.clone(),
                        symbol: execution_symbol.clone(),
                        ..Default::default()
                    });
                group.remaining_qty = group.remaining_qty.saturating_add(qty);
                group.opened_at = group.opened_at.min(at);
                group.buy_execution_ids.insert(id.to_owned());
                bucket.push(Lot {
                    remaining_qty: qty,
                    price,
                    fee: FeeAllocation::new(fees, qty),
                    at,
                    execution_id: id.to_owned(),
                    task_id,
                    entry_key,
                });
                continue;
            }
            let mut sell_fee = FeeAllocation::new(fees, qty);
            let mut remaining = qty;
            while remaining > 0 {
                let Some(lot) = bucket.first_mut() else {
                    break;
                };
                let used = remaining.min(lot.remaining_qty);
                let gross = price.sub(&lot.price).mul_int(used);
                let buy_fee = lot.fee.next(used);
                let sell_slice = sell_fee.next(used);
                let net = match (&buy_fee, &sell_slice) {
                    (Some(buy), Some(sell)) => Some(gross.sub(buy).sub(sell)),
                    _ => None,
                };
                if let Some(group) = groups.get_mut(&lot.entry_key) {
                    group.remaining_qty = group.remaining_qty.saturating_sub(used);
                }
                slices.push(MatchSlice {
                    symbol: execution_symbol.clone(),
                    entry_key: lot.entry_key.clone(),
                    task_id: lot.task_id.clone(),
                    buy_execution_id: lot.execution_id.clone(),
                    sell_execution_id: id.to_owned(),
                    qty: used,
                    buy_price: lot.price.clone(),
                    sell_price: price.clone(),
                    gross,
                    buy_fee,
                    sell_fee: sell_slice,
                    net,
                    opened_at: lot.at,
                    closed_at: at,
                    in_period: in_period(at),
                });
                lot.remaining_qty -= used;
                remaining -= used;
                if lot.remaining_qty == 0 {
                    bucket.remove(0);
                }
            }
            if remaining > 0 && in_period(at) {
                exclusions.push(json!({"executionId":id,"reason":"missing_buy_lot"}));
            }
        }
        let open_lots: u64 = lots.values().map(|bucket| bucket.len() as u64).sum();
        let open_qty: u64 = lots
            .values()
            .flat_map(|bucket| bucket.iter())
            .map(|lot| lot.remaining_qty)
            .sum();

        let sample_set = entry_samples(&slices, &groups, from, to);
        let summary = summarize(&slices, open_lots, open_qty, exclusions.len(), &sample_set);
        let all_items = slices
            .iter()
            .filter(|slice| slice.in_period)
            .map(item_json)
            .collect::<Vec<_>>();
        let all_samples = sample_set
            .iter()
            .map(|sample| sample_page_json(sample, 0, 50))
            .collect::<anyhow::Result<Vec<_>>>()?;
        let view = self.data["view"].as_str().unwrap_or("items");
        if !matches!(view, "items" | "samples" | "exclusions") {
            return Ok(failure("INVALID_PARAMS"));
        }
        // The cursor binds the complete normalized query and all three
        // result sets, not just the item page. A price revision, a new
        // exclusion, or a different filter invalidates every view cursor.
        let digest = crate::contract::digest(&json!({
            "pair":self.pair,"epoch":self.epoch,"symbol":symbol,
            "from":from,"to":to,"basis":PAIRING,"version":VERSION,
            "items":all_items,"samples":all_samples,"exclusions":exclusions,"summary":summary
        }))?;
        if let Some(entry_key) = self.data["sampleEntryKey"].as_str() {
            if view != "samples" {
                return Ok(failure("INVALID_PARAMS"));
            }
            let Some(sample) = sample_set
                .iter()
                .find(|sample| sample.entry_key == entry_key)
            else {
                return Ok(failure("INVALID_PARAMS"));
            };
            let digest = crate::contract::digest(&json!(sample.buy_execution_ids))?;
            let offset = match self.data["sampleCursor"].as_str() {
                Some(cursor) => {
                    let parts = cursor.split(':').collect::<Vec<_>>();
                    if parts.len() != 3 || parts[0] != "v2" || parts[2] != digest {
                        return Ok(failure("TRADE_REVIEW_CURSOR_EXPIRED"));
                    }
                    let Some(offset) = parts[1].parse::<usize>().ok() else {
                        return Ok(failure("TRADE_REVIEW_CURSOR_EXPIRED"));
                    };
                    if offset > sample.buy_execution_ids.len() {
                        return Ok(failure("TRADE_REVIEW_CURSOR_EXPIRED"));
                    }
                    offset
                }
                None => 0,
            };
            let page = sample_page_json(sample, offset, limit)?;
            return Ok(Ok(json!({
                "version":VERSION,
                "basis":{"pairing":PAIRING,"version":VERSION,"priceBasis":"recorded_execution",
                         "feeSource":"user_declared","holdingPeriod":"calendar_days_asia_shanghai",
                         "asOf":self.now},
                "period":{"from":from,"to":to,"fromInclusive":true,"toExclusive":true,
                          "timezone":"Asia/Shanghai"},
                "summary":summary,
                "items":[],
                "exclusions":[],
                "samples":[page],
                "nextCursor":Value::Null,
                "hasMore":false,
            })));
        }
        let selected = match view {
            "samples" => &all_samples,
            "exclusions" => &exclusions,
            _ => &all_items,
        };
        let (offset, as_of) = match self.data["cursor"].as_str() {
            Some(cursor) => {
                let parts = cursor.split(':').collect::<Vec<_>>();
                if parts.len() != 5 || parts[0] != "v2" || parts[1] != view || parts[4] != digest {
                    return Ok(failure("TRADE_REVIEW_CURSOR_EXPIRED"));
                }
                let Some(offset) = parts[2].parse::<usize>().ok() else {
                    return Ok(failure("TRADE_REVIEW_CURSOR_EXPIRED"));
                };
                let Some(as_of) = parts[3].parse::<u64>().ok() else {
                    return Ok(failure("TRADE_REVIEW_CURSOR_EXPIRED"));
                };
                if offset > selected.len() {
                    return Ok(failure("TRADE_REVIEW_CURSOR_EXPIRED"));
                }
                (offset, as_of)
            }
            None => (0, self.now),
        };
        let total = selected.len();
        let mut len = limit.min(total.saturating_sub(offset));
        loop {
            let page: Vec<Value> = selected[offset..offset + len].to_vec();
            let next_cursor = (offset + len < total)
                .then(|| format!("v2:{view}:{}:{as_of}:{digest}", offset + len));
            let (items, samples, exclusion_body) = match view {
                "samples" => (Vec::new(), page, Vec::new()),
                "exclusions" => (Vec::new(), Vec::new(), page),
                _ => {
                    let page: Vec<Value> = page;
                    (page, Vec::new(), Vec::new())
                }
            };
            let body = json!({
                "version":VERSION,
                "basis":{"pairing":PAIRING,"version":VERSION,"priceBasis":"recorded_execution",
                         "feeSource":"user_declared","holdingPeriod":"calendar_days_asia_shanghai",
                         "asOf":as_of},
                "period":{"from":from,"to":to,"fromInclusive":true,"toExclusive":true,
                          "timezone":"Asia/Shanghai"},
                "summary":summary,
                "items":items,
                "exclusions":exclusion_body,
                "samples":samples,
                "nextCursor":next_cursor,
                "hasMore":next_cursor.is_some(),
            });
            let size = serde_json::to_vec(&body).map_or(0, |bytes| bytes.len());
            if size <= 58_000 {
                return Ok(Ok(body));
            }
            if len == 0 {
                return Ok(failure("MESSAGE_TOO_LARGE"));
            }
            len -= 1;
        }
    }
}

fn summarize(
    slices: &[MatchSlice],
    open_lots: u64,
    open_qty: u64,
    excluded: usize,
    samples: &[EntrySample],
) -> Value {
    let closed: Vec<&MatchSlice> = slices.iter().filter(|slice| slice.in_period).collect();
    let mut gross_total = Dec::zero();
    let mut net_total = Dec::zero();
    let mut missing_fee = 0_u64;
    let (mut wins, mut losses, mut flat) = (0_u64, 0_u64, 0_u64);
    let (mut price_wins, mut price_losses, mut price_flat) = (0_u64, 0_u64, 0_u64);
    for slice in &closed {
        gross_total = gross_total.add(&slice.gross);
        match slice.gross.cmp_zero() {
            Ordering::Greater => price_wins += 1,
            Ordering::Less => price_losses += 1,
            Ordering::Equal => price_flat += 1,
        }
        match &slice.net {
            Some(net) => {
                net_total = net_total.add(net);
                match net.cmp_zero() {
                    Ordering::Greater => wins += 1,
                    Ordering::Less => losses += 1,
                    Ordering::Equal => flat += 1,
                }
            }
            None => missing_fee += 1,
        }
    }
    let net_known = closed.len() as u64 - missing_fee;
    let (mut sample_wins, mut sample_losses, mut sample_flat) = (0_u64, 0_u64, 0_u64);
    let (mut sample_missing, mut sample_gross, mut sample_net) = (0_u64, Dec::zero(), Dec::zero());
    for sample in samples {
        sample_gross = sample_gross.add(&sample.gross);
        match &sample.net {
            Some(net) => {
                sample_net = sample_net.add(net);
                match net.cmp_zero() {
                    Ordering::Greater => sample_wins += 1,
                    Ordering::Less => sample_losses += 1,
                    Ordering::Equal => sample_flat += 1,
                }
            }
            None => sample_missing += 1,
        }
    }
    let closed_samples = samples.len() as u64;
    json!({
        "closedTrades":closed.len() as u64,
        "wins":wins, "losses":losses, "flat":flat,
        "netKnownClosedTrades":net_known,
        "priceWins":price_wins, "priceLosses":price_losses, "priceFlat":price_flat,
        "priceWinRateBasisPoints":win_rate(price_wins, price_losses),
        "openLots":open_lots, "openQty":open_qty,
        "grossPnl":(!closed.is_empty()).then(|| gross_total.format()),
        "netPnl":(!closed.is_empty() && missing_fee == 0).then(|| net_total.format()),
        "netPnlComplete":missing_fee == 0,
        "winRateBasisPoints":win_rate(wins, losses),
        "closedSamples":closed_samples,
        "sampleWins":sample_wins, "sampleLosses":sample_losses, "sampleFlat":sample_flat,
        "sampleNetKnown":closed_samples - sample_missing,
        "sampleGrossPnl":(closed_samples > 0).then(|| sample_gross.format()),
        "sampleNetPnl":(closed_samples > 0 && sample_missing == 0)
            .then(|| sample_net.format()),
        "sampleNetPnlComplete":sample_missing == 0,
        "sampleWinRateBasisPoints":win_rate(sample_wins, sample_losses),
        "excludedCount":excluded,
        "missingFeeCount":missing_fee,
    })
}

/// A fully closed entry unit. Grouping by task keeps one entry decision from
/// becoming several hit-rate samples merely because it was sold in pieces.
/// The group is classified by its final close, while its amounts include every
/// slice, including slices that closed before `from`.
fn entry_samples(
    slices: &[MatchSlice],
    groups: &BTreeMap<String, EntryGroup>,
    from: Option<u64>,
    to: Option<u64>,
) -> Vec<EntrySample> {
    let in_window = |at: u64| from.is_none_or(|from| at >= from) && to.is_none_or(|to| at < to);
    let mut result = Vec::new();
    for (key, group) in groups {
        if group.remaining_qty != 0 {
            continue;
        }
        let mut closed_at = 0;
        let mut opened_at = u64::MAX;
        let mut gross = Dec::zero();
        let mut net = Dec::zero();
        let mut qty = 0_u64;
        let mut missing = 0_u64;
        for slice in slices.iter().filter(|slice| &slice.entry_key == key) {
            opened_at = opened_at.min(slice.opened_at);
            closed_at = closed_at.max(slice.closed_at);
            gross = gross.add(&slice.gross);
            qty = qty.saturating_add(slice.qty);
            match &slice.net {
                Some(value) => net = net.add(value),
                None => missing += 1,
            }
        }
        if qty == 0 || !in_window(closed_at) {
            continue;
        }
        result.push(EntrySample {
            entry_key: key.clone(),
            symbol: group.symbol.clone(),
            buy_execution_ids: group.buy_execution_ids.iter().cloned().collect(),
            task_id: group.task_id.clone(),
            qty,
            gross,
            net: (missing == 0).then_some(net),
            opened_at,
            closed_at,
        });
    }
    result.sort_by(|a, b| {
        a.closed_at
            .cmp(&b.closed_at)
            .then_with(|| a.opened_at.cmp(&b.opened_at))
            .then_with(|| a.entry_key.cmp(&b.entry_key))
    });
    result
}

fn sample_page_json(sample: &EntrySample, offset: usize, limit: usize) -> anyhow::Result<Value> {
    let total = sample.buy_execution_ids.len();
    let page = sample
        .buy_execution_ids
        .iter()
        .skip(offset)
        .take(limit.min(50))
        .cloned()
        .collect::<Vec<_>>();
    let next = (offset + page.len() < total).then(|| {
        format!(
            "v2:{}:{}",
            offset + page.len(),
            crate::contract::digest(&json!(sample.buy_execution_ids)).unwrap_or_default()
        )
    });
    Ok(sample_json(sample, page, total, next))
}

fn sample_json(
    sample: &EntrySample,
    buy_execution_ids: Vec<String>,
    total: usize,
    next: Option<String>,
) -> Value {
    json!({
        "entryKey":sample.entry_key,
        "symbol":sample.symbol,
        "buyExecutionIds":buy_execution_ids,
        "buyExecutionCount":total,
        "buyExecutionIdsNextCursor":next,
        "taskId":sample.task_id,
        "qty":sample.qty,
        "grossPnl":sample.gross.format(),
        "netPnl":sample.net.as_ref().map(Dec::format),
        "feeSource":if sample.net.is_some() {"user"} else {"unknown"},
        "openedAt":sample.opened_at,
        "closedAt":sample.closed_at,
        "holdingDays":holding_days(sample.opened_at, sample.closed_at),
    })
}

fn item_json(slice: &MatchSlice) -> Value {
    json!({
        "symbol":slice.symbol, "buyExecutionId":slice.buy_execution_id,
        "sellExecutionId":slice.sell_execution_id, "qty":slice.qty,
        "buyPrice":slice.buy_price.format(), "sellPrice":slice.sell_price.format(),
        "grossPnl":slice.gross.format(),
        "buyFee":slice.buy_fee.as_ref().map(Dec::format),
        "sellFee":slice.sell_fee.as_ref().map(Dec::format),
        "netPnl":slice.net.as_ref().map(Dec::format),
        "openedAt":slice.opened_at, "closedAt":slice.closed_at,
        "holdingDays":holding_days(slice.opened_at, slice.closed_at),
        "taskId":slice.task_id,
        "feeSource":if slice.net.is_some() {"user"} else {"unknown"},
    })
}

fn win_rate(wins: u64, losses: u64) -> Option<u64> {
    let decided = wins.checked_add(losses)?;
    (decided > 0).then(|| wins.saturating_mul(10_000) / decided)
}

fn holding_days(opened: u64, closed: u64) -> u64 {
    let day = |at: u64| {
        chrono::DateTime::from_timestamp_millis(at.try_into().ok()?)
            .map(|stamp| stamp.with_timezone(&chrono_tz::Asia::Shanghai).date_naive())
    };
    day(closed).zip(day(opened)).map_or(0, |(closed, opened)| {
        (closed - opened).num_days().max(0) as u64
    })
}
