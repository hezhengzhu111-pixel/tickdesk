import json
import sqlite3
from datetime import datetime
from decimal import Decimal

import httpx
import pytest

from app.services.tickdesk_facts import (
    DeepSeekFacts,
    FactArchive,
    FactCollector,
    FactLimits,
    FactLookupError,
    run_fact_collection,
)


class Clock:
    def __init__(self, value="2026-11-14T18:00:00+08:00"):
        self.now = int(datetime.fromisoformat(value).timestamp() * 1000)

    def __call__(self):
        return self.now

    def advance(self, minutes):
        self.now += minutes * 60_000


def result(
    url="https://www.sse.com.cn/disclosure/listedinfo/announcement/c/example.shtml",
    body="证券代码：600000。董事会公布年度经营事项。",
    title="600000 公司公告",
    date=None,
):
    return {
        "url": url,
        "title": title,
        "content": body,
        "date": date,
        "site_name": "上海证券交易所",
        "snippet": "公告摘要",
    }


class Provider:
    def __init__(self, rows=()):
        self.rows = rows
        self.queries = []

    async def search(self, query):
        self.queries.append(query)
        if isinstance(self.rows, FactLookupError):
            raise self.rows
        return self.rows if any(symbol in query for symbol in ("600000", "000001")) else []

    async def fetch(self, url):
        pytest.fail("Fetch must be skipped for results with content or unapproved URLs")


@pytest.mark.asyncio
async def test_delayed_indexing_and_revisions_respect_historical_cutoff(tmp_path):
    clock = Clock()
    archive = FactArchive(tmp_path / "facts.sqlite3")
    provider = Provider([result(date="2026-11-13T08:00:00Z")])
    collector = FactCollector(archive, provider, clock=clock)
    await collector.tick(["600000.SH"])
    first, _ = archive.context(clock(), "600000.SH", [])
    assert len(first) == 1
    assert first[0]["availableAt"] == clock()
    assert first[0]["publishedAt"] < first[0]["availableAt"]
    assert archive.context(clock() - 1, "600000.SH", [])[0] == []

    clock.advance(5)
    revised = "证券代码：600000。公司撤回董事会事项。"
    provider.rows = [result(body=revised, title="600000 撤回公告")]
    await collector.tick(["600000.SH"])
    current, _ = archive.context(clock(), "600000.SH", [])
    assert current[0]["publishedAt"] is None
    assert current[0]["withdrawsFactId"] == first[0]["factId"]
    assert current[0]["documentId"] == first[0]["documentId"]
    assert current[0]["factId"] != first[0]["factId"]
    assert archive.context(clock() - 1, "600000.SH", [])[0] == first


@pytest.mark.asyncio
async def test_weekend_risk_and_aggregate_due_survive_restart(tmp_path):
    clock = Clock("2026-11-14T21:25:00+08:00")
    path = tmp_path / "facts.sqlite3"
    provider = Provider()
    collector = FactCollector(FactArchive(path), provider, clock=clock)
    await collector.tick(["600000.SH", "000001.SZ"])
    first_queries = list(provider.queries)
    assert len([q for q in first_queries if "重大风险" in q]) == 2
    assert not any("休市汇总" in q for q in first_queries)

    clock.advance(5)
    collector = FactCollector(FactArchive(path), provider, clock=clock)
    await collector.tick(["600000.SH", "000001.SZ"])
    late_queries = provider.queries[len(first_queries) :]
    assert len([q for q in late_queries if "重大风险" in q]) == 2
    assert len([q for q in late_queries if "休市汇总" in q]) == 1
    assert not any("普通新闻" in q or "行业供需" in q for q in provider.queries)
    await collector.tick(["600000.SH", "000001.SZ"])
    assert provider.queries == first_queries + late_queries


@pytest.mark.asyncio
async def test_identical_repost_is_one_fact_but_independent_account_is_retained(tmp_path):
    clock = Clock()
    archive = FactArchive(tmp_path / "facts.sqlite3")
    primary = result()
    repost = result(url="https://news.example.com/repost")
    independent = result(
        url="https://press.example.com/story", body="证券代码：600000。消息另有解释。"
    )
    unrelated = result(
        url="https://other.example.com/",
        body="证券代码：000001。另一家公司。",
        title="000001 公司公告",
    )
    await FactCollector(
        archive, Provider([primary, repost, independent, unrelated]), clock=clock
    ).tick(["600000.SH"])
    facts, _ = archive.context(clock(), "600000.SH", [])
    assert {fact["url"] for fact in facts} == {primary["url"], independent["url"]}
    assert facts[0]["relatedSources"] == [repost["url"]]
    assert facts[0]["potentialConflicts"] == [facts[1]["factId"]]
    assert archive.context(clock(), "000001.SZ", [])[0] == []


