"""Observed Shanghai/Shenzhen exchange listings, independent of vendor quote coverage."""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from io import BytesIO

import httpx
import polars as pl

from app.services.fs_utils import atomic_write_parquet
from app.services.tickdesk_adapter import CN, timestamp

SSE_LIST = "https://www.sse.com.cn/assortment/stock/list/share/"
SZSE_LIST = "https://www.szse.cn/market/product/stock/list/"
SSE_DELISTINGS = "https://www.sse.com.cn/assortment/stock/list/delisting/"
SZSE_DELISTINGS = "https://www.szse.cn/market/stock/suspend/index.html"
_SSE_API = "https://query.sse.com.cn/sseQuery/commonQuery.do"
_SZSE_API = "https://www.szse.cn/api/report/ShowReport"
_MASTER_FILE = "exchange-master.parquet"
_DELISTING_DIR = "delisting-observations"
logger = logging.getLogger(__name__)


def _exchange_row(code, name, listing, board, exchange, today):
    if not (isinstance(code, str) and re.fullmatch(r"[0-9]{6}", code)
            and isinstance(name, str) and name.strip() and listing <= today):
        raise ValueError("invalid official listing row")
    source, ref = ("上海证券交易所", SSE_LIST) if exchange == "SH" else ("深圳证券交易所", SZSE_LIST)
    return {"symbol": f"{code}.{exchange}", "name": name.strip(), "exchange": exchange, "board": board,
            "status": "listed", "listing_date": listing, "source": source, "source_ref": ref}


def _shanghai_rows(client, stock_type, company_status, referer, *, max_rows=8000):
    rows = []
    total = None
    sql_id = "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L"
    for page in range(1, 9):
        params = {"sqlId": sql_id, "type": "inParams", "STOCK_TYPE": stock_type, "REG_PROVINCE": "",
                  "CSRC_CODE": "", "STOCK_CODE": "", "COMPANY_STATUS": company_status, "isPagination": "true",
                  "pageHelp.pageSize": 1000, "pageHelp.pageNo": page,
                  "pageHelp.beginPage": page, "pageHelp.endPage": page}
        result = json.loads(_exchange_file(client, _SSE_API, referer, params).read())
        entry = result["pageHelp"]
        if (result.get("sqlId") != sql_id or result.get("actionErrors") or result.get("fieldErrors")
                or type(entry["total"]) is not int or not 0 < entry["total"] <= max_rows
                or (total is not None and total != entry["total"])):
            raise ValueError("Shanghai listing page count changed")
        total = entry["total"]
        batch = entry["data"]
        if not isinstance(batch, list) or not batch:
            raise ValueError("Shanghai listing page missing")
        rows.extend(batch)
        if len(rows) >= total:
            break
    if len(rows) != total:
        raise ValueError("Shanghai listing coverage incomplete")
    return rows


def _shanghai(client, today, board, stock_type, list_board):
    listings = []
    for row in _shanghai_rows(client, stock_type, "2,4,5,7,8", SSE_LIST):
        if row["STOCK_TYPE"] != stock_type or row["LIST_BOARD"] != list_board:
            raise ValueError("Shanghai listing board mismatch")
        listing = date.fromisoformat(row["LIST_DATE"])
        if row["DELIST_DATE"] != "-" and date.fromisoformat(row["DELIST_DATE"]) <= today:
            continue
        listings.append(_exchange_row(row["A_STOCK_CODE"], row["SEC_NAME_CN"], listing, board, "SH", today))
    return listings


def _shenzhen(client, today):
    sheet = _shenzhen_sheet(client, "1110", "tab1", SZSE_LIST, max_rows=8000)
    rows = sheet.select("A股代码", "A股简称", "A股上市日期", "板块").iter_rows()
    boards = {"主板": "深主板", "创业板": "创业板"}
    return [_exchange_row(code, name, date.fromisoformat(listing), boards[board], "SZ", today)
            for code, name, listing, board in rows]


def _shenzhen_sheet(client, catalog, tab, referer, *, max_rows, module=None):
    params = {"SHOWTYPE": "xlsx", "CATALOGID": catalog, "TABKEY": tab}
    if module is not None:
        params["selectModule"] = module
    sheet = pl.read_excel(_exchange_file(client, _SZSE_API, referer, params), infer_schema_length=0)
    if not 0 < sheet.height <= max_rows:
        raise ValueError("Shenzhen listing coverage invalid")
    return sheet


def _exchange_file(client, endpoint, referer, params):
    headers = {"Referer": referer, "User-Agent": "Mozilla/5.0"}
    file = BytesIO()
    with client.stream("GET", endpoint, params=params, headers=headers) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            if file.tell() + len(chunk) > 4 * 1024 * 1024:
                raise ValueError("exchange listing report file size")
            file.write(chunk)
    file.seek(0)
    return file


def _delisting_row(code, name, listing, ended, board, exchange, source_ref, today):
    start, end = date.fromisoformat(listing), date.fromisoformat(ended)
    if not start < end <= today:
        raise ValueError("invalid effective delisting dates")
    row = _exchange_row(code, name, start, board, exchange, today)
    return {**row, "status": "delisted", "delisted_on": end, "source_ref": source_ref}


def _shanghai_delistings(client, today, board, stock_type, list_board):
    delistings = []
    for row in _shanghai_rows(client, stock_type, "3", SSE_DELISTINGS, max_rows=1000):
        if row["STOCK_TYPE"] != stock_type or row["LIST_BOARD"] != list_board or row["STATE_CODE"] != "3":
            raise ValueError("Shanghai delisting board or status mismatch")
        if date.fromisoformat(row["DELIST_DATE"]) <= today:
            delistings.append(_delisting_row(row["A_STOCK_CODE"], row["COMPANY_ABBR"], row["LIST_DATE"], row["DELIST_DATE"],
                                             board, "SH", SSE_DELISTINGS, today))
    return delistings


