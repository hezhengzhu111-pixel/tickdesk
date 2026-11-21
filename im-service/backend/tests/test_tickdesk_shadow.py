import json
import shutil
import sqlite3
from datetime import datetime

import polars as pl
import pytest
from tickdesk_quant_fixture import BAR_COLUMNS, NEXT_DAY, build_fixture

from app.services import tickdesk_shadow
from app.services.tickdesk_adapter import MarketAdapter, fingerprint, timestamp
from app.services.tickdesk_research import read_research_config, scan_research
from app.services.tickdesk_worker import research_in_worker
from app.strategy.engine import StrategyEngine
from app.tickflow.repository import DataStore, KlineRepository


def test_explicit_shadow_capture_preserves_clocks_origin_and_same_session_identity(tmp_path, monkeypatch):
    root = tmp_path / "inputs"
    output = tmp_path / "archive"
    api = build_fixture(root)
    api.state.repo.store.db.close()
    strategies = root / "strategies"
    (strategies / "custom").mkdir()
    for source in strategies.glob("*.py"):
        shutil.move(source, strategies / "custom" / source.name)
    calendar = root / "tickdesk-market"
    calendar.mkdir()
    observed = timestamp("2026-01-02T15:25:00+08:00")
    snapshot = {
        "sessions": ["2026-01-02", "2026-01-05"], "through": "2026-01-05",
        "source": "fixture-calendar", "observedAt": observed,
    }
    calendar_path = calendar / "calendar.json"
    calendar_path.write_text(json.dumps(snapshot), encoding="utf-8")
    now = timestamp("2026-01-02T15:20:00+08:00")
    monkeypatch.setattr(tickdesk_shadow.time, "time_ns", lambda: now * 1_000_000)

    with pytest.raises(ValueError, match="observed trading session"):
        tickdesk_shadow.capture_observation(root, output)
    now = timestamp("2026-01-02T15:35:00+08:00")
    saved = tickdesk_shadow.capture_observation(root, output)
    assert saved["origin"] == "local_forward"
    assert saved["session"] == "2026-01-02"
    assert saved["cutoffMs"] == saved["recordedAtMs"] == now
    assert saved["completeBarDays"] == ["2026-01-02"]
    now = timestamp("2026-01-02T16:00:00+08:00")
    assert tickdesk_shadow.capture_observation(root, output) == saved

    historical = tickdesk_shadow.capture_observation(
        root, output, cutoff=datetime.fromisoformat("2026-01-02T15:22:00+08:00"),
    )
    assert historical["origin"] == "historical_replay"
    assert historical["recordedAtMs"] == now > historical["cutoffMs"]
    assert tickdesk_shadow.replay_run(output, historical["hash"])["origin"] == "historical_replay"
    assert tickdesk_shadow.replay_run(output, saved["hash"])["status"] == "matched"
    assert len(tickdesk_shadow.list_runs(output)) == 2
    assert tickdesk_shadow.list_runs(root) == []

    now = timestamp("2026-01-05T16:00:00+08:00")
    calendar_path.write_text(json.dumps({**snapshot, "observedAt": now}), encoding="utf-8")
    with pytest.raises(ValueError, match="same-session finalized bars"):
        tickdesk_shadow.capture_observation(root, output)
    with pytest.raises(ValueError, match="outside the source data directory"):
        tickdesk_shadow.capture_observation(root, root / "nested")
    with pytest.raises(ValueError, match="future cutoff"):
        tickdesk_shadow.capture_observation(
            root, output, cutoff=datetime.fromisoformat("2026-01-06T16:00:00+08:00"),
        )
    rows = pl.read_parquet(root / "kline_daily_enriched" / f"date={NEXT_DAY}" / "part.parquet").to_dicts()
    store = DataStore.readonly(root)
    try:
        KlineRepository(store).write_quant_reference_evidence([{
            "symbol": row["symbol"], "field": "daily_bar_finality", "source": "second-fixture-session",
            "effective_date": str(NEXT_DAY), "available_at": f"{NEXT_DAY}T15:30:00+08:00",
            "value": {"end_at": f"{NEXT_DAY}T15:00:00+08:00",
                      "bar_hash": fingerprint([str(NEXT_DAY), row["symbol"], *[float(row[col]) for col in BAR_COLUMNS]])},
        } for row in rows])
    finally:
        store.db.close()
    next_day = tickdesk_shadow.capture_observation(root, output)
    assert next_day["origin"] == saved["origin"] and next_day["hash"] != saved["hash"]
    assert next_day["session"] == str(NEXT_DAY) and next_day["completeBarDays"] == [str(NEXT_DAY)]
    assert len(tickdesk_shadow.list_runs(output)) == 3


