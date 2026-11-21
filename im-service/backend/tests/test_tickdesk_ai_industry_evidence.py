import asyncio
import json
from datetime import timedelta

import pytest

from app.services.tickdesk_ai import ChatJob, Evidence, chat
from app.services.tickdesk_ai_industry_evidence import read_archived_market_context
from app.services.tickdesk_regime_archive import import_probe, replay
from tests.test_tickdesk_regime_archive import (
    DAY,
    INDUSTRY,
    MARKET,
    MORNING,
    SESSIONS,
    SYMBOL,
    artifact,
    bar,
    members,
    prices,
)


def millis(value):
    return int(value.timestamp() * 1000)


def seen_membership(at, name="通信设备"):
    packet = members(at)
    packet["response"]["data"]["item"][0]["index_list"][0]["name"] = name
    return packet


def daily(code, at, rows=None):
    packet = prices(code, at)
    if rows is not None:
        packet["response"]["data"]["item"][0]["item"] = rows
    return packet


def indexed(rows):
    return {row["record"]["kind"]: row for row in rows}


@pytest.mark.parametrize("model_sources", [0, 20])
def test_manual_report_includes_source_bound_market_context_before_ai(tmp_path, model_sources):
    db = tmp_path / "scope.sqlite3"
    at = MORNING.replace(hour=11, minute=46)
    import_probe(db, *artifact({
        "membership": seen_membership(at),
        "broad": daily(MARKET, at, [bar(SESSIONS[-1], 3979.8)]),
        "industry": daily(INDUSTRY, at, [bar(SESSIONS[-1], 7067.999)]),
    }))
    rows, coverage = read_archived_market_context(db, millis(at), [SYMBOL])
    original_refs = [f"local:document:{number}" for number in range(model_sources)]
    local_rows = [{"collection": "documents", "source": {"ref": ref, "name": "来源", "asOf": millis(at)},
                   "record": {"symbol": SYMBOL}} for ref in original_refs]

    class Model:
        model = "fixture"
        json_output = True

        async def complete(self, _messages, **_kwargs):
            return json.dumps({"text": "分析资料需复核。", "sourceRefs": original_refs})

    job = ChatJob(version=1, jobId="look", requestId="look", deadline=millis(at + timedelta(seconds=30)),
                  text=f"/分析 {SYMBOL}", evidence=Evidence(
                      asOf=millis(at), snapshotSeq=1, serverEpoch="test", items=local_rows,
                  ))
    result = asyncio.run(chat(job, Model(), market=rows, market_coverage=coverage))
    snapshot, interpretation = result["text"].split("\n\nAI 解读\n")
    assert "分析资料需复核" in interpretation
    if model_sources:
        assert result["sourceRefs"] == original_refs
        assert "请求时点市场资料" not in snapshot
        return
    assert "请求时点市场资料" in snapshot and "通信设备" in snapshot and "11:46" in snapshot
    assert f"{SESSIONS[-1]} · 沪深 300 日收盘 3979.8 点" in snapshot
    assert f"{SESSIONS[-1]} · 行业指数日收盘 7067.999 点" in snapshot
    refs = {row["source"]["ref"] for row in rows}
    assert set(result["sourceRefs"]) == refs == {row["source"]["ref"] for row in result["extraEvidence"]}


