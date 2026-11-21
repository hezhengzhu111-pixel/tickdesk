"""日线趋势突破研究基线。只在已完成的日线输入上评估。"""

import numpy as np

from app.backtest.matrix import (
    _build_valid_bar_index,
    entry_reason_with_listing,
    listing_minimum,
    make_signal_matrix,
    matrix_feature,
    valid_rolling_max,
    valid_rolling_mean,
    valid_shift,
)

META = {
    "id": "custom_quant_swing_breakout",
    "name": "日线趋势突破研究",
    "description": "收盘突破前窗最高价，MA20 在 MA60 上方，并通过量能及市场、行业条件。",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "research_only": True,
    "parameter_version": "daily-swing-breakout-v2",
    "entry_invalidation": "趋势或收盘突破条件不再成立，当前报价执行条件须由本地服务重检。",
    "params": {
        "lookback": {"type": "int", "default": 60},
        "min_volume_ratio": {"type": "float", "default": 1.3},
        "minimum_listing_sessions": {"type": "int", "default": None},
        "minimum_amount": {"type": "float", "default": None},
        "stop_loss_pct": {"type": "float", "default": None},
        "max_hold_bars": {"type": "int", "default": None},
    },
    "quant_contract": {
        "version": 3, "group": "daily_swing", "family": "trend_breakout",
        "decision_timeframe": "1d", "price_basis": "forward_adjusted",
        "required_fields": {
            "entry": ["close", "volume", "amount", "ma20", "ma60", "is_st", "listing_sessions_lower_bound",
                      "ext_market_regime", "ext_industry_regime"],
            "exit": ["close", "ma20", "ma60"],
        },
        "entry_description": "完整日线收盘高于前窗最高价与 MA60，MA20 高于 MA60，量比达到阈值。",
        "exit_description": "完整日线收盘低于 MA60 或 MA20 不再高于 MA60。",
        "risk_clocks": {
            "stop_loss": "新鲜原始报价复核持仓保护，触发价按实际成交和 stop_loss_pct 确定。",
            "max_hold": "从实际建仓计算 max_hold_bars 个完整交易日。",
        },
        "scoring_scope": "entry_candidates",
    },
    "scoring": {"momentum_20d": 1.0},
    "order_by": "score",
}

EXECUTION_BACKEND = "matrix_native"
BASIC_FILTER = {"enabled": False}
ENTRY_SIGNALS = ["signal_quant_daily_ohlc_breakout"]
EXIT_SIGNALS = ["signal_quant_daily_trend_invalid"]


class DailySwingBreakout:
    def required_fields(self):
        return frozenset({"high", "close", "volume", "amount", "is_st", "listing_sessions", "listing_sessions_lower_bound",
                          "ext_market_regime", "ext_industry_regime"})

    def required_warmup_bars(self, params):
        return max(60, _integer(params, "lookback", 60) + 1)

    def compute_signals(self, market, params):
        lookback = _integer(params, "lookback", 60)
        min_volume_ratio = _number(params, "min_volume_ratio", 1.3)
        minimum_listing = _integer(params, "minimum_listing_sessions")
        listing_known, listing_enough = listing_minimum(market, minimum_listing)
        minimum_amount = _number(params, "minimum_amount")
        _number(params, "stop_loss_pct", maximum=1.0)
        _integer(params, "max_hold_bars")

        present = np.isfinite(market.close) & (market.close > 0)
        observed = np.isfinite(market.close) | np.isfinite(market.open) | np.isfinite(market.high)
        observed_index = _build_valid_bar_index(observed)
        high_valid = present & np.isfinite(market.high) & (market.high >= market.close)
        # Keep bad high cells on the same bar axis so the next window cannot skip them.
        highs = valid_rolling_max(np.where(high_valid, market.high, 0), observed, lookback, bar_index=observed_index)
        coverage = valid_rolling_mean(high_valid.astype(np.float32), observed, lookback, bar_index=observed_index)
        prior_high = valid_shift(np.nan_to_num(highs, nan=0.0), 1, observed, bar_index=observed_index)
        high_ready = valid_shift(np.nan_to_num(coverage, nan=0.0), 1, observed, bar_index=observed_index) == 1
        traded = present & np.isfinite(market.volume) & (market.volume > 0)
        mean_volume = valid_rolling_mean(market.volume, traded, 5)
        prior_volume = valid_shift(mean_volume, 1, traded)

        ma20 = matrix_feature(market, "ma20")
        ma60 = matrix_feature(market, "ma60")
        market_regime = market.field("ext_market_regime")
        industry_regime = market.field("ext_industry_regime")
        exit_ready = present & np.isfinite(ma20) & np.isfinite(ma60)
        valid_regimes = ((market_regime == 0) | (market_regime == 1)) & (
            (industry_regime == 0) | (industry_regime == 1)
        )
        entry_ready = exit_ready & high_ready & np.isfinite(prior_volume) & valid_regimes
        entry_ready &= listing_known
        entry = entry_ready & (market.close > prior_high) & (market.close > ma60) & (ma20 > ma60)
        entry &= market.volume >= (prior_volume * min_volume_ratio)
        entry &= (market_regime == 1) & (industry_regime == 1) & (market.field("is_st") == 0)
        entry &= listing_enough
        entry &= market.field("amount") >= minimum_amount
        exit_ = exit_ready & ((market.close < ma60) | (ma20 <= ma60))
        return make_signal_matrix(
            market.shape, entry=entry, exit=exit_,
            entry_available=entry_ready, exit_available=exit_ready,
            entry_reason=entry_reason_with_listing(entry_ready, listing_known, "prior_window_or_regime_unavailable"),
            exit_reason=np.where(exit_ready, "", "trend_window_unavailable"),
            entry_signal_code=np.where(entry, 0, -1), exit_signal_code=np.where(exit_, 0, -1),
            entry_signal_ids=tuple(ENTRY_SIGNALS), exit_signal_ids=tuple(EXIT_SIGNALS),
        )


def _integer(params, name, default=None):
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 500:
        raise ValueError(f"{name}: supply an integer between 1 and 500")
    return value


def _number(params, name, default=None, *, maximum=None):
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not np.isfinite(value) or value <= 0:
        raise ValueError(f"{name}: supply a positive finite number")
    if maximum is not None and value >= maximum:
        raise ValueError(f"{name}: must be under {maximum}")
    return value


MATRIX_STRATEGY = DailySwingBreakout()
