"""Independent TickDesk collection and point-in-time external evidence archive."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import sqlite3
import stat
import time
import unicodedata
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from app.market_time import CN_TZ, in_continuous_session
from app.services.trading_day import cached_verdict

MINUTE = 60_000
DAY = 24 * 60 * MINUTE
RISK_INTERVAL = 5 * MINUTE
RESULT_LIMIT = 5
RESPONSE_LIMIT = 512 * 1024
BODY_LIMIT = 24_000
CONTEXT_LIMIT = 12
CONTEXT_BYTES = 32 * 1024
SCOPE_LIMIT = 64
MARKET = "market:CN"
SYMBOL = re.compile(r"(?<![A-Z0-9])([0-9]{6}\.(?:SH|SZ|BJ))(?![A-Z0-9])", re.I)
AUTHORITY_HOSTS = {
    "www.sse.com.cn",
    "www.szse.cn",
    "www.bse.cn",
    "www.csrc.gov.cn",
    "static.cninfo.com.cn",
    "www.stats.gov.cn",
    "www.pbc.gov.cn",
    "www.gov.cn",
    "www.mof.gov.cn",
    "www.ndrc.gov.cn",
}
# DeepSeek provides the Fetch redirect policy. Submit only exact official HTTPS
# origins with self-contained article paths and no query, encoded path or redirect routes.
FETCH_PATHS = {
    "www.sse.com.cn": re.compile(r"/disclosure/listedinfo/announcement/c/[A-Za-z0-9_-]+\.shtml"),
    "www.szse.cn": re.compile(r"/disclosure/listed/notice/detail/[A-Za-z0-9_-]+\.html"),
    "www.csrc.gov.cn": re.compile(r"/csrc/c[0-9]+/c[0-9]+/content\.shtml"),
    "www.gov.cn": re.compile(r"/zhengce/[0-9]{4}[0-9]{2}/content_[0-9]+\.htm"),
    "www.stats.gov.cn": re.compile(r"/sj/zxfb/[0-9]{6}/t[0-9]{8}_[0-9]+\.html"),
}


class FactLookupError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class Provider(Protocol):
    async def search(self, query: str) -> list[dict]: ...

    async def fetch(self, url: str) -> str: ...


def _public_url(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or len(value) > 2048
        or re.search(r"[\x00-\x20\x7f\\<>\ud800-\udfff]", value)
    ):
        return None
    try:
        parts = urlsplit(value)
        host = parts.hostname
        if parts.scheme not in {"http", "https"} or not host or parts.username is not None:
            return None
        default_port = 443 if parts.scheme == "https" else 80
        if parts.port not in {None, default_port}:
            return None
        host = host.lower().rstrip(".")
        try:
            # All literal addresses are excluded, even where globally routable.
            ipaddress.ip_address(host)
            return None
        except ValueError:
            pass
        if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}", host):
            return None
        if host.endswith((".localhost", ".local", ".internal", ".test", ".invalid", ".onion")):
            return None
        query = urlencode(
            [
                (key, val)
                for key, val in parse_qsl(parts.query, max_num_fields=40, keep_blank_values=True)
                if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}
            ]
        )
        return urlunsplit((parts.scheme, host, parts.path or "/", query, ""))
    except ValueError:
        return None


def _fetchable(value: str) -> bool:
    if _public_url(value) is None:
        return False
    parts = urlsplit(value)
    route = FETCH_PATHS.get(parts.netloc)
    return (
        parts.scheme == "https"
        and not parts.query
        and not parts.fragment
        and route is not None
        and route.fullmatch(parts.path) is not None
    )


class DeepSeekFacts:
    """HTTP transport for the official, standalone Search and Fetch APIs."""

    def __init__(self, client: httpx.AsyncClient, token: str):
        self._client = client
        self._token = token

    async def _post(self, tool: str, payload: dict) -> dict:
        try:
            async with (
                asyncio.timeout(8),
                self._client.stream(
                    "POST",
                    f"https://api.deepseek.com/v1/tools/{tool}",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Accept-Encoding": "identity",
                    },
                    follow_redirects=False,
                ) as response,
            ):
                code = response.status_code
                if code != 200:
                    reasons = {
                        401: "authentication",
                        402: "provider_balance",
                        403: "provider_policy",
                        404: "body_unavailable",
                        408: "timeout",
                        429: "rate_limited",
                        504: "timeout",
                    }
                    if 300 <= code < 400:
                        raise FactLookupError("unsafe_redirect")
                    raise FactLookupError(
                        reasons.get(
                            code, "provider_unavailable" if code >= 500 else "provider_error"
                        )
                    )
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise FactLookupError("invalid_response")
                raw = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=16 * 1024):
                    if len(raw) + len(chunk) > RESPONSE_LIMIT:
                        raise FactLookupError("response_limit")
                    raw.extend(chunk)
                body = json.loads(raw.decode("utf-8"))
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise FactLookupError("timeout") from exc
        except httpx.HTTPError as exc:
            raise FactLookupError("provider_unavailable") from exc
        except (ValueError, RecursionError) as exc:
            raise FactLookupError("invalid_response") from exc
        if not isinstance(body, dict):
            raise FactLookupError("invalid_response")
        return body

    async def search(self, query: str) -> list[dict]:
        results = (
            await self._post(
                "search",
                {
                    "query": query,
                    "count": RESULT_LIMIT,
                    "need_content": True,
                },
            )
        ).get("results")
        if not isinstance(results, list) or len(results) > RESULT_LIMIT:
            raise FactLookupError("invalid_response")
        return results

    async def fetch(self, url: str) -> str:
        if not _fetchable(url):
            raise FactLookupError("unsafe_url")
        page = await self._post("fetch", {"url": url})
        # If the provider begins returning redirect metadata, validate it before use.
        for field in ("url", "final_url", "finalUrl", "redirected_url"):
            if field in page and page[field] != url:
                raise FactLookupError("unsafe_redirect")
        content = page.get("content")
        if not isinstance(content, str):
            raise FactLookupError("invalid_response")
        return content


@dataclass(frozen=True)
class FactLimits:
    daily: Decimal = Decimal("0")
    search_per_1000: Decimal = Decimal("0")
    fetch_per_1000: Decimal = Decimal("0")
    search_per_minute: int = 90
    fetch_per_minute: int = 30
    search_per_day: int = 21_000
    fetch_per_day: int = 1_000
    currency: str = "USD"
    search_daily: Decimal | None = None
    fetch_daily: Decimal | None = None


@dataclass(frozen=True)
class Plan:
    name: str
    terms: str
    slots: tuple[tuple[int, int], ...] = ()
    trading_only: bool = False
    weekly: bool = False
    closed_only: bool = False


RISK = Plan("companyRisk", "公告 重大风险 监管 停复牌 撤回 更正")
FINANCE = Plan(
    "companyFinance", "财报 经营 现金流 审计 业绩预期 估值", ((16, 30),), trading_only=True
)
PROFILE = Plan(
    "companyProfile", "主营 产品 竞争 治理 客户 融资 公司基本资料", ((16, 30),), weekly=True
)
HOURLY = ((9, 30), (10, 30), (11, 30), (13, 0), (14, 0), (15, 0))
SHARED = (
    Plan("industry", "A股 行业供需 产业链 原料 产业政策", HOURLY, trading_only=True),
    Plan("marketNews", "A股 普通新闻 市场风格 资金 流动性 舆情", HOURLY, trading_only=True),
    Plan(
        "macroPolicy",
        "中国 A股 宏观 利率 汇率 财政 货币 政策 国际",
        ((8, 45), (12, 0), (16, 30), (21, 30)),
        trading_only=True,
    ),
    Plan("calendarRules", "A股 交易所 事件日历 规则 变更 官网", ((16, 30),)),
    Plan("closedRecap", "A股 休市汇总 重要事件 公告 监管", ((21, 30),), closed_only=True),
)
COMPANY = (RISK, FINANCE, PROFILE)
PLAN_BY_NAME = {plan.name: plan for plan in (*COMPANY, *SHARED)}


def _clock_ms() -> int:
    return time.time_ns() // 1_000_000


def _as_ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _wall_time(stamp: int) -> datetime:
    return datetime.fromtimestamp(stamp / 1000, CN_TZ)


def _trades(day: datetime, verdict: Callable[[datetime], bool | None]) -> bool:
    trading = verdict(day)
    return day.weekday() < 5 if trading is None else trading


def _fixed_due(plan: Plan, stamp: int, verdict, *, initial: bool = False) -> int:
    if plan is RISK:
        return stamp if initial else stamp + RISK_INTERVAL
    now = _wall_time(stamp)
    for offset in range(14):
        day = now + timedelta(days=offset)
        if (
            (plan.weekly and day.weekday() != 4)
            or (plan.trading_only and not _trades(day, verdict))
            or (plan.closed_only and _trades(day, verdict))
        ):
            continue
        slots = [
            day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            for hour, minute in plan.slots
        ]
        if initial and offset == 0:
            elapsed = [item for item in slots if item <= now]
            if elapsed and (plan.slots != HOURLY or in_continuous_session(now)):
                return _as_ms(elapsed[-1])
        for item in slots:
            if item > now:
                return _as_ms(item)
    return stamp + DAY


def _clean(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f\ud800-\udfff]", "", value[:limit]).strip()


def _canonical_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _digest(*values: str) -> str:
    return sha256("\x00".join(values).encode("utf-8")).hexdigest()


def _publish_time(value: object) -> int | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            return _as_ms(parsed)
    except (ValueError, OverflowError, OSError):
        pass
    return None


_SCHEMA = """
    CREATE TABLE IF NOT EXISTS fact_jobs (
        entity TEXT NOT NULL, category TEXT NOT NULL, first_due INTEGER NOT NULL,
        next_due INTEGER NOT NULL, lease_until INTEGER NOT NULL DEFAULT 0,
        attempt INTEGER, last_success INTEGER, PRIMARY KEY(entity, category)
    );
    CREATE TABLE IF NOT EXISTS fact_attempts (
        id INTEGER PRIMARY KEY, entity TEXT NOT NULL, category TEXT NOT NULL,
        due INTEGER NOT NULL, started INTEGER NOT NULL, finished INTEGER,
        last_success INTEGER, next_due INTEGER, window_start INTEGER, window_end INTEGER,
        reason TEXT, detail TEXT, count INTEGER, gap INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS fact_attempts_cutoff
        ON fact_attempts(entity, category, started DESC, id DESC);
    CREATE TABLE IF NOT EXISTS fact_versions (
        fact_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, body_hash TEXT NOT NULL,
        observed INTEGER NOT NULL, available INTEGER NOT NULL, title_key TEXT NOT NULL,
        authority INTEGER NOT NULL, content TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS fact_document ON fact_versions(document_id, observed DESC);
    CREATE INDEX IF NOT EXISTS fact_hash ON fact_versions(body_hash, observed DESC);
    CREATE TABLE IF NOT EXISTS fact_urls (
        url TEXT NOT NULL, fact_id TEXT NOT NULL, observed INTEGER NOT NULL, raw_url TEXT NOT NULL,
        PRIMARY KEY(url, fact_id)
    );
    CREATE INDEX IF NOT EXISTS fact_urls_version ON fact_urls(fact_id, observed);
    CREATE TABLE IF NOT EXISTS fact_entities (
        entity TEXT NOT NULL, category TEXT NOT NULL, fact_id TEXT NOT NULL,
        observed INTEGER NOT NULL, PRIMARY KEY(entity, category, fact_id)
    );
    CREATE INDEX IF NOT EXISTS fact_entities_cutoff ON fact_entities(entity, observed DESC);
    CREATE TABLE IF NOT EXISTS fact_requests (
        id INTEGER PRIMARY KEY, time INTEGER NOT NULL, day TEXT NOT NULL,
        tool TEXT NOT NULL, price INTEGER NOT NULL, outcome TEXT, completed INTEGER,
        billed INTEGER, argument_bytes INTEGER, returned_bytes INTEGER
    );
    CREATE INDEX IF NOT EXISTS fact_requests_time ON fact_requests(tool, time DESC);
    CREATE INDEX IF NOT EXISTS fact_requests_day ON fact_requests(day, tool);
    CREATE TABLE IF NOT EXISTS fact_daily_budget (
        day TEXT PRIMARY KEY, search_attempts INTEGER NOT NULL DEFAULT 0,
        fetch_attempts INTEGER NOT NULL DEFAULT 0, reserved_cost INTEGER NOT NULL DEFAULT 0,
        currency TEXT NOT NULL DEFAULT 'USD'
    );
    CREATE TABLE IF NOT EXISTS fact_service (
        id INTEGER PRIMARY KEY CHECK(id = 1), status TEXT NOT NULL,
        scope_size INTEGER NOT NULL, scope_truncated INTEGER NOT NULL, calendar TEXT NOT NULL
    );
"""


@dataclass(frozen=True)
class Lease:
    id: int
    entity: str
    plan: Plan
    due: int
    started: int
    last_success: int | None


class FactArchive:
    """SQLite archive with independent leases, evidence versions and provider cost reservations."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            request_columns = {row["name"] for row in db.execute("PRAGMA table_info(fact_requests)")}
            if "argument_bytes" not in request_columns:
                db.execute("ALTER TABLE fact_requests ADD COLUMN argument_bytes INTEGER")
            if "returned_bytes" not in request_columns:
                db.execute("ALTER TABLE fact_requests ADD COLUMN returned_bytes INTEGER")
            budget_columns = {row["name"] for row in db.execute("PRAGMA table_info(fact_daily_budget)")}
            if "currency" not in budget_columns:
                db.execute("ALTER TABLE fact_daily_budget ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD'")
        # A previous process's status is no evidence that this process enabled collection.
        self.configuration = "disabled"

    @contextmanager
    def _db(self):
        with closing(sqlite3.connect(self.path, timeout=2)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def record_scope(self, status: str, size: int, truncated: bool, calendar: str) -> None:
        with self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO fact_service VALUES (1, ?, ?, ?, ?)",
                (status, size, truncated, calendar),
            )

    def register(self, jobs: list[tuple[Plan, str]], now: int, verdict) -> None:
        due_jobs = [
            (entity, plan.name, _fixed_due(plan, now, verdict, initial=True))
            for plan, entity in jobs
        ]
        with self._db() as db:
            db.executemany(
                "INSERT OR IGNORE INTO fact_jobs(entity,category,first_due,next_due) VALUES (?,?,?,?)",
                [(entity, category, due, due) for entity, category, due in due_jobs],
            )

    def claim(self, jobs: list[tuple[Plan, str]], now: int, limit: int) -> list[Lease]:
        active = {(plan.name, entity): index for index, (plan, entity) in enumerate(jobs)}
        if not active:
            return []
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """SELECT * FROM fact_jobs WHERE next_due <= ? AND lease_until <= ?
                   AND entity IN (SELECT value FROM json_each(?))""",
                (now, now, json.dumps(list(dict.fromkeys(entity for _, entity in active)))),
            ).fetchall()
            rows = [row for row in rows if (row["category"], row["entity"]) in active]
            rows.sort(key=lambda row: (row["next_due"], active[row["category"], row["entity"]]))
            leases = []
            for row in rows[:limit]:
                entity, category, due = row["entity"], row["category"], row["next_due"]
                if row["attempt"] is not None:
                    db.execute(
                        """UPDATE fact_attempts SET finished=?,reason='interrupted',next_due=?
                           WHERE id=? AND finished IS NULL""",
                        (now, due, row["attempt"]),
                    )
                result = db.execute(
                    """INSERT INTO fact_attempts(entity,category,due,started,last_success)
                       VALUES (?,?,?,?,?)""",
                    (entity, category, due, now, row["last_success"]),
                )
                db.execute(
                    "UPDATE fact_jobs SET attempt=?,lease_until=? WHERE entity=? AND category=?",
                    (result.lastrowid, now + MINUTE, entity, category),
                )
                leases.append(
                    Lease(
                        result.lastrowid,
                        entity,
                        PLAN_BY_NAME[category],
                        due,
                        now,
                        row["last_success"],
                    )
                )
            return leases

    def reserve(self, tool: str, now: int, limits: FactLimits, *, argument_bytes: int | None = None) -> int:
        price = getattr(limits, f"{tool}_per_1000") / 1000
        price_units = int((price * 10**9).to_integral_value(rounding="ROUND_CEILING"))
        daily_units = int(limits.daily * 10**9)
        tool_limit = getattr(limits, f"{tool}_daily")
        day = _wall_time(now).date().isoformat()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            recent = db.execute(
                """SELECT outcome, time FROM fact_requests WHERE tool=? AND time>?
                   ORDER BY id DESC LIMIT ?""",
                (tool, now - MINUTE, getattr(limits, f"{tool}_per_minute")),
            ).fetchall()
            if len(recent) >= getattr(limits, f"{tool}_per_minute"):
                raise FactLookupError("rate_budget")
            blocked = {
                "timeout",
                "provider_unavailable",
                "provider_balance",
                "authentication",
                "rate_limited",
            }
            if len(recent) >= 3 and all(row["outcome"] in blocked for row in recent[:3]):
                raise FactLookupError("circuit_open")
            db.execute("INSERT OR IGNORE INTO fact_daily_budget (day,currency) VALUES (?,?)", (day, limits.currency))
            totals = db.execute("SELECT * FROM fact_daily_budget WHERE day=?", (day,)).fetchone()
            if totals["currency"] != limits.currency:
                raise FactLookupError("budget_currency_changed")
            count, spend = totals[f"{tool}_attempts"], totals["reserved_cost"]
            if count >= getattr(limits, f"{tool}_per_day") or spend + price_units > daily_units:
                raise FactLookupError("budget_exhausted")
            if tool_limit is not None:
                spend = db.execute("""SELECT coalesce(sum(price),0) FROM fact_requests
                    WHERE day=? AND tool=? AND billed IS NOT 0""", (day, tool)).fetchone()[0]
                if spend + price_units > int(tool_limit * 10**9):
                    raise FactLookupError("budget_exhausted")
            db.execute(
                """UPDATE fact_daily_budget SET search_attempts=search_attempts+?,
                   fetch_attempts=fetch_attempts+?, reserved_cost=reserved_cost+? WHERE day=?""",
                (tool == "search", tool == "fetch", price_units, day),
            )
            return db.execute(
                "INSERT INTO fact_requests(time,day,tool,price,argument_bytes) VALUES (?,?,?,?,?)",
                (now, day, tool, price_units, argument_bytes),
            ).lastrowid

    def request_outcome(self, request: int, outcome: str, now: int, billed: bool | None,
                        *, returned_bytes: int | None = None) -> None:
        with self._db() as db:
            row = db.execute(
                """UPDATE fact_requests SET outcome=?,completed=?,billed=?,returned_bytes=?
                   WHERE id=? AND completed IS NULL RETURNING day,price""",
                (outcome, now, billed, returned_bytes, request),
            ).fetchone()
            if billed is False and row is not None:
                db.execute(
                    "UPDATE fact_daily_budget SET reserved_cost=reserved_cost-? WHERE day=?",
                    (row["price"], row["day"]),
                )

    def budget_diagnostics(self, *, now: int) -> dict:
        try:
            limits = _configured_limits()
            status = "configured" if limits is not None else "unconfigured"
        except (ValueError, InvalidOperation, KeyError):
            limits = None
            status = "invalid_configuration"
        with self._db() as db:
            db.execute("BEGIN")
            booked = db.execute("SELECT currency FROM fact_daily_budget WHERE day=?",
                                (_wall_time(now).date().isoformat(),)).fetchone()
            if limits and booked and booked["currency"] != limits.currency:
                status = "currency_change_pending"
            usage = self._metrics(db, now)
        return {"configuration": status,
                "currency": limits.currency if limits else None,
                "combinedDaily": str(limits.daily) if limits else None,
                "toolDaily": {tool: str(cap) if cap is not None else None for tool, cap in (
                    ("search", limits.search_daily if limits else None), ("fetch", limits.fetch_daily if limits else None),
                )},
                "dailyUsage": usage}

    def finish(
        self,
        lease: Lease,
        *,
        now: int,
        next_due: int,
        window_start: int,
        reason: str,
        details: list[str],
        gap: bool,
        results: list[dict],
        searched: bool,
    ) -> None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT attempt FROM fact_jobs WHERE entity=? AND category=?",
                (lease.entity, lease.plan.name),
            ).fetchone()
            if row is None or row["attempt"] != lease.id:
                return
            for result in results:
                self._save_fact(db, lease, result, now)
            last_success = now if searched else lease.last_success
            db.execute(
                """UPDATE fact_attempts SET finished=?,last_success=?,next_due=?,window_start=?,
                   window_end=?,reason=?,detail=?,count=?,gap=? WHERE id=?""",
                (
                    now,
                    last_success,
                    next_due,
                    window_start,
                    lease.started,
                    reason,
                    json.dumps(details),
                    len(results),
                    gap,
                    lease.id,
                ),
            )
            db.execute(
                """UPDATE fact_jobs SET next_due=?,lease_until=0,attempt=NULL,last_success=?
                   WHERE entity=? AND category=?""",
                (next_due, last_success, lease.entity, lease.plan.name),
            )

    @staticmethod
    def _save_fact(db, lease: Lease, result: dict, now: int) -> None:
        url = result["canonicalUrl"]
        prior = db.execute(
            """SELECT version.* FROM fact_urls AS alias JOIN fact_versions AS version USING(fact_id)
               WHERE alias.url=? ORDER BY version.observed DESC, version.rowid DESC LIMIT 1""",
            (url,),
        ).fetchone()
        body_hash = result.pop("observedHash")
        primary_url = url
        if prior is not None:
            previous = json.loads(prior["content"])
            primary_url = previous["canonicalUrl"]
            if not result["body"] and previous["body"] and result["title"] == previous["title"]:
                # An omitted body is a coverage failure, not a newer version of the page.
                body_hash = prior["body_hash"]
        match = prior if prior is not None and prior["body_hash"] == body_hash else None
        if prior is None and result["body"]:
            match = db.execute(
                """SELECT * FROM fact_versions WHERE body_hash=?
                   AND NOT EXISTS (SELECT 1 FROM fact_versions later WHERE later.document_id=fact_versions.document_id
                       AND later.rowid>fact_versions.rowid)
                   ORDER BY authority DESC, observed LIMIT 1""",
                (body_hash,),
            ).fetchone()
        if match is None:
            is_revision = prior is not None and primary_url == url
            document = prior["document_id"] if is_revision else "doc-" + _digest(url)
            prior_id = prior["fact_id"] if is_revision else None
            revision = _digest(document, body_hash, prior_id or "")
            fact_id = "fact-" + revision
            published = result["publishedAt"]
            fetched = result["fetchedAt"]
            available = max(now, published) if published is not None else now
            withdrawal = (
                prior_id
                if result["sourceType"] == "official" and "撤回" in result["title"]
                else None
            )
            result.update(
                {
                    "factId": fact_id,
                    "documentId": document,
                    "revisionId": revision,
                    "bodySha256": sha256(result["body"].encode()).hexdigest()
                    if result["body"]
                    else None,
                    "firstObservedAt": now,
                    "availableAt": available,
                    "fetchedAt": fetched,
                    "supersedesFactId": prior_id,
                    "withdrawsFactId": withdrawal,
                    "reprintsFactId": prior["fact_id"]
                    if prior is not None and not is_revision
                    else None,
                    "extractedFields": [],
                    "extractionMethod": "deepseek_search_fetch_raw",
                    "verification": "unverified_source_claim",
                }
            )
            db.execute(
                "INSERT INTO fact_versions VALUES (?,?,?,?,?,?,?,?)",
                (
                    fact_id,
                    document,
                    body_hash,
                    now,
                    available,
                    _canonical_text(result["title"]),
                    result["sourceType"] == "official",
                    json.dumps(result, ensure_ascii=False),
                ),
            )
        else:
            fact_id = match["fact_id"]
        db.execute(
            "INSERT OR IGNORE INTO fact_urls VALUES (?,?,?,?)", (url, fact_id, now, result["url"])
        )
        db.execute(
            "INSERT OR IGNORE INTO fact_entities VALUES (?,?,?,?)",
            (lease.entity, lease.plan.name, fact_id, now),
        )

    def context(
        self, cutoff: int, text: str, local_items: Sequence[dict]
    ) -> tuple[list[dict], dict]:
        """Return archived evidence knowable at the Unix millisecond cutoff and coverage by plan."""
        local = {_symbol(item.get("record", {}).get("symbol")) for item in local_items}
        explicit = {_symbol(match[0]) for match in SYMBOL.finditer(text)}
        named = {item for item in local if item and re.search(rf"(?<!\d){item[:6]}(?!\d)", text)}
        entities = sorted((explicit or named or local) - {None})
        with self._db() as db:
            db.execute("BEGIN")
            facts, bounded = self._facts(db, cutoff, [*entities, MARKET])
            service = db.execute("SELECT * FROM fact_service WHERE id=1").fetchone()
            plans = {
                entity: {
                    plan.name: self._coverage(db, cutoff, entity, plan)
                    for plan in (SHARED if entity == MARKET else COMPANY)
                }
                for entity in [*entities, MARKET]
            }
            metrics = self._metrics(db, cutoff)
        return facts, {
            "asOf": cutoff,
            "status": self.configuration,
            "scopeStatus": service["status"]
            if service and self.configuration == "active"
            else self.configuration,
            "scopeTruncated": bool(service and service["scope_truncated"]),
            "scopeSize": service["scope_size"] if service else None,
            "tradingCalendar": service["calendar"] if service else "unavailable",
            "truncated": bounded,
            "plans": plans,
            "usage": metrics,
            "limitations": [
                "search_is_not_exhaustive",
                "no_verified_external_numeric_extraction",
                "company_match_requires_security_code",
                "industry_scope_is_market_wide",
            ],
        }

    @staticmethod
    def _metrics(db, cutoff: int) -> dict:
        rows = db.execute(
            """SELECT r.tool,coalesce(b.currency,'USD') AS currency,
                      count(*) AS attempts,sum(r.completed<=? AND r.billed=1) AS billed,
                      sum(CASE WHEN r.completed<=? AND r.billed=0 THEN 0 ELSE r.price END) AS spent,
                      sum(r.argument_bytes) AS arguments,
                      count(r.argument_bytes) AS measured_arguments,
                      sum(CASE WHEN r.completed<=? THEN r.returned_bytes END) AS returned,
                      count(CASE WHEN r.completed<=? THEN r.returned_bytes END) AS measured_returns
               FROM fact_requests r LEFT JOIN fact_daily_budget b ON b.day=r.day
               WHERE r.day=? AND r.time<=? GROUP BY r.tool,b.currency""",
            (cutoff, cutoff, cutoff, cutoff, _wall_time(cutoff).date().isoformat(), cutoff),
        )
        return {
            row["tool"]: {
                "attempted": row["attempts"],
                "billable": row["billed"] or 0,
                "currency": row["currency"],
                "reservedCost": str(Decimal(row["spent"]) / 10**9),
                "argumentBytes": row["arguments"],
                "attemptsWithArgumentBytes": row["measured_arguments"],
                "returnedTextBytes": row["returned"],
                "attemptsWithReturnedTextBytes": row["measured_returns"],
                **({"reservedUsd": str(Decimal(row["spent"]) / 10**9)} if row["currency"] == "USD" else {}),
            }
            for row in rows
        }

    @staticmethod
    def _coverage(db, cutoff: int, entity: str, plan: Plan) -> dict:
        row = db.execute(
            """SELECT * FROM fact_attempts WHERE entity=? AND category=? AND started<=?
               ORDER BY started DESC, id DESC LIMIT 1""",
            (entity, plan.name, cutoff),
        ).fetchone()
        job = db.execute(
            "SELECT first_due FROM fact_jobs WHERE entity=? AND category=?", (entity, plan.name)
        ).fetchone()
        if row is None:
            due = job["first_due"] if job else None
            return {
                "status": "unavailable",
                "reason": "overdue" if due is not None and cutoff >= due else "no_archived_attempt",
                "dueAt": due,
                "nextDueAt": due,
                "lastAttemptAt": None,
                "lastSuccessAt": None,
            }
        complete = row["finished"] is not None and row["finished"] <= cutoff
        reason = row["reason"] if complete else "in_progress"
        # A search is an explicitly partial inventory, even if all returned bodies were saved.
        status = (
            "partial"
            if reason
            in {
                "observed",
                "no_results",
                "missing_body",
                "unresolved_entity",
                "filtered_results",
                "body_limit",
            }
            else "unavailable"
        )
        lagged = complete and row["next_due"] is not None and cutoff > row["next_due"] + MINUTE
        last_success = row["last_success"]
        if last_success is not None and last_success > cutoff:
            prior = db.execute(
                """SELECT MAX(last_success) FROM fact_attempts WHERE entity=? AND category=?
                   AND finished<=? AND last_success<=?""",
                (entity, plan.name, cutoff, cutoff),
            ).fetchone()
            last_success = prior[0]
        if lagged or (status == "unavailable" and last_success is not None):
            status = "stale"
        return {
            "status": status,
            "reason": "overdue" if lagged else reason,
            "attemptReason": reason,
            "details": json.loads(row["detail"] or "[]") if complete else [],
            "lastAttemptAt": row["started"],
            "lastSuccessAt": last_success,
            "dueAt": row["due"],
            "nextDueAt": row["next_due"] if complete else None,
            "windowStart": row["window_start"] if complete else None,
            "windowEnd": row["window_end"] if complete else None,
            "gap": bool(complete and row["gap"]),
            "count": row["count"] if complete else None,
        }

    @staticmethod
    def _facts(db, cutoff: int, entities: list[str]) -> tuple[list[dict], bool]:
        rows = db.execute(
            """SELECT version.*, mapping.entity, mapping.category FROM fact_entities AS mapping
               JOIN fact_versions AS version USING(fact_id)
               WHERE mapping.entity IN (SELECT value FROM json_each(?)) AND mapping.observed<=?
               AND version.available<=? AND mapping.observed>=?
               AND NOT EXISTS (
                   SELECT 1 FROM fact_versions later WHERE later.document_id=version.document_id
                   AND later.available<=? AND later.rowid>version.rowid
               )
               ORDER BY mapping.entity=? ASC, mapping.category='companyRisk' DESC,
                        version.authority DESC, version.observed DESC, version.fact_id
               LIMIT ?""",
            (
                json.dumps(entities),
                cutoff,
                cutoff,
                cutoff - 45 * DAY,
                cutoff,
                MARKET,
                (CONTEXT_LIMIT + 1) * len(PLAN_BY_NAME),
            ),
        ).fetchall()
        facts = {}
        for row in rows:
            fact_id = row["fact_id"]
            if fact_id not in facts:
                fact = json.loads(row["content"])
                fact.pop("body")
                canonical = fact["canonicalUrl"]
                fact["citationRef"] = (
                    canonical if len(canonical) <= 512 else f"local:tickdesk-fact:{fact_id}"
                )
                fact["entity"] = row["entity"]
                fact["entities"] = []
                fact["categories"] = []
                aliases = db.execute(
                    """SELECT url FROM fact_urls WHERE fact_id=? AND observed<=? AND url<>?
                       AND length(url)<=512 ORDER BY url LIMIT 3""",
                    (fact_id, cutoff, fact["canonicalUrl"]),
                )
                fact["relatedSources"] = [alias["url"] for alias in aliases]
                facts[fact_id] = fact
            fact = facts[fact_id]
            if row["entity"] not in fact["entities"]:
                fact["entities"].append(row["entity"])
            if row["category"] not in fact["categories"]:
                fact["categories"].append(row["category"])
        selected = []
        budget = CONTEXT_BYTES - 4096
        for fact in facts.values():
            size = len(json.dumps(fact, ensure_ascii=False).encode("utf-8"))
            if size > budget or len(selected) == CONTEXT_LIMIT:
                break
            budget -= size
            selected.append(fact)
        titles = Counter(fact["title"] for fact in selected)
        for fact in selected:
            fact["potentialConflicts"] = (
                [
                    other["factId"]
                    for other in selected
                    if other["title"] == fact["title"]
                    and other["factId"] != fact["factId"]
                    and set(other["entities"]) & set(fact["entities"])
                ][:3]
                if titles[fact["title"]] > 1
                else []
            )
        return selected, len(facts) > len(selected)


