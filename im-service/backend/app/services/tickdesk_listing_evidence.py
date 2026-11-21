"""Point-in-time lower bounds for trading sessions since a confirmed listing."""

import json
import sqlite3
from datetime import date, datetime
from hashlib import sha256
from io import BytesIO

import polars as pl

from app.quant_evidence import SHANGHAI, EvidenceFact
from app.services.tickdesk_exchange_master import SSE_LIST, SZSE_LIST
from app.services.tickdesk_regime_archive import INDEX_ARCHIVE, lookup
from app.services.tickdesk_risk_archive import read_bytes

_ROSTERS = "tickdesk-market/listing-observations"
_LIMIT = 2 * 1024 * 1024
_SOURCES = {"SH": ("上海证券交易所", SSE_LIST), "SZ": ("深圳证券交易所", SZSE_LIST)}


def freeze_listing_rosters(root) -> dict[str, str]:
    revisions = {}
    for path in (root / _ROSTERS).glob("????-??-??.parquet"):
        try:
            if date.fromisoformat(path.stem).isoformat() == path.stem:
                revisions[path.stem] = sha256(read_bytes(path, _LIMIT)).hexdigest()
        except (OSError, ValueError):
            continue
    return revisions


def read_listing_bounds(root, symbols, cutoff, roster_sha256, calendar_revision):
    day = cutoff.date().isoformat()
    if roster_sha256 is None or calendar_revision is None:
        return {}
    try:
        observed = lookup(root / INDEX_ARCHIVE, "calendar", "fuyao", cutoff, revision=calendar_revision)
        record = observed.record
        if record is None or json.loads(record.body)["through"] != day:
            return {}
        calendar = record.observation
        sessions = calendar.data
        if day not in sessions:
            return {}
        blob = read_bytes(root / _ROSTERS / f"{day}.parquet", _LIMIT)
        if sha256(blob).hexdigest() != roster_sha256:
            return {}
        roster = pl.read_parquet(BytesIO(blob), columns=[
            "symbol", "exchange", "listing_date", "status", "source", "source_ref", "as_of",
        ]).filter(pl.col("symbol").is_in(symbols))
        facts = {}
        for (symbol,), rows in roster.group_by("symbol"):
            if rows.height != 1:
                continue
            row = rows.row(0, named=True)
            venue = row["exchange"]
            listing = row["listing_date"]
            if (venue not in _SOURCES or not symbol.endswith(f".{venue}") or row["status"] != "listed"
                    or (row["source"], row["source_ref"]) != _SOURCES[venue]
                    or type(row["as_of"]) is not int or type(listing) is not date or listing > cutoff.date()):
                continue
            roster_at = datetime.fromtimestamp(row["as_of"] / 1000, SHANGHAI)
            if roster_at.date() != cutoff.date() or roster_at > cutoff:
                continue
            count = sum(listing.isoformat() <= session <= day for session in sessions)
            available = max(roster_at, calendar.observed_at).astimezone(SHANGHAI)
            facts[symbol] = (listing.isoformat(), EvidenceFact(
                count, None, (roster_sha256, record.artifact_sha256), available.isoformat(),
            ))
        return facts
    except (OSError, ValueError, KeyError, TypeError, OverflowError, sqlite3.Error, pl.exceptions.PolarsError):
        return {}
