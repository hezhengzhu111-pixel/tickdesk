"""Build frozen v3 candidates from reviewed decisions and qualified quote evidence."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time
from decimal import Decimal

from app.services.tickdesk_adapter import (
    CN,
    _finite_decimal,
    _weighted_score,
    _wire_decimal,
    fingerprint,
)
from app.services.tickdesk_v3_decisions import evaluate_reviewed_daily


def _current_quote(row, cutoff, day):
    stamp, observed = row.get("market_timestamp"), row.get("observed_at")
    basis, source = row.get("timeBasis"), row.get("name")
    price = _finite_decimal(row.get("price"))
    if (type(stamp) is not int or type(observed) is not int or not 0 < stamp <= observed <= cutoff
            or cutoff - stamp > 90_000 or basis not in ("snapshot", "trade")
            or not isinstance(source, str) or not source.strip() or len(source) > 100
            or price is None or price <= 0 or _wire_decimal(price) is None):
        return None
    at = datetime.fromtimestamp(stamp / 1000, CN)
    if at.date() != day or not (time(9, 15) <= at.time() < time(11, 30) or time(13) <= at.time() < time(15)):
        return None
    amount = _finite_decimal(row.get("amount")) if row.get("amount_trade_date") == day.isoformat() else None
    return {
        "price": _wire_decimal(price), "name": source, "asOf": stamp, "observedAt": observed, "timeBasis": basis,
        "amount": _wire_decimal(amount) if amount is not None and amount >= 0 else None,
    }


def _strategy_outcome(rows, quotes, expected_day, side):
    reasons, selected = set(), []
    for row in rows:
        symbol = row["symbol"]
        bar = row["bar"]
        if (expected_day is None or bar["status"] != "complete"
                or datetime.fromtimestamp(bar["endMs"] / 1000, CN).date().isoformat() != expected_day):
            reasons.add("EXPECTED_DECISION_BAR_UNAVAILABLE")
        if "REVIEWED_UNIVERSE_INCOMPLETE" in row["blockingReasons"]:
            reasons.add("REVIEWED_UNIVERSE_INCOMPLETE")
        if row["matrixGate"]["value"] is None:
            reasons.add("REVIEWED_DIRECTION_UNAVAILABLE")
        # Buy execution still needs a current quote for price and turnover.
        # An exit is a rule fact over a completed bar; a missing quote makes it
        # restricted/unknown executable quantity, not "not computable".
        if side == "buy" and symbol not in quotes:
            reasons.add("CURRENT_EXECUTION_QUOTE_UNAVAILABLE")
        if row["matrixGate"]["value"] is not True:
            continue
        if side == "sell":
            # An exit is decided by its own rule and completed bar. The entry
            # ranking score is not a sell score, so it is neither required nor
            # reused here.
            if row["exit"]["value"] is not True:
                if row["exit"]["value"] is None:
                    reasons.add("EXIT_CONDITION_UNAVAILABLE")
                continue
            selected.append({"symbol": symbol, "score": None, "row": row})
            continue
        score = _finite_decimal(row["score"]["value"])
        if score is None or not 0 <= score <= 100:
            reasons.add("ENTRY_SCORE_UNVERIFIED")
        if symbol in quotes and quotes[symbol]["amount"] is None:
            reasons.add("CURRENT_TURNOVER_UNAVAILABLE")
        selected.append({"symbol": symbol, "score": score, "row": row})
    if reasons:
        return {"status": "not_computable", "reasons": sorted(reasons)}, []
    return {"status": "matched" if selected else "no_match", "reasons": []}, selected


def freeze_v3_scan(adapter, cutoff, approval, weights, policy, overrides):
    day = datetime.fromtimestamp(cutoff / 1000, CN).date()
    # Carry the verified group and rule version with the code, parameter and
    # dependency hashes. The local service matches holding partitions against
    # this full version, not just the strategy name.
    specs = [
        {**spec, "group": adapter.engine.get(spec["id"]).quant_contract.group, "ruleVersion": 1}
        for spec in approval["strategies"]
    ]
    symbols = {symbol for spec in specs for symbol in spec["symbols"]}
    generation = adapter.repo.get_matrix_data_generation("stock")
    original_definition_revision = adapter.engine.definition_revision
    quotes = {}
    for symbol in sorted(symbols):
        quote = _current_quote(adapter.execution_quotes.get(symbol, {}), cutoff, day)
        if quote is not None:
            quotes[symbol] = quote
    master = {item["symbol"]: item["name"] for item in adapter.symbols(cutoff)
              if item["symbol"] in symbols and item["supported"] and item["instrument"] == "stock"}
    expected_day = adapter._latest_completed_session(cutoff, day)
    reviewed = evaluate_reviewed_daily(adapter, cutoff, specs, overrides)
    rows = defaultdict(list)
    for row in reviewed["decisions"]:
        rows[row["strategyId"]].append(row)
    expected = sorted((spec["id"], spec["direction"], symbol) for spec in specs for symbol in spec["symbols"])
    actual = sorted((row["strategyId"], row["direction"], row["symbol"]) for row in reviewed["decisions"])
    if actual != expected or reviewed["decisionCutoffMs"] != cutoff:
        raise ValueError("reviewed strategy scope changed")

    outcomes = {}
    groups = defaultdict(list)
    successful_quotes = set()
    for spec in specs:
        sid, side = spec["id"], spec["direction"]
        outcomes[sid], selected = _strategy_outcome(rows[sid], quotes, expected_day, side)
        if outcomes[sid]["status"] != "not_computable":
            successful_quotes.update(spec["symbols"])
        for item in selected:
            groups[item["symbol"], side].append((sid, item["score"], weights[sid], item["row"]))

    failed = sorted(sid for sid, outcome in outcomes.items() if outcome["status"] == "not_computable")
    candidates = []
    exits = []
    for (symbol, side), hits in sorted(groups.items()):
        quote = quotes.get(symbol)
        if side == "sell":
            # One exit per security: several sell rules merge their evidence and
            # never add up to selling the same shares twice. The quote is only
            # executability evidence; its absence must not erase the exit fact.
            ids = sorted({sid for sid, _, _, _ in hits})
            row = hits[0][3]
            exits.append({
                "symbol": symbol, "name": master[symbol], "strategyId": ids[0], "strategyIds": ids,
                "ruleId": "matrix_daily_exit", "ruleVersion": 1,
                "condition": "已批准日线策略的独立退出条件成立",
                "decisionClock": "completed_daily_bar",
                "barEndMs": row["bar"]["endMs"], "barAvailableAtMs": row["bar"]["availableAtMs"],
                "quote": ({"price": quote["price"], "asOf": quote["asOf"],
                           "source": {"name": quote["name"], "ref": f"kernel:realtime:{symbol}",
                                      "asOf": quote["asOf"]}} if quote else None),
            })
            continue
        candidates.append({
            "candidate": {
                "symbol": symbol, "name": master[symbol],
                "score": _weighted_score([(sid, score, weight) for sid, score, weight, _ in hits]),
                "strategies": sorted(sid for sid, _, _, _ in hits), "changeRatio": None, "volumeRatio": None,
                "marketCap": None, "lastPrice": quote["price"], "dataAsOf": quote["asOf"], "riskStatus": "unknown",
            },
            "direction": side, "approved": True, "supported": True,
            "liquidityPassed": Decimal(quote["amount"]) >= Decimal(policy["minimumAmount"]),
            "source": {"name": quote["name"], "ref": f"kernel:realtime:{symbol}", "asOf": quote["asOf"]},
        })
    if (adapter.repo.get_matrix_data_generation("stock") != generation
            or adapter.engine.definition_revision != original_definition_revision
            or adapter._latest_completed_session(cutoff, day) != expected_day
            or (adapter.override_loader and fingerprint(adapter.override_loader()) != fingerprint(overrides))):
        raise ValueError("reviewed strategy inputs changed during evaluation")
    artifact = {
        "schemaVersion": 3, "inputs": {"stockGeneration": generation, "expectedDecisionDay": expected_day,
                                       "executionQuotes": quotes},
        "approvalHash": fingerprint(approval), "strategyHashes": specs, "parametersHash": fingerprint(overrides),
        "strategyOutcomes": outcomes, "researchDecisions": reviewed["decisions"],
    }
    frozen = fingerprint(artifact)
    return {
        "tradeDate": day.isoformat(), "isTradingDay": True, "generation": frozen, "generationAfter": frozen,
        "asOf": min((quotes[symbol]["asOf"] for symbol in successful_quotes if symbol in quotes), default=None),
        "strategies": {"total": len(specs), "succeeded": len(specs) - len(failed), "failed": failed},
        "policy": policy, "candidates": candidates, "exits": exits, "manifest": artifact,
    }
