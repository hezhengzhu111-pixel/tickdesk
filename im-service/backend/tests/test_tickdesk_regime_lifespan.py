import asyncio
import hashlib
import json
import sqlite3
import threading
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.services import tickdesk_market_data, tickdesk_regime_worker
from app.services.tickdesk_regime_archive import import_probe, lookup
from tests.test_tickdesk_regime_archive import (
    DAY,
    MARKET,
    MORNING,
    SESSIONS,
    SYMBOL,
    artifact,
    prices,
)

OPEN = MORNING.replace(hour=10)


def workspace(tmp_path, *, ready=True, source="fuyao"):
    root = tmp_path / "market"
    root.mkdir()
    directory = root / "tickdesk-market"
    directory.mkdir()
    sessions = [*SESSIONS, DAY] if ready else SESSIONS
    calendar = {
        "source": source,
        "observedAt": int(MORNING.timestamp() * 1_000),
        "through": sessions[-1].isoformat(),
        "sessions": [str(day) for day in sessions],
    }
    body = (json.dumps(calendar, indent=2) + "\n").encode()
    (directory / "calendar.json").write_bytes(body)
    archive = directory / "analysis-index-sources.sqlite3"
    return SimpleNamespace(store=SimpleNamespace(data_dir=root)), archive, body