def _symbol(value: object) -> str | None:
    return value.upper() if isinstance(value, str) and SYMBOL.fullmatch(value) else None


def _associated(entity: str, title: str, body: str) -> bool:
    if entity == MARKET:
        return True
    explicit = {match[0].upper() for match in SYMBOL.finditer(title + "\n" + body)}
    if explicit:
        return entity in explicit
    code = entity[:6]
    labelled = re.findall(r"(?:证券|股票)代码\s*[：:]?\s*([0-9]{6})(?![0-9])", body)
    return code in labelled if labelled else bool(re.search(rf"(?<![0-9]){code}(?![0-9])", title))


def _build_result(
    raw: dict, *, body: str, fetched: int | None, entity: str, canonical: str
) -> dict | None:
    title = _clean(raw.get("title"), 200)
    if not title or not _associated(entity, title, body):
        return None
    limited = len(body) > BODY_LIMIT
    snippet = _clean(raw.get("snippet"), 320)
    observed_hash = _digest(_canonical_text(title), _canonical_text(body or snippet))
    body = body[:BODY_LIMIT]
    excerpt = body[:600] or snippet
    source = urlsplit(canonical).hostname
    return {
        "url": raw["url"],
        "canonicalUrl": canonical,
        "title": title,
        "publisher": _clean(raw.get("site_name"), 120) or source,
        "publishedAt": _publish_time(raw.get("date")),
        "fetchedAt": fetched,
        "sourceType": "official" if source in AUTHORITY_HOSTS else "web",
        "kind": "third_party_opinion" if "研报" in title or "观点" in title else "source_claim",
        "excerpt": excerpt,
        "excerptKind": "page_content" if body else "search_snippet",
        "body": body,
        "bodyTruncated": limited,
        "citationAvailable": bool(body),
        "observedHash": observed_hash,
    }


