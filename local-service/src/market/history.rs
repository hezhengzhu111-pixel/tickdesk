use crate::{commands::Outcome, contract::validate_definition, domain::Domain};
use rusqlite::{params, Connection};
use serde_json::{json, Value};

pub(super) const VERSION: u64 = 2;
const PROBE_TTL_MS: u64 = 90_000;

fn unavailable(reason: &str, checked_at: Option<u64>) -> Value {
    json!({"status":"unavailable","checkedAt":checked_at,"coverage":null,"reason":reason})
}

fn attribution_consistent(coverage: &Value) -> bool {
    let attribution = &coverage["attribution"];
    if attribution.is_null() {
        return true;
    }
    let (Some(missing), Some(attributed), Some(unexplained)) = (
        attribution["missingCells"].as_u64(),
        attribution["attributedCells"].as_u64(),
        attribution["unexplainedCells"].as_u64(),
    ) else {
        return false;
    };
    let (Some(window_start), Some(window_end), Some(through), Some(start)) = (
        attribution["window"]["start"].as_str(),
        attribution["window"]["end"].as_str(),
        coverage["through"].as_str(),
        coverage["start"].as_str(),
    ) else {
        return false;
    };
    let all_attributed = missing == attributed && unexplained == 0;
    let status_matches = attribution["status"].as_str()
        == Some(if all_attributed {
            "all_attributed"
        } else {
            "partial"
        });
    attributed <= missing
        && missing == attributed + unexplained
        && status_matches
        && window_start >= start
        && window_end == through
        && attribution["sources"]["archiveDays"]
            .as_u64()
            .is_some_and(|days| days > 0)
        && attribution["archiveSignature"]
            .as_str()
            .is_some_and(|value| value.len() == 64)
}

fn usable(snapshot: &Value, now: u64) -> bool {
    if validate_definition("result_get_history_coverage", snapshot).is_err() {
        return false;
    }
    let coverage = &snapshot["coverage"];
    match snapshot["status"].as_str() {
        Some("ready" | "partial") if coverage.is_object() => {
            let start = coverage["start"].as_str();
            let through = coverage["through"].as_str();
            let target = coverage["expectedThrough"].as_str();
            let requested = coverage["requestedSymbols"].as_u64();
            let observed = coverage["observedSymbols"].as_u64();
            let latest = &coverage["latestDay"];
            let latest_observed = latest["observed"].as_u64();
            let latest_expected = latest["expected"].as_u64();
            coverage["updatedAt"].as_u64().is_some_and(|at| at <= now)
                && start <= through
                && through <= target
                && latest_observed <= latest_expected
                && latest_expected <= requested
                && latest_observed <= observed
                && observed <= requested
                && observed <= coverage["rows"].as_u64()
                && attribution_consistent(coverage)
                && (snapshot["status"] == "partial"
                    || (through == target
                        && latest_observed == latest_expected
                        && coverage["conflictingRows"] == 0
                        && snapshot["reason"].is_null()))
        }
        Some("unavailable") => coverage.is_null() && !snapshot["reason"].is_null(),
        Some("disabled") => coverage.is_null() && snapshot["reason"].is_null(),
        _ => false,
    }
}

pub(super) fn persist(
    db: &Connection,
    epoch: &str,
    response: &Value,
    now: u64,
) -> anyhow::Result<()> {
    let Some(raw) = response.get("historyCoverage") else {
        return Ok(());
    };
    let mut snapshot = unavailable("snapshot_invalid", Some(now));
    if raw.is_object() {
        let mut candidate = raw.clone();
        candidate["checkedAt"] = json!(now);
        if usable(&candidate, now) {
            snapshot = candidate;
        }
    }
    db.execute("INSERT INTO objects(collection,object_id,body) VALUES ('market_meta','history_coverage',?1)
                ON CONFLICT(collection,object_id) DO UPDATE SET body=excluded.body",
               params![json!({"epoch":epoch,"result":snapshot}).to_string()])?;
    Ok(())
}

