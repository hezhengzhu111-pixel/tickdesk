"""Verify Shanghai risk warning boards against the same-day official stock roster."""

import argparse
import json
import re
import tempfile
from datetime import datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path

import httpx
import polars as pl

from app.quant_evidence import SHANGHAI, EvidenceFact
from app.services.tickdesk_exchange_master import SSE_LIST
from app.services.tickdesk_risk_archive import (
    MANIFEST,
    MANIFEST_LIMIT,
    read_bytes,
    read_json,
    receipt,
)

SSE_ST_ARCHIVE = Path("tickdesk-market/sse-risk-snapshots")
_QUERY = "https://query.sse.com.cn/commonSoaQuery.do"
_RISK_PAGE = "https://www.sse.com.cn/disclosure/listedinfo/riskplate/"
_STAR_PAGE = "https://star.sse.com.cn/disclosure/listedinfo/starriskplate/"
_MAIN_SQL = "PL_SSGSXX_FXJSBGPLB"
_STAR_SQL = "SSE_PL_SSGSXX_KCBFXTS_L"
_BOARD_FILES = {
    "main-risk.json": (f"{_QUERY}?sqlId={_MAIN_SQL}&domesticIndicator=S&productType=0", _RISK_PAGE, _MAIN_SQL),
    "main-delisting.json": (f"{_QUERY}?sqlId={_MAIN_SQL}&domesticIndicator=P&productType=0", _RISK_PAGE, _MAIN_SQL),
    "star-risk.json": (f"{_QUERY}?sqlId={_STAR_SQL}&type=0", _STAR_PAGE, _STAR_SQL),
    "star-delisting.json": (f"{_QUERY}?sqlId={_STAR_SQL}&type=1", _STAR_PAGE, _STAR_SQL),
}
_ROSTER = "active-roster.parquet"
_SYMBOL = re.compile(r"(?:60[0-9]{4}|688[0-9]{3})\.SH")


def _board(data: bytes, sql: str) -> dict[str, str]:
    result = read_json(data)
    rows, page = result["result"], result["pageHelp"]
    if (result["sqlId"] != sql or result["actionErrors"] != [] or result["fieldErrors"] != {}
            or result["isPagination"] != "false" or not isinstance(rows, list)
            or type(page["total"]) is not int or not 0 <= page["total"] == len(rows) <= 1000
            or page["data"] != rows or page["pageNo"] != 1 or page["pageCount"] != int(bool(rows))):
        raise ValueError("Shanghai board coverage is incomplete")
    code_field, name_field = ("INSTRUMENT_ID", "INSTRUMENT_SHORT") if sql == _MAIN_SQL else ("secCode", "secNameCn")
    codes = re.compile(r"(?:60[0-9]{4}|900[0-9]{3})" if sql == _MAIN_SQL else r"(?:688|689)[0-9]{3}")
    listing = {}
    for row in rows:
        code, name = row[code_field], row[name_field]
        if not isinstance(code, str) or not codes.fullmatch(code) or code in listing:
            raise ValueError("invalid Shanghai risk-board code")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("empty Shanghai risk-board name")
        listing[code] = name.strip()
    return {f"{code}.SH": name for code, name in listing.items() if _SYMBOL.fullmatch(f"{code}.SH")}


def _roster(data: bytes, seen: datetime) -> dict[str, dict[str, str]]:
    columns = ["symbol", "exchange", "board", "name", "status", "listing_date", "source", "source_ref", "as_of"]
    frame = pl.read_parquet(BytesIO(data), columns=columns).filter(pl.col("exchange") == "SH")
    roster: dict[str, dict[str, str]] = {"main": {}, "star": {}}
    for row in frame.iter_rows(named=True):
        symbol = row["symbol"]
        if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
            continue
        segment = "main" if symbol.startswith("60") else "star"
        board = "沪主板" if segment == "main" else "科创板"
        name = row["name"]
        if (symbol in roster[segment] or row["board"] != board or row["status"] != "listed"
                or row["source"] != "上海证券交易所" or row["source_ref"] != SSE_LIST
                or type(row["as_of"]) is not int or row["as_of"] != int(seen.timestamp() * 1000)
                or row["listing_date"] is None or row["listing_date"] > seen.date()
                or not isinstance(name, str) or not name.strip()):
            raise ValueError("Shanghai roster is inconsistent with its dated receipt")
        roster[segment][symbol] = name.strip()
    if not all(roster.values()):
        raise ValueError("Shanghai stock roster is incomplete")
    return roster


