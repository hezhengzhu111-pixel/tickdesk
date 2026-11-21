import hashlib
import json
import sqlite3
import time as clock
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import polars as pl
import pytest
from fastapi.testclient import TestClient

from app.services.tickdesk_regime_archive import (
    INDEX_ARCHIVE,
    import_calendar,
    import_probe,
    lookup,
    replay,
)
from app.services.tickdesk_regime_evidence import INDUSTRY_FIELD, MARKET_FIELD
from app.strategy.quant_panel import _with_pit_values

CN = ZoneInfo("Asia/Shanghai")
DAY = date(2025, 2, 18)
MORNING = datetime.combine(DAY, time(9), CN)
SYMBOL = "300502.SZ"
MARKET = "000300.SH"
INDUSTRY = "881129.TI"
SESSIONS = [
    DAY - timedelta(days=distance)
    for distance in range(35, 0, -1)
    if (DAY - timedelta(days=distance)).weekday() < 5
][-20:]


def bar(day, close):
    return {
        "date_ms": int(datetime.combine(day, time.min, CN).timestamp() * 1000),
        "open_price": close,
        "low_price": close,
        "high_price": close,
        "close_price": close,
        "volume": 10,
        "turnover": 100,
    }


def packet(path, params, data, at, char):
    return {
        "requestPath": path,
        "parameters": params,
        "httpStatus": 200,
        "responseSha256": char * 64,
        "requestedAt": (at - timedelta(milliseconds=10)).isoformat(),
        "receivedAt": at.isoformat(),
        "response": {"code": 0, "data": data},
    }


def prices(code, at, close=101.125, *, sessions=SESSIONS):
    rows = [bar(day, close if day == sessions[-1] else 100) for day in sessions]
    return packet(
        "/api/a-share-index/prices/historical-batch",
        {
            "thscodes": code,
            "interval": "1d",
            "start": rows[0]["date_ms"],
            "end": rows[-1]["date_ms"],
        },
        {"interval": "1d", "adjust": None, "item": [{"thscode": code, "item": rows}]},
        at,
        "a",
    )


def members(at, *, symbol=SYMBOL):
    return packet(
        "/api/a-share-index/constituents/ths-index-list-by-stock",
        {"thscode": symbol, "type": "industry"},
        {"item": [{"thscode": symbol, "index_list": [{"thscode": INDUSTRY, "type": "industry"}]}]},
        at,
        "b",
    )


def artifact(data):
    body = json.dumps(data).encode()
    return body, hashlib.sha256(body).hexdigest()


def test_append_and_query_exact_observation_boundary_without_rewriting_bytes(tmp_path):
    db = tmp_path / "archive.sqlite3"
    captured = artifact({"genuine-capture-shape": prices(MARKET, MORNING)})
    assert import_probe(db, *captured) == 1
    assert import_probe(db, *captured) == 0
    assert lookup(db, "indices", MARKET, MORNING - timedelta(milliseconds=1)).state == "missing"
    selected = lookup(db, "indices", MARKET, MORNING.astimezone(UTC))
    assert selected.state == "known"
    assert selected.record.body == captured[0]
    assert selected.record.observation.observed_at == MORNING
    assert selected.record.ingested_at > MORNING
    assert selected.record.provider_sha256 == "a" * 64
    value = selected.record.observation.data["item"][0]["item"][-1]["close_price"]
    assert value == Decimal("101.125")
    assert lookup(db, "membership", SYMBOL, MORNING).state == "missing"


def test_default_strategy_reads_only_the_frozen_archived_market_and_industry_facts(tmp_path):
    from test_quant_data_quality import repo

    repository = repo(tmp_path)
    archive = tmp_path / INDEX_ARCHIVE
    archive.parent.mkdir()
    fields = {MARKET_FIELD, INDUSTRY_FIELD}
    close = MORNING.replace(hour=15)
    import_calendar(archive, *artifact({"source": "fuyao", "through": DAY.isoformat(),
                                        "observedAt": int(MORNING.timestamp() * 1000),
                                        "sessions": [str(day) for day in [*SESSIONS, DAY]]}))
    import_probe(archive, *artifact({"market": prices(MARKET, MORNING)}))
    frozen = repository.get_quant_evidence_snapshot(fields)
    industry_at = close + timedelta(milliseconds=1)
    import_probe(archive, *artifact({"member": members(MORNING + timedelta(minutes=5)),
                                     "industry": prices(INDUSTRY, industry_at, 99.75)}))

    close_evidence = repository.get_quant_input_evidence(close, [SYMBOL], fields)
    assert close_evidence.fact(SYMBOL, MARKET_FIELD).value == 1
    assert close_evidence.fact(SYMBOL, INDUSTRY_FIELD).reason == "index_observation_unavailable"
    after = repository.get_quant_input_evidence(industry_at, [SYMBOL], fields)
    assert after.fact(SYMBOL, INDUSTRY_FIELD).value == 0
    assert after.fact(SYMBOL, INDUSTRY_FIELD).available_at == industry_at.isoformat()
    original = repository.get_quant_input_evidence(industry_at, [SYMBOL], fields, snapshot=frozen)
    assert original.fact(SYMBOL, INDUSTRY_FIELD).reason == "membership_unknown"
    assert original.identity != after.identity
    arbitrary = pl.DataFrame({"date": [DAY], "symbol": [SYMBOL], MARKET_FIELD: [9], INDUSTRY_FIELD: [9]})
    result = _with_pit_values(arbitrary, {str(DAY): close_evidence}, fields)
    assert result[MARKET_FIELD].item() == 1 and result[INDUSTRY_FIELD].item() is None


