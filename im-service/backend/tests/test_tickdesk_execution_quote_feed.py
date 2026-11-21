from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.plugins.fuyao.client import FuyaoError
from app.plugins.fuyao.provider import FuyaoProvider
from app.services import preferences, tickdesk_quote_feed
from app.services.tickdesk_adapter import CN, MarketAdapter

NOW = 1794794400000  # 2026-11-16 10:00 Asia/Shanghai
SYMBOL = "600000.SH"


@pytest.mark.parametrize(("raw_turnover", "expected"), [
    (103241275.38, 103241275.38),
    (1400, 1400.0),
    (0, 0.0),
    (None, None),
    (True, None),
    ("12", None),
    (-1, None),
    (float("nan"), None),
    (float("inf"), None),
])
def test_fuyao_page_amount_remains_diagnostic_only(tmp_path, monkeypatch, raw_turnover, expected):
    raw = {"thscode": SYMBOL, "last_price": 10.77, "turnover": raw_turnover}
    monkeypatch.setattr(FuyaoProvider, "_get_client", lambda _: SimpleNamespace(snapshot_timed=lambda: [([raw], NOW)]))
    monkeypatch.setattr(tickdesk_quote_feed.custom, "provider_has_dataset", lambda *args: True)
    monkeypatch.setattr(tickdesk_quote_feed.custom, "get_provider", lambda _: FuyaoProvider())
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "fuyao")
    monkeypatch.setattr(preferences, "data_source_stamp", lambda: ("fixture",))

    feed = tickdesk_quote_feed.QuoteFeed(tmp_path)
    feed.source = "fuyao"
    feed.stock_delivery, feed.stocks = tickdesk_quote_feed._sample(feed.source)
    reference = feed.rows()[SYMBOL]
    assert feed.stock_delivery == "available"
    assert reference["market_timestamp"] == NOW and reference["timeBasis"] == "snapshot"
    if expected is None:
        assert "snapshot_amount" not in reference
    else:
        assert type(reference["snapshot_amount"]) is float and reference["snapshot_amount"] == expected
    assert feed.execution_rows() == {}
    assert MarketAdapter(None, None, quote_snapshot=feed.rows()).quotes([SYMBOL], NOW) == [{
        "symbol": SYMBOL, "price": "10.77", "asOf": NOW, "timeBasis": "snapshot",
        "source": {"name": "fuyao", "ref": f"kernel:realtime:{SYMBOL}", "asOf": NOW},
    }]


def test_provider_errors_switches_and_funds_do_not_produce_execution_rows(tmp_path, monkeypatch):
    def rate_limited():
        raise FuyaoError("rate limited", code=429)

    monkeypatch.setattr(tickdesk_quote_feed.custom, "provider_has_dataset", lambda *args: True)
    monkeypatch.setattr(tickdesk_quote_feed.custom, "get_provider", lambda _: SimpleNamespace(get_tickdesk_realtime=rate_limited))
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "fuyao")
    monkeypatch.setattr(preferences, "data_source_stamp", lambda: ("fixture",))
    feed = tickdesk_quote_feed.QuoteFeed(tmp_path)
    feed.source = "fuyao"
    feed.stocks[SYMBOL] = {"name": "fuyao", "snapshot_amount": 200.0, "market_timestamp": NOW, "price": 10.0}
    feed.funds["510300.SH"] = {"name": "fuyao", "market_timestamp": NOW, "price": 4.0}
    feed.stock_delivery, rows = tickdesk_quote_feed._sample(feed.source)
    assert rows == {} and feed.stock_delivery == "rate_limited"
    assert set(feed.rows()) == {SYMBOL, "510300.SH"} and feed.execution_rows() == {}

    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "other")
    assert feed.rows() == {} and feed.execution_rows() == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(("trading_day", "outcome", "next_poll_at"), [
    (True, "available", 10),
    (True, "rate_limited", 300),
    (False, "available", 300),
])
async def test_stock_and_fund_poll_resume_at_verified_afternoon_open(
    monkeypatch, tmp_path, trading_day, outcome, next_poll_at,
):
    fund = "510300.SH"
    morning_break = datetime(2026, 11, 17, 12, 59, 50, tzinfo=CN)
    clock = 0
    requests = []
    feed = tickdesk_quote_feed.QuoteFeed(tmp_path)
    feed.update({SYMBOL, fund}, set())
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "fuyao")
    monkeypatch.setattr("app.services.tickdesk_market_data.calendar_verdict", lambda *_: trading_day)
    monkeypatch.setattr(tickdesk_quote_feed, "read_fund_master", lambda *_: [{"symbol": fund}])
    monkeypatch.setattr(tickdesk_quote_feed, "datetime", SimpleNamespace(
        now=lambda _tz: morning_break + timedelta(seconds=clock),
        fromtimestamp=datetime.fromtimestamp,
        combine=datetime.combine,
    ))

    def sample(source, symbols=None):
        requests.append((clock, source, symbols))
        return outcome, {}

    class Done(Exception):
        pass

    async def timeout(event, delay):
        nonlocal clock
        event.close()
        if len(requests) == 4:
            raise Done
        clock += delay
        raise TimeoutError

    async def in_thread(fn, *args):
        return fn(*args)

    monkeypatch.setattr(tickdesk_quote_feed, "_sample", sample)
    monkeypatch.setattr(tickdesk_quote_feed, "asyncio", SimpleNamespace(
        get_running_loop=lambda: SimpleNamespace(time=lambda: clock),
        to_thread=in_thread,
        wait_for=timeout,
    ))
    with pytest.raises(Done):
        await feed.run()
    assert requests == [(0, "fuyao", None), (0, "fuyao", [fund]),
                        (next_poll_at, "fuyao", None), (next_poll_at, "fuyao", [fund])]
