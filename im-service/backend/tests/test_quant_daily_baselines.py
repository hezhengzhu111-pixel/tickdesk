from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.api.strategy import StrategyCodeSaveRequest, save_strategy_code_core
from app.quant_evidence import EvidenceFact, QuantInputEvidence, required_quant_fields
from app.services.tickdesk_regime_evidence import INDUSTRY_FIELD, MARKET_FIELD
from app.strategy.engine import StrategyDataContext, StrategyEngine

BASELINES = Path(__file__).resolve().parents[1] / "app/strategy/research/baselines"
SEEDS = BASELINES.parents[1] / "seeds"
IDS = tuple(f"custom_quant_swing_{name}" for name in (
    "breakout", "pullback", "relative_strength", "compression",
))
PARAMS = {
    "minimum_amount": 1_000,
    "minimum_listing_sessions": 30,
    "stop_loss_pct": 0.07,
    "max_hold_bars": 15,
}


def _context(periods, prices, *, missing_regime=None):
    days = [date(2025, 1, 2) + timedelta(days=i) for i in range(periods)]
    records = []
    for symbol, bars in prices.items():
        for day, bar in zip(days[-len(bars):], bars, strict=True):
            records.append({
                "symbol": symbol, "date": day, "amount": 20_000.0,
                "ext_market_regime": 1.0,
                "ext_industry_regime": None if symbol == missing_regime else 1.0,
                **bar,
            })
    frame = pl.DataFrame(records)
    facts = {
        (symbol, "is_st"): EvidenceFact(False, None)
        for symbol in prices
    } | {
        (symbol, "listing_sessions"): EvidenceFact(120, None)
        for symbol in prices
    } | {
        (symbol, MARKET_FIELD): EvidenceFact(1, None)
        for symbol in prices
    } | {
        (symbol, INDUSTRY_FIELD): (EvidenceFact(reason="industry_membership_unavailable")
                                   if symbol == missing_regime else EvidenceFact(1, None))
        for symbol in prices
    }
    return StrategyDataContext(
        asset_type="stock", timeframe="1d", as_of=days[-1],
        current=frame.filter(pl.col("date") == days[-1]), history=frame,
        quant_inputs={
            str(day): QuantInputEvidence(str(day), facts, "synthetic")
            for day in days
        },
    )


def _bar(close, *, high=None, low=None, open_=None, volume=100.0):
    return {
        "close": float(close), "open": float(close if open_ is None else open_),
        "high": float(close + 0.1 if high is None else high),
        "low": float(close - 0.1 if low is None else low), "volume": volume,
    }


@pytest.mark.parametrize(("exact", "bound", "expected"), [(None, 60, True), (None, 59, None), (59, 59, False)])
def test_minimum_listing_can_use_a_proven_lower_bound_without_inventing_a_total(exact, bound, expected):
    symbol = "000001.SZ"
    history = [_bar(10 + i * 0.05) for i in range(64)]
    context = _context(65, {symbol: [*history, _bar(15, high=18, volume=130.0)]})
    inputs = {
        day: replace(snapshot, facts={**snapshot.facts, (symbol, "listing_sessions"): EvidenceFact(exact, None),
                                      (symbol, "listing_sessions_lower_bound"): EvidenceFact(bound, None)})
        for day, snapshot in context.quant_inputs.items()
    }
    engine = StrategyEngine([BASELINES])
    assert {"listing_sessions", "listing_sessions_lower_bound"} <= required_quant_fields(engine.get(IDS[0]))
    result = engine.run(
        IDS[0], replace(context, quant_inputs=inputs), params={**PARAMS, "minimum_listing_sessions": 60},
    ).quant_observations[symbol]
    assert result["entry"]["value"] is expected
    assert result["exit"]["value"] is False
    if expected is None:
        assert result["entry"]["reason"] == "listing_sessions_minimum_unverified"


