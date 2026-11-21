from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest

from app.tickflow.repository import KlineRepository


def repo(tmp_path):
    return KlineRepository(SimpleNamespace(data_dir=tmp_path, db=None))


def row(field, value, available="2024-01-01T08:00:00+08:00", effective="2024-01-01"):
    return dict(symbol="A", field=field, value=value, source="fixture", effective_date=effective,
                available_at=available)


def test_reference_archive_cutoff_and_revision_identity(tmp_path):
    r = repo(tmp_path)
    assert hasattr(r, "write_quant_reference_evidence")
    r.write_quant_reference_evidence([row("is_st", False)])
    early = r.get_quant_input_evidence(date(2024, 1, 3), ["A"], ["is_st"])
    assert early.fact("A", "is_st").value is False
    r.write_quant_reference_evidence([row("is_st", True, "2024-02-01T08:00:00+08:00")])
    assert r.get_quant_input_evidence(date(2024, 1, 3), ["A"], ["is_st"]).identity == early.identity
    assert r.get_quant_input_evidence(date(2024, 2, 2), ["A"], ["is_st"]).fact("A", "is_st").value is True


def test_missing_master_never_establishes_history_and_short_coverage(tmp_path):
    r = repo(tmp_path)
    assert hasattr(r, "get_quant_input_evidence")
    missing = r.get_quant_input_evidence(date(2024, 1, 9), ["A"], ["listing_sessions", "is_st"])
    assert missing.fact("A", "is_st").reason == "history_not_provided"
    r.write_quant_reference_evidence([row("listing_date", "2024-01-01"), row("trading_sessions", {
        "start": "2024-01-01", "end": "2024-01-09",
        "sessions": [f"2024-01-{d:02d}" for d in range(1, 9)]})])
    evidence = r.get_quant_input_evidence(date(2024, 1, 9), ["A"], ["listing_sessions"])
    assert evidence.fact("A", "listing_sessions").value == 8
    assert evidence.fact("A", "listing_sessions").value < 60


def test_same_clock_conflict_field_scoped_and_intraday(tmp_path):
    r = repo(tmp_path)
    assert hasattr(r, "write_quant_reference_evidence")
    r.write_quant_reference_evidence([row("is_st", False), row("is_st", True), row("settlement_days", 1)])
    facts = r.get_quant_input_evidence(date(2024, 1, 3), ["A"], ["is_st", "settlement_days"])
    assert facts.fact("A", "is_st").reason == "conflicting_evidence"
    assert facts.fact("A", "settlement_days").value == 1
    with pytest.raises(ValueError):
        r.write_quant_reference_evidence([row("is_st", "False")])


def test_shares_raw_market_cap_and_financial_quality(tmp_path):
    r = repo(tmp_path)
    assert hasattr(r, "get_quant_input_evidence")
    path = tmp_path / "financials" / "shares" / "part.parquet"
    path.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": ["A"], "period_end": ["2023-12-31"],
                  "announce_date": ["2024-01-01"], "total_shares": [100.0], "float_shares": [80.0]}).write_parquet(path)
    facts = r.get_quant_input_evidence(date(2024, 1, 3), ["A"], ["market_cap", "roe_latest"],
                                      raw_prices={"A": 10.0})
    assert facts.fact("A", "market_cap").value == 1000
    assert facts.fact("A", "roe_latest").value is None



def test_intraday_available_at_and_date_only_without_calendar(tmp_path):
    from datetime import datetime
    r = repo(tmp_path)
    r.write_quant_reference_evidence([row("is_st", False, "2024-01-03T10:00:00+08:00"),
                                      row("trading_status", "active", "2024-01-02")])
    before = r.get_quant_input_evidence(datetime.fromisoformat("2024-01-03T09:59:00+08:00"), ["A"], ["is_st"])
    after = r.get_quant_input_evidence(datetime.fromisoformat("2024-01-03T10:00:00+08:00"), ["A"], ["is_st"])
    assert before.fact("A", "is_st").value is None
    assert after.fact("A", "is_st").value is False
    assert r.get_quant_input_evidence(date(2024, 1, 3), ["A"], ["trading_status"]).fact("A", "trading_status").value is None


