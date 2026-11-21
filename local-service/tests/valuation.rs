use rusqlite::{params, Connection};
use serde_json::{json, Value};
use tickdesk_local::{storage::Storage, valuation};

const NOW: u64 = 1_800_000_000_000;

fn db() -> (tempfile::TempDir, Storage, Connection) {
    let dir = tempfile::tempdir().unwrap();
    let store = Storage::open(dir.path()).unwrap();
    let connection = Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();
    (dir, store, connection)
}
fn put(db: &Connection, collection: &str, key: &str, value: Value) {
    db.execute("INSERT INTO objects VALUES (?1,?2,?3) ON CONFLICT(collection,object_id) DO UPDATE SET body=excluded.body",params![collection,key,value.to_string()]).unwrap();
}
fn pos(symbol: &str, cost: &str, qty: u64) -> Value {
    json!({"symbol":symbol,"name":symbol,"qty":qty,"availableQty":qty,"cost":cost,"pendingLots":[],"asOf":NOW-1000,"positionRevision":1,"reconciliationRequired":false,"lastPrice":null,"priceAsOf":null,"priceStatus":"missing","marketValue":null,"unrealizedPnl":null,"unrealizedPnlRatio":null})
}
fn quote(symbol: &str, price: &str, at: u64) -> Value {
    json!({"symbol":symbol,"price":price,"asOf":at,"source":{"name":"local-fixture","ref":"fixture","asOf":at},"status":"fresh"})
}
fn refresh(db: &mut Connection, now: u64) -> Value {
    let tx = db.transaction().unwrap();
    valuation::refresh(&tx, now).unwrap();
    tx.commit().unwrap();
    valuation::summary(db, now).unwrap()
}
fn object(db: &Connection, collection: &str, key: &str) -> Value {
    let raw: String = db
        .query_row(
            "SELECT body FROM objects WHERE collection=?1 AND object_id=?2",
            [collection, key],
            |r| r.get(0),
        )
        .unwrap();
    serde_json::from_str(&raw).unwrap()
}

#[test]
fn exact_decimal_values_do_not_round_money_or_claim_cash_and_fees() {
    let (_dir, _store, mut db) = db();
    put(&db, "positions", "600000.SH", pos("600000.SH", "0.1", 3));
    put(
        &db,
        "market_quotes",
        "600000.SH",
        quote("600000.SH", "0.2", NOW),
    );
    let summary = refresh(&mut db, NOW);
    assert_eq!(summary["positionMarketValue"], "0.6");
    assert_eq!(summary["unrealizedPnl"], "0.3");
    assert_eq!(summary["includesCash"], false);
    assert_eq!(summary["includesFees"], false);
    let position = object(&db, "positions", "600000.SH");
    assert_eq!(position["unrealizedPnlRatio"], "1");
    assert_eq!(position["positionRevision"], 1);
}

#[test]
fn missing_or_stale_quotes_leave_total_unknown_but_keep_priced_subtotal() {
    let (_dir, _store, mut db) = db();
    put(&db, "positions", "600000.SH", pos("600000.SH", "10", 2));
    put(&db, "positions", "000001.SZ", pos("000001.SZ", "20", 3));
    put(
        &db,
        "market_quotes",
        "600000.SH",
        quote("600000.SH", "12", NOW),
    );
    let summary = refresh(&mut db, NOW);
    assert_eq!(summary["positionMarketValue"], Value::Null);
    assert_eq!(summary["pricedSubtotal"], "24");
    assert_eq!(summary["coveredCount"], 1);
    assert_eq!(summary["unpricedSymbols"], json!(["000001.SZ"]));
    let summary = refresh(&mut db, NOW + 90_001);
    assert_eq!(summary["pricedSubtotal"], "0");
    let position = object(&db, "positions", "600000.SH");
    assert_eq!(position["priceStatus"], "stale");
    assert_eq!(position["lastPrice"], "12");
    assert_eq!(position["marketValue"], Value::Null);
}

#[test]
fn zero_cost_has_no_invented_ratio_and_reconciliation_prevents_known_total() {
    let (_dir, _store, mut db) = db();
    let mut position = pos("600000.SH", "0", 2);
    position["reconciliationRequired"] = json!(true);
    put(&db, "positions", "600000.SH", position);
    put(
        &db,
        "market_quotes",
        "600000.SH",
        quote("600000.SH", "12", NOW),
    );
    let summary = refresh(&mut db, NOW);
    assert_eq!(summary["positionMarketValue"], Value::Null);
    assert_eq!(summary["reconciliationSymbols"], json!(["600000.SH"]));
    assert_eq!(
        object(&db, "positions", "600000.SH")["unrealizedPnlRatio"],
        Value::Null
    );
}

