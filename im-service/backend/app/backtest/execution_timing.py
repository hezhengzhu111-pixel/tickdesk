"""Evidence-backed entry timing for the existing portfolio matcher."""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise
from types import MappingProxyType
from typing import ClassVar
from zoneinfo import ZoneInfo

from app.backtest.evidence import json_digest

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_PRICE = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
_MINUTE_MS = 60_000
_MODEL = "continuous_raw_minute_open_v2"
_CAPACITY_MODEL = "continuous_raw_minute_open_capacity_v1"


def _day(milliseconds: int) -> str:
    if type(milliseconds) is not int or not 0 <= milliseconds < 253402272000000:
        raise ValueError("execution evidence requires a valid epoch millisecond")
    return datetime.fromtimestamp(milliseconds // 1000, UTC).astimezone(_SHANGHAI).date().isoformat()


def _source(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("execution evidence source is required")


@dataclass(frozen=True)
class DecisionPublication:
    symbol: str
    bar_end_ms: int
    available_at_ms: int
    published_at_ms: int
    source: str

    def __post_init__(self) -> None:
        _day(self.bar_end_ms)
        _day(self.available_at_ms)
        _day(self.published_at_ms)
        _source(self.source)
        if not self.bar_end_ms <= self.available_at_ms <= self.published_at_ms:
            raise ValueError("bar close, availability, and publication must be chronological")


@dataclass(frozen=True)
class ContinuousSession:
    start_ms: int
    end_ms: int
    source: str

    def __post_init__(self) -> None:
        if _day(self.start_ms) != _day(self.end_ms) or self.start_ms >= self.end_ms:
            raise ValueError("continuous session must have a valid same-day interval")
        _source(self.source)


@dataclass(frozen=True)
class RawMinuteOpen:
    symbol: str
    at_ms: int
    price: str
    status: str
    source: str
    available_shares: int | None = None

    def __post_init__(self) -> None:
        _day(self.at_ms)
        _source(self.source)
        if self.at_ms % 60_000 or self.status not in {"executable", "blocked", "unknown"}:
            raise ValueError("raw minute evidence requires an opening timestamp and execution status")
        if (
            not isinstance(self.price, str) or len(self.price) > 48 or not _PRICE.fullmatch(self.price)
            or Decimal(self.price) <= 0
        ):
            raise ValueError("raw minute open must be a positive decimal string")
        if self.available_shares is not None and (type(self.available_shares) is not int or self.available_shares < 0):
            raise ValueError("verified execution capacity must be a nonnegative integer number of shares")


@dataclass(frozen=True)
class EntryOpenTiming:
    """Match daily signals against side-specific publication and minute evidence.

    Capacity is the evidenced executable quantity at the raw opening price for that side.
    A minute's total trading volume does not establish this quantity.
    """

    publications: tuple[DecisionPublication, ...]
    sessions: tuple[ContinuousSession, ...]
    minute_opens: tuple[RawMinuteOpen, ...]
    response_delay_ms: int
    exit_publications: tuple[DecisionPublication, ...] = ()
    exit_minute_opens: tuple[RawMinuteOpen, ...] = ()
    require_capacity: bool = False
    _publications: ClassVar[Mapping]
    _opens: ClassVar[Mapping]
    _exit_publications: ClassVar[Mapping]
    _exit_opens: ClassVar[Mapping]
    _sessions: ClassVar[Mapping]
    _sha256: ClassVar[str]

    def __post_init__(self) -> None:
        if type(self.response_delay_ms) is not int or self.response_delay_ms <= 0:
            raise ValueError("entry timing requires a positive predeclared response delay")
        if type(self.require_capacity) is not bool:
            raise ValueError("execution capacity policy must be a boolean")
        if not self.require_capacity and any(
            row.available_shares is not None for rows in (self.minute_opens, self.exit_minute_opens) for row in rows
        ):
            raise ValueError("capacity evidence requires the capacity-aware timing policy")
        sessions = tuple(sorted(self.sessions, key=lambda row: row.start_ms))
        if any(left.end_ms > right.start_ms for left, right in pairwise(sessions)):
            raise ValueError("continuous execution sessions overlap")
        sessions_by_day = defaultdict(list)
        for session in sessions:
            sessions_by_day[_day(session.start_ms)].append(session)

        object.__setattr__(self, "sessions", sessions)
        object.__setattr__(self, "_sessions", MappingProxyType({day: tuple(rows) for day, rows in sessions_by_day.items()}))
        for prefix in ("", "exit_"):
            publications = tuple(sorted(getattr(self, f"{prefix}publications"), key=lambda row: (row.symbol, row.bar_end_ms)))
            by_signal = {(row.symbol, _day(row.bar_end_ms)): row for row in publications}
            if len(by_signal) != len(publications):
                raise ValueError("ambiguous daily publication evidence")
            minute_opens = tuple(sorted(getattr(self, f"{prefix}minute_opens"), key=lambda row: (row.symbol, row.at_ms)))
            by_minute = {(row.symbol, row.at_ms): row for row in minute_opens}
            if len(by_minute) != len(minute_opens):
                raise ValueError("ambiguous minute open evidence")
            object.__setattr__(self, f"{prefix}publications", publications)
            object.__setattr__(self, f"{prefix}minute_opens", minute_opens)
            object.__setattr__(self, f"_{prefix}publications", MappingProxyType(by_signal))
            object.__setattr__(self, f"_{prefix}opens", MappingProxyType(by_minute))
        evidence = asdict(self)
        if not self.require_capacity:
            # Preserve the identity of historical clock-only evidence archives.
            evidence.pop("require_capacity")
            for side in ("minute_opens", "exit_minute_opens"):
                for row in evidence[side]:
                    row.pop("available_shares")
        object.__setattr__(self, "_sha256", json_digest({
            "model": _CAPACITY_MODEL if self.require_capacity else _MODEL, **evidence,
        }))

    def summary(self) -> dict:
        return {
            "model": _CAPACITY_MODEL if self.require_capacity else _MODEL,
            "response_delay_ms": self.response_delay_ms,
            "evidence_sha256": self._sha256,
            "coverage": "entry_and_signal_exit_clocks" if self.exit_publications and self.exit_minute_opens else "entry_clock_only",
            "admission_eligible": False,
        }

    @classmethod
    def from_dict(cls, value: Mapping) -> EntryOpenTiming:
        required = {"publications", "sessions", "minute_opens", "response_delay_ms"}
        optional = {"exit_publications", "exit_minute_opens"}
        if set(value) - {"require_capacity"} not in (required, required | optional):
            raise ValueError("invalid entry timing evidence fields")
        return cls(
            tuple(DecisionPublication(**row) for row in value["publications"]),
            tuple(ContinuousSession(**row) for row in value["sessions"]),
            tuple(RawMinuteOpen(**row) for row in value["minute_opens"]),
            value["response_delay_ms"],
            tuple(DecisionPublication(**row) for row in value.get("exit_publications", [])),
            tuple(RawMinuteOpen(**row) for row in value.get("exit_minute_opens", [])),
            value.get("require_capacity", False),
        )

    def resolve(self, symbol: str, signal_day: str, fill_day: str) -> dict | None:
        return self._resolve(symbol, signal_day, fill_day, self._publications, self._opens)

    def resolve_exit(self, symbol: str, signal_day: str, fill_day: str) -> dict | None:
        return self._resolve(symbol, signal_day, fill_day, self._exit_publications, self._exit_opens)

    def _resolve(self, symbol, signal_day, fill_day, publications, opens):
        publication = publications.get((symbol, signal_day))
        if publication is None or fill_day <= signal_day:
            return None
        ready_at_ms = publication.published_at_ms + self.response_delay_ms
        for session in self._sessions.get(fill_day, ()):
            earliest = max(session.start_ms, ready_at_ms)
            first_minute = ((earliest + _MINUTE_MS - 1) // _MINUTE_MS) * _MINUTE_MS
            for minute in range(first_minute, session.end_ms, _MINUTE_MS):
                row = opens.get((symbol, minute))
                if row is None or row.status == "unknown":
                    return None
                if row.status == "blocked":
                    continue
                if self.require_capacity:
                    if row.available_shares is None:
                        return None
                    if row.available_shares < 100:
                        continue
                return {
                    "symbol": symbol, "bar_end_ms": publication.bar_end_ms,
                    "available_at_ms": publication.available_at_ms, "published_at_ms": publication.published_at_ms,
                    "ready_at_ms": ready_at_ms, "fill_at_ms": row.at_ms, "raw_open_price": row.price,
                    "publication_source": publication.source, "minute_source": row.source,
                    "calendar_source": session.source,
                    **({"available_shares": row.available_shares} if self.require_capacity else {}),
                }
        return None
