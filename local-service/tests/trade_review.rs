mod support;

use serde_json::{json, Value};
use support::{command, deliver, LOCAL_PAIR, PEER};
use tickdesk_local::storage::Storage;

fn run(db: &mut Storage, key: &str, cmd: &str, fields: Value, now: u64) -> Value {
    let input = command(db, key, key, cmd, fields);
    db.receive(&deliver(db, PEER, key, &input)).unwrap();
    db.process(db.job(key).unwrap().unwrap(), now)
        .unwrap_or_else(|error| panic!("{cmd}: {error}"))
        .unwrap()
}

fn record(db: &mut Storage, key: &str, fields: Value, now: u64) -> Value {
    run(db, key, "record_execution", fields, now)
}

const NOW: u64 = 1_794_794_400_000;
const DAY: u64 = 86_400_000;

fn read_review(db: &mut Storage, key: &str, fields: Value) -> Value {
    run(db, key, "get_trade_review", fields, NOW)
}

#[test]
fn fifo_review_separates_price_difference_from_known_fee_result() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    record(
        &mut db,
        "buy",
        json!({"symbol":"300502.SZ","side":"buy","price":"10","qty":100,
            "fees":"2","executedAt":NOW - 2 * DAY}),
        NOW - DAY,
    );
    record(
        &mut db,
        "sell",
        json!({"symbol":"300502.SZ","side":"sell","price":"11","qty":100,
            "fees":"3","executedAt":NOW}),
        NOW,
    );
    let result = read_review(&mut db, "review", json!({}));
    assert_eq!(result["data"]["ok"], true, "{result}");
    let review = &result["data"]["result"];
    assert_eq!(review["version"], 1);
    assert_eq!(review["basis"]["pairing"], "fifo");
    assert_eq!(review["basis"]["priceBasis"], "recorded_execution");
    assert_eq!(review["summary"]["closedTrades"], 1);
    assert_eq!(review["summary"]["wins"], 1);
    assert_eq!(review["summary"]["winRateBasisPoints"], 10_000);
    assert_eq!(review["summary"]["grossPnl"], "100");
    assert_eq!(review["summary"]["netPnl"], "95");
    assert_eq!(review["summary"]["netPnlComplete"], true);
    assert_eq!(review["summary"]["missingFeeCount"], 0);
    let item = &review["items"][0];
    assert_eq!(item["symbol"], "300502.SZ");
    assert_eq!(item["qty"], 100);
    assert_eq!(item["grossPnl"], "100");
    assert_eq!(item["buyFee"], "2");
    assert_eq!(item["sellFee"], "3");
    assert_eq!(item["netPnl"], "95");
    assert_eq!(item["feeSource"], "user");
    assert_eq!(item["holdingDays"], 2);
    drop(db);

    let mut db = Storage::open(dir.path()).unwrap();
    let again = read_review(&mut db, "review-again", json!({}));
    assert_eq!(again["data"]["result"]["summary"]["netPnl"], "95");
}

#[test]
fn missing_fees_never_report_a_net_amount() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    record(
        &mut db,
        "buy",
        json!({"symbol":"300502.SZ","side":"buy","price":"10","qty":100,
            "executedAt":NOW - 2 * DAY}),
        NOW - DAY,
    );
    record(
        &mut db,
        "sell",
        json!({"symbol":"300502.SZ","side":"sell","price":"11","qty":100,
            "executedAt":NOW}),
        NOW,
    );
    let result = read_review(&mut db, "review", json!({}));
    let review = &result["data"]["result"];
    assert_eq!(review["summary"]["grossPnl"], "100");
    assert!(review["summary"]["netPnl"].is_null());
    assert_eq!(review["summary"]["netPnlComplete"], false);
    assert_eq!(review["summary"]["missingFeeCount"], 1);
    assert!(review["items"][0]["netPnl"].is_null());
    assert_eq!(review["items"][0]["feeSource"], "unknown");
}

#[test]
fn partial_close_keeps_the_open_lot_and_allocates_the_buy_fee() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    record(
        &mut db,
        "buy",
        json!({"symbol":"300502.SZ","side":"buy","price":"10","qty":100,
            "fees":"2","executedAt":NOW - 2 * DAY}),
        NOW - DAY,
    );
    record(
        &mut db,
        "sell",
        json!({"symbol":"300502.SZ","side":"sell","price":"11","qty":40,
            "fees":"3","executedAt":NOW}),
        NOW,
    );
    let result = read_review(&mut db, "review", json!({}));
    let review = &result["data"]["result"];
    assert_eq!(review["summary"]["closedTrades"], 1);
    assert_eq!(review["summary"]["openLots"], 1);
    assert_eq!(review["items"][0]["qty"], 40);
    assert_eq!(review["items"][0]["grossPnl"], "40");
    assert_eq!(review["items"][0]["buyFee"], "0.8");
    assert_eq!(review["items"][0]["sellFee"], "3");
    assert_eq!(review["items"][0]["netPnl"], "36.2");
    assert_eq!(review["summary"]["netPnl"], "36.2");
}

