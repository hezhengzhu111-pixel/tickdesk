"""日线趋势回调研究基线。一次运行只接受一种恢复确认。"""

import numpy as np

from app.backtest.matrix import (
    _build_valid_bar_index,
    entry_reason_with_listing,
    listing_minimum,
    make_signal_matrix,
    matrix_feature,
    valid_shift,
)

META = {
    "id": "custom_quant_swing_pullback",
    "name": "日线趋势回调研究",
    "description": "多头趋势中发生收盘回撤后，用预先选定的恢复条件确认。",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "research_only": True,
    "parameter_version": "daily-swing-pullback-v2",
    "entry_invalidation": "回撤、恢复确认或趋势条件不能成立时，入场观察失效。",
    "params": {
        "maximum_pullback_ratio": {"type": "float", "default": None},
        "confirmation": {"type": "string", "default": None},
        "minimum_listing_sessions": {"type": "int", "default": None},
        "minimum_amount": {"type": "float", "default": None},
        "stop_loss_pct": {"type": "float", "default": None},
        "max_hold_bars": {"type": "int", "default": None},
    },
    "quant_contract": {
        "version": 3, "group": "daily_swing", "family": "trend_pullback",
        "decision_timeframe": "1d", "price_basis": "forward_adjusted",
        "required_fields": {
            "entry": ["close", "ma20", "ma60", "volume", "amount", "is_st", "listing_sessions_lower_bound",
                      "ext_market_regime", "ext_industry_regime"],
            "exit": ["close", "ma20", "ma60"],
        },
        "entry_description": "前一完整收盘低于 MA20 且在配置回撤范围内，随后站回 MA20，"
                             "或按所选独立方案要求站回 MA20 且形成收盘突破前高的反转阳线。",
        "exit_description": "完整日线收盘低于 MA20 或 MA20 不再高于 MA60。",
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
ENTRY_SIGNALS = ["signal_quant_pullback_recovery"]
EXIT_SIGNALS = ["signal_quant_pullback_trend_invalid"]


class DailySwingPullback:
    def required_fields(self):
        return frozenset({"open", "high", "close", "volume", "amount", "is_st", "listing_sessions", "listing_sessions_lower_bound",
                          "ext_market_regime", "ext_industry_regime"})

    def required_warmup_bars(self, params):
        return 60

    def compute_signals(self, market, params):
        maximum_pullback = _number(params, "maximum_pullback_ratio", maximum=1.0)
        confirmation = params.get("confirmation")
        if confirmation not in ("close_reclaim", "reversal_bar"):
            raise ValueError("confirmation: choose close_reclaim or reversal_bar")
        minimum_listing = _integer(params, "minimum_listing_sessions")
        listing_known, listing_enough = listing_minimum(market, minimum_listing)
        minimum_amount = _number(params, "minimum_amount")
        _number(params, "stop_loss_pct", maximum=1.0)
        _integer(params, "max_hold_bars")

        present = np.isfinite(market.close) & (market.close > 0)
        observed = np.isfinite(market.close) | np.isfinite(market.open) | np.isfinite(market.high)
        observed_index = _build_valid_bar_index(observed)
        ma20 = matrix_feature(market, "ma20")
        ma60 = matrix_feature(market, "ma60")
        prior_close = valid_shift(np.where(present, market.close, 0), 1, observed, bar_index=observed_index)
        prior_ma20 = valid_shift(np.nan_to_num(ma20, nan=0.0), 1, observed, bar_index=observed_index)
        market_regime = market.field("ext_market_regime")
        industry_regime = market.field("ext_industry_regime")

        exit_ready = present & np.isfinite(ma20) & np.isfinite(ma60)
        entry_ready = exit_ready & (prior_close > 0) & (prior_ma20 > 0)
        entry_ready &= listing_known
        entry_ready &= ((market_regime == 0) | (market_regime == 1)) & (
            (industry_regime == 0) | (industry_regime == 1)
        )
        recovered = market.close > ma20
        if confirmation == "reversal_bar":
            valid_high = np.isfinite(market.high) & (market.high >= market.close)
            prior_high = valid_shift(np.where(valid_high, market.high, 0), 1, observed, bar_index=observed_index)
            entry_ready &= (prior_high > 0) & np.isfinite(market.open) & (market.open > 0)
            recovered &= (market.close > market.open) & (market.close > prior_high)

        pulled_back = (prior_close < prior_ma20) & (prior_close >= prior_ma20 * (1 - maximum_pullback))
        entry = entry_ready & pulled_back & recovered & (ma20 > ma60) & (market.close > ma60)
        entry &= (market_regime == 1) & (industry_regime == 1) & (market.field("is_st") == 0)
        entry &= (listing_enough) & (market.volume > 0)
        entry &= market.field("amount") >= minimum_amount
        exit_ = exit_ready & ((market.close < ma20) | (ma20 <= ma60))
        return make_signal_matrix(
            market.shape, entry=entry, exit=exit_,
            entry_available=entry_ready, exit_available=exit_ready,
            entry_reason=entry_reason_with_listing(entry_ready, listing_known, "recovery_window_or_regime_unavailable"),
            exit_reason=np.where(exit_ready, "", "trend_window_unavailable"),
            entry_signal_code=np.where(entry, 0, -1), exit_signal_code=np.where(exit_, 0, -1),
            entry_signal_ids=tuple(ENTRY_SIGNALS), exit_signal_ids=tuple(EXIT_SIGNALS),
        )


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


MATRIX_STRATEGY = DailySwingPullback()
