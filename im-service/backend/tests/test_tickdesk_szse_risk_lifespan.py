import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.services import tickdesk_market_data, tickdesk_szse_risk_worker
from tests.test_tickdesk_regime_lifespan import OPEN, workspace

DAY = OPEN.date().isoformat()


def sealed(path, started, available):
    path.mkdir()
    manifest = {
        "exchangeDate": available.date().isoformat(),
        "collectionStartedAt": started.isoformat(),
        "availableAt": available.isoformat(),
        "availableAtMs": int(available.timestamp() * 1000),
        "workbookRows": 105,
        "activeSecurityCount": 2905,
    }
    (path / "capture-evidence.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def forbidden(*args):
    pytest.fail("unexpected exchange request")


@pytest.mark.parametrize("hour", [8, 12, 15, 22])
def test_no_public_request_outside_market_or_when_disabled(tmp_path, hour):
    repo, _, _ = workspace(tmp_path)
    cfg = tickdesk_szse_risk_worker.SzseRiskSnapshots(
        repo.store.data_dir, enabled=True, capture=forbidden
    )
    at = OPEN.replace(hour=hour)
    expected = {"outside_market_window", "calendar_unknown"}
    assert cfg.step(clock=lambda: at)["reason"] in expected
    disabled = tickdesk_szse_risk_worker.SzseRiskSnapshots(
        repo.store.data_dir, enabled=False, capture=forbidden
    )
    assert disabled.step(clock=lambda: OPEN)["status"] == "disabled"
    assert not cfg.archive.exists()


@pytest.mark.parametrize(("ready", "source"), [(False, "fuyao"), (True, "tickflow")])
def test_calendar_must_confirm_scope_and_current_day(tmp_path, ready, source):
    repo, _, _ = workspace(tmp_path, ready=ready, source=source)
    runner = tickdesk_szse_risk_worker.SzseRiskSnapshots(
        repo.store.data_dir, enabled=True, capture=forbidden
    )
    assert runner.step(clock=lambda: OPEN)["reason"] == "calendar_unknown"
    assert not runner.archive.exists()


def test_success_promotes_original_manifest_and_restart_makes_no_requests(tmp_path):
    repo, _, _ = workspace(tmp_path)
    now = [OPEN]
    paths = []

    def capture(path):
        paths.append(path)
        now[0] += timedelta(milliseconds=250)
        return sealed(path, OPEN, now[0])

    runner = tickdesk_szse_risk_worker.SzseRiskSnapshots(
        repo.store.data_dir, enabled=True, capture=capture
    )
    ready = runner.step(clock=lambda: now[0])
    assert ready["status"] == "ready" and ready["workbookRows"] == 105
    assert len(paths) == 1 and paths[0] != runner.archive / DAY
    on_disk = json.loads((runner.archive / DAY / "capture-evidence.json").read_bytes())
    assert ready["availableAt"] == on_disk["availableAt"] == now[0].isoformat()
    fresh = tickdesk_szse_risk_worker.SzseRiskSnapshots(
        repo.store.data_dir, enabled=True, capture=forbidden
    )
    state = fresh.step(clock=lambda: now[0])
    assert state["status"] == "deduplicated" and state["priorState"] == "ready"
    after = fresh.step(clock=lambda: now[0].replace(hour=16))
    assert after["status"] == "skipped" and after["priorState"] == "ready"


def test_late_original_receipt_is_kept_out_of_decision_directory(tmp_path):
    repo, _, _ = workspace(tmp_path)
    start = OPEN.replace(hour=14, minute=59, second=59)
    observed = start + timedelta(seconds=2)
    now = [start]

    def capture(path):
        now[0] = observed
        return sealed(path, start, observed)

    runner = tickdesk_szse_risk_worker.SzseRiskSnapshots(
        repo.store.data_dir, enabled=True, capture=capture
    )
    late = runner.step(clock=lambda: now[0])
    assert late["status"] == "late" and late["availableAt"] == observed.isoformat()
    assert not (runner.archive / DAY).exists()
    stash = runner.archive / ".attempts" / DAY / "late-snapshot" / "capture-evidence.json"
    assert json.loads(stash.read_bytes())["availableAt"] == observed.isoformat()
    previous = runner.step(clock=lambda: observed)
    assert previous["priorState"] == "late"


def test_failure_and_interrupted_claim_cannot_retry_same_session(tmp_path):
    repo, _, _ = workspace(tmp_path)
    in_progress, release = threading.Event(), threading.Event()

    def capture(path):
        in_progress.set()
        assert release.wait(3)
        raise TimeoutError("upstream detailed internal message")

    runner = tickdesk_szse_risk_worker.SzseRiskSnapshots(
        repo.store.data_dir, enabled=True, capture=capture
    )
    restarted = tickdesk_szse_risk_worker.SzseRiskSnapshots(
        repo.store.data_dir, enabled=True, capture=forbidden
    )
    with ThreadPoolExecutor(max_workers=1) as work:
        pending = work.submit(runner.step, clock=lambda: OPEN)
        try:
            assert in_progress.wait(2)
            skipped = restarted.step(clock=lambda: OPEN)
            assert (
                skipped["status"] == "deduplicated"
                and skipped["priorState"] == "in_progress_or_interrupted"
            )
        finally:
            release.set()
        failed = pending.result(3)
    assert failed["status"] == "failed" and "upstream detailed" not in json.dumps(failed)
    restart = restarted.step(clock=lambda: OPEN)
    assert restart["priorState"] == "failed" and not (runner.archive / DAY).exists()


def test_market_lifespan_drains_dossier_writer_independently(tmp_path, monkeypatch):
    repo, _, _ = workspace(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def fake_step(self):
        entered.set()
        release.wait(3)
        return {"status": "failed"}

    async def idle(*args):
        await asyncio.Event().wait()

    monkeypatch.setenv("TICKDESK_QUANT_DATA_ENABLED", "1")
    monkeypatch.delenv("TICKDESK_SZSE_ST_CAPTURE_ENABLED", raising=False)
    monkeypatch.setattr(tickdesk_szse_risk_worker.SzseRiskSnapshots, "step", fake_step)
    for name in ("_run", "_run_chart_preparation", "_run_master", "_run_regime_collection"):
        monkeypatch.setattr(tickdesk_market_data, name, idle)
    monkeypatch.setattr("app.services.tickdesk_daily_history.run_daily_history", idle)
    monkeypatch.setattr("app.services.tickdesk_quote_feed.QuoteFeed.run", idle)

    async def scenario():
        app = SimpleNamespace(state=SimpleNamespace(repo=repo))
        lifecycle = tickdesk_market_data.market_data_lifespan(app)
        await lifecycle.__aenter__()
        assert await asyncio.to_thread(entered.wait, 1)
        assert app.state.tickdesk_szse_risk["status"] == "starting"
        assert app.state.tickdesk_chart_preparation.request("300502.SZ") == "CHART_PREPARING"
        closing = asyncio.create_task(lifecycle.__aexit__(None, None, None))
        try:
            await asyncio.sleep(0.02)
            assert not closing.done()
        finally:
            release.set()
            await closing

    asyncio.run(scenario())


def test_invalid_clock_cannot_promote_dossier_or_clear_claim(tmp_path):
    repo, _, _ = workspace(tmp_path)

    def capture(path):
        return sealed(path, OPEN, OPEN - timedelta(seconds=1))

    runner = tickdesk_szse_risk_worker.SzseRiskSnapshots(
        repo.store.data_dir, enabled=True, capture=capture
    )
    failed = runner.step(clock=lambda: OPEN)
    assert failed["status"] == "failed" and failed["errorKind"] == "ValueError"
    assert not (runner.archive / DAY).exists()
    restart = tickdesk_szse_risk_worker.SzseRiskSnapshots(
        repo.store.data_dir, enabled=True, capture=forbidden
    )
    assert restart.step(clock=lambda: OPEN)["priorState"] == "failed"


def test_status_api_exposes_snapshot_stage_only_after_authorization(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.tickdesk import router

    credential = tmp_path / "token"
    credential.write_text("a" * 43)
    monkeypatch.setenv("TICKDESK_AI_TOKEN_FILE", str(credential))
    app = FastAPI()
    app.include_router(router)
    app.state.tickdesk_szse_risk = {
        "status": "late",
        "reason": "decision_cutoff_elapsed",
        "date": DAY,
    }
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        assert client.get("/api/tickdesk/ai/v1/status").status_code == 401
        accepted = client.get(
            "/api/tickdesk/ai/v1/status", headers={"Authorization": "Bearer " + "a" * 43}
        )
        assert accepted.status_code == 200
        assert accepted.json()["szseRiskSnapshot"] == app.state.tickdesk_szse_risk
