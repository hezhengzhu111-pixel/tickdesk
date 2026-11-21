from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.tickdesk import router
from app.services.tickdesk_adapter import MarketAdapter, fingerprint, timestamp
from app.strategy.engine import StrategyDataContext, StrategyEngine
from app.tickflow.repository import DataStore, KlineRepository

NOW = 1794794400000


class Repository:
    def __init__(self, directory):
        self.store = SimpleNamespace(data_dir=directory)
        self.generation = "stable-1"

    def get_instruments_asset(self, asset):
        if asset != "stock":
            return pl.DataFrame()
        return pl.DataFrame([{"symbol": "300502.SZ", "name": "fixture", "exchange": "SZSE",
                              "board": "创业板", "status": "listed", "source": "fixture-provider",
                              "as_of": date(2026, 11, 16)}])

    def get_matrix_data_generation(self, asset):
        return self.generation

    def get_enriched_latest_asset(self, asset):
        return pl.DataFrame([{"symbol":"300502.SZ", "raw_close":12.25, "quote_ts":NOW,
                              "source":"fixture-provider"}]), date(2026,11,16)

    def get_daily_asset(self, asset, symbol, start, end):
        return pl.DataFrame([{"date": date(2026, 11, 13), "open": 10.0, "high": 12.0,
                              "low": 9.0, "close": 11.0, "volume": 1000,
                              "amount": 11000.0}])


def setup(tmp_path, *, score=88.0, source="fixture-provider"):
    files = tmp_path / "strategies"
    files.mkdir()
    for sid in ["a", "b", "c"]:
        (files / f"{sid}.py").write_text(
            f'import polars as pl\nMETA={{"id":"{sid}","name":"{sid}"}}\n'
            'def filter(df, params): return pl.col("close") > 10\n', encoding="utf-8")
    engine = StrategyEngine([files])
    repo = Repository(tmp_path)
    current = pl.DataFrame([{"symbol": "300502.SZ", "date": date(2026, 11, 16),
                             "name": "fixture", "close": 12.0, "raw_close": 12.0,
                             "quote_ts": NOW, "amount": 20000000.0, "score": score,
                             "source": source}])
    calls = []

    def context(engine, day, ids, **kwargs):
        calls.append(tuple(ids))
        return StrategyDataContext("stock", "1d", day, current=current, data_generation=repo.generation)

    overrides = {sid: {"basic_filter": {"enabled": False}} for sid in ["a", "b", "c"]}
    approval = {"version": 1, "approvedBy": "test reviewer", "approvedAt": NOW - 1000,
                "evidence": {"causal": "sha256:causal", "outOfSample": "sha256:oos", "shadow": "sha256:shadow"},
                "profiles": {"conservative": {"minScore": "60", "minimumAmount": "10000"}},
                "strategies": [{"id": sid, "direction": "buy", "weight": "1",
                                "codeHash": fingerprint((files / f"{sid}.py").read_bytes()),
                                "parametersHash": fingerprint(overrides[sid])} for sid in overrides]}
    return MarketAdapter(repo, engine, context_builder=context, override_loader=lambda: overrides,
                         calendar=lambda day: True), approval, calls


def test_native_engine_uses_one_frozen_context_and_rejects_generation_change(tmp_path):
    adapter, approval, calls = setup(tmp_path)
    result = adapter.scan({"requestedAt": NOW, "profile": "conservative"}, approval)
    assert calls == [("a", "b", "c")]
    assert result["strategies"] == {"total": 3, "succeeded": 3, "failed": []}
    assert result["candidates"][0]["candidate"]["strategies"] == ["a", "b", "c"]
    original = adapter.engine.run

    def change(*args, **kwargs):
        result = original(*args, **kwargs)
        adapter.repo.generation = "changed"
        return result

    adapter.engine.run = change
    with pytest.raises(ValueError, match="generation"):
        adapter.scan({"requestedAt": NOW, "profile": "conservative"}, approval)


