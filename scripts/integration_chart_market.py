"""Loopback market routes for isolated Android integration."""
import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys
import time


def configure_market_root(root):
    os.environ["DATA_DIR"] = str(root)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "im-service" / "backend"))


def serve(root, port):
    root.mkdir()
    configure_market_root(root)

    from fastapi import FastAPI
    import polars as pl
    import uvicorn
    from app.api.tickdesk import router
    from app.data_providers import custom
    from app.services import preferences
    from app.services.tickdesk_adapter import CN, timestamp
    from app.services.tickdesk_market_data import (
        ChartPreparation, _completed_through, _run_chart_preparation, refresh_research_data,
    )
    from app.services.tickdesk_quote_feed import QuoteFeed
    from app.tickflow.repository import DataStore, KlineRepository

    first, second = "300502.SZ", "600000.SH"
    instruments = [
        dict(symbol=first, code="300502", name="新易盛", exchange="SZSE", region="CN", type="stock", board="创业板", status="listed"),
        dict(symbol=second, code="600000", name="浦发银行", exchange="SSE", region="CN", type="stock", board="沪主板", status="listed"),
    ]

    class Provider:
        @staticmethod
        def get_research_daily(symbols, start, end):
            if second in symbols:
                time.sleep(3)  # The first detail read exposes the real preparation gate.
            now = datetime.now(CN)
            sessions = [start.date() + timedelta(days=offset)
                        for offset in range((now.date() - start.date()).days + 1)
                        if (start.date() + timedelta(days=offset)).weekday() < 5]
            bars = []
            factors = []
            trading_days = [day for day in sessions if day <= end.date()]
            for symbol in symbols:
                for index, day in enumerate(trading_days):
                    price = (12 if symbol == first else 32) + (index % 10) / 4
                    bars.append(dict(date=day, symbol=symbol, open=price, high=price + 1, low=price - 1,
                                     close=price + .25, amount=price * 1000, volume=1000))
                factors.append(dict(symbol=symbol, trade_date=trading_days[-1], ex_factor=2.0))
            return dict(daily=pl.DataFrame(bars), factors=pl.DataFrame(factors), instruments=instruments,
                        sessions=sessions, source="隔离行情夹具", observed_at=now)

    config = root / "user_data"
    config.mkdir(exist_ok=True)
    (config / "preferences.json").write_text(json.dumps({
        "daily_data_provider": "tickflow", "realtime_data_provider": "tickflow", "realtime_quotes_enabled": False,
    }), encoding="utf-8")
    custom.get_provider = lambda _name: Provider()

    @asynccontextmanager
    async def lifespan(app):
        store = DataStore(root)
        app.state.repo = repo = KlineRepository(store)
        prepared = refresh_research_data(repo, chart_symbols=[first])
        assert prepared["symbols"] == [first] and prepared["dailyRows"] > 140
        print(f"FIXTURE_CHART_READY {first} rows={prepared['dailyRows']}", flush=True)
        demand = app.state.tickdesk_chart_preparation = ChartPreparation()
        now = datetime.now(CN)
        demand.attempts[first] = (time.monotonic(), _completed_through(now), True)
        app.state.tickdesk_daily_lock = asyncio.Lock()
        feed = app.state.tickdesk_quote_feed = QuoteFeed(root)
        feed.source = preferences.get_realtime_data_provider()
        feed.stocks[first] = dict(name="隔离行情夹具", price="12.25", market_timestamp=timestamp(now), timeBasis="snapshot")
        feed.stock_delivery = "rate_limited"
        worker = asyncio.create_task(_run_chart_preparation(app, demand))
        try:
            yield
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
            store.db.close()

    api = FastAPI(lifespan=lifespan)
    api.include_router(router)
    uvicorn.run(api, host="127.0.0.1", port=port, proxy_headers=False, access_log=False)


def serve_history_archive(root, port):
    configure_market_root(root)

    import polars as pl
    import uvicorn
    from fastapi import FastAPI
    from app.api.tickdesk import router
    from app.services.tickdesk_daily_history import coverage_diagnostic

    market = root / "tickdesk-market"
    state = json.loads((market / "history-coverage.json").read_text(encoding="utf-8"))
    cells = pl.read_parquet(market / "history-cells.parquet")
    assert cells.height == state["rows"] == cells.unique(subset=["date", "symbol"]).height
    assert cells["symbol"].n_unique() == state["observedSymbols"]
    assert {day.isoformat(): count for day, count in cells.group_by("date").len().iter_rows()} == {
        day: row["observed"] for day, row in state["dates"].items() if row["observed"]
    }
    latest = pl.read_parquet(root / f"kline_daily/date={state['through']}/part.parquet", columns=["symbol"])
    assert set(cells.filter(pl.col("date").cast(pl.String) == state["through"])["symbol"]) <= set(latest["symbol"])
    print(json.dumps({"archiveDiagnostic": coverage_diagnostic(state)}), flush=True)

    api = FastAPI()
    api.include_router(router)
    # Only the quote cycle's empty scope is read. The isolated host starts no market workers.
    api.state.repo = None
    api.state.tickdesk_history = state
    uvicorn.run(api, host="127.0.0.1", port=port, proxy_headers=False, access_log=False)


if __name__ == "__main__":
    if sys.argv[1] == "--history-archive":
        serve_history_archive(Path(sys.argv[2]), int(sys.argv[3]))
    else:
        serve(Path(sys.argv[1]), int(sys.argv[2]))
