"""Schedule official Shenzhen risk snapshots within the existing market lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, time, timedelta
from pathlib import Path

from app.services.fs_utils import atomic_write_text
from app.services.tickdesk_adapter import CN, timestamp
from app.services.tickdesk_market_data import read_calendar_snapshot
from app.services.tickdesk_quote_feed import quote_session
from app.services.tickdesk_szse_risk import SZSE_ST_ARCHIVE, capture_szse_st_snapshot

_ATTEMPT_STATES = {"ready", "late", "skipped", "failed"}


def _now():
    return datetime.now(CN)


def _stamp(value):
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None:
        raise ValueError("snapshot time needs an offset")
    return stamp.astimezone(CN)


class SzseRiskSnapshots:
    def __init__(self, root, *, enabled=None, capture=None):
        self.root = Path(root)
        self.archive = self.root / SZSE_ST_ARCHIVE
        self.enabled = (
            os.getenv("TICKDESK_SZSE_ST_CAPTURE_ENABLED", "1") == "1"
            if enabled is None
            else enabled
        )
        self.capture = capture_szse_st_snapshot if capture is None else capture

    def _eligibility(self, now):
        at = timestamp(now)
        cached = read_calendar_snapshot(self.root, at)
        if (
            not cached
            or cached[1].get("source") != "fuyao"
            or cached[1]["through"] < now.date().isoformat()
        ):
            return "calendar_unknown", None
        if (
            now.date().isoformat() not in cached[1]["sessions"]
            or quote_session(self.root, at) != "open"
        ):
            return "outside_market_window", None
        return None, cached[1]["observedAt"]

    @staticmethod
    def _prior_state(attempt, destination):
        try:
            with (attempt / "result.json").open("rb") as source:
                result = json.loads(source.read(4096))
            if result.get("status") in _ATTEMPT_STATES:
                return result["status"]
        except (ValueError, AttributeError, TypeError, OSError):
            pass
        if destination.exists() or destination.is_symlink():
            return "archive_present_unrecorded"
        if attempt.exists() or attempt.is_symlink():
            return "in_progress_or_interrupted"
        return None

    @staticmethod
    def _record(attempt, result):
        try:
            atomic_write_text(attempt / "result.json", json.dumps(result, ensure_ascii=False))
        except OSError:
            result["statusPersisted"] = False
        return result

    def _publish(self, attempt, destination, requested, calendar_at, clock):
        with tempfile.TemporaryDirectory(prefix=".capture-", dir=attempt) as temp:
            candidate = Path(temp) / "snapshot"
            manifest = self.capture(candidate)
            finished = clock().astimezone(CN)
            started, available = (
                _stamp(manifest["collectionStartedAt"]),
                _stamp(manifest["availableAt"]),
            )
            day = requested.date()
            if (
                manifest["exchangeDate"] != day.isoformat()
                or started.date() != day
                or available.date() != day
                or not requested <= started <= available <= finished + timedelta(milliseconds=1)
                or type(manifest["availableAtMs"]) is not int
                or timestamp(available) != manifest["availableAtMs"]
            ):
                raise ValueError("snapshot observation clock disagrees with attempt")
            counts = {name: manifest[name] for name in ("workbookRows", "activeSecurityCount")}
            if (
                any(type(value) is not int or value < 0 for value in counts.values())
                or not 0 < counts["activeSecurityCount"] <= 8000
                or counts["workbookRows"] > counts["activeSecurityCount"]
            ):
                raise ValueError("invalid snapshot coverage summary")
            with (candidate / "capture-evidence.json").open("rb") as manifest_file:
                body = manifest_file.read(131_073)
            if len(body) > 131_072 or json.loads(body) != manifest:
                raise ValueError("snapshot does not match its manifest")
            cutoff = datetime.combine(day, time(15), CN)
            late = max(available, finished, clock().astimezone(CN)) >= cutoff
            target = attempt / "late-snapshot" if late else destination
            if target.exists() or target.is_symlink():
                raise FileExistsError("snapshot target already recorded")
            candidate.rename(target)
            return {
                "status": "late" if late else "ready",
                "date": day.isoformat(),
                "collectionStartedAt": started.isoformat(),
                "availableAt": available.isoformat(),
                "calendarObservedAt": calendar_at,
                "manifestSha256": hashlib.sha256(body).hexdigest(),
                **counts,
                **({"reason": "decision_cutoff_elapsed"} if late else {}),
            }

    def step(self, *, clock=_now):
        if not self.enabled:
            return {"status": "disabled"}
        now = clock().astimezone(CN)
        stamp = now.date().isoformat()
        destination = self.archive / stamp
        attempt = self.archive / ".attempts" / stamp
        base = {"date": stamp}
        reason, _ = self._eligibility(now)
        prior = self._prior_state(attempt, destination)
        if reason is not None:
            return {
                **base,
                "status": "skipped",
                "reason": reason,
                **({"priorState": prior} if prior else {}),
            }
        if prior:
            return {**base, "status": "deduplicated", "priorState": prior}
        try:
            attempt.mkdir(parents=True)
        except FileExistsError:
            return {
                **base,
                "status": "deduplicated",
                "priorState": self._prior_state(attempt, destination),
            }
        except OSError:
            return {**base, "status": "failed", "reason": "collection_claim_unavailable"}

        requested = clock().astimezone(CN)
        reason, calendar_at = self._eligibility(requested)
        if reason or requested.date() != now.date() or requested < now:
            return self._record(
                attempt,
                {**base, "status": "skipped", "reason": reason or "collection_clock_changed"},
            )
        try:
            result = self._publish(attempt, destination, requested, calendar_at, clock)
        except Exception as exc:  # noqa: BLE001 -- the optional background source must leave a terminal attempt
            result = {
                **base,
                "status": "failed",
                "reason": "snapshot_capture_failed",
                "errorKind": type(exc).__name__,
            }
        return self._record(attempt, result)
