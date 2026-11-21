"""日线压缩突破研究基线。压缩和参考区间都在突破当根之前结束。"""

import numpy as np

from app.backtest.matrix import (
    _build_valid_bar_index,
    entry_reason_with_listing,
    listing_minimum,
    make_signal_matrix,
    matrix_feature,
    valid_rolling_max,
    valid_rolling_mean,
    valid_rolling_min,
    valid_shift,
)

META = {
    "id": "custom_quant_swing_compression",
    "name": "日线波动压缩突破研究",
    "description": "最新完整收盘突破先前窄区间，压缩相对更长区间成立且放量。",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "research_only": True,
    "parameter_version": "daily-swing-compression-v2",
    "entry_invalidation": "前窗压缩、放量或突破条件失效，当前执行条件须另行复核。",
    "params": {
        "compression_bars": {"type": "int", "default": None},
        "comparison_bars": {"type": "int", "default": None},
        "maximum_compression_ratio": {"type": "float", "default": None},
        "minimum_volume_ratio": {"type": "float", "default": None},
        "minimum_listing_sessions": {"type": "int", "default": None},
        "minimum_amount": {"type": "float", "default": None},
        "stop_loss_pct": {"type": "float", "default": None},
        "max_hold_bars": {"type": "int", "default": None},
    },
    "quant_contract": {
        "version": 3, "group": "daily_swing", "family": "compression_breakout",
        "decision_timeframe": "1d", "price_basis": "forward_adjusted",
        "required_fields": {
            "entry": ["close", "volume", "amount", "is_st", "listing_sessions_lower_bound",
                      "ext_market_regime", "ext_industry_regime"],
            "exit": ["close", "ma20"],
        },
        "entry_description": "前短窗口振幅相对前长窗口压缩，两个振幅各用本窗口高低点中点计算；"
                             "完整收盘突破短窗高点且量比达标。",
        "exit_description": "完整日线收盘低于 MA20。",
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
ENTRY_SIGNALS = ["signal_quant_daily_compressed_breakout"]
EXIT_SIGNALS = ["signal_quant_compression_trend_break"]


class DailySwingCompression:
    def required_fields(self):
        return frozenset({"high", "low", "close", "volume", "amount", "is_st", "listing_sessions", "listing_sessions_lower_bound",
                          "ext_market_regime", "ext_industry_regime"})

    def required_warmup_bars(self, params):
        return max(21, _integer(params, "comparison_bars") + 1)

    def compute_signals(self, market, params):
        short = _integer(params, "compression_bars")
        long = _integer(params, "comparison_bars")
        if short >= long:
            raise ValueError("compression_bars must be below comparison_bars")
        maximum_ratio = _number(params, "maximum_compression_ratio", maximum=1.0)
        minimum_volume_ratio = _number(params, "minimum_volume_ratio")
        minimum_listing = _integer(params, "minimum_listing_sessions")
        listing_known, listing_enough = listing_minimum(market, minimum_listing)
        minimum_amount = _number(params, "minimum_amount")
        _number(params, "stop_loss_pct", maximum=1.0)
        _integer(params, "max_hold_bars")

        present = np.isfinite(market.close) & (market.close > 0)
        observed = np.isfinite(market.close) | np.isfinite(market.high) | np.isfinite(market.low)
        observed_index = _build_valid_bar_index(observed)
        valid = np.isfinite(market.high) & np.isfinite(market.low) & (market.low > 0)
        valid &= present & (market.low <= market.close) & (market.high >= market.close)
        short_high, short_width = _prior_window(market, observed, observed_index, valid, short)
        _, long_width = _prior_window(market, observed, observed_index, valid, long)
        traded = present & np.isfinite(market.volume) & (market.volume > 0)
        avg_volume = valid_rolling_mean(market.volume, traded, 5)
        prior_volume = valid_shift(avg_volume, 1, traded)

        market_regime = market.field("ext_market_regime")
        industry_regime = market.field("ext_industry_regime")
        entry_ready = present & np.isfinite(short_width) & np.isfinite(long_width) & np.isfinite(prior_volume)
        entry_ready &= ((market_regime == 0) | (market_regime == 1)) & (
            (industry_regime == 0) | (industry_regime == 1)
        )
        entry_ready &= listing_known
        entry = entry_ready & (long_width > 0) & (short_width <= long_width * maximum_ratio)
        entry &= (market.close > short_high) & (market.volume >= prior_volume * minimum_volume_ratio)
        entry &= (market_regime == 1) & (industry_regime == 1) & (market.field("is_st") == 0)
        entry &= listing_enough
        entry &= market.field("amount") >= minimum_amount

        ma20 = matrix_feature(market, "ma20")
        exit_ready = present & np.isfinite(ma20)
        exit_ = exit_ready & (market.close < ma20)
        return make_signal_matrix(
            market.shape, entry=entry, exit=exit_,
            entry_available=entry_ready, exit_available=exit_ready,
            entry_reason=entry_reason_with_listing(entry_ready, listing_known, "compression_window_or_regime_unavailable"),
            exit_reason=np.where(exit_ready, "", "trend_window_unavailable"),
            entry_signal_code=np.where(entry, 0, -1), exit_signal_code=np.where(exit_, 0, -1),
            entry_signal_ids=tuple(ENTRY_SIGNALS), exit_signal_ids=tuple(EXIT_SIGNALS),
        )


def _prior_window(market, observed, index, valid, length):
    high = valid_rolling_max(np.where(valid, market.high, 0), observed, length, bar_index=index)
    low = valid_rolling_min(np.where(valid, market.low, 0), observed, length, bar_index=index)
    coverage = valid_rolling_mean(valid.astype(np.float32), observed, length, bar_index=index)
    prior_high = valid_shift(np.nan_to_num(high, nan=0.0), 1, observed, bar_index=index)
    prior_low = valid_shift(np.nan_to_num(low, nan=0.0), 1, observed, bar_index=index)
    ready = (valid_shift(np.nan_to_num(coverage, nan=0.0), 1, observed, bar_index=index) == 1) & (prior_low > 0)
    width = np.full(market.shape, np.nan, dtype=np.float32)
    np.divide(2 * (prior_high - prior_low), prior_high + prior_low, out=width, where=ready)
    return prior_high, width


def _integer(params, name):
    value = params.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 500:
        raise ValueError(f"{name}: supply an integer between 1 and 500")
    return value


def _number(params, name, *, maximum=None):
    value = params.get(name)
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not np.isfinite(value) or value <= 0:
        raise ValueError(f"{name}: supply a positive finite number")
    if maximum is not None and value >= maximum:
        raise ValueError(f"{name}: must be under {maximum}")
    return value


MATRIX_STRATEGY = DailySwingCompression()