def test_missing_approval_and_board_fail_closed_and_raw_chart_is_not_forward(tmp_path):
    adapter, approval, _ = setup(tmp_path)
    with pytest.raises(ValueError, match="approval"):
        adapter.scan({"requestedAt": NOW, "profile": "conservative"}, {})
    approval["strategies"][0]["codeHash"] = "wrong"
    with pytest.raises(ValueError, match="approval"):
        adapter.scan({"requestedAt": NOW, "profile": "conservative"}, approval)
    raw = adapter.chart({"symbol": "300502.SZ", "adjustment": "none"}, NOW)
    assert raw["candles"] == []
    adjusted = adapter.chart({"symbol": "300502.SZ", "adjustment": "forward"}, NOW)
    assert adjusted["candles"][0]["close"] == "11.0"
    original = adapter.repo.get_instruments_asset
    adapter.repo.get_instruments_asset = lambda a: original(a).drop("board") if a == "stock" else pl.DataFrame()
    assert adapter.symbols(NOW)[0]["supported"] is False


def test_quotes_omit_unproven_sources_even_with_price_and_timestamp(tmp_path):
    adapter, _, _ = setup(tmp_path)
    assert adapter.quotes(["300502.SZ"], NOW) == []


def test_prepare_quotes_returns_only_requested_prices_with_real_source_clock(tmp_path):
    api = FastAPI()
    api.include_router(router)
    api.state.repo = Repository(tmp_path)
    api.state.tickdesk_quotes = {
        "300502.SZ": {"name": "test-feed", "price": 12.25, "market_timestamp": NOW - 120_000},
        "600000.SH": {"name": "test-feed", "price": 11.7, "market_timestamp": NOW + 1},
        "000001.SZ": {"name": "test-feed", "price": 10.1, "market_timestamp": None, "timestamp": NOW},
        "600519.SH": {"name": "test-feed", "price": 1350, "market_timestamp": NOW},
    }
    with TestClient(api, client=("127.0.0.1", 12345)) as client:
        response = client.post("/api/tickdesk/prepare", json={
            "requestedAt": NOW, "data": {"cmd": "quotes"},
            "objects": [{"symbol": symbol} for symbol in ["300502.SZ", "600000.SH", "000001.SZ"]],
        })
    assert response.json() == {"quotes": [{
        "symbol": "300502.SZ", "price": "12.25", "asOf": NOW - 120_000,
        "source": {"name": "test-feed", "ref": "kernel:realtime:300502.SZ", "asOf": NOW - 120_000},
    }]}


def test_quote_read_keeps_closed_session_separate_from_missing_and_rate_limited(tmp_path, monkeypatch):
    from datetime import datetime

    from app.services.tickdesk_quote_feed import QuoteFeed
    from app.services import preferences
    from app.services.tickdesk_adapter import CN, timestamp

    closed = timestamp(datetime(2026, 11, 16, 16, 30, tzinfo=CN))
    market = tmp_path / "tickdesk-market"
    market.mkdir()
    (market / "calendar.json").write_text(
        '{"sessions":["2026-11-16"],"through":"2026-11-16","observedAt":'+str(closed)+'}', encoding="utf-8",
    )
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "fuyao")
    api = FastAPI()
    api.include_router(router)
    api.state.repo = Repository(tmp_path)
    feed = api.state.tickdesk_quote_feed = QuoteFeed(tmp_path)
    feed.source = "fuyao"
    feed.stock_delivery = "available"
    feed.stocks = {"300502.SZ": {"name": "fuyao", "price": 12.25, "market_timestamp": closed - 1000,
                                  "timeBasis": "snapshot"}}
    feed.fund_symbols = {"510300.SH"}
    feed.fund_delivery = {"510300.SH": "rate_limited"}
    with TestClient(api, client=("127.0.0.1", 12345)) as client:
        payload = client.post("/api/tickdesk/prepare", json={
            "requestedAt": closed, "objects": [],
            "data": {"cmd": "get_quote_status", "symbols": ["300502.SZ", "510300.SH", "600000.SH"]},
        }).json()
    assert payload["quoteFeed"] == {"session": "closed", "delivery": {
        "300502.SZ": "available", "510300.SH": "rate_limited", "600000.SH": "missing",
    }}
    assert [q["symbol"] for q in payload["quotes"]] == ["300502.SZ"]
    assert payload["quotes"][0]["asOf"] == closed - 1000
    assert payload["quotes"][0]["timeBasis"] == "snapshot"