def test_evening_exact_cutoff_scope_and_revisions_remain_source_grounded(tmp_path):
    db = tmp_path / "scope.sqlite3"
    member_at = MORNING.replace(hour=19, minute=42, second=50, microsecond=915000)
    index_at = member_at + timedelta(milliseconds=51)
    import_probe(
        db,
        *artifact(
            {
                "m": seen_membership(member_at),
                "broad": daily(MARKET, index_at, [bar(DAY, 3979.8)]),
                "sector": daily(INDUSTRY, index_at, [bar(DAY, 7067.999)]),
            }
        ),
    )

    early, coverage = read_archived_market_context(db, millis(MORNING.replace(hour=15)), [SYMBOL])
    assert not early and coverage["status"] == "unavailable"
    member, coverage = read_archived_market_context(db, millis(index_at) - 1, [SYMBOL])
    assert set(indexed(member)) == {"industryMembership"}
    assert coverage["bySymbol"][SYMBOL]["marketIndex"]["state"] == "missing_at_cutoff"

    rows, coverage = read_archived_market_context(db, millis(index_at), [SYMBOL])
    by_kind = indexed(rows)
    assert set(by_kind) == {"industryMembership", "marketIndex", "industryIndex"}
    assert by_kind["industryMembership"]["record"]["industryName"] == "通信设备"
    assert by_kind["marketIndex"]["metrics"]["marketIndexClose"][1:] == [
        "3979.8",
        "点",
        DAY.isoformat(),
    ]
    assert by_kind["industryIndex"]["record"]["indexCode"] == INDUSTRY
    assert by_kind["industryIndex"]["metrics"]["industryIndexClose"][1] == "7067.999"
    assert coverage["status"] == "partial"
    assert coverage["bySymbol"][SYMBOL]["industryIndex"] == {
        "state": "low_coverage",
        "dailyBars": 1,
    }
    for name, row in by_kind.items():
        rec = row["record"]
        assert rec["symbol"] == SYMBOL
        assert (
            len(rec["sourceRevisionId"])
            == len(rec["artifactSha256"])
            == len(rec["providerResponseSha256"])
            == 64
        )
        assert row["source"]["asOf"] == millis(
            member_at if name == "industryMembership" else index_at
        )
        assert row["source"]["ref"].endswith(rec["revisionId"])

    later_at = index_at + timedelta(seconds=1)
    import_probe(db, *artifact({"broad-new-clock": daily(MARKET, later_at, [bar(DAY, 3979.8)])}))
    new, _ = read_archived_market_context(db, millis(later_at), [SYMBOL])
    new_market = indexed(new)["marketIndex"]
    assert new_market["metrics"] == by_kind["marketIndex"]["metrics"]
    assert new_market["source"]["ref"] != by_kind["marketIndex"]["source"]["ref"]
    assert replay(db, SYMBOL, MORNING.replace(hour=15))["ext_market_regime"].value is None


def test_same_clock_conflict_does_not_choose_or_leak_a_source(tmp_path):
    db = tmp_path / "scope.sqlite3"
    at = MORNING.replace(hour=19)
    import_probe(db, *artifact({"one": seen_membership(at)}))
    import_probe(
        db,
        *artifact(
            {
                "conflict": seen_membership(at, "其他行业"),
                "broad": daily(MARKET, at, [bar(DAY, 100)]),
                "sector": daily(INDUSTRY, at, [bar(DAY, 130)]),
            }
        ),
    )
    rows, status = read_archived_market_context(db, millis(at), [SYMBOL, "000001.SZ"])
    assert {row["record"]["kind"] for row in rows} == {"marketIndex"}
    assert status["bySymbol"][SYMBOL]["membership"]["state"] == "source_conflict"
    assert status["bySymbol"]["000001.SZ"]["membership"]["state"] == "missing_at_cutoff"


def test_incomplete_same_session_bar_never_becomes_complete_later_by_waiting(tmp_path):
    db = tmp_path / "scope.sqlite3"
    at = MORNING.replace(hour=14)
    import_probe(
        db,
        *artifact(
            {
                "m": seen_membership(at),
                "broad": daily(MARKET, at, [bar(SESSIONS[-1], 99), bar(DAY, 12345)]),
                "sector": daily(INDUSTRY, at, [bar(DAY, 56789)]),
            }
        ),
    )
    rows, coverage = read_archived_market_context(db, millis(at.replace(hour=20)), [SYMBOL])
    by_kind = indexed(rows)
    assert set(by_kind) == {"industryMembership", "marketIndex"}
    assert by_kind["marketIndex"]["record"]["tradingDate"] == SESSIONS[-1].isoformat()
    assert by_kind["marketIndex"]["metrics"]["marketIndexClose"][1] == "99"
    assert coverage["bySymbol"][SYMBOL]["industryIndex"]["state"] == "no_observed_completed_bar"


def test_malformed_data_and_prior_membership_do_not_claim_current_coverage(tmp_path):
    db = tmp_path / "scope.sqlite3"
    at = MORNING.replace(hour=19)
    fake = bar(DAY, 123)
    fake["high_price"] = -1
    import_probe(
        db,
        *artifact(
            {
                "before": seen_membership(at - timedelta(days=1)),
                "broken": daily(MARKET, at, [fake]),
                "unlinked": daily(INDUSTRY, at, [bar(DAY, 456)]),
            }
        ),
    )
    rows, coverage = read_archived_market_context(db, millis(at), [SYMBOL])
    assert not rows
    assert coverage["bySymbol"][SYMBOL]["membership"]["state"] == "membership_not_observed_today"
    assert coverage["bySymbol"][SYMBOL]["marketIndex"]["state"] == "invalid_daily_series"
    assert coverage["bySymbol"][SYMBOL]["industryIndex"]["state"] == "membership_unavailable"


