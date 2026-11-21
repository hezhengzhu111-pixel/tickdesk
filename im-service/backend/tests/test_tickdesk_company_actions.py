import asyncio
import json
from datetime import date, datetime

import httpx

from app.services import tickdesk_company_actions as actions
from app.services.tickdesk_adapter import CN, timestamp


def _snapshot(company, record="20261105", ex="20261106", cash="0.11"):
    return {"COMPANY_CODE": company, "A_STOCK_CODE": company, "A_REG_DATE": record, "A_DIV_DATE": ex,
            "A_BEFR_TAX_DIV": cash, "PRE_CLOSE_PRICE": "7.3", "OPEN_PRICE": "7.25"}


def _report(rows, *, total=None):
    return {"sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_LRFP_FH_L", "pageHelp": {"pageNo": 1, "total": len(rows) if total is None else total},
            "result": rows}


def test_scoped_cash_dividend_archive_preserves_amount_and_two_clocks(tmp_path, monkeypatch):
    now = datetime(2026, 11, 16, 16, tzinfo=CN)

    class Clock(datetime):
        @classmethod
        def now(cls, tz):
            return now.astimezone(tz)

    monkeypatch.setattr(actions, "datetime", Clock)
    monkeypatch.setattr(actions, "read_exchange_master", lambda *_: [
        {"symbol": "601319.SH", "board": "沪主板", "status": "listed"},
        {"symbol": "000001.SZ", "board": "深主板", "status": "listed"},
        {"symbol": "688069.SH", "board": "科创板", "status": "listed"},
    ])
    matched = []

    def respond(request):
        matched.append(request.url.params["COMPANY_CODE"])
        assert request.url.params["IS_STAR"] == "0" and request.url.params["CONDITION_ZBA"] == "1"
        assert request.url.params["pageHelp.pageSize"] == "5"
        return httpx.Response(200, json=_report([_snapshot("601319"), _snapshot("601319", "20261119", "20261120")]))

    async def collect():
        with_scope = {"601319.SH", "000001.SZ", "688069.SH", "510300.SH"}
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await actions.collect_sse_cash_dividends(client, tmp_path, with_scope)

    seen = timestamp(now)
    status = asyncio.run(collect())
    assert status["status"] == "partial" and status["archivedEvents"] == 1
    assert asyncio.run(collect())["archivedEvents"] == 1
    assert matched == ["601319"]
    day = date(2026, 11, 6)
    assert actions.read_sse_cash_dividends(tmp_path, "601319.SH", seen - 1, effective_on=day) == []
    assert actions.read_sse_cash_dividends(tmp_path, "601319.SH", seen, effective_on=date(2026, 11, 5)) == []
    event, = actions.read_sse_cash_dividends(tmp_path, "601319.SH", seen, effective_on=day)
    assert event == {"symbol": "601319.SH", "effectiveDate": "2026-11-06", "recordDate": "2026-11-05",
                     "cashGrossPerShare": "0.11", "currency": "CNY",
                     "source": {"name": "上海证券交易所", "ref": actions.SSE_COMPANY + "?COMPANY_CODE=601319", "asOf": seen}}


def test_invalid_rows_or_incomplete_page_cannot_invent_an_event(tmp_path):
    findings = [_report([_snapshot("600000")]), _report([_snapshot("601319")], total=4),
                _report([_snapshot("601319", cash="0.11"), _snapshot("601319", cash="0.25")])]
    responses = iter(findings)

    async def read_all():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=next(responses)))) as client:
            for _ in findings:
                assert await actions.observe_sse_cash_dividends(client, tmp_path, "601319.SH") is None

    asyncio.run(read_all())
    assert not list(tmp_path.rglob("*.json"))


def _cninfo_distribution():
    return {"F010N": 1, "F011N": None, "F012N": 0.4, "F013D": "2026-06-04", "F014D": "2026-06-11",
            "F015D": "2026-06-10", "F016D": "2026-06-11", "F017D": "2026-06-11"}