def test_breakout_import_and_real_engine_use_prior_ohlc_not_prior_close(tmp_path):
    sid = IDS[0]
    engine = StrategyEngine([tmp_path / "strategies" / "custom"])
    assert engine.list_strategies() == []
    source = (BASELINES / f"{sid}.py").read_text(encoding="utf-8")
    saved = save_strategy_code_core(StrategyCodeSaveRequest(
        strategy_id=sid, code=source, target_source="custom", mode="create",
    ), engine, tmp_path)
    assert saved["research_only"] is True
    history = [_bar(10 + i * 0.05) for i in range(64)]
    prior_wick = history.copy()
    prior_wick[-4] = _bar(13.0, high=20)
    context = _context(65, {
        "000001.SZ": [*history, _bar(15, high=18, volume=130.0)],
        "000002.SZ": [*prior_wick, _bar(15, high=18, volume=130.0)],
        "000003.SZ": [*history, _bar(8, volume=130.0)],
        "000004.SZ": [*history[-24:], _bar(8, volume=130.0)],
    }, missing_regime="000003.SZ")

    result = engine.run(sid, context, params=PARAMS)

    assert [item["symbol"] for item in result.entry_signal_hits] == ["000001.SZ"]
    assert result.quant_observations["000001.SZ"]["entry"]["value"] is True
    assert result.quant_observations["000002.SZ"]["entry"]["value"] is False
    assert result.quant_observations["000003.SZ"]["entry"]["value"] is None
    assert result.quant_observations["000003.SZ"]["exit"]["value"] is True
    assert result.quant_observations["000004.SZ"]["entry"]["value"] is None

    with pytest.raises(ValueError, match="minimum_amount"):
        engine.run(sid, context, params={k: v for k, v in PARAMS.items() if k != "minimum_amount"})

    with pytest.raises(ValueError, match="已存在"):
        save_strategy_code_core(StrategyCodeSaveRequest(
            strategy_id=sid, code=source, target_source="custom", mode="create",
        ), engine, tmp_path)


def test_pullback_requires_confirmed_recovery_and_keeps_ordinary_exit():
    engine = StrategyEngine([BASELINES])
    history = [_bar(10 + 0.05 * i) for i in range(63)]
    context = _context(65, {
        "000001.SZ": [*history, _bar(12.45), _bar(13.2, low=12.4, open_=12.45)],
        "000002.SZ": [*history, _bar(13.1, low=12.45), _bar(13.2)],
        "000003.SZ": [*history, _bar(12.45), _bar(11.0)],
    })
    result = engine.run(IDS[1], context, params={
        **PARAMS, "maximum_pullback_ratio": 0.04, "confirmation": "close_reclaim",
    })

    assert [item["symbol"] for item in result.entry_signal_hits] == ["000001.SZ"]
    assert result.quant_observations["000002.SZ"]["entry"]["value"] is False
    assert result.quant_observations["000003.SZ"]["exit"]["value"] is True


@pytest.mark.parametrize("sid", IDS[1:])
def test_each_baseline_is_explicitly_importable_by_existing_validated_save(tmp_path, sid):
    engine = StrategyEngine([tmp_path / "strategies" / "custom", BASELINES.parent])
    assert all(item not in {strategy.meta["id"] for strategy in engine.list_strategies()}
               for item in IDS)
    source = (BASELINES / f"{sid}.py").read_text(encoding="utf-8")
    saved = save_strategy_code_core(StrategyCodeSaveRequest(
        strategy_id=sid, code=source, target_source="custom", mode="create",
    ), engine, tmp_path)
    strategy = engine.get(sid)
    assert saved["research_only"] is True
    assert strategy.quant_contract.group == "daily_swing"
    assert strategy.meta["parameter_version"]
    assert strategy.meta["entry_invalidation"]


def test_relative_strength_ranks_causal_same_pool_returns_with_skip():
    engine = StrategyEngine([BASELINES])
    context = _context(70, {
        "000001.SZ": [_bar(10 + i * 0.10) for i in range(70)],
        "000002.SZ": [_bar(10 + i * 0.05) for i in range(65)] + [_bar(40)] * 5,
        "000003.SZ": [_bar(10 + i * 0.02) for i in range(70)],
    })
    params = {**PARAMS, "minimum_rank_percentile": 0.9}
    result = engine.run(IDS[2], context, params=params)
    assert [item["symbol"] for item in result.entry_signal_hits] == ["000001.SZ"]
    assert result.quant_observations["000002.SZ"]["entry"]["value"] is False
    assert result.quant_observations["000002.SZ"]["score"]["value"] is not None
    smaller = engine.run(IDS[2], context, params=params, pool=["000002.SZ", "000003.SZ"])
    assert [item["symbol"] for item in smaller.entry_signal_hits] == ["000002.SZ"]


