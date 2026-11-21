"""Sealed inputs for replaying v3 research runs without production effects."""
from __future__ import annotations

import base64
import gzip
import io
import json
import re
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from types import MappingProxyType

import polars as pl

from app.quant_evidence import EvidenceFact, QuantInputEvidence, _freeze, _plain
from app.services.tickdesk_adapter import CN, fingerprint, timestamp

MAX_PAYLOAD = 24 * 1024 * 1024
MAX_ARCHIVE = 256 * 1024 * 1024
MAX_RUNS = 2_000
MAX_SCAN_RECEIPTS = 100_000
_SHA = re.compile(r"[a-f0-9]{64}")
_SOURCE = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,100}\.py")


def observation_session(request):
    at = request.get("requestedAt")
    if (request.get("leaseAcquired") is not True
            or not all(isinstance(request.get(field), str) and 0 < len(request[field]) <= 64
                       for field in ("scanLease", "epoch")) or type(at) is not int
            or not 0 <= time.time_ns() // 1_000_000 - at <= 8_000):
        return None
    return datetime.fromtimestamp(at / 1000, CN).date().isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def capture_evaluation(strategy, context, pool, params, overrides, result, bar_proofs):
    if context.history.estimated_size() > MAX_PAYLOAD // 2:
        raise ValueError("research history exceeds archive limit")
    history = io.BytesIO()
    context.history.write_ipc(history, compression="zstd")
    return {
        "strategy": strategy, "asset": context.asset_type, "day": str(context.as_of),
        "pool": pool, "parameters": params, "overrides": overrides, "generation": context.data_generation,
        "history": base64.b64encode(history.getvalue()).decode("ascii"),
        "barFinality": _plain(bar_proofs),
        "inputs": {
            day: {"cutoff": row.cutoff, "identity": row.identity, "facts": [
                [symbol, field, _plain(fact.value), fact.reason, list(fact.revision_ids),
                 fact.available_at, fact.financial_contributor]
                for (symbol, field), fact in row.facts.items()
            ]} for day, row in context.quant_inputs.items()
        },
        "resultHash": fingerprint(result) if result is not None else None,
    }


def runtime_identity():
    from app.backtest.evidence import runtime_version

    root = Path(__file__).resolve().parents[1]
    modules = ("backtest/matrix.py", "quant_evidence.py", "services/tickdesk_research.py",
               "strategy/engine.py", "strategy/quant_panel.py")
    sources = {name: fingerprint((root / name).read_bytes()) for name in modules}
    return fingerprint([runtime_version(), sources])


def _archive(root):
    return Path(root) / "research" / "tickdesk-shadow" / "history.sqlite3"


def _summary(result):
    rows = result["observations"]
    by_strategy = {}
    for row in rows:
        counts = by_strategy.setdefault(row["strategyId"], {"entryKnown": 0, "exitKnown": 0})
        counts["entryKnown"] += row["entry"]["value"] is not None
        counts["exitKnown"] += row["exit"]["value"] is not None
    return {
        "observations": len(rows),
        "entryKnown": sum(row["entry"]["value"] is not None for row in rows),
        "exitKnown": sum(row["exit"]["value"] is not None for row in rows),
        "completeBarDays": sorted({
            datetime.fromtimestamp(row["bar"]["endMs"] / 1000, CN).date().isoformat()
            for row in rows if row["bar"]["status"] == "complete"
        }),
        "strategies": by_strategy,
    }


def _insert_scan_receipt(db, request, session, archive_hash):
    cutoff = request["requestedAt"]
    recorded = time.time_ns() // 1_000_000
    if not 0 <= recorded - cutoff <= 8_000:
        return False
    lease = fingerprint(request["scanLease"].encode())
    db.execute("""CREATE TABLE IF NOT EXISTS scan_receipts (
        epoch TEXT NOT NULL, session TEXT NOT NULL, cutoff INTEGER NOT NULL,
        lease_hash TEXT NOT NULL, recorded_at INTEGER NOT NULL, run_hash TEXT NOT NULL REFERENCES runs(hash),
        PRIMARY KEY(epoch,session,cutoff,lease_hash))""")
    key = request["epoch"], session, cutoff, lease
    existing = db.execute(
        "SELECT run_hash FROM scan_receipts WHERE epoch=? AND session=? AND cutoff=? AND lease_hash=?", key,
    ).fetchone()
    if existing is not None:
        return existing == (archive_hash,)
    count, = db.execute("SELECT COUNT(*) FROM scan_receipts").fetchone()
    if count >= MAX_SCAN_RECEIPTS:
        return False
    db.execute("INSERT INTO scan_receipts VALUES (?,?,?,?,?,?)", (*key, recorded, archive_hash))
    return True


def _leased_run_for_input(db, request, session, input_identity):
    return db.execute("""SELECT hash FROM runs WHERE epoch=? AND session=? AND input_key=? AND cutoff<=?
                         AND json_extract(summary,'$.origin')='leased_scan'""",
                      (request["epoch"], session, input_identity, request["requestedAt"])).fetchone()


def link_reused_run(root, request, session, input_identity):
    """Give a later leased scan its own receipt for a previously sealed input."""
    path = _archive(root)
    if not path.is_file():
        return False
    with closing(sqlite3.connect(path, timeout=.05)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        saved = _leased_run_for_input(db, request, session, input_identity)
        return bool(saved and _insert_scan_receipt(db, request, session, saved[0]))


def archive_run(root, request, session, input_identity, stage, config, sources, manifests, result, *,
                origin=None, calendar=None) -> bool:
    """Return whether this session's input has a durable research archive."""
    if not stage.is_file() or stage.stat().st_size > MAX_PAYLOAD:
        return False
    evaluations = json.loads(stage.read_bytes())
    recorded_at = time.time_ns() // 1_000_000
    if origin is None:
        origin = "leased_scan" if request.get("leaseAcquired") is True else "legacy_unclassified"
    recorded_session = datetime.fromtimestamp(recorded_at / 1000, CN).date().isoformat()
    if recorded_at < request["requestedAt"] or (origin == "local_forward" and recorded_session != session):
        return False
    summary = {"recordedAtMs": recorded_at, "origin": origin, **_summary(result)}
    body = _json({
        "version": 1, "stage": "research", "cutoff": request["requestedAt"], "epoch": request["epoch"],
        "recordedAtMs": recorded_at, "origin": origin, "calendar": calendar,
        "session": session, "runtime": runtime_identity(), "observations": result["observations"],
        "leaseHash": fingerprint(request["scanLease"].encode()) if origin == "leased_scan" else None,
        "inputIdentity": input_identity, "selection": config,
        "evaluations": evaluations, "manifests": manifests,
        "sources": {path.name: base64.b64encode(data).decode("ascii") for path, data in sources.items()},
    })
    if len(body) > MAX_PAYLOAD:
        return False
    key = fingerprint(body)
    compressed = gzip.compress(body, compresslevel=1, mtime=0)
    path = _archive(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=.05)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS runs (
            hash TEXT PRIMARY KEY, epoch TEXT NOT NULL, session TEXT NOT NULL, input_key TEXT NOT NULL,
            cutoff INTEGER NOT NULL, summary TEXT NOT NULL, payload BLOB NOT NULL,
            UNIQUE(epoch, session, input_key))""")
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT hash FROM runs WHERE epoch=? AND session=? AND input_key=?",
                              (request["epoch"], session, input_identity)).fetchone()
        if existing is not None:
            if origin == "leased_scan":
                saved = _leased_run_for_input(db, request, session, input_identity)
                return bool(saved and _insert_scan_receipt(db, request, session, saved[0]))
            return True
        count, size = db.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(payload)),0) FROM runs").fetchone()
        if count >= MAX_RUNS or size + len(compressed) > MAX_ARCHIVE:
            return False
        db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
                   (key, request["epoch"], session, input_identity, request["requestedAt"], _json(summary), compressed))
        if origin == "leased_scan":
            _insert_scan_receipt(db, request, session, key)
    return True


def capture_observation(data_dir: Path, archive_dir: Path, *, cutoff: datetime | None = None) -> dict:
    """Seal local market data independently of a production scan lease."""
    from app.services.tickdesk_adapter import MarketAdapter
    from app.services.tickdesk_market_data import read_calendar_snapshot
    from app.services.tickdesk_research import (
        read_research_config,
        research_data_identity,
        research_sources,
        strategy_directories,
    )
    from app.services.tickdesk_worker import _scan_research, run_bounded
    from app.strategy.config import list_overrides
    from app.strategy.engine import StrategyEngine
    from app.tickflow.repository import DataStore, KlineRepository

    archive_path = _archive(archive_dir).resolve()
    if archive_path.is_relative_to(data_dir.resolve()):
        raise ValueError("research capture needs an archive outside the source data directory")
    now = time.time_ns() // 1_000_000
    if cutoff is not None and cutoff.utcoffset() is None:
        raise ValueError("research replay cutoff needs a timezone")
    at = now if cutoff is None else timestamp(cutoff)
    if at > now:
        raise ValueError("research replay cannot use a future cutoff")
    session = datetime.fromtimestamp(at / 1000, CN).date().isoformat()
    origin = "local_forward" if cutoff is None else "historical_replay"
    calendar = None
    if cutoff is None:
        current = read_calendar_snapshot(data_dir, at)
        if current is None or session not in current[1]["sessions"]:
            raise ValueError("research forward capture requires a currently observed trading session")
        calendar = {"sha256": fingerprint(current[0]), "observedAtMs": current[1]["observedAt"],
                    "through": current[1]["through"], "source": current[1].get("source")}

    config = read_research_config(data_dir)
    if config is None:
        raise ValueError("research configuration unavailable")
    overrides = list_overrides(data_dir)
    engine = StrategyEngine(strategy_directories(data_dir))
    sources, manifests = research_sources(engine, config["strategies"])
    store = DataStore.readonly(data_dir)
    request = {"requestedAt": at, "epoch": f"{origin}-v1"}
    try:
        adapter = MarketAdapter(KlineRepository(store), engine)
        before = research_data_identity(adapter, config, at)
        with tempfile.TemporaryDirectory(prefix="tickdesk-shadow-observe-") as directory:
            staged = Path(directory)
            for source, data in sources.items():
                target = staged / source.name
                if target.exists() and target.read_bytes() != data:
                    raise ValueError("research source name collision")
                target.write_bytes(data)
            stage = staged / "sealed-evaluations.json"
            budget = 12.0
            result = run_bounded(
                _scan_research, (str(data_dir), directory, request, config, manifests, overrides,
                                 str(stage), time.monotonic() + budget),
                timeout=budget,
            )
            if (research_data_identity(adapter, config, at) != before
                    or read_research_config(data_dir) != config or list_overrides(data_dir) != overrides
                    or research_sources(StrategyEngine(strategy_directories(data_dir)), config["strategies"])
                    != (sources, manifests)):
                raise ValueError("research inputs changed during evaluation")
            if calendar is not None:
                if read_calendar_snapshot(data_dir, at) != current:
                    raise ValueError("research calendar changed during evaluation")
                if session not in _summary(result)["completeBarDays"]:
                    raise ValueError("research forward capture requires same-session finalized bars")
            if not result["observations"]:
                raise ValueError("research produced no observations")
            input_identity = fingerprint([config, manifests, overrides, before, result["observations"],
                                          at if cutoff is not None else None, runtime_identity()])
            if not archive_run(archive_dir, request, session, input_identity, stage, config, sources, manifests,
                               result, origin=origin, calendar=calendar):
                raise ValueError("research observation could not be sealed")
    finally:
        store.db.close()
    with closing(sqlite3.connect(f"{archive_path.as_uri()}?mode=ro", uri=True)) as db:
        row = db.execute("SELECT hash,epoch,session,cutoff,summary FROM runs WHERE epoch=? AND session=? AND input_key=?",
                         (request["epoch"], session, input_identity)).fetchone()
    return _stored_summary(row)


def _stored_summary(row):
    key, epoch, session, cutoff, summary = row
    return {"hash": key, "epoch": epoch, "session": session, "cutoffMs": cutoff, "stage": "research",
            "origin": "legacy_unclassified", **json.loads(summary)}


def list_runs(root, limit=30):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("research archive query limit")
    path = _archive(root)
    if not path.is_file():
        return []
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        return [
            _stored_summary(row) for row in db.execute(
                "SELECT hash,epoch,session,cutoff,summary FROM runs ORDER BY cutoff DESC,hash LIMIT ?", (limit,))
        ]


def _replay(payload):
    from app.services.tickdesk_research import _verified_row
    from app.strategy.engine import StrategyDataContext, StrategyEngine

    if payload["runtime"] != runtime_identity():
        raise ValueError("research replay requires the recorded engine version")
    with tempfile.TemporaryDirectory(prefix="tickdesk-replay-") as directory:
        root = Path(directory)
        for name, body in payload["sources"].items():
            if not _SOURCE.fullmatch(name):
                raise ValueError("invalid research source name")
            (root / name).write_bytes(base64.b64decode(body, validate=True))
        engine = StrategyEngine([root])
        compared = 0
        incomplete = 0
        for row in payload["evaluations"]:
            if row["resultHash"] is None:
                incomplete += 1
                continue
            strategy = engine.get(row["strategy"])
            if fingerprint(strategy.file_path.read_bytes()) != payload["manifests"][row["strategy"]]["codeHash"]:
                raise ValueError("research source hash mismatch")
            history = pl.read_ipc(io.BytesIO(base64.b64decode(row["history"], validate=True)), memory_map=False)
            witnesses = {(proof["effective_date"], proof["symbol"]): proof for proof in row["barFinality"]}
            if (len(witnesses) != history.height or len(row["barFinality"]) != history.height
                    or any(not (0 < timestamp(proof["value"]["end_at"]) <= timestamp(proof["available_at"])
                                <= payload["cutoff"]) for proof in witnesses.values())
                    or any(_verified_row(bar, witnesses) is None for bar in history.iter_rows(named=True))):
                raise ValueError("research bar history does not match point-in-time evidence")
            ends_by_day = {}
            for (day, _), proof in witnesses.items():
                end = timestamp(proof["value"]["end_at"])
                ends_by_day[day] = min(end, ends_by_day.get(day, end))
            if ({day: timestamp(item["cutoff"]) for day, item in row["inputs"].items()} != ends_by_day):
                raise ValueError("research fundamentals use a different decision clock")
            session = date.fromisoformat(row["day"])
            inputs = {
                day: QuantInputEvidence(item["cutoff"], MappingProxyType({
                    (symbol, field): EvidenceFact(_freeze(value), reason, tuple(revisions), available, contributor)
                    for symbol, field, value, reason, revisions, available, contributor in item["facts"]
                }), item["identity"]) for day, item in row["inputs"].items()
            }
            context = StrategyDataContext(
                row["asset"], "1d", session, current=history.filter(pl.col("date") == session), history=history,
                data_generation=row["generation"], quant_inputs=inputs, current_quant_inputs=inputs,
                quant_evidence=inputs[row["day"]], evidence_identity=inputs[row["day"]].identity,
            )
            result = engine.run(row["strategy"], context, pool=row["pool"], params=row["parameters"],
                                overrides=row["overrides"]).quant_observations
            if fingerprint(result) != row["resultHash"]:
                raise ValueError("research replay changed a verdict")
            compared += 1
    status = ("partial" if incomplete else "matched") if compared else "unverifiable"
    return {"status": status, "evaluationsMatched": compared,
            "evaluationsFailedAtCapture": incomplete, "stage": "research",
            "origin": payload.get("origin", "legacy_unclassified"), "cutoffMs": payload["cutoff"],
            "recordedAtMs": payload["recordedAtMs"], **_summary(payload)}


def read_run(root, key):
    if not isinstance(key, str) or not _SHA.fullmatch(key):
        raise ValueError("invalid research archive hash")
    path = _archive(root)
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=.15)) as db:
        row = db.execute(
            "SELECT epoch, session, cutoff, payload FROM runs WHERE hash=? AND LENGTH(payload)<=?",
            (key, MAX_PAYLOAD),
        ).fetchone()
    if row is None:
        raise ValueError("research archive unavailable")
    epoch, session, cutoff, packed = row
    with gzip.GzipFile(fileobj=io.BytesIO(packed)) as stream:
        raw = stream.read(MAX_PAYLOAD + 1)
    if len(raw) > MAX_PAYLOAD or fingerprint(raw) != key:
        raise ValueError("research archive hash mismatch")
    payload = json.loads(raw)
    if (not isinstance(payload, dict) or payload.get("version") != 1 or payload.get("stage") != "research"
            or (payload.get("epoch"), payload.get("session"), payload.get("cutoff")) != (epoch, session, cutoff)):
        raise ValueError("research archive schema mismatch")
    return payload


def replay_run(root, key):
    from app.services.tickdesk_worker import run_bounded

    payload = read_run(root, key)
    return run_bounded(_replay, (payload,), timeout=5.0)