def test_research_archive_is_continued_across_sessions_and_replays_frozen_inputs(tmp_path, monkeypatch):
    root = tmp_path / "kernel"
    api = build_fixture(root)
    repo = api.state.repo
    adapter = MarketAdapter(repo, api.state.strategy_engine)
    config = read_research_config(root)
    epoch = "test-epoch"

    def evaluate(local_clock, *, durable=True):
        cutoff = timestamp(datetime.fromisoformat(f"{local_clock}+08:00"))
        monkeypatch.setattr(tickdesk_shadow.time, "time_ns", lambda: cutoff * 1_000_000)
        request = {"requestedAt": cutoff}
        if durable:
            request.update(epoch=epoch, scanLease=f"lease-{cutoff}", leaseAcquired=True)
        return research_in_worker(adapter, request, config)

    try:
        assert evaluate("2026-01-05T10:00:00", durable=False)["observations"]
        assert tickdesk_shadow.list_runs(root) == []
        original = evaluate("2026-01-05T10:00:01")
        cached = evaluate("2026-01-05T10:05:00")
        assert original["observations"] == cached["observations"]
        [first] = tickdesk_shadow.list_runs(root)
        assert first["session"] == "2026-01-05" and first["completeBarDays"] == ["2026-01-02"]
        assert first["origin"] == "leased_scan"
        assert first["entryKnown"] > 0
        with sqlite3.connect(tickdesk_shadow._archive(root)) as db:
            receipts = db.execute("SELECT cutoff,lease_hash,run_hash FROM scan_receipts ORDER BY cutoff").fetchall()
        assert receipts == [
            (cutoff, fingerprint(f"lease-{cutoff}".encode()), first["hash"])
            for cutoff in (original["decisionCutoffMs"], cached["decisionCutoffMs"])
        ]

        path = root / "kline_daily_enriched" / f"date={NEXT_DAY}" / "part.parquet"
        rows = pl.read_parquet(path).to_dicts()
        repo.write_quant_reference_evidence([{
            "symbol": row["symbol"], "field": "daily_bar_finality", "source": "separate-fixture-session",
            "effective_date": str(NEXT_DAY), "available_at": f"{NEXT_DAY}T15:30:00+08:00",
            "value": {"end_at": f"{NEXT_DAY}T15:00:00+08:00",
                      "bar_hash": fingerprint([str(NEXT_DAY), row["symbol"], *[float(row[col]) for col in BAR_COLUMNS]])},
        } for row in rows])
        evaluate("2026-01-06T10:00:00")
        second, same = tickdesk_shadow.list_runs(root)
        assert second["session"] == "2026-01-06" and second["completeBarDays"] == [str(NEXT_DAY)]
        assert same == first
        from app.services.tickdesk_admission import strategy_sources
        from app.services.tickdesk_admission_shadow import review_shadow
        from app.strategy.config import load_override

        strategy_id = config["strategies"][0]
        definition = adapter.engine.get(strategy_id)
        sources, code_hash, dependencies_hash = strategy_sources(definition)
        overrides = load_override(root, strategy_id) or {}
        parameters = adapter.engine.resolve_params(definition, None, overrides)
        spec = {"id": strategy_id, "direction": "sell", "symbols": config["symbols"],
                "codeHash": code_hash, "dependenciesHash": dependencies_hash,
                "parametersHash": fingerprint({"params": parameters, "overrides": overrides})}
        examined_at = timestamp("2026-01-06T10:00:01+08:00")
        observed = {"runHashes": [first["hash"], second["hash"]]}
        review = review_shadow(root, observed, spec, sources, examined_at)
        assert review["blockers"] == []
        assert review["qualifiedSessions"] == ["2026-01-05", "2026-01-06"]
        assert "SHADOW_OBSERVATION_DAYS_INSUFFICIENT" in review_shadow(
            root, {"runHashes": [first["hash"]]}, spec, sources, examined_at,
        )["blockers"]
        assert "SHADOW_REVIEW_SCOPE_MISMATCH" in review_shadow(
            root, observed, {**spec, "symbols": config["symbols"][1:]}, sources, examined_at,
        )["blockers"]
        assert "SHADOW_OBSERVATION_CLOCK_MISMATCH" in review_shadow(
            root, observed, spec, sources, second["recordedAtMs"],
        )["blockers"]

        # A later revision to live data cannot change the sealed prior replay.
        pl.DataFrame(rows).with_columns(pl.lit(42.0).alias("close")).write_parquet(path)
        for archived in (first, second):
            replay = tickdesk_shadow.replay_run(root, archived["hash"])
            assert replay["status"] == "matched" and replay["evaluationsMatched"] == 1
            assert replay["stage"] == "research"
            assert replay["completeBarDays"] == archived["completeBarDays"]
        with sqlite3.connect(root / "research" / "tickdesk-shadow" / "history.sqlite3") as db:
            db.execute("UPDATE runs SET payload=(SELECT payload FROM runs WHERE hash=?) WHERE hash=?",
                       (first["hash"], second["hash"]))
        with pytest.raises(ValueError, match="archive hash mismatch"):
            tickdesk_shadow.replay_run(root, second["hash"])
    finally:
        repo.store.db.close()