#[test]
fn equal_inputs_are_idempotent_and_corrections_append_a_linked_point() {
    let (_dir, _store, mut db) = db();
    put(&db, "positions", "600000.SH", pos("600000.SH", "10", 2));
    put(
        &db,
        "market_quotes",
        "600000.SH",
        quote("600000.SH", "12", NOW),
    );
    refresh(&mut db, NOW);
    refresh(&mut db, NOW + 1);
    let count: u64 = db
        .query_row(
            "SELECT count(*) FROM objects WHERE collection='valuationPoints'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(count, 1);
    let first: String = db
        .query_row(
            "SELECT object_id FROM objects WHERE collection='valuationPoints'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    put(
        &db,
        "market_quotes",
        "600000.SH",
        quote("600000.SH", "13", NOW),
    );
    refresh(&mut db, NOW + 2);
    let raw: String = db
        .query_row(
            "SELECT body FROM objects WHERE collection='valuationPoints' AND object_id<>?1",
            [&first],
            |r| r.get(0),
        )
        .unwrap();
    let point: Value = serde_json::from_str(&raw).unwrap();
    assert_eq!(point["supersedesPointId"], first);
    assert_eq!(point["revision"], 2);
    assert_eq!(point["marketValue"], "26");
    assert_eq!(object(&db, "valuationPoints", &first)["marketValue"], "24");
}

#[test]
fn quantity_change_points_retain_frozen_evidence_and_new_change_ids() {
    let (_dir, _store, mut db) = db();
    let before = pos("600000.SH", "10", 2);
    put(&db, "positions", "600000.SH", before.clone());
    put(
        &db,
        "market_quotes",
        "600000.SH",
        quote("600000.SH", "12", NOW),
    );
    refresh(&mut db, NOW);
    let first: String = db
        .query_row(
            "SELECT object_id FROM objects WHERE collection='valuationPoints'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    let mut after = pos("600000.SH", "10", 5);
    after["asOf"] = json!(NOW + 500);
    after["positionRevision"] = json!(2);
    put(&db, "positions", "600000.SH", after.clone());
    put(
        &db,
        "adjustments",
        "adjustment1",
        json!({"adjustmentId":"adjustment1","symbol":"600000.SH","asOf":NOW+500,"before":before,"after":after,"createdAt":NOW+500,"reason":"reported quantity","resolvedExecutionIds":[]}),
    );
    put(
        &db,
        "market_quotes",
        "600000.SH",
        quote("600000.SH", "12", NOW + 1000),
    );
    refresh(&mut db, NOW + 1000);
    let key: String = db
        .query_row(
            "SELECT object_id FROM objects WHERE collection='valuationPoints' AND object_id<>?1",
            [&first],
            |r| r.get(0),
        )
        .unwrap();
    let point = object(&db, "valuationPoints", &key);
    assert_eq!(point["quantityChangeIds"], json!(["adjustment1"]));
    assert_eq!(point["marketValue"], "60");
    assert_eq!(point["supersedesPointId"], Value::Null);
    assert_eq!(
        object(&db, "valuationEvidence", &first)["positions"][0]["qty"],
        2
    );
    assert_eq!(
        object(&db, "valuationEvidence", &key)["quotes"][0]["source"]["name"],
        "local-fixture"
    );
    refresh(&mut db, NOW + 1001);
    let count: u64 = db
        .query_row(
            "SELECT count(*) FROM objects WHERE collection='valuationPoints'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(count, 2);
}

#[test]
fn future_or_unattributed_quotes_do_not_produce_known_values() {
    let (_dir, _store, mut db) = db();
    put(&db, "positions", "600000.SH", pos("600000.SH", "3", 2));
    put(
        &db,
        "market_quotes",
        "600000.SH",
        quote("600000.SH", "4", NOW + 1),
    );
    assert_eq!(refresh(&mut db, NOW)["coveredCount"], 0);
    let mut unattributed = quote("600000.SH", "4", NOW);
    unattributed["source"] = Value::Null;
    put(&db, "market_quotes", "600000.SH", unattributed);
    assert_eq!(refresh(&mut db, NOW)["coveredCount"], 0);
    put(
        &db,
        "market_quotes",
        "600000.SH",
        quote("600000.SH", "4", NOW),
    );
    refresh(&mut db, NOW);
    assert_eq!(
        object(&db, "positions", "600000.SH")["unrealizedPnlRatio"],
        Value::Null
    );
    assert_eq!(object(&db, "positions", "600000.SH")["marketValue"], "8");
}

#[test]
fn empty_ledger_is_zero_without_inventing_a_price_history() {
    let (_dir, _store, mut db) = db();
    assert_eq!(refresh(&mut db, NOW)["positionMarketValue"], "0");
    let count: u64 = db
        .query_row(
            "SELECT count(*) FROM objects WHERE collection='valuationPoints'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(count, 0);
}
