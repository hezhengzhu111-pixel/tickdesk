"""Bulk raw daily history for the current TickDesk stock universe."""
from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import time
from bisect import bisect_left, bisect_right
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from datetime import time as wall_time
from functools import partial
from pathlib import Path
from threading import Event

import polars as pl

from app.data_providers import custom
from app.plugins.fuyao.client import FuyaoError
from app.services import preferences
from app.services import tickdesk_daily_attribution as daily_attribution
from app.services.fs_utils import atomic_write_parquet, atomic_write_text
from app.services.kline_sync import (
    CANONICAL_DAILY_COLS,
    publish_staged_daily_chunks,
    stage_daily_chunks,
)
from app.services.tickdesk_adapter import CN, fingerprint, timestamp
from app.services.tickdesk_exchange_master import read_exchange_master
from app.services.tickdesk_market_data import _completed_through, read_calendar

logger = logging.getLogger(__name__)
HISTORY_DAYS = 300
_REFRESH_DAYS = 7
_BOARDS = {"沪主板", "深主板", "创业板"}
_SOURCE_WINDOW_UNAVAILABLE = "MARKET_HISTORY_PROVIDER_WINDOW_UNAVAILABLE"


class HistoryUnavailableError(ValueError):
    """There is no source-backed history covering the requested window."""


def _history_plan(root, now):
    cutoff = timestamp(now)
    calendar = read_calendar(root, cutoff)
    through = _completed_through(now)
    source = preferences.get_daily_data_provider()
    if not calendar or calendar.get("source") != source or calendar["through"] < through.isoformat():
        raise HistoryUnavailableError("MARKET_HISTORY_CALENDAR_UNAVAILABLE")
    all_days = [date.fromisoformat(value) for value in calendar["sessions"]]
    anchor = max((day for day in all_days if day <= through), default=None)
    if anchor is None or all_days[0] > anchor - timedelta(days=HISTORY_DAYS - 7):
        raise HistoryUnavailableError("MARKET_HISTORY_CALENDAR_COVERAGE")
    days = [day for day in all_days if anchor - timedelta(days=HISTORY_DAYS) <= day <= anchor]
    listings = {row["symbol"]: row["listing_date"] for row in read_exchange_master(root, cutoff)
                if row["board"] in _BOARDS and row["status"] == "listed"}
    if not 0 < len(listings) <= 8000:
        raise HistoryUnavailableError("MARKET_HISTORY_UNIVERSE_UNAVAILABLE")
    return (source, days, listings)


