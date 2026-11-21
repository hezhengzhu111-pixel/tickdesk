"""A disposable process owns every CPU-heavy scan, including context construction."""
from __future__ import annotations

import contextlib
import json
import logging
import multiprocessing
import os
import sqlite3
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path

import psutil

ANALYSIS_RESEARCH_SECONDS = 12.0
_SCAN = threading.Lock()
_ANALYSIS_RESEARCH = threading.Lock()
_RESEARCH_CACHE: OrderedDict[tuple[str, str], tuple[bytes, tuple[str, str] | None]] = OrderedDict()
_ANALYSIS_CACHE: OrderedDict[tuple[str, str], tuple[bytes, tuple[str, str] | None]] = OrderedDict()


def _entry(pipe, target, args):
    logging.disable(logging.CRITICAL)
    os.environ["POLARS_MAX_THREADS"] = "2"
    os.environ["OMP_NUM_THREADS"] = "2"
    try:
        value = {"ok": True, "result": target(*args)}
        body = json.dumps(value, ensure_ascii=False, allow_nan=False, default=str).encode()
        if len(body) > 4 * 1024 * 1024:
            raise ValueError("result limit")
        pipe.send_bytes(body)
    except Exception:  # noqa: BLE001 -- sanitized failure protocol; raw paths and provider input stay local
        pipe.send_bytes(b'{"ok":false}')
    finally:
        pipe.close()


def run_bounded(target, args, *, timeout=5.0, memory_limit=1024 * 1024 * 1024):
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=_entry, args=(send, target, args), daemon=True)
    start = time.monotonic()
    process.start()
    send.close()
    try:
        while True:
            if receive.poll(0.02):
                result = json.loads(receive.recv_bytes(4 * 1024 * 1024))
                if not result["ok"]:
                    raise ValueError("scan worker failed")
                return result["result"]
            if time.monotonic() - start >= timeout:
                raise TimeoutError("scan deadline exceeded")
            if not process.is_alive():
                raise ValueError("scan worker exited")
            if psutil.Process(process.pid).memory_info().rss > memory_limit:
                raise MemoryError("scan memory limit exceeded")
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=1)
        if process.is_alive():
            process.kill()
            process.join(timeout=1)
        process.close()
        receive.close()


def _scan(data_dir, strategy_dir, request, approval, trading, overrides, execution_quotes):
    from app.services.tickdesk_adapter import MarketAdapter
    from app.strategy.engine import StrategyEngine
    from app.tickflow.repository import DataStore, KlineRepository

    store = DataStore.readonly(Path(data_dir))
    try:
        repo = KlineRepository(store)
        engine = StrategyEngine([Path(strategy_dir)])
        adapter = MarketAdapter(repo, engine, calendar=lambda day: trading, override_loader=lambda: overrides,
                                execution_quotes=execution_quotes)
        return adapter.scan(request, approval)
    finally:
        store.db.close()


def _remaining(deadline, limit_seconds=5.0):
    available = limit_seconds if deadline is None else min(limit_seconds, deadline - time.monotonic())
    if available <= 0:
        raise TimeoutError("adapter scan deadline expired")
    return available


def scan_in_worker(adapter, request, approval, *, trading, deadline=None):
    from app.services.tickdesk_adapter import fingerprint
    from app.services.tickdesk_admission import validate_approval
    from app.strategy.config import list_overrides

    if not _SCAN.acquire(blocking=False):
        raise ValueError("scan already running")
    try:
        root = adapter.repo.store.data_dir
        overrides = list_overrides(root)
        admitted = validate_approval(
            adapter.engine, root, approval, overrides, requested_at=request["requestedAt"], profile=request.get("profile"),
        )
        with tempfile.TemporaryDirectory(prefix="tickdesk-static-") as directory:
            destination = Path(directory)
            for _, _, sources in admitted.values():
                for source, body in sources.items():
                    output = destination / source.name
                    if output.exists() and output.read_bytes() != body:
                        raise ValueError("approved source name collision")
                    output.write_bytes(body)
            execution_quotes = {}
            if approval["version"] == 3:
                for spec in approval["strategies"]:
                    for symbol in spec["symbols"]:
                        if symbol in adapter.execution_quotes:
                            execution_quotes[symbol] = dict(adapter.execution_quotes[symbol])
            arguments = (str(root), directory, request, approval, trading, overrides, execution_quotes)
            result = run_bounded(_scan, arguments, timeout=_remaining(deadline))
        if (fingerprint(list_overrides(root)) != fingerprint(overrides)
                or any(path.read_bytes() != body for _, _, sources in admitted.values() for path, body in sources.items())):
            raise ValueError("admitted inputs changed during evaluation")
        return result
    finally:
        _SCAN.release()


