"""Attribute missing daily bars to official exchange whole-day suspensions.

The archive under ``tickdesk-market/suspension-observations`` stores the exchange
responses per trading day. Attribution only accepts whole-day suspensions: a bar is
expected for every listed security, so an intraday halt does not explain a missing bar.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import date, datetime
from pathlib import Path

import httpx
import polars as pl

from app.services.fs_utils import atomic_write_text
from app.services.tickdesk_adapter import timestamp

logger = logging.getLogger(__name__)

VERSION = 1
ARCHIVE_DIR = "tickdesk-market/suspension-observations"
STATE_PATH = "tickdesk-market/history-coverage.json"
SSE_URL = "https://query.sse.com.cn/commonSoaQuery.do"
SSE_REFERER = "https://www.sse.com.cn/disclosure/dealinstruc/suspension/stock/"
SZSE_URL = "https://www.szse.cn/api/report/ShowReport/data"
SZSE_REFERER = "https://www.szse.cn/disclosure/memo/index.html"
SSE_SOURCE = {"name": "上海证券交易所停复牌信息", "url": SSE_REFERER,
              "api": f"{SSE_URL} (sqlId=GW_PL_JYTS_TFPXX)"}
SZSE_SOURCE = {"name": "深圳证券交易所停复牌提示", "url": SZSE_REFERER,
               "api": f"{SZSE_URL} (CATALOGID=1798)"}
MAX_FETCH_DAYS = 10


class ArchiveUnavailableError(ValueError):
    """No verified official suspension archive is available for attribution."""


def archive_path(root, day: str) -> Path:
    return Path(root) / ARCHIVE_DIR / f"{day}.json"


def archive_signature(root):
    """Cheap signature of the archived responses so unchanged loops skip recomputation."""
    directory = Path(root) / ARCHIVE_DIR
    if not directory.is_dir():
        return None
    entries = []
    for path in sorted(directory.glob("*.json")):
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append([path.name, stat.st_size, stat.st_mtime_ns])
    if not entries:
        return None
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()


def _compact(value):    return date(int(value[:4]), int(value[4:6]), int(value[6:8]))


def _load_archive(root):
    directory = Path(root) / ARCHIVE_DIR
    if not directory.is_dir():
        raise ArchiveUnavailableError("SUSPENSION_ARCHIVE_MISSING")
    payloads = []
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("version") == VERSION and isinstance(payload.get("day"), str):
            payloads.append(payload)
    if not payloads:
        raise ArchiveUnavailableError("SUSPENSION_ARCHIVE_EMPTY")
    return payloads


def _suspended_sse(payloads):
    suspended = {}
    for payload in payloads:
        try:
            day = date.fromisoformat(payload["day"])
        except (KeyError, ValueError):
            continue
        for row in payload.get("sse") or []:
            if not isinstance(row, dict):
                continue
            code = row.get("productCode") or ""
            if not (len(code) == 6 and code.isdigit()):
                continue
            if row.get("controlType") != "TR" or row.get("type") not in ("LSTP", "LXTP"):
                continue
            if row.get("stopTime") not in ("", "WH"):
                continue
            start, end = row.get("startStopDate") or "", row.get("endStopDate") or ""
            if len(start) != 8:
                continue
            try:
                start_day = _compact(start)
                end_day = _compact(end) if len(end) == 8 else None
            except ValueError:
                continue
            if start_day <= day and (end_day is None or end_day >= day):
                suspended.setdefault(f"{code}.SH", set()).add(payload["day"])
    return suspended


def _suspended_szse(payloads):
    starts = {}
    for payload in payloads:
        for row in payload.get("szse") or []:
            if not isinstance(row, dict):
                continue
            code = row.get("zqdm") or ""
            if not (len(code) == 6 and code.isdigit()):
                continue
            symbol = f"{code}.SZ"
            kind = row.get("tpsj") or ""
            start_text = row.get("tpkssj") or ""
            resume_text = row.get("fpkssj") or ""
            try:
                start_day = date.fromisoformat(start_text.split(" ")[0]) if start_text else None
            except ValueError:
                start_day = None
            if kind == "停牌" and start_text.endswith("开市") and start_day:
                starts.setdefault(symbol, []).append({"start": start_day, "resume": None})
            elif kind == "1天" and start_text.endswith("开市") and start_day:
                resume = None
                if resume_text.endswith("开市"):
                    try:
                        resume = date.fromisoformat(resume_text.split(" ")[0])
                    except ValueError:
                        resume = None
                starts.setdefault(symbol, []).append({"start": start_day, "resume": resume})
            elif kind == "取消停牌" and resume_text.endswith("开市"):
                try:
                    resume_day = date.fromisoformat(resume_text.split(" ")[0])
                except ValueError:
                    continue
                open_episodes = [episode for episode in starts.get(symbol, []) if episode["resume"] is None]
                if open_episodes:
                    open_episodes[-1]["resume"] = resume_day
    suspended = {}
    for symbol, episodes in starts.items():
        days = set()
        for episode in episodes:
            end = episode["resume"] or date(2099, 12, 31)
            day = episode["start"]
            while day < end:
                if day.weekday() < 5:
                    days.add(day.isoformat())
                day = date.fromordinal(day.toordinal() + 1)
        suspended[symbol] = days
    return suspended


def suspended_days(root, sessions):
    """Return symbol -> set of session strings with an official whole-day suspension."""
    payloads = _load_archive(root)
    sessions = set(sessions)
    merged = {}
    for source in (_suspended_sse(payloads), _suspended_szse(payloads)):
        for symbol, days in source.items():
            merged.setdefault(symbol, set()).update(days & sessions)
    return merged, {"sseDays": sum(1 for payload in payloads if payload.get("sse")),
                    "szseDays": sum(1 for payload in payloads if payload.get("szse")),
                    "archiveDays": len(payloads)}


def _observed_days(root, sessions):
    observed = {}
    for day in sessions:
        part = Path(root) / "kline_daily" / f"date={day}" / "part.parquet"
        if part.is_file():
            observed[day] = set(pl.read_parquet(part, columns=["symbol"])["symbol"].to_list())
        else:
            observed[day] = set()
    return observed


def attribute(root, coverage):
    """Compute the suspension attribution for a persisted history-coverage result."""
    sessions = sorted(coverage.get("dates") or {})
    symbols = coverage.get("symbols") or {}
    if not sessions or not symbols:
        raise ArchiveUnavailableError("SUSPENSION_ARCHIVE_NO_COVERAGE")
    suspended, archive_sources = suspended_days(root, sessions)
    observed = _observed_days(root, sessions)
    missing = 0
    attributed = 0
    by_day = {}
    unexplained = []
    for symbol, item in symbols.items():
        try:
            listed_index = len(sessions) - int(item["expected"])
        except (KeyError, TypeError, ValueError):
            continue
        if listed_index < 0:
            continue
        for day in sessions[listed_index:]:
            if symbol in observed[day]:
                continue
            missing += 1
            if day in suspended.get(symbol, ()):
                attributed += 1
                by_day[day] = by_day.get(day, 0) + 1
            else:
                unexplained.append({"day": day, "symbol": symbol})
    return {
        "version": VERSION,
        "calculatedAt": timestamp(datetime.now().astimezone()),
        "window": {"start": sessions[0], "end": sessions[-1], "sessions": len(sessions)},
        "missingCells": missing,
        "attributedCells": attributed,
        "unexplainedCells": len(unexplained),
        "attributedByDay": dict(sorted(by_day.items())),
        "unexplained": unexplained,
        "sources": archive_sources,
        "archiveSignature": archive_signature(root),
        "status": "all_attributed" if missing == attributed else "partial",
    }


def enrich(root, coverage):
    """Return the coverage mapping with attribution when the archive can explain it."""
    existing = coverage.get("attribution") if isinstance(coverage, dict) else None
    if existing and existing.get("archiveSignature") == archive_signature(root):
        return coverage
    try:
        attribution = attribute(root, coverage)
    except ArchiveUnavailableError as error:
        logger.info("Suspension attribution skipped (%s)", error)
        return coverage
    if coverage.get("attribution") == attribution:
        return coverage
    return {**coverage, "attribution": attribution}


def attach(root, coverage):
    """Add attribution to a coverage mapping and persist it when it changed."""
    enriched = enrich(root, coverage)
    if enriched is coverage:
        return coverage
    try:
        atomic_write_text(Path(root) / STATE_PATH, json.dumps(enriched))
    except OSError:
        logger.warning("Suspension attribution could not be persisted")
    return enriched


async def _fetch_sse_day(client, day):
    rows = []
    page = 1
    total = None
    while page <= 10:
        params = {"isPagination": "true", "sqlId": "GW_PL_JYTS_TFPXX",
                  "startStopDate": day.strftime("%Y%m%d"), "endStopDate": day.strftime("%Y%m%d"),
                  "productCode": "", "keyWords": "", "pageHelp.pageSize": 100,
                  "pageHelp.pageNo": page, "pageHelp.beginPage": page, "pageHelp.endPage": page,
                  "pageHelp.cacheSize": 1}
        data = await _request(client, SSE_URL, params, SSE_REFERER)
        batch = data.get("result") or []
        if total is None:
            total = (data.get("pageHelp") or {}).get("total")
        if type(total) is not int or total < 0 or len(batch) > 100:
            raise ValueError("incomplete SSE suspension list")
        rows.extend(batch)
        if len(rows) >= total:
            break
        page += 1
    if type(total) is not int or len(rows) != total:
        raise ValueError("incomplete SSE suspension list")
    return rows


async def _fetch_szse_day(client, day):
    rows = []
    page = 1
    pages = None
    stamp = day.isoformat()
    while page <= 10:
        params = {"SHOWTYPE": "JSON", "CATALOGID": "1798", "TABKEY": "tab1",
                  "txtKsrq": stamp, "txtZzrq": stamp, "PAGENO": page}
        data = await _request(client, SZSE_URL, params, SZSE_REFERER)
        tab = data[0]
        meta = tab["metadata"]
        if pages is None:
            pages = meta["pagecount"]
            if not 0 <= pages <= 10:
                raise ValueError("unexpected SZSE suspension page count")
        rows.extend(tab["data"] if meta["recordcount"] else [])
        if page >= pages:
            break
        page += 1
    return rows


async def _request(client, url, params, referer):
    async with client.stream("GET", url, params=params,
                             headers={"Referer": referer, "User-Agent": "Mozilla/5.0"},
                             timeout=20) as response:
        response.raise_for_status()
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            if len(raw) + len(chunk) > 2 * 1024 * 1024:
                raise ValueError("suspension response too large")
            raw.extend(chunk)
    return json.loads(raw)


async def ensure_archive(root, sessions, *, limit=MAX_FETCH_DAYS):
    """Fetch missing official suspension lists for the most recent sessions."""
    recent = [day for day in sorted(sessions) if day][-limit:]
    missing = [day for day in recent if not archive_path(root, day).is_file()]
    if not missing:
        return []
    fetched = []
    async with httpx.AsyncClient() as client:
        for day_text in missing:
            day = date.fromisoformat(day_text)
            try:
                sse = await _fetch_sse_day(client, day)
                szse = await _fetch_szse_day(client, day)
            except (httpx.HTTPError, ValueError, KeyError, IndexError, json.JSONDecodeError) as error:
                logger.warning("Exchange suspension fetch failed for %s (%s)", day_text, type(error).__name__)
                continue
            payload = {"version": VERSION, "day": day_text,
                       "capturedAt": timestamp(datetime.now().astimezone()),
                       "sse": sse, "szse": szse,
                       "sources": {"sse": SSE_SOURCE, "szse": SZSE_SOURCE}}
            target = archive_path(root, day_text)
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(target, json.dumps(payload))
            fetched.append(day_text)
            await asyncio.sleep(0.3)
    return fetched
