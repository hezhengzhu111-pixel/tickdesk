use super::*;
use crate::contract::digest;
use std::collections::BTreeMap;

/// The caller holds the completion transaction. No partially valid batch is applied.
pub(super) fn auto_apply(
    d: &Domain<'_, '_>,
    item: &mut Value,
    master: Option<&Value>,
) -> anyhow::Result<Outcome> {
    if d.position_revision()? != int(item, "basePositionRevision") {
        return Ok(failure("IMPORT_DIFF_STALE"));
    }
    let Some(master) = master.and_then(Value::as_array) else {
        return Ok(failure("VISION_SYMBOLS_UNAVAILABLE"));
    };
    if master.is_empty()
        || master.len() > 20_000
        || master
            .iter()
            .any(|entry| crate::contract::validate_definition("SymbolInfo", entry).is_err())
    {
        return Ok(failure("VISION_SYMBOLS_UNAVAILABLE"));
    }
    let Some(rows) = normalized_rows(&item["recognizedRows"], master) else {
        return Ok(failure("IMPORT_CONTENT_INVALID"));
    };
    let input = json!({"cmd":"apply_import","importId":item["importId"],"phase":"preview",
        "snapshotAsOf":item["createdAt"],"rows":rows,"excludedRows":[],"missingActions":[]});
    let domain = Domain { data: &input, ..*d };
    if let Err(error) = preview(&domain, item)? {
        return Ok(Err(error));
    }
    let input = json!({"cmd":"apply_import","importId":item["importId"],"phase":"confirm",
        "importVersion":item["importVersion"],"diffHash":item["diffHash"],"confirmRemovals":false});
    let domain = Domain { data: &input, ..*d };
    // Only the final completion event is published by submit_vision.
    confirm(&domain, item, &mut Vec::new())
}

fn normalized_rows(recognized: &Value, master: &[Value]) -> Option<Vec<Value>> {
    let recognized = recognized.as_array()?;
    if recognized.is_empty() {
        return None;
    }
    let mut symbols = BTreeSet::new();
    let mut rows = Vec::with_capacity(recognized.len());
    for row in recognized {
        let raw = &row["raw"];
        let code = raw["symbol"]
            .as_str()
            .map(str::trim)
            .filter(|value| !value.is_empty());
        let name = raw["name"]
            .as_str()
            .map(str::trim)
            .filter(|value| !value.is_empty());
        if code.is_none() && name.is_none() {
            return None;
        }
        let mut essential = vec![
            "qty",
            "cost",
            if code.is_some() { "symbol" } else { "name" },
        ];
        if !raw["availableQty"].is_null() {
            essential.push("availableQty");
        }
        // RecognitionRow has already constrained confidence to a decimal in [0,1].
        // Compare the first decimal digit exactly; binary rounding must not admit <0.9.
        if essential.iter().any(|field| {
            row["confidence"][*field].as_str().is_none_or(|value| {
                !(value == "1" || value.starts_with("1.") || value.starts_with("0.9"))
            })
        }) || row["issues"]
            .as_array()?
            .iter()
            .any(|issue| essential.contains(&issue["field"].as_str().unwrap_or("")))
        {
            return None;
        }
        let matches: Vec<_> = master
            .iter()
            .filter(|entry| {
                let symbol = entry["symbol"].as_str().unwrap_or("");
                match code {
                    Some(code) => {
                        symbol.eq_ignore_ascii_case(code) || symbol.get(..6) == Some(code)
                    }
                    None => name.is_some_and(|name| entry["name"].as_str() == Some(name)),
                }
            })
            .collect();
        if matches.len() != 1 {
            return None;
        }
        let entry = matches[0];
        if name.is_some_and(|name| entry["name"].as_str() != Some(name)) {
            return None;
        }
        let symbol = entry["symbol"].as_str()?;
        if !symbols.insert(symbol) {
            return None;
        }
        let qty = quantity(&raw["qty"])?;
        if qty == 0 {
            return None;
        }
        let available = if raw["availableQty"].is_null() {
            None
        } else {
            Some(quantity(&raw["availableQty"])?)
        };
        if available.is_some_and(|value| value > qty) {
            return None;
        }
        let cost = number_text(raw["cost"].as_str()?)?;
        let normalized = json!({"rowId":row["rowId"],"symbol":symbol,"name":entry["name"],"qty":qty,
            "availableQty":available,"cost":cost,"pendingLots":null,"resolvedExecutionIds":[]});
        if crate::contract::validate_definition("ImportRow", &normalized).is_err() {
            return None;
        }
        rows.push(normalized);
    }
    Some(rows)
}