def test_research_archive_retries_unchanged_input_after_a_database_lock(tmp_path, monkeypatch):
    root = tmp_path / "kernel"
    api = build_fixture(root)
    repo = api.state.repo
    adapter = MarketAdapter(repo, api.state.strategy_engine)
    config = read_research_config(root)
    archive = root / "research" / "tickdesk-shadow" / "history.sqlite3"
    archive.parent.mkdir(parents=True, exist_ok=True)
    request = {"epoch": "test-epoch", "scanLease": "test-lease", "leaseAcquired": True}

    def evaluate(local_clock):
        cutoff = timestamp(datetime.fromisoformat(f"{local_clock}+08:00"))
        monkeypatch.setattr(tickdesk_shadow.time, "time_ns", lambda: cutoff * 1_000_000)
        return research_in_worker(adapter, {**request, "requestedAt": cutoff}, config)

    try:
        with sqlite3.connect(archive) as locked:
            locked.execute("BEGIN EXCLUSIVE")
            first = evaluate("2026-01-05T10:00:00")
        assert first["observations"]
        retry = evaluate("2026-01-05T10:05:00")
        assert retry["observations"] == first["observations"]
        [saved] = tickdesk_shadow.list_runs(root)
        assert saved["cutoffMs"] == retry["decisionCutoffMs"]
        evaluate("2026-01-05T10:10:00")
        assert tickdesk_shadow.list_runs(root) == [saved]
        assert tickdesk_shadow.replay_run(root, saved["hash"])["status"] == "matched"
    finally:
        repo.store.db.close()


def test_research_reads_financial_evidence_only_at_verified_bar_close(tmp_path, monkeypatch):
    root = tmp_path / "kernel"
    api = build_fixture(root)
    repo = api.state.repo
    adapter = MarketAdapter(repo, api.state.strategy_engine)
    config = read_research_config(root)
    reads = []
    lookup = repo.get_quant_input_evidence

    def read(stamp, *args, **kwargs):
        reads.append(stamp)
        return lookup(stamp, *args, **kwargs)

    monkeypatch.setattr(repo, "get_quant_input_evidence", read)
    cutoff = timestamp(datetime.fromisoformat("2026-01-05T10:00:00+08:00"))
    try:
        batch = scan_research(adapter, {"requestedAt": cutoff}, config, {config["strategies"][0]: {}}, {})
        assert batch["observations"] and reads
        assert all(isinstance(stamp, datetime) and stamp.hour == 15 and stamp.minute == 0 for stamp in reads)
        assert len(set(reads)) == len(reads)
    finally:
        repo.store.db.close()


@pytest.mark.parametrize("change_during_scan", [False, True])
def test_multi_strategy_research_freezes_one_snapshot_and_preserves_each_fields_scope(
    tmp_path, monkeypatch, change_during_scan,
):
    from app.services.screener import ScreenerService

    root = tmp_path / "kernel"
    api = build_fixture(root)
    repo = api.state.repo
    config = read_research_config(root)
    original = config["strategies"][0]
    alternate = "separate_financial_fields"
    source = root / "strategies" / f"{original}.py"
    alternate_source = source.read_text(encoding="utf-8").replace(original, alternate).replace("roe_latest", "pb_latest")
    (source.parent / f"{alternate}.py").write_text(alternate_source, encoding="utf-8")
    config["strategies"].append(alternate)
    adapter = MarketAdapter(repo, StrategyEngine([source.parent]))
    snapshots, holdings_reads, captured = [], [], {}
    read_snapshot = repo.get_quant_evidence_snapshot
    load_current = ScreenerService._load_enriched_for_date

    def snapshot(fields):
        snapshots.append(set(fields))
        return read_snapshot(fields)

    def current(service, target_date):
        holdings_reads.append(target_date)
        return load_current(service, target_date)

    def capture(strategy, context, *_):
        captured[strategy] = {field for _, field in context.quant_evidence.facts}
        if change_during_scan and strategy == original:
            repo.write_quant_reference_evidence([{
                "symbol": config["symbols"][0], "source": "new-reference", "field": "is_st",
                "effective_date": "2026-01-05", "available_at": "2026-01-05T09:00:00+08:00", "value": False,
            }])

    monkeypatch.setattr(repo, "get_quant_evidence_snapshot", snapshot)
    monkeypatch.setattr(ScreenerService, "_load_enriched_for_date", current)
    request = {"requestedAt": timestamp("2026-01-05T10:00:00+08:00")}
    try:
        if change_during_scan:
            with pytest.raises(ValueError, match="historical evidence changed"):
                scan_research(adapter, request, config, {original: {}, alternate: {}}, {}, capture=capture)
        else:
            batch = scan_research(adapter, request, config, {original: {}, alternate: {}}, {}, capture=capture)
            assert len(batch["observations"]) == 8
        assert snapshots == [{"daily_bar_finality", "roe_latest", "pb_latest", "raw_close"}] * 2
        assert len(holdings_reads) == 1
        assert captured[original] == {"roe_latest", "raw_close"}
        assert captured[alternate] == {"pb_latest", "raw_close"}
    finally:
        repo.store.db.close()


