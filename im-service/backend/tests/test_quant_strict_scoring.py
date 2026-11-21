from datetime import date
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest

from app.backtest.matrix import build_market_data_matrix, build_matrix_score
from app.backtest.strategy import StrategyBacktestService
from app.factors.registry import FactorSpec, register_factor, unregister_factor
from app.strategy.engine import StrategyEngine


def frame():
    return pl.DataFrame({"symbol": ["A", "B", "C"], "date": [date(2024, 1, 2)] * 3,
                         "open": [10.] * 3, "high": [11.] * 3, "low": [9.] * 3,
                         "close": [0., 1000., 10.], "volume": [10.] * 3,
                         "turnover_rate": [0., None, 10.]})


@pytest.mark.parametrize("backend", ["matrix", "screen", "panel"])
def test_strict_complete_pool_and_observed_zero(backend):
    data = frame()
    weights = {"close": 1., "turnover_rate": 1.}
    if backend == "matrix":
        market = build_market_data_matrix(data, field_columns={"turnover_rate"})
        scores = build_matrix_score(market, np.ones((1, 3), dtype=bool), weights, None, True,
                                    fallback=np.zeros((1, 3)), strict=True)[0].tolist()
    elif backend == "screen":
        scored = StrategyEngine._apply_scoring(data, weights, strict=True)
        scores = scored["score"].to_list()
        assert scored["score_available"].to_list() == [True, False, True]
    else:
        strategy = SimpleNamespace(meta={"scoring": weights}, quant_contract=object())
        scored = StrategyBacktestService._apply_score(data, strategy, None)
        scores = scored["score"].to_list()
        assert scored["score_available"].to_list() == [True, False, True]
    assert scores[0] == 0.
    assert scores[2] == 100.


@pytest.mark.parametrize("backend", ["screen", "panel"])
def test_strict_missing_column_does_not_drop_weight(backend):
    data = frame().drop("turnover_rate")
    weights = {"close": 1., "turnover_rate": 1.}
    if backend == "screen":
        scored = StrategyEngine._apply_scoring(data, weights, strict=True)
    else:
        strategy = SimpleNamespace(meta={"scoring": weights}, quant_contract=object())
        scored = StrategyBacktestService._apply_score(data, strategy, None)
    assert scored["score_available"].to_list() == [False] * 3
    assert scored["score"].to_list() == [None] * 3


def test_complete_pool_directions_constants_and_empty():
    from app.strategy.scoring import complete_pool_frame_score
    data = frame().filter(pl.col("symbol") != "B")
    result = complete_pool_frame_score(data, {"turnover_rate": 1.}, {"turnover_rate": "low"})
    assert result["score"].to_list() == [100., 0.]
    assert result["score_available"].to_list() == [True, True]
    result = complete_pool_frame_score(data.with_columns(pl.lit(0.).alias("turnover_rate")),
                                       {"turnover_rate": 1.})
    assert result["score"].to_list() == [50., 50.]
    result = complete_pool_frame_score(data.head(0), {"turnover_rate": 1.})
    assert result.is_empty()
    assert result.schema["score_available"] == pl.Boolean


def test_complete_pool_dates_and_explicit_pool():
    from app.strategy.scoring import complete_pool_frame_score
    first = frame().filter(pl.col("symbol") != "B")
    second = first.with_columns(pl.lit(date(2024, 1, 3)).alias("date"),
                               (pl.col("close") + 100).alias("close"))
    result = complete_pool_frame_score(pl.concat([first, second]), {"close": 1.},
                                       universe=[True, True, False, True])
    assert result["score"].to_list() == [0., 100., None, 50.]
    assert result["score_reason"].to_list() == ["", "", "outside_scoring_pool", ""]


def test_composite_normalization_excludes_outlier_missing_an_independent_factor():
    from app.strategy.scoring import complete_pool_frame_score

    name = "uf_quant_composite"
    register_factor(FactorSpec(
        id=name, label=name, group="custom", formula_text="", kind="composite",
        components=(("close", 1.0), ("volume", 1.0)),
    ))
    panel = frame().with_columns(
        pl.Series("volume", [10.0, 1.0, 0.0]),
        pl.Series("turnover_rate", [1.0, None, 1.0]),
    )
    try:
        result = complete_pool_frame_score(panel, {name: 1.0, "turnover_rate": 1.0})
        assert result["score"].to_list() == [50.0, None, 50.0]
    finally:
        unregister_factor(name)