def _shenzhen_delistings(client, today):
    delistings = []
    boards = (("main", "深主板", r"(?:00|20)[0-9]{4}"), ("nm", "创业板", r"30[0-9]{4}"))
    for module, board, allowed_codes in boards:
        sheet = _shenzhen_sheet(client, "1793_ssgs", "tab2", SZSE_DELISTINGS, max_rows=1000, module=module)
        rows = sheet.select("证券代码", "证券简称", "上市日期", "终止上市日期").iter_rows()
        for code, name, listing, ended in rows:
            if not isinstance(code, str) or not re.fullmatch(allowed_codes, code):
                raise ValueError("invalid Shenzhen delisting board or code")
            if not code.startswith("20") and date.fromisoformat(ended) <= today:
                delistings.append(_delisting_row(code, name, listing, ended, board, "SZ", SZSE_DELISTINGS, today))
    return delistings


def _read_current_listings(root, now):
    path = root / "tickdesk-market" / _MASTER_FILE
    if not path.is_file():
        return []
    return pl.read_parquet(path).filter(
        pl.col("as_of").is_between(now - 3 * 86_400_000, now)).to_dicts()


def read_exchange_delistings(root, now, *, effective_on=None):
    """Return positive delisting evidence covered by both date and observation cutoffs."""
    today = datetime.fromtimestamp(now / 1000, CN).date()
    through = min(effective_on, today) if effective_on is not None else today
    directory = root / "tickdesk-market" / _DELISTING_DIR
    for path in sorted(directory.glob("????-??-??.parquet"), reverse=True):
        if path.stem > str(today):
            continue
        frame = pl.read_parquet(path)
        latest = frame["as_of"].max()
        if latest is not None and latest <= now:
            return frame.filter(pl.col("delisted_on") <= through).to_dicts()
    return []


def refresh_exchange_delistings(root):
    now = datetime.now(CN)
    today = now.date()
    directory = root / "tickdesk-market" / _DELISTING_DIR
    archive = directory / f"{today}.parquet"
    if archive.is_file():
        return pl.read_parquet(archive).height
    with httpx.Client(timeout=8, follow_redirects=False, trust_env=False) as client:
        observed = (_shanghai_delistings(client, today, "沪主板", "1", "1")
                    + _shanghai_delistings(client, today, "科创板", "8", "2")
                    + _shenzhen_delistings(client, today))
    arrival = datetime.now(CN)
    if arrival.date() != today:
        raise ValueError("delisting observation crossed the exchange day")
    available = timestamp(arrival)
    known = {(row["symbol"], row["listing_date"]): row for row in read_exchange_delistings(root, timestamp(now))}
    seen = set()
    for row in observed:
        key = row["symbol"], row["listing_date"]
        if key in seen:
            raise ValueError("duplicate official delisting")
        seen.add(key)
        previous = known.get(key)
        if previous is None or any(previous.get(field) != value for field, value in row.items()):
            known[key] = {**row, "as_of": available}
    if not seen:
        raise ValueError("empty official delisting report")
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(pl.DataFrame(list(known.values())).sort(["symbol", "listing_date"]), archive)
    return len(known)


def read_exchange_catalog(root, now):
    """Resolve current identities against dated prior delistings."""
    try:
        history = read_exchange_delistings(root, now)
    except (OSError, pl.exceptions.PolarsError) as error:
        logger.warning("TickDesk delisting archive unavailable (%s)", type(error).__name__)
        history = []
    identities = {row["symbol"]: row for row in sorted(history, key=lambda row: row["delisted_on"])}
    for listing in _read_current_listings(root, now):
        code = listing["symbol"]
        prior = identities.get(code)
        if prior and listing["listing_date"] <= prior["delisted_on"]:
            observed_day = datetime.fromtimestamp(listing["as_of"] / 1000, CN).date()
            if observed_day >= prior["delisted_on"]:
                identities[code] = {**listing, "status": "unknown"}
        else:
            identities[code] = listing
    return list(identities.values())


def read_exchange_master(root, now):
    """Only exchange-confirmed current listings enter the active stock universe."""
    return [row for row in read_exchange_catalog(root, now) if row["status"] == "listed"]


def refresh_exchange_master(root):
    directory = root / "tickdesk-market"
    now = datetime.now(CN)
    today = now.date()
    archive = directory / "listing-observations" / f"{today.isoformat()}.parquet"
    if archive.is_file():
        frame = pl.read_parquet(archive)
    else:
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            rows = (_shanghai(client, today, "沪主板", "1", "1")
                    + _shanghai(client, today, "科创板", "8", "2")
                    + _shenzhen(client, today))
        if len({row["symbol"] for row in rows}) != len(rows):
            raise ValueError("duplicate official listing")
        observed_at = timestamp(datetime.now(CN))
        frame = pl.DataFrame(rows).with_columns(pl.lit(observed_at, dtype=pl.Int64).alias("as_of"))
        archive.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_parquet(frame, archive)
    path = directory / _MASTER_FILE
    if not path.is_file() or path.stat().st_mtime < archive.stat().st_mtime:
        atomic_write_parquet(frame, path)
    try:
        refresh_exchange_delistings(root)
    except (httpx.HTTPError, KeyError, OSError, TypeError, ValueError, pl.exceptions.PolarsError) as error:
        logger.warning("TickDesk delisting refresh failed (%s)", type(error).__name__)
    return frame.height
