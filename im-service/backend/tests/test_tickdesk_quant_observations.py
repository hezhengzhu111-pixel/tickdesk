import polars as pl
import pytest
from fastapi.testclient import TestClient
from tickdesk_quant_fixture import (
    CUTOFF_MS,
    DAY,
    NEXT_DAY,
    STRATEGY_ID,
    SYMBOLS,
    build_fixture,
    install_scan_calendar,
)

from app.backtest.engine import BacktestEngine
from app.backtest.execution_timing import EntryOpenTiming
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService
from app.financial_history import merge_report_revisions


def test_real_adapter_worker_retains_false_missing_and_independent_exit(tmp_path, monkeypatch):
    api = build_fixture(tmp_path / "kernel")
    request = {"data": {"cmd": "scan_now"}, "requestedAt": CUTOFF_MS}
    try:
        with TestClient(api, client=("127.0.0.1", 12345)) as client:
            assert client.post("/api/tickdesk/prepare", json=request).json() == {
                "error": "DATA_UNAVAILABLE", "quotes": [],
                "productionReason": "TRADING_CALENDAR_UNAVAILABLE",
                "tradingCalendar": {"date": DAY.isoformat(), "state": "unknown", "observedAt": None,
                                    "through": None, "source": None},
            }
            install_scan_calendar(monkeypatch.setattr)
            result = client.post("/api/tickdesk/prepare", json={**request, "quantObservationVersion": 3}).json()
    finally:
        api.state.repo.store.db.close()

    batch = result["quantObservationBatch"]
    assert batch["version"] == 3
    assert batch["decisionCutoffMs"] == CUTOFF_MS
    rows = {row["symbol"]: row for row in batch["observations"]}
    assert set(rows) == set(SYMBOLS)
    assert rows[SYMBOLS[0]]["entry"]["value"] is True
    assert rows[SYMBOLS[0]]["exit"]["value"] is False
    assert rows[SYMBOLS[0]]["score"]["value"] == "0"
    assert rows[SYMBOLS[1]]["score"]["value"] == "100"
    assert rows[SYMBOLS[2]]["entry"]["value"] is False
    assert rows[SYMBOLS[3]]["entry"]["value"] is None
    assert rows[SYMBOLS[3]]["exit"]["value"] is True
    assert rows[SYMBOLS[3]]["bar"]["status"] == "complete"
    assert rows[SYMBOLS[3]]["score"]["value"] is None
    assert all(row["admission"] == "research" and row["quote"] is None for row in rows.values())


def test_late_arrival_and_following_intraday_slots_reuse_the_complete_decision(tmp_path, monkeypatch):
    from app.services import tickdesk_worker

    api = build_fixture(tmp_path / "kernel")
    install_scan_calendar(monkeypatch.setattr)
    jobs = 0
    real_worker = tickdesk_worker.run_bounded

    def run(*args, **kwargs):
        nonlocal jobs
        jobs += 1
        return real_worker(*args, **kwargs)

    monkeypatch.setattr(tickdesk_worker, "run_bounded", run)
    try:
        with TestClient(api, client=("127.0.0.1", 12345)) as client:
            def observations(cutoff):
                result = client.post("/api/tickdesk/prepare", json={
                    "data": {"cmd": "scan_now"}, "requestedAt": cutoff, "quantObservationVersion": 3,
                }).json()["quantObservationBatch"]
                assert result["decisionCutoffMs"] == cutoff
                return result["observations"]

            pre_arrival = observations(CUTOFF_MS - 71 * 60_000)
            assert all(row["bar"]["endMs"] < CUTOFF_MS - 24 * 3_600_000 for row in pre_arrival)
            completed = observations(CUTOFF_MS)
            for slot in range(1, 13):
                assert observations(CUTOFF_MS + slot * 300_000) == completed
            following_intraday = CUTOFF_MS + 66 * 3_600_000
            assert observations(following_intraday) == completed
    finally:
        api.state.repo.store.db.close()
    assert jobs == 2


@pytest.mark.parametrize("prepared", [False, True])
def test_backtest_without_entry_preserves_independent_observation(tmp_path, prepared):
    api = build_fixture(tmp_path / "kernel")
    service = StrategyBacktestService(BacktestEngine(api.state.repo), api.state.strategy_engine)
    config = StrategyBacktestConfig(strategy_id=STRATEGY_ID, symbols=[SYMBOLS[3]], start=DAY, end=DAY)
    try:
        inputs = service.prepare_matrix_optimization([config]) if prepared else None
        try:
            result = service.run(config, prepared=inputs)
        finally:
            if inputs is not None:
                inputs.compute_cache.close()
    finally:
        api.state.repo.store.db.close()
    assert result.error is None
    assert result.trades == []
    assert result.stats["quant_observations"] == {
        "entryAvailable": 0, "entryTrue": 0, "exitAvailable": 1, "exitTrue": 1, "scoreAvailable": 0,
    }


def test_malformed_legacy_manifest_does_not_discard_research(tmp_path, monkeypatch):
    root = tmp_path / "kernel"
    api = build_fixture(root)
    install_scan_calendar(monkeypatch.setattr)
    (root / "tickdesk_approvals.json").write_text("{invalid", encoding="utf-8")
    try:
        with TestClient(api, client=("127.0.0.1", 12345)) as client:
            result = client.post("/api/tickdesk/prepare", json={
                "data": {"cmd": "scan_now"}, "requestedAt": CUTOFF_MS, "quantObservationVersion": 3,
            }).json()
    finally:
        api.state.repo.store.db.close()
    assert result["error"] == "DATA_UNAVAILABLE"
    assert len(result["quantObservationBatch"]["observations"]) == 4


