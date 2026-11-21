"""Version 3 research observations through the existing screener and strategy engine."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from types import MappingProxyType

import polars as pl

from app.quant_evidence import (
    LISTING_SESSIONS_LOWER_BOUND,
    REGIME_FIELDS,
    merge_st_evidence,
    regime_evidence_fact,
    required_quant_fields,
)
from app.services.tickdesk_adapter import CN, _wire_decimal, fingerprint, timestamp

BAR_COLUMNS = ("open", "high", "low", "close", "raw_high", "raw_low", "raw_close", "volume", "amount")
FINALITY = "daily_bar_finality"


def strategy_directories(root):
    research = Path(__file__).resolve().parents[1] / "strategy" / "research"
    return [research, research / "baselines", *[Path(root) / "strategies" / kind
                                               for kind in ("custom", "ai", "composite")]]


def read_research_config(root):
    path = root / "tickdesk_research.json"
    if not path.exists():
        return None
    if path.stat().st_size > 65536:
        raise ValueError("research configuration limit")
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or set(config) != {"version", "strategies", "symbols"} or config["version"] != 3:
        raise ValueError("invalid research configuration")
    for name, pattern, limit in (("strategies", r"[a-zA-Z0-9_-]{1,64}", 25), ("symbols", r"[0-9]{6}\.(SZ|SH)", 500)):
        items = config[name]
        if (not isinstance(items, list) or not 0 < len(items) <= limit
                or any(not isinstance(item, str) or not re.fullmatch(pattern, item) for item in items)
                or len(set(items)) != len(items)):
            raise ValueError("invalid research selection")
    return config


def research_sources(engine, strategy_ids):
    sources, manifests = {}, {}
    for strategy_id in strategy_ids:
        definition = engine.get(strategy_id)
        path = definition.file_path
        if path is None or definition.quant_contract is None or definition.source == "ai":
            raise ValueError("explicit v3 research source required")
        code = path.read_bytes()
        helpers = {helper: helper.read_bytes() for helper in sorted(path.parent.glob("_*.py"))}
        sources.update({path: code, **helpers})
        manifests[strategy_id] = {
            "codeHash": fingerprint(code),
            "dependenciesHash": fingerprint({helper.name: fingerprint(body) for helper, body in helpers.items()}),
        }
    return sources, manifests


def _verified_row(row, witnesses):
    day, symbol = str(row["date"]), row["symbol"]
    evidence = witnesses.get((day, symbol))
    if evidence is None:
        return None
    try:
        digest = fingerprint([day, symbol, *[float(row[col]) for col in BAR_COLUMNS]])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return evidence if evidence["value"]["bar_hash"] == digest else None


def _witnesses(snapshot, cutoff):
    # One exact-day witness per security. A contradictory latest revision is
    # withheld; a delayed final bar cannot be admitted before its arrival.
    selected = {}
    for row in snapshot.reference_rows:
        if row["field"] != FINALITY or timestamp(row["available_at"]) > cutoff:
            continue
        key = row["effective_date"], row["symbol"]
        old = selected.get(key)
        if old is None or row["available_at"] > old[0]:
            selected[key] = row["available_at"], [row]
        elif row["available_at"] == old[0]:
            old[1].append(row)
    return {
        key: rows[0] for key, (_, rows) in selected.items()
        if rows[0]["value"] is not None and all(row["value"] == rows[0]["value"] for row in rows)
    }


def research_data_identity(adapter, config, cutoff):
    """Observed input identity rather than the five-minute request timestamp."""
    repo = adapter.repo
    pool = set(config["symbols"])
    master = sorted((item["symbol"], item["instrument"]) for item in adapter.symbols(cutoff) if item["symbol"] in pool)
    fields = {FINALITY, LISTING_SESSIONS_LOWER_BOUND, *REGIME_FIELDS}
    if any(symbol.endswith((".SZ", ".SH")) for symbol in pool):
        fields.add("is_st")
    snapshot = repo.get_quant_evidence_snapshot(fields)
    evidence = [r["revision_id"] for r in snapshot.reference_rows
                if r["field"] != FINALITY or timestamp(r["available_at"]) <= cutoff]
    root = repo.store.data_dir
    financial_digests = {}
    for dataset in ("metrics", "shares"):
        try:
            with (root / "financials" / dataset / "part.parquet").open("rb") as stream:
                financial_digests[dataset] = hashlib.file_digest(stream, "sha256").hexdigest()
        except FileNotFoundError:
            financial_digests[dataset] = None
    return fingerprint({
        "master": master, "evidence": sorted(evidence), "referenceState": snapshot.reference_failure,
        "szseStManifests": dict(snapshot.szse_st_manifests),
        **({"sseStManifests": dict(snapshot.sse_st_manifests)} if snapshot.sse_st_manifests else {}),
        **({"listingRosters": dict(snapshot.listing_roster_revisions)} if snapshot.listing_roster_revisions else {}),
        "regimeRevision": snapshot.regime_revision,
        "regimeState": snapshot.regime_failure,
        "financials": financial_digests,
        "generation": {asset: repo.get_matrix_data_generation(asset) for asset in sorted({kind for _, kind in master})},
    })


def _unavailable(reason, scope):
    return {
        "entry": {"value": None, "reason": reason},
        "exit": {"value": None, "reason": reason},
        "score": {"value": None, "reason": reason, "scope": scope},
    }


def _project_regime_trial(history, inputs, archive, symbol, day, cache, *, st_reader=None):
    from app.services.tickdesk_regime_archive import replay

    key = str(day)
    original = inputs[key]
    if original.cutoff not in cache:
        cutoff = datetime.fromisoformat(original.cutoff)
        regimes = replay(archive, symbol, cutoff)
        observed_st = st_reader(symbol, cutoff) if st_reader is not None else None
        cache[original.cutoff] = regimes, observed_st
    verified, observed_st = cache[original.cutoff]

    resolved = {field: regime_evidence_fact(trial) for field, trial in verified.items()}
    if observed_st is not None:
        resolved["is_st"] = merge_st_evidence(original.fact(symbol, "is_st"), observed_st,
                                              datetime.fromisoformat(original.cutoff))
    facts = {**original.facts, **{(symbol, field): fact for field, fact in resolved.items()}}
    identity = fingerprint([original.identity, [[name, fact.value, fact.reason, fact.revision_ids, fact.available_at]
                                                  for name, fact in sorted(resolved.items())]])
    inputs[key] = replace(original, facts=MappingProxyType(facts), identity=identity)
    # An explicit trial uses only those two sourced inputs at this exact close.
    return history.with_columns([
        pl.when(pl.col("date") == day).then(pl.lit(trial.value, dtype=pl.Float64)).otherwise(None).alias(field)
        for field, trial in verified.items()
    ])


def scan_research(adapter, request, config, manifests, overrides, *, capture=None, capture_execution=None,
                  regime_archive=None, st_reader=None):
    from decimal import Decimal

    from app.services.screener import ScreenerService

    cutoff = request["requestedAt"]
    repo = adapter.repo
    symbols = set(config["symbols"])
    if st_reader is not None and regime_archive is None:
        raise ValueError("ST evidence is only consumed as part of a regime trial")
    if regime_archive is not None and len(symbols) != 1:
        raise ValueError("regime trial requires exactly one security")
    regime_inputs = {}
    master = {item["symbol"]: item["instrument"] for item in adapter.symbols(cutoff) if item["symbol"] in symbols}
    assets = sorted(set(master.values()))
    generation = {asset: repo.get_matrix_data_generation(asset) for asset in assets}
    plans = []
    fields = {FINALITY}
    for strategy_id in config["strategies"]:
        definition = adapter.engine.get(strategy_id)
        contract = definition.quant_contract
        if contract is None or contract.decision_timeframe != "1d":
            raise ValueError("research strategy requires the daily v3 contract")
        strategy_overrides = overrides.get(strategy_id, {})
        params = adapter.engine.resolve_params(definition, None, strategy_overrides)
        data_fields = required_quant_fields(definition, strategy_overrides)
        fields.update(data_fields)
        plans.append((strategy_id, definition, contract, strategy_overrides, params, data_fields))
    snapshot = repo.get_quant_evidence_snapshot(fields)
    witnesses = _witnesses(snapshot, cutoff)
    current_frames = {}
    observations = []
    for strategy_id, definition, contract, strategy_overrides, params, data_fields in plans:
        for asset in assets:
            if asset not in definition.meta.get("asset_types", ["stock"]):
                continue
            pool = sorted(symbol for symbol, instrument in master.items() if instrument == asset)
            pool_set = set(pool)
            latest = max((stamp for (stamp, symbol) in witnesses if symbol in pool_set), default=None)
            day = date.fromisoformat(latest) if latest is not None else datetime.fromtimestamp(cutoff / 1000, CN).date()
            context = ScreenerService(repo, asset_type=asset).build_strategy_context(
                adapter.engine, day, [strategy_id], params_map={strategy_id: params},
                overrides_map={strategy_id: strategy_overrides}, include_quant_evidence=False,
                current=current_frames.get(asset),
            )
            current_frames[asset] = context.current
            history = context.history if context.history is not None else context.current
            current = context.current
            confirmed = {}
            results = {}
            inputs = {}
            history_hash = None
            if current is not None and not current.is_empty() and history is not None and not history.is_empty():
                selection = history.filter(
                    (pl.col("date") <= day) & pl.col("symbol").is_in(pool),
                ).sort(["date", "symbol"])
                clocks = {}
                for row in selection.iter_rows(named=True):
                    witness = _verified_row(row, witnesses)
                    if witness is not None:
                        clocks[(row["date"], row["symbol"])] = witness
                # An incomplete input anywhere in a security's supplied history
                # cannot become a shorter, apparently complete indicator window.
                unsafe = {r["symbol"] for r in selection.select("date", "symbol").iter_rows(named=True)
                          if (r["date"], r["symbol"]) not in clocks}
                confirmed = {symbol: clocks[(day, symbol)] for symbol in pool
                             if symbol not in unsafe and (day, symbol) in clocks}
                if confirmed:
                    history = selection.filter(pl.col("symbol").is_in(confirmed))
                    for batch in history.partition_by("date", maintain_order=True):
                        decision_day = batch["date"][0]
                        prices = dict(batch.select("symbol", "raw_close").iter_rows())
                        # Financial and reference snapshots for a completed
                        # close use the decision bar's market timestamp.
                        end = min(clocks[(decision_day, symbol)]["value"]["end_at"] for symbol in prices)
                        inputs[str(decision_day)] = repo.get_quant_input_evidence(
                            datetime.fromisoformat(end), list(prices), data_fields,
                            raw_prices=prices, snapshot=snapshot,
                        )
                    if regime_archive is not None:
                        history = _project_regime_trial(
                            history, inputs, regime_archive, pool[0], day, regime_inputs, st_reader=st_reader,
                        )
                    current = history.filter(pl.col("date") == day)
                    sealed = replace(
                        context, current=current, history=history, quant_inputs=inputs,
                        current_quant_inputs=inputs, market=None, market_quant_inputs={},
                        quant_evidence=inputs[str(day)], evidence_identity=inputs[str(day)].identity,
                    )
                    history_hash = fingerprint(history.to_dicts())
                    archived_result = None
                    try:
                        engine_result = adapter.engine.run(
                            strategy_id, sealed, pool=list(confirmed), params=params, overrides=strategy_overrides,
                        )
                        results = engine_result.quant_observations
                    except Exception:  # noqa: BLE001 -- preserve failure state without exposing source paths
                        pass
                    else:
                        archived_result = results
                        if capture_execution is not None:
                            capture_execution(strategy_id, asset, engine_result)
                    if capture is not None:
                        bar_proofs = [clocks[key] for key in history.select("date", "symbol").iter_rows()]
                        capture(strategy_id, sealed, list(confirmed), params, strategy_overrides,
                                archived_result, bar_proofs)
            input_hash = fingerprint({
                "generation": generation[asset], "assets": pool, "history": history_hash,
                "evidence": {key: value.identity for key, value in inputs.items()},
                "bars": [value["revision_id"] for value in confirmed.values()],
            })
            for symbol in pool:
                witness = confirmed.get(symbol)
                if witness is None:
                    bar = {"status": "unknown", "endMs": None, "availableAtMs": None}
                    verdict = _unavailable("complete_bar_unavailable", contract.scoring_scope)
                    evidence = {}
                else:
                    bar = {"status": "complete", "endMs": timestamp(witness["value"]["end_at"]),
                           "availableAtMs": timestamp(witness["available_at"])}
                    verdict = results.get(symbol) or _unavailable("strategy_evaluation_unavailable", contract.scoring_scope)
                    score = verdict["score"]["value"]
                    if score is not None:
                        rendered = _wire_decimal(Decimal(str(score)))
                        verdict = {**verdict, "score": {**verdict["score"], "value": rendered,
                                   "reason": None if rendered is not None else "score_outside_wire_precision"}}
                    decision = inputs[str(day)]
                    evidence = {"barSource": witness["source"], "barRevision": witness["revision_id"],
                                "decisionInput": decision.identity}
                    regimes = {}
                    for field in sorted(data_fields & REGIME_FIELDS):
                        fact = decision.fact(symbol, field)
                        if fact.reason is None and fact.available_at and len(fact.revision_ids) == 2:
                            regimes[field] = {"value": fact.value, "availableAtMs": timestamp(fact.available_at),
                                              "ruleId": fact.revision_ids[0], "sha256": fact.revision_ids[1]}
                        elif fact.reason is not None:
                            regimes[field] = {"value": None, "reason": fact.reason}
                    if regimes:
                        evidence["regime"] = regimes
                observations.append({
                    "symbol": symbol, "strategyId": strategy_id, "group": contract.group,
                    "timeframe": contract.decision_timeframe, **manifests[strategy_id],
                    "parametersHash": fingerprint({"params": params, "overrides": strategy_overrides}),
                    "inputHash": input_hash, "rulesHash": None, "admission": "research",
                    "bar": bar, "quote": None, "evidence": evidence, **verdict,
                })
                if len(observations) > 500:
                    raise ValueError("research observation limit")
    if repo.get_quant_evidence_snapshot(fields) != snapshot:
        raise ValueError("historical evidence changed during evaluation")
    if generation != {asset: repo.get_matrix_data_generation(asset) for asset in assets}:
        raise ValueError("market generation changed during evaluation")
    return {"version": 3, "decisionCutoffMs": cutoff, "observations": observations}
