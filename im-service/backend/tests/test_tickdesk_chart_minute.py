from datetime import datetime, timedelta
from decimal import Decimal

import polars as pl

from app.services.tickdesk_adapter import MarketAdapter, timestamp

NOW = timestamp(datetime.fromisoformat("2026-01-06T10:00:00+08:00"))
SYMBOL = "300502.SZ"


class Repo:
    def __init__(self, rows):
        self.rows = rows

    def get_minute_range(self, symbols, start, end, asset_type=None):
        return pl.DataFrame(self.rows)


def rows(count, start_minute=30, day=5):
    base = datetime(2026, 1, day, 9, start_minute)
    return [
        {
            "datetime": base + timedelta(minutes=index),
            "open": Decimal("10") + Decimal(index) / 100,
            "high": Decimal("10.1") + Decimal(index) / 100,
            "low": Decimal("9.9") + Decimal(index) / 100,
            "close": Decimal("10.05") + Decimal(index) / 100,
            "volume": 100 + index,
            "amount": 1000 + index,
        }
        for index in range(count)
    ]


def adapter(count, **kwargs):
    return MarketAdapter(Repo(rows(count, **kwargs)), None)


def data(timeframe="1m", **fields):
    return {"symbol": SYMBOL, "adjustment": "none", "timeframe": timeframe, **fields}


def missing():
    return {"symbol": SYMBOL, "adjustment": "none", "dataStatus": "missing", "candles": [],
            "nextBeforeDate": None, "hasMore": False, "source": None, "asOf": None,
            "nextCursor": None, "timeframe": "1m"}


MASTER = {"symbol": SYMBOL, "instrument": "stock", "name": "测试"}


def test_minute_chart_defaults_to_latest_page_and_pages_backwards():
    result = adapter(10)._minute_chart(data(limit=4), NOW, MASTER, missing())
    assert len(result["candles"]) == 4
    assert result["hasMore"] is True
    assert result["nextCursor"].startswith("v2:")
    assert all(candle["complete"] is True for candle in result["candles"])
    starts = [candle["startAt"] for candle in result["candles"]]
    assert starts == sorted(starts)
    page2 = adapter(10)._minute_chart(
        data(limit=4, cursor=result["nextCursor"]), NOW, MASTER, missing()
    )
    assert len(page2["candles"]) == 4
    assert page2["candles"][-1]["startAt"] + 60_000 == result["candles"][0]["startAt"]
    page3 = adapter(10)._minute_chart(
        data(limit=4, cursor=page2["nextCursor"]), NOW, MASTER, missing()
    )
    assert [c["startAt"] for c in page3["candles"]] == [
        timestamp(datetime.fromisoformat("2026-01-05T09:30:00+08:00")),
        timestamp(datetime.fromisoformat("2026-01-05T09:31:00+08:00")),
    ]
    assert page3["hasMore"] is False


def test_five_minute_requires_five_distinct_consecutive_completed_minutes():
    result = adapter(8)._minute_chart(data("5m"), NOW, MASTER, missing())
    assert len(result["candles"]) == 2
    assert result["candles"][0]["complete"] is True
    assert result["candles"][1]["complete"] is False
    assert result["candles"][1]["quality"] == "incomplete_group"
    assert result["candles"][1]["volume"] == 100 + 5 + 100 + 6 + 100 + 7


def test_duplicate_minute_is_not_complete_and_does_not_get_double_counted():
    values = rows(4)
    duplicate = dict(values[1])
    duplicate["close"] = Decimal("10.50")
    duplicate["high"] = Decimal("10.60")
    result = MarketAdapter(Repo(values + [duplicate]), None)._minute_chart(
        data(limit=10), NOW, MASTER, missing()
    )
    assert len(result["candles"]) == 4
    assert result["dataStatus"] == "partial"
    duplicated_at = timestamp(values[1]["datetime"])
    duplicated = [c for c in result["candles"] if c["startAt"] == duplicated_at]
    assert len(duplicated) == 1
    assert duplicated[0]["complete"] is False
    assert duplicated[0]["quality"] == "duplicate_minute"
    assert all(c["quality"] == "ok" for c in result["candles"] if c["startAt"] != duplicated_at)


def test_unfinished_last_bar_stays_on_the_page_without_a_future_available_time():
    now = timestamp(datetime.fromisoformat("2026-01-05T09:30:30+08:00"))
    values = rows(1)
    result = MarketAdapter(Repo(values), None)._minute_chart(
        data(limit=10), now, MASTER, missing()
    )
    assert len(result["candles"]) == 1
    candle = result["candles"][0]
    assert candle["complete"] is False
    assert candle["availableAtMs"] is None
    assert candle["endMs"] > now
    assert result["asOf"] is None


def test_same_timestamp_price_revision_invalidates_the_old_cursor():
    repo = Repo(rows(6))
    adapter = MarketAdapter(repo, None)
    first = adapter._minute_chart(data(limit=2), NOW, MASTER, missing())
    repo.rows[3]["close"] = Decimal("10.99")
    repo.rows[3]["high"] = Decimal("11.00")
    revised = adapter._minute_chart(data(limit=2, cursor=first["nextCursor"]), NOW, MASTER, missing())
    assert revised.get("cursorExpired") is True


def test_new_minute_invalidates_the_old_cursor_instead_of_resetting_to_first_page():
    repo = Repo(rows(6))
    adapter = MarketAdapter(repo, None)
    first = adapter._minute_chart(data(limit=2), NOW, MASTER, missing())
    repo.rows.append({
        "datetime": datetime(2026, 1, 5, 9, 36),
        "open": Decimal("10.2"), "high": Decimal("10.3"), "low": Decimal("10.1"),
        "close": Decimal("10.25"), "volume": 200, "amount": 2000,
    })
    extended = adapter._minute_chart(data(limit=2, cursor=first["nextCursor"]), NOW, MASTER, missing())
    assert extended.get("cursorExpired") is True