fn quantity(raw: &Value) -> Option<u64> {
    let text = number_text(raw.as_str()?)?;
    if text.is_empty() || !text.bytes().all(|c| c.is_ascii_digit()) {
        return None;
    }
    text.parse::<u64>()
        .ok()
        .filter(|value| *value <= 9_007_199_254_740_991)
}

/// A displayed thousands separator is syntax, never a unit multiplier or a guessed value.
fn number_text(raw: &str) -> Option<String> {
    let raw = raw.trim();
    let (whole, fraction) = raw
        .split_once('.')
        .map_or((raw, None), |(w, f)| (w, Some(f)));
    if whole.contains(',') {
        let mut groups = whole.split(',');
        let first = groups.next()?;
        if first.is_empty()
            || first.len() > 3
            || !first.bytes().all(|c| c.is_ascii_digit())
            || groups.any(|g| g.len() != 3 || !g.bytes().all(|c| c.is_ascii_digit()))
        {
            return None;
        }
    }
    let mut text = whole.replace(',', "");
    if let Some(fraction) = fraction {
        text.push('.');
        text.push_str(fraction);
    }
    Some(text)
}

pub(super) fn apply(d: &Domain<'_, '_>, events: &mut Vec<Value>) -> anyhow::Result<Outcome> {
    let key = str_at(d.data, "importId");
    let Some(mut item) = d.get("imports", key)? else {
        return Ok(failure("IMPORT_NOT_FOUND"));
    };
    if item["status"] == "confirmed" && d.data["phase"] == "confirm" {
        let prior:Option<String>=d.tx.query_row("SELECT result FROM import_commits WHERE import_id=?1 AND import_version=?2 AND diff_hash=?3",params![key,int(d.data,"importVersion"),str_at(d.data,"diffHash")],|r|r.get(0)).optional()?;
        return Ok(if let Some(prior) = prior {
            let mut result: Value = serde_json::from_str(&prior)?;
            result["duplicate"] = json!(true);
            Ok(result)
        } else {
            failure("IMPORT_STATE_CONFLICT")
        });
    }
    if item["status"] != "recognized" {
        return Ok(failure("IMPORT_STATE_CONFLICT"));
    }
    match str_at(d.data, "phase") {
        "preview" => preview(d, &mut item),
        "confirm" => confirm(d, &mut item, events),
        "reject" => {
            if d.data["importVersion"] != item["importVersion"] {
                return Ok(failure("IMPORT_VERSION_CONFLICT"));
            }
            // Recognition already released its image lease; rejecting is a durable terminal decision.
            item["status"] = json!("rejected");
            item["readyForConfirm"] = json!(false);
            item["importVersion"] = json!(int(&item, "importVersion") + 1);
            item["updatedAt"] = json!(d.now);
            save(d.tx, &item)?;
            events.push(event(d.epoch, d.seq()?, d.now, item.clone()));
            Ok(Ok(
                json!({"importId":key,"importStatus":"rejected","snapshotSeq":d.seq()?}),
            ))
        }
        _ => Ok(failure("INVALID_PARAMS")),
    }
}

fn values(row: &Value, as_of: u64) -> Value {
    json!({"name":row["name"],"qty":row["qty"],"availableQty":row["availableQty"],"cost":row["cost"],"pendingLots":row["pendingLots"],"asOf":as_of})
}

