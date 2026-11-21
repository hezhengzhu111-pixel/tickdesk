from datetime import datetime
from zoneinfo import ZoneInfo

import polars as pl
import pytest

CN = ZoneInfo("Asia/Shanghai")


def test_deployed_catalog_loads_all_daily_research_baselines(tmp_path):
    from app.services.tickdesk_research import strategy_directories
    from app.strategy.engine import StrategyEngine

    engine = StrategyEngine(strategy_directories(tmp_path))
    for name in ("breakout", "pullback", "compression", "relative_strength"):
        strategy = engine.get("custom_quant_swing_" + name)
        assert strategy.meta["research_only"] is True
        assert strategy.quant_contract.version == 3


def test_shared_matrix_prewarm_does_not_invent_v3_research_parameters(tmp_path):
    from app.backtest.strategy import build_matrix_cache_profile
    from app.services.tickdesk_research import strategy_directories
    from app.strategy.engine import StrategyEngine

    profile = build_matrix_cache_profile(StrategyEngine(strategy_directories(tmp_path)), "stock")
    assert profile.warmup_bars > 0


def test_fuyao_master_retains_identity_without_guessing_eligibility(monkeypatch):
    from app.plugins.fuyao.provider import FuyaoProvider

    class Client:
        def ticker_list(self, asset_type):
            assert asset_type == "a-share"
            return [{"thscode": "600000.SH", "ticker": "600000", "name": "浦发银行", "exchange": "SH",
                     "asset_type": "a-share", "list_date": "1999-11-10"}]

    monkeypatch.setattr(FuyaoProvider, "_get_client", lambda self: Client())
    rows = FuyaoProvider().get_instruments("stock")
    assert rows[0]["symbol"] == "600000.SH"
    assert rows[0]["source"] == "fuyao"
    assert rows[0]["board"] == "unknown"
    assert rows[0]["status"] == "unknown"
    assert rows[0]["ext"]["listing_date"] == "1999-11-10"


def test_strict_adjustment_failure_cannot_be_used_as_no_corporate_actions(monkeypatch):
    from app.plugins.fuyao.client import FuyaoError
    from app.plugins.fuyao.provider import FuyaoProvider

    def failure(*args, **kwargs):
        raise FuyaoError("provider unavailable")

    monkeypatch.setattr(FuyaoProvider, "_load_adj_events", failure)
    with pytest.raises(FuyaoError):
        FuyaoProvider().get_adj_factors(["600000.SH"], None, None, strict=True)


def test_completed_bar_witnesses_bind_values_and_use_actual_arrival(tmp_path):
    from app.services.tickdesk_market_data import completed_bar_witnesses
    from app.services.tickdesk_research import BAR_COLUMNS

    at = datetime(2026, 11, 16, 11, 0, tzinfo=CN)
    rows = [{"date": day, "symbol": "600000.SH", **dict.fromkeys(BAR_COLUMNS, 10.0)}
            for day in ("2026-11-13", "2026-11-16")]
    rows[0]["volume"] = 100
    rows[1]["volume"] = 100
    bars = pl.DataFrame(rows).with_columns(pl.col("date").str.to_date())
    witnesses = completed_bar_witnesses(bars, at, source="fuyao", sessions={"2026-11-13", "2026-11-16"})
    assert len(witnesses) == 1
    assert witnesses[0]["effective_date"] == "2026-11-13"
    assert witnesses[0]["available_at"] == at.isoformat()
    assert witnesses[0]["value"]["end_at"] == "2026-11-13T15:00:00+08:00"
    assert len(witnesses[0]["value"]["bar_hash"]) == 64
    changed = bars.with_columns((pl.col("close") + 1).alias("close"))
    # An inconsistent OHLC cannot receive a finality witness.
    with pytest.raises(ValueError):
        completed_bar_witnesses(changed, at, source="fuyao", sessions={"2026-11-13"})


