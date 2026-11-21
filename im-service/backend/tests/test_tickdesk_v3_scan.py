from datetime import datetime

from tickdesk_quant_fixture import STRATEGY_ID, SYMBOLS, build_fixture

from app.services.tickdesk_adapter import MarketAdapter, fingerprint, timestamp
from app.services.tickdesk_admission import strategy_sources

NOW = timestamp(datetime.fromisoformat("2026-01-05T10:00:00+08:00"))


def approved_scan(api, monkeypatch, symbols, direction, execution_quotes):
    engine = api.state.strategy_engine
    definition = engine.get(STRATEGY_ID)
    sources, code, helpers = strategy_sources(definition)
    parameters = engine.resolve_params(definition, None, {})
    approval = {
        "version": 3, "profiles": {"conservative": {"minScore": "0", "minimumAmount": "500"}},
        "strategies": [{
            "id": STRATEGY_ID, "symbols": symbols, "direction": direction, "weight": "1", "codeHash": code,
            "dependenciesHash": helpers, "parametersHash": fingerprint({"params": parameters, "overrides": {}}),
        }],
    }
    from decimal import Decimal

    monkeypatch.setattr("app.services.tickdesk_admission.validate_approval",
                        lambda *_a, **_kw: {STRATEGY_ID: (definition, Decimal("1"), sources)})
    adapter = MarketAdapter(api.state.repo, engine, calendar=lambda _: True,
                            override_loader=dict, execution_quotes=execution_quotes)
    monkeypatch.setattr(adapter, "_latest_completed_session", lambda *_: "2026-01-02")
    return adapter.scan({"requestedAt": NOW, "profile": "conservative"}, approval)


def _quote(**changes):
    return {"name": "fixture supplier", "price": "14.25", "market_timestamp": NOW - 2000,
            "observed_at": NOW - 1000, "timeBasis": "trade", "amount_trade_date": "2026-01-05",
            "amount": "750.50", **changes}


def test_v3_buy_uses_approved_scope_real_matrix_score_and_separate_quote_clock(tmp_path, monkeypatch):
    api = build_fixture(tmp_path / "kernel")
    try:
        result = approved_scan(api, monkeypatch, [SYMBOLS[0]], "buy", {
            SYMBOLS[0]: _quote(), SYMBOLS[1]: _quote(price="1", amount="500000"),
        })
        assert result["strategies"] == {"total": 1, "succeeded": 1, "failed": []}
        [raw] = result["candidates"]
        candidate = raw["candidate"]
        assert candidate["symbol"] == SYMBOLS[0] and candidate["lastPrice"] == "14.25"
        assert candidate["dataAsOf"] == raw["source"]["asOf"] == result["asOf"] == NOW - 2000
        assert candidate["strategies"] == [STRATEGY_ID] and raw["direction"] == "buy"
        assert raw["liquidityPassed"] and raw["source"]["name"] == "fixture supplier"
        assert result["manifest"]["strategyOutcomes"][STRATEGY_ID]["status"] == "matched"
    finally:
        api.state.repo.store.db.close()


def test_v3_unavailable_execution_proof_is_not_a_successful_empty_scan(tmp_path, monkeypatch):
    api = build_fixture(tmp_path / "kernel")
    try:
        result = approved_scan(api, monkeypatch, [SYMBOLS[0]], "buy", {
            SYMBOLS[0]: _quote(amount_trade_date="2026-01-02"),
        })
        assert result["candidates"] == [] and result["strategies"]["failed"] == [STRATEGY_ID]
        assert "CURRENT_TURNOVER_UNAVAILABLE" in result["manifest"]["strategyOutcomes"][STRATEGY_ID]["reasons"]

        result = approved_scan(api, monkeypatch, [SYMBOLS[0]], "buy", {
            SYMBOLS[0]: _quote(observed_at=NOW + 1),
        })
        assert result["asOf"] is None and result["strategies"]["succeeded"] == 0
        assert "CURRENT_EXECUTION_QUOTE_UNAVAILABLE" in result["manifest"]["strategyOutcomes"][STRATEGY_ID]["reasons"]
    finally:
        api.state.repo.store.db.close()


def test_v3_known_empty_and_rule_based_exit_stay_distinct(tmp_path, monkeypatch):
    api = build_fixture(tmp_path / "kernel")
    try:
        no_match = approved_scan(api, monkeypatch, [SYMBOLS[2]], "buy", {SYMBOLS[2]: _quote()})
        assert no_match["manifest"]["strategyOutcomes"][STRATEGY_ID]["status"] == "no_match"
        assert no_match["candidates"] == [] and no_match["strategies"]["succeeded"] == 1
        no_exit = approved_scan(api, monkeypatch, [SYMBOLS[0]], "sell", {SYMBOLS[0]: _quote()})
        assert no_exit["manifest"]["strategyOutcomes"][STRATEGY_ID]["status"] == "no_match"
        assert no_exit["exits"] == [] and no_exit["candidates"] == []
        assert no_exit["strategies"]["succeeded"] == 1
        exit_met = approved_scan(api, monkeypatch, [SYMBOLS[3]], "sell", {SYMBOLS[3]: _quote()})
        assert exit_met["manifest"]["strategyOutcomes"][STRATEGY_ID]["status"] == "matched"
        assert exit_met["candidates"] == []
        [exit_signal] = exit_met["exits"]
        assert exit_met["manifest"]["strategyHashes"][0]["group"] == "daily_swing"
        assert exit_met["manifest"]["strategyHashes"][0]["ruleVersion"] == 1
        assert exit_signal["symbol"] == SYMBOLS[3]
        assert exit_signal["strategyIds"] == [STRATEGY_ID]
        assert exit_signal["ruleId"] == "matrix_daily_exit" and exit_signal["ruleVersion"] == 1
        assert exit_signal["decisionClock"] == "completed_daily_bar"
        assert exit_signal["barEndMs"] <= exit_signal["barAvailableAtMs"]
        assert "EXIT_SCORE_UNVERIFIED" not in exit_met["manifest"]["strategyOutcomes"][STRATEGY_ID]["reasons"]
    finally:
        api.state.repo.store.db.close()


def test_v3_exit_fact_survives_missing_execution_quote(tmp_path, monkeypatch):
    api = build_fixture(tmp_path / "kernel")
    try:
        # No quote at all: the exit rule still matched; executability is a
        # separate concern for the local service.
        result = approved_scan(api, monkeypatch, [SYMBOLS[3]], "sell", {})
        assert result["manifest"]["strategyOutcomes"][STRATEGY_ID]["status"] == "matched"
        assert result["strategies"]["failed"] == []
        [exit_signal] = result["exits"]
        assert exit_signal["symbol"] == SYMBOLS[3]
        assert exit_signal["quote"] is None
        assert result["asOf"] is None
        assert "CURRENT_EXECUTION_QUOTE_UNAVAILABLE" not in result["manifest"]["strategyOutcomes"][STRATEGY_ID]["reasons"]
    finally:
        api.state.repo.store.db.close()