#[test]
fn empty_review_reports_no_rate_and_no_amount() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let result = read_review(&mut db, "review", json!({}));
    let review = &result["data"]["result"];
    assert_eq!(review["summary"]["closedTrades"], 0);
    assert!(review["summary"]["winRateBasisPoints"].is_null());
    assert!(review["summary"]["grossPnl"].is_null());
    assert!(review["summary"]["netPnl"].is_null());
    assert_eq!(review["summary"]["openLots"], 0);
    assert_eq!(review["items"], json!([]));
}

#[test]
fn superseded_execution_is_excluded_and_the_correction_counts_once() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    record(
        &mut db,
        "buy",
        json!({"symbol":"300502.SZ","side":"buy","price":"10","qty":100,
            "fees":"2","executedAt":NOW - 2 * DAY}),
        NOW - DAY,
    );
    let mistaken = record(
        &mut db,
        "sell-mistake",
        json!({"symbol":"300502.SZ","side":"sell","price":"11","qty":100,
            "fees":"3","executedAt":NOW}),
        NOW,
    );
    let old = mistaken["data"]["result"]["execution"]["executionId"]
        .as_str()
        .unwrap()
        .to_owned();
    record(
        &mut db,
        "sell-corrected",
        json!({"symbol":"300502.SZ","side":"sell","price":"10.5","qty":100,
            "fees":"3","executedAt":NOW,"supersedesExecutionId":old}),
        NOW,
    );
    let result = read_review(&mut db, "review", json!({}));
    let review = &result["data"]["result"];
    assert_eq!(review["summary"]["closedTrades"], 1);
    assert_eq!(review["items"][0]["sellPrice"], "10.5");
    assert_eq!(review["items"][0]["grossPnl"], "50");
    assert_eq!(review["summary"]["grossPnl"], "50");
    assert_eq!(review["summary"]["excludedCount"], 1);
    let excluded = read_review(&mut db, "review-exclusions", json!({"view":"exclusions"}));
    let excluded = &excluded["data"]["result"]["exclusions"];
    assert_eq!(excluded[0]["executionId"], old);
    assert_eq!(excluded[0]["reason"], "superseded");
}

#[test]
fn negative_gross_and_net_amounts_survive_the_summary() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    record(
        &mut db,
        "buy",
        json!({"symbol":"300502.SZ","side":"buy","price":"10","qty":100,
            "fees":"2","executedAt":NOW - 2 * DAY}),
        NOW - DAY,
    );
    record(
        &mut db,
        "sell",
        json!({"symbol":"300502.SZ","side":"sell","price":"9","qty":100,
            "fees":"3","executedAt":NOW}),
        NOW,
    );
    let result = read_review(&mut db, "review", json!({}));
    let review = &result["data"]["result"];
    assert_eq!(review["items"][0]["grossPnl"], "-100");
    assert_eq!(review["items"][0]["netPnl"], "-105");
    assert_eq!(review["summary"]["grossPnl"], "-100");
    assert_eq!(review["summary"]["netPnl"], "-105");
    assert_eq!(review["summary"]["netPnlComplete"], true);
    assert_eq!(review["summary"]["wins"], 0);
    assert_eq!(review["summary"]["losses"], 1);
    assert_eq!(review["summary"]["flat"], 0);
    assert_eq!(review["summary"]["winRateBasisPoints"], 0);
}

#[test]
fn buy_fee_is_allocated_once_across_two_sells() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    record(
        &mut db,
        "buy",
        json!({"symbol":"300502.SZ","side":"buy","price":"10","qty":100,
            "fees":"2","executedAt":NOW - 3 * DAY}),
        NOW - 2 * DAY,
    );
    record(
        &mut db,
        "sell-a",
        json!({"symbol":"300502.SZ","side":"sell","price":"11","qty":40,
            "fees":"3","executedAt":NOW - DAY}),
        NOW - DAY,
    );
    record(
        &mut db,
        "sell-b",
        json!({"symbol":"300502.SZ","side":"sell","price":"11","qty":60,
            "fees":"3","executedAt":NOW}),
        NOW,
    );
    let result = read_review(&mut db, "review", json!({}));
    let review = &result["data"]["result"];
    assert_eq!(review["summary"]["closedTrades"], 2);
    assert_eq!(review["items"][0]["buyFee"], "0.8");
    assert_eq!(review["items"][0]["sellFee"], "3");
    assert_eq!(review["items"][0]["netPnl"], "36.2");
    assert_eq!(review["items"][1]["buyFee"], "1.2");
    assert_eq!(review["items"][1]["sellFee"], "3");
    assert_eq!(review["items"][1]["netPnl"], "55.8");
    assert_eq!(review["summary"]["grossPnl"], "100");
    assert_eq!(review["summary"]["netPnl"], "92");
}