def test_finality_requires_provider_session_and_after_close_grace():
    from app.services.tickdesk_market_data import completed_bar_witnesses
    from app.services.tickdesk_research import BAR_COLUMNS

    bars = pl.DataFrame([{"date": "2026-11-16", "symbol": "600000.SH", **dict.fromkeys(BAR_COLUMNS, 10.0)}])
    for hour, minute, expected in ((14, 0, 0), (15, 20, 0), (15, 30, 1)):
        at = datetime(2026, 11, 16, hour, minute, tzinfo=CN)
        assert len(completed_bar_witnesses(bars, at, source="fuyao", sessions={"2026-11-16"})) == expected
    assert not completed_bar_witnesses(bars, datetime(2026, 11, 17, tzinfo=CN), source="fuyao", sessions=set())


def test_existing_witness_is_not_retimestamped_and_revision_is_append_only(tmp_path):
    from app.services.tickdesk_market_data import new_witnesses

    old = {"symbol": "600000.SH", "field": "daily_bar_finality", "effective_date": "2026-11-13",
           "source": "fuyao", "available_at": "2026-11-16T09:00:00+08:00",
           "value": {"end_at": "2026-11-13T15:00:00+08:00", "bar_hash": "a" * 64}}
    same = {**old, "available_at": "2026-11-16T10:00:00+08:00"}
    assert new_witnesses([same], [old]) == []
    revised = {**same, "value": {**old["value"], "bar_hash": "b" * 64}}
    assert new_witnesses([revised], [old]) == [revised]