@pytest.mark.parametrize("late_industry", [False, True], ids=["before-close", "after-close"])
def test_manual_ai_report_displays_the_regime_facts_that_drove_entry(monkeypatch, tmp_path, late_industry):
    from tickdesk_quant_fixture import CUTOFF_MS, STRATEGY, STRATEGY_ID, SYMBOLS, build_fixture
    from tickdesk_quant_fixture import DAY as REPORT_DAY

    from app.strategy.engine import StrategyEngine

    token = "s" * 43
    token_file = tmp_path / "token"
    token_file.write_text(token, encoding="utf-8")
    monkeypatch.setenv("TICKDESK_AI_TOKEN_FILE", str(token_file))
    root = tmp_path / "fixture"
    api = build_fixture(root)
    strategy = STRATEGY.replace('["roe_latest"]', f'["{MARKET_FIELD}", "{INDUSTRY_FIELD}"]')
    strategy = strategy.replace('"roe_latest", "raw_close"', f'"{MARKET_FIELD}", "{INDUSTRY_FIELD}", "raw_close"')
    strategy = strategy.replace('market.field("roe_latest") > 0',
                                f'(market.field("{MARKET_FIELD}") == 1) & (market.field("{INDUSTRY_FIELD}") == 0)')
    (root / "strategies" / f"{STRATEGY_ID}.py").write_text(strategy, encoding="utf-8")
    api.state.strategy_engine = StrategyEngine([root / "strategies"])
    archive = root / INDEX_ARCHIVE
    archive.parent.mkdir()
    symbol = SYMBOLS[-1]
    receipt = datetime.combine(REPORT_DAY, time(9), CN)
    close = datetime.combine(REPORT_DAY, time(15), CN)
    industry_receipt = close + timedelta(milliseconds=1) if late_industry else receipt
    previous_days = [REPORT_DAY - timedelta(days=distance) for distance in range(40, 0, -1)]
    sessions = [day for day in previous_days if day.weekday() < 5 and day != date(2026, 1, 1)][-20:]
    import_calendar(archive, *artifact({"source": "fuyao", "through": str(REPORT_DAY),
                                        "observedAt": int(receipt.timestamp() * 1000),
                                        "sessions": [str(day) for day in [*sessions, REPORT_DAY]]}))
    import_probe(archive, *artifact({
        "market": prices(MARKET, receipt, sessions=sessions),
        "member": members(receipt, symbol=symbol),
        "industry": prices(INDUSTRY, industry_receipt, 99.75, sessions=sessions),
    }))
    captured = []

    class Model:
        model = "fixture"
        json_output = True

        async def complete(self, messages, **kwargs):
            inputs = json.loads(messages[1]["content"])
            captured.append(inputs)
            return json.dumps({
                "text": "指数条件已纳入入场判断。", "metricRefs": [], "sourceRefs": inputs["quantRefs"],
            })

    api.state.tickdesk_chat_model = Model()
    try:
        with TestClient(api, client=("127.0.0.1", 5000)) as client:
            response = client.post("/api/tickdesk/ai/v1/chat", headers={"Authorization": f"Bearer {token}"}, json={
                "version": 1, "jobId": "regime", "requestId": "regime",
                "deadline": clock.time_ns() // 1_000_000 + 30_000, "text": f"/分析 {symbol}",
                "evidence": {"items": [], "asOf": CUTOFF_MS, "snapshotSeq": 7, "serverEpoch": "epoch1"},
            })
    finally:
        api.state.repo.store.db.close()
    assert response.status_code == 200, response.json()
    result = response.json()["result"]
    [row] = captured[0]["context"]["quant"]
    assert row["entry"]["value"] is (None if late_industry else True)
    expected = replay(archive, symbol, close)
    for field, value in ((MARKET_FIELD, 1), (INDUSTRY_FIELD, 0)):
        if late_industry and field == INDUSTRY_FIELD:
            assert row["evidence"]["regime"][field] == {"value": None, "reason": "index_observation_unavailable"}
            continue
        assert row["evidence"]["regime"][field] == {
            "value": value, "availableAtMs": int(receipt.timestamp() * 1000),
            "ruleId": expected[field].rule_id, "sha256": expected[field].evidence_hash,
        }
    assert result["artifact"]["report"]["quantOriginal"] == [row]
    assert "沪深 300前一收盘高于" in result["text"]
    if late_industry:
        assert "待补量化资料 · 行业状态" in result["text"]
        assert "所属行业指数前一收盘未高于" not in result["text"]
    else:
        assert "所属行业指数前一收盘未高于" in result["text"]
    [broad] = [item for item in captured[0]["context"]["marketEvidence"] if item["record"].get("kind") == "marketIndex"]
    assert result["sourceRefs"] == [*captured[0]["quantRefs"], broad["source"]["ref"]]
    assert "请求时点市场资料" in result["text"]


