"""Source-backed quote snapshots and independent feed and trading-session status."""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, time

from app.data_providers import custom
from app.plugins.fuyao.client import FuyaoError
from app.services import preferences
from app.services.tickdesk_adapter import CN, timestamp
from app.services.tickdesk_fund_chart import read_fund_master

logger = logging.getLogger(__name__)
_FUND_BATCH = 100
_QUOTE_WINDOWS = ((time(9, 15), time(11, 30)), (time(13), time(15)))


def _quote_timing(root, at):
    from app.services.tickdesk_market_data import calendar_verdict

    verdict = calendar_verdict(root, at)
    if verdict is None:
        return "unknown", None
    if verdict:
        local = datetime.fromtimestamp(at / 1000, CN)
        wall = local.time()
        for opening, closing in _QUOTE_WINDOWS:
            if wall < opening:
                return "closed", (datetime.combine(local.date(), opening, CN) - local).total_seconds()
            if wall < closing:
                return "open", None
    return "closed", None


def quote_session(root, at):
    return _quote_timing(root, at)[0]


def _sample(source, symbols=None):
    try:
        if not custom.provider_has_dataset(source, "realtime"):
            return "unavailable", {}
        stamp = preferences.data_source_stamp()
        provider = custom.get_provider(source)
        if symbols is None:
            read = getattr(provider, "get_tickdesk_realtime", None) or provider.get_realtime
            rows = read()
        else:
            read = getattr(provider, "get_fund_realtime", None)
            if not callable(read):
                return "unavailable", {}
            rows = read(symbols)
    except FuyaoError as error:
        if str(error.code) in {"429", "4001"}:
            return "rate_limited", {}
        if error.code == "NETWORK_UNAVAILABLE":
            return "offline", {}
        if str(error.code) == "3002" and symbols is not None:
            return "missing", {}
        return "unavailable", {}
    except Exception as error:  # noqa: BLE001 -- optional supplier isolation
        logger.warning("TickDesk quote refresh failed (%s)", type(error).__name__)
        return "unavailable", {}
    if stamp != preferences.data_source_stamp():
        return "unknown", {}
    if not isinstance(rows, list):
        return "unavailable", {}
    result = {}
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("symbol"), str)
                or type(row.get("market_timestamp")) is not int or row["market_timestamp"] <= 0
                or row.get("last_price") is None):
            continue
        quote = {"name": source, "price": row["last_price"], "market_timestamp": row["market_timestamp"],
                 "timeBasis": "snapshot" if source == "fuyao" else None}
        if source == "fuyao" and symbols is None and "snapshot_amount" in row:
            quote["snapshot_amount"] = row["snapshot_amount"]
        result[row["symbol"]] = quote
    return ("available" if result else "missing"), result


class QuoteFeed:
    def __init__(self, root):
        self.root = root
        self.source = None
        self.stocks = {}
        self.stock_delivery = "unknown"
        self.funds = {}
        self.fund_delivery = {}
        self.fund_symbols = set()
        self.scope = set()
        self.requested = set()
        self.wake = asyncio.Event()

    def update(self, scope, requested):
        if scope != self.scope or (requested and requested != self.requested):
            self.scope = scope
            if requested:
                self.requested = requested
            self.wake.set()

    def rows(self):
        return {**self.stocks, **self.funds} if self.source == preferences.get_realtime_data_provider() else {}

    def execution_rows(self) -> dict[str, dict]:
        # Current feeds supply reference prices only. Fuyao's snapshot clock is the
        # latest upstream clock in a page and cannot date an individual security's price or amount.
        return {}

    def diagnostics(self, symbols, delivered, at):
        delivery = {}
        matching_source = self.source == preferences.get_realtime_data_provider()
        for symbol in sorted(symbols):
            if not matching_source:
                state = "unknown"
            elif symbol in self.fund_symbols:
                state = self.fund_delivery.get(symbol, "unknown")
            else:
                state = self.stock_delivery
            delivery[symbol] = "missing" if state == "available" and symbol not in delivered else state
        return {"session": quote_session(self.root, at), "delivery": delivery}

    async def run(self):
        clock = asyncio.get_running_loop().time
        stock_due = fund_due = 0
        offset = 0
        previous_targets = set()
        while True:
            source = preferences.get_realtime_data_provider()
            if source != self.source:
                self.source = source
                self.stocks, self.funds, self.fund_delivery = {}, {}, {}
                self.stock_delivery = "unknown"
                stock_due = fund_due = 0
            observed = clock()
            now = timestamp(datetime.now(CN))
            session, opens_in = _quote_timing(self.root, now)
            delay = 30 if session == "open" else 300
            opening_due = observed + opens_in if opens_in is not None else None
            if clock() >= stock_due:
                self.stock_delivery, rows = await asyncio.to_thread(_sample, source)
                if rows:
                    self.stocks = rows
                stock_due = clock() + delay
                if opening_due is not None and self.stock_delivery != "rate_limited":
                    stock_due = min(stock_due, opening_due)
            try:
                master = await asyncio.to_thread(read_fund_master, self.root, now)
                if master:
                    self.fund_symbols = {item["symbol"] for item in master}
            except Exception as error:  # noqa: BLE001 -- catalog does not own stock availability
                logger.warning("TickDesk ETF quote identity catalog unavailable (%s)", type(error).__name__)
            wanted = (self.scope | self.requested) & self.fund_symbols
            if wanted != previous_targets:
                self.funds = {symbol: row for symbol, row in self.funds.items() if symbol in wanted}
                self.fund_delivery = {symbol: state for symbol, state in self.fund_delivery.items() if symbol in wanted}
                previous_targets = wanted
                fund_due = offset = 0
            if wanted and clock() >= fund_due:
                ordered = sorted(wanted)
                batch = ordered[offset:offset + _FUND_BATCH]
                outcome, rows = await asyncio.to_thread(_sample, source, batch)
                self.funds.update(rows)
                for symbol in batch:
                    self.fund_delivery[symbol] = "missing" if outcome == "available" and symbol not in rows else outcome
                offset = offset + len(batch) if offset + len(batch) < len(ordered) else 0
                fund_due = clock() + (delay if not offset or outcome == "rate_limited" else 5)
                if opening_due is not None and outcome != "rate_limited":
                    fund_due = min(fund_due, opening_due)
            wait = min(stock_due, fund_due) if wanted else stock_due
            with suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), max(0.2, wait - clock()))
            self.wake.clear()
