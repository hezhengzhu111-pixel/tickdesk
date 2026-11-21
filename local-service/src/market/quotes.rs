use crate::{commands::Outcome, domain::Domain};
use chrono::{DateTime, NaiveDate, Timelike, Utc};
use rusqlite::{OptionalExtension, Transaction};
use serde_json::{json, Value};

pub(super) const QUOTE_TTL_MS: u64 = 90_000;

pub(super) fn session(response: &Value) -> &'static str {
    match response["quoteFeed"]["session"].as_str() {
        Some("open") => "open",
        Some("closed") => "closed",
        _ => "unknown",
    }
}

fn open_session(at: u64) -> Option<(NaiveDate, bool)> {
    let local = DateTime::<Utc>::from_timestamp_millis(at.try_into().ok()?)?
        .with_timezone(&chrono_tz::Asia::Shanghai);
    let minute = local.hour() * 60 + local.minute();
    let afternoon = (780..900).contains(&minute);
    ((555..690).contains(&minute) || afternoon).then(|| (local.date_naive(), afternoon))
}

pub(super) fn current_in_session(at: u64, now: u64) -> bool {
    at <= now
        && now - at <= QUOTE_TTL_MS
        && open_session(now).is_some_and(|span| open_session(at) == Some(span))
}

pub(super) fn verified_suspension(
    tx: &Transaction<'_>,
    symbol: &str,
    now: u64,
) -> anyhow::Result<bool> {
    let scope: Option<(String, String)> = tx.query_row(
        "SELECT pair_id,(SELECT value FROM metadata WHERE key='server_epoch') FROM peer WHERE singleton=1",
        [], |row| Ok((row.get(0)?, row.get(1)?)),
    ).optional()?;
    let (Some((pair, epoch)), Some((day, _))) = (scope, open_session(now)) else {
        return Ok(false);
    };
    let domain = Domain {
        tx,
        data: &Value::Null,
        pair: &pair,
        epoch: &epoch,
        recovering: false,
        now,
    };
    Ok(domain
        .verified_auction_status_at(symbol, &day.to_string(), now)?
        .is_some_and(|rule| rule["tradingStatus"] == "suspended"))
}

pub(super) fn read(d: &Domain<'_, '_>, response: Option<&Value>) -> anyhow::Result<Outcome> {
    let response = response.unwrap_or(&Value::Null);
    let phase = session(response);
    let day = DateTime::<Utc>::from_timestamp_millis(d.now as i64).map(|at| {
        at.with_timezone(&chrono_tz::Asia::Shanghai)
            .date_naive()
            .to_string()
    });
    let mut items = Vec::new();
    let returned = response["quotes"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or_default();
    for symbol in d.data["symbols"].as_array().unwrap() {
        let symbol = symbol.as_str().unwrap();
        let quote = d.get("market_quotes", symbol)?.unwrap_or(Value::Null);
        let trading = match &day {
            Some(day) => d.verified_auction_status_at(symbol, day, d.now)?,
            None => None,
        };
        let trading = match trading
            .as_ref()
            .and_then(|rule| rule["tradingStatus"].as_str())
        {
            Some("suspended") => "suspended",
            Some("trading") => "trading",
            _ => "unknown",
        };
        let delivery = match response["quoteFeed"]["delivery"][symbol].as_str() {
            Some("available") => "available",
            Some("missing") => "missing",
            Some("rate_limited") => "rate_limited",
            Some("offline") => "offline",
            Some("unavailable") => "unavailable",
            _ if response.is_null() || response["error"] == "DATA_UNAVAILABLE" => "offline",
            _ => "unknown",
        };
        let same_quote = returned.iter().any(|sample| {
            sample["symbol"] == symbol
                && sample["asOf"] == quote["asOf"]
                && sample["price"] == quote["price"]
                && sample["source"] == quote["source"]
                && sample["timeBasis"] == quote["timeBasis"]
        });
        let quality = match quote["asOf"].as_u64() {
            None => "missing",
            Some(at)
                if same_quote
                    && phase == "open"
                    && trading != "suspended"
                    && delivery == "available"
                    && current_in_session(at, d.now) =>
            {
                "fresh"
            }
            Some(_) => "stale",
        };
        items.push(
            json!({"symbol":symbol,"price":quote["price"],"asOf":quote["asOf"],
            "source":quote["source"],"timeBasis":quote["timeBasis"],"quality":quality,
            "delivery":delivery,"tradingStatus":trading}),
        );
    }
    Ok(Ok(json!({"session":phase,"items":items})))
}