fn validate_row(
    d: &Domain<'_, '_>,
    row: &Value,
    as_of: u64,
    executions: &mut BTreeSet<String>,
) -> anyhow::Result<Outcome> {
    let qty = int(row, "qty");
    let available = row["availableQty"].as_u64();
    if available.is_some_and(|a| a > qty) {
        return Ok(failure("INVALID_SELLABLE_QTY"));
    }
    if let Some(lots) = row["pendingLots"].as_array() {
        let sum = lots
            .iter()
            .try_fold(0_u64, |a, lot| a.checked_add(int(lot, "qty")));
        let date = chrono::DateTime::from_timestamp_millis(as_of as i64)
            .map(|t| t.with_timezone(&chrono_tz::Asia::Shanghai).date_naive());
        if available.is_none_or(|a| sum != Some(qty - a))
            || lots.iter().any(|lot| {
                let parsed =
                    chrono::NaiveDate::parse_from_str(str_at(lot, "sellableDate"), "%Y-%m-%d").ok();
                int(lot, "qty") == 0 || parsed.is_none() || parsed <= date
            })
        {
            return Ok(failure("INVALID_SELLABLE_QTY"));
        }
    }
    for execution_id in row["resolvedExecutionIds"].as_array().unwrap() {
        let key = execution_id.as_str().unwrap();
        let Some(execution) = d.get("executions", key)? else {
            return Ok(failure("INVALID_PARAMS"));
        };
        if !executions.insert(key.to_owned())
            || execution["symbol"] != row["symbol"]
            || execution["positionEffect"] != "needs_reconciliation"
            || int(&execution, "executedAt") > as_of
        {
            return Ok(failure("INVALID_PARAMS"));
        }
    }
    Ok(Ok(Value::Null))
}