def _previous_state(state_path, cells_path, identity, through):
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if (state["identity"] == identity and state["through"] <= through and isinstance(state["dates"], Mapping)
                and cells_path.is_file()):
            return state
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _stage_history(stage, source, sessions, listings, check_cancel):
    # This worker must never switch to thousands of individual requests when a
    # bulk entitlement or a release file is missing.
    if not custom.provider_has_dataset(source, "daily"):
        raise HistoryUnavailableError("MARKET_HISTORY_PROVIDER_UNAVAILABLE")
    reader = getattr(custom.get_provider(source), "iter_daily", None)
    if not callable(reader) or source != "fuyao":
        raise HistoryUnavailableError("MARKET_HISTORY_BULK_UNSUPPORTED")
    observed = {day: set() for day in sessions}
    prices = ["open", "high", "low", "close"]

    def normalized():
        chunks = reader(sorted(listings), datetime.combine(sessions[0], wall_time(), CN),
                        datetime.combine(sessions[-1], wall_time(), CN), bulk_only=True, check_cancel=check_cancel)
        for frame in chunks:
            check_cancel()
            if frame.is_empty():
                continue
            invalid = (~pl.col("date").is_in(sessions)
                       | ~pl.col("symbol").is_in(list(listings))
                       | pl.any_horizontal([~pl.col(key).is_finite() | (pl.col(key) <= 0) for key in prices])
                       | pl.any_horizontal([~pl.col(key).is_finite() | (pl.col(key) < 0) for key in ("volume", "amount")])
                       | (pl.col("low") > pl.min_horizontal("open", "close"))
                       | (pl.col("high") < pl.max_horizontal("open", "close")))
            if not set(CANONICAL_DAILY_COLS).issubset(frame.columns) or frame.filter(invalid.fill_null(True)).height:
                raise HistoryUnavailableError("MARKET_HISTORY_INVALID_BARS")
            accepted = []
            for (day,), block in frame.select("date", "symbol").partition_by("date", as_dict=True).items():
                symbols = set(block["symbol"].to_list())
                seen = observed[day]
                if len(symbols) != block.height or not seen.isdisjoint(symbols):
                    raise HistoryUnavailableError("MARKET_HISTORY_DUPLICATE_BARS")
                symbols = {symbol for symbol in symbols if listings[symbol] <= day}
                accepted.extend((day, symbol) for symbol in sorted(symbols))
                seen.update(symbols)
            if len(accepted) != frame.height:
                keys = pl.DataFrame(accepted, schema={"date": pl.Date, "symbol": pl.String}, orient="row")
                frame = frame.join(keys, on=["date", "symbol"], how="semi")
            yield frame.select(CANONICAL_DAILY_COLS)

    try:
        stage_daily_chunks(normalized(), stage, partition=False)
    except FuyaoError as error:
        if error.code == "BULK_DAILY_COVERAGE_UNAVAILABLE":
            raise HistoryUnavailableError(_SOURCE_WINDOW_UNAVAILABLE) from None
        raise
    if any(not symbols for symbols in observed.values()):
        raise HistoryUnavailableError("MARKET_HISTORY_SESSION_MISSING")
    return observed


def _publish_history(repo, stage, state_path, cells_path, identity, source, previous, days, expected_through, listings,
                     observed, observed_at, check_cancel):
    conflicts = publish_staged_daily_chunks(stage, repo, days=observed, check_cancel=check_cancel)
    for day, symbols in conflicts.items():
        observed[day].difference_update(symbols)
    check_cancel()
    source_time = timestamp(datetime.now(CN))
    updated_days = list(observed)
    cells = pl.DataFrame({"date": [day for day, values in observed.items() for _ in values],
                          "symbol": [symbol for values in observed.values() for symbol in sorted(values)]},
                         schema={"date": pl.Date, "symbol": pl.String})
    cells = cells.with_columns(pl.lit(observed_at).alias("observedAt"))
    if previous:
        retained = pl.read_parquet(cells_path).filter(
            pl.col("date").is_in(days) & ~pl.col("date").is_in(updated_days))
        cells = pl.concat([retained, cells], how="vertical")
    date_counts = dict(cells.group_by("date").len().iter_rows())
    listing_days = sorted(listings.values())
    daily_coverage = {day.isoformat(): {"observed": date_counts.get(day, 0),
                                      "expected": bisect_right(listing_days, day)} for day in days}
    symbol_counts = dict(cells.group_by("symbol").len().iter_rows())
    symbol_coverage = {
        symbol: {"observed": symbol_counts.get(symbol, 0),
                 "expected": len(days) - bisect_left(days, listed)}
        for symbol, listed in sorted(listings.items())
    }
    if not repo.refresh_view("kline_daily", f"{repo.store.data_dir.as_posix()}/kline_daily/**/*.parquet"):
        raise HistoryUnavailableError("MARKET_HISTORY_REPOSITORY_VIEW_UNAVAILABLE")
    old_conflicts = previous.get("overlapConflictsByDate", {}) if previous else {}
    conflict_counts = {
        day.isoformat(): len(conflicts.get(day, ())) if day in observed else old_conflicts.get(day.isoformat(), 0)
        for day in days
    }
    result = {"version": 1, "identity": identity, "source": source, "basis": "none", "updatedAt": source_time,
              "start": days[0].isoformat(), "through": days[-1].isoformat(),
              "expectedThrough": expected_through.isoformat(),
              "requestedSymbols": len(listings), "observedSymbols": len(symbol_counts), "rows": cells.height,
              "conflictingRows": sum(conflict_counts.values()), "overlapConflictsByDate": conflict_counts,
              "status": "raw_ready" if days[-1] == expected_through
              and all(row["observed"] == row["expected"] for row in daily_coverage.values()) else "raw_partial",
              "dates": daily_coverage, "symbols": symbol_coverage}
    atomic_write_parquet(cells.sort("date", "symbol"), cells_path)
    result = daily_attribution.enrich(repo.store.data_dir, result)
    atomic_write_text(state_path, json.dumps(result))
    return result


