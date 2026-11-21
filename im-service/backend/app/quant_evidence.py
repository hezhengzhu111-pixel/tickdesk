"""Cutoff-aware normalized reference and financial evidence, never master fallback.

Reference archive v1 is append-only through the repository. Exact timestamps use
Shanghai time. Date-only availability requires a separately verified calendar;
without one it remains unavailable. This module produces facts, not admission.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import date, datetime, time
from pathlib import Path
from types import MappingProxyType
from zoneinfo import ZoneInfo

import polars as pl

from app.financial_history import point_in_time_report_timeline
from app.services.financial_sync import get_financial_history
from app.services.fs_utils import atomic_write_text
from app.services.tickdesk_regime_archive import INDEX_ARCHIVE, archive_revision, replay_many
from app.services.tickdesk_regime_evidence import INDUSTRY_FIELD, MARKET_FIELD, RegimeFact

SHANGHAI = ZoneInfo("Asia/Shanghai")
_LOCK = threading.RLock()
REFERENCE_FIELDS = frozenset({"listing_date", "is_st", "trading_status", "name", "settlement_days",
                              "price_limit_ratio", "trading_sessions", "daily_bar_finality", "holding_rules_v3"})
FINANCIAL_FIELDS = {"pb_latest": "bps", "roe_latest": "roe", "gross_margin_latest": "gross_margin",
                    "net_margin_latest": "net_margin", "revenue_yoy_latest": "revenue_yoy",
                    "net_income_yoy_latest": "net_income_yoy", "debt_ratio_latest": "debt_to_asset_ratio"}
REGIME_FIELDS = frozenset({MARKET_FIELD, INDUSTRY_FIELD})
LISTING_SESSIONS_LOWER_BOUND = "listing_sessions_lower_bound"
_REGIME_SYMBOL = re.compile(r"[0-9]{6}\.(?:SH|SZ)")
PIT_FIELDS = frozenset(REFERENCE_FIELDS | FINANCIAL_FIELDS.keys() | REGIME_FIELDS | {
    "listing_sessions", LISTING_SESSIONS_LOWER_BOUND, "total_shares", "float_shares", "market_cap", "float_cap", "turnover_rate",
})


def basic_evidence_fields(basic):
    if not basic.get("enabled", True):
        return set()
    fields = {
        name for prefix, name in (("market_cap", "market_cap"), ("float_cap", "float_cap"),
                                 ("amount", "amount"), ("turnover", "turnover_rate"))
        if any(basic.get(f"{prefix}_{bound}") is not None for bound in ("min", "max"))
    }
    if basic.get("exclude_st"):
        fields.add("is_st")
    if basic.get("price_min") is not None or basic.get("price_max") is not None:
        fields.add("close")
    return fields


def required_quant_fields(strategy, overrides=None):
    """Include actual framework inputs in the preparation snapshot."""
    if strategy.quant_contract is None:
        return frozenset()
    from app.factors.registry import factor_dependencies
    from app.strategy.custom_signals import build_overlay_expr
    from app.strategy.scoring import effective_scoring, scoring_dependencies

    overrides = overrides or {}
    basic = {**(strategy.basic_filter or {}), **(overrides.get("basic_filter") or {})}
    fields = strategy.quant_contract.required_fields | basic_evidence_fields(basic)
    fields |= scoring_dependencies(effective_scoring(strategy.meta.get("scoring"), overrides))
    overlay = build_overlay_expr(overrides["overlay_filter"]) if overrides.get("overlay_filter") else None
    if overlay is not None:
        fields |= factor_dependencies(overlay.meta.root_names())
    fields |= factor_dependencies(fields)
    if "turnover_rate" in fields:
        fields |= {"float_shares"}
    if LISTING_SESSIONS_LOWER_BOUND in fields:
        fields |= {"listing_sessions"}
    return fields


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(_plain(value), sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class EvidenceFact:
    value: object = None
    reason: str | None = "history_not_provided"
    revision_ids: tuple[str, ...] = ()
    available_at: str | None = None
    financial_contributor: str | None = None


@dataclass(frozen=True)
class QuantInputEvidence:
    cutoff: str
    facts: Mapping[tuple[str, str], EvidenceFact]
    identity: str

    def fact(self, symbol: str, field: str) -> EvidenceFact:
        return self.facts.get((symbol, field), EvidenceFact(reason="not_requested"))


def _timestamp(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("availability timestamp requires timezone")
    return stamp.astimezone(SHANGHAI)


def _normalize(row):
    row = dict(row)
    allowed = {"symbol", "field", "value", "source", "effective_date", "available_at", "revision_id"}
    if set(row) - allowed or not allowed - {"revision_id"} <= set(row):
        raise ValueError("malformed reference evidence")
    if row["field"] not in REFERENCE_FIELDS:
        raise ValueError("unknown reference field")
    for key in ("symbol", "source"):
        if not isinstance(row[key], str) or not row[key].strip():
            raise ValueError("missing reference " + key)
    date.fromisoformat(row["effective_date"])
    availability = row["available_at"]
    if len(availability) == 10:
        date.fromisoformat(availability)
    else:
        row["available_at"] = _timestamp(availability).isoformat()
    value, field = row["value"], row["field"]
    if field == "holding_rules_v3":
        from app.services.tickdesk_holding_rules import validate_holding_reference

        validate_holding_reference(row)
    if value is not None:
        if field == "is_st" and type(value) is not bool:
            raise ValueError("is_st must be boolean")
        if field == "settlement_days" and (type(value) is not int or value < 0):
            raise ValueError("settlement_days must be nonnegative integer")
        if field == "price_limit_ratio" and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
            raise ValueError("invalid price limit")
        if field in {"listing_date", "name", "trading_status"} and (not isinstance(value, str) or not value):
            raise ValueError("invalid text reference")
        if field == "listing_date":
            date.fromisoformat(value)
        if field == "trading_sessions":
            if not isinstance(value, dict) or set(value) != {"start", "end", "sessions"}:
                raise ValueError("invalid calendar coverage")
            start, end = date.fromisoformat(value["start"]), date.fromisoformat(value["end"])
            sessions = [date.fromisoformat(item) for item in value["sessions"]]
            if start > end or sessions != sorted(set(sessions)) or any(not start <= d <= end for d in sessions):
                raise ValueError("invalid calendar sessions")
        if field == "daily_bar_finality":
            if not isinstance(value, dict) or set(value) not in (
                {"end_at", "bar_hash"}, {"end_at", "bar_hash", "raw_open"},
            ):
                raise ValueError("invalid daily bar witness")
            if "raw_open" in value and not (type(value["raw_open"]) in (int, float)
                                           and 0 < value["raw_open"] < float("inf")):
                raise ValueError("invalid raw open witness")
            end = _timestamp(value["end_at"])
            if (end.date().isoformat() != row["effective_date"] or _timestamp(row["available_at"]) < end
                    or not re.fullmatch(r"[0-9a-f]{64}", value["bar_hash"])):
                raise ValueError("invalid daily bar witness")
    supplied = row.pop("revision_id", None)
    identity = _digest(row)
    if supplied is not None and supplied != identity:
        raise ValueError("revision_id must match immutable content")
    return {**row, "revision_id": identity}


def _read(root):
    path = Path(root) / "reference_evidence" / "v1.json"
    if not path.exists():
        return []
    archive = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(archive, Mapping) or archive.get("version") != 1 or not isinstance(archive.get("rows"), list):
        raise ValueError("unsupported reference archive")
    return [_normalize(row) for row in archive["rows"]]


def write_reference_evidence(root, rows):
    """Single application main-process writer; preparation workers only read."""
    from multiprocessing import parent_process

    if parent_process() is not None:
        raise RuntimeError("reference evidence writes belong to the application main process")
    normalized = [_normalize(row) for row in rows]
    with _LOCK:
        merged = {row["revision_id"]: row for row in _read(root)}
        merged.update({row["revision_id"]: row for row in normalized})
        path = Path(root) / "reference_evidence" / "v1.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps({"version": 1, "rows": list(merged.values())}, ensure_ascii=False))


@dataclass(frozen=True)
class QuantEvidenceSnapshot:
    """Preparation-owned immutable input; no global cache or file handles."""

    requested_fields: frozenset[str]
    reference_rows: tuple[Mapping, ...]
    reference_failure: str
    reference_by_symbol: Mapping[str, tuple[Mapping, ...]]
    financial_rows: Mapping[str, Mapping[str, tuple[Mapping, ...]]]
    szse_st_manifests: Mapping[str, str]
    sse_st_manifests: Mapping[str, str]
    listing_roster_revisions: Mapping[str, str]
    regime_revision: tuple[int, str] | None
    regime_failure: str
    reference_by_symbol_field: Mapping[tuple[str, str], tuple[Mapping, ...]] = dataclass_field(compare=False)


def load_input_snapshot(root, fields) -> QuantEvidenceSnapshot:
    requested = frozenset(fields)
    try:
        rows, failure = _read(root), "history_not_provided"
    except (OSError, ValueError, TypeError, KeyError):
        rows, failure = [], "malformed_history"
    financial = {}
    for table, columns in (("metrics", sorted(set(FINANCIAL_FIELDS.values()))),
                           ("shares", ["total_shares", "float_shares"])):
        needed = requested.intersection(FINANCIAL_FIELDS if table == "metrics" else
                                        {"total_shares", "float_shares", "market_cap", "float_cap"})
        if not needed:
            continue
        try:
            frame = get_financial_history(Path(root), table)
            timeline = point_in_time_report_timeline(frame, columns, include_evidence=True)
            by_symbol = {}
            for row in timeline.iter_rows(named=True):
                by_symbol.setdefault(row["symbol"], []).append(_freeze(row))
            financial[table] = _freeze(by_symbol)
        except (OSError, ValueError, pl.exceptions.PolarsError):
            financial[table] = MappingProxyType({})
    reference_rows = tuple(_freeze(row) for row in rows)
    reference_by_symbol = {}
    reference_by_symbol_field = {}
    for row in reference_rows:
        reference_by_symbol.setdefault(row["symbol"], []).append(row)
        reference_by_symbol_field.setdefault((row["symbol"], row["field"]), []).append(row)
    szse_manifests, sse_manifests = {}, {}
    if "is_st" in requested:
        from app.services.tickdesk_risk_archive import archived_manifests
        from app.services.tickdesk_sse_risk import SSE_ST_ARCHIVE
        from app.services.tickdesk_szse_risk import archived_st_manifests

        szse_manifests = archived_st_manifests(Path(root))
        sse_manifests = archived_manifests(Path(root) / SSE_ST_ARCHIVE)
    rosters = {}
    if LISTING_SESSIONS_LOWER_BOUND in requested:
        from app.services.tickdesk_listing_evidence import freeze_listing_rosters

        rosters = freeze_listing_rosters(Path(root))
    revision, regime_failure = None, "history_not_provided"
    if requested & (REGIME_FIELDS | {LISTING_SESSIONS_LOWER_BOUND}):
        try:
            revision = archive_revision(Path(root) / INDEX_ARCHIVE)
        except (OSError, sqlite3.Error, ValueError):
            regime_failure = "malformed_regime_archive"
    return QuantEvidenceSnapshot(requested, reference_rows, failure,
                                 MappingProxyType({symbol: tuple(items) for symbol, items in reference_by_symbol.items()}),
                                 MappingProxyType(financial), MappingProxyType(szse_manifests), MappingProxyType(sse_manifests),
                                 MappingProxyType(rosters), revision, regime_failure,
                                 MappingProxyType({key: tuple(items) for key, items in reference_by_symbol_field.items()}))


def regime_evidence_fact(regime: RegimeFact) -> EvidenceFact:
    revisions = (regime.rule_id, regime.evidence_hash) if regime.evidence_hash else (regime.rule_id,)
    return EvidenceFact(regime.value, regime.reason, revisions,
                        regime.available_at.isoformat() if regime.available_at else None)


def merge_st_evidence(original: EvidenceFact, observed: EvidenceFact, cutoff: datetime) -> EvidenceFact:
    if observed.reason is not None and observed.reason != "conflicting_evidence":
        return observed if original.reason == "history_not_provided" else original
    try:
        available = _timestamp(observed.available_at or "")
        verified = bool(observed.revision_ids) and available <= cutoff and available.date() == cutoff.date()
    except (TypeError, ValueError):
        verified = False
    if not verified or (observed.reason is None and type(observed.value) is not bool):
        return EvidenceFact(reason="unverified_st_observation") if original.reason == "history_not_provided" else original
    revisions = tuple(sorted(set(original.revision_ids) | set(observed.revision_ids)))
    if (original.reason == "conflicting_evidence" or observed.reason == "conflicting_evidence"
            or (original.reason is None and original.value != observed.value)):
        latest = max(value for value in (original.available_at, observed.available_at) if value is not None)
        return EvidenceFact(reason="conflicting_evidence", revision_ids=revisions, available_at=latest)
    if original.reason is None and original.value == observed.value:
        latest = max(value for value in (original.available_at, observed.available_at) if value is not None)
        return EvidenceFact(observed.value, None, revisions, latest)
    return observed


def read_input_evidence(root, cutoff, symbols, fields, *, raw_prices=None, snapshot=None):
    stamp = cutoff if isinstance(cutoff, datetime) else datetime.combine(cutoff, time.max, SHANGHAI)
    if stamp.tzinfo is None:
        raise ValueError("cutoff timestamp requires timezone")
    stamp = stamp.astimezone(SHANGHAI)
    day = stamp.date()
    requested = set(fields)
    symbols = sorted(set(symbols))
    facts = {}
    snapshot = snapshot if snapshot is not None else load_input_snapshot(root, requested)
    if not requested <= snapshot.requested_fields:
        raise ValueError("evidence snapshot does not cover requested fields")
    failure = snapshot.reference_failure
    regimes, regime_failure = {}, snapshot.regime_failure
    regime_symbols = set()
    if requested & REGIME_FIELDS:
        regime_symbols = {symbol for symbol in symbols if _REGIME_SYMBOL.fullmatch(symbol)}
    if regime_symbols and snapshot.regime_revision is not None:
        try:
            regimes = replay_many(Path(root) / INDEX_ARCHIVE, sorted(regime_symbols), stamp,
                                  revision=snapshot.regime_revision)
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            regime_failure = "malformed_regime_archive"
    st_board = {}
    if "is_st" in requested:
        day_key = day.isoformat()
        if (digest := snapshot.szse_st_manifests.get(day_key)) is not None:
            from app.services.tickdesk_szse_risk import SZSE_ST_ARCHIVE, read_szse_st_board

            st_board.update(read_szse_st_board(Path(root) / SZSE_ST_ARCHIVE / day_key, symbols, stamp,
                                               manifest_sha256=digest))
        if (digest := snapshot.sse_st_manifests.get(day_key)) is not None:
            from app.services.tickdesk_sse_risk import SSE_ST_ARCHIVE, read_sse_st_board

            st_board.update(read_sse_st_board(Path(root) / SSE_ST_ARCHIVE / day_key, symbols, stamp,
                                              manifest_sha256=digest))
    listing_bounds = {}
    if LISTING_SESSIONS_LOWER_BOUND in requested and day.isoformat() in snapshot.listing_roster_revisions:
        from app.services.tickdesk_listing_evidence import read_listing_bounds

        listing_bounds = read_listing_bounds(Path(root), symbols, stamp,
                                             snapshot.listing_roster_revisions[day.isoformat()], snapshot.regime_revision)
    eligible_calendars = {}

    def visible(rows):
        return [row for row in rows if len(row["available_at"]) > 10
                and _timestamp(row["available_at"]) <= stamp
                and date.fromisoformat(row["effective_date"]) <= day]

    def reference(symbol, field):
        rows = snapshot.reference_by_symbol_field.get((symbol, field), ())
        candidates = visible(rows)
        # Date-only facts need a calendar whose own availability is proven.
        if symbol not in eligible_calendars:
            eligible_calendars[symbol] = visible(snapshot.reference_by_symbol_field.get((symbol, "trading_sessions"), ()))
        calendar = eligible_calendars[symbol]
        calendars = []
        if calendar:
            latest_clock = max((r["effective_date"], r["available_at"]) for r in calendar)
            calendars = [r for r in calendar if (r["effective_date"], r["available_at"]) == latest_clock]
            values = {_digest(r["value"]) for r in calendars}
            if len(values) == 1 and calendars[0]["value"] is not None:
                cal = calendars[0]["value"]
                for row in rows:
                    if len(row["available_at"]) != 10:
                        continue
                    later = [d for d in cal["sessions"] if row["available_at"] < d <= day.isoformat()]
                    if cal["start"] <= row["available_at"] and later and row["effective_date"] <= day.isoformat():
                        candidates.append(row)
        if not candidates:
            return EvidenceFact(reason=failure)
        clock = max((r["effective_date"], r["available_at"]) for r in candidates)
        selected = [r for r in candidates if (r["effective_date"], r["available_at"]) == clock]
        # Content-bound calendar IDs include the exact availability/effective
        # clocks used to admit date-only facts. Timestamped facts need no calendar.
        supporting = calendars if len(clock[1]) == 10 else []
        ids = tuple(sorted({r["revision_id"] for r in [*selected, *supporting]}))
        if len({_digest(r["value"]) for r in selected}) != 1:
            return EvidenceFact(reason="conflicting_evidence", revision_ids=ids, available_at=clock[1])
        value = selected[0]["value"]
        return EvidenceFact(value, None if value is not None else "unknown_value", ids, clock[1])

    financial = {
        table: {symbol: next((row for row in reversed(by_symbol.get(symbol, ()))
                              if row["_available"] < day), None) for symbol in symbols}
        for table, by_symbol in snapshot.financial_rows.items()
    }
    for symbol in symbols:
        for field in sorted(requested):
            fact = EvidenceFact(reason="unsupported_field")
            if field in REFERENCE_FIELDS:
                fact = reference(symbol, field)
                if field == "is_st" and symbol in st_board:
                    fact = merge_st_evidence(fact, st_board[symbol], stamp)
            elif field in {"listing_sessions", LISTING_SESSIONS_LOWER_BOUND}:
                listing, calendar = reference(symbol, "listing_date"), reference(symbol, "trading_sessions")
                fact = EvidenceFact(reason="unknown_calendar_or_listing")
                if listing.reason is None and calendar.reason is None:
                    cal = calendar.value
                    if cal["start"] <= listing.value <= day.isoformat() <= cal["end"]:
                        count = sum(listing.value <= d <= day.isoformat() for d in cal["sessions"])
                        fact = EvidenceFact(count, None, tuple(sorted(listing.revision_ids + calendar.revision_ids)))
                if field == LISTING_SESSIONS_LOWER_BOUND and fact.reason is not None and symbol in listing_bounds:
                    roster_date, bound = listing_bounds[symbol]
                    if (listing.reason == "conflicting_evidence" or calendar.reason == "conflicting_evidence"
                            or (listing.reason is None and listing.value != roster_date)):
                        fact = EvidenceFact(reason="conflicting_listing_evidence")
                    else:
                        fact = bound
            elif field in REGIME_FIELDS:
                if symbol in regimes:
                    fact = regime_evidence_fact(regimes[symbol][field])
                else:
                    fact = EvidenceFact(reason=regime_failure if symbol in regime_symbols else "unsupported_regime_symbol")
            elif field in FINANCIAL_FIELDS or field in {"total_shares", "float_shares", "market_cap", "float_cap"}:
                table = "metrics" if field in FINANCIAL_FIELDS else "shares"
                column = FINANCIAL_FIELDS.get(field, {"market_cap": "total_shares", "float_cap": "float_shares"}.get(field, field))
                row = financial.get(table, {}).get(symbol)
                fact = EvidenceFact(reason="history_not_provided")
                if row is not None:
                    value = row[column]
                    identity = _digest({
                        "symbol": symbol, "field": column, "value": value,
                        "available": row["_available"], "announce": row["_announce"],
                        "contributor": row["_field_evidence"][column],
                    })
                    if table == "shares" and (value is None or value <= 0):
                        value = None
                    if field in {"market_cap", "float_cap"}:
                        price = (raw_prices or {}).get(symbol)
                        value = value * price if value is not None and price is not None and math.isfinite(price) and price > 0 else None
                        identity = _digest([identity, price])
                    elif field == "pb_latest":
                        price = (raw_prices or {}).get(symbol)
                        value = price / value if value is not None and value > 0 and price is not None and math.isfinite(price) and price > 0 else None
                        identity = _digest([identity, price])
                    fact = EvidenceFact(value, None if value is not None else "missing_value", (identity,),
                                        str(row["_available"]), row["_field_evidence"][column])
            facts[(symbol, field)] = fact
    identity = _digest([(k, v.value, v.reason, v.revision_ids, v.available_at) for k, v in sorted(facts.items())])
    return QuantInputEvidence(stamp.isoformat(), MappingProxyType(facts), identity)


def daily_decision_cutoff(day: date) -> datetime:
    return datetime.combine(day, time(15), SHANGHAI)


def assemble_quant_input_series(repo, fields, data, *, snapshot=None, not_after=None, daily_at_close=False):
    """Select facts on actual panel or matrix axes, sharing an optional snapshot.

    Keys preserve panel str(date/datetime) or matrix timestamp_labels. Datetimes
    without an offset are Shanghai wall time. Daily strategy decisions use the
    Shanghai close; callers reading dates as of day end keep that default.
    not_after excludes later axes (never relabels a daily close).
    Only each axis's raw_close is passed for derived market capitalization.
    """
    fields = tuple(fields)
    if not fields or data is None or (isinstance(data, pl.DataFrame) and data.is_empty()):
        return {}
    if isinstance(data, pl.DataFrame):
        clock = "datetime" if "datetime" in data.columns else "date"
        batches = []
        for frame in data.partition_by(clock, maintain_order=True):
            prices = dict(frame.select("symbol", "raw_close").iter_rows()) if "raw_close" in frame.columns else {}
            batches.append((str(frame[clock][0]), frame["symbol"].to_list(), prices))
    else:
        batches = [(label, data.symbols, {
            symbol: float(data.fields["raw_close"][index, column])
            for column, symbol in enumerate(data.symbols)
        } if "raw_close" in data.fields else {}) for index, label in enumerate(data.timestamp_labels)]
    if snapshot is None:
        snapshot = repo.get_quant_evidence_snapshot(fields)
    result = {}
    for label, symbols, prices in batches:
        if len(label) == 10:
            day = date.fromisoformat(label)
            cutoff = daily_decision_cutoff(day) if daily_at_close else day
        else:
            cutoff = datetime.fromisoformat(label)
        if isinstance(cutoff, datetime) and cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=SHANGHAI)
        stamp = cutoff if isinstance(cutoff, datetime) else datetime.combine(cutoff, time.max, SHANGHAI)
        if not_after is not None and stamp > not_after:
            continue
        result[label] = repo.get_quant_input_evidence(
            cutoff, symbols, fields, raw_prices=prices, snapshot=snapshot,
        )
    return result