@pytest.mark.parametrize("has_archive", [False, True])
def test_disabled_worker_locally_syncs_real_shape_without_calling_supplier(tmp_path, monkeypatch, has_archive):
    repo, archive, body = workspace(tmp_path)
    if has_archive:
        import_probe(archive, *artifact({"existing": prices(MARKET, OPEN)}))
    baseline = lookup(archive, "indices", MARKET, OPEN)
    worker = tickdesk_regime_worker.RegimeCollection(
        repo, lambda: {SYMBOL}, enabled=False, symbol=SYMBOL
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("disabled worker called collection")

    monkeypatch.setattr(tickdesk_regime_worker, "collect_once", forbidden)
    status = worker.step(clock=lambda: OPEN)
    assert status["status"] == "disabled" and status["calendar"]["status"] == "archived"
    got = lookup(archive, "calendar", "fuyao", OPEN)
    assert got.record.body == body and got.record.observation.observed_at == MORNING
    assert (
        lookup(archive, "calendar", "fuyao", MORNING - timedelta(milliseconds=1)).state == "missing"
    )
    assert lookup(archive, "indices", MARKET, OPEN) == baseline


@pytest.mark.parametrize(("hour", "minute"), [(8, 59), (11, 30), (12, 30), (15, 0), (20, 5)])
def test_closed_windows_do_not_call_collector(tmp_path, monkeypatch, hour, minute):
    repo, _, _ = workspace(tmp_path)
    at = OPEN.replace(hour=hour, minute=minute)
    if at < MORNING:
        # Preserve a genuinely visible same-day calendar for the pre-open check.
        path = repo.store.data_dir / "tickdesk-market" / "calendar.json"
        obj = json.loads(path.read_bytes())
        obj["observedAt"] = int((at - timedelta(minutes=1)).timestamp() * 1000)
        path.write_text(json.dumps(obj), encoding="utf-8")
    worker = tickdesk_regime_worker.RegimeCollection(
        repo, lambda: {SYMBOL}, enabled=True, symbol=SYMBOL
    )
    monkeypatch.setattr(
        tickdesk_regime_worker, "collect_once", lambda *a, **k: pytest.fail("outside window")
    )
    assert worker.step(clock=lambda: at)["reason"] == "outside_market_window"


def test_enabled_collection_requires_actual_scope_and_stock_master(tmp_path, monkeypatch):
    repo, archive, _ = workspace(tmp_path)
    calls = []
    scopes = set()
    worker = tickdesk_regime_worker.RegimeCollection(
        repo, lambda: scopes, enabled=True, symbol=SYMBOL
    )
    catalog = []
    monkeypatch.setattr(
        tickdesk_regime_worker.MarketAdapter, "symbols", lambda _adapter, _now: list(catalog)
    )

    def collect(path, symbol, **kwargs):
        assert lookup(path, "calendar", "fuyao", OPEN).state == "known"
        assert kwargs["window_allowed"](OPEN)
        assert not kwargs["window_allowed"](OPEN.replace(hour=12))
        calls.append((path, symbol))
        return {
            "state": "partial",
            "reason": "sources_incomplete",
            "requests": 2,
            "archivedResponses": 1,
        }

    monkeypatch.setattr(tickdesk_regime_worker, "collect_once", collect)
    assert worker.step(clock=lambda: OPEN)["reason"] == "symbol_outside_workspace_scope"
    scopes.add(SYMBOL)
    catalog.append(
        {"symbol": SYMBOL, "instrument": "etf", "source": {"asOf": 1}, "supported": True}
    )
    assert worker.step(clock=lambda: OPEN)["reason"] == "stock_identity_unavailable"
    catalog[0]["instrument"] = "stock"
    status = worker.step(clock=lambda: OPEN)
    assert status["status"] == "partial" and status["archivedResponses"] == 1
    assert calls == [(archive, SYMBOL)]


def test_static_research_scope_uses_single_explicit_symbol(tmp_path, monkeypatch):
    repo, archive, _ = workspace(tmp_path)
    (repo.store.data_dir / "tickdesk_research.json").write_text(
        json.dumps(
            {
                "version": 3,
                "strategies": ["example"],
                "symbols": [SYMBOL, "600000.SH"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TICKDESK_REGIME_COLLECTION_ENABLED", "1")
    monkeypatch.setenv("TICKDESK_REGIME_COLLECTION_SYMBOL", f"{SYMBOL},600000.SH")
    invalid = tickdesk_regime_worker.RegimeCollection(repo, set)
    assert invalid.step(clock=lambda: OPEN)["reason"] == "invalid_symbol_configuration"
    monkeypatch.setenv("TICKDESK_REGIME_COLLECTION_SYMBOL", SYMBOL)
    worker = tickdesk_regime_worker.RegimeCollection(repo, set)
    monkeypatch.setattr(
        tickdesk_regime_worker.MarketAdapter,
        "symbols",
        lambda *_: [
            {"symbol": s, "supported": True, "instrument": "stock", "source": {"asOf": 1}}
            for s in (SYMBOL, "600000.SH")
        ],
    )
    calls = []

    def collect(path, symbol, **kwargs):
        calls.append((path, symbol))
        return {"state": "ready", "requests": 2, "archivedResponses": 2}

    monkeypatch.setattr(tickdesk_regime_worker, "collect_once", collect)
    assert worker.step(clock=lambda: OPEN)["status"] == "ready"
    assert calls == [(archive, SYMBOL)]


@pytest.mark.parametrize(("ready", "source"), [(False, "fuyao"), (True, "tickflow")])
def test_unverified_calendar_does_not_open_window(tmp_path, monkeypatch, ready, source):
    repo, _, _ = workspace(tmp_path, ready=ready, source=source)
    worker = tickdesk_regime_worker.RegimeCollection(
        repo, lambda: {SYMBOL}, enabled=True, symbol=SYMBOL
    )
    monkeypatch.setattr(
        tickdesk_regime_worker, "collect_once", lambda *a, **k: pytest.fail("unknown calendar")
    )
    status = worker.step(clock=lambda: OPEN)
    assert status["status"] == "skipped" and status["reason"] in {
        "calendar_unknown",
        "calendar_source_unverified",
    }


def test_lifespan_keeps_ordinary_market_responsive_and_drains_regime_publication(
    tmp_path, monkeypatch
):
    repo, _, _ = workspace(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def step(self):
        entered.set()
        release.wait(timeout=5)
        return {"status": "ready"}

    async def idle(*args):
        await asyncio.Event().wait()

    monkeypatch.setenv("TICKDESK_QUANT_DATA_ENABLED", "1")
    monkeypatch.setattr(tickdesk_regime_worker.RegimeCollection, "step", step)
    monkeypatch.setattr(tickdesk_market_data, "_run", idle)
    monkeypatch.setattr(tickdesk_market_data, "_run_chart_preparation", idle)
    monkeypatch.setattr(tickdesk_market_data, "_run_master", idle)
    monkeypatch.setattr("app.services.tickdesk_daily_history.run_daily_history", idle)
    monkeypatch.setattr("app.services.tickdesk_quote_feed.QuoteFeed.run", idle)

    async def scenario():
        app = SimpleNamespace(state=SimpleNamespace(repo=repo))
        lifecycle = tickdesk_market_data.market_data_lifespan(app)
        await lifecycle.__aenter__()
        assert await asyncio.to_thread(entered.wait, 1)
        assert app.state.tickdesk_chart_preparation.request(SYMBOL) == "CHART_PREPARING"
        closing = asyncio.create_task(lifecycle.__aexit__(None, None, None))
        try:
            await asyncio.sleep(0.02)
            assert not closing.done()
        finally:
            release.set()
            await closing

    asyncio.run(scenario())


def test_expired_calendar_disables_collection_without_pruning_history(tmp_path, monkeypatch):
    repo, archive, _ = workspace(tmp_path)
    import_probe(archive, *artifact({"then": prices(MARKET, OPEN)}))
    worker = tickdesk_regime_worker.RegimeCollection(
        repo, lambda: {SYMBOL}, enabled=False, symbol=SYMBOL
    )
    assert worker.step(clock=lambda: OPEN)["calendar"]["status"] == "archived"
    then = lookup(archive, "calendar", "fuyao", OPEN)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    monkeypatch.setattr(
        tickdesk_regime_worker, "collect_once", lambda *a, **k: pytest.fail("calendar stale")
    )
    restarted = tickdesk_regime_worker.RegimeCollection(
        repo, lambda: {SYMBOL}, enabled=True, symbol=SYMBOL
    )
    beyond_ttl = MORNING + timedelta(days=3, milliseconds=1)
    assert restarted.step(clock=lambda: beyond_ttl)["reason"] == "calendar_unknown"
    assert lookup(archive, "calendar", "fuyao", OPEN) == then
    assert lookup(archive, "indices", MARKET, OPEN).state == "known"
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == digest


def test_lifespan_without_repository_does_not_construct_regime_collector(monkeypatch):
    async def idle(*args):
        await asyncio.Event().wait()

    monkeypatch.setenv("TICKDESK_QUANT_DATA_ENABLED", "1")
    monkeypatch.setenv("TICKDESK_REGIME_COLLECTION_ENABLED", "1")
    monkeypatch.setattr(
        tickdesk_regime_worker, "RegimeCollection", lambda *_: pytest.fail("no repository")
    )
    monkeypatch.setattr(tickdesk_market_data, "_run", idle)
    monkeypatch.setattr(tickdesk_market_data, "_run_chart_preparation", idle)
    monkeypatch.setattr(tickdesk_market_data, "_run_master", idle)
    monkeypatch.setattr("app.services.tickdesk_daily_history.run_daily_history", idle)

    async def scenario():
        app = SimpleNamespace(state=SimpleNamespace(repo=None))
        async with tickdesk_market_data.market_data_lifespan(app):
            assert not hasattr(app.state, "tickdesk_regime_collection")
            assert hasattr(app.state, "tickdesk_chart_preparation")

    asyncio.run(scenario())


def test_authorized_status_keeps_market_and_regime_visible_on_facts_archive_failure(
    tmp_path, monkeypatch
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.tickdesk import router

    def broken_budget(**kwargs):
        raise sqlite3.OperationalError("separate facts state not available")

    credential = tmp_path / "token"
    credential.write_text("a" * 43)
    monkeypatch.setenv("TICKDESK_AI_TOKEN_FILE", str(credential))
    app = FastAPI()
    app.include_router(router)
    app.state.tickdesk_facts = SimpleNamespace(
        configuration="active", budget_diagnostics=broken_budget
    )
    app.state.tickdesk_market_data = {"status": "ready"}
    app.state.tickdesk_regime_collection = {
        "status": "disabled",
        "calendar": {"status": "archived"},
        "requests": 0,
    }
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        response = client.get(
            "/api/tickdesk/ai/v1/status", headers={"Authorization": "Bearer " + "a" * 43}
        )
        assert response.status_code == 200
    assert response.json()["factBudget"] is None
    assert response.json()["quantData"] == {"status": "ready"}
    assert response.json()["regimeCollection"] == app.state.tickdesk_regime_collection
