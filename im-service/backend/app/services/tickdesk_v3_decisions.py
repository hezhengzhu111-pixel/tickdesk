"""Evaluate reviewed daily v3 scope without producing actionable market candidates."""
from __future__ import annotations

import re

from app.services.tickdesk_adapter import fingerprint
from app.services.tickdesk_research import research_data_identity, research_sources, scan_research

_SYMBOL = re.compile(r"[0-9]{6}\.(?:SZ|SH)")


def evaluate_reviewed_daily(adapter, requested_at: int, specs: list[dict], overrides: dict) -> dict:
    """Read the frozen v3 directional signals at the requested market cutoff.

    Specs identify the reviewed sources, settings, direction and security pool.
    Caller still needs production admission and independently verified execution data.
    """
    if type(requested_at) is not int or requested_at <= 0 or not specs:
        raise ValueError("reviewed strategy requires a decision cutoff and scope")
    strategy_ids = [spec["id"] for spec in specs]
    if len(set(strategy_ids)) != len(strategy_ids):
        raise ValueError("duplicate reviewed strategy")
    symbol_count = 0
    pools = {}
    for spec in specs:
        symbols = spec.get("symbols")
        if (spec.get("direction") not in ("buy", "sell") or not isinstance(symbols, list) or not symbols
                or any(not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol) for symbol in symbols)
                or len(set(symbols)) != len(symbols)):
            raise ValueError("invalid reviewed strategy scope")
        symbol_count += len(symbols)
        pools.setdefault(tuple(symbols), []).append(spec["id"])
    if symbol_count > 500:
        raise ValueError("reviewed strategy observation limit")

    engine = adapter.engine
    sources, manifests = research_sources(engine, strategy_ids)
    for spec in specs:
        sid = spec["id"]
        definition = engine.get(sid)
        contract = definition.quant_contract
        if (contract.group != "daily_swing" or contract.decision_timeframe != "1d"
                or definition.execution_backend != "matrix_native"
                or "stock" not in definition.meta.get("asset_types", ["stock"])):
            raise ValueError("reviewed strategy outside daily swing scope")
        strategy_overrides = overrides.get(sid, {})
        params = engine.resolve_params(definition, None, strategy_overrides)
        if (spec.get("codeHash") != manifests[sid]["codeHash"]
                or spec.get("dependenciesHash") != manifests[sid]["dependenciesHash"]
                or spec.get("parametersHash") != fingerprint({"params": params, "overrides": strategy_overrides})):
            raise ValueError("reviewed strategy identity changed")

    combined = {"version": 3, "strategies": strategy_ids,
                "symbols": sorted({symbol for spec in specs for symbol in spec["symbols"]})}
    planned = set(combined["symbols"])
    current_scope = _supported_stock_scope(adapter, requested_at, planned)
    before = research_data_identity(adapter, combined, requested_at)
    executions = {}

    def capture_execution(strategy_id, asset, result):
        executions[strategy_id, asset] = result

    observations = {sid: {} for sid in strategy_ids}
    for reviewed_symbols, ids in pools.items():
        pool = sorted(current_scope.intersection(reviewed_symbols))
        if not pool:
            continue
        config = {"version": 3, "strategies": ids, "symbols": pool}
        evaluated = scan_research(adapter, {"requestedAt": requested_at}, config, manifests, overrides,
                                  capture_execution=capture_execution)
        for row in evaluated["observations"]:
            observations[row["strategyId"]][row["symbol"]] = row

    decisions = []
    for spec in specs:
        sid = spec["id"]
        contract = engine.get(sid).quant_contract
        reviewed = set(spec["symbols"])
        strategy_observations = observations[sid]
        complete_scope = reviewed == {symbol for symbol, row in strategy_observations.items()
                                      if row["bar"]["status"] == "complete"}
        result = executions.get((sid, "stock"))
        entries = ({row["symbol"] for row in result.rows}
                   & {hit["symbol"] for hit in result.entry_signal_hits}) if result is not None else set()
        exits = {hit["symbol"] for hit in result.exit_signal_hits} if result is not None else set()
        for symbol in sorted(reviewed):
            decisions.append(_directional_row(spec, symbol, strategy_observations.get(symbol), entries, exits,
                                              complete_scope, contract.scoring_scope))
    if (_supported_stock_scope(adapter, requested_at, planned) != current_scope
            or research_data_identity(adapter, combined, requested_at) != before
            or research_sources(engine, strategy_ids) != (sources, manifests)):
        raise ValueError("reviewed strategy inputs changed during evaluation")
    return {"version": 3, "stage": "review", "decisionCutoffMs": requested_at, "decisions": decisions}


def _supported_stock_scope(adapter, requested_at, planned):
    return {item["symbol"] for item in adapter.symbols(requested_at)
            if item["symbol"] in planned and item["supported"] and item["instrument"] == "stock"}


def _directional_row(spec, symbol, observation, entries, exits, complete_scope, scoring_scope):
    missing = {"value": None, "reason": "REVIEWED_SYMBOL_UNCONFIRMED"}
    row = observation or {
        "entry": missing, "exit": missing, "score": {**missing, "scope": scoring_scope},
        "bar": {"status": "unknown", "endMs": None, "availableAtMs": None}, "evidence": {},
    }
    buy = spec["direction"] == "buy"
    decision = row["entry"] if buy else row["exit"]
    selected = symbol in (entries if buy else exits)
    decision_value = decision["value"]
    if decision_value is True:
        gate = {"value": selected, "reason": None if selected else "DIRECTION_NOT_SELECTED_BY_MATRIX"}
    else:
        gate = decision
    blocking = ["CURRENT_EXECUTION_EVIDENCE_UNAVAILABLE"]
    if not complete_scope:
        blocking.append("REVIEWED_UNIVERSE_INCOMPLETE")
    if decision_value is True:
        if buy and row["score"]["value"] is None:
            blocking.append("ENTRY_SCORE_UNVERIFIED")
    return {
        "strategyId": spec["id"], "symbol": symbol, "direction": spec["direction"],
        "codeHash": spec["codeHash"], "dependenciesHash": spec["dependenciesHash"],
        "parametersHash": spec["parametersHash"], "inputHash": row.get("inputHash"),
        "entry": row["entry"], "exit": row["exit"], "decision": decision, "matrixGate": gate,
        "score": row["score"], "bar": row["bar"], "evidence": row["evidence"],
        "quote": None, "publicationEligible": False, "blockingReasons": blocking,
    }