@pytest.mark.asyncio
async def test_repost_can_diverge_without_replacing_exchange_announcement(tmp_path):
    clock = Clock()
    archive = FactArchive(tmp_path / "facts.sqlite3")
    official = result()
    url = "https://news.example.com/repost"
    provider = Provider([official, result(url=url)])
    collector = FactCollector(archive, provider, clock=clock)
    await collector.tick(["600000.SH"])
    old, _ = archive.context(clock(), "600000.SH", [])

    clock.advance(5)
    provider.rows = [official, result(url=url, body="证券代码：600000。转载方随后更新了内容。")]
    await collector.tick(["600000.SH"])
    current, _ = archive.context(clock(), "600000.SH", [])
    assert {fact["url"] for fact in current} == {official["url"], url}
    repost = next(fact for fact in current if fact["url"] == url)
    assert repost["documentId"] != old[0]["documentId"]
    assert repost["reprintsFactId"] == old[0]["factId"]
    assert repost["supersedesFactId"] is None


@pytest.mark.asyncio
async def test_long_offline_window_is_coalesced_with_an_explicit_gap(tmp_path):
    clock = Clock()
    path = tmp_path / "facts.sqlite3"
    provider = Provider()
    await FactCollector(FactArchive(path), provider, clock=clock).tick(["600000.SH"])
    first_queries = len([q for q in provider.queries if "重大风险" in q])
    clock.advance(60 * 24 * 20)
    archive = FactArchive(path)
    collector = FactCollector(archive, provider, clock=clock)
    await collector.tick(["600000.SH"])
    await collector.tick(["600000.SH"])
    _, coverage = archive.context(clock(), "600000.SH", [])
    risk = coverage["plans"]["600000.SH"]["companyRisk"]
    assert risk["gap"] is True
    assert risk["lastSuccessAt"] == clock()
    assert risk["nextDueAt"] == clock() + 300_000
    assert len([q for q in provider.queries if "重大风险" in q]) == first_queries + 1


@pytest.mark.asyncio
async def test_unmatched_risk_waiting_in_queue_is_probed_without_starvation(tmp_path):
    clock = Clock("2026-11-14T21:30:00+08:00")
    archive = FactArchive(tmp_path / "facts.sqlite3")
    provider = Provider()
    symbols = [f"{600000 + index}.SH" for index in range(64)]
    collector = FactCollector(archive, provider, clock=clock)
    for _ in range(10):
        await collector.tick(symbols)
    assert {query.split()[0] for query in provider.queries if "重大风险" in query} == {
        item.split(".")[0] for item in symbols
    }
    assert any("休市汇总" in query for query in provider.queries)


@pytest.mark.asyncio
async def test_persistent_fee_budget_and_failed_coverage(tmp_path):
    clock = Clock()
    path = tmp_path / "facts.sqlite3"
    limits = FactLimits(daily=Decimal("0.01"), search_per_1000=Decimal("10"))
    provider = Provider([result()])
    first = FactArchive(path)
    await FactCollector(first, provider, clock=clock, limits=limits).tick(["600000.SH"])
    assert (
        first.context(clock(), "600000.SH", [])[1]["plans"]["600000.SH"]["companyRisk"]["status"]
        == "partial"
    )
    clock.advance(5)
    second = FactArchive(path)
    await FactCollector(second, provider, clock=clock, limits=limits).tick(["600000.SH"])
    _, coverage = second.context(clock(), "600000.SH", [])
    assert coverage["plans"]["600000.SH"]["companyRisk"]["reason"] == "budget_exhausted"
    assert len(provider.queries) == 1


