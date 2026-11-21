import asyncio
from datetime import date, datetime

import httpx
import pytest

from app.services import tickdesk_exchange_holding as exchange_holding
from app.services.tickdesk_exchange_holding import build_current_rules, suspension_episodes
from app.services.tickdesk_holding_rules import (
    prepare_holding_rule_batch,
    validate_holding_reference,
)
from app.tickflow.repository import DataStore, KlineRepository


def ms(value):
    return int(datetime.fromisoformat(value + "+08:00").timestamp() * 1000)


def test_current_exchange_rules_keep_unconfirmed_axes_and_cross_holiday():
    observed = ms("2026-09-30T08:20:00")
    listing = {"symbol": "605255.SH", "status": "listed", "exchange": "SH", "board": "沪主板",
               "as_of": ms("2026-09-30T07:00:00"), "source": "上海证券交易所", "source_ref": "https://www.sse.com.cn/assortment/stock/list/share/"}
    extra = {**listing, "symbol": "688798.SH", "board": "科创板"}
    record, = build_current_rules([listing, extra], {"605255.SH", "688798.SH"}, {}, observed)
    validate_holding_reference(record)
    value, = record["value"]
    assert value["rule"]["settlement"] == "T+1" and value["rule"]["dayTrading"] == "NO"
    assert value["rule"]["tradingStatus"] == "unknown" and value["rule"]["dataStatus"] == "missing"
    assert value["companyActionsCheckedThrough"] is None
    calendar = value["calendar"]["sessions"]
    following = next(day for day in calendar if day["date"] > "2026-09-30")
    assert following == {"date": "2026-10-08", "firstExecutionAtMs": ms("2026-10-08T09:25:00")}


def test_suspension_only_accepts_positive_unambiguous_exchange_rows():
    base = {"controlType": "TR", "type": "LXTP", "startStopDate": "20261112", "endStopDate": "", "stopTime": ""}
    rows = [
        {**base, "productCode": "600185"},
        {**base, "productCode": "600186", "endStopDate": "20261116"},
        {**base, "productCode": "600187", "type": "LSTP", "startStopDate": "20261116",
         "endStopDate": "20261116", "stopTime": "AM"},
        {**base, "productCode": "600188", "type": "LSTP", "startStopDate": "20261116",
         "endStopDate": "20261116", "stopTime": "WH"},
        {**base, "productCode": "600189", "startStopDate": "20261117"},
        {**base, "productCode": "600190", "controlType": "GB"},
    ]
    assert suspension_episodes(rows, "20261116") == {"600185.SH": "20261112", "600188.SH": "20261116"}


def test_partial_official_suspension_crosses_the_existing_holding_batch(tmp_path):
    observed = ms("2026-11-16T09:10:00")
    listing = {"symbol": "605255.SH", "status": "listed", "exchange": "SH", "board": "沪主板",
               "as_of": observed, "source": "上海证券交易所", "source_ref": "https://www.sse.com.cn/assortment/stock/list/share/"}
    records = build_current_rules([listing, {**listing, "symbol": "600185.SH"}],
                                  {"605255.SH", "600185.SH"}, {"605255.SH": "20261116"}, observed)
    store = DataStore.readonly(tmp_path)
    try:
        repo = KlineRepository(store)
        repo.write_quant_reference_evidence(records)
        result = prepare_holding_rule_batch(repo, {"requestedAt": observed + 1,
                                           "objects": [{"symbol": row["symbol"], "qty": 100} for row in records]})
        evidence, = result["rules"]
        rule = evidence["rule"]
        assert rule["symbol"] == "605255.SH" and rule["dataStatus"] == "missing"
        assert rule["sources"][-1]["ref"].endswith("#605255.SH-20261116")
        assert rule["tradingStatus"] == "suspended" and evidence["companyActionsCheckedThrough"] is None
        assert result.get("unavailable") is None
    finally:
        store.db.close()


def sz_row(code, start="", resume="", duration="停牌"):
    return {"zqdm": code, "tpkssj": start, "fpkssj": resume, "tpsj": duration, "tpyy": "重大事项"}


def test_szse_report_requires_all_pages_and_filters_ambiguous_or_intraday_rows():
    day = date(2026, 11, 16)
    start = f"{day} 开市"
    rows = [sz_row("000001", start), sz_row("000002", resume=start, duration="取消停牌"),
            sz_row("000003", "2026-11-13 开市"), sz_row("000004", start, f"{day} 10:30:00", "1小时"),
            sz_row("000005", start), sz_row("000005", resume=start, duration="取消停牌")]

    def response(request, *, complete):
        fields = request.url.params
        assert fields["txtKsrq"] == fields["txtZzrq"] == str(day)
        assert fields["CATALOGID"] == "1798"
        page = int(fields["PAGENO"])
        start_index = (page - 1) * 3
        page_rows = rows[start_index:start_index + 3] if complete or page == 1 else []
        return httpx.Response(200, json=[{
            "metadata": {"catalogid": "1798", "tabkey": "tab1", "subname": str(day),
                         "pagesize": 3, "pagecount": 2, "pageno": page, "recordcount": 6},
            "data": page_rows,
        }])

    async def read(complete):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response(request, complete=complete))) as client:
            return await exchange_holding.read_szse_suspensions(client, day)

    assert asyncio.run(read(True)) == {"000001.SZ": "suspension", "000002.SZ": "withdrawal", "000005.SZ": "conflict"}
    with pytest.raises(ValueError, match="incomplete Shenzhen suspension evidence"):
        asyncio.run(read(False))


