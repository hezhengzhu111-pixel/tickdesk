"""Offline, append-only archive of research regime source observations."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from app.services.tickdesk_regime_evidence import (
    INDUSTRY_FIELD,
    MARKET_FIELD,
    ObservedData,
    RegimeFact,
    _industry,
    evaluate_for_decision,
)

_APPLICATION_ID = 0x54445247
INDEX_ARCHIVE = Path("tickdesk-market/analysis-index-sources.sqlite3")
_MAX_ARTIFACT = 512 * 1024
_INDEX = re.compile(r"[0-9]{6}\.(?:SH|SZ|TI)")
_STOCK = re.compile(r"[0-9]{6}\.(?:SH|SZ)")
_HASH = re.compile(r"[0-9a-f]{64}")
_SCHEMA = (
    """CREATE TABLE artifact (
        digest TEXT PRIMARY KEY, body BLOB NOT NULL, ingested_at TEXT NOT NULL
    )""",
    """CREATE TABLE source (
        kind TEXT NOT NULL, scope TEXT NOT NULL, observed_at TEXT NOT NULL,
        digest TEXT NOT NULL REFERENCES artifact(digest), entry TEXT NOT NULL,
        PRIMARY KEY (kind, scope, observed_at, digest, entry)
    )""",
    """CREATE TRIGGER artifact_immutable_insert BEFORE INSERT ON artifact
    WHEN EXISTS (SELECT 1 FROM artifact WHERE digest=NEW.digest)
    BEGIN SELECT RAISE(ABORT, 'immutable artifact'); END""",
    """CREATE TRIGGER source_immutable_insert BEFORE INSERT ON source
    WHEN EXISTS (SELECT 1 FROM source WHERE kind=NEW.kind AND scope=NEW.scope AND
        observed_at=NEW.observed_at AND digest=NEW.digest AND entry=NEW.entry)
    BEGIN SELECT RAISE(ABORT, 'immutable source'); END""",
)


@dataclass(frozen=True)
class SourceRecord:
    body: bytes
    artifact_sha256: str
    provider_sha256: str | None
    observation: ObservedData
    ingested_at: datetime


@dataclass(frozen=True)
class SourceLookup:
    state: str
    record: SourceRecord | None = None


@dataclass(frozen=True)
class _Observation:
    kind: str
    scopes: tuple[str, ...]
    observed_at: datetime
    payload: object
    provider_sha256: str | None = None


def _stamp(value: datetime | str) -> datetime:
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(stamp, datetime) or stamp.utcoffset() is None:
        raise ValueError("observation clock must include timezone")
    return stamp


def _utc(value: datetime | str) -> str:
    return _stamp(value).astimezone(UTC).isoformat(timespec="microseconds")


def _json_pairs(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate observation key")
    return result


def _decode(body: bytes, digest: str) -> dict:
    if len(body) > _MAX_ARTIFACT or hashlib.sha256(body).hexdigest() != digest:
        raise ValueError("capture size or artifact hash mismatch")
    result = json.loads(body, parse_float=Decimal, object_pairs_hook=_json_pairs)
    if not isinstance(result, dict):
        raise ValueError("unexpected capture format")
    return result


def _codes(text: object, pattern: re.Pattern, limit: int) -> tuple[str, ...]:
    parts = text.split(",") if isinstance(text, str) and len(text) <= 1000 else []
    if (
        not 0 < len(parts) <= limit
        or len(set(parts)) != len(parts)
        or not all(pattern.fullmatch(code) for code in parts)
    ):
        raise ValueError("unexpected capture scope")
    return tuple(parts)


def _probe(raw: object) -> _Observation:
    if not isinstance(raw, dict) or raw.get("httpStatus") != 200:
        raise ValueError("expected a successful recorded response")
    response, params = raw.get("response"), raw.get("parameters")
    declared_hash = raw.get("responseSha256")
    if "captureLayer" in raw or "responseBodyBase64" in raw:
        encoded = raw.get("responseBodyBase64")
        if (
            raw.get("captureLayer") != "http-entity-body"
            or "response" in raw
            or not isinstance(encoded, str)
        ):
            raise ValueError("unexpected HTTP capture layer")
        try:
            body = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ValueError("invalid encoded response body") from exc
        response = _decode(body, declared_hash)
    if (
        not isinstance(response, dict)
        or type(response.get("code")) is not int
        or response["code"] != 0
        or not isinstance(response.get("data"), dict)
        or not isinstance(params, dict)
        or not isinstance(declared_hash, str)
        or not _HASH.fullmatch(declared_hash)
    ):
        raise ValueError("invalid stored provider response or hash")
    received = _stamp(raw.get("receivedAt"))
    if _stamp(raw.get("requestedAt")) > received:
        raise ValueError("inconsistent recorded observation clock")
    path = raw.get("requestPath")
    if (
        path == "/api/a-share-index/prices/historical-batch"
        and set(params) == {"thscodes", "interval", "start", "end"}
        and params["interval"] == "1d"
        and type(params["start"]) is int
        and type(params["end"]) is int
        and 0 <= params["start"] <= params["end"]
    ):
        kind, scopes = "indices", _codes(params["thscodes"], _INDEX, 10)
    elif (
        path == "/api/a-share-index/constituents/ths-index-list-by-stock"
        and set(params) == {"thscode", "type"}
        and params["type"] == "industry"
    ):
        kind, scopes = "membership", _codes(params["thscode"], _STOCK, 100)
    else:
        raise ValueError("unexpected provider capture route or parameters")
    return _Observation(kind, scopes, received, response["data"], declared_hash)


def _calendar(raw: dict) -> _Observation:
    sessions, observed = raw.get("sessions"), raw.get("observedAt")
    if (
        raw.get("source") != "fuyao"
        or type(observed) is not int
        or observed < 0
        or not isinstance(sessions, list)
        or not 0 < len(sessions) <= 370
    ):
        raise ValueError("untrusted calendar cache")
    through = date.fromisoformat(raw["through"])
    received = datetime.fromtimestamp(observed / 1000, UTC)
    days = [date.fromisoformat(day) for day in sessions]
    if (
        [day.isoformat() for day in days] != sessions
        or days != sorted(set(days))
        or any(day > through for day in days)
    ):
        raise ValueError("invalid calendar coverage")
    return _Observation("calendar", ("fuyao",), received, sessions)


def _is_archive(db: sqlite3.Connection) -> bool:
    return (
        db.execute("PRAGMA application_id").fetchone()[0] == _APPLICATION_ID
        and db.execute("PRAGMA user_version").fetchone()[0] == 1
    )


def _initialize(db: sqlite3.Connection) -> None:
    if _is_archive(db):
        return
    if (
        db.execute("PRAGMA application_id").fetchone()[0] != 0
        or db.execute("PRAGMA user_version").fetchone()[0] != 0
        or db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    ):
        raise ValueError("file is not a research regime archive")
    for statement in _SCHEMA:
        db.execute(statement)
    for table in ("artifact", "source"):
        for action in ("UPDATE", "DELETE"):
            db.execute(
                f"CREATE TRIGGER {table}_immutable_{action.lower()} BEFORE {action} ON {table} "
                f"BEGIN SELECT RAISE(ABORT, 'immutable {table}'); END"
            )
    db.execute(f"PRAGMA application_id={_APPLICATION_ID}")
    db.execute("PRAGMA user_version=1")


def _write(path: Path, body: bytes, digest: str, readings: dict[str, _Observation]) -> int:
    ingested = datetime.now(UTC)
    if any(reading.observed_at > ingested for reading in readings.values()):
        raise ValueError("observation clock is later than import")
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        with db:
            db.execute("BEGIN IMMEDIATE")
            _initialize(db)
            written = db.execute(
                "INSERT INTO artifact SELECT ?,?,? WHERE NOT EXISTS (SELECT 1 FROM artifact WHERE digest=?)",
                (digest, body, _utc(ingested), digest),
            ).rowcount
            if not written:
                return 0
            db.executemany(
                "INSERT INTO source VALUES (?,?,?,?,?)",
                [
                    (record.kind, scope, _utc(record.observed_at), digest, entry)
                    for entry, record in readings.items()
                    for scope in record.scopes
                ],
            )
            return len(readings)


def import_probe(path: Path, body: bytes, expected_sha256: str) -> int:
    """Store the original capture bytes with per-request scope and original receipt clock."""
    document = _decode(body, expected_sha256)
    if not 0 < len(document) <= 16 or any(not 0 < len(key) <= 128 for key in document):
        raise ValueError("too many or invalid capture entries")
    return _write(
        path, body, expected_sha256, {entry: _probe(raw) for entry, raw in document.items()}
    )


def import_calendar(path: Path, body: bytes, expected_sha256: str) -> int:
    """Archive the existing observed Fuyao calendar cache without extending its coverage."""
    return _write(path, body, expected_sha256, {"": _calendar(_decode(body, expected_sha256))})


def _lookup(db: sqlite3.Connection, kind: str, scope: str, cutoff: datetime,
            last_row: int | None = None) -> SourceLookup:
    candidates = db.execute(
        "SELECT observed_at,digest,entry FROM source WHERE kind=? AND scope=? AND observed_at<=? "
        "AND (? IS NULL OR rowid<=?) ORDER BY observed_at DESC LIMIT 2",
        (kind, scope, _utc(cutoff), last_row, last_row),
    ).fetchall()
    if not candidates:
        return SourceLookup("missing")
    observed, digest, entry = candidates[0]
    if len(candidates) > 1 and candidates[1][0] == observed:
        return SourceLookup("conflict")
    artifact = db.execute(
        "SELECT body,ingested_at FROM artifact WHERE digest=?", (digest,)
    ).fetchone()
    if artifact is None:
        raise ValueError("archive source artifact is absent")
    body, ingested = artifact
    envelope = _decode(body, digest)
    raw = _calendar(envelope) if kind == "calendar" else _probe(envelope[entry])
    if raw.kind != kind or scope not in raw.scopes or _utc(raw.observed_at) != observed:
        raise ValueError("archive index differs from stored capture")
    record_hash = hashlib.sha256((digest + "\n" + entry).encode()).hexdigest()
    data = ObservedData(raw.payload, raw.observed_at, record_hash)
    return SourceLookup(
        "known", SourceRecord(body, digest, raw.provider_sha256, data, _stamp(ingested))
    )


def _read(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        if not _is_archive(db):
            raise ValueError("file is not a research regime archive")
        db.execute("BEGIN")
    except (ValueError, sqlite3.Error):
        db.close()
        raise
    return db


def _frozen_row(db, revision):
    if revision is None:
        return None
    last_row, digest = revision
    if db.execute("SELECT digest FROM source WHERE rowid=?", (last_row,)).fetchone() != (digest,):
        raise ValueError("frozen regime revision is absent")
    return last_row


def lookup(path: Path, kind: str, scope: str, cutoff: datetime, *, revision=None) -> SourceLookup:
    """Read only the latest visible capture of the given requested scope."""
    if kind not in {"indices", "membership", "calendar"}:
        raise ValueError("unknown observation kind")
    _stamp(cutoff)
    if not path.exists():
        if revision is not None:
            raise ValueError("frozen regime archive is absent")
        return SourceLookup("missing")
    with closing(_read(path)) as db:
        return _lookup(db, kind, scope, cutoff, _frozen_row(db, revision))


def replay(path: Path, symbol: str, cutoff: datetime) -> dict[str, RegimeFact]:
    """Replay both legs using one read-only archive snapshot and the original receipt clocks."""
    return replay_many(path, [symbol], cutoff)[symbol]


def archive_revision(path: Path) -> tuple[int, str] | None:
    """Identify the newest committed row of the append-only source table."""
    if not path.exists():
        return None
    with closing(_read(path)) as db:
        return db.execute("SELECT rowid,digest FROM source ORDER BY rowid DESC LIMIT 1").fetchone()


def replay_many(path: Path, symbols: list[str], cutoff: datetime, *,
                revision: tuple[int, str] | None = None) -> dict[str, dict[str, RegimeFact]]:
    """Share the calendar and index reads, optionally excluding later imports."""
    if not path.exists():
        if revision is not None:
            raise ValueError("frozen regime archive is absent")
        return {symbol: evaluate_for_decision(symbol, cutoff, calendar=None, indices=None, membership=None)
                for symbol in symbols}
    with closing(_read(path)) as db:
        last_row = _frozen_row(db, revision)
        calendar = _lookup(db, "calendar", "fuyao", cutoff, last_row)
        if calendar.state == "conflict":
            return {symbol: {field: RegimeFact(reason="calendar_observation_conflict")
                             for field in (MARKET_FIELD, INDUSTRY_FIELD)} for symbol in symbols}
        if calendar.record is None and revision is not None:
            return {symbol: evaluate_for_decision(symbol, cutoff, calendar=None, indices=None, membership=None)
                    for symbol in symbols}
        market = _lookup(db, "indices", "000300.SH", cutoff, last_row)
        calendar_input = calendar.record.observation if calendar.record else None
        industries: dict[str, SourceLookup] = {}
        results = {}
        for symbol in symbols:
            member = _lookup(db, "membership", symbol, cutoff, last_row)
            industry_code = _industry(member.record.observation.data, symbol) if member.record else None
            industry = SourceLookup("missing")
            if industry_code:
                if industry_code not in industries:
                    industries[industry_code] = _lookup(db, "indices", industry_code, cutoff, last_row)
                industry = industries[industry_code]
            results[symbol] = _evaluate_sources(symbol, cutoff, calendar_input, market, member, industry)
        return results


def _evaluate_sources(symbol: str, cutoff: datetime, calendar: ObservedData | None, market: SourceLookup,
                      member: SourceLookup, industry: SourceLookup) -> dict[str, RegimeFact]:
    market_result = evaluate_for_decision(
        symbol,
        cutoff,
        calendar=calendar,
        indices=market.record.observation if market.record else None,
        membership=None,
    )[MARKET_FIELD]
    industry_result = evaluate_for_decision(
        symbol,
        cutoff,
        calendar=calendar,
        indices=industry.record.observation if industry.record else None,
        membership=member.record.observation if member.record else None,
    )[INDUSTRY_FIELD]
    if market.state == "conflict":
        market_result = RegimeFact(reason="market_index_observation_conflict")
    if member.state == "conflict" or industry.state == "conflict":
        industry_result = RegimeFact(reason="industry_observation_conflict")
    return {MARKET_FIELD: market_result, INDUSTRY_FIELD: industry_result}


def _describe(kind: str, scope: str, source: ObservedData) -> dict:
    data = source.data
    if kind == "calendar":
        return {"payloadState": "available", "sessionCount": len(data), "lastSession": data[-1]}
    if kind == "membership":
        code = _industry(data, scope)
        return (
            {"payloadState": "available", "industryCode": code}
            if code
            else {"payloadState": "unknown"}
        )
    groups = data.get("item")
    if not isinstance(groups, list):
        return {"payloadState": "unknown"}
    matches = [
        group for group in groups if isinstance(group, dict) and group.get("thscode") == scope
    ]
    rows = matches[0].get("item") if len(matches) == 1 else None
    if not isinstance(rows, list) or not rows or not isinstance(rows[-1], dict):
        return {"payloadState": "unknown"}
    last = rows[-1]
    return {
        "payloadState": "available",
        "rowCount": len(rows),
        "lastDateMs": last.get("date_ms"),
        "lastClose": str(last["close_price"]) if last.get("close_price") is not None else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("import-probe", "import-calendar"):
        command = actions.add_parser(name)
        command.add_argument("capture", type=Path)
        command.add_argument("--sha256", required=True, type=str.lower)
    query = actions.add_parser("lookup")
    query.add_argument("kind", choices=("indices", "membership", "calendar"))
    query.add_argument("scope")
    query.add_argument("at", type=_stamp)
    evaluate = actions.add_parser("replay")
    evaluate.add_argument("symbol")
    evaluate.add_argument("at", type=_stamp)
    args = parser.parse_args()
    if args.action.startswith("import-"):
        with args.capture.open("rb") as source:
            body = source.read(_MAX_ARTIFACT + 1)
        store = import_probe if args.action == "import-probe" else import_calendar
        result = {"addedObservations": store(args.archive, body, args.sha256)}
    elif args.action == "lookup":
        selected = lookup(args.archive, args.kind, args.scope, args.at)
        result = {"state": selected.state, "kind": args.kind, "scope": args.scope}
        if selected.record:
            result.update(
                observedAt=selected.record.observation.observed_at.isoformat(),
                importedAt=selected.record.ingested_at.isoformat(),
                artifactSha256=selected.record.artifact_sha256,
                providerResponseSha256=selected.record.provider_sha256,
                **_describe(args.kind, args.scope, selected.record.observation),
            )
    else:
        result = {
            field: asdict(fact)
            for field, fact in replay(args.archive, args.symbol, args.at).items()
        }
    print(json.dumps(result, default=lambda value: value.isoformat(), ensure_ascii=False))


if __name__ == "__main__":
    main()
