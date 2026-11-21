"""日线同池相对强弱研究基线。因果收益区间与当前门禁独立。"""

import numpy as np

from app.backtest.matrix import (
    _build_valid_bar_index,
    entry_reason_with_listing,
    listing_minimum,
    make_signal_matrix,
    matrix_feature,
    valid_rolling_mean,
    valid_shift,
)

META = {
    "id": "custom_quant_swing_relative_strength",
    "name": "日线同池相对强弱研究",
    "description": "近期跳过区间之外的收益，在同一历史可交易池内排序后再检查流动性和环境。",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "research_only": True,
    "parameter_version": "daily-swing-relative-strength-v2",
    "entry_invalidation": "同池收益百分位或当日独立入场门禁失效。",
    "params": {
        "lookback": {"type": "int", "default": 63},
        "skip_recent": {"type": "int", "default": 5},
        "minimum_rank_percentile": {"type": "float", "default": None},
        "minimum_listing_sessions": {"type": "int", "default": None},
        "minimum_amount": {"type": "float", "default": None},
        "stop_loss_pct": {"type": "float", "default": None},
        "max_hold_bars": {"type": "int", "default": None},
    },
    "quant_contract": {
        "version": 3, "group": "daily_swing", "family": "relative_strength",
        "decision_timeframe": "1d", "price_basis": "forward_adjusted",
        "required_fields": {
            "entry": ["close", "volume", "amount", "is_st", "listing_sessions_lower_bound",
                      "ext_market_regime", "ext_industry_regime"],
            "exit": ["close", "ma60"],
        },
        "entry_description": "按跳过近期后的区间收益取历史合格证券池的百分位，再检查当日门禁。",
        "exit_description": "完整日线收盘低于 MA60，持仓移出新买入池仍计算。",
        "risk_clocks": {
            "stop_loss": "新鲜原始报价复核持仓保护，触发价按实际成交和 stop_loss_pct 确定。",
            "max_hold": "从实际建仓计算 max_hold_bars 个完整交易日。",
        },
        "scoring_scope": "eligible_universe",
    },
    "scoring": {"momentum_20d": 1.0},
    "order_by": "score",
}

EXECUTION_BACKEND = "matrix_native"
BASIC_FILTER = {"enabled": False}
ENTRY_SIGNALS = ["signal_quant_daily_relative_rank"]
EXIT_SIGNALS = ["signal_quant_relative_strength_trend_break"]


class DailySwingRelativeStrength:
    def required_fields(self):
        return frozenset({"close", "volume", "amount", "is_st", "listing_sessions", "listing_sessions_lower_bound",
                          "ext_market_regime", "ext_industry_regime"})

    def required_warmup_bars(self, params):
        return max(60, _integer(params, "lookback", 63) + _integer(params, "skip_recent", 5, minimum=0) + 1)

    def compute_signals(self, market, params):
        lookback = _integer(params, "lookback", 63)
        skip = _integer(params, "skip_recent", 5, minimum=0)
        minimum_rank = _number(params, "minimum_rank_percentile", maximum=1.0)
        minimum_listing = _integer(params, "minimum_listing_sessions")
        listing_known, listing_enough = listing_minimum(market, minimum_listing)
        minimum_amount = _number(params, "minimum_amount")
        stop_loss = _number(params, "stop_loss_pct")
        if stop_loss >= 1:
            raise ValueError("stop_loss_pct: must be under 1.0")
        _integer(params, "max_hold_bars")

        present = np.isfinite(market.close) & (market.close > 0)
        observed = np.isfinite(market.close) | np.isfinite(market.open) | np.isfinite(market.high)
        observed_index = _build_valid_bar_index(observed)
        closes = np.where(present, market.close, 0)
        numerator = valid_shift(closes, skip, observed, bar_index=observed_index)
        denominator = valid_shift(closes, lookback + skip, observed, bar_index=observed_index)
        coverage = valid_rolling_mean(
            present.astype(np.float32), observed, lookback + skip + 1, bar_index=observed_index,
        )
        returns = np.full(market.shape, np.nan, dtype=np.float32)
        np.divide(numerator, denominator, out=returns, where=denominator > 0)
        returns -= 1
        rank_input = present & (coverage == 1) & np.isfinite(returns)
        historically_eligible = (market.field("is_st") == 0) & (
            listing_enough
        ) & (market.volume > 0)
        if market.eligible_universe is None:
            raise ValueError("historical scoring pool is unavailable")
        pool = market.eligible_universe & rank_input & historically_eligible
        ranks = np.full(market.shape, np.nan, dtype=np.float32)
        for row in range(market.shape[0]):
            selected = pool[row]
            values = returns[row, selected]
            if values.size:
                # Right-inclusive empirical percentile gives equal returns equal ranks.
                ranks[row, selected] = np.searchsorted(np.sort(values), values, side="right") / values.size

        market_regime = market.field("ext_market_regime")
        industry_regime = market.field("ext_industry_regime")
        entry_ready = rank_input & ((market_regime == 0) | (market_regime == 1)) & (
            (industry_regime == 0) | (industry_regime == 1)
        )
        entry_ready &= ~historically_eligible | np.isfinite(ranks)
        entry_ready &= listing_known
        entry = entry_ready & (ranks >= minimum_rank)
        entry &= (market_regime == 1) & (industry_regime == 1) & (market.field("amount") >= minimum_amount)

        ma60 = matrix_feature(market, "ma60")
        exit_ready = present & np.isfinite(ma60)
        exit_ = exit_ready & (market.close < ma60)
        return make_signal_matrix(
            market.shape, entry=entry, exit=exit_, eligible_universe=pool,
            entry_available=entry_ready, exit_available=exit_ready,
            entry_reason=entry_reason_with_listing(entry_ready, listing_known, "relative_rank_input_unavailable"),
            exit_reason=np.where(exit_ready, "", "trend_window_unavailable"),
            entry_signal_code=np.where(entry, 0, -1), exit_signal_code=np.where(exit_, 0, -1),
            entry_signal_ids=tuple(ENTRY_SIGNALS), exit_signal_ids=tuple(EXIT_SIGNALS),
        )


def _integer(params, name, default=None, *, minimum=1):
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= 500:
        raise ValueError(f"{name}: supply an integer between {minimum} and 500")
    return value


def _number(params, name, *, maximum=None):
    value = params.get(name)
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not np.isfinite(value) or value <= 0:
        raise ValueError(f"{name}: supply a positive finite number")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name}: must not exceed {maximum}")
    return value


MATRIX_STRATEGY = DailySwingRelativeStrength()
