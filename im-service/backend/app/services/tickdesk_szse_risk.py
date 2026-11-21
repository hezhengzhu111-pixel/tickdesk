"""Read a sealed current-day SZSE risk-warning board and active A-share roster."""

from __future__ import annotations

import html
import io
import json
import re
import tempfile
import time
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from zipfile import BadZipFile, ZipFile

import httpx
import polars as pl

from app.quant_evidence import SHANGHAI, EvidenceFact
from app.services.tickdesk_adapter import fingerprint, timestamp
from app.services.tickdesk_risk_archive import (
    MANIFEST as _MANIFEST,
)
from app.services.tickdesk_risk_archive import (
    MANIFEST_LIMIT as _MANIFEST_LIMIT,
)
from app.services.tickdesk_risk_archive import (
    archived_manifests,
)
from app.services.tickdesk_risk_archive import (
    read_bytes as _read_bytes,
)
from app.services.tickdesk_risk_archive import (
    read_json as _read_json,
)
from app.services.tickdesk_risk_archive import (
    receipt as _receipt,
)

_API = "https://www.szse.cn/api/report/ShowReport"
_ROSTER = f"{_API}?SHOWTYPE=xlsx&CATALOGID=1110&TABKEY=tab1"
_WORKBOOK = f"{_API}?SHOWTYPE=xlsx&CATALOGID=fxjsb&TABKEY=tab1"
_BOARD_PAGE = "https://www.szse.cn/disclosure/listed/warn/index.html"
_ROSTER_PAGE = "https://www.szse.cn/market/product/stock/list/"
_NOTICE = "300027-2026-031.pdf"
_NOTICE_URL = ("https://disc.static.szse.cn/download/disc/disk03/finalpage/2026-04-29/"
               "db0b4786-9fcc-48a2-a462-98d897250412.PDF")
SZSE_ST_ARCHIVE = Path("tickdesk-market/szse-risk-snapshots")
_SYMBOL = re.compile(r"(?:00|30)[0-9]{4}\.SZ")
_CODE = re.compile(r"[0-9]{6}")
_LIST_COLUMNS = {"zqdm": "证券代码", "gsjc": "证券简称"}
_TAB_NAMES = {"tab1": "风险警示股票", "tab2": "退市整理股票"}
_PAGE_SIZE = 20
_MAX_PAGES = 16


class _ConflictingSnapshotError(ValueError):
    pass


def _excel(data: bytes) -> pl.DataFrame:
    with ZipFile(io.BytesIO(data)) as archive:
        if sum(item.file_size for item in archive.infolist()) > 16 * 1024 * 1024:
            raise ValueError("snapshot workbook exceeds expected size")
    return pl.read_excel(io.BytesIO(data), infer_schema_length=0)


def _risk_rows(rows):
    pairs = list(rows)
    if any(not isinstance(code, str) or not _CODE.fullmatch(code) or not isinstance(name, str)
           or not name.strip() for code, name in pairs):
        raise ValueError("malformed risk-board entry")
    codes = [code for code, _ in pairs]
    if codes != sorted(set(codes)):
        raise ValueError("duplicate or unordered risk-board entries")
    return dict(pairs)


def _page_url(page):
    return f"{_API}/data?SHOWTYPE=JSON&CATALOGID=fxjsb&TABKEY=tab1&PAGENO={page}"