#[test]
fn period_filter_keeps_pre_period_buys_for_fifo() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    record(
        &mut db,
        "buy",
        json!({"symbol":"300502.SZ","side":"buy","price":"10","qty":100,
            "fees":"2","executedAt":NOW - 5 * DAY}),
        NOW - 4 * DAY,
    );
    record(
        &mut db,
        "sell-before",
        json!({"symbol":"300502.SZ","side":"sell","price":"11","qty":40,
            "fees":"3","executedAt":NOW - 2 * DAY}),
        NOW - 2 * DAY,
    );
    record(
        &mut db,
        "sell-inside",
        json!({"symbol":"300502.SZ","side":"sell","price":"11","qty":60,
            "fees":"3","executedAt":NOW}),
        NOW,
    );
    let result = read_review(&mut db, "review", json!({"from":NOW - DAY, "to":NOW + 1}));
    let review = &result["data"]["result"];
    assert_eq!(review["summary"]["closedTrades"], 1);
    assert_eq!(review["items"].as_array().unwrap().len(), 1);
    assert_eq!(review["items"][0]["qty"], 60);
    assert_eq!(review["items"][0]["grossPnl"], "60");
    assert_eq!(review["items"][0]["buyFee"], "1.2");
    assert_eq!(review["summary"]["grossPnl"], "60");
    assert_eq!(review["summary"]["openLots"], 0);
}

#[test]
fn gross_positive_but_known_fees_turn_the_sample_into_a_loss() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    record(
        &mut db,
        "buy",
        json!({"symbol":"300502.SZ","side":"buy","price":"10","qty":100,
            "fees":"50","executedAt":NOW - 2 * DAY}),
        NOW - DAY,
    );
    record(
        &mut db,
        "sell",
        json!({"symbol":"300502.SZ","side":"sell","price":"10.2","qty":100,
            "fees":"60","executedAt":NOW}),
        NOW,
    );
    let result = read_review(&mut db, "review", json!({}));
    let review = &result["data"]["result"];
    assert_eq!(review["items"][0]["grossPnl"], "20");
    assert_eq!(review["items"][0]["netPnl"], "-90");
    assert_eq!(review["summary"]["grossPnl"], "20");
    assert_eq!(review["summary"]["netPnl"], "-90");
    assert_eq!(review["summary"]["wins"], 0);
    assert_eq!(review["summary"]["losses"], 1);
    assert_eq!(review["summary"]["priceWins"], 1);
    assert_eq!(review["summary"]["priceLosses"], 0);
}

#[test]
fn declared_zero_fee_is_known_not_missing() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    record(
        &mut db,
        "buy",
        json!({"symbol":"300502.SZ","side":"buy","price":"10","qty":100,
            "fees":"0","executedAt":NOW - 2 * DAY}),
        NOW - DAY,
    );
    record(
        &mut db,
        "sell",
        json!({"symbol":"300502.SZ","side":"sell","price":"11","qty":100,
            "fees":"0","executedAt":NOW}),
        NOW,
    );
    let result = read_review(&mut db, "review", json!({}));
    let review = &result["data"]["result"];
    assert_eq!(review["items"][0]["buyFee"], "0");
    assert_eq!(review["items"][0]["sellFee"], "0");
    assert_eq!(review["items"][0]["netPnl"], "100");
    assert_eq!(review["items"][0]["feeSource"], "user");
    assert_eq!(review["summary"]["netPnl"], "100");
    assert_eq!(review["summary"]["missingFeeCount"], 0);
    assert_eq!(review["summary"]["netPnlComplete"], true);
}

#[test]
fn one_sell_across_two_lots_splits_the_sell_fee_without_gain() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    record(
        &mut db,
        "buy-a",
        json!({"symbol":"300502.SZ","side":"buy","price":"10","qty":40,
            "fees":"1","executedAt":NOW - 3 * DAY}),
        NOW - 2 * DAY,
    );
    record(
        &mut db,
        "buy-b",
        json!({"symbol":"300502.SZ","side":"buy","price":"10","qty":60,
            "fees":"2","executedAt":NOW - 2 * DAY}),
        NOW - DAY,
    );
    record(
        &mut db,
        "sell",
        json!({"symbol":"300502.SZ","side":"sell","price":"11","qty":100,
            "fees":"1","executedAt":NOW}),
        NOW,
    );
    let result = read_review(&mut db, "review", json!({}));
    let review = &result["data"]["result"];
    assert_eq!(review["items"].as_array().unwrap().len(), 2);
    assert_eq!(review["items"][0]["buyFee"], "1");
    assert_eq!(review["items"][0]["sellFee"], "0.4");
    assert_eq!(review["items"][1]["buyFee"], "2");
    assert_eq!(review["items"][1]["sellFee"], "0.6");
    assert_eq!(review["summary"]["grossPnl"], "100");
    assert_eq!(review["summary"]["netPnl"], "96");
    assert_eq!(review["summary"]["closedSamples"], 2);
    assert_eq!(review["summary"]["sampleNetPnl"], "96");
    let samples = read_review(&mut db, "review-samples", json!({"view":"samples"}));
    let samples = &samples["data"]["result"]["samples"];
    assert_eq!(samples[0]["netPnl"], "38.6");
    assert_eq!(samples[1]["netPnl"], "57.4");
}

