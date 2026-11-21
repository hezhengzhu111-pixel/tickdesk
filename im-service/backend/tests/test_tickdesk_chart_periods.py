import json
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl

from app.services.tickdesk_adapter import CN, MarketAdapter, fingerprint, timestamp
from app.services.tickdesk_chart_periods import completed_periods, period_chart_page
from app.services.tickdesk_research import BAR_COLUMNS, FINALITY


def bar(day, prices, *, volume=100, amount=1000):
    return dict(zip(("open", "high", "low", "close"), prices, strict=True),
                symbol="600000.SH", date=day, volume=volume, amount=amount)


def setup_stock(tmp_path, rows, sessions, at, *, omit_witness=()):
    seen = timestamp(at) - 1
    root = tmp_path / "tickdesk-market"
    root.mkdir()
    (root / "calendar.json").write_text(json.dumps(
        {"through": at.date().isoformat(), "observedAt": seen, "sessions": sessions},
    ))
    repo_rows = []
    proof = []
    for item in rows:
        row = {**item, "raw_open": item["open"] * 2, "raw_high": item["high"] * 2,
               "raw_low": item["low"] * 2, "raw_close": item["close"] * 2}
        day = row["date"]
        repo_rows.append({**row, "date": date.fromisoformat(day)})
        if day not in omit_witness:
            proof.append({
                "field": FINALITY, "source": "fixture-feed", "effective_date": day, "symbol": row["symbol"],
                "available_at": at.isoformat(),
                "value": {"bar_hash": fingerprint([day, row["symbol"], *[float(row[key]) for key in BAR_COLUMNS]]),
                          "raw_open": row["raw_open"], "end_at": day + "T15:00:00+08:00"},
            })
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_instruments_asset=lambda asset: pl.DataFrame([{"symbol": "600000.SH", "exchange": "SH", "name": "股票"}])
        if asset == "stock" else pl.DataFrame(),
        get_daily_asset=lambda *args: pl.DataFrame(repo_rows),
        get_quant_evidence_snapshot=lambda fields: SimpleNamespace(reference_rows=proof),
    )
    return MarketAdapter(repo, None)


def test_week_aggregates_the_complete_sessions_only_and_cursor_never_splits_a_week(tmp_path):
    at = datetime(2026, 11, 16, 16, tzinfo=CN)
    sessions = ["2026-11-02", "2026-11-06", "2026-11-09", "2026-11-10", "2026-11-12", "2026-11-16"]
    adapter = setup_stock(tmp_path, [
        bar("2026-11-02", (3, 4, 2, 3)), bar("2026-11-06", (3, 4, 2, 3)),
        bar("2026-11-09", (7, 8, 6, 7)), bar("2026-11-10", (6, 9, 4, 5), volume=200, amount=2000),
        bar("2026-11-12", (5, 10, 4, 9), volume=300), bar("2026-11-16", (77, 90, 75, 88)),
    ], sessions, at)
    now = timestamp(at)
    query = {"symbol": "600000.SH", "adjustment": "none", "limit": 1, "timeframe": "1w"}
    newest = adapter.chart(query, now)
    assert newest["timeframe"] == "1w" and newest["dataStatus"] == "fresh"
    assert newest["candles"] == [{"date": "2026-11-12", "open": "14", "high": "20", "low": "8",
                                  "close": "18", "volume": 600, "amount": "4000"}]
    assert newest["nextBeforeDate"] == "2026-11-12"
    older = adapter.chart({**query, "beforeDate": "2026-11-12"}, now)
    assert older["candles"][0]["date"] == "2026-11-06"
    assert older["hasMore"] is False
    forward = adapter.chart({**query, "adjustment": "forward"}, now)
    assert forward["candles"][0]["open"] == "7" and forward["candles"][0]["close"] == "9"
    assert "timeframe" not in adapter.chart({**query, "timeframe": "1d"}, now)


def test_month_excludes_unfinished_and_unverified_periods(tmp_path):
    at = datetime(2026, 11, 16, 16, tzinfo=CN)
    sessions = ["2026-08-31", "2026-09-01", "2026-09-30", "2026-10-08", "2026-10-30", "2026-11-16"]
    adapter = setup_stock(tmp_path, [bar(day, (2, 4, 1, 3)) for day in sessions], sessions, at,
                          omit_witness=["2026-10-08"])
    result = adapter.chart({"symbol": "600000.SH", "adjustment": "forward", "timeframe": "1mo"}, timestamp(at))
    assert result["timeframe"] == "1mo" and result["dataStatus"] == "stale"
    assert [row["date"] for row in result["candles"]] == ["2026-09-30"]
    assert result["asOf"] == timestamp(datetime(2026, 9, 30, 15, tzinfo=CN))


def test_friday_session_must_be_settled_before_finishing_the_week(tmp_path):
    friday = date(2026, 11, 13)
    sessions = ["2026-11-06", "2026-11-09", "2026-11-13"]
    before_close = datetime(2026, 11, 13, 15, 29, tzinfo=CN)
    settled = datetime(2026, 11, 13, 15, 30, tzinfo=CN)
    setup_stock(tmp_path, [bar(day, (2, 4, 1, 3)) for day in sessions], sessions, before_close)
    assert completed_periods(tmp_path, timestamp(before_close), friday, "1w") == []
    assert completed_periods(tmp_path, timestamp(settled), friday, "1w") == [("2026-11-09", "2026-11-13")]


def test_period_null_amount_does_not_estimate_turnover():
    dates = ("2026-11-12", "2026-11-13")
    source = {"name": "fuyao", "ref": "fund-daily", "asOf": 10}
    daily = {
        dates[0]: (dict(open="9.9", high="10", low="9.5", close="9.8", volume=100, amount=None), source),
        dates[1]: (dict(open="9.8", high="9.9", low="9.7", close="9.7", volume=150, amount="1470"), source),
    }
    chart = period_chart_page({"symbol": "510300.SH", "timeframe": "1w"}, 20, [dates], daily)
    assert chart["candles"] == [dict(date=dates[-1], open="9.9", high="10", low="9.5", close="9.7",
                                     volume=250, amount=None)]