class FactCollector:
    def __init__(
        self,
        archive: FactArchive,
        provider: Provider,
        *,
        clock=_clock_ms,
        verdict=cached_verdict,
        limits: FactLimits | None = None,
    ):
        self.archive = archive
        self.provider = provider
        self.clock = clock
        self.verdict = verdict
        self.limits = limits or FactLimits()
        self._registered: set[tuple[Plan, str]] = set()
        self._lock = asyncio.Lock()
        self._last_scope = None

    async def tick(self, symbols: Sequence[str] | dict, *, scope_status: str = "active") -> None:
        async with self._lock:
            raw = symbols.get("symbols", ()) if isinstance(symbols, dict) else symbols
            valid = list(dict.fromkeys(symbol for value in raw if (symbol := _symbol(value))))
            truncated = len(valid) > SCOPE_LIMIT or bool(
                isinstance(symbols, dict) and symbols.get("truncated")
            )
            selected = valid[:SCOPE_LIMIT]
            now = self.clock()
            self.archive.configuration = "active"
            calendar = (
                "weekday_approximate"
                if self.verdict(_wall_time(now)) is None
                else "existing_calendar"
            )
            scope = (scope_status, len(selected), truncated, calendar)
            if scope != self._last_scope:
                self.archive.record_scope(*scope)
                self._last_scope = scope
            risk = [(RISK, symbol) for symbol in selected]
            other = [(plan, MARKET) for plan in SHARED]
            other.extend((plan, symbol) for symbol in selected for plan in (FINANCE, PROFILE))
            needed = [item for item in risk + other if item not in self._registered]
            if needed:
                self.archive.register(needed, now, self.verdict)
            self._registered = set(risk + other)
            # One reserved lane for lower-frequency work prevents it from falling
            # behind when many portfolio symbols are due simultaneously.
            urgent = self.archive.claim(risk, now, 7)
            background = self.archive.claim(other, now, 8 - len(urgent))
            pending = urgent + background
            if pending:
                await asyncio.gather(*(self._run(lease) for lease in pending))

    async def _provider_call(self, tool: str, value: str):
        for index in range(2):
            request = self.archive.reserve(tool, self.clock(), self.limits, argument_bytes=_text_bytes(value))
            try:
                result = await getattr(self.provider, tool)(value)
            except FactLookupError as exc:
                uncharged = exc.code in {
                    "authentication",
                    "provider_balance",
                    "provider_policy",
                    "body_unavailable",
                    "rate_limited",
                    "provider_error",
                }
                self.archive.request_outcome(
                    request, exc.code, self.clock(), False if uncharged else None
                )
                if index or exc.code not in {"timeout", "provider_unavailable"}:
                    raise
                await asyncio.sleep(0.2)
            else:
                received = _text_bytes(result) if tool == "fetch" else sum(
                    _text_bytes(row.get(field)) for row in result if isinstance(row, dict)
                    for field in ("title", "snippet", "summary", "content")
                )
                self.archive.request_outcome(request, "ok", self.clock(), bool(result), returned_bytes=received)
                return result

    async def _run(self, lease: Lease) -> None:
        clock, plan = self.clock, lease.plan
        start = lease.started
        # Revisit older publication dates for late indexing and continuing revisions.
        overlap = DAY if plan is RISK else 3 * DAY
        lookback = 14 * DAY if plan is RISK else 30 * DAY
        cursor = lease.last_success if lease.last_success is not None else lease.due
        window = max(start - lookback, min(cursor, start) - overlap)
        gap = cursor - overlap < window or (
            lease.last_success is None and start - lease.due > lookback
        )
        after = (_wall_time(window).date() - timedelta(days=1)).isoformat()
        entity = "" if lease.entity == MARKET else f"{lease.entity[:6]} "
        query = f"{entity}{plan.terms} after:{after}"
        searched = False
        accepted = []
        reasons = set()
        try:
            rows = await self._provider_call("search", query)
            searched = True
            fetch_available = True
            for raw in sorted(
                rows[:RESULT_LIMIT],
                key=lambda row: (
                    urlsplit(_public_url(row.get("url")) or "").hostname not in AUTHORITY_HOSTS
                    if isinstance(row, dict)
                    else True
                ),
            ):
                canonical = _public_url(raw.get("url")) if isinstance(raw, dict) else None
                if not canonical:
                    reasons.add("filtered_results")
                    continue
                body = _clean(raw.get("content"), RESPONSE_LIMIT)
                fetched = clock() if body else None
                if (
                    not body
                    and fetch_available
                    and _fetchable(raw["url"])
                    and _associated(lease.entity, _clean(raw.get("title"), 200), "")
                ):
                    fetch_available = False
                    try:
                        body = _clean(
                            await self._provider_call("fetch", raw["url"]), RESPONSE_LIMIT
                        )
                        fetched = clock() if body else None
                    except FactLookupError as exc:
                        reasons.add(f"fetch_{exc.code}")
                fact = _build_result(
                    raw, body=body, fetched=fetched, entity=lease.entity, canonical=canonical
                )
                if fact is None:
                    reasons.add("unresolved_entity")
                    continue
                if not body:
                    reasons.add("missing_body")
                if fact["bodyTruncated"]:
                    reasons.add("body_limit")
                accepted.append(fact)
            reason = next(
                (
                    value
                    for value in (
                        "missing_body",
                        "body_limit",
                        "unresolved_entity",
                        "filtered_results",
                    )
                    if value in reasons
                ),
                "observed" if rows else "no_results",
            )
        except FactLookupError as exc:
            reason = exc.code
        now = clock()
        next_due = _fixed_due(plan, start, self.verdict)
        self.archive.finish(
            lease,
            now=now,
            next_due=next_due,
            window_start=window,
            reason=reason,
            details=sorted(reasons),
            gap=gap,
            results=accepted,
            searched=searched,
        )