#[test]
fn indivisible_fee_tail_goes_to_the_last_slice() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    record(
        &mut db,
        "buy",
        json!({"symbol":"300502.SZ","side":"buy","price":"10","qty":100,
            "fees":"1","executedAt":NOW - 4 * DAY}),
        NOW - 3 * DAY,
    );
    for (key, qty, day) in [("sell-a", 33, 3), ("sell-b", 33, 2), ("sell-c", 34, 1)] {
        record(
            &mut db,
            key,
            json!({"symbol":"300502.SZ","side":"sell","price":"11","qty":qty,
                "fees":"0.1","executedAt":NOW - day * DAY}),
            NOW - day * DAY,
        );
    }
    let result = read_review(&mut db, "review", json!({}));
    let review = &result["data"]["result"];
    let fees = review["items"]
        .as_array()
        .unwrap()
        .iter()
        .map(|item| item["buyFee"].as_str().unwrap().to_owned())
        .collect::<Vec<_>>();
    assert_eq!(fees, vec!["0.33", "0.33", "0.34"]);
    let buy_fee_total: f64 = fees.iter().map(|fee| fee.parse::<f64>().unwrap()).sum();
    assert!((buy_fee_total - 1.0).abs() < 1e-9, "{buy_fee_total}");
    assert_eq!(review["summary"]["grossPnl"], "100");
    assert_eq!(review["summary"]["netPnl"], "98.7");
}

#[test]
fn two_sells_of_one_entry_are_one_sample_but_two_fragments() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    record(
        &mut db,
        "buy",
        json!({"symbol":"300502.SZ","side":"buy","price":"10","qty":100,
            "fees":"2","executedAt":NOW - 3 * DAY}),
        NOW - 2 * DAY,
    );
    record(
        &mut db,
        "sell-a",
        json!({"symbol":"300502.SZ","side":"sell","price":"11","qty":40,
            "fees":"3","executedAt":NOW - DAY}),
        NOW - DAY,
    );
    record(
        &mut db,
        "sell-b",
        json!({"symbol":"300502.SZ","side":"sell","price":"11","qty":60,
            "fees":"3","executedAt":NOW}),
        NOW,
    );
    let result = read_review(&mut db, "review", json!({}));
    let review = &result["data"]["result"];
    assert_eq!(review["summary"]["closedTrades"], 2);
    assert_eq!(review["summary"]["closedSamples"], 1);
    assert_eq!(review["summary"]["sampleWins"], 1);
    assert_eq!(review["summary"]["sampleWinRateBasisPoints"], 10_000);
    let samples = read_review(&mut db, "review-samples", json!({"view":"samples"}));
    let samples = &samples["data"]["result"]["samples"];
    assert_eq!(samples[0]["qty"], 100);
    assert_eq!(samples[0]["netPnl"], "92");
}

#[test]
fn all_flat_samples_report_no_win_rate() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    record(
        &mut db,
        "buy",
        json!({"symbol":"300502.SZ","side":"buy","price":"10","qty":100,
            "fees":"0","executedAt":NOW - 2 * DAY}),
        NOW - DAY,
    );
    record(
        &mut db,
        "sell",
        json!({"symbol":"300502.SZ","side":"sell","price":"10","qty":100,
            "fees":"0","executedAt":NOW}),
        NOW,
    );
    let result = read_review(&mut db, "review", json!({}));
    let review = &result["data"]["result"];
    assert_eq!(review["summary"]["flat"], 1);
    assert_eq!(review["summary"]["wins"], 0);
    assert_eq!(review["summary"]["losses"], 0);
    assert!(review["summary"]["winRateBasisPoints"].is_null());
    assert!(review["summary"]["sampleWinRateBasisPoints"].is_null());
}

#[test]
fn oversell_matches_only_the_real_lot_and_reports_the_rest() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    record(
        &mut db,
        "buy",
        json!({"symbol":"300502.SZ","side":"buy","price":"10","qty":100,
            "fees":"2","executedAt":NOW - 2 * DAY}),
        NOW - DAY,
    );
    record(
        &mut db,
        "sell",
        json!({"symbol":"300502.SZ","side":"sell","price":"11","qty":150,
            "fees":"3","executedAt":NOW}),
        NOW,
    );
    let result = read_review(&mut db, "review", json!({}));
    let review = &result["data"]["result"];
    assert_eq!(review["summary"]["closedTrades"], 1);
    assert_eq!(review["items"][0]["qty"], 100);
    assert_eq!(review["items"][0]["grossPnl"], "100");
    assert_eq!(review["summary"]["openLots"], 0);
    let excluded = read_review(&mut db, "review-exclusions", json!({"view":"exclusions"}));
    assert_eq!(
        excluded["data"]["result"]["exclusions"][0]["reason"],
        "missing_buy_lot"
    );
}