pub(crate) fn read(domain: &Domain<'_, '_>) -> anyhow::Result<Outcome> {
    let snapshot = domain
        .get("market_meta", "history_coverage")?
        .filter(|row| row["epoch"] == domain.epoch)
        .map(|mut row| row["result"].take());
    let Some(snapshot) = snapshot else {
        return Ok(Ok(unavailable("history_pending", None)));
    };
    let checked = snapshot["checkedAt"].as_u64();
    if checked.is_some_and(|at| domain.now >= at && domain.now - at <= PROBE_TTL_MS) {
        return Ok(Ok(snapshot));
    }
    let mut stale = unavailable("source_unavailable", checked.filter(|at| *at <= domain.now));
    if stale["checkedAt"].is_number() {
        stale["coverage"] = snapshot["coverage"].clone();
    }
    Ok(Ok(stale))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn persisted_coverage_validates_time_and_count_relationships() -> anyhow::Result<()> {
        let directory = tempfile::tempdir()?;
        let storage = crate::storage::Storage::open(directory.path())?;
        let mut coverage = json!({"status":"partial","reason":null,"coverage":{
            "source":"fuyao","start":"2026-01-20","through":"2026-11-13","expectedThrough":"2026-11-16",
            "updatedAt":1000,"rows":16,"observedSymbols":5,"requestedSymbols":6,"conflictingRows":0,
            "latestDay":{"observed":4,"expected":6}}});
        let read = || -> anyhow::Result<Value> {
            let body: String = storage.connection.query_row(
                "SELECT body FROM objects WHERE collection='market_meta' AND object_id='history_coverage'",
                [], |row| row.get(0),
            )?;
            Ok(serde_json::from_str::<Value>(&body)?["result"].clone())
        };
        persist(
            &storage.connection,
            "epoch",
            &json!({"historyCoverage":coverage}),
            1000,
        )?;
        assert_eq!(read()?["coverage"], coverage["coverage"]);
        coverage["coverage"]["latestDay"]["observed"] = json!(7);
        persist(
            &storage.connection,
            "epoch",
            &json!({"historyCoverage":coverage}),
            2000,
        )?;
        assert_eq!(read()?, unavailable("snapshot_invalid", Some(2000)));
        coverage["coverage"]["latestDay"]["observed"] = json!(4);
        coverage["coverage"]["updatedAt"] = json!(3001);
        persist(
            &storage.connection,
            "epoch",
            &json!({"historyCoverage":coverage}),
            3000,
        )?;
        assert_eq!(read()?, unavailable("snapshot_invalid", Some(3000)));
        Ok(())
    }

    #[test]
    fn persisted_attribution_must_be_internally_consistent() -> anyhow::Result<()> {
        let directory = tempfile::tempdir()?;
        let storage = crate::storage::Storage::open(directory.path())?;
        let base = json!({"status":"partial","reason":null,"coverage":{
            "source":"fuyao","start":"2026-01-21","through":"2026-11-17","expectedThrough":"2026-11-17",
            "updatedAt":1000,"rows":16,"observedSymbols":5,"requestedSymbols":6,"conflictingRows":0,
            "latestDay":{"observed":4,"expected":6},"attribution":{
                "status":"all_attributed","window":{"start":"2026-01-21","end":"2026-11-17","sessions":1},
                "missingCells":1,"attributedCells":1,"unexplainedCells":0,
                "sources":{"sseDays":1,"szseDays":1,"archiveDays":2},
                "archiveSignature":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                "unexplained":[]}}});
        let read = || -> anyhow::Result<Value> {
            let body: String = storage.connection.query_row(
                "SELECT body FROM objects WHERE collection='market_meta' AND object_id='history_coverage'",
                [], |row| row.get(0),
            )?;
            Ok(serde_json::from_str::<Value>(&body)?["result"].clone())
        };
        persist(
            &storage.connection,
            "epoch",
            &json!({"historyCoverage":base}),
            1000,
        )?;
        let persisted = read()?;
        assert_eq!(persisted["coverage"], base["coverage"]);
        assert_eq!(persisted["status"], base["status"]);
        let mut wrong_count = base.clone();
        wrong_count["coverage"]["attribution"]["unexplainedCells"] = json!(1);
        persist(
            &storage.connection,
            "epoch",
            &json!({"historyCoverage":wrong_count}),
            2000,
        )?;
        assert_eq!(read()?, unavailable("snapshot_invalid", Some(2000)));
        let mut wrong_window = base.clone();
        wrong_window["coverage"]["attribution"]["window"]["end"] = json!("2026-11-16");
        persist(
            &storage.connection,
            "epoch",
            &json!({"historyCoverage":wrong_window}),
            3000,
        )?;
        assert_eq!(read()?, unavailable("snapshot_invalid", Some(3000)));
        Ok(())
    }
}
