import json
from datetime import datetime

import polars as pl

from app.financial_history import merge_report_revisions
from app.services.tickdesk_adapter import timestamp
from app.services.tickdesk_ai import _metric_catalog
from app.services.tickdesk_ai_inputs import read_market_evidence
from app.services.tickdesk_company_actions import CNINFO_COMPANY, SSE_COMPANY
from app.tickflow.repository import DataStore, KlineRepository


def test_financial_context_uses_payload_capture_and_field_provenance(tmp_path):
    symbol = "300502.SZ"
    base = {"symbol": symbol, "period_end": "2026-09-30", "announce_date": "2026-10-30",
            "_financial_source": "test-supplier"}
    for table, rows in {
        "metrics": [
            {**base, "roe": 24.47, "gross_margin": 51.25, "_financial_observation_only": True,
             "_financial_observed_at": "2026-11-16T08:11:34.000000+00:00"},
            {**base, "roe": 25, "_financial_observation_only": True,
             "_financial_observed_at": "2026-11-16T08:19:00.000000+00:00"},
            {**base, "roe": 25, "_financial_observation_only": True,
             "_financial_observed_at": "2026-11-16T08:20:00.000000+00:00"},
        ],
        "income": [
            {**base, "revenue": 31849895741.23, "net_income_attributable": 11372330534.73, "currency": "CNY",
             "_financial_observed_at": "2026-11-16T08:10:34.000000+00:00"},
        ],
    }.items():
        path = tmp_path / "financials" / table
        path.mkdir(parents=True)
        merge_report_revisions(pl.DataFrame(rows)).write_parquet(path / "part.parquet")
    store = DataStore.readonly(tmp_path)
    try:
        repo = KlineRepository(store)

        def read(clock):
            cutoff = timestamp(datetime.fromisoformat(f"2026-11-16T{clock}+00:00"))
            rows, _ = read_market_evidence(repo, cutoff, [symbol])
            return {row["record"]["field"]: row for row in rows}

        assert read("08:09:00") == {}
        assert set(read("08:10:35")) == {"revenue", "net_income_attributable"}
        first = read("08:11:34")
        revised = read("08:19:00")
        for row, value in ((first["roe_latest"], "24.47"), (revised["roe_latest"], "25")):
            assert row["record"]["value"] == value and row["record"]["unit"] == "%"
            assert row["source"]["asOf"] == timestamp(row["record"]["observedAt"])
            assert row["record"]["sourceRevisionId"]
        assert first["gross_margin_latest"] == revised["gross_margin_latest"]
        assert revised == read("08:20:00")
        revenue = first["revenue"]["record"]
        assert (revenue["value"], revenue["currency"], revenue["announcedDate"]) == (
            "31849895741.23", "CNY", "2026-10-30",
        )
        assert revenue["periodEnd"] == "2026-09-30"
    finally:
        store.db.close()


def test_missing_publication_clock_uses_capture_and_invalid_capture_stays_unknown(tmp_path):
    base = {"period_end": "2026-09-30", "announce_date": "2026-10-30", "roe": 12,
            "_financial_source": "test-supplier"}
    frame = merge_report_revisions(pl.DataFrame([
        {**base, "symbol": "000001.SZ"},
        {**base, "symbol": "000002.SZ", "_financial_observed_at": "malformed"},
        {**base, "symbol": "000003.SZ", "announce_date": None,
         "_financial_observed_at": "2026-10-30T16:00:00+08:00"},
    ]))
    path = tmp_path / "financials" / "metrics"
    path.mkdir(parents=True)
    frame.write_parquet(path / "part.parquet")
    store = DataStore.readonly(tmp_path)
    try:
        repo = KlineRepository(store)

        def read(local_clock):
            stamp = timestamp(datetime.fromisoformat(local_clock + "+08:00"))
            return read_market_evidence(repo, stamp, ["000001.SZ", "000002.SZ", "000003.SZ"])[0]

        assert read("2026-10-30T15:59:59") == []
        [observed] = read("2026-10-30T16:00:00")
        assert observed["record"]["symbol"] == "000003.SZ"
        assert observed["record"]["announcedDate"] is None
        assert {row["record"]["symbol"] for row in read("2026-10-31T00:00:00")} == {"000001.SZ", "000003.SZ"}
    finally:
        store.db.close()