def test_search_and_fetch_have_separate_currency_checked_spend_limits(tmp_path):
    clock = Clock()
    archive = FactArchive(tmp_path / "facts.sqlite3")
    limits = FactLimits(currency="CNY", daily=Decimal("0.10"), search_per_1000=Decimal("20"),
                        fetch_per_1000=Decimal("30"), search_daily=Decimal("0.04"), fetch_daily=Decimal("0.07"))
    first = archive.reserve("search", clock(), limits, argument_bytes=9)
    second = archive.reserve("search", clock(), limits, argument_bytes=5)
    with pytest.raises(FactLookupError, match="budget_exhausted"):
        archive.reserve("search", clock(), limits)
    first_fetch = archive.reserve("fetch", clock(), limits, argument_bytes=45)
    archive.request_outcome(first, "ok", clock(), True, returned_bytes=15)
    archive.request_outcome(second, "ok", clock(), False, returned_bytes=0)
    archive.reserve("search", clock(), limits, argument_bytes=7)
    archive.request_outcome(first_fetch, "timeout", clock(), None)
    archive.reserve("fetch", clock(), limits)
    with pytest.raises(FactLookupError, match="budget_exhausted"):
        archive.reserve("fetch", clock(), limits)
    with pytest.raises(FactLookupError, match="budget_currency_changed"):
        archive.reserve("search", clock(), FactLimits(currency="USD", daily=Decimal("0.10")))
    usage = FactArchive(archive.path).context(clock(), "600000.SH", [])[1]["usage"]
    assert usage["search"] == {"currency": "CNY", "attempted": 3, "billable": 1, "reservedCost": "0.04",
                               "argumentBytes": 21, "attemptsWithArgumentBytes": 3, "returnedTextBytes": 15,
                               "attemptsWithReturnedTextBytes": 2}
    assert usage["fetch"]["reservedCost"] == "0.06" and usage["fetch"]["returnedTextBytes"] is None


def test_legacy_fact_accounting_migrates_without_reinterpreting_cost_or_volume(tmp_path, monkeypatch):
    path = tmp_path / "prior-facts.sqlite3"
    clock = Clock()
    with sqlite3.connect(path) as db:
        db.executescript("""CREATE TABLE fact_requests(id INTEGER PRIMARY KEY,time INTEGER NOT NULL,day TEXT NOT NULL,
                         tool TEXT NOT NULL,price INTEGER NOT NULL,outcome TEXT,completed INTEGER,billed INTEGER);
                         CREATE TABLE fact_daily_budget(day TEXT PRIMARY KEY,search_attempts INTEGER NOT NULL DEFAULT 0,
                         fetch_attempts INTEGER NOT NULL DEFAULT 0,reserved_cost INTEGER NOT NULL DEFAULT 0);""")
        db.execute("INSERT INTO fact_daily_budget VALUES ('2026-11-14',1,0,1000000)")
        db.execute("INSERT INTO fact_requests VALUES (1,?,'2026-11-14','search',1000000,'ok',?,1)", (clock(), clock()))
    archive = FactArchive(path)
    usage = archive.budget_diagnostics(now=clock())["dailyUsage"]
    assert usage == {"search": {"currency": "USD", "attempted": 1, "billable": 1,
                                "reservedCost": "0.001", "reservedUsd": "0.001", "argumentBytes": None,
                                "attemptsWithArgumentBytes": 0, "returnedTextBytes": None,
                                "attemptsWithReturnedTextBytes": 0}}
    for key, value in {"CURRENCY": "CNY", "DAILY": "1", "SEARCH_PER_1000": "1", "FETCH_PER_1000": "1"}.items():
        monkeypatch.setenv(f"TICKDESK_FACTS_{key}", value)
    assert archive.budget_diagnostics(now=clock())["configuration"] == "currency_change_pending"
    with pytest.raises(FactLookupError, match="budget_currency_changed"):
        archive.reserve("fetch", clock(), FactLimits(currency="CNY", daily=Decimal("10")))
    clock.advance(24 * 60)
    assert archive.budget_diagnostics(now=clock())["configuration"] == "configured"
    token = archive.reserve("fetch", clock(), FactLimits(currency="CNY", daily=Decimal("10")), argument_bytes=24)
    archive.request_outcome(token, "ok", clock() + 1, True, returned_bytes=45)
    assert archive.budget_diagnostics(now=clock())["dailyUsage"]["fetch"]["returnedTextBytes"] is None
    assert archive.budget_diagnostics(now=clock() + 1)["dailyUsage"]["fetch"]["returnedTextBytes"] == 45


def test_configured_fact_budget_requires_currency_consistent_values(tmp_path, monkeypatch):
    archive = FactArchive(tmp_path / "facts.sqlite3")
    clock = Clock()
    for key, value in {"CURRENCY": "CNY", "DAILY": "0.10", "SEARCH_PER_1000": "7", "FETCH_PER_1000": "9",
                       "SEARCH_DAILY": "0.02", "FETCH_DAILY": "0.03", "SEARCH_CALLS_PER_DAY": "3"}.items():
        monkeypatch.setenv(f"TICKDESK_FACTS_{key}", value)
    assert archive.budget_diagnostics(now=clock()) == {
        "configuration": "configured", "currency": "CNY", "combinedDaily": "0.10",
        "toolDaily": {"search": "0.02", "fetch": "0.03"}, "dailyUsage": {},
    }
    monkeypatch.setenv("TICKDESK_FACTS_DAILY_USD", "0.50")
    assert archive.budget_diagnostics(now=clock())["configuration"] == "invalid_configuration"