@pytest.mark.parametrize("missing_latest", [False, True])
def test_first_real_pipeline_publication_refreshes_empty_repository_views(tmp_path, monkeypatch, missing_latest):
    import json
    from datetime import date, timedelta

    from app.data_providers import custom
    from app.services import preferences
    from app.services.tickdesk_market_data import refresh_research_data
    from app.tickflow.repository import DataStore, KlineRepository

    root = tmp_path / "data"
    repo = KlineRepository(DataStore(root))
    (root / "tickdesk_research.json").write_text(json.dumps({
        "version": 3, "strategies": ["custom_quant_swing_breakout"], "symbols": ["600000.SH"],
    }))
    end = date(2026, 11, 13)
    dates = [end - timedelta(days=i) for i in range(100) if (end - timedelta(days=i)).weekday() < 5]
    pl.DataFrame({"symbol": ["600000.SH"], "total_shares": [1234567.0],
                  "float_shares": [765432.0], "tick_size": [0.01]}).write_parquet(root / "instruments/instruments.parquet")

    class Provider:
        def get_research_daily(self, symbols, start, finish):
            return {"daily": pl.DataFrame([{"date": day, "symbol": "600000.SH", "open": 10.0,
                        "high": 11.0, "low": 9.0, "close": 10.0, "volume": 1000, "amount": 1000000.0}
                       for day in dates if not missing_latest or day != end]),
                    "factors": pl.DataFrame(schema={"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64}),
                    "sessions": dates, "source": "test-provider",
                    "observed_at": datetime(2026, 11, 16, 11, tzinfo=CN),
                    "instruments": [{"symbol": "600000.SH", "name": "Fixture", "code": "600000",
                                     "exchange": "SH", "type": "stock", "ext": {}}]}

    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "fixture")
    monkeypatch.setattr(custom, "get_provider", lambda source: Provider())
    try:
        if missing_latest:
            with pytest.raises(ValueError, match="RESEARCH_LATEST_SESSION_MISSING"):
                refresh_research_data(repo, now=datetime(2026, 11, 16, 11, tzinfo=CN))
            assert not (root / "tickdesk-market/state.json").exists()
            return
        result = refresh_research_data(repo, now=datetime(2026, 11, 16, 11, tzinfo=CN))
        assert result["status"] == "ready"
        assert result["witnesses"] == len(dates)
        assert repo.get_enriched_range(min(dates), max(dates)) is not None
        instrument = pl.read_parquet(root / "instruments/instruments.parquet").row(0, named=True)
        assert instrument["total_shares"] == 1234567.0
        assert instrument["float_shares"] == 765432.0
        assert instrument["tick_size"] == 0.01
    finally:
        repo.store.db.close()


@pytest.mark.parametrize("allot,price", [(0.2, None), (0.2, 0.0), (0.0, 0.0)])
def test_strict_adjustment_rejects_incomplete_events_before_filtering(monkeypatch, allot, price):
    from app.plugins.fuyao.client import FuyaoError
    from app.plugins.fuyao.provider import FuyaoProvider

    event = {"thscode": "600000.SH", "ex_date_ms": int(datetime(2026, 6, 1, tzinfo=CN).timestamp() * 1000),
             "dividend_per_share": 0.0, "per_share_bonus": 0.0,
             "allotment_ratio": allot, "allotment_price": price}
    monkeypatch.setattr(FuyaoProvider, "_ensure_dump", lambda *args: pl.DataFrame([event]))
    provider = FuyaoProvider()
    with pytest.raises(FuyaoError, match="incomplete"):
        provider.get_adj_factors(["600000.SH"], datetime(2026, 1, 1), datetime(2026, 11, 1), strict=True)
    # Unrelated securities/windows cannot poison the requested evidence.
    assert provider.get_adj_factors(["000001.SZ"], None, None, strict=True).is_empty()
    assert provider.get_adj_factors(["600000.SH"], datetime(2026, 7, 1), None, strict=True).is_empty()


def test_strict_adjustment_checks_new_release_in_same_process(monkeypatch):
    from app.plugins.fuyao.provider import _ADJ_DUMP_KIND, FuyaoProvider

    provider = FuyaoProvider()
    checks = []

    def load(kind, prefix):
        assert kind not in provider._dump_memo
        assert kind not in provider._dump_path_memo
        checks.append(kind)
        frame = pl.DataFrame({"thscode": ["600000.SH"],
            "ex_date_ms": [int(datetime(2026, 6, 1, tzinfo=CN).timestamp() * 1000)],
            "dividend_per_share": [len(checks) * 0.1], "per_share_bonus": [0.0],
            "allotment_ratio": [0.0], "allotment_price": [0.0]})
        provider._dump_memo[_ADJ_DUMP_KIND] = frame
        provider._dump_path_memo[_ADJ_DUMP_KIND] = "cached"
        return frame

    monkeypatch.setattr(provider, "_ensure_dump", load)
    monkeypatch.setattr(provider, "_fetch_closes", lambda *args: {datetime(2026, 5, 29).date(): 10.0})
    first = provider.get_adj_factors(["600000.SH"], None, None, strict=True)
    second = provider.get_adj_factors(["600000.SH"], None, None, strict=True)
    assert len(checks) == 2
    assert first["ex_factor"][0] != second["ex_factor"][0]


def test_shutdown_waits_for_inflight_publication(monkeypatch):
    import asyncio
    import threading
    from types import SimpleNamespace

    from app.services import tickdesk_market_data as service

    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def refresh(repo):
        started.set()
        release.wait(5)
        finished.set()
        return {"status": "ready"}

    monkeypatch.setenv("TICKDESK_QUANT_DATA_ENABLED", "1")
    monkeypatch.setattr(service, "refresh_research_data", refresh)

    async def scenario():
        context = service.market_data_lifespan(SimpleNamespace(state=SimpleNamespace(repo=None)))
        await context.__aenter__()
        assert await asyncio.to_thread(started.wait, 2)
        closing = asyncio.create_task(context.__aexit__(None, None, None))
        try:
            await asyncio.sleep(0.05)
            assert not closing.done(), "Shutdown released the repository while a publisher was still running"
        finally:
            release.set()
            await closing
        assert finished.is_set()

    asyncio.run(scenario())