def test_immutable_revision_and_unknown_field_rejected(tmp_path):
    r = repo(tmp_path)
    with pytest.raises(ValueError, match="revision_id"):
        r.write_quant_reference_evidence([{**row("is_st", False), "revision_id": "reused"}])
    with pytest.raises(ValueError, match="unknown reference field"):
        r.write_quant_reference_evidence([row("invented", 1)])


def test_later_unknown_reference_does_not_rewrite_early_cutoff(tmp_path):
    r = repo(tmp_path)
    r.write_quant_reference_evidence([row("name", "Old"), row("settlement_days", 1)])
    early = r.get_quant_input_evidence(date(2024, 1, 3), ["A"], ["name", "settlement_days"])
    r.write_quant_reference_evidence([row("name", "New", "2024-02-01T08:00:00+08:00"),
                                      row("settlement_days", None, "2024-02-01T08:00:00+08:00")])
    assert r.get_quant_input_evidence(date(2024, 1, 3), ["A"], ["name", "settlement_days"]).identity == early.identity
    assert r.get_quant_input_evidence(date(2024, 2, 2), ["A"], ["settlement_days"]).fact("A", "settlement_days").reason == "unknown_value"


def test_screener_real_context_assembles_union_and_identity(tmp_path):
    from test_quant_contract import CONTRACT, source

    from app.services.screener import ScreenerService
    from app.strategy.engine import StrategyEngine
    r = repo(tmp_path)
    r.write_quant_reference_evidence([row("is_st", False)])
    contract = {**CONTRACT, "required_fields": {"entry": ["is_st"], "exit": ["settlement_days"]}}
    path = tmp_path / "definitions" / "custom_quant.py"
    path.parent.mkdir()
    path.write_text(source({"quant_contract": contract}), encoding="utf-8")
    engine = StrategyEngine(strategy_dirs=[path.parent])
    service = ScreenerService(r)
    current = pl.DataFrame({"symbol": ["A"], "date": [date(2024, 1, 3)], "raw_close": [10.]})
    context = service.build_strategy_context(engine, date(2024, 1, 3), ["custom_quant"], current=current)
    assert context.quant_evidence.fact("A", "is_st").value is False
    assert context.quant_evidence.fact("A", "settlement_days").value is None
    before = context.evidence_identity
    r.write_quant_reference_evidence([row("is_st", True, "2024-01-02T08:00:00+08:00")])
    after = service.build_strategy_context(engine, date(2024, 1, 3), ["custom_quant"], current=current)
    assert after.evidence_identity != before


def test_backtest_preparation_exposes_each_actual_cutoff(tmp_path):
    from test_quant_contract import CONTRACT, source

    from app.backtest.strategy import StrategyBacktestService
    from app.strategy.engine import StrategyEngine
    r = repo(tmp_path)
    r.write_quant_reference_evidence([row("is_st", False), row("is_st", True, "2024-01-04T08:00:00+08:00")])
    path = tmp_path / "custom_quant.py"
    path.write_text(source({"quant_contract": {**CONTRACT, "required_fields": {"entry": ["is_st"], "exit": []}}}), encoding="utf-8")
    definition = StrategyEngine._load_file(path)
    service = StrategyBacktestService(SimpleNamespace(repo=r), StrategyEngine())
    panel = pl.DataFrame({"symbol": ["A", "A"], "date": [date(2024, 1, 3), date(2024, 1, 5)]})
    evidence = service.prepare_quant_inputs(definition, panel)
    assert evidence["2024-01-03"].fact("A", "is_st").value is False
    assert evidence["2024-01-05"].fact("A", "is_st").value is True