def _verified_blobs(snapshot_dir, sources, available):
    expected = {name: entry[0] for name, entry in _BOARD_FILES.items()} | {_ROSTER: SSE_LIST}
    if set(sources) != set(expected):
        raise ValueError("Shanghai snapshot sources are incomplete")
    blobs, arrivals = {}, {}
    for name, url in expected.items():
        source = sources[name]
        limit = 2 * 1024 * 1024 if name == _ROSTER else MANIFEST_LIMIT
        data = read_bytes(snapshot_dir / name, limit)
        seen = receipt(source["receivedAt"])
        if (source["url"] != url or seen.date() != available.date() or seen > available
                or type(source["bytes"]) is not int or source["bytes"] != len(data)
                or source["sha256"] != sha256(data).hexdigest()):
            raise ValueError("Shanghai snapshot payload differs from its receipt")
        blobs[name], arrivals[name] = data, seen
    return blobs, arrivals[_ROSTER]


def read_sse_st_board(snapshot_dir: Path, symbols, cutoff: datetime, *, manifest_sha256: str) -> dict[str, EvidenceFact]:
    selected = {symbol for symbol in symbols if isinstance(symbol, str) and _SYMBOL.fullmatch(symbol)}
    if not selected:
        return {}

    def unavailable(reason):
        return dict.fromkeys(selected, EvidenceFact(reason=reason))

    if not isinstance(cutoff, datetime) or cutoff.utcoffset() is None:
        return unavailable("invalid_cutoff")
    local = cutoff.astimezone(SHANGHAI)
    try:
        raw = read_bytes(snapshot_dir / MANIFEST, MANIFEST_LIMIT)
        if sha256(raw).hexdigest() != manifest_sha256:
            return unavailable("snapshot_manifest_changed")
        manifest = read_json(raw)
        available = receipt(manifest["availableAt"])
        if type(manifest["version"]) is not int or manifest["version"] != 1 or available.date().isoformat() != manifest["exchangeDate"]:
            return unavailable("invalid_snapshot")
        if local.date() != available.date():
            return unavailable("snapshot_day_mismatch")
        if local < available:
            return unavailable("snapshot_not_yet_available")
        blobs, roster_at = _verified_blobs(snapshot_dir, manifest["sources"], available)
        boards = {name: _board(blobs[name], entry[2]) for name, entry in _BOARD_FILES.items()}
        roster = _roster(blobs[_ROSTER], roster_at)
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError, pl.exceptions.PolarsError):
        return unavailable("invalid_snapshot")

    facts = {}
    evidence = {"revision_ids": (manifest_sha256,), "available_at": available.isoformat()}
    for segment, listed in roster.items():
        risk = boards[f"{segment}-risk.json"]
        delisting = boards[f"{segment}-delisting.json"]
        expected = {symbol: name for symbol, name in listed.items() if name.startswith(("ST", "*ST"))}
        consistent = risk == expected and not risk.keys() & delisting.keys()
        for symbol in selected.intersection(listed):
            if not consistent:
                facts[symbol] = EvidenceFact(reason="conflicting_evidence", **evidence)
            elif symbol in delisting:
                facts[symbol] = EvidenceFact(reason="delisting_in_progress", **evidence)
            else:
                facts[symbol] = EvidenceFact(symbol in risk, None, **evidence)
    return {symbol: facts.get(symbol, EvidenceFact(reason="security_not_in_active_roster", **evidence)) for symbol in selected}


def _now():
    return datetime.now(SHANGHAI)


