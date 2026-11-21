import pytest
from tickdesk_quant_fixture import CUTOFF_MS, STRATEGY, STRATEGY_ID, SYMBOLS, build_fixture

from app.services import tickdesk_v3_decisions
from app.services.tickdesk_adapter import MarketAdapter, fingerprint
from app.services.tickdesk_research import research_sources
from app.services.tickdesk_v3_decisions import evaluate_reviewed_daily
from app.strategy.engine import StrategyEngine


def review(adapter, symbols, direction, overrides=None, *, strategy_id=STRATEGY_ID):
    engine = adapter.engine
    _, manifests = research_sources(engine, [strategy_id])
    overrides = overrides or {}
    parameters = engine.resolve_params(engine.get(strategy_id), None, overrides)
    return {
        "id": strategy_id, "symbols": symbols, "direction": direction,
        **manifests[strategy_id], "parametersHash": fingerprint({"params": parameters, "overrides": overrides}),
    }


def test_v3_reviewed_direction_uses_matrix_witness_and_only_reviewed_symbols(tmp_path):
    api = build_fixture(tmp_path / "kernel")
    repo = api.state.repo
    adapter = MarketAdapter(repo, api.state.strategy_engine)
    try:
        exit_only = evaluate_reviewed_daily(adapter, CUTOFF_MS, [review(adapter, [SYMBOLS[3]], "sell")], {})
        [exit_row] = exit_only["decisions"]
        assert exit_row["symbol"] == SYMBOLS[3] and exit_row["direction"] == "sell"
        assert exit_row["entry"]["value"] is None and exit_row["exit"]["value"] is True
        assert exit_row["decision"]["value"] is True and exit_row["matrixGate"]["value"] is True
        assert exit_row["score"]["value"] is None
        assert exit_row["inputHash"] and exit_row["evidence"]["decisionInput"]
        # The exit has its own rule and completed bar; it is not blocked for
        # lacking an entry ranking score.
        assert exit_row["blockingReasons"] == ["CURRENT_EXECUTION_EVIDENCE_UNAVAILABLE"]
        assert exit_row["bar"]["endMs"] <= exit_row["bar"]["availableAtMs"] <= CUTOFF_MS
        assert exit_row["quote"] is None and exit_row["publicationEligible"] is False

        symbols = [SYMBOLS[0], SYMBOLS[2], SYMBOLS[3], "000005.SZ"]
        buy = evaluate_reviewed_daily(adapter, CUTOFF_MS, [review(adapter, symbols, "buy")], {})
        decisions = {row["symbol"]: row for row in buy["decisions"]}
        assert set(decisions) == set(symbols)
        assert decisions[SYMBOLS[0]]["decision"]["value"] is True
        assert decisions[SYMBOLS[0]]["matrixGate"]["value"] is True
        assert decisions[SYMBOLS[2]]["decision"]["value"] is False
        assert decisions[SYMBOLS[2]]["matrixGate"]["value"] is False
        assert decisions[SYMBOLS[3]]["decision"]["value"] is None
        assert decisions[SYMBOLS[3]]["matrixGate"]["value"] is None
        assert decisions["000005.SZ"]["decision"]["reason"] == "REVIEWED_SYMBOL_UNCONFIRMED"
        assert "REVIEWED_UNIVERSE_INCOMPLETE" in decisions[SYMBOLS[0]]["blockingReasons"]
        assert all("CURRENT_EXECUTION_EVIDENCE_UNAVAILABLE" in row["blockingReasons"] for row in decisions.values())
        assert all(row["publicationEligible"] is False for row in decisions.values())
    finally:
        repo.store.db.close()