#[test]
fn duplicate_declaration_is_rejected_and_counted_once() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let first = json!({"symbol":"300502.SZ","side":"buy","price":"10","qty":100,
        "fees":"2","executedAt":NOW - DAY});
    record(&mut db, "buy-a", first.clone(), NOW - DAY);
    let second = record(&mut db, "buy-b", first, NOW - DAY);
    assert_eq!(second["data"]["ok"], false);
    assert_eq!(second["data"]["error"]["code"], "POSSIBLE_DUPLICATE");
    record(
        &mut db,
        "sell",
        json!({"symbol":"300502.SZ","side":"sell","price":"11","qty":100,
            "fees":"0","executedAt":NOW}),
        NOW,
    );
    let result = read_review(&mut db, "review", json!({}));
    let review = &result["data"]["result"];
    assert_eq!(review["summary"]["closedTrades"], 1);
    assert_eq!(review["items"].as_array().unwrap().len(), 1);
}

#[test]
fn to_bound_is_exclusive_and_limit_does_not_change_the_summary() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    record(
        &mut db,
        "buy",
        json!({"symbol":"300502.SZ","side":"buy","price":"10","qty":100,
            "fees":"2","executedAt":NOW - 3 * DAY}),
        NOW - 2 * DAY,
    );
    record(
        &mut db,
        "sell",
        json!({"symbol":"300502.SZ","side":"sell","price":"11","qty":100,
            "fees":"3","executedAt":NOW}),
        NOW,
    );
    let before = read_review(&mut db, "review-before", json!({"to":NOW}));
    assert_eq!(before["data"]["result"]["summary"]["closedTrades"], 0);
    assert_eq!(before["data"]["result"]["summary"]["openLots"], 1);
    let limited = read_review(&mut db, "review-limit", json!({"limit":1}));
    assert_eq!(
        limited["data"]["result"]["items"].as_array().unwrap().len(),
        1
    );
    assert_eq!(limited["data"]["result"]["summary"]["closedTrades"], 1);
    assert_eq!(limited["data"]["result"]["summary"]["netPnl"], "95");
    let samples = read_review(
        &mut db,
        "review-limit-samples",
        json!({"limit":1,"view":"samples"}),
    );
    assert_eq!(
        samples["data"]["result"]["samples"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
}

#[test]
fn review_cursor_pages_keep_the_full_summary_and_order() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    for index in 0..5 {
        record(
            &mut db,
            &format!("buy-{index}"),
            json!({"symbol":"300502.SZ","side":"buy","price":"10","qty":10,
                "fees":"0","executedAt":NOW - (10 - index) * DAY}),
            NOW - (9 - index) * DAY,
        );
        record(
            &mut db,
            &format!("sell-{index}"),
            json!({"symbol":"300502.SZ","side":"sell","price":"11","qty":10,
                "fees":"0","executedAt":NOW - (5 - index) * DAY}),
            NOW - (5 - index) * DAY,
        );
    }
    let first = read_review(&mut db, "review-page-1", json!({"limit":2}));
    let one = &first["data"]["result"];
    assert_eq!(one["items"].as_array().unwrap().len(), 2);
    assert_eq!(one["summary"]["closedTrades"], 5);
    assert_eq!(one["summary"]["grossPnl"], "50");
    assert_eq!(one["hasMore"], true);
    let cursor = one["nextCursor"].as_str().unwrap().to_owned();
    let second = read_review(
        &mut db,
        "review-page-2",
        json!({"limit":2, "cursor":cursor}),
    );
    let two = &second["data"]["result"];
    assert_eq!(two["items"].as_array().unwrap().len(), 2);
    assert_eq!(two["summary"]["closedTrades"], 5);
    assert!(two["items"][0]["buyExecutionId"] != one["items"][1]["buyExecutionId"]);
    let cursor = two["nextCursor"].as_str().unwrap().to_owned();
    let third = read_review(
        &mut db,
        "review-page-3",
        json!({"limit":2, "cursor":cursor}),
    );
    let three = &third["data"]["result"];
    assert_eq!(three["items"].as_array().unwrap().len(), 1);
    assert_eq!(three["hasMore"], false);
    assert!(three["nextCursor"].is_null());
}

#[test]
fn review_cursor_expires_when_the_snapshot_changes() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    record(
        &mut db,
        "buy",
        json!({"symbol":"300502.SZ","side":"buy","price":"10","qty":100,
            "fees":"0","executedAt":NOW - 3 * DAY}),
        NOW - 2 * DAY,
    );
    record(
        &mut db,
        "sell-a",
        json!({"symbol":"300502.SZ","side":"sell","price":"11","qty":20,
            "fees":"0","executedAt":NOW - 2 * DAY}),
        NOW - 2 * DAY,
    );
    record(
        &mut db,
        "sell-b",
        json!({"symbol":"300502.SZ","side":"sell","price":"11","qty":30,
            "fees":"0","executedAt":NOW - DAY}),
        NOW - DAY,
    );
    let first = read_review(&mut db, "review-expire-1", json!({"limit":1}));
    let cursor = first["data"]["result"]["nextCursor"]
        .as_str()
        .unwrap()
        .to_owned();
    record(
        &mut db,
        "sell-c",
        json!({"symbol":"300502.SZ","side":"sell","price":"11","qty":50,
            "fees":"0","executedAt":NOW}),
        NOW,
    );
    let expired = read_review(
        &mut db,
        "review-expire-2",
        json!({"limit":1, "cursor":cursor}),
    );
    assert_eq!(expired["data"]["ok"], false);
    assert_eq!(
        expired["data"]["error"]["code"],
        "TRADE_REVIEW_CURSOR_EXPIRED"
    );
}