def test_prepare_quotes_omits_vendor_from_real_repository_fallback(tmp_path, monkeypatch):
    day = date(2026, 11, 16)
    part = tmp_path / "kline_daily_enriched" / f"date={day}"
    part.mkdir(parents=True)
    pl.DataFrame([{"symbol": "300502.SZ", "date": day, "open": 11.0, "high": 13.0,
                   "low": 10.0, "close": 12.25, "raw_close": 12.25, "volume": 1000.0,
                   "amount": 12250.0, "quote_ts": NOW, "source": "fixture-bar-provider"}]).write_parquet(
        part / "part.parquet",
    )
    instruments = tmp_path / "instruments"
    instruments.mkdir()
    Repository(tmp_path).get_instruments_asset("stock").write_parquet(instruments / "part.parquet")

    def fail_calculation(*args, **kwargs):
        raise ValueError("fixture indicator computation failed")

    monkeypatch.setattr("app.indicators.pipeline.compute_enriched_history_window", fail_calculation)
    store = DataStore.readonly(tmp_path)
    try:
        repo = KlineRepository(store)
        fallback, _ = repo.get_enriched_latest_asset("stock")
        assert fallback.select("source", "quote_ts").row(0) == ("fixture-bar-provider", NOW)
        api = FastAPI()
        api.include_router(router)
        api.state.repo = repo
        with TestClient(api, client=("127.0.0.1", 12345)) as client:
            response = client.post("/api/tickdesk/prepare", json={
                "requestedAt": NOW, "data": {"cmd": "quotes"}, "objects": [{"symbol": "300502.SZ"}],
            })
        assert response.status_code == 200
        assert response.json() == {"quotes": []}
    finally:
        store.db.close()


def test_normalizer_preserves_explicit_master_evidence_without_inventing_board():
    from app.data_providers.normalizer import normalize_instruments
    row = {"symbol":"300502.SZ","name":"fixture","exchange":"SZSE","board":"创业板",
           "status":"listed","as_of":"2026-11-16"}
    normalized = normalize_instruments([row], "stock", "fixture-provider").to_dicts()[0]
    assert normalized["board"] == "创业板"
    assert normalized["status"] == "listed"
    assert normalized["as_of"] == "2026-11-16"
    old = normalize_instruments([{"symbol":"300502.SZ"}], "stock").to_dicts()[0]
    assert old.get("board") is None


def test_search_master_uses_exchange_board_and_listing_observation(tmp_path):
    path = tmp_path / "tickdesk-market" / "exchange-master.parquet"
    path.parent.mkdir()
    pl.DataFrame([{"symbol": "000001.SZ", "name": "fixture main", "exchange": "SZ",
                   "board": "深主板", "status": "listed", "listing_date": date(1991, 4, 3),
                   "source": "深圳证券交易所", "source_ref": "https://www.szse.cn/market/product/stock/list/",
                   "as_of": NOW - 100}]).write_parquet(path)
    adapter = MarketAdapter(Repository(tmp_path), None)
    main = next((row for row in adapter.symbols(NOW) if row["symbol"] == "000001.SZ"), None)
    assert main == {
        "symbol": "000001.SZ", "name": "fixture main", "exchange": "SZSE", "instrument": "stock",
        "board": "深主板", "supported": True,
        "source": {"name": "深圳证券交易所", "ref": "https://www.szse.cn/market/product/stock/list/", "asOf": NOW - 100},
    }
    assert all(row["symbol"] != "000001.SZ" for row in adapter.symbols(NOW - 101))