def test_regime_trial_clears_unproven_extension_values_and_archives_the_exact_cutoff(tmp_path):
    from types import MappingProxyType

    from app.quant_evidence import QuantInputEvidence
    from app.services.tickdesk_regime_archive import import_calendar, import_probe
    from app.services.tickdesk_regime_evidence import INDUSTRY_FIELD, MARKET_FIELD, RULE_ID
    from app.services.tickdesk_research import _project_regime_trial
    from tests.test_tickdesk_regime_archive import (
        DAY,
        INDUSTRY,
        MARKET,
        MORNING,
        SESSIONS,
        SYMBOL,
        artifact,
        members,
        prices,
    )

    archive = tmp_path / "verified-regime.sqlite3"
    early = MORNING.replace(hour=15)
    import_calendar(archive, *artifact({"observedAt": timestamp(MORNING), "source": "fuyao",
                                       "through": str(DAY), "sessions": [str(day) for day in [*SESSIONS, DAY]]}))
    import_probe(archive, *artifact({"market": prices(MARKET, MORNING),
                                     "member": members(MORNING),
                                     "industry": prices(INDUSTRY, early.replace(microsecond=1000))}))
    initial = QuantInputEvidence(early.isoformat(), MappingProxyType({}), "original-identity")
    inputs = {str(DAY): initial}
    history = pl.DataFrame({"date": [SESSIONS[-1], DAY], "symbol": [SYMBOL] * 2,
                            MARKET_FIELD: [9.0, 9.0], INDUSTRY_FIELD: [9.0, 9.0]})
    projected = _project_regime_trial(history, inputs, archive, SYMBOL, DAY, {})
    assert projected[MARKET_FIELD].to_list() == [None, 1.0]
    assert projected[INDUSTRY_FIELD].to_list() == [None, None]
    sealed = inputs[str(DAY)]
    assert sealed.cutoff == early.isoformat() and sealed.identity != initial.identity
    market = sealed.fact(SYMBOL, MARKET_FIELD)
    assert market.value == 1 and market.available_at == MORNING.isoformat()
    assert market.revision_ids[0] == RULE_ID and len(market.revision_ids[1]) == 64
    assert sealed.fact(SYMBOL, INDUSTRY_FIELD).reason == "index_observation_unavailable"


def test_regime_trial_uses_st_fact_at_the_close_and_keeps_conflicts_unknown(tmp_path):
    from datetime import timedelta
    from types import MappingProxyType

    from app.quant_evidence import EvidenceFact, QuantInputEvidence
    from app.services.tickdesk_research import _project_regime_trial
    from app.strategy.quant_panel import _with_pit_values
    from tests.test_tickdesk_regime_archive import DAY, MORNING, SYMBOL

    close = MORNING.replace(hour=15)
    day = str(DAY)
    archive = tmp_path / "archive-not-yet-collected.sqlite3"
    history = pl.DataFrame({"date": [DAY], "symbol": [SYMBOL], "is_st": [True]})
    official = EvidenceFact(False, None, ("a" * 64,), MORNING.isoformat())
    decision_times = []

    def st_reader(symbol, cutoff):
        assert symbol == SYMBOL
        decision_times.append(cutoff)
        return official

    for observed, original, expected, reason in (
        (official, EvidenceFact(), False, None),
        (official, EvidenceFact(True, None, ("b" * 64,), MORNING.isoformat()), None, "conflicting_evidence"),
        (EvidenceFact(False, None, ("a" * 64,), (close + timedelta(milliseconds=1)).isoformat()),
         EvidenceFact(), None, "unverified_st_observation"),
    ):
        inputs = {day: QuantInputEvidence(close.isoformat(), MappingProxyType({(SYMBOL, "is_st"): original}), "baseline")}
        official = observed
        projected = _project_regime_trial(history, inputs, archive, SYMBOL, DAY, {}, st_reader=st_reader)
        fact = inputs[day].fact(SYMBOL, "is_st")
        assert fact.value is expected and fact.reason == reason
        assert ("a" * 64 in fact.revision_ids) == (reason != "unverified_st_observation")
        assert _with_pit_values(projected, inputs, {"is_st"})["is_st"].to_list() == [expected]
        assert inputs[day].identity != "baseline"
    assert decision_times == [close, close, close]