def test_replay_market_ready_while_industry_unknown_then_joins_independent_prices(tmp_path):
    db = tmp_path / "archive.sqlite3"
    import_calendar(
        db,
        *artifact(
            {
                "source": "fuyao",
                "through": DAY.isoformat(),
                "observedAt": int(MORNING.timestamp() * 1000),
                "sessions": [d.isoformat() for d in [*SESSIONS, DAY]],
            }
        ),
    )
    import_probe(db, *artifact({"market": prices(MARKET, MORNING)}))
    membership_at = MORNING + timedelta(minutes=5)
    import_probe(db, *artifact({"members": members(membership_at)}))
    industry_at = MORNING + timedelta(minutes=10)
    import_probe(db, *artifact({"industry": prices(INDUSTRY, industry_at, 99.75)}))
    early = replay(db, SYMBOL, MORNING)
    assert early[MARKET_FIELD].value == 1
    assert early[INDUSTRY_FIELD].reason == "membership_unknown"
    mid = replay(db, SYMBOL, industry_at - timedelta(milliseconds=1))
    assert mid[MARKET_FIELD].value == 1
    assert mid[INDUSTRY_FIELD].reason == "index_observation_unavailable"
    late = replay(db, SYMBOL, industry_at)
    assert late[MARKET_FIELD] == early[MARKET_FIELD]
    assert late[INDUSTRY_FIELD].value == 0
    assert late[INDUSTRY_FIELD].available_at == industry_at
    assert all(
        fact.value is None for fact in replay(db, SYMBOL, MORNING - timedelta(days=1)).values()
    )


def test_append_guards_digest_and_same_clock_conflicts_are_local(tmp_path):
    db = tmp_path / "archive.sqlite3"
    body, digest = artifact({"market": prices(MARKET, MORNING)})
    with pytest.raises(ValueError, match="hash"):
        import_probe(db, body + b" ", digest)
    assert not db.exists()
    import_probe(db, body, digest)
    import_probe(
        db, *artifact({"another-market": prices(MARKET, MORNING, 99.125), "m": members(MORNING)})
    )
    assert lookup(db, "indices", MARKET, MORNING).state == "conflict"
    assert lookup(db, "membership", SYMBOL, MORNING).state == "known"
    with sqlite3.connect(db) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE artifact SET body = ?", (b"forged",))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "INSERT OR REPLACE INTO artifact SELECT digest, ?, ingested_at FROM artifact LIMIT 1",
                (b"forged",),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM source")


def test_malformed_capture_and_read_missing_file_leave_no_state(tmp_path):
    db = tmp_path / "archive.sqlite3"
    assert lookup(db, "indices", MARKET, MORNING).state == "missing"
    assert not db.exists()
    assert all(fact.value is None for fact in replay(db, SYMBOL, MORNING).values())
    invalid = prices(MARKET, MORNING)
    invalid["requestedAt"] = (MORNING + timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError, match="clock"):
        import_probe(db, *artifact({"valid": members(MORNING), "invalid": invalid}))
    assert not db.exists()


def test_newer_matching_scope_with_missing_group_does_not_fall_back(tmp_path):
    db = tmp_path / "archive.sqlite3"
    import_probe(db, *artifact({"market": prices(MARKET, MORNING)}))
    later = MORNING + timedelta(minutes=1)
    empty = prices(MARKET, later)
    empty["response"]["data"]["item"] = []
    import_probe(db, *artifact({"market": empty}))
    selected = lookup(db, "indices", MARKET, later)
    assert selected.state == "known"
    assert selected.record.observation.data["item"] == []
    assert lookup(db, "indices", MARKET, MORNING).record.observation.data["item"]


def test_concurrent_identical_imports_are_idempotent(tmp_path):
    db = tmp_path / "archive.sqlite3"
    captured = artifact({"m": members(MORNING), "i": prices(MARKET, MORNING)})
    with ThreadPoolExecutor(max_workers=2) as threads:
        attempts = [threads.submit(import_probe, db, *captured) for _ in range(2)]
        assert sorted(attempt.result() for attempt in attempts) == [0, 2]
    assert lookup(db, "membership", SYMBOL, MORNING).state == "known"
    assert lookup(db, "indices", MARKET, MORNING).state == "known"


def test_a_different_database_is_rejected_without_modification(tmp_path):
    db = tmp_path / "workbench.sqlite3"
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("CREATE TABLE existing_table (sample TEXT)")
        conn.execute("INSERT INTO existing_table VALUES ('preserve this')")
    before = db.read_bytes()
    with pytest.raises(ValueError, match="not a research regime archive"):
        import_probe(db, *artifact({"market": prices(MARKET, MORNING)}))
    with pytest.raises(ValueError, match="not a research regime archive"):
        lookup(db, "indices", MARKET, MORNING)
    assert db.read_bytes() == before
