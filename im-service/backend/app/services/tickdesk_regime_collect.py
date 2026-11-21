"""Explicit, bounded Fuyao collection for the offline regime research archive."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo

import httpx

from app.plugins.fuyao.client import FuyaoClient, FuyaoError
from app.services.fs_utils import atomic_write_text
from app.services.tickdesk_regime_archive import import_probe, lookup, replay
from app.services.tickdesk_regime_evidence import (
    INDUSTRY_FIELD,
    MARKET_FIELD,
    _industry,
    _session_days,
)

_CHINA = ZoneInfo("Asia/Shanghai")
_MEMBERSHIP = "/api/a-share-index/constituents/ths-index-list-by-stock"
_HISTORY = "/api/a-share-index/prices/historical-batch"
_MARKET_INDEX = "000300.SH"
_RESPONSE_LIMIT = 128 * 1024
_TIMEOUT = 8


def _clock() -> datetime:
    return datetime.now(_CHINA)


@contextmanager
def _fuyao_http():
    from app.plugins.fuyao.provider import get_api_key

    client = FuyaoClient(api_key=get_api_key(), timeout=_TIMEOUT)
    try:
        # Reuse Fuyao's authenticated HTTP transport. The provider's parsed
        # endpoint wrapper drops the body and the actual receipt clock.
        yield client._http
    finally:
        client.close()


def _millis(stamp: datetime) -> int:
    return int(stamp.timestamp() * 1000)


def _capture(http, path, params, started, clock) -> tuple[dict | None, dict | None, str | None]:
    body = bytearray()
    deadline = monotonic() + _TIMEOUT
    try:
        with http.stream(
            "GET",
            path,
            params=params,
            headers={"Accept-Encoding": "identity"},
            follow_redirects=False,
        ) as response:
            if response.status_code != 200:
                return None, None, f"http_{response.status_code}"
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                return None, None, "unsupported_content_encoding"
            for chunk in response.iter_bytes():
                if monotonic() > deadline:
                    return None, None, "provider_timeout"
                if len(body) + len(chunk) > _RESPONSE_LIMIT:
                    return None, None, "response_limit"
                body.extend(chunk)
            received = clock()
    except httpx.HTTPError:
        return None, None, "provider_unreachable"
    if received < started:
        return None, None, "observation_clock_regressed"
    try:
        decoded = json.loads(body)
    except (ValueError, UnicodeError):
        return None, None, "invalid_response"
    if not isinstance(decoded, dict) or type(decoded.get("code")) is not int:
        return None, None, "invalid_response"
    if decoded["code"] != 0:
        return None, None, "provider_rejected"
    payload = decoded.get("data")
    if not isinstance(payload, dict):
        return None, None, "invalid_response"
    return (
        {
            "requestPath": path,
            "parameters": params,
            "httpStatus": 200,
            "requestedAt": started.isoformat(),
            "receivedAt": received.isoformat(),
            "responseSha256": hashlib.sha256(body).hexdigest(),
            "captureLayer": "http-entity-body",
            "responseBodyBase64": base64.b64encode(body).decode("ascii"),
        },
        payload,
        None,
    )


def _claim_status(path: Path) -> dict:
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
        state = previous.get("state")
    except (OSError, ValueError, AttributeError):
        state = None
    return {
        "state": "deduplicated",
        "priorState": state or "in_progress_or_interrupted",
        "requests": 0,
    }


def _missing_indices(facts, industry):
    codes = []
    if facts[MARKET_FIELD].value is None:
        codes.append(_MARKET_INDEX)
    if industry and facts[INDUSTRY_FIELD].value is None:
        codes.append(industry)
    return codes


def collect_once(
    path: Path,
    symbol: str,
    *,
    enabled=False,
    clock=_clock,
    open_http=_fuyao_http,
    window_allowed=None,
) -> dict:
    """Collect one symbol once per archive/trading date, with no automatic retry."""
    if not enabled:
        return {"state": "disabled", "requests": 0}
    if not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{6}\.(?:SH|SZ)", symbol):
        return {"state": "skipped", "reason": "unsupported_symbol", "requests": 0}
    started = clock().astimezone(_CHINA)
    day = started.date()
    cutoff = datetime.combine(day, time(15), _CHINA)
    if started >= cutoff:
        return {"state": "skipped", "reason": "decision_cutoff_elapsed", "requests": 0}
    if window_allowed is not None and not window_allowed(started):
        return {"state": "skipped", "reason": "outside_market_window", "requests": 0}
    try:
        calendar = lookup(path, "calendar", "fuyao", started)
    except (OSError, ValueError, sqlite3.Error):
        return {"state": "failed", "reason": "calendar_archive_unavailable", "requests": 0}
    sessions = _session_days(calendar.record.observation.data, day) if calendar.record else None
    if sessions is None:
        return {"state": "skipped", "reason": "calendar_unknown", "requests": 0}

    # An exclusive mkdir prevents overlapping processes from spending twice.
    # A crash consumes this day's attempt just like an explicit failure.
    claim = path.resolve().with_suffix(path.suffix + ".collection") / day.isoformat() / symbol
    result_file = claim / "result.json"
    try:
        claim.mkdir(parents=True)
    except FileExistsError:
        return _claim_status(result_file)
    except OSError:
        return {"state": "failed", "reason": "collection_claim_unavailable", "requests": 0}

    issues = {}
    counts = {"requests": 0, "archivedResponses": 0}
    latest = started

    def request(http, name, route, params):
        nonlocal latest
        at = clock()
        if at < latest or at >= cutoff:
            issues[name] = (
                "decision_cutoff_elapsed" if at >= cutoff else "observation_clock_regressed"
            )
            return None
        if window_allowed is not None and not window_allowed(at):
            issues[name] = "outside_market_window"
            return None
        latest = at
        counts["requests"] += 1
        packet, data, problem = _capture(http, route, params, at, clock)
        if problem:
            issues[name] = problem
            return None
        latest = datetime.fromisoformat(packet["receivedAt"])
        body = json.dumps({name: packet}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        try:
            counts["archivedResponses"] += import_probe(
                path, body, hashlib.sha256(body).hexdigest()
            )
        except (OSError, ValueError, sqlite3.Error):
            issues[name] = "archive_write_failed"
            return None
        return data

    try:
        facts = replay(path, symbol, started)
        cached_member = lookup(path, "membership", symbol, started).record
        industry = None
        if cached_member and cached_member.observation.observed_at.astimezone(_CHINA).date() == day:
            industry = _industry(cached_member.observation.data, symbol)
        codes = _missing_indices(facts, industry)
        if not industry or codes:
            with open_http() as http:
                if not industry:
                    member = request(http, "membership", _MEMBERSHIP, {"thscode": symbol, "type": "industry"})
                    industry = _industry(member, symbol) if member is not None else None
                    if member is not None and industry is None:
                        issues["membership"] = "industry_unknown"
                    if industry:
                        facts = replay(path, symbol, min(latest, cutoff))
                        codes = _missing_indices(facts, industry)
                if codes:
                    first = datetime.combine(sessions[0], time.min, _CHINA)
                    last = datetime.combine(sessions[-1] + timedelta(days=1), time.min, _CHINA)
                    request(http, "indices", _HISTORY, {
                        "thscodes": ",".join(codes), "interval": "1d",
                        "start": _millis(first), "end": _millis(last) - 1,
                    })
    except (FuyaoError, httpx.HTTPError):
        issues["provider"] = "provider_unavailable"
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        issues["archive"] = "archive_read_failed"

    finished = clock().astimezone(_CHINA)
    if finished < latest:
        issues["clock"] = "observation_clock_regressed"
    try:
        fields = {
            field: asdict(fact)
            for field, fact in replay(path, symbol, min(finished, cutoff)).items()
        }
    except (OSError, ValueError, sqlite3.Error):
        fields = {}
        issues["archive"] = "archive_read_failed"
    ready = sum(field["value"] is not None for field in fields.values())
    state = "ready" if ready == 2 and not issues else "partial" if ready else "failed"
    reason = "decision_cutoff_elapsed" if finished > cutoff else None
    if state != "ready" and reason is None:
        reason = "sources_incomplete"
    result = {
        "state": state,
        "reason": reason,
        "symbol": symbol,
        "decisionDay": day.isoformat(),
        **counts,
        "sourceIssues": issues,
        "fields": fields,
        "finishedAt": finished.isoformat(),
    }
    try:
        atomic_write_text(result_file, json.dumps(result, default=_iso, ensure_ascii=False))
    except OSError:
        result["statusPersisted"] = False
    return result


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument(
        "--enable",
        action="store_true",
        help="Allow at most two Fuyao requests once for this session",
    )
    args = parser.parse_args()
    result = collect_once(args.archive, args.symbol, enabled=args.enable)
    print(json.dumps(result, default=_iso, ensure_ascii=False))
    if result["state"] == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