def test_latest_distribution_enters_context_and_metric_catalog_only_after_first_observation(tmp_path):
    observed = timestamp("2026-11-16T19:18:24.658+08:00")
    cninfo = f"{CNINFO_COMPANY}?code=300502"
    sse = f"{SSE_COMPANY}?COMPANY_CODE=601319"
    latest = {
        "symbol": "300502.SZ", "announcementDate": "2026-06-04",
        "recordDate": "2026-06-10", "effectiveDate": "2026-06-11",
        "cashDistributionDate": "2026-06-11", "shareDistributionDate": "2026-06-11",
        "cashPerShare": "1", "capitalReserveSharesPerShare": "0.4", "bonusSharesPerShare": None, "currency": "CNY",
        "source": {"ref": cninfo, "name": "巨潮资讯网", "asOf": observed},
    }
    older = {**latest, "announcementDate": "2025-06-04", "recordDate": "2025-06-10",
             "effectiveDate": "2025-06-11", "cashDistributionDate": "2025-06-11", "shareDistributionDate": "2025-06-11"}
    invalid = {**latest, "announcementDate": "2026-08-01", "recordDate": "2026-08-02", "effectiveDate": "2026-08-03",
               "source": {**latest["source"], "ref": "https://www.cninfo.com.cn.example/private"}}
    shanghai = {
        "symbol": "601319.SH", "recordDate": "2026-11-05", "effectiveDate": "2026-11-06",
        "cashGrossPerShare": "0.11", "currency": "CNY",
        "source": {"ref": sse, "name": "上海证券交易所", "asOf": observed},
    }
    for branch, symbol, events in (("szse-distributions", "300502.SZ", [older, latest, invalid]),
                                   ("sse-cash-dividends", "601319.SH", [shanghai])):
        path = tmp_path / "tickdesk-market" / "company-actions" / branch / symbol / "2026-11-16.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"version": 1, "symbol": symbol, "observedAt": observed, "events": events}),
                        encoding="utf-8")

    symbols = ["300502.SZ", "601319.SH", "000001.SZ"]
    store = DataStore.readonly(tmp_path)
    try:
        repo = KlineRepository(store)
        previous, _ = read_market_evidence(repo, observed - 1, symbols)
        rows, quality = read_market_evidence(repo, observed, symbols)
    finally:
        store.db.close()

    assert previous == []
    assert [row["source"]["ref"] for row in rows] == [cninfo, sse]
    assert quality["companyActions"] == {"300502.SZ": "partial", "601319.SH": "partial",
                                         "000001.SZ": "no_distribution_at_cutoff"}
    assert rows[0]["record"] == {
        **{key: value for key, value in latest.items() if key not in {"source", "bonusSharesPerShare"}},
        "eventType": "issuer_distribution", "termsBasis": "provider_reported",
        "revisionId": rows[0]["record"]["revisionId"],
    }
    assert len(rows[0]["record"]["revisionId"]) == 64
    metrics = _metric_catalog({"business": None}, {}, {}, {row["source"]["ref"]: row for row in rows})
    assert [(item["field"], item["value"], item["unit"], item["sourceRef"], item["period"], item["asOf"])
            for item in metrics.values()] == [
        ("cashPerShare", "1", "元/股", cninfo, "2026-06-11", observed),
        ("capitalReserveSharesPerShare", "0.4", "股/股", cninfo, "2026-06-11", observed),
        ("cashGrossPerShare", "0.11", "元/股", sse, "2026-11-06", observed),
    ]