#[test]
fn large_review_pages_items_samples_and_exclusions_within_the_business_limit() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    for index in 0..190_u64 {
        record(
            &mut db,
            &format!("large-buy-{index}"),
            json!({"symbol":"300510.SZ","side":"buy","price":"10","qty":1,
                "fees":"0","executedAt":NOW - 20 * DAY + index * 1_000}),
            NOW,
        );
        record(
            &mut db,
            &format!("large-sell-{index}"),
            json!({"symbol":"300510.SZ","side":"sell","price":"11","qty":1,
                "fees":"0","executedAt":NOW - 10 * DAY + index * 1_000}),
            NOW,
        );
    }
    for index in 0..300_u64 {
        record(
            &mut db,
            &format!("large-unmatched-{index}"),
            json!({"symbol":"300510.SZ","side":"sell","price":"11","qty":1,
                "fees":"0","executedAt":NOW - 5 * DAY + index * 1_000}),
            NOW,
        );
    }
    let mut cursor: Option<String> = None;
    let mut item_ids = std::collections::BTreeSet::new();
    let mut first_as_of = None;
    loop {
        let fields = match &cursor {
            Some(cursor) => json!({"symbol":"300510.SZ","limit":50,"view":"items","cursor":cursor}),
            None => json!({"symbol":"300510.SZ","limit":50,"view":"items"}),
        };
        let result = read_review(&mut db, &format!("large-items-{}", item_ids.len()), fields);
        assert_eq!(result["data"]["ok"], true, "{result}");
        let review = &result["data"]["result"];
        assert!(
            serde_json::to_vec(&result).unwrap().len() <= 64 * 1024,
            "items page exceeds the business limit"
        );
        if first_as_of.is_none() {
            first_as_of = review["basis"]["asOf"].as_u64();
        }
        assert_eq!(review["basis"]["asOf"].as_u64(), first_as_of);
        for item in review["items"].as_array().unwrap() {
            item_ids.insert(item["sellExecutionId"].as_str().unwrap().to_owned());
        }
        match review["nextCursor"].as_str() {
            Some(next) => cursor = Some(next.to_owned()),
            None => break,
        }
    }
    assert_eq!(item_ids.len(), 190);
    let mut sample_ids = std::collections::BTreeSet::new();
    cursor = None;
    loop {
        let fields = match &cursor {
            Some(cursor) => {
                json!({"symbol":"300510.SZ","limit":50,"view":"samples","cursor":cursor})
            }
            None => json!({"symbol":"300510.SZ","limit":50,"view":"samples"}),
        };
        let result = read_review(
            &mut db,
            &format!("large-samples-{}", sample_ids.len()),
            fields,
        );
        assert_eq!(result["data"]["ok"], true, "{result}");
        let review = &result["data"]["result"];
        assert!(serde_json::to_vec(&result).unwrap().len() <= 64 * 1024);
        assert_eq!(review["items"], json!([]));
        for sample in review["samples"].as_array().unwrap() {
            sample_ids.insert(sample["entryKey"].as_str().unwrap().to_owned());
        }
        match review["nextCursor"].as_str() {
            Some(next) => cursor = Some(next.to_owned()),
            None => break,
        }
    }
    assert_eq!(sample_ids.len(), 190);
    let mut exclusion_ids = std::collections::BTreeSet::new();
    cursor = None;
    loop {
        let fields = match &cursor {
            Some(cursor) => {
                json!({"symbol":"300510.SZ","limit":100,"view":"exclusions","cursor":cursor})
            }
            None => json!({"symbol":"300510.SZ","limit":100,"view":"exclusions"}),
        };
        let result = read_review(
            &mut db,
            &format!("large-exclusions-{}", exclusion_ids.len()),
            fields,
        );
        assert_eq!(result["data"]["ok"], true, "{result}");
        let review = &result["data"]["result"];
        assert!(serde_json::to_vec(&result).unwrap().len() <= 64 * 1024);
        assert_eq!(review["items"], json!([]));
        assert_eq!(review["samples"], json!([]));
        for exclusion in review["exclusions"].as_array().unwrap() {
            exclusion_ids.insert(exclusion["executionId"].as_str().unwrap().to_owned());
        }
        match review["nextCursor"].as_str() {
            Some(next) => cursor = Some(next.to_owned()),
            None => break,
        }
    }
    assert_eq!(exclusion_ids.len(), 300);

    // A cursor binds its view, query, and complete normalized content.
    let first = read_review(
        &mut db,
        "large-first",
        json!({"symbol":"300510.SZ","limit":2,"view":"items"}),
    );
    let items_cursor = first["data"]["result"]["nextCursor"]
        .as_str()
        .unwrap()
        .to_owned();
    let wrong_view = read_review(
        &mut db,
        "large-wrong-view",
        json!({"symbol":"300510.SZ","limit":2,"view":"samples","cursor":items_cursor}),
    );
    assert_eq!(
        wrong_view["data"]["error"]["code"],
        "TRADE_REVIEW_CURSOR_EXPIRED"
    );
    let wrong_query = read_review(
        &mut db,
        "large-wrong-query",
        json!({"symbol":"300510.SZ","from":NOW - DAY,"limit":2,"view":"items","cursor":items_cursor}),
    );
    assert_eq!(
        wrong_query["data"]["error"]["code"],
        "TRADE_REVIEW_CURSOR_EXPIRED"
    );
}

