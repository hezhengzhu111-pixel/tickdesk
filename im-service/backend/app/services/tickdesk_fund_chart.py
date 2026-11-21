"""Source-backed ETF chart data whose forward basis is separate from raw history."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

import polars as pl

from app.data_providers import custom
from app.services import preferences
from app.services.fs_utils import atomic_write_parquet, atomic_write_text
from app.services.tickdesk_adapter import CN, render_candle, timestamp

_SOURCE_DOC = "https://fuyao.aicubes.cn/docs/api-reference/fund-market/"


def read_fund_master(root, now):
    file = root / "tickdesk-market" / "fund-master.parquet"
    if not file.is_file():
        return []
    return pl.read_parquet(file).filter(pl.col("as_of").is_between(now - 3 * 86_400_000, now)).to_dicts()


def refresh_fund_master(root):
    provider = preferences.get_daily_data_provider()
    if not custom.provider_has_dataset(provider, "daily"):
        return 0
    active = custom.get_provider(provider)
    if not callable(getattr(active, "get_fund_forward_daily", None)):
        return 0
    now = datetime.now(CN)
    cached = read_fund_master(root, timestamp(now))
    if cached and datetime.fromtimestamp(cached[0]["as_of"] / 1000, CN).date() == now.date():
        return len(cached)
    rows = active.get_instruments("etf")
    normalized = [{
        "symbol": row["symbol"], "name": row["name"], "exchange": row["exchange"],
        "as_of": timestamp(row["as_of"]), "source": provider,
        "source_ref": "https://fuyao.aicubes.cn/docs/api-reference/ticker-list/", "status": "unknown",
    } for row in rows if row.get("type") == "etf" and isinstance(row.get("name"), str) and row["name"].strip()
        and isinstance(row.get("symbol"), str) and re.fullmatch(r"[0-9]{6}\.(SH|SZ)", row["symbol"])]
    if not 0 < len(normalized) <= 8000 or len({row["symbol"] for row in normalized}) != len(normalized):
        raise ValueError("ETF catalog incomplete")
    file = root / "tickdesk-market" / "fund-master.parquet"
    file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(pl.DataFrame(normalized), file)
    return len(normalized)


def refresh_fund_charts(repo, symbols):
    from app.services.tickdesk_market_data import _completed_through, read_calendar

    source = preferences.get_daily_data_provider()
    if not custom.provider_has_dataset(source, "daily"):
        return {"symbols": []}
    fetch = getattr(custom.get_provider(source), "get_fund_forward_daily", None)
    if not callable(fetch):
        return {"symbols": []}
    now = datetime.now(CN)
    root = repo.store.data_dir
    calendar = read_calendar(root, timestamp(now))
    end = _completed_through(now)
    if not calendar or calendar.get("source") != source or calendar["through"] < str(end):
        raise ValueError("ETF calendar unavailable")
    sessions = {day for day in calendar["sessions"] if day <= str(end)}
    if not sessions:
        raise ValueError("ETF completed session unavailable")
    latest = max(sessions)
    groups = fetch(symbols, end - timedelta(days=300), date.fromisoformat(latest))
    observed_at = timestamp(datetime.now(CN))
    ready = []
    for symbol, rows in groups.items():
        rows = sorted((row for row in rows if row["date"] in sessions), key=lambda row: row["date"])
        if not rows:
            continue
        if (len({row["date"] for row in rows}) != len(rows)
                or any(render_candle(row["date"], {key: row[key] for key in ("open", "high", "low", "close")},
                                     row["volume"], row["amount"]) is None for row in rows)):
            raise ValueError("invalid ETF chart bars")
        file = root / "tickdesk-market" / "etf-charts" / f"{symbol}.json"
        file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(file, json.dumps({
            "symbol": symbol, "source": source, "adjustment": "forward", "observedAt": observed_at,
            "rows": rows,
        }, allow_nan=False))
        if rows[-1]["date"] == latest:
            ready.append(symbol)
    return {"symbols": ready}


def fund_chart_page(root, missing, limit, end, expected, now, *, periods=None):
    from app.services.tickdesk_chart_periods import period_chart_page

    symbol = missing["symbol"]
    file = root / "tickdesk-market" / "etf-charts" / f"{symbol}.json"
    if not file.is_file():
        return None
    record = json.loads(file.read_text(encoding="utf-8"))
    if (record["symbol"] != symbol or record["adjustment"] != "forward"
            or type(record["observedAt"]) is not int or not 0 < record["observedAt"] <= now):
        return missing
    bound = min(str(end), expected) if expected else str(end)
    rows = [row for row in record["rows"] if row["date"] <= bound]
    if not rows:
        return missing
    source = {"name": record["source"], "ref": _SOURCE_DOC, "asOf": record["observedAt"]}
    candles = [render_candle(row["date"], {key: row[key] for key in ("open", "high", "low", "close")},
                             row["volume"], row["amount"]) for row in (rows if periods is not None else rows[-limit:])]
    if any(candle is None for candle in candles):
        return missing
    if periods is not None:
        return period_chart_page(missing, limit, periods, {candle["date"]: (candle, source) for candle in candles})
    last = candles[-1]["date"]
    return {**missing, "asOf": timestamp(last + "T15:00:00+08:00"),
            "dataStatus": "fresh" if last == expected else "stale", "candles": candles,
            "nextBeforeDate": candles[0]["date"] if len(rows) > limit else None, "hasMore": len(rows) > limit,
            "source": source}
