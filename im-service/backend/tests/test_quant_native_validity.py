from datetime import date

import numpy as np
import polars as pl
import pytest

from app.backtest.matrix import (
    MatrixComputeCache,
    MatrixPipelineConfig,
    MatrixStrategyPipeline,
    apply_time_masks,
    build_market_data_matrix,
    build_market_matrix_from_signals,
    make_signal_matrix,
    slice_signal_matrix,
    validate_signal_matrix,
)
from app.factors.registry import unregister_factor
from app.factors.store import register_definition
from app.quant_evidence import EvidenceFact, QuantInputEvidence
from app.strategy.engine import StrategyDataContext, StrategyEngine
from app.strategy.quant_contract import parse_quant_contract

CONTRACT = dict(version=3, group="daily_swing", family="trend", decision_timeframe="1d",
                required_fields={"entry": ["roe_latest"], "exit": ["raw_close"]},
                price_basis="raw", entry_description="entry", exit_description="exit",
                risk_clocks={"stop_loss": "1d"})


@pytest.mark.parametrize("backend", ["matrix_native", "polars_expr"])
def test_strategy_run_uses_historical_values_and_keeps_independent_verdicts(tmp_path, backend):
    strategy = tmp_path / "synthetic_pit.py"
    metadata = f"""
META = {{
    "id": "synthetic_pit", "name": "PIT", "description": "PIT",
    "execution_backend": {backend!r}, "quant_contract": {CONTRACT!r},
    "scoring": {{"roe_latest": 1}},
}}
EXECUTION_BACKEND = {backend!r}
BASIC_FILTER = {{"enabled": False}}
EXIT_SIGNALS = ["raw_exit"]
"""
    implementation = """
import polars as pl
def filter(frame, params):
    return pl.col("roe_latest") > 0
""" if backend == "polars_expr" else """
from app.backtest.matrix import make_signal_matrix

class SyntheticPIT:
    def required_fields(self):
        return frozenset({"roe_latest", "raw_close"})

    def required_warmup_bars(self, params):
        return 1

    def compute_signals(self, market, params):
        return make_signal_matrix(
            market.shape, entry=market.field("roe_latest") > 0,
            exit=market.field("raw_close") < 10,
        )

MATRIX_STRATEGY = SyntheticPIT()
"""
    strategy.write_text(metadata + implementation, encoding="utf-8")
    symbols = ["000001.SZ", "000002.SZ", "000003.SZ", "000004.SZ"]
    day = date(2025, 1, 2)
    panel = pl.DataFrame({
        "date": [day] * 4, "symbol": symbols, "open": [11.0] * 4,
        "high": [12.0] * 4, "low": [8.0] * 4, "close": [11.0] * 4,
        "volume": [100] * 4, "raw_close": [11.0, 11.0, 11.0, 9.0],
        "roe_latest": [-1.0, -2.0, 9.0, 9.0],
        "signal_raw_exit": [False, False, False, True],
    })
    evidence = QuantInputEvidence(
        "2025-01-02T15:00:00+08:00",
        {(symbol, "roe_latest"): EvidenceFact(value, None) for symbol, value in
         zip(symbols[:3], [1.0, 2.0, -1.0], strict=True)},
        "synthetic",
    )
    context = StrategyDataContext(
        asset_type="stock", timeframe="1d", as_of=day, current=panel, history=panel,
        quant_inputs={str(day): evidence}, current_quant_inputs={str(day): evidence},
    )

    engine = StrategyEngine(strategy_dirs=[tmp_path])
    result = engine.run("synthetic_pit", context)
    shared = engine.run_all(context, strategy_ids=["synthetic_pit"], parallel=False)["synthetic_pit"]
    assert shared.quant_observations == result.quant_observations

    assert result.scores == {symbols[0]: 0.0, symbols[1]: 100.0}
    verdicts = result.quant_observations
    assert verdicts[symbols[0]]["entry"]["value"] is True
    assert verdicts[symbols[2]]["entry"]["value"] is False
    assert verdicts[symbols[2]]["exit"]["value"] is False
    assert verdicts[symbols[3]]["entry"]["value"] is None
    assert verdicts[symbols[3]]["score"]["value"] is None
    assert verdicts[symbols[3]]["exit"]["value"] is True
    assert panel["roe_latest"].to_list() == [-1.0, -2.0, 9.0, 9.0]


