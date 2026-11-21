"""Test-only synthetic minute kernel for the isolated four-component chain."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import polars as pl

from app.services.tickdesk_adapter import CN, MarketAdapter, timestamp


class Repo:
    def __init__(self, day):
        base = datetime.combine(day, datetime.min.time(), tzinfo=CN).replace(hour=9, minute=30)
        rows = []
        for index in range(29):
            rows.append({
                "datetime": base + timedelta(minutes=index),
                "open": Decimal("10") + Decimal(index) / 100,
                "high": Decimal("10.1") + Decimal(index) / 100,
                "low": Decimal("9.9") + Decimal(index) / 100,
                "close": Decimal("10.05") + Decimal(index) / 100,
                "volume": 100 + index,
                "amount": 1000 + index,
            })
        # A conflicting duplicate minute must not become a complete 1m bar.
        duplicate = dict(rows[3])
        duplicate["close"] = Decimal("10.50")
        duplicate["high"] = Decimal("10.60")
        rows.append(duplicate)
        self.rows = rows

    def get_minute_range(self, symbols, start, end, asset_type=None):
        return pl.DataFrame(self.rows)


class FixtureAdapter(MarketAdapter):
    def __init__(self, day):
        super().__init__(Repo(day), None)

    def symbols(self, now):
        return [{"symbol": "300502.SZ", "name": "minute fixture", "instrument": "stock",
                 "supported": True, "board": "创业板", "exchange": "SZSE",
                 "source": None}]


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        data = body.get("data") or {}
        now = body.get("requestedAt") or timestamp(datetime.now(CN))
        if (self.path != "/api/tickdesk/prepare" or data.get("cmd") != "get_chart"
                or data.get("timeframe") not in ("1m", "5m")):
            # The fixture only owns raw minute storage. The initial 1d read must
            # fail cleanly instead of calling a daily calendar the stub lacks.
            response = {"error": "DATA_UNAVAILABLE"}
        else:
            day = datetime.fromtimestamp(now / 1000, CN).date()
            chart = FixtureAdapter(day).chart(data, now)
            if chart.get("cursorExpired"):
                chart.pop("cursorExpired", None)
                response = {"error": "CHART_CURSOR_EXPIRED"}
            else:
                response = {"chart": chart, "quotes": []}
        raw = json.dumps(response, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
