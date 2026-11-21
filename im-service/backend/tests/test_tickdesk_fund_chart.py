import json
from datetime import datetime
from types import SimpleNamespace

import polars as pl

from app.services.tickdesk_adapter import CN, MarketAdapter, timestamp


def test_sourced_etf_chart_preserves_forward_only_basis_and_completed_session(tmp_path):
    now = timestamp(datetime(2026, 11, 16, 16, tzinfo=CN))
    root = tmp_path / "tickdesk-market"
    charts = root / "etf-charts"
    charts.mkdir(parents=True)
    pl.DataFrame([{"symbol": "510300.SH", "name": "示例ETF", "exchange": "SH",
                   "source": "fuyao", "as_of": now - 1, "status": "unknown"}]).write_parquet(root / "fund-master.parquet")
    (root / "calendar.json").write_text(json.dumps({
        "through": "2026-11-16", "observedAt": now - 1,
        "sessions": ["2026-11-13", "2026-11-16"],
    }))
    (charts / "510300.SH.json").write_text(json.dumps({
        "symbol": "510300.SH", "source": "fuyao", "adjustment": "forward", "observedAt": now - 1,
        "rows": [
            {"date": "2026-11-13", "open": 4.0, "high": 4.2, "low": 3.9, "close": 4.1, "volume": 1000, "amount": 4000},
            {"date": "2026-11-16", "open": 4.1, "high": 4.3, "low": 4.0, "close": 4.2, "volume": 1200, "amount": 5040},
        ],
    }))
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           get_instruments_asset=lambda asset: pl.DataFrame(),
                           get_daily_asset=lambda *args: pl.DataFrame())
    api = MarketAdapter(repo, None)
    symbol = api.symbols(now)[0]
    assert symbol["symbol"] == "510300.SH" and symbol["instrument"] == "etf"
    assert symbol["source"]["name"] == "fuyao" and symbol["supported"] is False
    raw = api.chart({"symbol": "510300.SH", "adjustment": "none"}, now)
    assert raw["dataStatus"] == "missing" and raw["candles"] == []
    newest = api.chart({"symbol": "510300.SH", "adjustment": "forward", "limit": 1}, now)
    assert newest["dataStatus"] == "fresh"
    assert newest["candles"][0]["close"] == "4.2"
    assert newest["candles"][0]["volume"] == 1200
    assert newest["asOf"] == timestamp(datetime(2026, 11, 16, 15, tzinfo=CN))
    assert newest["source"]["asOf"] == now - 1
    older = api.chart({"symbol": "510300.SH", "adjustment": "forward", "beforeDate": newest["nextBeforeDate"]}, now)
    assert older["dataStatus"] == "fresh" and older["candles"][0]["date"] == "2026-11-13"
    raw_archive = charts / "510300.SH.json"
    archive = json.loads(raw_archive.read_text())
    archive["rows"].pop()
    raw_archive.write_text(json.dumps(archive))
    pending = api.chart({"symbol": "510300.SH", "adjustment": "forward"}, now)
    assert pending["dataStatus"] == "stale" and pending["candles"][-1]["date"] == "2026-11-13"
    assert pending["source"]["name"] == "fuyao"
    raw_archive.unlink()
    missing = api.chart({"symbol": "510300.SH", "adjustment": "forward"}, now)
    assert missing["dataStatus"] == "missing" and missing["source"] is None