fn preview(d: &Domain<'_, '_>, item: &mut Value) -> anyhow::Result<Outcome> {
    let as_of = int(d.data, "snapshotAsOf");
    if as_of > d.now {
        return Ok(failure("IMPORT_ASOF_STALE"));
    }
    let rows = d.data["rows"].as_array().unwrap();
    let excluded = d.data.get("excludedRows").cloned().unwrap_or(json!([]));
    let raw_ids: BTreeSet<_> = item["recognizedRows"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| str_at(r, "rowId"))
        .collect();
    let mut resolved = BTreeSet::new();
    let mut symbols = BTreeSet::new();
    let mut execution_ids = BTreeSet::new();
    for row in rows {
        if !resolved.insert(str_at(row, "rowId")) || !symbols.insert(str_at(row, "symbol")) {
            return Ok(failure("INVALID_PARAMS"));
        }
        if let Err(failure) = validate_row(d, row, as_of, &mut execution_ids)? {
            return Ok(Err(failure));
        }
    }
    for row in excluded.as_array().unwrap() {
        if !raw_ids.contains(str_at(row, "rowId"))
            || !resolved.insert(str_at(row, "rowId"))
            || str_at(row, "reason").trim().is_empty()
        {
            return Ok(failure("IMPORT_ROWS_UNRESOLVED"));
        }
    }
    if !raw_ids.is_subset(&resolved) {
        return Ok(failure("IMPORT_ROWS_UNRESOLVED"));
    }
    let positions = d.list("positions")?;
    if rows.is_empty() && positions.iter().any(|p| int(p, "qty") > 0) {
        return Ok(failure("IMPORT_EMPTY_SNAPSHOT"));
    }
    if positions.iter().any(|p| int(p, "asOf") > as_of) {
        return Ok(failure("IMPORT_ASOF_STALE"));
    }
    let by_symbol: BTreeMap<_, _> = positions.iter().map(|p| (str_at(p, "symbol"), p)).collect();
    let mut actions = BTreeMap::new();
    if let Some(missing) = d.data["missingActions"].as_array() {
        for action in missing {
            let symbol = str_at(action, "symbol");
            if !by_symbol.contains_key(symbol)
                || symbols.contains(symbol)
                || actions.insert(symbol, str_at(action, "action")).is_some()
            {
                return Ok(failure("INVALID_PARAMS"));
            }
        }
    }
    let mut added = Vec::new();
    let mut changed = Vec::new();
    let mut unchanged = Vec::new();
    let mut missing = Vec::new();
    for row in rows {
        let symbol = str_at(row, "symbol");
        if let Some(before) = by_symbol.get(symbol) {
            let before = values(before, int(before, "asOf"));
            let after = values(row, as_of);
            let mut old = before.clone();
            old["asOf"] = json!(as_of);
            if old == after && row["resolvedExecutionIds"].as_array().unwrap().is_empty() {
                unchanged.push(json!(symbol))
            } else {
                changed.push(json!({"symbol":symbol,"before":before,"after":after}))
            }
        } else {
            added.push(json!(symbol))
        }
    }
    for (symbol, before) in by_symbol {
        if !symbols.contains(symbol) {
            missing.push(json!({"symbol":symbol,"before":values(before,int(before,"asOf")),"action":actions.get(symbol).copied().unwrap_or("keep")}))
        }
    }
    added.sort_by_key(|v| v.as_str().unwrap().to_owned());
    unchanged.sort_by_key(|v| v.as_str().unwrap().to_owned());
    changed.sort_by_key(|v| str_at(v, "symbol").to_owned());
    item["rows"] = json!(rows);
    item["excludedRows"] = excluded;
    item["snapshotAsOf"] = json!(as_of);
    item["basePositionRevision"] = json!(d.position_revision()?);
    item["importVersion"] = json!(int(item, "importVersion") + 1);
    item["diff"] = json!({"added":added,"changed":changed,"missing":missing,"unchanged":unchanged});
    let missing_actions: Vec<_> = missing
        .iter()
        .map(|r| json!({"symbol":r["symbol"],"action":r["action"]}))
        .collect();
    item["diffHash"] = json!(digest(
        &json!({"importId":item["importId"],"importVersion":item["importVersion"],"basePositionRevision":item["basePositionRevision"],"snapshotAsOf":as_of,"rows":rows,"excludedRows":item["excludedRows"],"missingActions":missing_actions,"diff":item["diff"]})
    )?);
    item["readyForConfirm"] = json!(true);
    item["updatedAt"] = json!(d.now);
    save(d.tx, item)?;
    Ok(Ok(item.clone()))
}

