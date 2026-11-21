"""Serve the real adapter/worker with a newly seeded synthetic repository."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import polars as pl
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_tickdesk_holding_rules import holding_evidence_row

from app.api import tickdesk as tickdesk_api
from app.api.tickdesk import router
from app.financial_history import merge_report_revisions
from app.services.tickdesk_adapter import MarketAdapter, fingerprint
from app.strategy.engine import StrategyEngine
from app.tickflow.repository import DataStore, KlineRepository

SYMBOLS = [f"{number:06}.SZ" for number in range(1, 5)]
DAY = date(2026, 1, 2)
NEXT_DAY = date(2026, 1, 5)
CUTOFF_MS = 1767342600000
STRATEGY_ID = "synthetic_pit"
SOURCE = "synthetic-quant-fixture"
BAR_COLUMNS = ("open", "high", "low", "close", "raw_high", "raw_low", "raw_close", "volume", "amount")
STRATEGY = '''
from app.backtest.matrix import make_signal_matrix

META = {
    "id": "synthetic_pit", "name": "Synthetic PIT", "description": "Synthetic research",
    "execution_backend": "matrix_native", "research_only": True,
    "scoring": {"roe_latest": 1},
    "quant_contract": {
        "version": 3, "group": "daily_swing", "family": "synthetic",
        "decision_timeframe": "1d", "required_fields": {
            "entry": ["roe_latest"], "exit": ["raw_close"],
        },
        "price_basis": "raw", "entry_description": "ROE > 0", "exit_description": "raw close < 10",
        "risk_clocks": {"stop_loss": "1d"},
    },
}
EXECUTION_BACKEND = "matrix_native"
BASIC_FILTER = {"enabled": False}

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
'''


def build_fixture(root: Path) -> FastAPI:
    root.mkdir()
    strategies = root / "strategies"
    strategies.mkdir()
    (strategies / f"{STRATEGY_ID}.py").write_text(STRATEGY, encoding="utf-8")
    (root / "tickdesk_research.json").write_text(json.dumps({
        "version": 3, "strategies": [STRATEGY_ID], "symbols": SYMBOLS,
    }), encoding="utf-8")
    rows = []
    evidence = []
    for day in (DAY - timedelta(days=2), DAY, NEXT_DAY):
        for i, symbol in enumerate(SYMBOLS):
            price = 30.0 if day == NEXT_DAY else (9.0 if i == 3 else 11.0)
            row = {
                "date": day, "symbol": symbol, "open": price, "high": price + 1.0,
                "low": price - 1.0, "close": price, "raw_high": price + 1.0,
                "raw_low": price - 1.0, "raw_close": price, "volume": 1000,
                "amount": price * 1000, "turnover_rate": 1.0,
                "consecutive_limit_ups": 0, "consecutive_limit_downs": 0,
            }
            rows.append(row)
            if day == NEXT_DAY:
                continue
            evidence.append({
                "symbol": symbol, "field": "daily_bar_finality", "source": SOURCE,
                "effective_date": str(day), "available_at": f"{day}T15:20:00+08:00",
                "value": {
                    "end_at": f"{day}T15:00:00+08:00",
                    "bar_hash": fingerprint([str(day), symbol, *[float(row[col]) for col in BAR_COLUMNS]]),
                },
            })
        directory = root / "kline_daily_enriched" / f"date={day}"
        directory.mkdir(parents=True)
        pl.DataFrame(rows[-len(SYMBOLS):]).write_parquet(directory / "part.parquet")
    instruments = root / "instruments"
    instruments.mkdir()
    pl.DataFrame([{
        "symbol": symbol, "name": f"Fixture {symbol}", "exchange": "SZSE", "code": symbol[:6],
        "board": "深主板", "status": "listed", "as_of": str(DAY), "source": SOURCE,
        "float_shares": 100000.0, "total_shares": 200000.0,
    } for symbol in SYMBOLS]).write_parquet(instruments / "part.parquet")
    financials = root / "financials" / "metrics"
    financials.mkdir(parents=True)
    reports = pl.DataFrame({
        "symbol": SYMBOLS[:3], "period_end": ["2025-09-30"] * 3,
        "announce_date": ["2025-11-01"] * 3, "roe": [1.0, 2.0, -1.0],
        "_financial_source": [SOURCE] * 3,
        "_financial_observed_at": ["2025-11-02T02:00:00Z"] * 3,
    })
    merge_report_revisions(reports).write_parquet(financials / "part.parquet")
    api = FastAPI()
    api.include_router(router)
    api.state.repo = KlineRepository(DataStore.readonly(root))
    evidence.extend(
        holding_evidence_row(symbol, day=str(day))
        for symbol in (SYMBOLS[0], SYMBOLS[3]) for day in (DAY, NEXT_DAY)
    )
    # Seed via the same archive writer a normalized provider can call.
    try:
        api.state.repo.write_quant_reference_evidence(evidence)
    except BaseException:
        api.state.repo.store.db.close()
        raise
    api.state.strategy_engine = StrategyEngine([strategies])
    return api


def install_scan_calendar(set_attribute, *, integration_marker=False):
    """Enable the fixture's two synthetic sessions independently of the production schedule."""

    class FixtureAdapter(MarketAdapter):
        def __init__(self, *args, **kwargs):
            # 16:30 exercises same-day bars after their synthetic 15:20 observation.
            super().__init__(*args, **kwargs, calendar=lambda day: day in (DAY, NEXT_DAY))

        def prepare(self, request):
            response = super().prepare(request)
            if integration_marker and request["data"]["cmd"] in ("scan_now", "resume"):
                response["fixture"] = "tickdesk-integration-v1"
            return response

    set_attribute(tickdesk_api, "MarketAdapter", FixtureAdapter)


def main():
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    install_scan_calendar(setattr, integration_marker=True)
    uvicorn.run(build_fixture(args.data_dir), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
