import asyncio
import json
from datetime import date, datetime
from threading import Event

import polars as pl
import pytest

from app.services import tickdesk_daily_history as history
from app.services.tickdesk_adapter import CN
from app.tickflow.repository import DataStore, KlineRepository


def row(day, symbol, price=10.0):
    return {"date": day, "symbol": symbol, "open": price, "high": price + 1, "low": price - 1,
            "close": price, "volume": 1000.0, "amount": 100000.0}


@pytest.fixture
def repository(tmp_path):
    repo = KlineRepository(DataStore(tmp_path))
    yield repo
    repo.store.db.close()


def configure(monkeypatch, repository, plan, frames):
    windows = []

    class Provider:
        def iter_daily(self, symbols, start, end, *, bulk_only, check_cancel):
            assert bulk_only and symbols == sorted(plan[2])
            windows.append((start.date(), end.date()))
            for frame in frames:
                check_cancel()
                yield frame

    root = repository.store.data_dir
    (root / "tickdesk-market").mkdir()
    monkeypatch.setattr(history, "_history_plan", lambda root, now: plan)
    monkeypatch.setattr(history.custom, "provider_has_dataset", lambda source, dataset: True)
    monkeypatch.setattr(history.custom, "get_provider", lambda source: Provider())
    return windows


def test_stages_then_publishes_daily_history_and_reports_known_listing_gaps(repository, monkeypatch):
    root = repository.store.data_dir
    fri, mon = date(2026, 11, 13), date(2026, 11, 16)
    bank, recent = "600000.SH", "000001.SZ"
    repo_only = "688001.SH"
    repository.append_daily(pl.DataFrame([row(fri, repo_only)]))
    frames = [pl.DataFrame([row(fri, bank), row(mon, bank), row(fri, recent)])]
    windows = configure(monkeypatch, repository, ("fuyao", [fri, mon], {bank: fri, recent: mon}), frames)

    async def import_history():
        return await history.refresh_daily_history(repository, asyncio.Lock(), now=datetime(2026, 11, 16, 17, tzinfo=CN))

    result = asyncio.run(import_history())
    assert result["dates"] == {"2026-11-13": {"observed": 1, "expected": 1},
                               "2026-11-16": {"observed": 1, "expected": 2}}
    public = history.coverage_diagnostic(result)
    assert public == {"status": "partial", "reason": None, "coverage": {
        "source": "fuyao", "start": "2026-11-13", "through": "2026-11-16", "expectedThrough": "2026-11-16",
        "updatedAt": result["updatedAt"], "rows": 2, "requestedSymbols": 2, "observedSymbols": 1,
        "conflictingRows": 0, "latestDay": {"observed": 1, "expected": 2},
    }}
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.tickdesk import router
    from app.services.tickdesk_adapter import timestamp

    app = FastAPI()
    app.include_router(router)
    app.state.repo = repository
    app.state.tickdesk_history = result
    with TestClient(app, client=("127.0.0.1", 20000)) as api:
        request = {"data": {"cmd": "quotes"}, "requestedAt": timestamp(datetime.now(CN))}
        assert api.post("/api/tickdesk/prepare", json=request).json() == {"quotes": []}
        reply = api.post("/api/tickdesk/prepare", json={**request, "historyCoverageVersion": 1}).json()
        assert reply == {"quotes": [], "historyCoverage": public}
        app.state.tickdesk_history = {"status": "raw_ready"}
        broken = api.post("/api/tickdesk/prepare", json={**request, "historyCoverageVersion": 1}).json()
        assert broken == {"quotes": [], "historyCoverage": {
            "status": "unavailable", "coverage": None, "reason": "snapshot_invalid",
        }}
    assert result["symbols"][recent] == {"observed": 0, "expected": 1}
    assert result["status"] == "raw_partial"
    assert result["basis"] == "none"
    cached = asyncio.run(import_history())
    assert cached == result
    assert windows == [(fri, mon)]
    assert set(pl.read_parquet(root / "kline_daily/date=2026-11-13/part.parquet")["symbol"]) == {bank, repo_only}
    assert (root / "kline_daily/date=2026-11-16/part.parquet").is_file()
    archive = json.loads((root / "tickdesk-market/history-coverage.json").read_text(encoding="utf-8"))
    assert archive["dates"] == result["dates"] and archive["rows"] == 2


