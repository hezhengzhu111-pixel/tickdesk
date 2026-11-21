import hashlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.data_providers import custom
from app.services import (
    preferences,
    tickdesk_admission,
    tickdesk_market_data,
    tickdesk_research,
    tickdesk_worker,
)
from app.services.tickdesk_adapter import MarketAdapter
from app.services.tickdesk_quote_feed import quote_session
from app.services.tickdesk_regime_archive import INDEX_ARCHIVE, import_calendar, lookup


def milliseconds(day_time):
    return int(datetime.fromisoformat(f"{day_time}+08:00").timestamp() * 1000)


def write_calendar(root, observed, through, *, source="fuyao", sessions=None):
    path = root / "tickdesk-market" / "calendar.json"
    path.parent.mkdir()
    path.write_text(json.dumps({
        "source": source,
        "observedAt": milliseconds(observed),
        "through": through,
        "sessions": ["2026-11-12", "2026-11-13"] if sessions is None else sessions,
    }), encoding="utf-8")


def adapter_for(root):
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=root))
    return MarketAdapter(repo, None)


def prepare(adapter, command, cutoff, version=3):
    return adapter.prepare({
        "data": {"cmd": command},
        "objects": [{"symbol": "600000.SH"}],
        "requestedAt": milliseconds(cutoff),
        "quantObservationVersion": version,
        "holdingRuleVersion": 3,
    })


def forbidden(*args, **kwargs):
    pytest.fail("a denied scan consumed a different data path")


@pytest.mark.parametrize("command", ["scan_now", "resume"])
@pytest.mark.parametrize(("observed", "through", "source", "cutoff", "state"), [
    ("2026-11-15T00:01:00", "2026-11-15", "fuyao", "2026-11-15T10:00:00", "closed"),
    ("2026-11-13T00:01:00", "2026-11-13", "fuyao", "2026-11-13T11:36:00", "closed"),
    (None, None, None, "2026-11-16T10:00:00", "unknown"),
    ("2026-11-13T00:01:00", "2026-11-13", "fuyao", "2026-11-15T10:00:00", "unknown"),
    ("2026-11-16T10:01:00", "2026-11-16", "fuyao", "2026-11-16T10:00:00", "unknown"),
    ("2026-11-13T00:01:00", "2026-11-16", "fuyao", "2026-11-16T00:02:00", "unknown"),
    ("2026-11-15T00:01:00", "2026-11-15", "unknown-source", "2026-11-15T10:00:00", "unknown"),
    ("2026-11-15T00:01:00", "2026-11-15", "fuyao", "2026-11-18T00:01:00.001", "unknown"),
])
def test_denied_scan_stops_before_any_worker_or_holding_read(
    tmp_path, monkeypatch, command, observed, through, source, cutoff, state,
):
    if observed:
        write_calendar(tmp_path, observed, through, source=source)
    adapter = adapter_for(tmp_path)
    monkeypatch.setattr(adapter, "quotes", forbidden)
    monkeypatch.setattr(adapter, "archived_holding_batch", forbidden)
    monkeypatch.setattr(tickdesk_admission, "read_approval", forbidden)
    monkeypatch.setattr(tickdesk_research, "read_research_config", forbidden)
    monkeypatch.setattr(tickdesk_worker, "scan_in_worker", forbidden)
    monkeypatch.setattr(tickdesk_worker, "research_in_worker", forbidden)

    response = prepare(adapter, command, cutoff)
    expected = {
        "error": "SCAN_OUTSIDE_WINDOW" if state == "closed" else "DATA_UNAVAILABLE",
        "quotes": [],
        "tradingCalendar": {
            "date": cutoff[:10],
            "state": state,
            "observedAt": milliseconds(observed) if state == "closed" else None,
            "through": through if state == "closed" else None,
            "source": source if state == "closed" else None,
        },
    }
    if state == "unknown":
        expected["productionReason"] = "TRADING_CALENDAR_UNAVAILABLE"
    assert response == expected


@pytest.mark.parametrize("command", ["scan_now", "resume"])
def test_open_scan_retains_quotes_holding_and_both_workers(tmp_path, monkeypatch, command):
    observed = "2026-11-16T08:00:00"
    cutoff = "2026-11-16T09:25:00"
    write_calendar(tmp_path, observed, cutoff[:10], sessions=["2026-11-13", "2026-11-16"])
    adapter = adapter_for(tmp_path)
    quote = {"symbol": "600000.SH", "asOf": milliseconds(cutoff)}
    calls = []

    def quotes(symbols, now):
        assert symbols == {"600000.SH"} and now == milliseconds(cutoff)
        return [quote]

    def scan(adapter, request, approval, *, trading, deadline):
        assert trading is True and approval == {"id": "approved"}
        calls.append("scan")
        return {"session": "fixture"}

    def research(adapter, request, config, *, deadline):
        assert config == {"id": "config"}
        calls.append("research")
        return {"version": 3}

    monkeypatch.setattr(adapter, "quotes", quotes)
    monkeypatch.setattr(adapter, "archived_holding_batch", lambda request: {"holdingRuleBatch": {"version": 3}})
    monkeypatch.setattr(tickdesk_admission, "read_approval", lambda root: {"id": "approved"})
    monkeypatch.setattr(tickdesk_research, "read_research_config", lambda root: {"id": "config"})
    monkeypatch.setattr(tickdesk_worker, "scan_in_worker", scan)
    monkeypatch.setattr(tickdesk_worker, "research_in_worker", research)

    assert prepare(adapter, command, cutoff) == {
        "tradingCalendar": {
            "date": cutoff[:10], "state": "open", "observedAt": milliseconds(observed),
            "through": cutoff[:10], "source": "fuyao",
        },
        "quotes": [quote],
        "frozen": {"session": "fixture"},
        "quantObservationBatch": {"version": 3},
        "holdingRuleBatch": {"version": 3},
    }
    assert calls == ["scan", "research"]


