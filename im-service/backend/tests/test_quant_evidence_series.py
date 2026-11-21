from collections import Counter
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
from test_quant_contract import CONTRACT
from test_quant_data_quality import repo, row
from test_shared_matrix_cache import _engine_with

from app import quant_evidence
from app.backtest.matrix import build_market_data_matrix
from app.backtest.strategy import StrategyBacktestService
from app.services.screener import ScreenerService
from app.strategy.quant_contract import parse_quant_contract


def setup_context(tmp_path, monkeypatch):
    r = repo(tmp_path)
    r.write_quant_reference_evidence([row("is_st", False), row("is_st", True, "2024-01-04T08:00:00+08:00")])
    engine = _engine_with("m1", {"close"})
    engine.get("m1").matrix_strategy.required_warmup_bars = lambda params: 2
    engine.get("m1").quant_contract = parse_quant_contract({"quant_contract": {
        **CONTRACT, "required_fields": {"entry": ["is_st", "roe_latest", "market_cap"], "exit": []}}})
    path = tmp_path / "financials/metrics/part.parquet"
    path.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": ["A", "A"], "period_end": ["2023-12-31"] * 2,
                  "announce_date": ["2024-01-01", "2024-01-04"], "roe": [1., 9.]}).write_parquet(path)
    shares = tmp_path / "financials/shares/part.parquet"
    shares.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": ["A"], "period_end": ["2023-12-31"],
                  "announce_date": ["2024-01-01"], "total_shares": [100.]}).write_parquet(shares)
    panel = pl.DataFrame({"symbol": ["A", "A"], "date": [date(2024, 1, 3), date(2024, 1, 5)],
                          "open": [999., 999.], "high": [999., 999.], "low": [999., 999.],
                          "close": [999., 999.], "raw_close": [10., 20.]})
    service = ScreenerService(r)
    monkeypatch.setattr(service, "_load_enriched_history", lambda *a: panel)
    monkeypatch.setattr(r, "get_matrix_data_generation", lambda *a: "g1")
    return r, engine, service, panel


def test_context_and_backtest_share_actual_panel_matrix_axes(tmp_path, monkeypatch):
    r, engine, service, panel = setup_context(tmp_path, monkeypatch)
    counts = Counter()
    original_read, original_financial = quant_evidence._read, quant_evidence.get_financial_history
    def read(root):
        counts["reference"] += 1
        return original_read(root)
    def financial(root, table):
        counts[table] += 1
        return original_financial(root, table)
    monkeypatch.setattr(quant_evidence, "_read", read)
    monkeypatch.setattr(quant_evidence, "get_financial_history", financial)
    market = build_market_data_matrix(panel, field_columns={"raw_close"})
    context = service.build_strategy_context(engine, date(2024, 1, 5), ["m1"], current=panel.tail(1), market=market)
    assert counts == {"reference": 1, "metrics": 1, "shares": 1}
    backtest = StrategyBacktestService(SimpleNamespace(repo=r), engine)
    for actual in (context.quant_inputs, context.market_quant_inputs,
                   backtest.prepare_quant_inputs(engine.get("m1"), panel),
                   backtest.prepare_quant_inputs(engine.get("m1"), market)):
        assert actual["2024-01-03"].fact("A", "roe_latest").value == 1.
        assert actual["2024-01-05"].fact("A", "roe_latest").value == 9.
        assert actual["2024-01-03"].fact("A", "market_cap").value == 1000.
        assert actual["2024-01-05"].fact("A", "market_cap").value == 2000.
    assert context.current_quant_inputs["2024-01-05"].identity == context.quant_evidence.identity


def test_daily_decisions_use_only_reference_revisions_available_by_close(tmp_path, monkeypatch):
    r, engine, service, panel = setup_context(tmp_path, monkeypatch)
    r.write_quant_reference_evidence([
        row("is_st", True, "2024-01-03T15:00:00+08:00"),
        row("is_st", False, "2024-01-03T15:00:00.001+08:00"),
    ])
    market = build_market_data_matrix(panel, field_columns={"raw_close"})
    context = service.build_strategy_context(engine, date(2024, 1, 3), ["m1"], current=panel.head(1), market=market)
    backtest = StrategyBacktestService(SimpleNamespace(repo=r), engine)
    for day in (context.quant_evidence, context.current_quant_inputs["2024-01-03"],
                context.quant_inputs["2024-01-03"], context.market_quant_inputs["2024-01-03"],
                backtest.prepare_quant_inputs(engine.get("m1"), panel)["2024-01-03"],
                backtest.prepare_quant_inputs(engine.get("m1"), market)["2024-01-03"]):
        assert day.fact("A", "is_st").value is True
        assert day.cutoff == "2024-01-03T15:00:00+08:00"
    assert r.get_quant_input_evidence(date(2024, 1, 3), ["A"], {"is_st"}).fact("A", "is_st").value is False