def test_index_conflict_is_independent_and_submillisecond_receipt_is_never_rounded_back(tmp_path):
    db = tmp_path / "scope.sqlite3"
    at = MORNING.replace(hour=19, microsecond=915001)
    import_probe(
        db,
        *artifact(
            {
                "sector": daily(INDUSTRY, at, [bar(DAY, 130)]),
                "member": seen_membership(at),
                "first": daily(MARKET, at, [bar(DAY, 100)]),
            }
        ),
    )
    import_probe(db, *artifact({"second": daily(MARKET, at, [bar(DAY, 200)])}))
    floor = millis(at)
    early, _ = read_archived_market_context(db, floor, [SYMBOL])
    assert not early
    rows, coverage = read_archived_market_context(db, floor + 1, [SYMBOL])
    assert set(indexed(rows)) == {"industryMembership", "industryIndex"}
    assert coverage["bySymbol"][SYMBOL]["marketIndex"]["state"] == "source_conflict"
    assert all(row["source"]["asOf"] == floor + 1 for row in rows)
    assert all(row["record"]["observedAt"] == at.isoformat() for row in rows)


def test_derived_industry_index_uses_later_of_membership_and_index_in_ai_metric_catalog(tmp_path):
    from app.services.tickdesk_ai import _metric_catalog

    db = tmp_path / "scope.sqlite3"
    at = MORNING.replace(hour=19)
    later = at + timedelta(minutes=1)
    import_probe(
        db,
        *artifact(
            {"sector": daily(INDUSTRY, at, [bar(DAY, 130)]), "member": seen_membership(later)}
        ),
    )
    prior, _ = read_archived_market_context(db, millis(at), [SYMBOL])
    assert not prior
    rows, _ = read_archived_market_context(db, millis(later), [SYMBOL])
    result = indexed(rows)["industryIndex"]
    assert result["record"]["observedAt"] == at.isoformat()
    assert result["record"]["availableAt"] == later.isoformat()
    assert result["record"]["membershipObservedAt"] == later.isoformat()
    metrics = _metric_catalog(
        {"business": None}, {}, {}, {row["source"]["ref"]: row for row in rows}
    )
    assert list(metrics.values()) == [
        {
            "sourceRef": result["source"]["ref"],
            "symbol": SYMBOL,
            "field": "industryIndexClose",
            "label": "同花顺行业指数日收盘",
            "value": "130",
            "unit": "点",
            "asOf": millis(later),
            "period": DAY.isoformat(),
        }
    ]


def test_market_input_builds_ai_context_from_archived_industry_sources(tmp_path):
    from app.services.tickdesk_ai_inputs import read_market_evidence
    from app.tickflow.repository import DataStore, KlineRepository

    at = MORNING.replace(hour=19)
    archive = tmp_path / "tickdesk-market" / "analysis-index-sources.sqlite3"
    archive.parent.mkdir()
    import_probe(archive, *artifact({
        "sector": daily(INDUSTRY, at, [bar(DAY, 130)]),
        "market": daily(MARKET, at, [bar(DAY, 140)]),
        "member": seen_membership(at),
    }))
    store = DataStore.readonly(tmp_path)
    try:
        repo = KlineRepository(store)
        previous, _ = read_market_evidence(repo, millis(at) - 1, [SYMBOL])
        current, quality = read_market_evidence(repo, millis(at), [SYMBOL])
    finally:
        store.db.close()

    assert previous == []
    resolved = indexed(current)
    assert set(resolved) == {"industryMembership", "industryIndex", "marketIndex"}
    assert resolved["industryMembership"]["record"]["industryName"] == "通信设备"
    assert resolved["industryIndex"]["record"]["indexClose"] == "130"
    assert resolved["marketIndex"]["record"]["indexClose"] == "140"
    assert all(row["source"]["asOf"] == millis(at) for row in current)
    assert quality["industryAndIndexes"]["bySymbol"][SYMBOL]["industryIndex"]["state"] == "low_coverage"