def test_lunch_break_and_out_of_session_minutes_are_not_fabricated():
    values = rows(3, start_minute=28)
    values += [
        {
            "datetime": datetime(2026, 1, 5, 11, 30),
            "open": Decimal("10.2"), "high": Decimal("10.3"), "low": Decimal("10.1"),
            "close": Decimal("10.25"), "volume": 200, "amount": 2000,
        },
        {
            "datetime": datetime(2026, 1, 5, 13, 0),
            "open": Decimal("10.3"), "high": Decimal("10.4"), "low": Decimal("10.2"),
            "close": Decimal("10.35"), "volume": 300, "amount": 3000,
        },
    ]
    result = MarketAdapter(Repo(values), None)._minute_chart(data("5m"), NOW, MASTER, missing())
    starts = {c["startAt"] for c in result["candles"]}
    assert timestamp(datetime.fromisoformat("2026-01-05T11:30:00+08:00")) not in starts
    assert timestamp(datetime.fromisoformat("2026-01-05T13:00:00+08:00")) in starts
    assert all(c["quality"] != "ok" or c["complete"] for c in result["candles"])


def test_minute_chart_never_labels_forward_adjusted_prices():
    result = adapter(10)._minute_chart(
        {"symbol": SYMBOL, "adjustment": "forward", "timeframe": "1m"}, NOW, MASTER, missing()
    )
    assert result["dataStatus"] == "missing"
    assert result["candles"] == []


def test_request_cutoff_excludes_minutes_that_have_not_started():
    cutoff = timestamp(datetime.fromisoformat("2026-01-05T09:30:30+08:00"))
    for timeframe in ("1m", "5m"):
        result = MarketAdapter(Repo(rows(8)), None)._minute_chart(
            data(timeframe, limit=20), cutoff, MASTER, missing()
        )
        assert result["candles"], timeframe
        assert all(candle["startAt"] <= cutoff for candle in result["candles"])
        assert all(candle["endMs"] <= cutoff or not candle["complete"]
                   for candle in result["candles"])
        if timeframe == "1m":
            assert len(result["candles"]) == 1
            assert result["candles"][0]["complete"] is False
            assert result["candles"][0]["availableAtMs"] is None


def test_missing_bucket_start_is_a_visible_gap_not_a_borrowed_open():
    values = rows(9, start_minute=31)
    result = MarketAdapter(Repo(values), None)._minute_chart(
        data("5m"), NOW, MASTER, missing()
    )
    assert [candle["startAt"] for candle in result["candles"]] == [
        timestamp(datetime.fromisoformat("2026-01-05T09:35:00+08:00"))
    ]
    assert result["candles"][0]["complete"] is True
    [gap] = result["gaps"]
    assert gap["startAt"] == timestamp(datetime.fromisoformat("2026-01-05T09:30:00+08:00"))
    assert gap["quality"] == "missing_first_minute"
    assert gap["observedMinutes"] == 4
    assert timestamp(datetime.fromisoformat("2026-01-05T09:30:00+08:00")) in gap["missingMinutes"]
    assert all(candle["startAt"] != gap["startAt"] for candle in result["candles"])
    assert result["dataStatus"] == "partial"


def test_missing_middle_or_last_component_stays_incomplete():
    for missing_minute in (33, 34):
        values = [row for index, row in enumerate(rows(5)) if index != missing_minute - 30]
        result = MarketAdapter(Repo(values), None)._minute_chart(
            data("5m"), NOW, MASTER, missing()
        )
        assert len(result["candles"]) == 1
        candle = result["candles"][0]
        assert candle["complete"] is False
        assert candle["quality"] == "incomplete_group"
        assert candle["availableAtMs"] is None


def test_missing_component_amount_is_unknown_not_zero():
    values = rows(5)
    values[2]["amount"] = None
    result = MarketAdapter(Repo(values), None)._minute_chart(
        data("5m"), NOW, MASTER, missing()
    )
    [candle] = result["candles"]
    assert candle["quality"] == "incomplete_amount"
    assert candle["complete"] is False
    assert candle["amount"] is None


def test_weekend_and_unavailable_rows_are_rejected():
    weekend = rows(3, day=10)
    result = MarketAdapter(Repo(weekend), None)._minute_chart(
        data("1m"), timestamp(datetime.fromisoformat("2026-01-10T10:00:00+08:00")),
        MASTER, missing(),
    )
    assert result["dataStatus"] == "missing" and result["candles"] == []

    cutoff = timestamp(datetime.fromisoformat("2026-01-05T09:30:30+08:00"))
    delayed = rows(1)
    delayed[0]["available_at"] = datetime(2026, 1, 5, 9, 31)
    result = MarketAdapter(Repo(delayed), None)._minute_chart(
        data("1m"), cutoff, MASTER, missing()
    )
    assert result["dataStatus"] == "missing" and result["candles"] == []


def test_late_revision_after_close_invalidates_the_old_cursor():
    close = timestamp(datetime.fromisoformat("2026-01-05T16:00:00+08:00"))
    repo = Repo(rows(8))
    page = MarketAdapter(repo, None)._minute_chart(data(limit=2), close, MASTER, missing())
    repo.rows[6]["close"] = Decimal("11.11")
    repo.rows[6]["high"] = Decimal("11.20")
    revised = MarketAdapter(repo, None)._minute_chart(
        data(limit=2, cursor=page["nextCursor"]), close, MASTER, missing()
    )
    assert revised.get("cursorExpired") is True