@pytest.mark.parametrize("duplicate,reason", [(False, "SESSION_MISSING"), (True, "DUPLICATE_BARS")])
def test_invalid_provider_history_never_changes_live_partitions(repository, monkeypatch, duplicate, reason):
    fri, mon = date(2026, 11, 13), date(2026, 11, 16)
    bank = "600000.SH"
    repository.append_daily(pl.DataFrame([row(fri, bank, price=9.0)]))
    root = repository.store.data_dir
    frame = pl.DataFrame([row(fri, bank, price=20.0)])
    frames = [frame, frame] if duplicate else [frame]
    configure(monkeypatch, repository, ("fuyao", [fri, mon], {bank: fri}), frames)

    async def import_history():
        await history.refresh_daily_history(repository, asyncio.Lock())

    with pytest.raises(history.HistoryUnavailableError, match=reason):
        asyncio.run(import_history())
    assert pl.read_parquet(root / "kline_daily/date=2026-11-13/part.parquet")["close"].item() == 9
    assert not (root / "kline_daily/date=2026-11-16/part.parquet").exists()
    assert not (root / "tickdesk-market/history-coverage.json").exists()
    assert not list((root / ".daily_sync_staging").iterdir())


def test_next_session_refresh_reuses_archived_history_outside_recent_window(repository, monkeypatch):
    first, fri, mon = date(2026, 10, 20), date(2026, 11, 13), date(2026, 11, 16)
    bank = "600000.SH"
    days = [first, fri]
    frames = [pl.DataFrame([row(first, bank), row(fri, bank)])]
    windows = configure(monkeypatch, repository, ("fuyao", days, {bank: first}), frames)

    async def import_history():
        return await history.refresh_daily_history(repository, asyncio.Lock())

    baseline = asyncio.run(import_history())
    assert baseline["rows"] == 2
    days.append(mon)
    frames[:] = [pl.DataFrame([row(fri, bank), row(mon, bank)])]
    current = asyncio.run(import_history())
    assert windows == [(first, fri), (fri, mon)]
    assert current["rows"] == 3
    assert current["symbols"][bank] == {"expected": 3, "observed": 3}
    assert list(current["dates"]) == [str(first), str(fri), str(mon)]


def test_bulk_history_preserves_existing_bars_and_reports_conflicting_overlap(repository, monkeypatch):
    day = date(2026, 11, 13)
    held, added = "600000.SH", "000001.SZ"
    repository.append_daily(pl.DataFrame([row(day, held, price=9.0)]))
    frames = [pl.DataFrame([row(day, held, price=20.0), row(day, added)])]
    configure(monkeypatch, repository, ("fuyao", [day], {held: day, added: day}), frames)

    async def import_history():
        return await history.refresh_daily_history(repository, asyncio.Lock())

    result = asyncio.run(import_history())
    root = repository.store.data_dir
    raw = pl.read_parquet(root / "kline_daily/date=2026-11-13/part.parquet")
    assert dict(raw.select("symbol", "close").iter_rows()) == {held: 9.0, added: 10.0}
    assert result["conflictingRows"] == 1
    assert result["symbols"][held] == {"expected": 1, "observed": 0}