def test_financial_source_identity_changes_only_when_eligible(tmp_path):
    from app.financial_history import merge_report_revisions
    r = repo(tmp_path)
    path = tmp_path / "financials" / "metrics" / "part.parquet"
    path.parent.mkdir(parents=True)
    first = pl.DataFrame({"symbol": ["A"], "period_end": ["2023-12-31"],
                          "announce_date": ["2024-01-01"], "roe": [0.0],
                          "_financial_source": ["source1"], "_financial_observed_at": ["2024-01-01T08:00:00Z"]})
    first = merge_report_revisions(first)
    first.write_parquet(path)
    early = r.get_quant_input_evidence(date(2024, 1, 3), ["A"], ["roe_latest"])
    assert early.fact("A", "roe_latest").value == 0.0
    replacement = first.drop("_financial_content_id", "_financial_revision_id").with_columns(pl.lit("source2").alias("_financial_source"))
    merge_report_revisions(replacement).write_parquet(path)
    assert r.get_quant_input_evidence(date(2024, 1, 3), ["A"], ["roe_latest"]).identity != early.identity
    first.write_parquet(path)
    second = first.drop("_financial_content_id", "_financial_revision_id").with_columns(
        pl.lit("source2").alias("_financial_source"),
        pl.lit("2024-02-01T08:00:00Z").alias("_financial_observed_at"))
    merge_report_revisions(first, second).write_parquet(path)
    assert r.get_quant_input_evidence(date(2024, 1, 3), ["A"], ["roe_latest"]).identity == early.identity
    assert r.get_quant_input_evidence(date(2024, 2, 2), ["A"], ["roe_latest"]).identity != early.identity


def test_evidence_revisions_rebuild_shared_matrix_without_price_generation(tmp_path):
    from test_shared_matrix_cache import _ctx, _engine_with, _history
    r = repo(tmp_path)
    r.write_quant_reference_evidence([row("is_st", False)])
    engine = _engine_with("m1", {"close"})
    context = _ctx(_history(), "g1")
    context.evidence_identity = r.get_quant_input_evidence(date(2024, 1, 3), ["A"], ["is_st"]).identity
    first = engine.build_shared_matrix(context, [("m1", engine.get("m1"))])
    assert engine.build_shared_matrix(context, [("m1", engine.get("m1"))]) is first
    r.write_quant_reference_evidence([row("is_st", True, "2024-01-02T08:00:00+08:00")])
    context.evidence_identity = r.get_quant_input_evidence(date(2024, 1, 3), ["A"], ["is_st"]).identity
    assert engine.build_shared_matrix(context, [("m1", engine.get("m1"))]) is not first


def test_real_prepared_panel_rejects_changed_reference_cache(tmp_path, monkeypatch):
    from test_quant_contract import CONTRACT

    from app.strategy.quant_contract import parse_quant_contract
    from tests.backtest.test_prepare_panel import _cfg, _panel, _service, _strategy
    from tests.signal_seeds import install_pipeline_caches
    install_pipeline_caches(monkeypatch)
    r = repo(tmp_path)
    r.write_quant_reference_evidence([row("is_st", False)])
    definition = _strategy(quant_contract=parse_quant_contract({"quant_contract": {
        **CONTRACT, "required_fields": {"entry": ["is_st"], "exit": ["settlement_days"]}}}))
    service, engine = _service(_panel(), definition)
    engine.repo = r
    config = _cfg()
    prepared = service.prepare_panel_optimization([config])
    assert prepared.quant_inputs["2024-01-20"].fact("A", "is_st").value is False
    assert prepared.quant_inputs["2024-01-20"].fact("A", "settlement_days").value is None
    assert service.run(config, prepared_panel=prepared).error is None
    r.write_quant_reference_evidence([row("is_st", True, "2024-01-02T08:00:00+08:00")])
    assert "quant input evidence changed" in service.run(config, prepared_panel=prepared).error


def test_minute_context_does_not_use_end_of_day_evidence(tmp_path, monkeypatch):
    from datetime import datetime

    from test_quant_contract import CONTRACT, source

    from app.services.screener import ScreenerService
    from app.strategy.engine import StrategyEngine
    r = repo(tmp_path)
    r.write_quant_reference_evidence([row("is_st", True, "2024-01-03T14:00:00+08:00")])
    path = tmp_path / "definitions" / "custom_quant.py"
    path.parent.mkdir()
    path.write_text(source({"quant_contract": {**CONTRACT, "required_fields": {"entry": ["is_st"], "exit": []}}}), encoding="utf-8")
    engine = StrategyEngine(strategy_dirs=[path.parent])
    service = ScreenerService(r)
    current = pl.DataFrame({"symbol": ["A"], "datetime": [datetime(2024, 1, 3, 10)]})
    monkeypatch.setattr(service, "_load_minute_history", lambda *args: current)
    context = service.build_strategy_context(engine, date(2024, 1, 3), ["custom_quant"], current=current, timeframe="1m")
    assert context.quant_evidence.fact("A", "is_st").value is None