async def _drain_apply(call, cancel):
    task = asyncio.create_task(asyncio.to_thread(call))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancel.set()
        await asyncio.gather(task, return_exceptions=True)
        raise


async def refresh_daily_history(repo, publish_lock, *, now=None):
    """Import one bounded snapshot, or extend an established snapshot from bulk cache."""
    now = now or datetime.now(CN)
    root = repo.store.data_dir
    source, days, listings = _history_plan(root, now)
    expected_through = days[-1]
    identity = fingerprint([3, source, sorted((symbol, day.isoformat()) for symbol, day in listings.items())])
    stamp = preferences.data_source_stamp()
    directory = root / "tickdesk-market"
    state_path = directory / "history-coverage.json"
    cells_path = directory / "history-cells.parquet"
    previous = _previous_state(state_path, cells_path, identity, days[-1].isoformat())
    partitions = root / "kline_daily"
    if previous and any(not (partitions / f"date={value}" / "part.parquet").is_file()
                        for value in previous["dates"]):
        previous = None
    if previous and previous["through"] == days[-1].isoformat() and previous["start"] == days[0].isoformat():
        enriched = daily_attribution.enrich(root, previous)
        if enriched is not previous:
            atomic_write_text(state_path, json.dumps(enriched))
        return enriched
    retained = previous
    tail_start = days[-1] - timedelta(days=_REFRESH_DAYS)
    if previous and date.fromisoformat(previous["through"]) < tail_start:
        previous = None
    if previous and any(day.isoformat() not in previous["dates"] for day in days if day < tail_start):
        previous = None
    selected = [day for day in days if previous is None or day >= tail_start]
    cancel = Event()
    deadline = time.monotonic() + 900

    def check_cancel():
        if cancel.is_set() or time.monotonic() > deadline:
            raise HistoryUnavailableError("MARKET_HISTORY_CANCELLED_OR_EXPIRED")

    staging_base = root / ".daily_sync_staging"
    staging_base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=staging_base, prefix="history-") as scratch:
        stage = Path(scratch) / "requested"
        try:
            observed = await _drain_apply(partial(_stage_history, stage, source, selected, listings, check_cancel), cancel)
        except HistoryUnavailableError as error:
            if str(error) != _SOURCE_WINDOW_UNAVAILABLE:
                raise
            if stamp != preferences.data_source_stamp():
                raise HistoryUnavailableError("MARKET_HISTORY_SOURCE_CHANGED") from None
            if retained is not None:
                return {**retained, "status": "raw_partial", "expectedThrough": expected_through.isoformat(),
                        "refreshReason": _SOURCE_WINDOW_UNAVAILABLE}
            if len(days) < 2:
                raise
            # An unpublished closing session does not prevent a first install
            # from importing the older complete provider window.
            days = days[:-1]
            stage = Path(scratch) / "previous-session"
            observed = await _drain_apply(partial(_stage_history, stage, source, days, listings, check_cancel), cancel)
        observed_at = timestamp(datetime.now(CN))
        async with publish_lock:
            if stamp != preferences.data_source_stamp():
                raise HistoryUnavailableError("MARKET_HISTORY_SOURCE_CHANGED")
            return await _drain_apply(
                partial(_publish_history, repo, stage, state_path, cells_path, identity, source, previous, days,
                        expected_through, listings, observed, observed_at, check_cancel), cancel)