def _configured_limits() -> FactLimits | None:
    prefix = "TICKDESK_FACTS_"
    legacy = ("DAILY_USD", "SEARCH_USD_PER_1000", "FETCH_USD_PER_1000")
    generic = ("DAILY", "SEARCH_PER_1000", "FETCH_PER_1000")
    optional = ("SEARCH_DAILY", "FETCH_DAILY", "SEARCH_CALLS_PER_DAY", "FETCH_CALLS_PER_DAY",
                "SEARCH_CALLS_PER_MINUTE", "FETCH_CALLS_PER_MINUTE")
    configured = {key: os.environ[prefix + key] for key in (*legacy, *generic, *optional, "CURRENCY")
                  if prefix + key in os.environ}
    if not configured:
        return None
    currency = configured.get("CURRENCY", "USD")
    names = generic if "CURRENCY" in configured else legacy
    unused = legacy if names == generic else generic
    if currency not in {"USD", "CNY"} or any(key in configured for key in unused):
        raise ValueError("invalid fact budget currency")

    def amount(key):
        value = Decimal(configured[key])
        if not value.is_finite() or not Decimal("0.000000001") <= value <= Decimal("10000"):
            raise ValueError("invalid fact budget amount")
        return value

    defaults = FactLimits()

    def attempts(tool, window):
        field = f"{tool.upper()}_CALLS_PER_{window.upper()}"
        value = int(configured.get(field, getattr(defaults, f"{tool}_per_{window}")))
        if not 1 <= value <= getattr(defaults, f"{tool}_per_{window}"):
            raise ValueError("invalid fact request limit")
        return value

    return FactLimits(
        *(amount(key) for key in names), currency=currency,
        search_daily=amount("SEARCH_DAILY") if "SEARCH_DAILY" in configured else None,
        fetch_daily=amount("FETCH_DAILY") if "FETCH_DAILY" in configured else None,
        search_per_day=attempts("search", "day"), fetch_per_day=attempts("fetch", "day"),
        search_per_minute=attempts("search", "minute"), fetch_per_minute=attempts("fetch", "minute"),
    )