def capture_sse_st_snapshot(root: Path, client: httpx.Client, *, clock=_now) -> dict:
    """Capture all official Shanghai risk tabs for a verified current trading session."""
    from app.services.tickdesk_market_data import read_calendar_snapshot
    from app.services.tickdesk_quote_feed import quote_session

    now = clock()
    day = now.date().isoformat()
    target = root / SSE_ST_ARCHIVE / day
    if target.is_dir():
        return {"status": "already_recorded", "manifestSha256": sha256(read_bytes(target / MANIFEST, MANIFEST_LIMIT)).hexdigest()}
    if target.exists() or target.is_symlink():
        raise FileExistsError("unexpected Shanghai archive target")
    at = int(now.timestamp() * 1000)
    calendar = read_calendar_snapshot(root, at)
    if calendar is None or calendar[1].get("source") != "fuyao" or quote_session(root, at) != "open":
        raise ValueError("Shanghai risk-board collection requires a verified open session")
    listing = root / "tickdesk-market/listing-observations" / f"{day}.parquet"
    roster = read_bytes(listing, 2 * 1024 * 1024)
    listing_clocks = pl.read_parquet(BytesIO(roster), columns=["as_of"])["as_of"].unique().to_list()
    if len(listing_clocks) != 1 or type(listing_clocks[0]) is not int:
        raise ValueError("Shanghai roster has ambiguous receipt time")
    roster_at = datetime.fromtimestamp(listing_clocks[0] / 1000, SHANGHAI)
    if roster_at.date() != now.date() or roster_at > now:
        raise ValueError("Shanghai roster observation is outside the verified session date")
    listed = _roster(roster, roster_at)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".capture-", dir=target.parent) as temp:
        stage = Path(temp) / day
        stage.mkdir()
        sources = {}

        def store(name, url, data, observed_at):
            (stage / name).write_bytes(data)
            sources[name] = {"url": url, "receivedAt": observed_at.isoformat(),
                             "bytes": len(data), "sha256": sha256(data).hexdigest()}

        store(_ROSTER, SSE_LIST, roster, roster_at)
        for name, (url, referer, sql) in _BOARD_FILES.items():
            data = bytearray()
            with client.stream("GET", url, headers={"Referer": referer, "Cache-Control": "no-cache"}) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    if len(data) + len(chunk) > MANIFEST_LIMIT:
                        raise ValueError("Shanghai board response exceeds size bound")
                    data.extend(chunk)
            arrived = clock()
            _board(data, sql)
            store(name, url, data, arrived)
        available = clock()
        if available.date() != now.date() or quote_session(root, int(available.timestamp() * 1000)) != "open":
            raise ValueError("Shanghai risk-board capture crossed the verified open session")
        manifest = {"version": 1, "exchangeDate": day, "availableAt": available.isoformat(), "sources": sources}
        encoded = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        (stage / MANIFEST).write_bytes(encoded)
        digest = sha256(encoded).hexdigest()
        coverage = read_sse_st_board(stage, [symbol for board in listed.values() for symbol in board], available,
                                     manifest_sha256=digest)
        if not coverage or any(fact.reason not in (None, "delisting_in_progress") for fact in coverage.values()):
            raise ValueError("Shanghai risk boards disagree with the official active roster")
        stage.rename(target)
    return {"status": "recorded", "availableAt": available.isoformat(), "manifestSha256": digest,
            "checkedAStocks": len(coverage), "riskAStocks": sum(fact.value is True for fact in coverage.values())}


def main():
    parser = argparse.ArgumentParser(description=capture_sse_st_snapshot.__doc__)
    parser.add_argument("--root", type=Path, required=True, help="market evidence data directory")
    args = parser.parse_args()
    with httpx.Client(timeout=8, follow_redirects=False, headers={"User-Agent": "Mozilla/5.0"}) as client:
        print(json.dumps(capture_sse_st_snapshot(args.root.resolve(strict=True), client), ensure_ascii=False))


if __name__ == "__main__":
    main()
