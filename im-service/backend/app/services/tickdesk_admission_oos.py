"""Freeze a forward held-out window and verify its native backtest evidence."""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, time
from pathlib import Path

from app.backtest.evidence import json_digest, load_evidence
from app.services.tickdesk_adapter import CN
from app.services.tickdesk_regime_archive import INDEX_ARCHIVE, lookup

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PLANS = "tickdesk-oos"
_LIMIT = 64 * 1024


def _now():
    return datetime.now(CN)


def _clock(value):
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("the review requires an explicit observation clock")
    return parsed.astimezone(CN)


def _archive(root, reference):
    if not isinstance(reference, dict) or set(reference) != {"runId", "sha256"}:
        raise ValueError("native backtest reference required")
    report = load_evidence(root, reference["runId"])
    config = report["config"]
    if (json_digest(report) != reference["sha256"] or json_digest(config) != report["config_sha256"]
            or config["mode"] != "position" or config["asset_type"] != "stock"
            or report["strategy"]["backend"] != "matrix_native" or report["strategy"]["changed_during_run"] is not False):
        raise ValueError("backtest archive identity differs")
    return report


def _identity(report):
    config = report["config"]
    settings = {key: value for key, value in config.items() if key not in {"start", "end", "entry_open_timing"}}
    if clock := config.get("entry_open_timing"):
        settings["entry_open_timing"] = {key: clock[key] for key in ("model", "response_delay_ms")}
    return json_digest([report["strategy"]["sha256"], report["runtime"], settings])


def _plan_path(root, sha):
    if not isinstance(sha, str) or not _SHA256.fullmatch(sha):
        raise ValueError("invalid plan digest")
    path = Path(root).resolve() / _PLANS / f"{sha}.json"
    if path.resolve() != path:
        raise ValueError("linked plan archive path")
    return path


def _calendar(root, at):
    record = lookup(Path(root) / INDEX_ARCHIVE, "calendar", "fuyao", at).record
    if record is None:
        raise ValueError("no archived exchange calendar at the review time")
    body = json.loads(record.body)
    if (record.observation.observed_at > at
            or body["through"] != record.observation.observed_at.astimezone(CN).date().isoformat()):
        raise ValueError("calendar coverage and receipt clock differ")
    return record.observation.data


def _completed(calendar, anchor, at):
    day = at.date().isoformat()
    return [session for session in calendar if anchor < session
            and (session < day or (session == day and at.time() >= time(15, 30)))]