def test_native_rebuilds_pit_dependent_precomputed_factor_without_cross_cache_contamination():
    factor = "uf_quant_pit"
    register_definition({
        "id": factor, "label": factor, "formula": "roe_latest * 2",
        "kind": "custom", "status": "active", "version": 1,
    })

    class UsesMaterializedFactor:
        def required_fields(self):
            return frozenset({factor})

        def compute_signals(self, market, params):
            return make_signal_matrix(market.shape, entry=market.field(factor) > 0)

    strategy = UsesMaterializedFactor()
    contract = parse_quant_contract({"quant_contract": {
        **CONTRACT, "required_fields": {"entry": [factor], "exit": []},
    }})
    day = date(2025, 1, 2)
    panel = pl.DataFrame({
        "symbol": ["A", "B"], "date": [day, day], "open": [10.0, 10.0],
        "high": [10.0, 10.0], "low": [10.0, 10.0], "close": [10.0, 10.0],
        "volume": [100, 100], "roe_latest": [-1.0, 2.0], factor: [-2.0, 4.0],
    })
    market = build_market_data_matrix(panel, field_columns={factor, "roe_latest"})
    cache = MatrixComputeCache()
    pipeline = MatrixStrategyPipeline()

    def config(value, identity):
        evidence = QuantInputEvidence(
            "2025-01-02T15:00:00+08:00",
            {("A", "roe_latest"): EvidenceFact(value, None), ("B", "roe_latest"): EvidenceFact(-value, None)},
            identity,
        )
        return MatrixPipelineConfig(
            basic_filter={}, scoring={factor: 1.0}, order_by=None, descending=True,
            quant_contract=contract, quant_inputs={str(day): evidence},
        )

    try:
        with cache.activate(market):
            before = pipeline.run(strategy, market, {}, config(1.0, "positive"))
            after = pipeline.run(strategy, market, {}, config(-1.0, "negative"))
            assert before.entry.tolist() == [[1, 0]]
            assert after.entry.tolist() == [[0, 1]]
            assert market.field(factor).tolist() == [[-2.0, 4.0]]
            replay = pipeline.run(strategy, market, {}, config(1.0, "positive"))
            assert replay.entry.tolist() == [[1, 0]]
        assert cache.snapshot()["hits"] > 0
    finally:
        cache.close()
        unregister_factor(factor)


def test_scoring_scope_is_validated_and_defaults_to_entry_candidates():
    assert parse_quant_contract({"quant_contract": CONTRACT}).scoring_scope == "entry_candidates"
    assert parse_quant_contract({"quant_contract": {**CONTRACT, "scoring_scope": "eligible_universe"}}).scoring_scope == "eligible_universe"
    with pytest.raises(ValueError, match="scoring_scope"):
        parse_quant_contract({"quant_contract": {**CONTRACT, "scoring_scope": "all"}})


def evidence_signals():
    return make_signal_matrix(
        (3, 1), entry=np.array([[1], [0], [1]]), exit=np.array([[0], [1], [0]]),
        score=np.array([[0.0], [99.0], [50.0]]),
        entry_available=np.array([[True], [False], [True]]),
        exit_available=np.array([[True], [True], [False]]),
        score_available=np.array([[True], [False], [True]]),
        entry_reason=np.array([[""], ["missing:roe"], [""]]),
        exit_reason=np.array([[""], [""], ["missing:close"]]),
        score_reason=np.array([[""], ["missing:roe"], [""]]),
        eligible_universe=np.array([[True], [False], [True]]),
    )


def test_signal_evidence_survives_slice_and_time_mask_with_independent_exit():
    signals = evidence_signals()
    selected = apply_time_masks(slice_signal_matrix(signals, 0, 2), np.array([False, True]), np.array([True, True]))
    assert selected.entry[:, 0].tolist() == [0, 0]
    assert selected.exit[:, 0].tolist() == [0, 1]
    assert selected.entry_available[:, 0].tolist() == [True, False]
    assert selected.exit_available[:, 0].tolist() == [True, True]
    assert selected.score_available[:, 0].tolist() == [True, False]
    assert selected.score[0, 0] == 0.0
    assert selected.entry_reason[1, 0] == "missing:roe"
    assert selected.eligible_universe[:, 0].tolist() == [True, False]
    assert not selected.entry_available.flags.writeable
    validate_signal_matrix(selected, (2, 1))


def test_delayed_evidence_and_scores_belong_to_origin_including_false_exit():
    panel = pl.DataFrame({"date": [date(2025, 1, d) for d in (2, 3, 6)],
                          "symbol": ["A"] * 3, "open": [10.] * 3,
                          "high": [11.] * 3, "low": [9.] * 3,
                          "close": [10.] * 3, "volume": [100.] * 3})
    market = build_market_matrix_from_signals(build_market_data_matrix(panel), evidence_signals(), entry_delay_bars=1, exit_delay_bars=1)
    assert market.entry[:, 0].tolist() == [0, 1, 0]
    assert market.entry_available[:, 0].tolist() == [False, True, False]
    assert market.exit_available[:, 0].tolist() == [False, True, True]
    assert market.score_available[:, 0].tolist() == [False, True, False]
    assert market.score[1, 0] == 0.0
    assert market.entry_reason[2, 0] == "missing:roe"
    assert not market.score_available.flags.writeable


def test_unavailable_flags_cannot_become_matcher_entries_or_exits():
    with pytest.raises(ValueError, match="unavailable"):
        make_signal_matrix((1, 1), entry=np.ones((1, 1)), entry_available=np.zeros((1, 1), dtype=bool))
    with pytest.raises(ValueError, match="unavailable"):
        make_signal_matrix((1, 1), exit=np.ones((1, 1)), exit_available=np.zeros((1, 1), dtype=bool))
