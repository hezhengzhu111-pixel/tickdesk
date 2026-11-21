"""Observe positive exchange distribution facts without inferring other holding rules."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, datetime
from decimal import Decimal

import httpx
from starlette.concurrency import run_in_threadpool

from app.services.fs_utils import atomic_write_text
from app.services.tickdesk_adapter import CN, _wire_decimal, timestamp
from app.services.tickdesk_exchange_master import read_exchange_master

SSE_COMPANY = "https://www.sse.com.cn/assortment/stock/list/info/company/index.shtml"
CNINFO_COMPANY = "https://www.cninfo.com.cn/new/snapshot/companyDetailCn"
_CNINFO_DATA = "https://www.cninfo.com.cn/data/yellowpages"
_CNINFO_CALLBACK = "tickdeskDistribution"
_SSE_QUERY = "https://query.sse.com.cn/commonQuery.do"
_CASH_DIVIDENDS = "COMMON_SSE_CP_GPJCTPZ_GPLB_LRFP_FH_L"
_PAGE_SIZE = 5
_OBSERVATIONS_PER_CYCLE = 4
_SSE_STOCK = re.compile(r"60[0-9]{4}\.SH")
_SZSE_STOCK = re.compile(r"(?:00|30)[0-9]{4}\.SZ")
logger = logging.getLogger(__name__)


def _directory(root, symbol):
    kind = "sse-cash-dividends" if symbol.endswith(".SH") else "szse-distributions"
    return root / "tickdesk-market" / "company-actions" / kind / symbol


async def _read_public_report(client, url, referer, params):
    raw = bytearray()
    async with client.stream("GET", url, params=params, headers={"Referer": referer, "User-Agent": "Mozilla/5.0"}) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes():
            if len(raw) + len(chunk) > 256 * 1024:
                raise ValueError("distribution report too large")
            raw.extend(chunk)
    return raw


def _persist_observation(path, symbol, arrived, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps({"version": 1, "symbol": symbol, "observedAt": timestamp(arrived),
                                        "events": events}, ensure_ascii=False))


def _exchange_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{8}", value):
        raise ValueError("invalid dividend date")
    return date.fromisoformat(value)


def _cash_events(raw, symbol, arrived, referer):
    meta = raw["pageHelp"]
    total = meta["total"]
    rows = raw["result"]
    if total == 0 and rows is None:
        rows = []
    if (raw["sqlId"] != _CASH_DIVIDENDS or raw.get("actionErrors") or raw.get("fieldErrors")
            or meta["pageNo"] != 1 or type(total) is not int or not 0 <= total <= 1000
            or not isinstance(rows, list) or len(rows) != min(total, _PAGE_SIZE)):
        raise ValueError("incomplete SSE dividend page")
    events = {}
    for row in rows:
        if row["COMPANY_CODE"] != symbol[:6] or row["A_STOCK_CODE"] != symbol[:6]:
            raise ValueError("unexpected SSE dividend symbol")
        record = _exchange_date(row["A_REG_DATE"])
        ex_date = _exchange_date(row["A_DIV_DATE"])
        raw_amount = row["A_BEFR_TAX_DIV"]
        if (record >= ex_date or not isinstance(raw_amount, str) or len(raw_amount) > 48
                or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", raw_amount)):
            raise ValueError("invalid SSE cash dividend")
        cash = Decimal(raw_amount)
        if cash <= 0 or ex_date > arrived.date():
            continue
        amount = _wire_decimal(cash)
        if amount is None:
            raise ValueError("SSE cash dividend exceeds available precision")
        event = {"symbol": symbol, "effectiveDate": str(ex_date), "recordDate": str(record),
                 "cashGrossPerShare": amount, "currency": "CNY",
                 "source": {"name": "上海证券交易所", "ref": referer, "asOf": timestamp(arrived)}}
        if ex_date in events and events[ex_date] != event:
            raise ValueError("conflicting SSE cash dividends")
        events[ex_date] = event
    return [events[day] for day in sorted(events)]


async def observe_sse_cash_dividends(client, root, symbol):
    if not _SSE_STOCK.fullmatch(symbol):
        return None
    started = datetime.now(CN)
    path = _directory(root, symbol) / f"{started.date()}.json"
    referer = f"{SSE_COMPANY}?COMPANY_CODE={symbol[:6]}"
    params = {"sqlId": _CASH_DIVIDENDS, "COMPANY_CODE": symbol[:6], "IS_STAR": "0",
              "CONDITION_ZBA": "1", "CONDITION_ZBB": "", "isPagination": "true",
              "pageHelp.pageSize": _PAGE_SIZE, "pageHelp.pageNo": 1, "pageHelp.beginPage": 1,
              "pageHelp.cacheSize": 1, "pageHelp.endPage": 1}
    try:
        if path.is_file():
            return read_sse_cash_dividends(root, symbol, timestamp(started))
        async with asyncio.timeout(8):
            raw = await _read_public_report(client, _SSE_QUERY, referer, params)
        arrived = datetime.now(CN)
        if arrived.date() != started.date():
            raise ValueError("SSE dividend observation crossed the exchange day")
        events = _cash_events(json.loads(raw), symbol, arrived, referer)
        _persist_observation(path, symbol, arrived, events)
        return events
    except (httpx.HTTPError, TimeoutError, OSError, KeyError, TypeError, ValueError) as error:
        logger.warning("TickDesk SSE cash dividend unavailable (%s)", type(error).__name__)
        return None


def read_sse_cash_dividends(root, symbol, at, *, effective_on=None):
    if not _SSE_STOCK.fullmatch(symbol):
        return []
    return _read_distributions(root, symbol, at, effective_on)


def read_szse_distributions(root, symbol, at, *, effective_on=None):
    if not _SZSE_STOCK.fullmatch(symbol):
        return []
    return _read_distributions(root, symbol, at, effective_on)


def _read_distributions(root, symbol, at, effective_on=None):
    before = datetime.fromtimestamp(at / 1000, CN).date().isoformat()
    for path in sorted(_directory(root, symbol).glob("????-??-??.json"), reverse=True):
        if path.stem > before:
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        if (record.get("version") != 1 or record.get("symbol") != symbol
                or type(record.get("observedAt")) is not int or not 0 < record["observedAt"] <= at):
            continue
        if effective_on is None:
            return record["events"]
        return [event for event in record["events"] if event["effectiveDate"] == effective_on.isoformat()]
    return []


async def _cninfo_report(client, endpoint, referer, params):
    data = await _read_public_report(client, f"{_CNINFO_DATA}/{endpoint}", referer,
                                     {**params, "jsonpCallback": _CNINFO_CALLBACK})
    wrapper = data.decode("utf-8").strip().removesuffix(";").strip()
    prefix = _CNINFO_CALLBACK + "("
    if not wrapper.startswith(prefix) or not wrapper.endswith(")"):
        raise ValueError("invalid CNINFO dividend response")
    return json.loads(wrapper[len(prefix):-1], parse_float=Decimal)


def _cninfo_day(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("invalid CNINFO distribution date")
    return date.fromisoformat(value)


def _cninfo_amount(value):
    if value is None:
        return None
    if type(value) not in (int, Decimal):
        raise ValueError("invalid CNINFO distribution amount")
    amount = Decimal(value)
    if not amount.is_finite() or amount < 0 or (wire := _wire_decimal(amount)) is None:
        raise ValueError("invalid CNINFO distribution amount")
    return wire


def _cninfo_events(rows, symbol, arrived, referer):
    if not isinstance(rows, list) or len(rows) > 10:
        raise ValueError("invalid CNINFO distribution report size")
    events = {}
    for row in rows:
        if not isinstance(row, dict) or not {"F010N", "F011N", "F012N", "F013D", "F014D", "F015D", "F016D", "F017D"} <= row.keys():
            raise ValueError("invalid CNINFO distribution row")
        cash, bonus, reserve = (_cninfo_amount(row[key]) for key in ("F010N", "F011N", "F012N"))
        if not any(value not in (None, "0") for value in (cash, bonus, reserve)) or row["F014D"] is None:
            continue
        announced, effective, record = (_cninfo_day(row[key]) for key in ("F013D", "F014D", "F015D"))
        if not announced <= record < effective:
            raise ValueError("inconsistent CNINFO distribution dates")
        if effective > arrived.date():
            continue
        cash_date, share_date = (_cninfo_day(row[key]) if row[key] is not None else None for key in ("F016D", "F017D"))
        if any(day is not None and day < effective for day in (cash_date, share_date)):
            raise ValueError("inconsistent CNINFO payment schedule")
        event = {"symbol": symbol, "announcementDate": str(announced), "effectiveDate": str(effective), "recordDate": str(record),
                 "cashPerShare": cash, "bonusSharesPerShare": bonus, "capitalReserveSharesPerShare": reserve,
                 "cashDistributionDate": str(cash_date) if cash_date is not None else None,
                 "shareDistributionDate": str(share_date) if share_date is not None else None, "currency": "CNY",
                 "source": {"name": "巨潮资讯网", "ref": referer, "asOf": timestamp(arrived)}}
        if effective in events and events[effective] != event:
            raise ValueError("conflicting CNINFO distribution rows")
        events[effective] = event
    return [events[day] for day in sorted(events)]


async def observe_szse_distributions(client, root, symbol):
    if not _SZSE_STOCK.fullmatch(symbol):
        return None
    started = datetime.now(CN)
    path = _directory(root, symbol) / f"{started.date()}.json"
    referer = f"{CNINFO_COMPANY}?code={symbol[:6]}"
    params = {"scode": symbol[:6], "type": 1}
    try:
        previous = read_szse_distributions(root, symbol, timestamp(started))
        if path.is_file():
            return previous
        async with asyncio.timeout(8):
            identity = (await _cninfo_report(client, "getIndexData", referer, params))["codeInfo"]
            sign = identity["F003N"]
            flag = identity["F002N"]
            if (identity["SECCODE"] != symbol[:6] or not isinstance(sign, str) or not re.fullmatch(r"[0-9]", sign)
                    or type(flag) is not int or flag not in (0, 1)):
                raise ValueError("unexpected CNINFO company identity")
            payload = await _cninfo_report(client, "singleStockData", referer, {
                **params, "sign": sign, "mergerMark": "dividendData",
            }) if flag else {"dividendData": []}
        arrived = datetime.now(CN)
        if arrived.date() != started.date():
            raise ValueError("CNINFO distribution observation crossed the exchange day")
        events = _cninfo_events(payload["dividendData"], symbol, arrived, referer)
        by_date = {event["effectiveDate"]: event for event in previous}
        for index, event in enumerate(events):
            original = by_date.get(event["effectiveDate"])
            if original and {**event, "source": original["source"]} == original:
                events[index] = original
        _persist_observation(path, symbol, arrived, events)
        return events
    except (httpx.HTTPError, TimeoutError, OSError, KeyError, TypeError, ValueError) as error:
        logger.warning("TickDesk CNINFO distributions unavailable (%s)", type(error).__name__)
        return None


async def collect_sse_cash_dividends(client, root, symbols):
    return await _collect_distributions(client, root, symbols, _SSE_STOCK, {"沪主板"}, observe_sse_cash_dividends)


async def collect_szse_distributions(client, root, symbols):
    return await _collect_distributions(client, root, symbols, _SZSE_STOCK, {"深主板", "创业板"}, observe_szse_distributions)


async def _collect_distributions(client, root, symbols, stock, boards, observe):
    now = datetime.now(CN)
    day = now.date().isoformat()
    listings = await run_in_threadpool(read_exchange_master, root, timestamp(now))
    wanted = sorted(row["symbol"] for row in listings if row["symbol"] in symbols and row["board"] in boards
                    and row["status"] == "listed" and stock.fullmatch(row["symbol"]))
    pending = [symbol for symbol in wanted if not (_directory(root, symbol) / f"{day}.json").is_file()]
    if pending:
        offset = (int(now.timestamp()) // 300 * _OBSERVATIONS_PER_CYCLE) % len(pending)
        batch = (pending[offset:] + pending[:offset])[:_OBSERVATIONS_PER_CYCLE]
        await asyncio.gather(*(observe(client, root, symbol) for symbol in batch))
    checked = [symbol for symbol in wanted if (_directory(root, symbol) / f"{day}.json").is_file()]
    cutoff = timestamp(datetime.now(CN))
    count = sum(len(_read_distributions(root, symbol, cutoff)) for symbol in checked)
    return {"status": "partial" if checked else "unavailable" if wanted else "out_of_scope",
            "checkedSymbols": len(checked), "archivedEvents": count, "scopeSymbols": len(wanted)}
