"""Share the existing market calendar with the opt-in regime research collector."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from app.services.tickdesk_adapter import CN, MarketAdapter, timestamp
from app.services.tickdesk_market_data import read_calendar_snapshot
from app.services.tickdesk_quote_feed import quote_session
from app.services.tickdesk_regime_archive import INDEX_ARCHIVE, import_calendar
from app.services.tickdesk_regime_collect import collect_once
from app.services.tickdesk_research import read_research_config


def _now():
    return datetime.now(CN)


class RegimeCollection:
    def __init__(self, repo, workspace_scope, *, enabled=None, symbol=None):
        self.repo = repo
        self.workspace_scope = workspace_scope
        self.root = Path(repo.store.data_dir)
        self.archive = self.root / INDEX_ARCHIVE
        self.enabled = (
            os.getenv("TICKDESK_REGIME_COLLECTION_ENABLED") == "1" if enabled is None else enabled
        )
        self.symbol = (
            os.getenv("TICKDESK_REGIME_COLLECTION_SYMBOL", "") if symbol is None else symbol
        )
        self._calendar_digest = None

    def _archive_calendar(self, snapshot):
        if snapshot is None:
            return {"status": "calendar_unknown"}
        raw, metadata = snapshot
        if metadata.get("source") != "fuyao":
            return {"status": "calendar_source_unverified"}
        digest = hashlib.sha256(raw).hexdigest()
        if digest != self._calendar_digest or not self.archive.is_file():
            try:
                import_calendar(self.archive, raw, digest)
            except (OSError, ValueError, TypeError, sqlite3.Error):
                return {"status": "calendar_archive_unavailable"}
            self._calendar_digest = digest
        return {
            "status": "archived",
            "observedAt": metadata["observedAt"],
            "through": metadata["through"],
            "sha256": digest,
        }

    def step(self, *, clock=_now):
        now = clock().astimezone(CN)
        anchor = timestamp(now)
        snapshot = read_calendar_snapshot(self.root, anchor)
        calendar = self._archive_calendar(snapshot)
        status = {
            "status": "disabled" if not self.enabled else "skipped",
            "calendar": calendar,
            "requests": 0,
        }
        if not self.enabled:
            return status
        if not re.fullmatch(r"[0-9]{6}\.(?:SH|SZ)", self.symbol):
            return {**status, "reason": "invalid_symbol_configuration"}
        if calendar["status"] != "archived":
            return {**status, "reason": calendar["status"]}
        if calendar["through"] < now.date().isoformat():
            return {**status, "reason": "calendar_unknown"}

        def window_allowed(at):
            stamp = timestamp(at)
            current = read_calendar_snapshot(self.root, stamp)
            return bool(
                current
                and current[1].get("source") == "fuyao"
                and quote_session(self.root, stamp) == "open"
            )

        if not window_allowed(now):
            return {**status, "reason": "outside_market_window"}
        if self.symbol not in self.workspace_scope():
            try:
                scope = read_research_config(self.root) or {}
            except (OSError, ValueError, TypeError, KeyError):
                return {**status, "reason": "workspace_scope_unavailable"}
            if self.symbol not in scope.get("symbols", ()):
                return {**status, "reason": "symbol_outside_workspace_scope"}
        try:
            matches = [
                row
                for row in MarketAdapter(self.repo, None).symbols(anchor)
                if row["symbol"] == self.symbol
            ]
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            matches = []
        if (
            len(matches) != 1
            or matches[0].get("instrument") != "stock"
            or not matches[0].get("source")
            or matches[0].get("supported") is not True
        ):
            return {**status, "reason": "stock_identity_unavailable"}
        result = collect_once(
            self.archive, self.symbol, enabled=True, clock=clock, window_allowed=window_allowed
        )
        return {
            **status,
            "status": result["state"],
            "symbol": self.symbol,
            **{
                key: result[key]
                for key in (
                    "reason",
                    "requests",
                    "archivedResponses",
                    "sourceIssues",
                    "priorState",
                    "finishedAt",
                    "statusPersisted",
                )
                if key in result
            },
        }