def _read_sweep(blobs, sweep, page_count):
    pairs, coverage = [], {}
    for page in range(1, page_count + 1):
        seen = set()
        for item in _read_json(blobs[f"sweep{sweep}-page-{page}.json"]):
            meta, rows = item["metadata"], item["data"]
            tab = meta["tabkey"]
            if (tab in seen or meta["name"] != _TAB_NAMES[tab] or meta["catalogid"] != "fxjsb"
                    or meta["cols"] != _LIST_COLUMNS or meta["conditions"] != [] or meta["pagesize"] != _PAGE_SIZE
                    or not isinstance(rows, list)):
                raise ValueError("unrecognized Shenzhen risk-board report")
            seen.add(tab)
            records, pages = meta["recordcount"], meta["pagecount"]
            if (type(records) is not int or type(pages) is not int or records < 0
                    or pages != (records + _PAGE_SIZE - 1) // _PAGE_SIZE):
                raise ValueError("inconsistent Shenzhen risk-board pagination")
            summary = {"rows": records, "pages": pages, "pageSize": _PAGE_SIZE}
            if summary != coverage.setdefault(tab, summary):
                raise ValueError("risk-board pagination changed during observation")
            if tab == "tab2":
                if records or rows:
                    raise ValueError("nonempty delisting tab requires a complete separate observation")
                continue
            if (meta["pageno"] != page or pages != page_count
                    or len(rows) != min(_PAGE_SIZE, records - (page - 1) * _PAGE_SIZE)):
                raise ValueError("incomplete Shenzhen risk-board page")
            pairs.extend((row["zqdm"], html.unescape(re.sub(r"<[^>]+>", "", row["gsjc"])).strip())
                         for row in rows)
        if seen != set(_TAB_NAMES):
            raise ValueError("missing Shenzhen risk-board tab")
    return _risk_rows(pairs), coverage


def _current_board(blobs, pages):
    sheet = _excel(blobs["risk-board.xlsx"])
    if sheet.columns != list(_LIST_COLUMNS.values()):
        raise ValueError("unexpected Shenzhen risk-board worksheet")
    risk = _risk_rows(sheet.rows())
    first, coverage = _read_sweep(blobs, 1, pages)
    second, other_coverage = _read_sweep(blobs, 2, pages)
    if risk != first or first != second or coverage != other_coverage:
        raise _ConflictingSnapshotError("risk-board workbook disagrees with paged reports")
    return risk, coverage


def _verified_files(directory, manifest, started, available):
    pages = manifest["pagesRead"]
    if type(pages) is not int or not 1 <= pages <= _MAX_PAGES:
        raise ValueError("unrecognized risk-board page count")
    expected = {"active-roster.xlsx": _ROSTER, "risk-board.xlsx": _WORKBOOK}
    expected.update({f"sweep{sweep}-page-{page}.json": _page_url(page)
                     for sweep in (1, 2) for page in range(1, pages + 1)})
    sources = manifest["sources"]
    if _NOTICE in sources:
        # The first dossier included a historical disclosure as supplemental evidence.
        # It is bound to the revision without contributing to the current board status.
        expected[_NOTICE] = _NOTICE_URL
    if set(sources) != set(expected):
        raise ValueError("unexpected snapshot sources")
    blobs = {}
    receipts = []
    for name, reference in expected.items():
        source = sources[name]
        data = _read_bytes(directory / name, 2 * 1024 * 1024 if name == "active-roster.xlsx" else 128 * 1024)
        received = _receipt(source["receivedAt"])
        if (source["url"] != reference or not started <= received <= available or type(source["bytes"]) is not int
                or len(data) != source["bytes"] or sha256(data).hexdigest() != source["sha256"]):
            raise ValueError("snapshot source differs from its receipt")
        blobs[name] = data
        receipts.append((name, reference, source["sha256"], received.isoformat(), len(data)))
    revision = fingerprint(["szse_risk_board", started.isoformat(), available.isoformat(), sorted(receipts)])
    return blobs, revision


def archived_st_manifests(root: Path) -> dict[str, str]:
    """Freeze the published day/digest index without loading raw responses."""
    return archived_manifests(root / SZSE_ST_ARCHIVE)


def read_szse_st_snapshot(snapshot_dir: Path, symbol: str, cutoff: datetime) -> EvidenceFact:
    """Read a documented positive or negative observation available on the cutoff's Shanghai date."""
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        return EvidenceFact(reason="unsupported_symbol")
    return read_szse_st_board(snapshot_dir, [symbol], cutoff)[symbol]


def read_szse_st_board(snapshot_dir: Path, symbols, cutoff: datetime, *, manifest_sha256=None) -> dict[str, EvidenceFact]:
    """Verify the raw board and roster once for all Shenzhen securities at one cutoff."""
    selected = {symbol for symbol in symbols if isinstance(symbol, str) and _SYMBOL.fullmatch(symbol)}
    if not selected:
        return {}

    def unavailable(reason, **evidence):
        fact = EvidenceFact(reason=reason, **evidence)
        return dict.fromkeys(selected, fact)

    if not isinstance(cutoff, datetime) or cutoff.utcoffset() is None:
        return unavailable("invalid_cutoff")
    local = cutoff.astimezone(SHANGHAI)
    try:
        body = _read_bytes(snapshot_dir / _MANIFEST, _MANIFEST_LIMIT)
        if manifest_sha256 is not None and sha256(body).hexdigest() != manifest_sha256:
            return unavailable("snapshot_manifest_changed")
        manifest = _read_json(body)
    except FileNotFoundError:
        return unavailable("history_not_provided")
    except (OSError, ValueError, RecursionError):
        return unavailable("invalid_snapshot")
    try:
        available = _receipt(manifest["availableAt"])
        started = _receipt(manifest["collectionStartedAt"])
        if (started > available or started.date() != available.date()
                or available.date().isoformat() != manifest["exchangeDate"]
                or type(manifest["availableAtMs"]) is not int or timestamp(available) != manifest["availableAtMs"]):
            return unavailable("invalid_snapshot")
        if local.date() != available.date():
            return unavailable("snapshot_day_mismatch")
        if local < available:
            return unavailable("snapshot_not_yet_available")
        blobs, revision = _verified_files(snapshot_dir, manifest, started, available)
        evidence = {"revision_ids": (revision,), "available_at": available.isoformat()}
        try:
            risk, coverage = _current_board(blobs, manifest["pagesRead"])
            if coverage != manifest["coverage"] or len(risk) != manifest["workbookRows"]:
                raise _ConflictingSnapshotError("risk-board report disagrees with its manifest")
            roster = _excel(blobs["active-roster.xlsx"])
        except _ConflictingSnapshotError:
            return unavailable("conflicting_evidence", **evidence)
        if roster.height != manifest["activeSecurityCount"]:
            raise ValueError("incomplete Shenzhen active securities roster")
        listings = {symbol[:6]: [] for symbol in selected}
        for code, board in roster.select("A股代码", "板块").iter_rows():
            if code in listings:
                listings[code].append(board)
        facts = {}
        for symbol in selected:
            code = symbol[:6]
            boards = listings[code]
            if len(boards) > 1:
                facts[symbol] = EvidenceFact(reason="conflicting_evidence", **evidence)
            elif boards not in (["主板"], ["创业板"]):
                facts[symbol] = EvidenceFact(reason="security_not_listed", **evidence)
            else:
                facts[symbol] = EvidenceFact(value=code in risk, reason=None, **evidence)
        return facts
    except (BadZipFile, OSError, ValueError, KeyError, TypeError, RecursionError, pl.exceptions.PolarsError):
        return unavailable("invalid_snapshot")


def capture_szse_st_snapshot(snapshot_dir: Path) -> dict:
    """Collect one official dossier and publish it under a new directory."""
    if snapshot_dir.exists() or snapshot_dir.is_symlink():
        raise FileExistsError(snapshot_dir)
    if not snapshot_dir.parent.is_dir():
        raise FileNotFoundError(snapshot_dir.parent)
    started = datetime.now(SHANGHAI)
    deadline = time.monotonic() + 20
    sources, blobs = {}, {}

    def receive(client, name, reference, referer, size_limit):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("snapshot collection exceeded its time limit")
        headers = {"Referer": referer, "User-Agent": "Mozilla/5.0"}
        body = bytearray()
        with client.stream("GET", reference, headers=headers, timeout=min(7, remaining)) as response:
            response.raise_for_status()
            for block in response.iter_bytes():
                if time.monotonic() > deadline:
                    raise TimeoutError("snapshot collection exceeded its time limit")
                if len(body) + len(block) > size_limit:
                    raise ValueError("official response exceeds its size limit")
                body.extend(block)
            received = datetime.now(SHANGHAI)
            blobs[name] = bytes(body)
            sources[name] = {"url": reference, "referer": referer, "receivedAt": received.isoformat(),
                             "sha256": sha256(body).hexdigest(), "bytes": len(body),
                             "publisherHttpDate": response.headers.get("Date"), "age": response.headers.get("Age"),
                             "lastModified": response.headers.get("Last-Modified"), "etag": response.headers.get("ETag")}

    with httpx.Client(follow_redirects=False, trust_env=False) as client:
        receive(client, "active-roster.xlsx", _ROSTER, _ROSTER_PAGE, 2 * 1024 * 1024)
        receive(client, "risk-board.xlsx", _WORKBOOK, _BOARD_PAGE, 128 * 1024)
        receive(client, "sweep1-page-1.json", _page_url(1), _BOARD_PAGE, 128 * 1024)
        report = _read_json(blobs["sweep1-page-1.json"])
        counts = [entry["metadata"]["pagecount"] for entry in report if entry["metadata"]["tabkey"] == "tab1"]
        if len(counts) != 1 or type(counts[0]) is not int or not 1 <= counts[0] <= _MAX_PAGES:
            raise ValueError("unrecognized risk-board page count")
        pages = counts[0]
        for sweep, first in ((1, 2), (2, 1)):
            for page in range(first, pages + 1):
                receive(client, f"sweep{sweep}-page-{page}.json", _page_url(page), _BOARD_PAGE, 128 * 1024)
    risk, coverage = _current_board(blobs, pages)
    roster = _excel(blobs["active-roster.xlsx"])
    identities = roster.filter(pl.col("板块").is_in(["主板", "创业板"]), pl.col("A股代码").is_not_null())["A股代码"]
    if identities.is_empty() or identities.n_unique() != len(identities):
        raise ValueError("invalid Shenzhen listing identities")
    available = datetime.now(SHANGHAI)
    if remainder := available.microsecond % 1000:
        available += timedelta(microseconds=1000 - remainder)
    if (started.date() != available.date()
            or any(not started <= _receipt(source["receivedAt"]) <= available for source in sources.values())):
        raise ValueError("snapshot collection clock changed or crossed Shanghai dates")
    manifest = {"exchangeDate": available.date().isoformat(), "collectionStartedAt": started.isoformat(),
                "availableAt": available.isoformat(), "availableAtMs": timestamp(available),
                "pagesRead": pages, "coverage": coverage, "workbookRows": len(risk),
                "activeSecurityCount": roster.height, "workbookAndTwoJsonSweepsMatch": True,
                "publisherSnapshotOrStatusEffectiveTime": None, "sources": sources}
    with tempfile.TemporaryDirectory(prefix=".szse-risk-", dir=snapshot_dir.parent) as directory:
        stage = Path(directory) / "complete"
        stage.mkdir()
        for name, body in blobs.items():
            (stage / name).write_bytes(body)
        (stage / _MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        if snapshot_dir.exists() or snapshot_dir.is_symlink():
            raise FileExistsError(snapshot_dir)
        stage.rename(snapshot_dir)
    return manifest


def main():
    import argparse

    parser = argparse.ArgumentParser(description=capture_szse_st_snapshot.__doc__)
    parser.add_argument("snapshot_dir", type=Path)
    path = parser.parse_args().snapshot_dir
    manifest = capture_szse_st_snapshot(path)
    print(json.dumps({"availableAt": manifest["availableAt"], "coverage": manifest["coverage"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