def test_timestamp_preserves_the_same_instant_across_types():
    assert timestamp(datetime(2026, 11, 16, 2, tzinfo=UTC)) == NOW
    assert timestamp("2026-11-16T10:00:00+08:00") == NOW
    assert timestamp(NOW) == NOW


@pytest.mark.parametrize(("iso", "millis"), [
    ("2004-01-15T00:00:00.001+00:00", 1074124800001),
    ("2004-01-15T00:00:00.001999+00:00", 1074124800001),
    ("1969-12-31T23:59:59.998001+00:00", -1),
    ("1969-12-31T23:59:59.999999+00:00", 0),
])
def test_timestamp_uses_exact_milliseconds_and_truncates_submillisecond_values(iso, millis):
    assert timestamp(datetime.fromisoformat(iso)) == timestamp(iso) == timestamp(millis)


def test_timestamp_uses_shanghai_midnight_for_date_and_wall_clock_for_naive_datetime():
    assert timestamp(date(2026, 11, 16)) == 1794758400000
    assert timestamp("2026-11-16") == 1794758400000
    assert timestamp(datetime(2026, 11, 16, 10)) == NOW
    assert timestamp("2026-11-16T10:00:00") == NOW


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("minScore", "-0.01"), ("minScore", "100.01"), ("minScore", None),
        ("minScore", True), ("minScore", "NaN"), ("minScore", "Infinity"),
        ("minScore", "abc"), ("minimumAmount", "0"), ("minimumAmount", "-1"),
        ("minimumAmount", False), ("minimumAmount", "-Infinity"),
        ("weight", None), ("weight", True), ("weight", "abc"), ("weight", "NaN"),
        ("weight", "Infinity"), ("weight", "0"), ("weight", "-0.01"),
    ],
)
def test_scan_rejects_invalid_approval_decimal_before_building_context(tmp_path, field, invalid):
    adapter, approval, calls = setup(tmp_path)
    target = approval["strategies"][0] if field == "weight" else approval["profiles"]["conservative"]
    target[field] = invalid
    with pytest.raises(ValueError, match="approval"):
        adapter.scan({"requestedAt": NOW, "profile": "conservative"}, approval)
    assert calls == []


@pytest.mark.parametrize(("score", "expected"), [(0.0, ["0"]), (None, [])])
def test_scan_distinguishes_observed_zero_from_missing_score(tmp_path, score, expected):
    adapter, approval, _ = setup(tmp_path, score=score)
    approval["profiles"]["conservative"]["minScore"] = "0"
    result = adapter.scan({"requestedAt": NOW, "profile": "conservative"}, approval)
    assert [row["candidate"]["score"] for row in result["candidates"]] == expected


def test_scan_averages_large_finite_weights_without_decimal_overflow(tmp_path):
    adapter, approval, _ = setup(tmp_path)
    for spec in approval["strategies"]:
        spec["weight"] = "1E+1000000"
    result = adapter.scan({"requestedAt": NOW, "profile": "conservative"}, approval)
    assert Decimal(result["candidates"][0]["candidate"]["score"]) == Decimal("88")


def _set_scan_scores(adapter, scores):
    original = adapter.engine.run

    def score_strategy(sid, *args, **kwargs):
        result = original(sid, *args, **kwargs)
        for row in result.rows:
            row["score"] = scores[sid]
        return result

    adapter.engine.run = score_strategy


@pytest.mark.parametrize("weights", [("1", "2", "7"), ("1e1000000", "2e1000000", "7e1000000")])
def test_scan_keeps_exact_threshold_with_unequal_weights(tmp_path, weights):
    adapter, approval, _ = setup(tmp_path)
    _set_scan_scores(adapter, {"a": 0, "b": 1, "c": 4})
    for spec, weight in zip(approval["strategies"], weights, strict=True):
        spec["weight"] = weight
    approval["profiles"]["conservative"]["minScore"] = "3"
    result = adapter.scan({"requestedAt": NOW, "profile": "conservative"}, approval)
    assert result["candidates"][0]["candidate"]["score"] == result["policy"]["minScore"] == "3"


