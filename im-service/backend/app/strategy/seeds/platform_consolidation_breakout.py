"""放量突破前窗区间高点，前窗振幅按其自身高低点中点计算。"""

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    _build_valid_bar_index,
    make_signal_matrix,
    matrix_feature,
    valid_rolling_max,
    valid_rolling_mean,
    valid_rolling_min,
)
from app.backtest.matrix import (
    valid_shift as shift,
)

META = {
    "id": "platform_consolidation_breakout",
    "name": "平台整理突破",
    "description": "前 N 根完整日线的高低振幅达标，当日收盘突破前窗高点且量比达标。",
    "tags": ["形态", "平台", "突破"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "platform_days",
            "label": "平台天数",
            "type": "int",
            "default": 10,
            "min": 5,
            "max": 30,
            "step": 1,
        },
        {
            "id": "range_pct_max",
            "label": "平台振幅上限%",
            "type": "float",
            "default": 8.0,
            "min": 3.0,
            "max": 20.0,
            "step": 0.5,
        },
        {
            "id": "vol_ratio_min",
            "label": "突破日最低量比",
            "type": "float",
            "default": 1.5,
            "min": 1.0,
            "max": 5.0,
            "step": 0.1,
        },
    ],
    "scoring": {"vol_ratio_5d": 0.4, "momentum_20d": 0.3, "change_pct": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_platform_breakout"]
EXIT_SIGNALS = ["signal_platform_fail_ma20"]
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 15


class PlatformConsolidationBreakoutMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"high", "low", "close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        return int(params.get("platform_days", 10)) + 10

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        days = max(4, int(params.get("platform_days", 10)))

        present = np.isfinite(market.close) | np.isfinite(market.high) | np.isfinite(market.low)
        index = _build_valid_bar_index(present)
        valid = np.isfinite(market.close) & (market.high >= market.close) & (market.low <= market.close)
        valid &= np.isfinite(market.high) & np.isfinite(market.low) & (market.low > 0)
        high = valid_rolling_max(np.where(valid, market.high, 0), present, days, bar_index=index)
        low = valid_rolling_min(np.where(valid, market.low, 0), present, days, bar_index=index)
        coverage = valid_rolling_mean(valid.astype(np.float32), present, days, bar_index=index)
        prior_high = shift(np.nan_to_num(high, nan=0.0), 1, present, bar_index=index)
        prior_low = shift(np.nan_to_num(low, nan=0.0), 1, present, bar_index=index)
        ready = (shift(np.nan_to_num(coverage, nan=0.0), 1, present, bar_index=index) == 1) & (prior_low > 0)
        range_pct = np.full(market.shape, np.nan, dtype=np.float32)
        np.divide(200 * (prior_high - prior_low), prior_high + prior_low, out=range_pct, where=ready)

        # 平台成立 + 今日放量突破平台上沿
        entry = range_pct <= float(params.get("range_pct_max", 8.0))
        entry &= market.close > prior_high
        entry &= matrix_feature(market, "vol_ratio_5d") >= float(params.get("vol_ratio_min", 1.5))

        exit_ = market.close < matrix_feature(market, "ma20")
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_platform_breakout",),
            exit_signal_ids=("signal_platform_fail_ma20",),
        )


MATRIX_STRATEGY = PlatformConsolidationBreakoutMatrixStrategy()
