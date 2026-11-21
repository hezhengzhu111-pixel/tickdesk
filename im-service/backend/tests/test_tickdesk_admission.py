import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from tickdesk_quant_fixture import (
    CUTOFF_MS,
    DAY,
    NEXT_DAY,
    STRATEGY_ID,
    SYMBOLS,
    build_fixture,
    install_scan_calendar,
)

from app.api.strategy import router as strategy_router
from app.backtest.engine import BacktestEngine
from app.backtest.evidence import json_digest, load_evidence
from app.backtest.execution_timing import EntryOpenTiming
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService
from app.services.tickdesk_adapter import MarketAdapter, fingerprint, timestamp
from app.services.tickdesk_admission import (
    AdmissionBlockedError,
    strategy_sources,
    validate_approval,
)
from app.services.tickdesk_worker import scan_in_worker
from app.strategy.engine import StrategyEngine


@pytest.fixture
def kernel(tmp_path):
    root = tmp_path / "kernel"
    api = build_fixture(root)
    api.include_router(strategy_router)
    source = root / "strategies" / f"{STRATEGY_ID}.py"
    source.write_text(source.read_text(encoding="utf-8").replace('"research_only": True', '"research_only": False'),
                      encoding="utf-8")
    api.state.strategy_engine = StrategyEngine([source.parent])
    try:
        yield root, api
    finally:
        api.state.repo.store.db.close()


def _draft(engine, version, now):
    definition = engine.get(STRATEGY_ID)
    _, code_hash, dependencies_hash = strategy_sources(definition)
    params = engine.resolve_params(definition, None, {})
    return {
        "version": version, "approvedBy": "test reviewer", "approvedAt": now - 1,
        "evidence": {"causal": "truthy", "outOfSample": "truthy", "shadow": "truthy"},
        "profiles": {"conservative": {"minScore": "0", "minimumAmount": "1"}},
        "strategies": [{
            "id": STRATEGY_ID, "direction": "buy", "weight": "1", "symbols": [SYMBOLS[0]],
            "codeHash": code_hash, "parametersHash": fingerprint({"params": params, "overrides": {}}),
            "dependenciesHash": dependencies_hash,
        }],
    }


def test_legacy_string_approval_cannot_authorize_v3_and_health_matches_production(kernel, monkeypatch):
    root, api = kernel
    install_scan_calendar(monkeypatch.setattr)
    draft = _draft(api.state.strategy_engine, 1, CUTOFF_MS)
    (root / "tickdesk_approvals.json").write_text(json.dumps(draft), encoding="utf-8")
    with TestClient(api, client=("127.0.0.1", 12345)) as client:
        normal = client.get("/api/strategies?asset_type=stock&timeframe=1d").json()
        catalog = client.get("/api/strategies?asset_type=stock&timeframe=1d&tickdesk_admission=true").json()
        production = client.post("/api/tickdesk/prepare", json={
            "data": {"cmd": "scan_now"}, "requestedAt": CUTOFF_MS, "profile": "conservative",
            "quantObservationVersion": 3,
        }).json()
    assert STRATEGY_ID in {item["id"] for item in normal["strategies"]}
    assert "tickdesk_admission_version" not in normal
    assert catalog["tickdesk_admission_version"] == 1
    assert catalog["strategies"] == []
    assert catalog["admission"] == production["strategyAdmission"] == {
        "reason": "STRATEGY_APPROVAL_WITHHELD", "strategies": {STRATEGY_ID: ["V3_EVIDENCE_REQUIRED"]},
    }
    assert production["productionReason"] == "PRODUCTION_EVALUATION_FAILED"
    assert "frozen" not in production
    actual = next(row for row in production["quantObservationBatch"]["observations"] if row["symbol"] == SYMBOLS[0])
    assert actual["admission"] == "research" and actual["entry"]["value"] is True


def test_native_replay_archive_inspection_requires_real_strict_proof(kernel):
    root, api = kernel
    engine = api.state.strategy_engine
    service = StrategyBacktestService(BacktestEngine(api.state.repo), engine)
    run = service.run_with_evidence(StrategyBacktestConfig(
        STRATEGY_ID, [SYMBOLS[0]], DAY, NEXT_DAY, entry_open_timing=EntryOpenTiming((), (), (), 60_000),
    ))
    assert run.error is None and run.trades == []
    report = load_evidence(root, run.run_id)
    now = timestamp(datetime.now(UTC)) + 1000
    draft = _draft(engine, 3, now)
    spec = draft["strategies"][0]
    spec["evidence"] = {
        "causal": {"runId": run.run_id, "sha256": json_digest(report)},
        "outOfSample": "claimed", "shadow": "claimed",
    }
    adapter = MarketAdapter(api.state.repo, engine, calendar=lambda _: True)
    request = {"requestedAt": now, "profile": "conservative"}
    with pytest.raises(AdmissionBlockedError) as caught:
        scan_in_worker(adapter, request, draft, trading=True)
    assert set(caught.value.strategies[STRATEGY_ID]) >= {
        "STRICT_EXECUTION_EVIDENCE_UNAVAILABLE", "PIT_ADMISSION_ARCHIVE_UNAVAILABLE",
        "DISJOINT_OOS_EVIDENCE_UNAVAILABLE", "SHADOW_EVIDENCE_UNAVAILABLE",
    }
    spec["evidence"]["causal"]["sha256"] = "0" * 64
    with pytest.raises(AdmissionBlockedError) as caught:
        scan_in_worker(adapter, request, draft, trading=True)
    assert caught.value.strategies[STRATEGY_ID] == ["CAUSAL_ARCHIVE_HASH_MISMATCH"]
    spec["evidence"]["causal"]["runId"] = "../outside"
    with pytest.raises(AdmissionBlockedError) as caught:
        scan_in_worker(adapter, request, draft, trading=True)
    assert caught.value.strategies[STRATEGY_ID] == ["CAUSAL_ARCHIVE_UNREADABLE"]


def test_v3_accepts_a_definition_only_after_all_validators_return_once(kernel, monkeypatch):
    kernel_root, api = kernel
    definition = api.state.strategy_engine.get(STRATEGY_ID)
    now = timestamp(datetime.now(UTC))
    draft = _draft(api.state.strategy_engine, 3, now)
    # Exercise the approval dispatcher independently from the evidence verifiers.
    inspected = []

    def approved(*args):
        inspected.append(args)
        return []

    monkeypatch.setattr("app.services.tickdesk_admission._v3_blockers", approved)
    selected = validate_approval(api.state.strategy_engine, kernel_root, draft, {}, requested_at=now)
    assert len(inspected) == 1
    assert selected[STRATEGY_ID][0] is definition