def register_oos(root, selection, *, gap_sessions, test_sessions, min_trades, calendar_root=None):
    """Register a forward-only fixed-parameter research plan at the actual local clock."""
    training = _archive(root, selection)
    config = training["config"]
    span = max(config["holding_days"], config["params"].get("max_hold_bars", 0))
    if not (type(gap_sessions) is int and span <= gap_sessions <= 180
            and type(test_sessions) is int and 1 <= test_sessions <= 180
            and type(min_trades) is int and min_trades > 0):
        raise ValueError("holding horizon, embargo, test sessions or sample size is invalid")
    registered = _now()
    anchor = registered.date().isoformat()
    calendar = _calendar(calendar_root or root, registered)
    matrix = training["data"]["datasets"]["daily_matrix"]
    if (not config["start"] < config["end"] <= anchor
            or config["start"] not in calendar or config["end"] not in calendar
            or matrix["end"] != config["end"] or _clock(training["created_at"]) >= registered):
        raise ValueError("selection run is outside the observed development window")
    plan = {
        "version": 1, "registeredAt": registered.isoformat(), "anchorDay": anchor, "selection": selection,
        "strategyId": config["strategy_id"], "symbols": config["symbols"], "identity": _identity(training),
        "selectionStart": config["start"], "selectionEnd": config["end"],
        "gapSessions": gap_sessions, "testSessions": test_sessions, "minTrades": min_trades,
    }
    sha = json_digest(plan)
    path = _plan_path(root, sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(plan, stream, ensure_ascii=False, sort_keys=True, indent=2)
    return {"sha256": sha, "plan": plan}


def inspect_oos(root, selection, evidence, review_at, *, calendar_root=None):
    state = {"blockers": [], "gap": [], "window": [], "observedTestSessions": 0, "sampleTrades": None}

    def blocked(reason):
        return {**state, "blockers": [reason]}

    if not isinstance(evidence, dict) or "planSha256" not in evidence:
        return blocked("DISJOINT_OOS_EVIDENCE_UNAVAILABLE")
    try:
        review = datetime.fromtimestamp(review_at / 1000, CN)
        path = _plan_path(root, evidence["planSha256"])
        if not 0 < path.stat().st_size <= _LIMIT:
            raise ValueError("invalid plan size")
        plan = json.loads(path.read_text(encoding="utf-8"))
        if json_digest(plan) != evidence["planSha256"] or plan["version"] != 1:
            raise ValueError("changed plan")
        registered = _clock(plan["registeredAt"])
        gap, count, minimum = (plan[name] for name in ("gapSessions", "testSessions", "minTrades"))
        if (registered >= review or plan["anchorDay"] != registered.date().isoformat()
                or any(type(value) is not int for value in (gap, count, minimum))
                or not 0 <= gap <= 180 or not 1 <= count <= 180 or not minimum > 0):
            raise ValueError("plan chronology or coverage")
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return blocked("OOS_PLAN_UNREADABLE")
    if plan["selection"] != selection:
        return blocked("OOS_PLAN_SELECTION_MISMATCH")
    try:
        calendar = _calendar(calendar_root or root, review)
        if not calendar[0] <= plan["anchorDay"]:
            raise ValueError("calendar no longer covers the registration boundary")
    except (OSError, ValueError, TypeError, KeyError, OverflowError, sqlite3.Error):
        return blocked("OOS_CALENDAR_UNAVAILABLE")
    observed = _completed(calendar, plan["anchorDay"], review)
    state["gap"], state["window"] = observed[:gap], observed[gap:gap + count]
    state["observedTestSessions"] = len(state["window"])
    if len(state["window"]) < count:
        return blocked("OOS_SESSIONS_PENDING")
    if "result" not in evidence:
        return blocked("OOS_RESULT_REQUIRED")
    try:
        window = state["window"]
        result = _archive(root, evidence["result"])
        config = result["config"]
        recorded = _clock(result["created_at"])
        matrix = result["data"]["datasets"]["daily_matrix"]
        actual = [day for day in matrix["sessions"] if window[0] <= day <= window[-1]]
        if (_identity(result) != plan["identity"] or config["start"] != window[0] or config["end"] != window[-1]
                or matrix["end"] != window[-1] or actual != window
                or recorded < _clock(f"{window[-1]}T15:30:00+08:00") or recorded >= review):
            return blocked("OOS_RESULT_IDENTITY_MISMATCH")
        try:
            result_calendar = _calendar(calendar_root or root, recorded)
        except (OSError, ValueError, TypeError, KeyError, OverflowError, sqlite3.Error):
            return blocked("OOS_RESULT_CALENDAR_UNVERIFIED")
        if _completed(result_calendar, plan["anchorDay"], recorded)[:gap + count] != [*state["gap"], *window]:
            return blocked("OOS_RESULT_CALENDAR_UNVERIFIED")
        state["sampleTrades"] = result["result_summary"]["n_trades"]
        if type(state["sampleTrades"]) is not int or state["sampleTrades"] < minimum:
            return blocked("OOS_TRADE_SAMPLE_INSUFFICIENT")
        audit = result["execution_timing"]
        clock = audit.get("minute_evidence") or {}
        if (audit["status"] != "passed" or clock.get("admission_eligible") is not True
                or clock.get("matcher_ran") is not True
                or clock.get("exit", {}).get("count") != state["sampleTrades"]
                or (clock.get("entry", {}).get("count") or 0) < state["sampleTrades"]):
            return blocked("OOS_EXECUTION_UNVERIFIED")
        lookahead = result.get("lookahead") or {}
        if lookahead.get("status") != "passed" or lookahead.get("differences") != 0:
            return blocked("OOS_CAUSAL_AUDIT_INCONCLUSIVE")
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return blocked("OOS_RESULT_UNREADABLE")
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--calendar-root", type=Path)
    parser.add_argument("--selection-id", required=True)
    parser.add_argument("--selection-sha256", required=True)
    actions = parser.add_subparsers(dest="action", required=True)
    register = actions.add_parser("register")
    register.add_argument("--gap-sessions", required=True, type=int)
    register.add_argument("--test-sessions", required=True, type=int)
    register.add_argument("--min-trades", required=True, type=int)
    inspect = actions.add_parser("inspect")
    inspect.add_argument("--plan-sha256", required=True)
    inspect.add_argument("--result-id")
    inspect.add_argument("--result-sha256")
    args = parser.parse_args()
    selection = {"runId": args.selection_id, "sha256": args.selection_sha256}
    if args.action == "register":
        output = register_oos(args.root, selection, gap_sessions=args.gap_sessions, test_sessions=args.test_sessions,
                              min_trades=args.min_trades, calendar_root=args.calendar_root)
    else:
        reference = {"planSha256": args.plan_sha256}
        if args.result_id or args.result_sha256:
            reference["result"] = {"runId": args.result_id, "sha256": args.result_sha256}
        output = inspect_oos(args.root, selection, reference, int(_now().timestamp() * 1000),
                             calendar_root=args.calendar_root)
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
