"""Calculate versioned research regime evidence from observed Fuyao inputs."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

RULE_ID = "fuyao-csi300-ths-industry-sma20-tminus1-v1"
MARKET_FIELD = "ext_market_regime"
INDUSTRY_FIELD = "ext_industry_regime"

_MARKET_INDEX = "000300.SH"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_WINDOW = 20
_MAX_INDEX_ROWS = 64
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]{0,15})(?:\.[0-9]{1,16})?")


@dataclass(frozen=True)
class ObservedData:
    """A trusted collector supplies the actual receipt time and response body hash."""

    data: object
    observed_at: datetime
    sha256: str


@dataclass(frozen=True)
class RegimeFact:
    value: int | None = None
    reason: str | None = None
    index_code: str | None = None
    available_at: datetime | None = None
    evidence_hash: str | None = None
    rule_id: str = RULE_ID


def _local_stamp(stamp: datetime) -> datetime:
    if not isinstance(stamp, datetime) or stamp.utcoffset() is None:
        raise ValueError("regime evidence requires an aware time")
    return stamp.astimezone(_SHANGHAI)


def _visible(source: ObservedData | None, cutoff: datetime) -> datetime | None:
    if (not isinstance(source, ObservedData) or not isinstance(source.sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", source.sha256)):
        return None
    try:
        observed = _local_stamp(source.observed_at)
    except ValueError:
        return None
    return observed if observed <= cutoff else None


def _session_days(data: object, day: date) -> tuple[date, ...] | None:
    if not isinstance(data, list) or not _WINDOW < len(data) <= 370:
        return None
    try:
        sessions = [date.fromisoformat(item) for item in data]
    except (TypeError, ValueError):
        return None
    if [item.isoformat() for item in sessions] != data or sessions != sorted(set(sessions)) or day not in sessions:
        return None
    position = sessions.index(day)
    return tuple(sessions[position - _WINDOW:position]) if position >= _WINDOW else None


def _session_label(value: object) -> date | None:
    if type(value) is not int or value < 0:
        return None
    try:
        stamp = datetime.fromtimestamp(value / 1000, _SHANGHAI)
    except (OverflowError, OSError, ValueError):
        return None
    return stamp.date() if stamp.time() == time.min else None


def _number(value: object) -> Decimal | None:
    if type(value) not in (str, int, float, Decimal):
        return None
    if type(value) is int and value.bit_length() > 54:
        return None
    text = str(value)
    return Decimal(text) if len(text) <= 33 and _DECIMAL.fullmatch(text) else None


def _index_closes(rows: object, session_days: Sequence[date]) -> tuple[Decimal, ...] | None:
    if (len(session_days) != _WINDOW or any(type(day) is not date for day in session_days)
            or tuple(sorted(set(session_days))) != tuple(session_days)
            or not isinstance(rows, list) or not _WINDOW <= len(rows) <= _MAX_INDEX_ROWS):
        return None
    need = set(session_days)
    closes = {}
    previous = None
    for row in rows:
        if not isinstance(row, Mapping):
            return None
        day = _session_label(row.get("date_ms"))
        if day is None or day > session_days[-1] or (previous is not None and day <= previous):
            return None
        previous = day
        if day < session_days[0]:
            continue
        if day not in need:
            return None
        values = [_number(row.get(key)) for key in
                  ("open_price", "high_price", "low_price", "close_price", "volume", "turnover")]
        if any(value is None for value in values):
            return None
        opening, high, low, close, _, _ = values
        if not 0 < low <= min(opening, close) <= max(opening, close) <= high:
            return None
        closes[day] = close
    if set(closes) != need:
        return None
    return tuple(closes[day] for day in session_days)


def _direction(closes: Sequence[Decimal]) -> int:
    # 32-digit observed scalars plus the two digits from summing 20 values.
    with localcontext() as ctx:
        ctx.prec = 34
        return int(_WINDOW * closes[-1] > sum(closes))


def index_direction(rows: object, session_days: Sequence[date]) -> int | None:
    """Recompute the candidate threshold on exactly 20 verified prior sessions."""
    closes = _index_closes(rows, session_days)
    return _direction(closes) if closes is not None else None


def _find_group(data: object, code: str, *, limit: int) -> Mapping | None:
    items = data.get("item") if isinstance(data, Mapping) else None
    if not isinstance(items, list) or len(items) > limit:
        return None
    matches = [item for item in items if isinstance(item, Mapping) and item.get("thscode") == code]
    return matches[0] if len(matches) == 1 else None


def _industry(data: object, symbol: str) -> str | None:
    group = _find_group(data, symbol, limit=100)
    entries = group.get("index_list") if group is not None else None
    if not isinstance(entries, list) or len(entries) != 1:
        return None
    entry = entries[0]
    if not isinstance(entry, Mapping) or entry.get("type") != "industry":
        return None
    code = entry.get("thscode")
    return code if isinstance(code, str) and re.fullmatch(r"[0-9]{6}\.TI", code) else None


def evaluate_for_decision(
    symbol: str,
    decision_at: datetime,
    *,
    calendar: ObservedData | None,
    indices: ObservedData | None,
    membership: ObservedData | None,
) -> dict[str, RegimeFact]:
    """Derive same-session research facts at a caller-supplied decision cutoff."""
    if not isinstance(symbol, str) or re.fullmatch(r"[0-9]{6}\.(SH|SZ)", symbol) is None:
        raise ValueError("unsupported regime security symbol")
    cutoff = _local_stamp(decision_at)
    day = cutoff.date()
    calendar_at = _visible(calendar, cutoff)
    sessions = _session_days(calendar.data, day) if calendar_at is not None else None
    if sessions is None:
        unknown = RegimeFact(reason="calendar_session_or_window_unknown")
        return {MARKET_FIELD: unknown, INDUSTRY_FIELD: unknown}

    indices_at = _visible(indices, cutoff)
    # Index times label a trading date. Apply the current research collector's
    # completed-session buffer to the actual receipt time, not the label.
    completed_after = datetime.combine(sessions[-1], time(15, 30), _SHANGHAI)
    member_at = _visible(membership, cutoff)
    industry = _industry(membership.data, symbol) if member_at is not None and member_at.date() == day else None

    def fact(code: str, with_membership: bool) -> RegimeFact:
        if indices_at is None or indices_at < completed_after:
            return RegimeFact(reason="index_observation_unavailable", index_code=code)
        data = indices.data
        if not isinstance(data, Mapping) or data.get("interval") != "1d" or data.get("adjust", "missing") is not None:
            return RegimeFact(reason="invalid_index_response", index_code=code)
        group = _find_group(data, code, limit=10)
        closes = _index_closes(group.get("item"), sessions) if group is not None else None
        if closes is None:
            return RegimeFact(reason="incomplete_or_invalid_index_window", index_code=code)
        sources = [[calendar.sha256, calendar_at.isoformat()], [indices.sha256, indices_at.isoformat()]]
        available = max(calendar_at, indices_at)
        if with_membership:
            sources.append([membership.sha256, member_at.isoformat()])
            available = max(available, member_at)
        payload = [RULE_ID, symbol, day.isoformat(), code, sources,
                   [[session.isoformat(), str(close)] for session, close in zip(sessions, closes, strict=True)]]
        identity = hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()
        return RegimeFact(value=_direction(closes), index_code=code, available_at=available, evidence_hash=identity)

    return {
        MARKET_FIELD: fact(_MARKET_INDEX, with_membership=False),
        INDUSTRY_FIELD: fact(industry, with_membership=True) if industry is not None else RegimeFact(reason="membership_unknown"),
    }
