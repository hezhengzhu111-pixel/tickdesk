"""收盘触及前窗低点，同时零下 DIF 高于固定间隔前的值。"""

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    _build_valid_bar_index,
    make_signal_matrix,
    matrix_feature,
    valid_rolling_mean,
    valid_rolling_min,
)
from app.backtest.matrix import (
    valid_shift as shift,
)

META = {
    "id": "macd_below_zero_revival",
    "name": "收盘低位与 DIF 零下回升",
    "description": "当日收盘不高于前窗最低收盘，DIF 为负且高于固定间隔前的值。",
    "tags": ["MACD", "收盘低位"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "low_window",
            "label": "新低回看天数",
            "type": "int",
            "default": 20,
            "min": 10,
            "max": 60,
            "step": 5,
        },
        {
            "id": "revive_days",
            "label": "DIF 回升对比天数",
            "type": "int",
            "default": 10,
            "min": 5,
            "max": 20,
            "step": 1,
        },
    ],
    "scoring": {"momentum_20d": 0.4, "change_pct": 0.3, "vol_ratio_5d": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_macd_below_zero_revival"]
EXIT_SIGNALS = ["signal_macd_dif_crosses_zero"]
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 20


class MACDBelowZeroRevivalMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        return int(params.get("low_window", 20)) + 40

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        win = max(5, int(params.get("low_window", 20)))
        revive = max(3, int(params.get("revive_days", 10)))
        dif = matrix_feature(market, "macd_dif")

        valid_close = np.isfinite(market.close) & (market.close > 0)
        present = np.isfinite(market.close) | np.isfinite(market.open)
        index = _build_valid_bar_index(present)
        minimum = valid_rolling_min(np.where(valid_close, market.close, 0), present, win, bar_index=index)
        coverage = valid_rolling_mean(valid_close.astype(np.float32), present, win, bar_index=index)
        prior_min = shift(np.nan_to_num(minimum, nan=0.0), 1, present, bar_index=index)
        ready = shift(np.nan_to_num(coverage, nan=0.0), 1, present, bar_index=index) == 1
        new_low = ready & valid_close & (market.close <= prior_min)

        # 零下 + DIF 较 revive 日前抬升 (动能拒绝新低)
        entry = new_low & (dif < 0) & (dif > shift(dif, revive))

        # 出场: DIF 上穿零轴 (修复完成)
        exit_ = (dif > 0) & (shift(dif, 1) <= 0)
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_macd_below_zero_revival",),
            exit_signal_ids=tuple(EXIT_SIGNALS),
        )


MATRIX_STRATEGY = MACDBelowZeroRevivalMatrixStrategy()