def test_preparation_reads_one_snapshot_for_all_cutoffs_and_refreshes(tmp_path, monkeypatch):
    from collections import Counter

    from test_quant_contract import CONTRACT, source

    from app import quant_evidence
    from app.backtest.strategy import StrategyBacktestService
    from app.strategy.engine import StrategyEngine
    r = repo(tmp_path)
    r.write_quant_reference_evidence([row("is_st", False)])
    path = tmp_path / "custom_quant.py"
    path.write_text(source({"quant_contract": {**CONTRACT, "required_fields": {
        "entry": ["is_st", "roe_latest"], "exit": ["total_shares"]}}}), encoding="utf-8")
    financial_path = tmp_path / "financials" / "metrics" / "part.parquet"
    financial_path.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": ["A", "A"], "period_end": ["2023-12-31", "2023-12-31"],
                  "announce_date": ["2024-01-01", "2024-01-04"], "roe": [1.0, 2.0]}).write_parquet(financial_path)
    definition = StrategyEngine._load_file(path)
    service = StrategyBacktestService(SimpleNamespace(repo=r), StrategyEngine())
    counts = Counter()
    original_read = quant_evidence._read
    original_financial = quant_evidence.get_financial_history
    original_timeline = quant_evidence.point_in_time_report_timeline

    def read(root):
        counts["reference"] += 1
        return original_read(root)

    def financial(root, table):
        counts[table] += 1
        return original_financial(root, table)

    def timeline(*args, **kwargs):
        counts["timeline"] += 1
        return original_timeline(*args, **kwargs)

    monkeypatch.setattr(quant_evidence, "_read", read)
    monkeypatch.setattr(quant_evidence, "get_financial_history", financial)
    monkeypatch.setattr(quant_evidence, "point_in_time_report_timeline", timeline)
    panel = pl.DataFrame({"symbol": ["A", "A", "A"],
                          "date": [date(2024, 1, 3), date(2024, 1, 5), date(2024, 1, 6)]})
    first = service.prepare_quant_inputs(definition, panel)
    assert counts == {"reference": 1, "metrics": 1, "shares": 1, "timeline": 2}
    assert first["2024-01-03"].fact("A", "roe_latest").value == 1.0
    assert first["2024-01-05"].fact("A", "roe_latest").value == 2.0
    r.write_quant_reference_evidence([row("is_st", True, "2024-01-04T08:00:00+08:00")])
    counts.clear()
    second = service.prepare_quant_inputs(definition, panel)
    assert counts == {"reference": 1, "metrics": 1, "shares": 1, "timeline": 2}
    assert second["2024-01-03"].identity == first["2024-01-03"].identity
    assert second["2024-01-05"].identity != first["2024-01-05"].identity
    assert first["2024-01-05"].fact("A", "is_st").value is False
    assert second["2024-01-05"].fact("A", "is_st").value is True


def test_preparation_snapshot_is_deeply_immutable(tmp_path):
    r = repo(tmp_path)
    r.write_quant_reference_evidence([row("trading_sessions", {
        "start": "2024-01-01", "end": "2024-01-03", "sessions": ["2024-01-02"]})])
    snapshot = r.get_quant_evidence_snapshot(["trading_sessions"])
    with pytest.raises(TypeError):
        snapshot.reference_rows[0]["value"]["end"] = "2025-01-01"
    assert isinstance(snapshot.reference_rows[0]["value"]["sessions"], tuple)
    with pytest.raises(TypeError):
        snapshot.financial_rows["metrics"] = ()


def test_reference_archive_workers_are_read_only(tmp_path, monkeypatch):
    import multiprocessing
    r = repo(tmp_path)
    r.write_quant_reference_evidence([row("is_st", False)])
    monkeypatch.setattr(multiprocessing, "parent_process", lambda: object())
    with pytest.raises(RuntimeError, match="main process"):
        r.write_quant_reference_evidence([row("is_st", True)])
    assert r.get_quant_input_evidence(date(2024, 1, 3), ["A"], ["is_st"]).fact("A", "is_st").value is False