def test_cold_bootstrap_can_publish_prior_sessions_while_newest_dump_is_pending(repository, monkeypatch):
    from app.plugins.fuyao.client import FuyaoError

    fri, mon = date(2026, 11, 13), date(2026, 11, 16)
    bank = "600000.SH"
    configure(monkeypatch, repository, ("fuyao", [fri, mon], {bank: fri}), [])
    attempts = []

    class Provider:
        def iter_daily(self, symbols, start, end, **kwargs):
            attempts.append(end.date())
            if end.date() == mon:
                raise FuyaoError("bulk daily coverage unavailable", code="BULK_DAILY_COVERAGE_UNAVAILABLE")
            yield pl.DataFrame([row(fri, bank)])

    monkeypatch.setattr(history.custom, "get_provider", lambda source: Provider())

    async def import_history():
        return await history.refresh_daily_history(repository, asyncio.Lock())

    result = asyncio.run(import_history())
    assert result["through"] == str(fri) and result["expectedThrough"] == str(mon)
    assert result["status"] == "raw_partial"
    assert result["rows"] == 1
    assert attempts == [mon, fri]
    root = repository.store.data_dir
    coverage = root / "tickdesk-market/history-coverage.json"
    archived = coverage.read_bytes()
    retained = asyncio.run(import_history())
    assert retained["through"] == str(fri) and retained["expectedThrough"] == str(mon)
    assert retained["status"] == "raw_partial" and retained["rows"] == 1
    assert retained["refreshReason"] == "MARKET_HISTORY_PROVIDER_WINDOW_UNAVAILABLE"
    summary = history.coverage_diagnostic(retained)
    assert summary["reason"] == "provider_pending" and summary["coverage"]["through"] == str(fri)
    assert retained["updatedAt"] == result["updatedAt"] and coverage.read_bytes() == archived
    assert attempts == [mon, fri, mon]
    assert not (root / "kline_daily/date=2026-11-16/part.parquet").exists()


def test_cancel_signals_and_waits_for_staging_thread():
    entered, finished, cancel = Event(), Event(), Event()

    def work():
        entered.set()
        try:
            assert cancel.wait(2)
        finally:
            finished.set()

    async def run():
        publisher = asyncio.create_task(history._drain_apply(work, cancel))
        assert await asyncio.to_thread(entered.wait, 2)
        publisher.cancel()
        with pytest.raises(asyncio.CancelledError):
            await publisher
        assert finished.is_set()

    asyncio.run(run())

def test_v2_history_coverage_includes_whitelisted_suspension_attribution(repository):
    attribution = {
        "version": 1, "calculatedAt": 1000,
        "window": {"start": "2026-01-21", "end": "2026-11-17", "sessions": 198},
        "missingCells": 4, "attributedCells": 4, "unexplainedCells": 0,
        "attributedByDay": {"2026-11-17": 4}, "unexplained": [],
        "sources": {"sseDays": 214, "szseDays": 200, "archiveDays": 217},
        "archiveSignature": "a" * 64, "status": "all_attributed",
    }
    result = {"status": "raw_partial", "source": "fuyao", "start": "2026-01-21", "through": "2026-11-17",
              "expectedThrough": "2026-11-17", "updatedAt": 1000, "rows": 10, "observedSymbols": 2,
              "requestedSymbols": 2, "conflictingRows": 0,
              "dates": {"2026-11-17": {"observed": 1, "expected": 2}}, "attribution": attribution}
    public = history.coverage_diagnostic(result, include_attribution=True)
    assert public["coverage"]["attribution"] == {
        "status": "all_attributed",
        "window": {"start": "2026-01-21", "end": "2026-11-17", "sessions": 198},
        "missingCells": 4, "attributedCells": 4, "unexplainedCells": 0,
        "sources": {"sseDays": 214, "szseDays": 200, "archiveDays": 217},
        "archiveSignature": "a" * 64, "unexplained": [],
    }
    assert history.coverage_diagnostic(result)["coverage"].get("attribution") is None
    broken = {**result, "attribution": {"status": "all_attributed", "window": {}}}
    assert history.coverage_diagnostic(broken, include_attribution=True)["coverage"]["attribution"] is None
    from datetime import datetime

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.tickdesk import router
    from app.services.tickdesk_adapter import timestamp

    app = FastAPI()
    app.include_router(router)
    app.state.repo = repository
    app.state.tickdesk_history = result
    with TestClient(app, client=("127.0.0.1", 20001)) as api:
        request = {"data": {"cmd": "quotes"}, "requestedAt": timestamp(datetime.now(CN))}
        v1 = api.post("/api/tickdesk/prepare", json={**request, "historyCoverageVersion": 1}).json()
        assert "attribution" not in v1["historyCoverage"]["coverage"]
        v2 = api.post("/api/tickdesk/prepare", json={**request, "historyCoverageVersion": 2}).json()
        assert v2["historyCoverage"]["coverage"]["attribution"]["status"] == "all_attributed"
