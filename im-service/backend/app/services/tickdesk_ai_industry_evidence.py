"""Read archived Fuyao industry and daily index sources for AI request evidence."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.services.tickdesk_regime_archive import SourceLookup, _lookup, _read
from app.services.tickdesk_regime_evidence import _find_group, _industry, _number, _session_label

_CHINA = ZoneInfo("Asia/Shanghai")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MAX_SYMBOLS = 4
_HISTORY_TARGET = 20
_BROAD_INDEX = "000300.SH"


def _cutoff_value(at: datetime) -> int:
    delta = at - _EPOCH
    # Wire times have milliseconds. Round up so a fractional receipt cannot be
    # represented as visible on an earlier wire cutoff.
    return delta.days * 86_400_000 + delta.seconds * 1000 + (delta.microseconds + 999) // 1000


def _source_at(db, kind, code, cutoff):
    if db is None:
        return SourceLookup("archive_unavailable")
    try:
        return _lookup(db, kind, code, cutoff)
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error):
        return SourceLookup("archive_unavailable")


def _absence(source):
    state = {"conflict": "source_conflict", "missing": "missing_at_cutoff"}.get(
        source.state, source.state
    )
    return {"state": state}


def _provenance(source):
    return {
        "provider": "fuyao",
        "sourceRevisionId": source.observation.sha256,
        "observedAt": source.observation.observed_at.astimezone(_CHINA).isoformat(),
        "artifactSha256": source.artifact_sha256,
        "providerResponseSha256": source.provider_sha256,
    }


def _evidence(record, label, available, metrics):
    record["availableAt"] = available.astimezone(_CHINA).isoformat()
    identity = hashlib.sha256(
        json.dumps(record, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    record["revisionId"] = identity
    return {
        "source": {
            "ref": f"local:fuyao-index:{identity}",
            "name": label[:100],
            "asOf": _cutoff_value(available),
        },
        "record": record,
        "metrics": metrics,
    }


def _membership(source, symbol, cutoff):
    if source.record is None:
        return None, _absence(source)
    raw = source.record
    observed = raw.observation.observed_at.astimezone(_CHINA)
    if observed.date() != cutoff.date():
        return None, {"state": "membership_not_observed_today"}
    data = raw.observation.data
    code = _industry(data, symbol)
    if code is None:
        return None, {"state": "membership_missing_or_ambiguous"}
    name = _find_group(data, symbol, limit=100)["index_list"][0].get("name")
    if (
        not isinstance(name, str)
        or not 0 < len(name) <= 80
        or not all(char.isalnum() or char in " -()（）/、&－" for char in name)
        or not name.strip()
    ):
        return None, {"state": "industry_label_unavailable"}
    record = {
        "kind": "industryMembership",
        "symbol": symbol,
        "indexCode": code,
        "industryName": name,
        "basis": "same_day_membership_observation",
        **_provenance(raw),
    }
    label = f"{symbol} · 同花顺行业归属 · {name} · {observed.date()}"
    return _evidence(record, label, observed, {}), {"state": "available"}


def _daily_bars(source, code):
    if source.record is None:
        return [], {**_absence(source), "dailyBars": 0}
    raw = source.record.observation
    data = raw.data
    group = _find_group(data, code, limit=10)
    rows = (
        group.get("item")
        if group and data.get("interval") == "1d" and data.get("adjust", 1) is None
        else None
    )
    if not isinstance(rows, list) or len(rows) > 64:
        return [], {"state": "invalid_daily_series", "dailyBars": 0}
    verified = []
    previous = None
    for row in rows:
        day = _session_label(row.get("date_ms")) if isinstance(row, dict) else None
        if day is None or (previous is not None and day <= previous):
            return [], {"state": "invalid_daily_series", "dailyBars": 0}
        previous = day
        if raw.observed_at < datetime.combine(day, time(15, 30), _CHINA):
            continue
        ohlc = [
            _number(row.get(field))
            for field in ("open_price", "high_price", "low_price", "close_price")
        ]
        if any(value is None for value in ohlc):
            return [], {"state": "invalid_daily_series", "dailyBars": 0}
        opening, high, low, close = ohlc
        if not 0 < low <= min(opening, close) <= max(opening, close) <= high:
            return [], {"state": "invalid_daily_series", "dailyBars": 0}
        verified.append((day, close))
    count = len(verified)
    state = (
        "no_observed_completed_bar"
        if count == 0
        else "low_coverage"
        if count < _HISTORY_TARGET
        else "available"
    )
    return verified, {"state": state, "dailyBars": count}


def _index_evidence(symbol, code, source, bars, kind, membership=None):
    day, closing_price = bars[-1]
    close = format(closing_price, "f")
    raw = source.record
    available = raw.observation.observed_at
    label = "沪深300日收盘" if kind == "marketIndex" else "同花顺行业指数日收盘"
    record = {
        "kind": kind,
        "symbol": symbol,
        "indexCode": code,
        "tradingDate": day.isoformat(),
        "indexClose": close,
        "unit": "点",
        "basis": "provider_observed_daily_bar",
        "observedDailyBars": len(bars),
        **_provenance(raw),
    }
    if membership:
        entry = membership["record"]
        record.update(
            industryName=entry["industryName"],
            membershipRef=membership["source"]["ref"],
            membershipRevisionId=entry["revisionId"],
            membershipObservedAt=entry["observedAt"],
        )
        available = max(available, datetime.fromisoformat(entry["availableAt"]))
    return _evidence(
        record,
        f"{code} · {label} · {day} · 扶摇",
        available,
        {kind + "Close": [label, close, "点", day.isoformat()]},
    )


def _read_context(db, cutoff, symbols):
    rows, by_symbol = [], {}
    market = _source_at(db, "indices", _BROAD_INDEX, cutoff)
    market_bars, market_state = _daily_bars(market, _BROAD_INDEX)
    for symbol in symbols:
        if not re.fullmatch(r"[0-9]{6}\.(?:SH|SZ)", symbol):
            by_symbol[symbol] = {"state": "unsupported_symbol"}
            continue
        current, member_state = _membership(
            _source_at(db, "membership", symbol, cutoff), symbol, cutoff
        )
        industry_state = {"state": "membership_unavailable", "dailyBars": 0}
        if current is not None:
            rows.append(current)
            code = current["record"]["indexCode"]
            industry = _source_at(db, "indices", code, cutoff)
            industry_bars, industry_state = _daily_bars(industry, code)
            if industry_bars:
                rows.append(
                    _index_evidence(symbol, code, industry, industry_bars, "industryIndex", current)
                )
        if market_bars:
            rows.append(_index_evidence(symbol, _BROAD_INDEX, market, market_bars, "marketIndex"))
        by_symbol[symbol] = {
            "membership": member_state,
            "marketIndex": market_state,
            "industryIndex": industry_state,
        }
    return rows, by_symbol


def read_archived_market_context(
    path: Path, cutoff_ms: int, symbols: list[str]
) -> tuple[list[dict], dict]:
    """Return AI context rows with source revisions, independently of quant decisions."""
    if type(cutoff_ms) is not int or not 0 < cutoff_ms < 253402214400000:
        raise ValueError("invalid AI evidence cutoff")
    cutoff = (_EPOCH + timedelta(milliseconds=cutoff_ms)).astimezone(_CHINA)
    selected = list(dict.fromkeys(symbols[:_MAX_SYMBOLS]))
    rows, by_symbol = [], {}
    if selected:
        try:
            if path.exists():
                with closing(_read(path)) as db:
                    rows, by_symbol = _read_context(db, cutoff, selected)
            else:
                rows, by_symbol = _read_context(None, cutoff, selected)
        except (ValueError, OSError, sqlite3.Error):
            rows, by_symbol = _read_context(None, cutoff, selected)
    return rows, {
        "status": "partial" if rows else "unavailable",
        "requestedSymbols": len(symbols),
        "requestedLimit": _MAX_SYMBOLS,
        "dailyCoverageTarget": _HISTORY_TARGET,
        "bySymbol": by_symbol,
    }