def _text_bytes(value) -> int:
    return len(value.encode("utf-8", "replace")) if isinstance(value, str) else 0


def _configured_provider() -> tuple[str, FactLimits]:
    limits = _configured_limits()
    if limits is None:
        raise ValueError("fact budget is unconfigured")
    path = Path(os.environ["TICKDESK_FACTS_KEY_FILE"])
    with path.open("rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or (os.name != "nt" and info.st_mode & 0o077):
            raise ValueError("credential permissions")
        value = file.read(1025)
        if len(value) > 1024:
            raise ValueError("credential length")
        token = value.strip()
    if not re.fullmatch(rb"[\x21-\x7e]{1,1024}", token):
        raise ValueError("invalid credential")
    return token.decode("ascii"), limits


async def run_fact_collection(
    archive: FactArchive,
    scope_callback: Callable[[], Awaitable[Sequence[str] | dict]],
) -> None:
    """Lifespan-owned background loop. Cancelling it closes the network client."""
    if os.getenv("TICKDESK_FACTS_ENABLED") != "1":
        return
    try:
        token, limits = _configured_provider()
    except (OSError, KeyError, InvalidOperation, ValueError):
        archive.configuration = "invalid_configuration"
        return
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        collector = FactCollector(archive, DeepSeekFacts(client, token), limits=limits)
        while True:
            try:
                async with asyncio.timeout(3):
                    scope = await scope_callback()
            except (TimeoutError, OSError, ValueError, httpx.HTTPError, RuntimeError):
                # A bad scope poll cannot authorize reusing previous private-account scope.
                await collector.tick([], scope_status="scope_unavailable")
            else:
                await collector.tick(scope)
            await asyncio.sleep(2)