fn confirm(
    d: &Domain<'_, '_>,
    item: &mut Value,
    events: &mut Vec<Value>,
) -> anyhow::Result<Outcome> {
    if d.data["importVersion"] != item["importVersion"] {
        return Ok(failure("IMPORT_VERSION_CONFLICT"));
    }
    if item["readyForConfirm"] != true {
        return Ok(failure("IMPORT_PREVIEW_NOT_READY"));
    }
    if d.data["diffHash"] != item["diffHash"] {
        return Ok(failure("IMPORT_DIFF_MISMATCH"));
    }
    if d.position_revision()? != int(item, "basePositionRevision") {
        return Ok(failure("IMPORT_DIFF_STALE"));
    }
    let removals: Vec<_> = item["diff"]["missing"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|row| row["action"] == "delete")
        .cloned()
        .collect();
    if !removals.is_empty() && d.data["confirmRemovals"] != true {
        return Ok(failure("IMPORT_REMOVALS_UNCONFIRMED"));
    }
    let as_of = int(item, "snapshotAsOf");
    let mut execution_ids = BTreeSet::new();
    for row in item["rows"].as_array().unwrap() {
        if let Err(failure) = validate_row(d, row, as_of, &mut execution_ids)? {
            return Ok(Err(failure));
        }
    }
    for key in execution_ids {
        let mut execution = d.get("executions", &key)?.unwrap();
        execution["positionEffect"] = json!("reconciled");
        execution["reconciliationRequired"] = json!(false);
        d.put("executions", &key, &execution)?;
    }
    let revision = d.bump_position()?;
    for row in item["rows"].as_array().unwrap() {
        write_position(d, item, row, revision, false)?;
    }
    for removal in &removals {
        // Keep the deletion audit as a zero after-value, while removing the current projection.
        let mut row = removal["before"].clone();
        row["symbol"] = removal["symbol"].clone();
        row["qty"] = json!(0);
        row["availableQty"] = json!(0);
        row["pendingLots"] = json!([]);
        row["resolvedExecutionIds"] = json!([]);
        write_position(d, item, &row, revision, true)?;
    }
    let positions = d.list("positions")?;
    let positive: Vec<_> = positions.iter().filter(|p| int(p, "qty") > 0).collect();
    crate::valuation::refresh(d.tx, d.now)?;
    let valuation = crate::valuation::summary(d.tx, d.now)?;
    let preview_version = int(item, "importVersion");
    item["status"] = json!("confirmed");
    item["readyForConfirm"] = json!(false);
    item["updatedAt"] = json!(d.now);
    item["importVersion"] = json!(preview_version + 1);
    save(d.tx, item)?;
    let result = json!({"importId":item["importId"],"importStatus":"confirmed","duplicate":false,"positionCount":positive.len(),
        "addedCount":item["diff"]["added"].as_array().unwrap().len(),"changedCount":item["diff"]["changed"].as_array().unwrap().len(),
        "keptCount":item["diff"]["missing"].as_array().unwrap().len()-removals.len(),"deletedCount":removals.len(),"positionRevision":revision,"valuation":valuation,"snapshotSeq":d.seq()?});
    d.tx.execute(
        "INSERT INTO import_commits VALUES (?1,?2,?3,?4)",
        params![
            str_at(item, "importId"),
            preview_version,
            str_at(item, "diffHash"),
            serde_json::to_string(&result)?
        ],
    )?;
    events.push(event(d.epoch, d.seq()?, d.now, item.clone()));
    Ok(Ok(result))
}

fn write_position(
    d: &Domain<'_, '_>,
    item: &Value,
    row: &Value,
    revision: u64,
    remove: bool,
) -> anyhow::Result<()> {
    let symbol = str_at(row, "symbol");
    let before = d.get("positions", symbol)?;
    let unresolved:bool=d.tx.query_row("SELECT EXISTS(SELECT 1 FROM objects WHERE collection='executions' AND json_extract(body,'$.symbol')=?1 AND json_extract(body,'$.reconciliationRequired')=1)",[symbol],|r|r.get(0))?;
    let unresolved = unresolved || row["pendingLots"].is_null() || row["availableQty"].is_null();
    let mut after = values(row, int(item, "snapshotAsOf"));
    for (key,value) in json!({"symbol":symbol,"positionRevision":revision,"reconciliationRequired":unresolved,"lastPrice":null,"priceAsOf":null,"priceStatus":"missing","marketValue":null,"unrealizedPnl":null,"unrealizedPnlRatio":null}).as_object().unwrap() {after[key]=value.clone();}
    let key = id();
    let automatic = d
        .get("vision_options", str_at(item, "importId"))?
        .is_some_and(|options| options["autoApply"] == true);
    let reason = if automatic {
        format!(
            "import:{}; 当前申报，时点为本次选图导入登记时间，非截图历史时点",
            str_at(item, "importId")
        )
    } else {
        format!("import:{}", str_at(item, "importId"))
    };
    let adjustment = json!({"adjustmentId":key,"symbol":symbol,"asOf":item["snapshotAsOf"],"before":before,"after":after,"reason":reason,"resolvedExecutionIds":row["resolvedExecutionIds"],"createdAt":d.now});
    d.put("adjustments", &key, &adjustment)?;
    if remove {
        d.tx.execute(
            "DELETE FROM objects WHERE collection='positions' AND object_id=?1",
            [symbol],
        )?;
    } else {
        d.put("positions", symbol, &after)?;
    }
    Ok(())
}