#[test]
fn one_sample_with_201_buy_evidences_returns_a_bounded_sublist() {
    for count in [199_u64, 200, 201] {
        let dir = tempfile::tempdir().unwrap();
        let mut db = Storage::open(dir.path()).unwrap();
        db.bind_peer(LOCAL_PAIR, PEER).unwrap();
        let task = format!("task-{count}");
        let connection = rusqlite::Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();
        connection
            .execute(
                "INSERT INTO objects VALUES ('tasks',?1,?2)",
                rusqlite::params![
                    task,
                    json!({"taskId":task,"symbol":"300520.SZ","direction":"buy"}).to_string()
                ],
            )
            .unwrap();
        drop(connection);
        for index in 0..count {
            let result = record(
                &mut db,
                &format!("evidence-buy-{index}"),
                json!({"taskId":task,"symbol":"300520.SZ","side":"buy","price":"10","qty":1,
                    "fees":"0","executedAt":NOW - 20 * DAY + index * 1_000}),
                NOW,
            );
            assert_eq!(result["data"]["ok"], true, "{result}");
        }
        let sell = record(
            &mut db,
            "evidence-sell",
            json!({"symbol":"300520.SZ","side":"sell","price":"11","qty":count,
                "fees":"0","executedAt":NOW - DAY}),
            NOW,
        );
        assert_eq!(sell["data"]["ok"], true, "{sell}");
        let mut item_cursor: Option<String> = None;
        let mut item_count = 0usize;
        loop {
            let fields = match &item_cursor {
                Some(cursor) => {
                    json!({"symbol":"300520.SZ","limit":50,"view":"items","cursor":cursor})
                }
                None => json!({"symbol":"300520.SZ","limit":50,"view":"items"}),
            };
            let items = read_review(&mut db, &format!("evidence-items-{item_count}"), fields);
            assert_eq!(items["data"]["ok"], true, "{items}");
            let review = &items["data"]["result"];
            assert_eq!(review["summary"]["closedTrades"], count);
            assert_eq!(review["summary"]["closedSamples"], 1);
            item_count += review["items"].as_array().unwrap().len();
            item_cursor = review["nextCursor"].as_str().map(str::to_owned);
            if item_cursor.is_none() {
                break;
            }
        }
        assert_eq!(item_count, count as usize);
        let samples = read_review(
            &mut db,
            "evidence-samples",
            json!({"symbol":"300520.SZ","limit":1,"view":"samples"}),
        );
        assert_eq!(samples["data"]["ok"], true, "{samples}");
        assert!(serde_json::to_vec(&samples).unwrap().len() <= 64 * 1024);
        let [sample] = samples["data"]["result"]["samples"]
            .as_array()
            .unwrap()
            .as_slice()
        else {
            panic!("expected exactly one sample: {samples}");
        };
        assert_eq!(sample["buyExecutionCount"], count);
        assert_eq!(sample["qty"], count);
        assert_eq!(
            sample["buyExecutionIds"].as_array().unwrap().len(),
            count.min(50) as usize
        );
        let mut seen = std::collections::BTreeSet::new();
        let mut cursor = sample["buyExecutionIdsNextCursor"]
            .as_str()
            .map(str::to_owned);
        let mut pages = 1;
        for id in sample["buyExecutionIds"].as_array().unwrap() {
            assert!(seen.insert(id.as_str().unwrap().to_owned()));
        }
        while let Some(next) = cursor {
            let page = read_review(
                &mut db,
                &format!("evidence-sample-page-{pages}"),
                json!({"symbol":"300520.SZ","limit":200,"view":"samples",
                    "sampleEntryKey":sample["entryKey"],"sampleCursor":next}),
            );
            assert_eq!(page["data"]["ok"], true, "{page}");
            let [row] = page["data"]["result"]["samples"]
                .as_array()
                .unwrap()
                .as_slice()
            else {
                panic!("expected one sample proof page: {page}");
            };
            for id in row["buyExecutionIds"].as_array().unwrap() {
                assert!(seen.insert(id.as_str().unwrap().to_owned()));
            }
            cursor = row["buyExecutionIdsNextCursor"].as_str().map(str::to_owned);
            pages += 1;
            assert!(pages < 10);
        }
        assert_eq!(seen.len(), count as usize);
        assert_eq!(sample["buyExecutionIdsNextCursor"].is_string(), count > 50);
    }
}