def _cninfo_response(request, rows, *, code="300502"):
    callback = request.url.params["jsonpCallback"]
    assert request.url.params["scode"] == "300502"
    if request.url.path.endswith("getIndexData"):
        payload = {"codeInfo": {"SECCODE": code, "F003N": "1", "F002N": 1}}
    else:
        assert request.url.params["mergerMark"] == "dividendData"
        assert request.url.params["sign"] == "1"
        payload = {"dividendData": rows}
    return httpx.Response(200, content=f"{callback}({json.dumps(payload)});")


def test_szse_distributions_archive_announcement_schedule_and_original_observation(tmp_path, monkeypatch):
    now = datetime(2026, 11, 16, 19, tzinfo=CN)

    class Clock(datetime):
        @classmethod
        def now(cls, tz):
            return now.astimezone(tz)

    monkeypatch.setattr(actions, "datetime", Clock)
    monkeypatch.setattr(actions, "read_exchange_master", lambda *_: [
        {"symbol": "601319.SH", "board": "沪主板", "status": "listed"},
        {"symbol": "300502.SZ", "board": "创业板", "status": "listed"},
        {"symbol": "000006.SZ", "board": "深主板", "status": "delisted"},
    ])
    requested = []
    current_rows = [_cninfo_distribution()]

    def respond(request):
        requested.append(request.url.path)
        return _cninfo_response(request, current_rows)

    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await actions.collect_szse_distributions(client, tmp_path, {"300502.SZ", "601319.SH", "000006.SZ", "159915.SZ"})

    first = timestamp(now)
    date_ex = date(2026, 6, 11)
    assert asyncio.run(collect()) == {"status": "partial", "scopeSymbols": 1, "checkedSymbols": 1, "archivedEvents": 1}
    assert asyncio.run(collect())["archivedEvents"] == 1
    assert len(requested) == 2
    assert actions.read_szse_distributions(tmp_path, "300502.SZ", first - 1, effective_on=date_ex) == []
    assert actions.read_szse_distributions(tmp_path, "300502.SZ", first, effective_on=date(2026, 6, 10)) == []
    event, = actions.read_szse_distributions(tmp_path, "300502.SZ", first, effective_on=date_ex)
    assert event["announcementDate"] == "2026-06-04" and event["recordDate"] == "2026-06-10"
    assert event["effectiveDate"] == event["cashDistributionDate"] == event["shareDistributionDate"] == "2026-06-11"
    assert event["cashPerShare"] == "1" and event["capitalReserveSharesPerShare"] == "0.4"
    assert event["bonusSharesPerShare"] is None and event["source"]["asOf"] == first

    now = datetime(2026, 11, 17, 19, tzinfo=CN)
    assert asyncio.run(collect())["archivedEvents"] == 1 and len(requested) == 4
    assert actions.read_szse_distributions(tmp_path, "300502.SZ", timestamp(now), effective_on=date_ex) == [event]

    now = datetime(2026, 11, 18, 19, tzinfo=CN)
    current_rows[0] = {**current_rows[0], "F010N": 1.1}
    revised_at = timestamp(now)
    assert asyncio.run(collect())["archivedEvents"] == 1
    assert actions.read_szse_distributions(tmp_path, "300502.SZ", revised_at - 1, effective_on=date_ex) == [event]
    revised, = actions.read_szse_distributions(tmp_path, "300502.SZ", revised_at, effective_on=date_ex)
    assert (revised["cashPerShare"], revised["source"]["asOf"]) == ("1.1", revised_at)


def test_szse_identity_conflict_and_non_jsonp_cannot_create_a_distribution(tmp_path):
    first = _cninfo_distribution()
    variants = [lambda req: _cninfo_response(req, [first], code="300503"),
                lambda req: _cninfo_response(req, [first, {**first, "F010N": 2}]),
                lambda req: httpx.Response(200, content=b"alert('invalid');")]

    async def read():
        for variant in variants:
            async with httpx.AsyncClient(transport=httpx.MockTransport(variant)) as client:
                assert await actions.observe_szse_distributions(client, tmp_path, "300502.SZ") is None

    asyncio.run(read())
    assert not list(tmp_path.rglob("*.json"))