def test_malformed_research_configuration_preserves_legacy_and_holding_facts(tmp_path, monkeypatch):
    from app.services import tickdesk_worker

    root = tmp_path / "kernel"
    api = build_fixture(root)
    (root / "tickdesk_research.json").write_text("{invalid", encoding="utf-8")
    (root / "tickdesk_approvals.json").write_text("{}", encoding="utf-8")
    install_scan_calendar(monkeypatch.setattr)
    monkeypatch.setattr(tickdesk_worker, "scan_in_worker", lambda *args, **kwargs: {"asOf": CUTOFF_MS})
    try:
        with TestClient(api, client=("127.0.0.1", 12345)) as client:
            result = client.post("/api/tickdesk/prepare", json={
                "data": {"cmd": "scan_now"}, "requestedAt": CUTOFF_MS, "quantObservationVersion": 3,
                "objects": [{"symbol": SYMBOLS[0], "qty": 1000}],
            }).json()
    finally:
        api.state.repo.store.db.close()
    assert result["frozen"] == {"asOf": CUTOFF_MS}
    assert result["quantObservationError"] == "DATA_UNAVAILABLE"
    assert result["holdingRuleBatch"]["rules"][0]["rule"]["symbol"] == SYMBOLS[0]


def test_quote_cycle_delivers_archived_holding_rules_without_running_a_scan(tmp_path, monkeypatch):
    from app.services import tickdesk_market_data

    def no_scan(*args):
        pytest.fail("quote cycle called trading admission")

    api = build_fixture(tmp_path / "kernel")
    monkeypatch.setattr(tickdesk_market_data, "scan_calendar_evidence", no_scan)
    request = {"data": {"cmd": "quotes"}, "requestedAt": CUTOFF_MS, "paused": True,
               "objects": [{"symbol": SYMBOLS[0], "qty": 1000}, {"symbol": SYMBOLS[1]}]}
    try:
        with TestClient(api, client=("127.0.0.1", 12345)) as client:
            before = client.post("/api/tickdesk/prepare", json=request).json()
            after = client.post("/api/tickdesk/prepare", json={**request, "holdingRuleVersion": 3}).json()
    finally:
        api.state.repo.store.db.close()
    assert set(before) == {"quotes"}
    assert set(after) == {"quotes", "holdingRuleBatch"}
    assert after["quotes"] == before["quotes"]
    assert [item["rule"]["symbol"] for item in after["holdingRuleBatch"]["rules"]] == [SYMBOLS[0]]


def test_strategy_backtest_passes_explicit_entry_timing_to_the_matcher(tmp_path):
    api = build_fixture(tmp_path / "kernel")
    service = StrategyBacktestService(BacktestEngine(api.state.repo), api.state.strategy_engine)
    timing = EntryOpenTiming((), (), (), 5_000)
    try:
        result = service.run(StrategyBacktestConfig(
            strategy_id=STRATEGY_ID, symbols=[SYMBOLS[0]], start=DAY, end=NEXT_DAY,
            entry_open_timing=timing,
        ))
    finally:
        api.state.repo.store.db.close()
    assert result.error is None
    assert result.stats["entry_open_timing"]["fills"] == []
    assert result.stats["execution"]["buy_entry_clock_unavailable"] >= 1
    assert result.config["entry_open_timing"] == timing.summary()


def test_financial_revision_invalidates_research_reuse_without_price_changes(tmp_path, monkeypatch):
    from app.services import tickdesk_worker

    root = tmp_path / "kernel"
    api = build_fixture(root)
    install_scan_calendar(monkeypatch.setattr)
    path = root / "financials/metrics/part.parquet"
    real_worker = tickdesk_worker.run_bounded
    jobs = 0

    def run(*args, **kwargs):
        nonlocal jobs
        jobs += 1
        return real_worker(*args, **kwargs)

    def revise(announce_date, observed_at, roe):
        record = pl.DataFrame({
            "symbol": [SYMBOLS[0]], "period_end": ["2025-09-30"],
            "announce_date": [announce_date], "roe": [roe],
            "_financial_source": ["synthetic-revision"], "_financial_observed_at": [observed_at],
        })
        merge_report_revisions(pl.read_parquet(path), record).write_parquet(path)

    monkeypatch.setattr(tickdesk_worker, "run_bounded", run)
    try:
        with TestClient(api, client=("127.0.0.1", 12345)) as client:
            request = {"data": {"cmd": "scan_now"}, "requestedAt": CUTOFF_MS, "quantObservationVersion": 3}

            def observation():
                return client.post("/api/tickdesk/prepare", json=request).json()["quantObservationBatch"]["observations"][0]

            initial = observation()
            assert initial["entry"]["value"] is True
            revise("2025-12-01", "2025-12-02T02:00:00Z", -1.0)
            changed = observation()
            assert changed["bar"] == initial["bar"]
            assert changed["inputHash"] != initial["inputHash"]
            assert changed["entry"]["value"] is False
            revise("2026-01-06", "2026-01-07T02:00:00Z", 900.0)
            assert observation() == changed
    finally:
        api.state.repo.store.db.close()
    assert jobs == 3
