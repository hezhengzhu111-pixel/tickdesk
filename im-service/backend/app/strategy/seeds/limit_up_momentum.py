"""当日封板计数大于零并达到涨幅要求，包含首板。"""

import numpy as np

from app.backtest.matrix import MarketDataMatrix, SignalMatrix, make_signal_matrix, matrix_feature

META = {
    "id": "limit_up_momentum",
    "name": "封板涨幅筛选",
    "description": "当日封板计数至少为 1（含首板），且达到涨幅门槛。",
    "tags": ["涨停", "涨幅"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {"id": "use_change_filter", "label": "启用涨幅过滤", "type": "bool", "default": True},
        {
            "id": "min_change",
            "label": "最低涨幅%",
            "type": "float",
            "default": 5.0,
            "min": 2.0,
            "max": 15.0,
            "step": 0.5,
        },
        {"id": "use_boards_filter", "label": "启用连板数过滤", "type": "bool", "default": True},
        {
            "id": "min_boards",
            "label": "最少连板",
            "type": "int",
            "default": 1,
            "min": 1,
            "max": 10,
            "step": 1,
        },
    ],
    "scoring": {"consecutive_limit_ups": 0.4, "change_pct": 0.3, "amount": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 50,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_limit_up"]
EXIT_SIGNALS = []
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 5


class LimitUpMomentumMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close", "consecutive_limit_ups"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 60

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        entry = np.ones(market.shape, dtype=bool)
        if params.get("use_change_filter", True):
            entry &= (
                matrix_feature(market, "change_pct") > float(params.get("min_change", 5.0)) / 100.0
            )
        if params.get("use_boards_filter", True):
            entry &= matrix_feature(market, "consecutive_limit_ups") >= int(
                params.get("min_boards", 1)
            )
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_limit_up",),
        )


MATRIX_STRATEGY = LimitUpMomentumMatrixStrategy()