def test_earlier_revision_invalidates_shared_and_realtime_cache(tmp_path, monkeypatch):
    r, engine, service, panel = setup_context(tmp_path, monkeypatch)
    def context():
        return service.build_strategy_context(engine, date(2024, 1, 5), ["m1"], current=panel.tail(1), cache_key="test")
    first = context()
    selected = [("m1", engine.get("m1"))]
    shared = engine.build_shared_matrix(first, selected)
    engine.prepare_realtime_matrix(first, ["m1"])
    assert engine.build_shared_matrix(context(), selected) is shared
    r.write_quant_reference_evidence([row("is_st", True, "2024-01-02T08:00:00+08:00")])
    second = context()
    assert first.evidence_identity == second.evidence_identity
    assert first.quant_inputs["2024-01-03"].identity != second.quant_inputs["2024-01-03"].identity
    changed = engine.build_shared_matrix(second, selected)
    assert changed is not shared
    engine.prepare_realtime_matrix(second, ["m1"])
    assert engine.realtime_matrix_stats("test")["build_count"] == 2
    r.write_quant_reference_evidence([row("is_st", False, "2024-02-01T08:00:00+08:00")])
    future = context()
    assert future.quant_inputs == second.quant_inputs
    assert engine.build_shared_matrix(future, selected) is changed
    engine.prepare_realtime_matrix(future, ["m1"])
    assert engine.realtime_matrix_stats("test")["build_count"] == 2


def test_minute_and_daily_clocks_are_separate(tmp_path, monkeypatch):
    r, engine, service, panel = setup_context(tmp_path, monkeypatch)
    r.write_quant_reference_evidence([row("is_st", True, "2024-01-03T10:00:00+08:00")])
    minute = pl.DataFrame({"symbol": ["A", "A"],
                          "datetime": [datetime(2024, 1, 3, 9, 59), datetime(2024, 1, 3, 10)]})
    monkeypatch.setattr(service, "_load_minute_history", lambda *a: minute)
    monkeypatch.setattr(engine, "minute_daily_history_bars", lambda *a: 2)
    monkeypatch.setattr(service, "_load_enriched_history", lambda *a: panel.head(1).vstack(panel.head(1).with_columns(pl.lit(date(2024, 1, 2)).alias("date"))))
    snapshots = []
    original = r.get_quant_evidence_snapshot
    def snapshot(fields):
        snapshots.append(tuple(fields))
        return original(fields)
    monkeypatch.setattr(r, "get_quant_evidence_snapshot", snapshot)
    auxiliary = panel.head(1).vstack(panel.head(1).with_columns(pl.lit(date(2024, 1, 2)).alias("date")))
    market = build_market_data_matrix(auxiliary, field_columns={"raw_close"})
    context = service.build_strategy_context(engine, date(2024, 1, 3), ["m1"], current=minute.tail(1), timeframe="1m", market=market)
    assert len(snapshots) == 1
    assert set(context.market_quant_inputs) == {"2024-01-02"}
    assert context.quant_inputs["2024-01-03 09:59:00"].fact("A", "is_st").value is False
    assert context.quant_inputs["2024-01-03 10:00:00"].fact("A", "is_st").value is True
    assert set(context.daily_quant_inputs) == {"2024-01-02"}
    assert context.daily_quant_inputs["2024-01-02"].fact("A", "is_st").value is False
    assert context.quant_evidence.fact("A", "is_st").value is True


def test_legacy_context_does_not_read_evidence(tmp_path, monkeypatch):
    r, engine, service, panel = setup_context(tmp_path, monkeypatch)
    engine.get("m1").quant_contract = None
    monkeypatch.setattr(r, "get_quant_evidence_snapshot", lambda *a: (_ for _ in ()).throw(AssertionError("legacy read")))
    context = service.build_strategy_context(engine, date(2024, 1, 5), ["m1"], current=panel.tail(1))
    assert context.quant_inputs == context.daily_quant_inputs == context.current_quant_inputs == context.market_quant_inputs == {}
    assert context.quant_evidence is None


def test_explicit_context_missing_history_evidence_never_reuses_cache(tmp_path, monkeypatch):
    _r, engine, service, panel = setup_context(tmp_path, monkeypatch)
    context = service.build_strategy_context(engine, date(2024, 1, 5), ["m1"], current=panel.tail(1), cache_key="missing")
    context.quant_inputs.clear()
    selected = [("m1", engine.get("m1"))]
    first = engine.build_shared_matrix(context, selected)
    assert engine.build_shared_matrix(context, selected) is not first
    engine.prepare_realtime_matrix(context, ["m1"])
    engine.prepare_realtime_matrix(context, ["m1"])
    assert engine.realtime_matrix_stats("missing")["build_count"] == 2


def test_real_prepared_panel_retains_opposing_historical_facts(tmp_path, monkeypatch):
    from tests.backtest.test_prepare_panel import _cfg, _panel, _service, _strategy
    from tests.signal_seeds import install_pipeline_caches

    install_pipeline_caches(monkeypatch)
    r = repo(tmp_path)
    r.write_quant_reference_evidence([row("is_st", False), row("is_st", True, "2024-02-01T08:00:00+08:00")])
    definition = _strategy(quant_contract=parse_quant_contract({"quant_contract": {
        **CONTRACT, "required_fields": {"entry": ["is_st"], "exit": []}}}))
    service, engine = _service(_panel(), definition)
    engine.repo = r
    prepared = service.prepare_panel_optimization([_cfg()])
    assert prepared.quant_inputs["2024-01-20"].fact("A", "is_st").value is False
    assert prepared.quant_inputs["2024-02-02"].fact("A", "is_st").value is True
    assert prepared.quant_inputs["2024-02-02"].fact("B", "is_st").value is None