def test_szse_partial_withdrawal_stays_scoped_and_cannot_backdate_or_confirm_trading(tmp_path, monkeypatch):
    observed = ms("2026-11-16T09:30:00")
    listing = {"symbol": "000635.SZ", "status": "listed", "exchange": "SZ", "board": "深主板",
               "as_of": observed - 60_000, "source": "深圳证券交易所",
               "source_ref": "https://www.szse.cn/market/product/stock/list/"}
    monkeypatch.setattr(exchange_holding, "read_exchange_master", lambda *_: [listing])
    symbol = "000635.SZ"
    before_effective = observed - 6 * 60_000
    earlier, = build_current_rules([{**listing, "as_of": before_effective}], {symbol}, {}, before_effective,
                                    szse_statuses={symbol: "suspension"})
    assert earlier["value"][0]["rule"]["tradingStatus"] == "unknown"
    store = DataStore.readonly(tmp_path)
    try:
        repo = KlineRepository(store)

        def publish(statuses, at):
            return exchange_holding.publish_current_rules(repo, {symbol}, {}, at, szse_statuses=statuses)

        def batch(at):
            return prepare_holding_rule_batch(repo, {"requestedAt": at, "objects": [{"symbol": symbol, "qty": 100}]})

        assert publish({symbol: "suspension"}, observed)["published"] == 1
        assert batch(observed - 1) is None
        assert publish({}, observed + 1)["published"] == 0
        assert batch(observed + 1)["rules"][0]["rule"]["tradingStatus"] == "suspended"
        assert publish({symbol: "withdrawal"}, observed + 2)["published"] == 1
        assert batch(observed + 1)["rules"][0]["rule"]["tradingStatus"] == "suspended"
        result = batch(observed + 2)
        assert "unavailable" not in result
        rule = result["rules"][0]["rule"]
        assert (rule["channel"], rule["tradingStatus"], rule["dataStatus"]) == ("auction", "unknown", "missing")
        assert "tradingStatus" in rule["issueFields"]
        assert rule["sources"][-1]["ref"].endswith("#withdrawal-000635.SZ-20261116")
        assert result["rules"][0]["companyActionsCheckedThrough"] is None
        assert batch(observed + 24 * 60 * 60 * 1000) is None
    finally:
        store.db.close()


def test_conflicting_opening_actions_supersede_prior_suspension_and_allow_later_resolution(tmp_path, monkeypatch):
    observed = ms("2026-11-16T09:30:00")
    day = "2026-11-16"
    symbol = "000635.SZ"
    listing = {"symbol": symbol, "status": "listed", "exchange": "SZ", "board": "深主板", "as_of": observed,
               "source": "深圳证券交易所", "source_ref": "https://www.szse.cn/market/product/stock/list/"}
    monkeypatch.setattr(exchange_holding, "read_exchange_master", lambda *_: [listing])
    suspension = sz_row(symbol[:6], f"{day} 开市")
    withdrawal = sz_row(symbol[:6], resume=f"{day} 开市", duration="取消停牌")
    for rows in ([suspension, withdrawal], [withdrawal, suspension]):
        assert exchange_holding._szse_opening_actions(rows, day) == {symbol: "conflict"}
    assert exchange_holding._szse_opening_actions([suspension, sz_row(symbol[:6], f"{day} 10:10:00")], day) == {}

    store = DataStore.readonly(tmp_path)
    try:
        repo = KlineRepository(store)

        def publish(rows, at):
            statuses = exchange_holding._szse_opening_actions(rows, day)
            return exchange_holding.publish_current_rules(repo, {symbol}, {}, at, szse_statuses=statuses)

        def batch(at):
            return prepare_holding_rule_batch(repo, {"requestedAt": at, "objects": [{"symbol": symbol, "qty": 100}]})

        assert publish([suspension], observed)["published"] == 1
        assert publish([suspension, withdrawal], observed + 1)["published"] == 1
        assert batch(observed)["rules"][0]["rule"]["tradingStatus"] == "suspended"
        assert batch(observed + 1) == {"version": 3, "rules": [], "unavailable": [
            {"symbol": symbol, "tradeDate": day, "availableAtMs": observed + 1, "reason": "conflict", "scope": "auctionStatus"},
        ]}
        assert publish([], observed + 2)["published"] == 0
        assert batch(observed + 2) == batch(observed + 1)
        assert publish([withdrawal], observed + 3)["published"] == 1
        assert batch(observed + 3)["rules"][0]["rule"]["tradingStatus"] == "unknown"
        assert publish([withdrawal, suspension], observed + 4)["published"] == 1
        assert batch(observed + 4)["unavailable"][0]["reason"] == "conflict"
        assert publish([suspension], observed + 5)["published"] == 1
        assert batch(observed + 5)["rules"][0]["rule"]["tradingStatus"] == "suspended"
        assert batch(observed + 86_400_000) is None
    finally:
        store.db.close()
