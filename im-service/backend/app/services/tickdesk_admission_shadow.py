"""Check a strategy's recorded shadow evidence before production admission review."""
from __future__ import annotations

import base64
import re
import sqlite3
from datetime import datetime

from app.services.tickdesk_adapter import CN, fingerprint, timestamp
from app.services.tickdesk_shadow import read_run, runtime_identity

_SHA = re.compile(r"[0-9a-f]{64}\Z")


def _day(milliseconds):
    return datetime.fromtimestamp(milliseconds / 1000, CN).date().isoformat()


def _valid_sha(value):
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def _inspect(payload, spec, frozen_sources, review_at, reviewed_runtime):
    blockers = set()
    sid = spec["id"]
    session, cutoff, recorded = (payload[key] for key in ("session", "cutoff", "recordedAtMs"))
    origin = payload.get("origin", "legacy_unclassified")
    if (type(cutoff) is not int or type(recorded) is not int or not 0 < cutoff <= recorded < review_at
            or _day(cutoff) != session or _day(recorded) != session
            or (origin == "leased_scan" and recorded - cutoff > 8_000)):
        blockers.add("SHADOW_OBSERVATION_CLOCK_MISMATCH")
    if origin != "leased_scan" or not _valid_sha(payload.get("leaseHash")):
        blockers.add("SHADOW_FORWARD_LEASE_REQUIRED")
    if payload["runtime"] != reviewed_runtime:
        blockers.add("SHADOW_REVIEW_RUNTIME_CHANGED")
    if any(base64.b64decode(payload["sources"].get(name, ""), validate=True) != body
           for name, body in frozen_sources.items()):
        blockers.add("SHADOW_REVIEW_IDENTITY_MISMATCH")

    rows = [row for row in payload["observations"] if row["strategyId"] == sid]
    evaluations = [row for row in payload["evaluations"] if row["strategy"] == sid]
    expected_symbols = sorted(spec["symbols"])
    if (sorted(payload["selection"]["symbols"]) != expected_symbols
            or sorted(row["symbol"] for row in rows) != expected_symbols):
        blockers.add("SHADOW_REVIEW_SCOPE_MISMATCH")
    if len(evaluations) != 1 or not _valid_sha(evaluations[0].get("resultHash")):
        blockers.add("SHADOW_STRATEGY_EVALUATION_MISSING")
        return session, None, origin, blockers, 0
    evaluation = evaluations[0]
    decision_day = evaluation["day"]
    if sorted(evaluation["pool"]) != expected_symbols:
        blockers.add("SHADOW_REVIEW_SCOPE_MISMATCH")
    if (any(row[key] != spec[key] for row in rows for key in ("codeHash", "dependenciesHash", "parametersHash"))
            or fingerprint({"params": evaluation["parameters"], "overrides": evaluation["overrides"]})
            != spec["parametersHash"]):
        blockers.add("SHADOW_REVIEW_IDENTITY_MISMATCH")

    side = "entry" if spec["direction"] == "buy" else "exit"
    known = sum(type(row[side]["value"]) is bool for row in rows)
    if known != len(expected_symbols):
        blockers.add("SHADOW_DIRECTION_EVIDENCE_INCOMPLETE")
    proofs = {(item["effective_date"], item["symbol"]): item for item in evaluation["barFinality"]}
    if len(proofs) != len(evaluation["barFinality"]):
        blockers.add("SHADOW_BAR_EVIDENCE_MISMATCH")
    complete_ends = []
    for row in rows:
        bar = row["bar"]
        proof = proofs.get((decision_day, row["symbol"]))
        if (bar["status"] != "complete" or proof is None
                or not (0 < bar["endMs"] <= bar["availableAtMs"] <= cutoff)
                or _day(bar["endMs"]) != decision_day
                or timestamp(proof["value"]["end_at"]) != bar["endMs"]
                or timestamp(proof["available_at"]) != bar["availableAtMs"]
                or proof["revision_id"] != row["evidence"]["barRevision"]
                or proof["source"] != row["evidence"]["barSource"]):
            blockers.add("SHADOW_BAR_EVIDENCE_MISMATCH")
        else:
            complete_ends.append(bar["endMs"])
    facts = evaluation["inputs"].get(decision_day)
    if complete_ends and (
        facts is None or timestamp(facts["cutoff"]) != min(complete_ends)
        or any(not 0 < (timestamp(fact[5]) or 0) <= min(complete_ends)
               for fact in facts["facts"] if fact[5] is not None)
    ):
        blockers.add("SHADOW_POINT_IN_TIME_FACT_MISMATCH")
    return session, decision_day, origin, blockers, known


def review_shadow(root, evidence, spec, sources, review_at):
    """Read hash-addressed shadow runs and report which independent sessions qualify."""
    hashes = evidence.get("runHashes") if isinstance(evidence, dict) else None
    if (not isinstance(hashes, list) or not 1 <= len(hashes) <= 4
            or not all(_valid_sha(key) for key in hashes) or len(set(hashes)) != len(hashes)):
        return {"blockers": ["SHADOW_EVIDENCE_UNAVAILABLE"], "qualifiedSessions": [],
                "qualifiedDecisionDays": [], "runs": []}

    frozen_sources = {path.name: body for path, body in sources.items()}
    engine = runtime_identity()
    sessions, decision_days = set(), set()
    blockers, runs = set(), []
    for key in hashes:
        try:
            payload = read_run(root, key)
            session, day, origin, gaps, known = _inspect(payload, spec, frozen_sources, review_at, engine)
        except (OSError, EOFError, ValueError, KeyError, TypeError, OverflowError, sqlite3.Error):
            session, day, origin, known = None, None, None, None
            gaps = {"SHADOW_RUN_UNREADABLE"}
        blockers.update(gaps)
        if not gaps:
            sessions.add(session)
            decision_days.add(day)
        runs.append({"hash": key, "session": session, "decisionDay": day, "origin": origin,
                     "knownDirection": known, "blockers": sorted(gaps)})
    if len(sessions) < 2 or len(decision_days) < 2:
        blockers.add("SHADOW_OBSERVATION_DAYS_INSUFFICIENT")
    return {"blockers": sorted(blockers), "qualifiedSessions": sorted(sessions),
            "qualifiedDecisionDays": sorted(decision_days), "runs": runs}