def test_scan_serializes_repeating_average_at_the_last_wire_threshold_it_meets(tmp_path):
    adapter, approval, _ = setup(tmp_path)
    _set_scan_scores(adapter, {"a": 0, "b": 1, "c": 1})
    lower = "0." + "6" * 46
    higher = "0." + "6" * 45 + "7"
    for threshold, eligible in [(lower, True), (higher, False)]:
        approval["profiles"]["conservative"]["minScore"] = threshold
        result = adapter.scan({"requestedAt": NOW, "profile": "conservative"}, approval)
        score = result["candidates"][0]["candidate"]["score"]
        assert score == lower
        assert (Decimal(score) >= Decimal(result["policy"]["minScore"])) is eligible


def test_scan_keeps_observed_zero_with_widely_separated_weights(tmp_path):
    adapter, approval, _ = setup(tmp_path, score=0.0)
    approval["strategies"][0]["weight"] = "1e-1000000"
    result = adapter.scan({"requestedAt": NOW, "profile": "conservative"}, approval)
    assert result["candidates"][0]["candidate"]["score"] == "0"


def test_scan_never_rounds_a_positive_score_to_observed_zero(tmp_path):
    adapter, approval, _ = setup(tmp_path)
    _set_scan_scores(adapter, {"a": 0, "b": 0, "c": 1})
    approval["strategies"][2]["weight"] = "1e-50"
    with pytest.raises(ValueError, match="weighted score"):
        adapter.scan({"requestedAt": NOW, "profile": "conservative"}, approval)


def test_scan_fails_closed_when_mixed_scores_have_unbounded_weight_spread(tmp_path):
    adapter, approval, _ = setup(tmp_path)
    _set_scan_scores(adapter, {"a": 1, "b": 0, "c": 4})
    approval["strategies"][0]["weight"] = "1e-1000000"
    with pytest.raises(ValueError, match="weighted score"):
        adapter.scan({"requestedAt": NOW, "profile": "conservative"}, approval)


def test_scan_canonicalizes_approval_thresholds_on_the_wire(tmp_path):
    adapter, approval, _ = setup(tmp_path)
    policy = approval["profiles"]["conservative"]
    policy.update(minScore="1e0", minimumAmount="1e4")
    result = adapter.scan({"requestedAt": NOW, "profile": "conservative"}, approval)
    assert result["policy"] == {"minScore": "1", "minimumAmount": "10000"}
    assert policy == {"minScore": "1e0", "minimumAmount": "1e4"}


@pytest.mark.parametrize(("key", "value"), [("minScore", "1e-47"), ("minimumAmount", "1e48")])
def test_scan_rejects_unencodable_approval_thresholds_before_building_context(tmp_path, key, value):
    adapter, approval, calls = setup(tmp_path)
    approval["profiles"]["conservative"][key] = value
    with pytest.raises(ValueError, match="approval"):
        adapter.scan({"requestedAt": NOW, "profile": "conservative"}, approval)
    assert calls == []


@pytest.mark.parametrize("source", [None, "fixture-bar-provider"])
def test_scan_does_not_use_generic_row_source_as_quote_provenance(tmp_path, source):
    adapter, approval, _ = setup(tmp_path, source=source)
    result = adapter.scan({"requestedAt": NOW, "profile": "conservative"}, approval)
    assert result["candidates"][0]["source"] == {
        "name": "unknown", "ref": "kernel:quote:generation:stable-1", "asOf": NOW,
    }


def test_quotes_reject_rows_without_source(tmp_path):
    adapter, _, _ = setup(tmp_path)
    adapter.repo.get_enriched_latest_asset = lambda asset: (
        pl.DataFrame([{"symbol": "300502.SZ", "raw_close": 12.25, "quote_ts": NOW}]),
        date(2026, 11, 16),
    )
    assert adapter.quotes(["300502.SZ"], NOW) == []