def test_v3_condition_true_outside_matrix_ranking_remains_unselected(tmp_path):
    api = build_fixture(tmp_path / "kernel")
    repo = api.state.repo
    adapter = MarketAdapter(repo, api.state.strategy_engine)
    overrides = {"display_limit": 1}
    try:
        spec = review(adapter, SYMBOLS, "buy", overrides)
        result = evaluate_reviewed_daily(adapter, CUTOFF_MS, [spec], {STRATEGY_ID: overrides})
        rows = {row["symbol"]: row for row in result["decisions"]}
        assert rows[SYMBOLS[0]]["decision"]["value"] is True
        assert rows[SYMBOLS[0]]["matrixGate"] == {"value": False, "reason": "DIRECTION_NOT_SELECTED_BY_MATRIX"}
        assert rows[SYMBOLS[1]]["decision"]["value"] is True
        assert rows[SYMBOLS[1]]["matrixGate"]["value"] is True
    finally:
        repo.store.db.close()


def test_v3_reviewed_decisions_require_exact_identity_and_use_completed_bars(tmp_path):
    api = build_fixture(tmp_path / "kernel")
    repo = api.state.repo
    adapter = MarketAdapter(repo, api.state.strategy_engine)
    try:
        spec = review(adapter, [SYMBOLS[3]], "sell")
        earlier = CUTOFF_MS - 71 * 60_000
        previous = evaluate_reviewed_daily(adapter, earlier, [spec], {})
        [row] = previous["decisions"]
        assert row["bar"]["availableAtMs"] <= earlier
        assert row["bar"]["endMs"] < CUTOFF_MS - 24 * 3_600_000
        assert row["evidence"]["barRevision"]
        with pytest.raises(ValueError, match="reviewed strategy identity changed"):
            evaluate_reviewed_daily(adapter, CUTOFF_MS, [{**spec, "parametersHash": "0" * 64}], {})
        with pytest.raises(ValueError, match="duplicate reviewed strategy"):
            evaluate_reviewed_daily(adapter, CUTOFF_MS, [spec, spec], {})
    finally:
        repo.store.db.close()


def test_v3_freeze_groups_only_identical_ordered_review_universes(tmp_path, monkeypatch):
    root = tmp_path / "kernel"
    api = build_fixture(root)
    repo = api.state.repo
    strategies = root / "strategies"
    same, reordered, incomplete = "synthetic_same", "synthetic_reordered", "synthetic_incomplete"
    for sid in (same, reordered, incomplete):
        (strategies / f"{sid}.py").write_text(STRATEGY.replace(STRATEGY_ID, sid), encoding="utf-8")
    adapter = MarketAdapter(repo, StrategyEngine([strategies]))
    pool = [SYMBOLS[0], SYMBOLS[3]]
    unresolved = "000005.SZ"
    specs = [review(adapter, pool, "buy"),
             review(adapter, list(reversed(pool)), "buy", strategy_id=reordered),
             review(adapter, pool, "sell", strategy_id=same),
             review(adapter, [*pool, unresolved], "buy", strategy_id=incomplete)]
    scans = []
    engine = tickdesk_v3_decisions.scan_research

    def scan(adapter, request, config, *args, **kwargs):
        scans.append((config["strategies"], config["symbols"]))
        return engine(adapter, request, config, *args, **kwargs)

    monkeypatch.setattr(tickdesk_v3_decisions, "scan_research", scan)
    try:
        rows = evaluate_reviewed_daily(adapter, CUTOFF_MS, specs, {})["decisions"]
        assert scans == [([STRATEGY_ID, same], pool), ([reordered], pool), ([incomplete], pool)]
        assert list(dict.fromkeys(row["strategyId"] for row in rows)) == [spec["id"] for spec in specs]
        by_key = {(row["strategyId"], row["symbol"]): row for row in rows}
        assert by_key[STRATEGY_ID, SYMBOLS[0]]["decision"]["value"] is True
        assert by_key[same, SYMBOLS[0]]["decision"]["value"] is False
        assert by_key[same, SYMBOLS[3]]["matrixGate"]["value"] is True
        assert by_key[incomplete, unresolved]["decision"]["value"] is None
        assert "REVIEWED_UNIVERSE_INCOMPLETE" not in by_key[same, SYMBOLS[0]]["blockingReasons"]
        assert "REVIEWED_UNIVERSE_INCOMPLETE" in by_key[incomplete, SYMBOLS[0]]["blockingReasons"]
    finally:
        repo.store.db.close()
