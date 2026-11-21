"""Chart aggregation over completed exchange periods and verified daily candles."""
from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext

from app.services.tickdesk_adapter import CN, _wire_decimal, timestamp


def completed_periods(root, now, end, timeframe):
    from app.services.tickdesk_market_data import _completed_through, read_calendar

    calendar = read_calendar(root, now)
    if not calendar:
        return []
    observed_through = date.fromisoformat(calendar["through"])
    completed_through = min(observed_through, _completed_through(datetime.fromtimestamp(now / 1000, CN)))
    sessions = [date.fromisoformat(day) for day in calendar["sessions"]]
    groups = {}
    for day in sessions:
        first = day - timedelta(days=day.weekday()) if timeframe == "1w" else day.replace(day=1)
        groups.setdefault(first, []).append(day)
    result = []
    for first, days in groups.items():
        natural_end = first + timedelta(days=6) if timeframe == "1w" else first.replace(day=monthrange(first.year, first.month)[1])
        # The observed calendar defines holidays; only the exchange's ordinary
        # non-trading weekend may extend beyond the calendar's observed range.
        last_possible_session = natural_end - timedelta(days=max(0, natural_end.weekday() - 4))
        if first >= sessions[0] and last_possible_session <= observed_through and days[-1] <= min(completed_through, end):
            result.append(tuple(day.isoformat() for day in days))
    return result


def period_chart_page(missing, limit, periods, daily):
    ready = []
    for expected in periods:
        if not all(day in daily for day in expected):
            continue
        candles, sources = zip(*(daily[day] for day in expected), strict=True)
        category = {(source["name"], source["ref"]) for source in sources}
        if len(category) != 1:
            continue
        volume = sum(candle["volume"] for candle in candles)
        if volume > 9_007_199_254_740_991:
            continue
        amounts = [candle["amount"] for candle in candles]
        total_amount = None
        if all(amount is not None for amount in amounts):
            with localcontext() as ctx:
                ctx.prec = 108
                total_amount = _wire_decimal(sum(Decimal(amount) for amount in amounts))
            if total_amount is None:
                continue
        candle = {
            "date": expected[-1], "open": candles[0]["open"], "close": candles[-1]["close"],
            "high": max((part["high"] for part in candles), key=Decimal),
            "low": min((part["low"] for part in candles), key=Decimal),
            "volume": volume, "amount": total_amount,
        }
        name, reference = category.pop()
        ready.append((candle, {"name": name, "ref": reference, "asOf": max(source["asOf"] for source in sources)}))
    if not ready:
        return missing
    candles, sources = zip(*ready[-limit:], strict=True)
    last = candles[-1]["date"]
    source = None
    if len({(item["name"], item["ref"]) for item in sources}) == 1:
        source = {**sources[-1], "asOf": max(item["asOf"] for item in sources)}
    return {**missing, "asOf": timestamp(last + "T15:00:00+08:00") if source else None,
            "dataStatus": "fresh" if source and last == periods[-1][-1] else "stale",
            "candles": list(candles), "source": source,
            "nextBeforeDate": candles[0]["date"] if len(ready) > limit else None, "hasMore": len(ready) > limit}