def test_forward_filled_financial_provenance_reaches_preparation(tmp_path):
    from test_quant_contract import CONTRACT, source

    from app.backtest.strategy import StrategyBacktestService
    from app.financial_history import merge_report_revisions
    from app.strategy.engine import StrategyEngine
    r = repo(tmp_path)
    path = tmp_path / "financials/metrics/part.parquet"
    path.parent.mkdir(parents=True)
    def first(provider):
        return pl.DataFrame({"symbol": ["A"], "period_end": ["2023-12-31"],
            "announce_date": ["2024-01-01"], "roe": [1.0], "_financial_source": [provider],
            "_financial_observed_at": ["2024-01-01T00:00:00Z"]})
    later = pl.DataFrame({"symbol": ["A"], "period_end": ["2023-12-31"],
        "announce_date": ["2024-01-04"], "roe": [None], "gross_margin": [2.0],
        "_financial_source": ["second"], "_financial_observed_at": ["2024-01-04T00:00:00Z"]})
    definition_path = tmp_path / "custom_quant.py"
    definition_path.write_text(source({"quant_contract": {**CONTRACT, "required_fields": {
        "entry": ["roe_latest"], "exit": ["gross_margin_latest", "net_margin_latest"]}}}), encoding="utf-8")
    definition = StrategyEngine._load_file(definition_path)
    service = StrategyBacktestService(SimpleNamespace(repo=r), StrategyEngine())
    panel = pl.DataFrame({"symbol": ["A"], "date": [date(2024, 1, 6)]})
    merge_report_revisions(first("first-A"), later).write_parquet(path)
    before = service.prepare_quant_inputs(definition, panel)["2024-01-06"]
    merge_report_revisions(first("first-B"), later).write_parquet(path)
    after = service.prepare_quant_inputs(definition, panel)["2024-01-06"]
    assert before.fact("A", "roe_latest").value == after.fact("A", "roe_latest").value == 1.0
    assert before.fact("A", "roe_latest").revision_ids != after.fact("A", "roe_latest").revision_ids
    assert before.fact("A", "gross_margin_latest") == after.fact("A", "gross_margin_latest")
    assert after.fact("A", "gross_margin_latest").reason is None
    assert after.fact("A", "net_margin_latest").value is None


def test_date_only_identity_binds_only_eligible_supporting_calendars(tmp_path):
    r = repo(tmp_path)
    calendar = {"start": "2024-01-01", "end": "2024-01-06", "sessions": ["2024-01-03", "2024-01-05"]}
    r.write_quant_reference_evidence([row("is_st", False, "2024-01-02"),
        {**row("trading_sessions", calendar), "source": "calendar-A"}])
    before = r.get_quant_input_evidence(date(2024, 1, 6), ["A"], ["is_st"])
    r.write_quant_reference_evidence([{**row("trading_sessions", calendar), "source": "calendar-B"}])
    after = r.get_quant_input_evidence(date(2024, 1, 6), ["A"], ["is_st"])
    assert before.fact("A", "is_st").value is after.fact("A", "is_st").value is False
    assert before.identity != after.identity
    r.write_quant_reference_evidence([{**row("trading_sessions", calendar, "2024-02-01T08:00:00+08:00"), "source": "future"}])
    assert r.get_quant_input_evidence(date(2024, 1, 6), ["A"], ["is_st"]).identity == after.identity


@pytest.mark.parametrize("archive", [[], None, {}, {"version": 1, "rows": {}}, {"version": 1, "rows": [None]}])
def test_malformed_archive_shape_preserves_independent_financials(tmp_path, archive):
    import json
    r = repo(tmp_path)
    path = tmp_path / "reference_evidence/v1.json"
    path.parent.mkdir()
    path.write_text(json.dumps(archive), encoding="utf-8")
    financial = tmp_path / "financials/metrics/part.parquet"
    financial.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": ["A"], "period_end": ["2023-12-31"],
                  "announce_date": ["2024-01-01"], "roe": [0.0]}).write_parquet(financial)
    facts = r.get_quant_input_evidence(date(2024, 1, 3), ["A"], ["is_st", "roe_latest"])
    assert facts.fact("A", "is_st").reason == "malformed_history"
    assert facts.fact("A", "roe_latest").value == 0.0
    assert facts.fact("A", "roe_latest").reason is None