def test_quotes_refresh_retains_archived_holding_rules_with_no_scan_calendar(tmp_path, monkeypatch):
    adapter = adapter_for(tmp_path)
    monkeypatch.setattr(adapter, "quotes", lambda symbols, now: [{"symbol": next(iter(symbols))}])
    monkeypatch.setattr(adapter, "archived_holding_batch", lambda request: {"holdingRuleBatch": {"version": 3}})
    monkeypatch.setattr(tickdesk_worker, "scan_in_worker", forbidden)
    monkeypatch.setattr(tickdesk_worker, "research_in_worker", forbidden)

    assert prepare(adapter, "quotes", "2026-11-16T10:00:00") == {
        "quotes": [{"symbol": "600000.SH"}], "holdingRuleBatch": {"version": 3},
    }


def test_refresh_archives_the_original_calendar_receipt_before_any_collection_worker(tmp_path, monkeypatch):
    receipt = datetime.fromisoformat("2026-11-16T08:00:00.015+08:00")
    sessions = {"2026-11-12", "2026-11-13", "2026-11-16"}
    monkeypatch.setattr(tickdesk_market_data, "datetime", SimpleNamespace(now=lambda _: receipt))
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "fuyao")
    monkeypatch.setattr(custom, "get_provider", lambda _: SimpleNamespace(trading_days=lambda: sessions))

    tickdesk_market_data._refresh_calendar(tmp_path)

    archive = tmp_path / INDEX_ARCHIVE
    match = lookup(archive, "calendar", "fuyao", receipt)
    assert match.state == "known"
    assert match.record.body == (archive.parent / "calendar.json").read_bytes()
    assert match.record.observation.observed_at == receipt
    assert match.record.observation.data == sorted(sessions)
    assert lookup(archive, "calendar", "fuyao", receipt - timedelta(milliseconds=1)).state == "missing"


def test_scan_recovers_calendar_at_the_original_request_when_the_live_cache_is_newer(tmp_path):
    observed = "2026-11-16T08:00:00"
    cutoff = "2026-11-16T09:25:00"
    write_calendar(tmp_path, observed, cutoff[:10], sessions=["2026-11-13", "2026-11-16"])
    archive = tmp_path / INDEX_ARCHIVE
    cache = archive.parent / "calendar.json"
    original = cache.read_bytes()
    import_calendar(archive, original, hashlib.sha256(original).hexdigest())
    newer = {"source": "fuyao", "observedAt": milliseconds("2026-11-17T08:00:00"),
             "through": "2026-11-17", "sessions": ["2026-11-13", "2026-11-16", "2026-11-17"]}
    cache.write_text(json.dumps(newer), encoding="utf-8")

    assert tickdesk_market_data.scan_calendar_evidence(tmp_path, milliseconds(cutoff)) == {
        "date": cutoff[:10], "state": "open", "observedAt": milliseconds(observed),
        "through": cutoff[:10], "source": "fuyao",
    }
    assert quote_session(tmp_path, milliseconds(cutoff)) == "open"
    earlier = tickdesk_market_data.scan_calendar_evidence(tmp_path, milliseconds("2026-11-16T07:59:59.999"))
    assert earlier["state"] == "unknown" and earlier["observedAt"] is None
    assert quote_session(tmp_path, milliseconds("2026-11-16T07:59:59.999")) == "unknown"


@pytest.mark.parametrize("denied", [True, False])
def test_refresh_does_not_invent_today_on_permission_denial_or_midnight_rollover(tmp_path, monkeypatch, denied):
    start = "2026-11-15T23:59:59"
    finish = "2026-11-16T00:00:01"
    moments = iter([start, finish])

    def now(zone):
        return datetime.fromisoformat(f"{next(moments)}+08:00").astimezone(zone)

    def trading_days():
        if denied:
            raise PermissionError("provider calendar permission unavailable")
        return {"2026-11-12", "2026-11-13"}

    monkeypatch.setattr(tickdesk_market_data, "datetime", SimpleNamespace(now=now))
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "fuyao")
    monkeypatch.setattr(custom, "get_provider", lambda source: SimpleNamespace(trading_days=trading_days))
    write_calendar(tmp_path, "2026-11-14T09:00:00", "2026-11-14")
    path = tmp_path / "tickdesk-market" / "calendar.json"
    previous = path.read_bytes()
    if denied:
        with pytest.raises(PermissionError):
            tickdesk_market_data._refresh_calendar(tmp_path)
    else:
        tickdesk_market_data._refresh_calendar(tmp_path)

    assert path.read_bytes() == previous