#[test]
fn manual_review_notes_are_versioned_and_never_rewrite_trades() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    record(
        &mut db,
        "buy",
        json!({"symbol":"300502.SZ","side":"buy","price":"10","qty":100,
            "fees":"2","executedAt":NOW - 2 * DAY}),
        NOW - DAY,
    );
    let sold = record(
        &mut db,
        "sell",
        json!({"symbol":"300502.SZ","side":"sell","price":"11","qty":100,
            "fees":"3","executedAt":NOW}),
        NOW,
    );
    let execution = sold["data"]["result"]["execution"]["executionId"]
        .as_str()
        .unwrap()
        .to_owned();
    let note = run(
        &mut db,
        "note-a",
        "record_trade_review_note",
        json!({"noteId":"note_a","subjectType":"execution","subjectId":execution,
            "verdict":"false_positive","note":"人工复核：不属于该策略。",
            "reviewer":"local_user","evidenceExecutionIds":[execution],"at":NOW}),
        NOW,
    );
    assert_eq!(note["data"]["ok"], true, "{note}");
    assert_eq!(note["data"]["result"]["note"]["version"], 1);
    assert_eq!(note["data"]["result"]["note"]["status"], "active");

    let duplicate = run(
        &mut db,
        "note-duplicate",
        "record_trade_review_note",
        json!({"noteId":"note_b","subjectType":"execution","subjectId":execution,
            "verdict":"false_positive","note":"人工复核：不属于该策略。",
            "reviewer":"local_user","evidenceExecutionIds":[execution],"at":NOW}),
        NOW,
    );
    assert_eq!(
        duplicate["data"]["error"]["code"],
        "TRADE_REVIEW_NOTE_DUPLICATE"
    );

    let corrected = run(
        &mut db,
        "note-c",
        "record_trade_review_note",
        json!({"noteId":"note_c","subjectType":"execution","subjectId":execution,
            "verdict":"risk_confirmed","note":"补充证据后确认风险。",
            "reviewer":"local_user","evidenceExecutionIds":[execution],"at":NOW,
            "supersedesNoteId":"note_a"}),
        NOW,
    );
    assert_eq!(corrected["data"]["ok"], true, "{corrected}");
    assert_eq!(corrected["data"]["result"]["note"]["version"], 2);
    let listed = run(
        &mut db,
        "list-notes",
        "get_trade_review_notes",
        json!({"subjectId":execution}),
        NOW,
    );
    assert_eq!(listed["data"]["result"]["manualCount"], 1);
    assert_eq!(listed["data"]["result"]["items"][0]["noteId"], "note_c");
    assert_eq!(listed["data"]["result"]["items"][1]["status"], "superseded");
    let all_notes = run(
        &mut db,
        "list-all-notes",
        "get_trade_review_notes",
        json!({"limit":50}),
        NOW,
    );
    assert_eq!(all_notes["data"]["ok"], true, "{all_notes}");
    assert_eq!(all_notes["data"]["result"]["manualCount"], 1);
    assert_eq!(
        all_notes["data"]["result"]["items"]
            .as_array()
            .unwrap()
            .len(),
        2
    );

    let withdrawn = run(
        &mut db,
        "withdraw",
        "withdraw_trade_review_note",
        json!({"noteId":"note_c","baseVersion":2,"reason":"重复记录","at":NOW}),
        NOW,
    );
    assert_eq!(withdrawn["data"]["result"]["note"]["status"], "withdrawn");
    assert_eq!(withdrawn["data"]["result"]["note"]["version"], 3);
    let stale = run(
        &mut db,
        "withdraw-stale",
        "withdraw_trade_review_note",
        json!({"noteId":"note_c","baseVersion":2,"reason":"重复记录","at":NOW}),
        NOW,
    );
    assert_eq!(stale["data"]["error"]["code"], "TRADE_REVIEW_NOTE_CONFLICT");

    let review = read_review(&mut db, "review-after-note", json!({}));
    assert_eq!(review["data"]["result"]["summary"]["netPnl"], "95");
}

#[test]
fn review_period_and_symbol_filter_do_not_fabricate_pairs() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    record(
        &mut db,
        "buy",
        json!({"symbol":"300502.SZ","side":"buy","price":"10","qty":100,
            "executedAt":NOW - 2 * DAY}),
        NOW - DAY,
    );
    record(
        &mut db,
        "sell",
        json!({"symbol":"600000.SH","side":"sell","price":"11","qty":100,
            "executedAt":NOW}),
        NOW,
    );
    let other = read_review(&mut db, "review-symbol", json!({"symbol":"600000.SH"}));
    assert_eq!(other["data"]["result"]["summary"]["closedTrades"], 0);
    assert_eq!(
        other["data"]["result"]["summary"]["openLots"], 0,
        "a sell without a buy lot is not a closed trade"
    );
    let other_excluded = read_review(
        &mut db,
        "review-symbol-exclusions",
        json!({"symbol":"600000.SH","view":"exclusions"}),
    );
    assert_eq!(
        other_excluded["data"]["result"]["exclusions"][0]["reason"],
        "missing_buy_lot"
    );
    let period = read_review(
        &mut db,
        "review-period",
        json!({"from":NOW - 3 * DAY, "to":NOW}),
    );
    assert_eq!(period["data"]["result"]["period"]["from"], NOW - 3 * DAY);
    assert_eq!(period["data"]["result"]["summary"]["openLots"], 1);
}