@pytest.mark.asyncio
async def test_fetch_uses_safe_targets_and_rejects_transport_redirects():
    requests = []

    def response(request):
        requests.append(request.url)
        return httpx.Response(302, headers={"location": "http://127.0.0.1:9/private"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(response), follow_redirects=True
    ) as client:
        provider = DeepSeekFacts(client, "fake-test-key")
        for target in (
            "http://127.0.0.1/high",
            "https://news.example.com/redirect",
            "https://www.sse.com.cn/disclosure/listedinfo/announcement/c/evil.shtml?url=x",
            "https://www.sse.com.cn@127.0.0.1/x",
        ):
            with pytest.raises(FactLookupError, match="unsafe_url"):
                await provider.fetch(target)
        assert requests == []
        with pytest.raises(FactLookupError, match="unsafe_redirect"):
            await provider.fetch(result()["url"])
        assert [str(url) for url in requests] == ["https://api.deepseek.com/v1/tools/fetch"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_body", "reason"),
    [
        (b'{"results":["' + b"x" * (512 * 1024) + b'"]}', "response_limit"),
        (b'{"results":["\xff"]}', "invalid_response"),
    ],
    ids=["oversize", "malformed-utf8"],
)
async def test_direct_search_rejects_oversize_and_malformed_encoding(response_body, reason):
    def respond(request):
        assert request.url.path == "/v1/tools/search"
        return httpx.Response(200, content=response_body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(FactLookupError, match=reason):
            await DeepSeekFacts(client, "fake-test-key").search("600000 公告")


@pytest.mark.asyncio
async def test_body_truncation_still_compares_the_observed_content(tmp_path):
    clock = Clock()
    archive = FactArchive(tmp_path / "facts.sqlite3")
    prefix = "证券代码：600000。" + "资料" * 12_100
    provider = Provider([result(body=prefix + "首次")])
    collector = FactCollector(archive, provider, clock=clock)
    await collector.tick(["600000.SH"])
    first, _ = archive.context(clock(), "600000.SH", [])
    clock.advance(5)
    provider.rows = [result(body=prefix + "修订")]
    await collector.tick(["600000.SH"])
    latest, coverage = archive.context(clock(), "600000.SH", [])
    assert latest[0]["bodyTruncated"] is True
    assert latest[0]["supersedesFactId"] == first[0]["factId"]
    assert coverage["plans"]["600000.SH"]["companyRisk"]["reason"] == "body_limit"


@pytest.mark.asyncio
async def test_context_provides_a_bounded_citation_for_oversized_urls(tmp_path):
    clock = Clock()
    archive = FactArchive(tmp_path / "facts.sqlite3")
    url = "https://notice.example.com/" + "notice-" * 200
    await FactCollector(archive, Provider([result(url=url)]), clock=clock).tick(["600000.SH"])
    facts, _ = archive.context(clock(), "600000.SH", [])
    assert facts[0]["citationRef"] == "local:tickdesk-fact:" + facts[0]["factId"]
    assert "body" not in facts[0]
    assert len(json.dumps(facts, ensure_ascii=False).encode()) <= 32 * 1024


@pytest.mark.asyncio
async def test_missing_body_preserves_provider_failure_and_untrusted_snippet(tmp_path):
    clock = Clock()
    archive = FactArchive(tmp_path / "facts.sqlite3")

    class FetchLimited(Provider):
        async def fetch(self, url):
            raise FactLookupError("rate_limited")

    provider = FetchLimited([result(body="")])
    await FactCollector(archive, provider, clock=clock).tick(["600000.SH"])
    facts, coverage = archive.context(clock(), "600000.SH", [])
    assert facts[0]["excerptKind"] == "search_snippet"
    assert facts[0]["citationAvailable"] is False
    assert coverage["plans"]["600000.SH"]["companyRisk"]["details"] == [
        "fetch_rate_limited",
        "missing_body",
    ]


@pytest.mark.asyncio
async def test_disabled_runner_ignores_configured_key(tmp_path, monkeypatch):
    async def scope():
        pytest.fail("Disabled collector must not read scope or contact provider")

    monkeypatch.delenv("TICKDESK_FACTS_ENABLED", raising=False)
    monkeypatch.setenv("TICKDESK_FACTS_KEY_FILE", str(tmp_path / "missing"))
    await run_fact_collection(FactArchive(tmp_path / "facts.sqlite3"), scope)