def _scan_research(data_dir, strategy_dir, request, config, manifests, overrides, stage, deadline):
    from polars.exceptions import PolarsError

    from app.services.tickdesk_adapter import MarketAdapter
    from app.services.tickdesk_research import scan_research
    from app.services.tickdesk_shadow import MAX_PAYLOAD, _json, capture_evaluation
    from app.strategy.engine import StrategyEngine
    from app.tickflow.repository import DataStore, KlineRepository

    chunks = []
    total = 0
    capture_failed = False

    def capture(*args):
        nonlocal total, capture_failed
        if capture_failed or deadline - time.monotonic() < 0.75:
            capture_failed = True
            return
        try:
            packed = _json(capture_evaluation(*args))
        except (OSError, ValueError, TypeError, MemoryError, PolarsError):
            capture_failed = True
            return
        total += len(packed) + 1
        if total >= MAX_PAYLOAD:
            capture_failed = True
            return
        chunks.append(packed)

    store = DataStore.readonly(Path(data_dir))
    try:
        engine = StrategyEngine([Path(strategy_dir)])
        result = scan_research(
            MarketAdapter(KlineRepository(store), engine), request, config, manifests, overrides,
            capture=capture if stage else None,
        )
        if stage and not capture_failed and deadline - time.monotonic() >= 0.5:
            with contextlib.suppress(OSError):
                Path(stage).write_bytes(b"[" + b",".join(chunks) + b"]")
        return result
    finally:
        store.db.close()


def research_in_worker(adapter, request, config, *, deadline=None, analysis_symbols=None):
    from app.services.tickdesk_adapter import fingerprint
    from app.services.tickdesk_research import (
        read_research_config,
        research_data_identity,
        research_sources,
    )
    from app.services.tickdesk_shadow import archive_run, link_reused_run, observation_session
    from app.strategy.config import list_overrides

    on_demand = analysis_symbols is not None
    lock = _ANALYSIS_RESEARCH if on_demand else _SCAN
    cache = _ANALYSIS_CACHE if on_demand else _RESEARCH_CACHE
    if not lock.acquire(blocking=False):
        raise ValueError("scan already running")
    try:
        configured = config
        if analysis_symbols is not None:
            config = {**configured, "symbols": analysis_symbols}
        root = Path(adapter.repo.store.data_dir)
        cutoff = request["requestedAt"]
        overrides = list_overrides(root)
        revision = adapter.engine.definition_revision
        sources, manifests = research_sources(adapter.engine, config["strategies"])
        before = research_data_identity(adapter, config, cutoff)
        key = str(root.resolve()), fingerprint([config, manifests, overrides, before])
        session = None if on_demand else observation_session(request)
        archive_stamp = (request["epoch"], session) if session else None
        cached = cache.get(key)
        if cached and (not archive_stamp or cached[1] == archive_stamp):
            if session:
                try:
                    if not link_reused_run(root, request, session, key[1]):
                        logging.getLogger(__name__).warning("TickDesk cached research receipt unavailable")
                except (OSError, ValueError, TypeError, sqlite3.Error) as error:
                    logging.getLogger(__name__).warning("TickDesk cached research receipt unavailable (%s)", type(error).__name__)
            cache.move_to_end(key)
            return {"version": 3, "decisionCutoffMs": cutoff, "observations": json.loads(cached[0])}
        committed_archive = None
        with tempfile.TemporaryDirectory(prefix="tickdesk-research-") as directory:
            for source, body in sources.items():
                output = Path(directory) / source.name
                if output.exists() and output.read_bytes() != body:
                    raise ValueError("research source name collision")
                output.write_bytes(body)
            stage = Path(directory) / "sealed-evaluations.json"
            available = _remaining(deadline, ANALYSIS_RESEARCH_SECONDS if on_demand else 5.0)
            result = run_bounded(
                _scan_research, (str(root), directory, request, config, manifests, overrides,
                                 str(stage) if session else None, time.monotonic() + available),
                timeout=available,
            )
            if (adapter.engine.definition_revision != revision or read_research_config(root) != configured
                    or list_overrides(root) != overrides
                    or any(path.read_bytes() != body for path, body in sources.items())
                    or research_data_identity(adapter, config, cutoff) != before):
                raise ValueError("research inputs changed during evaluation")
            if session and (deadline is None or deadline - time.monotonic() >= 0.5):
                try:
                    if archive_run(root, request, session, key[1], stage, config, sources, manifests, result):
                        committed_archive = archive_stamp
                except (OSError, ValueError, TypeError, MemoryError, sqlite3.Error) as error:
                    logging.getLogger(__name__).warning("TickDesk research archive unavailable (%s)", type(error).__name__)
        cache[key] = json.dumps(result["observations"], allow_nan=False).encode(), committed_archive
        if len(cache) > 8:
            cache.popitem(last=False)
        return result
    finally:
        lock.release()