def test_compression_uses_pre_breakout_window_and_independent_price_exit():
    engine = StrategyEngine([BASELINES])
    compressed = [_bar(10 + 0.1 * i) for i in range(20)] + [_bar(12)] * 5
    wide = compressed.copy()
    wide[-3] = _bar(12, high=17)
    context = _context(26, {
        "000001.SZ": [*compressed, _bar(13, volume=150.0)],
        "000002.SZ": [*wide, _bar(50, volume=150.0)],
        "000003.SZ": [*compressed, _bar(8, volume=150.0)],
    }, missing_regime="000003.SZ")
    result = engine.run(IDS[3], context, params={
        **PARAMS, "compression_bars": 5, "comparison_bars": 20,
        "maximum_compression_ratio": 0.5, "minimum_volume_ratio": 1.3,
    })

    assert [item["symbol"] for item in result.entry_signal_hits] == ["000001.SZ"]
    assert result.quant_observations["000002.SZ"]["entry"]["value"] is False
    assert result.quant_observations["000003.SZ"]["entry"]["value"] is None
    assert result.quant_observations["000003.SZ"]["exit"]["value"] is True


def test_breakout_validity_does_not_skip_a_missing_high_or_read_future_bars():
    engine = StrategyEngine([BASELINES])
    history = [_bar(10 + i * 0.05) for i in range(64)]
    damaged = history.copy()
    damaged[-4] = {**damaged[-4], "high": None}
    incomplete = history.copy()
    incomplete[-4] = {**incomplete[-4], "close": None}
    prices = {
        "000001.SZ": [*history, _bar(15, high=18, volume=130.0)],
        "000002.SZ": [*damaged, _bar(15, high=18, volume=130.0)],
        "000003.SZ": [*incomplete, _bar(15, high=18, volume=130.0)],
    }
    prefix = _context(65, prices)
    extended = _context(66, {symbol: [*bars, _bar(200)] for symbol, bars in prices.items()})
    extended = replace(extended, as_of=prefix.as_of, current=prefix.current)
    result = engine.run(IDS[0], prefix, params=PARAMS)

    assert result.quant_observations["000002.SZ"]["entry"]["value"] is None
    assert result.quant_observations["000002.SZ"]["exit"]["value"] is False
    assert result.quant_observations["000003.SZ"]["entry"]["value"] is None
    assert engine.run(IDS[0], extended, params=PARAMS).quant_observations == result.quant_observations


def test_legacy_platform_requires_a_full_prior_window_of_narrow_price_action():
    engine = StrategyEngine([SEEDS])
    tight = [_bar(10)] * 12
    wide = [_bar(9 + 0.3 * i) for i in range(12)]
    missing = tight.copy()
    missing[-3] = {**missing[-3], "high": None}
    context = _context(13, {
        "000001.SZ": [*tight, _bar(11, volume=200.0)],
        "000002.SZ": [*wide, _bar(50, volume=200.0)],
        "000003.SZ": [*tight[-5:], _bar(11, volume=200.0)],
        "000004.SZ": [*missing, _bar(11, volume=200.0)],
    })
    result = engine.run("platform_consolidation_breakout", context, overrides={
        "basic_filter": {"enabled": False},
    })

    assert [item["symbol"] for item in result.entry_signal_hits] == ["000001.SZ"]


def test_legacy_dif_recovery_does_not_treat_a_short_window_as_full():
    engine = StrategyEngine([SEEDS])
    complete = [_bar(100)] + [_bar(10 - 0.01 * i) for i in range(30)]
    short = [_bar(100)] + [_bar(10 - 0.01 * i) for i in range(15)]
    context = _context(31, {"000001.SZ": complete, "000002.SZ": short})
    result = engine.run("macd_below_zero_revival", context, overrides={
        "basic_filter": {"enabled": False},
    })
    assert [item["symbol"] for item in result.entry_signal_hits] == ["000001.SZ"]


@pytest.mark.parametrize(("sid", "misleading", "actual"), [
    ("active_limit_gene", "当日未涨停", "含当日"),
    ("long_lower_shadow_reversal", "收复实体", "振幅上半区"),
    ("trend_breakout", "60日新高", "最高收盘"),
    ("breakout_new_high_60d", "最高价", "最高收盘"),
    ("broken_board_recovery", "反包", "封板"),
    ("limit_up_momentum", "接力", "含首板"),
    ("low_volatility_leader", "龙头", "正动量"),
    ("macd_below_zero_revival", "背离", "固定间隔"),
    ("n_day_low_reversal", "触及", "最低收盘"),
    ("near_limit_up", "距涨停 <", "名义涨幅"),
    ("pullback_ma20_bounce", "反弹", "当日上涨"),
    ("pullback_to_support", "支撑", "均线附近"),
    ("rsi_midline_pullback", "企稳", "此前"),
    ("strong_open", "竞价", "盘后"),
])
def test_legacy_template_labels_describe_measured_conditions(sid, misleading, actual):
    strategy = StrategyEngine([SEEDS]).get(sid)
    description = " ".join(strategy.meta[key] for key in ("name", "description"))
    assert misleading not in description
    assert actual in description
