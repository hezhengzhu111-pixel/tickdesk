"""Prepare sourced daily inputs for the existing research worker, never approvals."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import time as clock
from collections import OrderedDict
from datetime import date, datetime, time, timedelta
from functools import partial

import polars as pl

from app.services.tickdesk_adapter import CN, fingerprint, timestamp
from app.services.tickdesk_regime_archive import INDEX_ARCHIVE, import_calendar, lookup
from app.services.tickdesk_research import BAR_COLUMNS, FINALITY, read_research_config

logger = logging.getLogger(__name__)


def _completed_through(now):
    return now.date() if now.time() >= time(15, 30) else now.date() - timedelta(days=1)


def _decode_calendar(body, now):
    try:
        if len(body) > 65_536:
            return None
        calendar = json.loads(body)
        sessions = calendar["sessions"]
        through = date.fromisoformat(calendar["through"])
        if (type(calendar["observedAt"]) is int and 0 <= now - calendar["observedAt"] <= 3 * 86_400_000
                and isinstance(sessions, list) and 0 < len(sessions) <= 370 and sessions == sorted(set(sessions))
                and all(date.fromisoformat(day).isoformat() == day and day <= through.isoformat() for day in sessions)):
            return calendar
    except (ValueError, KeyError, TypeError):
        pass
    return None


def read_calendar_snapshot(root, now):
    path = root / "tickdesk-market" / "calendar.json"
    try:
        with path.open("rb") as source:
            body = source.read(65_537)
    except OSError:
        return None
    calendar = _decode_calendar(body, now)
    return (body, calendar) if calendar else None


def read_calendar(root, now):
    snapshot = read_calendar_snapshot(root, now)
    return snapshot[1] if snapshot else None


def calendar_verdict(root, now):
    """Return a trading-day verdict only inside an observed provider calendar."""
    local = datetime.fromtimestamp(now / 1000, CN)
    day = local.date().isoformat()
    calendar = read_calendar(root, now)
    if calendar and calendar["sessions"][0] <= day <= calendar["through"]:
        return day in calendar["sessions"]
    archived = _archived_scan_calendar(root, now, local)
    return day in archived["sessions"] if archived else None


def _verified_scan_calendar(calendar, day):
    if not calendar or calendar.get("source") != "fuyao":
        return False
    observed_at = calendar["observedAt"]
    through = calendar["through"]
    return (observed_at > 0 and through == datetime.fromtimestamp(observed_at / 1000, CN).date().isoformat()
            and calendar["sessions"][0] <= day <= through)


def _archived_scan_calendar(root, cutoff, local):
    try:
        archived = lookup(root / INDEX_ARCHIVE, "calendar", "fuyao", local)
        calendar = _decode_calendar(archived.record.body, cutoff) if archived.record else None
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        return None
    return calendar if _verified_scan_calendar(calendar, local.date().isoformat()) else None


def scan_calendar_evidence(root, cutoff, *, calendar_override=None):
    """Read scan eligibility at the original Shanghai request cutoff."""
    local = datetime.fromtimestamp(cutoff / 1000, CN)
    day = local.date().isoformat()
    evidence = {"date": day, "state": "unknown", "observedAt": None, "through": None, "source": None}
    if calendar_override is not None:
        # In-process fixtures have a verdict but do not supply provider provenance.
        verdict = calendar_override(local.date())
        if verdict is True:
            evidence["state"] = "open"
        elif verdict is False:
            evidence["state"] = "closed"
        return evidence

    cached = read_calendar(root, cutoff)
    if not _verified_scan_calendar(cached, day):
        cached = _archived_scan_calendar(root, cutoff, local)
        if not cached:
            return evidence

    # Mirror the scheduled Scan + manual Resume window in the local service.
    minute = local.hour * 60 + local.minute
    in_window = local.weekday() < 5 and (565 <= minute <= 695 or 775 <= minute <= 905)
    state = "open" if in_window and day in cached["sessions"] else "closed"
    return {"date": day, "state": state, "observedAt": cached["observedAt"],
            "through": cached["through"], "source": cached["source"]}


def _write_calendar(root, source, arrived, sessions):
    from app.services.fs_utils import atomic_write_text

    if not sessions:
        raise ValueError("provider calendar unavailable")
    calendar = {"observedAt": timestamp(arrived), "through": str(arrived.astimezone(CN).date()),
                "sessions": sorted(sessions), "source": source}
    path = root / "tickdesk-market" / "calendar.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(calendar)
    atomic_write_text(path, body)
    if source == "fuyao":
        raw = body.encode("utf-8")
        try:
            import_calendar(root / INDEX_ARCHIVE, raw, hashlib.sha256(raw).hexdigest())
        except (OSError, sqlite3.Error, ValueError) as error:
            # The local archive worker can retry from the just-published cache.
            logger.warning("TickDesk calendar archive write failed (%s)", type(error).__name__)


def _refresh_calendar(root):
    from app.data_providers import custom
    from app.services import preferences

    now = datetime.now(CN)
    calendar = read_calendar(root, timestamp(now))
    if calendar and calendar["through"] >= str(now.date()):
        return
    source = preferences.get_daily_data_provider()
    provider = custom.get_provider(source)
    fetch = getattr(provider, "trading_days", None)
    if callable(fetch):
        sessions = {str(day) for day in fetch()}
        arrived = datetime.now(CN)
        # The provider's query is relative to its current day. A response that
        # spans midnight cannot establish which local day its range ends on.
        if arrived.date() == now.date():
            _write_calendar(root, source, arrived, sessions)


def completed_bar_witnesses(frame, observed_at, *, source, sessions):
    if observed_at.tzinfo is None or not source:
        raise ValueError("source and aware arrival required")
    observed_at = observed_at.astimezone(CN)
    rows, keys = [], set()
    for row in frame.iter_rows(named=True):
        day = str(row["date"])[:10]
        close_at = datetime.fromisoformat(day + "T15:00:00+08:00")
        if day not in sessions or observed_at < close_at + timedelta(minutes=30):
            continue
        key = day, row["symbol"]
        values = [float(row[name]) for name in BAR_COLUMNS]
        if key in keys or not all(math.isfinite(v) and v >= 0 for v in values):
            raise ValueError("invalid or duplicate completed bar")
        if not (0 < row["low"] <= min(row["open"], row["close"])
                <= max(row["open"], row["close"]) <= row["high"]
                and 0 < row["raw_low"] <= row["raw_close"] <= row["raw_high"]):
            raise ValueError("invalid completed OHLC")
        keys.add(key)
        value = {"end_at": close_at.isoformat(), "bar_hash": fingerprint([day, row["symbol"], *values])}
        if "open_input" in row:
            raw_open = float(row["open_input"])
            if not row["raw_low"] <= raw_open <= row["raw_high"]:
                raise ValueError("invalid raw open")
            value["raw_open"] = raw_open
        rows.append({"symbol": row["symbol"], "field": FINALITY, "source": source,
                     "effective_date": day, "available_at": observed_at.isoformat(), "value": value})
    return rows


def new_witnesses(rows, existing):
    latest = {}
    for row in existing:
        if row["field"] != FINALITY:
            continue
        key = row["symbol"], row["effective_date"], row["source"]
        old = latest.get(key)
        if old is None or row["available_at"] > old["available_at"]:
            latest[key] = row
    return [row for row in rows if (old := latest.get((row["symbol"], row["effective_date"], row["source"]))) is None
            or old["value"] != row["value"]]


def refresh_research_data(repo, *, now=None, chart_symbols=None):
    from app.data_providers import custom
    from app.indicators.pipeline import run_pipeline
    from app.services import preferences
    from app.services.fs_utils import atomic_write_parquet, atomic_write_text
    from app.services.instrument_sync import _flatten_instruments, _preserve_instrument_fields

    root = repo.store.data_dir
    research = chart_symbols is None
    config = read_research_config(root) if research else {"symbols": sorted(chart_symbols)}
    if config is None:
        return {"status": "disabled", "reason": "RESEARCH_NOT_CONFIGURED"}
    symbols = config["symbols"]
    if len(symbols) > 10:
        raise ValueError("RESEARCH_DATA_SCOPE_LIMIT")
    now = now or datetime.now(CN)
    through = _completed_through(now)
    source_name = preferences.get_daily_data_provider()
    identity = fingerprint([3, config, source_name, str(through)])
    state_path = root / "tickdesk-market" / "state.json"
    if research:
        try:
            previous = json.loads(state_path.read_text(encoding="utf-8"))
            if previous.get("identity") == identity and 0 <= now.timestamp() - previous["updatedAt"] / 1000 < 6 * 3600:
                return previous
        except (OSError, ValueError, KeyError, TypeError):
            pass
    provider = custom.get_provider(source_name)
    fetch = getattr(provider, "get_research_daily", None)
    if not callable(fetch):
        raise ValueError("RESEARCH_DATA_PROVIDER_UNSUPPORTED")
    start = datetime.combine(through - timedelta(days=300), time(), CN)
    end = datetime.combine(through, time(), CN)
    bundle = fetch(symbols, start, end)
    arrived = bundle["observed_at"]
    sessions = {str(day) for day in bundle["sessions"]}
    if sessions:
        _write_calendar(root, bundle["source"], arrived, sessions)
    daily = bundle["daily"].filter(pl.col("date") <= through)
    if daily.is_empty() or (research and set(daily["symbol"].unique()) != set(symbols)):
        raise ValueError("RESEARCH_DAILY_MISSING")
    if daily.select(pl.struct("date", "symbol").n_unique()).item() != daily.height:
        raise ValueError("RESEARCH_DAILY_DUPLICATE")
    # The source's calendar, not weekdays, determines which bars can be sealed.
    daily = daily.filter(pl.col("date").cast(pl.String).is_in(sessions))
    if daily.is_empty():
        raise ValueError("RESEARCH_CALENDAR_MISSING")
    expected_last = max((day for day in sessions if str(start.date()) <= day <= str(through)), default=None)
    latest = daily.group_by("symbol").agg(pl.col("date").max())
    if research and (expected_last is None or latest.height != len(symbols)
                     or any(str(day) != expected_last for day in latest["date"])):
        # A delayed response (or suspension without separate evidence) must be
        # retried next cycle instead of locking yesterday's data into a 6h cache.
        raise ValueError("RESEARCH_LATEST_SESSION_MISSING")
    raw_hash = fingerprint(daily.to_dicts())
    rows = _flatten_instruments(bundle["instruments"])
    originals = {row["symbol"]: row for row in bundle["instruments"]}
    for row in rows:
        original = originals[row["symbol"]]
        row.update(source=bundle["source"], as_of=arrived.isoformat(),
                   board=original.get("board", "unknown"), status=original.get("status", "unknown"))
    master = _preserve_instrument_fields(pl.DataFrame(rows), root)
    atomic_write_parquet(master, root / "instruments" / "instruments.parquet")
    repo.append_daily(daily)
    factors = bundle["factors"]
    factor_path = root / "adj_factor" / "all.parquet"
    if factor_path.exists():
        retained = pl.read_parquet(factor_path).filter(
            ~(pl.col("symbol").is_in(symbols) & pl.col("trade_date").is_between(start.date(), through)))
        factors = pl.concat([retained, factors], how="diagonal_relaxed")
    atomic_write_parquet(factors, factor_path)
    run_pipeline(data_dir=root, symbols=symbols)
    repo.rebuild_views()
    repo.refresh_cache(background=False)
    first, last = daily["date"].min(), daily["date"].max()
    enriched = repo.get_enriched_range(first, last, symbols=symbols)
    if enriched is None or enriched.is_empty():
        raise ValueError("RESEARCH_ENRICHED_MISSING")
    # Only rows observed in this source response may be attributed to it.
    matching = enriched.join(daily.select("date", "symbol", "open", "close", "high", "low", "volume", "amount"),
                             on=["date", "symbol"], how="inner", suffix="_input")
    for output, original in (("raw_close", "close_input"), ("raw_high", "high_input"),
                             ("raw_low", "low_input"), ("volume", "volume_input"), ("amount", "amount_input")):
        if matching.filter(pl.col(output) != pl.col(original)).height:
            raise ValueError("RESEARCH_INPUT_CHANGED")
    witnesses = completed_bar_witnesses(matching, arrived, source=bundle["source"], sessions=sessions)
    if not witnesses:
        raise ValueError("RESEARCH_COMPLETED_BAR_MISSING")
    snapshot = repo.get_quant_evidence_snapshot({FINALITY})
    repo.write_quant_reference_evidence(new_witnesses(witnesses, snapshot.reference_rows))
    result = {"status": "ready", "identity": identity, "source": bundle["source"],
              "updatedAt": int(arrived.timestamp() * 1000), "through": str(last),
              "symbols": symbols, "dailyRows": daily.height, "witnesses": len(witnesses), "rawHash": raw_hash}
    if research:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(state_path, json.dumps(result, ensure_ascii=False))
    else:
        result["symbols"] = sorted({row["symbol"] for row in witnesses})
    return result


async def _run(app):
    while True:
        def refresh():
            repo = app.state.repo
            if repo is not None:
                try:
                    _refresh_calendar(repo.store.data_dir)
                except Exception as error:  # noqa: BLE001 -- research still has an independent calendar fetch
                    logger.warning("TickDesk calendar refresh failed (%s)", type(error).__name__)
            return refresh_research_data(repo)

        async with app.state.tickdesk_daily_lock:
            await _publish_daily(app, refresh, research=True)
        await asyncio.sleep(300)


async def _publish_daily(app, fetch, *, research=False):
    from starlette.concurrency import run_in_threadpool

    publication = asyncio.create_task(run_in_threadpool(fetch))
    try:
        result = await asyncio.shield(publication)
        if research:
            app.state.tickdesk_market_data = result
        return result
    except asyncio.CancelledError:
        # Thread cancellation does not stop file writes. Drain the publisher
        # before the outer lifespan closes the repository/releases its lock.
        await asyncio.gather(publication, return_exceptions=True)
        raise
    except Exception as error:  # noqa: BLE001 -- isolate a failing optional provider
        logger.warning("TickDesk daily data preparation failed (%s)", type(error).__name__)
        if research:
            app.state.tickdesk_market_data = {"status": "failed", "reason": "RESEARCH_DATA_UNAVAILABLE"}
        return {}


class ChartPreparation:
    """Coalesces detail requests and current authoritative workspace scope."""
    def __init__(self):
        self.explicit = {}
        self.scope = set()
        self.attempts = OrderedDict()
        self.loading = set()
        self.wake = asyncio.Event()

    def update_scope(self, symbols):
        self.scope = {s for s in symbols[:1000] if re.fullmatch(r"[0-9]{6}\.(SH|SZ)", s)}
        self.wake.set()

    def request(self, symbol):
        if symbol in self.loading:
            return "CHART_PREPARING"
        if self._recent(symbol):
            return "CHART_PREPARATION_FAILED"
        if len(self.explicit) >= 100 and symbol not in self.explicit:
            return "CHART_PREPARATION_FAILED"
        self.explicit[symbol] = None
        self.wake.set()
        return "CHART_PREPARING"

    def _recent(self, symbol):
        entry = self.attempts.get(symbol)
        if entry is None:
            return False
        attempted, day, ready = entry
        through = _completed_through(datetime.now(CN))
        return clock.monotonic() - attempted < (6 * 3600 if ready and day == through else 300)

    def start(self):
        batch = [s for s in dict.fromkeys((*self.explicit, *sorted(self.scope))) if not self._recent(s)][:10]
        for symbol in batch:
            self.explicit.pop(symbol, None)
        self.loading = set(batch)
        return batch

    def finish(self, ready):
        day = _completed_through(datetime.now(CN))
        now = clock.monotonic()
        for symbol in self.loading:
            self.attempts.pop(symbol, None)
            self.attempts[symbol] = (now, day, symbol in ready)
        while len(self.attempts) > 2000:
            self.attempts.popitem(last=False)
        self.loading.clear()


async def _run_chart_preparation(app, demand):
    from starlette.concurrency import run_in_threadpool

    from app.services.tickdesk_adapter import MarketAdapter
    from app.services.tickdesk_fund_chart import refresh_fund_charts

    while True:
        await demand.wake.wait()
        demand.wake.clear()
        batch = demand.start()
        if not batch:
            continue
        repo = app.state.repo
        now = timestamp(datetime.now(CN))
        try:
            catalog = await run_in_threadpool(MarketAdapter(repo, None).symbols, now)
            available = {row["symbol"]: row["instrument"] for row in catalog}
        except Exception:  # noqa: BLE001 -- leave existing chart data readable
            available = {}
        stocked = [symbol for symbol in batch if available.get(symbol) == "stock"]
        funds = [symbol for symbol in batch if available.get(symbol) == "etf"]
        ready = set()
        if stocked:
            async with app.state.tickdesk_daily_lock:
                result = await _publish_daily(app, partial(refresh_research_data, repo, chart_symbols=stocked))
            ready.update(result.get("symbols", []))
        if funds:
            result = await _publish_daily(app, partial(refresh_fund_charts, repo, funds))
            ready.update(result.get("symbols", []))
        demand.finish(ready)
        await asyncio.sleep(30)
        demand.wake.set()


async def _run_master(app):
    from starlette.concurrency import run_in_threadpool

    from app.services.tickdesk_exchange_master import refresh_exchange_master
    from app.services.tickdesk_fund_chart import refresh_fund_master

    while True:
        update = asyncio.create_task(run_in_threadpool(refresh_exchange_master, app.state.repo.store.data_dir))
        try:
            count = await asyncio.shield(update)
            app.state.tickdesk_exchange_master = {"status": "ready", "count": count}
        except asyncio.CancelledError:
            await asyncio.gather(update, return_exceptions=True)
            raise
        except Exception as error:  # noqa: BLE001 -- separate availability of official listings
            app.state.tickdesk_exchange_master = {"status": "failed"}
            logger.warning("TickDesk official listing refresh failed (%s)", type(error).__name__)
        await _publish_daily(app, partial(refresh_fund_master, app.state.repo.store.data_dir))
        await asyncio.sleep(300)


async def _run_regime_collection(app, collector):
    while True:
        status = await _publish_daily(app, collector.step)
        app.state.tickdesk_regime_collection = status or {"status": "failed", "reason": "collection_worker_unavailable"}
        await asyncio.sleep(300)


async def _run_szse_risk_snapshot(app, collector):
    while True:
        status = await _publish_daily(app, collector.step)
        app.state.tickdesk_szse_risk = status or {"status": "failed", "reason": "snapshot_worker_unavailable"}
        await asyncio.sleep(300)


@contextlib.asynccontextmanager
async def market_data_lifespan(app):
    if os.getenv("TICKDESK_QUANT_DATA_ENABLED") != "1":
        yield
        return
    from app.services.tickdesk_daily_history import run_daily_history
    from app.services.tickdesk_quote_feed import QuoteFeed
    from app.services.tickdesk_regime_worker import RegimeCollection
    from app.services.tickdesk_szse_risk_worker import SzseRiskSnapshots

    app.state.tickdesk_daily_lock = asyncio.Lock()
    demand = app.state.tickdesk_chart_preparation = ChartPreparation()
    workers = [asyncio.create_task(_run(app), name="tickdesk-research-data"),
               asyncio.create_task(_run_chart_preparation(app, demand), name="tickdesk-chart-preparation"),
               asyncio.create_task(_run_master(app), name="tickdesk-master"),
               asyncio.create_task(run_daily_history(app), name="tickdesk-daily-history")]
    if getattr(app.state, "repo", None) is not None:
        quotes = app.state.tickdesk_quote_feed = QuoteFeed(app.state.repo.store.data_dir)
        workers.append(asyncio.create_task(quotes.run(), name="tickdesk-quotes"))
        collector = RegimeCollection(app.state.repo, lambda: demand.scope)
        app.state.tickdesk_regime_collection = {"status": "starting" if collector.enabled else "disabled"}
        workers.append(asyncio.create_task(_run_regime_collection(app, collector), name="tickdesk-regime-collection"))
        risk_capture = SzseRiskSnapshots(app.state.repo.store.data_dir)
        app.state.tickdesk_szse_risk = {"status": "starting" if risk_capture.enabled else "disabled"}
        if risk_capture.enabled:
            workers.append(asyncio.create_task(_run_szse_risk_snapshot(app, risk_capture), name="tickdesk-szse-risk"))
    try:
        yield
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