def public_attribution(value):
    """Whitelist the archived suspension attribution for the v2 coverage payload."""
    if not isinstance(value, Mapping):
        return None
    try:
        window, sources = value["window"], value["sources"]
        status = value["status"]
        if status not in {"all_attributed", "partial"}:
            return None
        return {
            "status": status,
            "window": {"start": str(window["start"]), "end": str(window["end"]),
                       "sessions": int(window["sessions"])},
            "missingCells": int(value["missingCells"]),
            "attributedCells": int(value["attributedCells"]),
            "unexplainedCells": int(value["unexplainedCells"]),
            "sources": {key: int(sources[key]) for key in ("sseDays", "szseDays", "archiveDays")},
            "archiveSignature": str(value["archiveSignature"]),
            "unexplained": [{"day": str(row["day"]), "symbol": str(row["symbol"])}
                            for row in value["unexplained"][:1000]],
        }
    except (KeyError, TypeError, ValueError):
        return None


def coverage_diagnostic(result, *, include_attribution=False):
    invalid = {"status": "unavailable", "coverage": None, "reason": "snapshot_invalid"}
    if not isinstance(result, Mapping) or not isinstance(result.get("status"), str):
        return invalid
    status = result.get("status")
    if status in {"raw_ready", "raw_partial"}:
        keys = ("source", "start", "through", "expectedThrough", "updatedAt", "rows", "observedSymbols",
                "requestedSymbols", "conflictingRows")
        try:
            coverage = {key: result[key] for key in keys}
            coverage["latestDay"] = result.get("latestDay") or result["dates"][result["through"]]
        except (KeyError, TypeError):
            return invalid
        if include_attribution:
            coverage["attribution"] = public_attribution(result.get("attribution"))
        return {"status": "ready" if status == "raw_ready" else "partial", "coverage": coverage,
                "reason": "provider_pending" if result.get("refreshReason") == _SOURCE_WINDOW_UNAVAILABLE else None}
    if status == "disabled":
        return {"status": "disabled", "coverage": None, "reason": None}
    reason = {"unavailable": "source_unavailable", "failed": "refresh_failed"}.get(status, "history_pending")
    return {"status": "unavailable", "coverage": None, "reason": reason}


async def run_daily_history(app):
    delay = 30
    while True:
        root = app.state.repo.store.data_dir
        try:
            try:
                previous = json.loads((Path(root) / daily_attribution.STATE_PATH).read_text(encoding="utf-8"))
                previous_days = list(previous.get("dates") or {})
            except (OSError, ValueError):
                previous_days = []
            if previous_days:
                try:
                    await daily_attribution.ensure_archive(root, previous_days)
                except Exception as error:  # noqa: BLE001 -- optional official source
                    logger.warning("Exchange suspension archive refresh failed (%s)", type(error).__name__)
            result = await refresh_daily_history(app.state.repo, app.state.tickdesk_daily_lock)
            if "attribution" not in result:
                result = daily_attribution.attach(root, result)
            summary = {key: result[key] for key in (
                "status", "source", "start", "through", "expectedThrough", "requestedSymbols", "observedSymbols",
                "rows", "updatedAt",
                "conflictingRows", "refreshReason", "attribution",
            ) if key in result}
            summary["latestDay"] = result["dates"][result["through"]]
            app.state.tickdesk_history = summary
            delay = 300
        except HistoryUnavailableError as error:
            app.state.tickdesk_history = {"status": "unavailable", "reason": str(error)}
            delay = 30 if str(error) in {"MARKET_HISTORY_UNIVERSE_UNAVAILABLE", "MARKET_HISTORY_CALENDAR_UNAVAILABLE"} else 300
        except Exception as error:  # noqa: BLE001 -- isolate optional vendor and disk failure
            logger.warning("TickDesk full-market daily history refresh failed (%s)", type(error).__name__)
            app.state.tickdesk_history = {"status": "failed", "reason": "MARKET_HISTORY_REFRESH_FAILED"}
            delay = min(delay * 2, 3600)
        await asyncio.sleep(delay)
