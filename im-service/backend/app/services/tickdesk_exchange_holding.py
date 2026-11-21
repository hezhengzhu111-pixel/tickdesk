"""Official exchange inputs for the holding-rule archive. Unverified axes stay missing."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, datetime, time, timedelta

import httpx
import polars as pl
from starlette.concurrency import run_in_threadpool

from app.services.tickdesk_adapter import CN, fingerprint, timestamp
from app.services.tickdesk_ai import AiError, local_request, now_ms
from app.services.tickdesk_exchange_master import read_exchange_master
from app.services.tickdesk_holding_rules import (
    FIELD,
    SSE_SUSPENSION,
    SSE_SUSPENSION_NAME,
    SZSE_AUCTION_OPEN,
    SZSE_MEMO,
    SZSE_SUSPENSION_NAME,
    szse_auction_evidence,
)

SSE_QUERY = "https://query.sse.com.cn/commonSoaQuery.do"
SZSE_REPORT = "https://www.szse.cn/api/report/ShowReport/data"
SZSE_SUSPENSION_REPORT = "1798"
SZSE_PAGE_LIMIT = 10
EXCHANGE = {
    "SH": ("上海证券交易所", "SSE", "sse-2026-41",
           "https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml",
           "https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml"),
    "SZ": ("深圳证券交易所", "SZSE", "szse-2026-20260424",
           "https://www.szse.cn/lawrules/rule/trade/current/t20260424_620190.html",
           "https://www.szse.cn/disclosure/notice/t20251222_618087.html"),
}
BOARDS = {("SH", "沪主板"): "main", ("SZ", "深主板"): "main", ("SZ", "创业板"): "chinext"}
RULE_FROM = date(2026, 7, 6)
CALENDAR_THROUGH = date(2026, 12, 31)
# The exchanges' dated 2026 notices list these closures. No projection is made into 2027.
CLOSED = ((1, 1, 3), (2, 15, 23), (4, 4, 6), (5, 1, 5), (6, 19, 21), (9, 25, 27), (10, 1, 7))
logger = logging.getLogger(__name__)


def _compact_date(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9]{8}", value)) and date.fromisoformat(value)


def suspension_episodes(rows, today):
    """Keep positive full-session trading suspension records, with stable episode identity."""
    episodes = {}
    for row in rows:
        code = row.get("productCode")
        if row.get("controlType") != "TR" or not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
            continue
        start, end = row.get("startStopDate"), row.get("endStopDate")
        try:
            if not _compact_date(start) or (end != "" and not _compact_date(end)):
                continue
        except ValueError:
            continue
        ongoing = row.get("type") == "LXTP" and start <= today and end == "" and row.get("stopTime") in ("", "WH")
        whole_day = row.get("type") == "LSTP" and start == today == end and row.get("stopTime") == "WH"
        if ongoing or whole_day:
            symbol = code + ".SH"
            episodes.setdefault(symbol, set()).add(start)
    return {symbol: next(iter(starts)) for symbol, starts in episodes.items() if len(starts) == 1}


async def read_sse_suspensions(client, day):
    stamp = day.strftime("%Y%m%d")
    params = {"isPagination": "true", "sqlId": "GW_PL_JYTS_TFPXX", "startStopDate": stamp,
              "endStopDate": stamp, "productCode": "", "keyWords": "", "pageHelp.pageSize": 100,
              "pageHelp.pageNo": 1, "pageHelp.beginPage": 1, "pageHelp.endPage": 1, "pageHelp.cacheSize": 1}
    data = await _read_report(client, SSE_QUERY, SSE_SUSPENSION, params)
    rows, total = data["result"], data["pageHelp"]["total"]
    if (data.get("sqlId") != params["sqlId"] or data.get("actionErrors") or data.get("fieldErrors")
            or not isinstance(rows, list) or type(total) is not int or not 0 <= total == len(rows) <= 100
            or any(not isinstance(row, dict) for row in rows)):
        raise ValueError("incomplete suspension evidence")
    return suspension_episodes(rows, stamp)


async def _read_report(client, endpoint, referer, params):
    async with client.stream("GET", endpoint, params=params, headers={
        "Referer": referer, "User-Agent": "Mozilla/5.0",
    }) as response:
        response.raise_for_status()
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            if len(raw) + len(chunk) > 1024 * 1024:
                raise ValueError("suspension response too large")
            raw.extend(chunk)
    return json.loads(raw)


async def read_szse_suspensions(client, day):
    """Read the complete official report for one trading day and retain only definite opening actions."""
    stamp = day.isoformat()
    params = {"SHOWTYPE": "JSON", "CATALOGID": SZSE_SUSPENSION_REPORT, "TABKEY": "tab1",
              "txtKsrq": stamp, "txtZzrq": stamp}
    rows = []
    expected = None
    seen = set()
    async with asyncio.timeout(15):
        for page in range(1, SZSE_PAGE_LIMIT + 1):
            tabs = await _read_report(client, SZSE_REPORT, SZSE_MEMO, {**params, "PAGENO": page})
            if not isinstance(tabs, list) or len(tabs) != 1:
                raise ValueError("incomplete Shenzhen suspension evidence")
            meta, entries = tabs[0]["metadata"], tabs[0]["data"]
            count, size, pages = (meta[key] for key in ("recordcount", "pagesize", "pagecount"))
            if (meta["catalogid"] != SZSE_SUSPENSION_REPORT or meta["tabkey"] != "tab1" or meta["pageno"] != page
                    or any(type(value) is not int for value in (count, size, pages, meta["pageno"])) or not 1 <= size <= 100
                    or count < 0 or not 0 <= pages == (count + size - 1) // size <= SZSE_PAGE_LIMIT
                    or meta["subname"] != (stamp if count else "")
                    or not isinstance(entries, list) or len(entries) != min(size, count - len(rows))
                    or (expected is not None and expected != (count, size, pages))):
                raise ValueError("incomplete Shenzhen suspension evidence")
            keys = {json.dumps(row, sort_keys=True) for row in entries}
            if keys & seen:
                raise ValueError("incomplete Shenzhen suspension evidence")
            seen.update(keys)
            expected = count, size, pages
            rows.extend(entries)
            if page >= pages:
                break
    return _szse_opening_actions(rows, stamp)


def _szse_opening_actions(rows, stamp):
    by_symbol = {}
    opening = stamp + " 开市"
    for row in rows:
        code, start, resume, duration, reason = (row[key] for key in ("zqdm", "tpkssj", "fpkssj", "tpsj", "tpyy"))
        if not all(isinstance(value, str) for value in (code, start, resume, duration, reason)):
            raise ValueError("invalid Shenzhen suspension record")
        if not (re.fullmatch(r"(?:00|30)[0-9]{4}", code) and (start.startswith(stamp + " ") or resume.startswith(stamp + " "))):
            continue
        action = None
        if start == opening and not resume and duration == "停牌" and "盘中" not in reason:
            action = "suspension"
        elif not start and resume == opening and duration == "取消停牌":
            action = "withdrawal"
        by_symbol.setdefault(code + ".SZ", []).append(action)
    actions = {}
    for symbol, entries in by_symbol.items():
        if "suspension" in entries and "withdrawal" in entries:
            actions[symbol] = "conflict"
        elif len(entries) == 1 and entries[0] is not None:
            actions[symbol] = entries[0]
    return actions


def _source(name, ref, as_of):
    return {"name": name, "ref": ref, "asOf": as_of}


def _at(day, hour=0, minute=0):
    return timestamp(datetime.combine(day, time(hour, minute), CN))


def _planned_session(day):
    return day.weekday() < 5 and not any(day.month == m and first <= day.day <= last for m, first, last in CLOSED)


def _calendar(exchange, start):
    name, code, _, _, url = EXCHANGE[exchange]
    known = _at(date(2025, 12, 23))  # dated announcement, hour not specified
    sessions = []
    end = min(CALENDAR_THROUGH, start + timedelta(days=14))
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        if _planned_session(day):
            sessions.append({"date": day.isoformat(), "firstExecutionAtMs": _at(day, 9, 25)})
    return {"revisionId": f"{code}-2026-holidays-20251222-auction-{start}", "source": _source(name + "休市安排", url, known),
            "availableAtMs": known, "coverageStart": str(start), "coverageEnd": str(end),
            "sessions": sessions}


def build_current_rules(listings, symbols, episodes, observed_at, *, szse_statuses=None):
    observed = datetime.fromtimestamp(observed_at / 1000, CN)
    day = observed.date()
    if not RULE_FROM <= day <= CALENDAR_THROUGH or not _planned_session(day):
        return []
    records = []
    calendars = {exchange: _calendar(exchange, day) for exchange in EXCHANGE}
    for listing in listings:
        symbol, exchange = listing.get("symbol"), listing.get("exchange")
        board = BOARDS.get((exchange, listing.get("board")))
        listed_at = listing.get("as_of")
        if (symbol not in symbols or board is None or listing.get("status") != "listed"
                or type(listed_at) is not int or not _at(day) <= listed_at <= observed_at):
            continue
        name, exchange_code, version, policy, _ = EXCHANGE[exchange]
        sources = [_source(name + "交易规则", policy, _at(date(2026, 4, 25))),
                   _source(listing["source"], listing["source_ref"], listed_at)]
        episode = episodes.get(symbol) if exchange == "SH" else None
        szse_status = (szse_statuses or {}).get(symbol) if exchange == "SZ" and observed.time() >= SZSE_AUCTION_OPEN else None
        suspended = bool(episode) or szse_status == "suspension"
        issues = ["riskStatus", "limitRatio", "referencePrice", "upperLimit", "lowerLimit", "companyActions"]
        if episode:
            sources.append(_source(SSE_SUSPENSION_NAME, f"{SSE_SUSPENSION}#{symbol}-{episode}", observed_at))
        if szse_status in {"suspension", "withdrawal", "conflict"}:
            sources.append(_source(SZSE_SUSPENSION_NAME, f"{SZSE_MEMO}#{szse_status}-{symbol}-{day:%Y%m%d}", observed_at))
        if not suspended:
            issues.append("tradingStatus")
        rule = {"symbol": symbol, "tradeDate": str(day), "channel": "auction", "exchange": exchange_code,
                "instrumentType": "stock", "board": board, "dayTrading": "NO", "settlement": "T+1",
                "quantityUnit": 100, "maxOrderQty": None, "minPriceTick": "0.01", "limitRatio": None,
                "adjustedPrevClose": None, "referencePrice": None, "upperLimit": None, "lowerLimit": None,
                "riskStatus": "unknown", "tradingStatus": "suspended" if suspended else "unknown",
                "ruleVersion": version, "sources": sources,
                "sourceTime": observed_at if episode or szse_status else listed_at,
                "dataStatus": "conflict" if szse_status == "conflict" else "missing", "issueFields": issues}
        rule["ruleSnapshotId"] = fingerprint(rule)
        records.append({"symbol": symbol, "field": FIELD, "effective_date": str(day), "available_at": observed.isoformat(),
                        "source": f"{exchange_code}-official-holding-v1", "value": [
                            {"rule": rule, "calendar": calendars[exchange], "companyActionsCheckedThrough": None},
                        ]})
    return records


def publish_current_rules(repo, symbols, episodes, observed_at, *, szse_statuses=None):
    listings = read_exchange_master(repo.store.data_dir, observed_at)
    fresh = build_current_rules(listings, symbols, episodes, observed_at, szse_statuses=szse_statuses)
    if not fresh:
        return {"status": "unavailable", "rows": 0, "suspensions": 0}
    existing = repo.get_quant_evidence_snapshot({FIELD})
    pending = []
    for row in fresh:
        previous = [old for old in existing.reference_by_symbol.get(row["symbol"], ())
                    if old["field"] == FIELD and old["effective_date"] == row["effective_date"]]
        if previous:
            latest = max(previous, key=lambda item: item["available_at"])
            old_records = latest["value"]
            if latest["source"] != row["source"] or old_records is None:
                continue  # explicit evidence/withdrawal has precedence over this partial producer
            old_rule, new_rule = old_records[0]["rule"], row["value"][0]["rule"]
            old_refs = {source["ref"] for source in old_rule["sources"]}
            new_refs = {source["ref"] for source in new_rule["sources"]}
            if old_refs == new_refs or (new_rule["tradingStatus"] == "unknown"
                                        and not szse_auction_evidence(new_rule, observed_at)):
                continue  # no new claim from a refresh or an absent suspension row
        pending.append(row)
    if pending:
        repo.write_quant_reference_evidence(pending)
    return {"status": "partial", "rows": len(fresh),
            "suspensions": sum(row["value"][0]["rule"]["tradingStatus"] == "suspended" for row in fresh),
            "published": len(pending), "observedAt": observed_at}


async def run_holding_evidence(app, repo):
    from app.services.tickdesk_company_actions import (
        collect_sse_cash_dividends,
        collect_szse_distributions,
    )

    async with httpx.AsyncClient(timeout=8, follow_redirects=False, trust_env=False) as client:
        while True:
            try:
                scope = await local_request("GET", "/internal/ai/v1/scope")
                symbols = set(scope["symbols"][:64])
                day = datetime.now(CN).date()
                if symbols and RULE_FROM <= day <= CALENDAR_THROUGH and _planned_session(day):
                    episodes = {}
                    suspension_status = "out_of_scope"
                    if any(s.endswith(".SH") for s in symbols):
                        try:
                            episodes = await read_sse_suspensions(client, day)
                            suspension_status = "observed"
                        except (httpx.HTTPError, KeyError, ValueError, TypeError):
                            suspension_status = "unavailable"
                    szse_statuses = {}
                    szse_status = "out_of_scope"
                    if any(s.endswith(".SZ") for s in symbols):
                        try:
                            szse_statuses = await read_szse_suspensions(client, day)
                            szse_status = "observed"
                        except (httpx.HTTPError, KeyError, ValueError, TypeError, TimeoutError):
                            szse_status = "unavailable"
                    observed = now_ms()
                    if datetime.fromtimestamp(observed / 1000, CN).date() == day:
                        publication = asyncio.create_task(run_in_threadpool(
                            publish_current_rules, repo, symbols, episodes, observed, szse_statuses=szse_statuses,
                        ))
                        try:
                            result = await asyncio.shield(publication)
                        except asyncio.CancelledError:
                            await asyncio.gather(publication, return_exceptions=True)
                            raise
                        state = {**result, "sseSuspension": suspension_status, "szseSuspension": szse_status}
                        app.state.tickdesk_holding_evidence = state
                        for field, collect in (("cashDividends", collect_sse_cash_dividends),
                                               ("szseDistributions", collect_szse_distributions)):
                            try:
                                state[field] = await collect(client, repo.store.data_dir, symbols)
                            except (KeyError, OSError, ValueError, TypeError, pl.exceptions.PolarsError) as error:
                                state[field] = {"status": "unavailable"}
                                logger.warning("TickDesk distribution archive refresh failed (%s)", type(error).__name__)
                else:
                    app.state.tickdesk_holding_evidence = {"status": "no_scope" if not symbols else "calendar_out_of_scope"}
            except (AiError, KeyError, OSError, ValueError, TypeError, pl.exceptions.PolarsError) as error:
                app.state.tickdesk_holding_evidence = {"status": "unavailable"}
                logger.warning("TickDesk holding evidence refresh failed (%s)", type(error).__name__)
            await asyncio.sleep(300)