def test_chart_retains_bars_with_unknown_origin_and_availability(tmp_path):
    adapter, _, _ = setup(tmp_path)
    chart = adapter.chart({"symbol": "300502.SZ", "adjustment": "forward"}, NOW)
    assert chart["candles"][0]["date"] == "2026-11-13"
    assert chart["dataStatus"] == "stale"
    assert chart["source"] is None
    assert chart["asOf"] is None


def test_chart_proves_latest_completed_page_and_keeps_raw_open(tmp_path):
    import json

    from app.services.tickdesk_research import FINALITY

    bar = {"symbol": "300502.SZ", "date": date(2026, 11, 13), "open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0,
           "raw_high": 24.0, "raw_low": 18.0, "raw_close": 22.0, "volume": 1000, "amount": 11000.0}
    row = {"field": FINALITY, "effective_date": "2026-11-13", "symbol": "300502.SZ",
           "source": "test-feed", "available_at": "2026-11-13T15:45:00+08:00",
           "value": {"end_at": "2026-11-13T15:00:00+08:00",
                     "bar_hash": fingerprint(["2026-11-13", "300502.SZ", 10.0, 12.0, 9.0, 11.0,
                                              24.0, 18.0, 22.0, 1000.0, 11000.0]),
                     "raw_open": 20.0}}
    repo = Repository(tmp_path)
    repo.get_daily_asset = lambda *args: pl.DataFrame([bar])
    repo.get_quant_evidence_snapshot = lambda fields: SimpleNamespace(reference_rows=(row,))
    calendar = tmp_path / "tickdesk-market" / "calendar.json"
    calendar.parent.mkdir()
    calendar.write_text(json.dumps({"observedAt": NOW - 1, "through": "2026-11-16",
                                    "sessions": ["2026-11-13", "2026-11-16"]}), encoding="utf-8")
    adapter = MarketAdapter(repo, None)
    result = adapter.chart({"symbol": "300502.SZ", "adjustment": "none"}, NOW)
    assert result["candles"][0]["open"] == "20.0"
    assert result["candles"][0]["close"] == "22.0"
    assert result["dataStatus"] == "fresh"
    assert result["asOf"] == 1794553200000
    assert result["source"] == {"name": "test-feed", "ref": "kernel:daily:300502.SZ:none", "asOf": 1794555900000}


def test_chart_does_not_promote_a_quote_timestamp_to_daily_bar_availability(tmp_path):
    adapter, _, _ = setup(tmp_path)
    adapter.repo.get_daily_asset = lambda asset, symbol, start, end: pl.DataFrame([{
        "date": date(2026, 11, 16), "open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0,
        "volume": 1000, "amount": 11000.0, "quote_ts": NOW, "source": "fixture-quotes",
    }])
    chart = adapter.chart({"symbol": "300502.SZ", "adjustment": "forward"}, NOW)
    assert chart["candles"][0]["date"] == "2026-11-16"
    assert chart["dataStatus"] == "stale"
    assert chart["source"] is None
    assert chart["asOf"] is None


def test_prepare_chart_http_keeps_nullable_metadata_and_candles(tmp_path):
    api = FastAPI()
    api.include_router(router)
    api.state.repo = Repository(tmp_path)
    with TestClient(api, client=("127.0.0.1", 12345)) as client:
        response = client.post("/api/tickdesk/prepare", json={
            "requestedAt": NOW,
            "data": {"cmd": "get_chart", "symbol": "300502.SZ", "adjustment": "forward"},
        })
    assert response.status_code == 200
    assert response.json() == {
        "quotes": [],
        "chart": {
            "symbol": "300502.SZ", "adjustment": "forward", "dataStatus": "stale",
            "asOf": None, "source": None, "hasMore": False, "nextBeforeDate": None,
            "nextCursor": None,
            "candles": [{
                "date": "2026-11-13", "open": "10.0", "high": "12.0", "low": "9.0",
                "close": "11.0", "volume": 1000, "amount": "11000.0",
            }],
        },
    }
