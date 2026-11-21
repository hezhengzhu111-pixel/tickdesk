"""Point-in-time inputs and phase validity for the existing matrix pipeline."""
from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType

import numpy as np
import polars as pl

from app.backtest.matrix import (
    build_pipeline_filter_mask,
    make_signal_matrix,
    matrix_feature,
    validate_signal_matrix,
)
from app.factors.registry import factor_calculation_order, factor_dependencies, get_factor
from app.quant_evidence import LISTING_SESSIONS_LOWER_BOUND, PIT_FIELDS
from app.strategy.scoring import complete_pool_frame_score, complete_pool_score


def _value(market, name):
    try:
        return matrix_feature(market, name)
    except ValueError:
        return np.full(market.shape, np.nan, dtype=np.float32)


def _strict_market(market, inputs, required):
    values = {
        name: arr for name, arr in market.fields.items()
        if name not in PIT_FIELDS and factor_dependencies((name,)).isdisjoint(PIT_FIELDS)
    }
    evidence_fields = PIT_FIELDS.intersection(set(market.fields) | factor_dependencies(required) | required)
    derives_turnover = "turnover_rate" in evidence_fields
    if derives_turnover:
        evidence_fields.add("float_shares")
    for name in evidence_fields:
        arr = np.full(market.shape, np.nan, dtype=np.float32)
        for row, stamp in enumerate(market.timestamp_labels):
            evidence = inputs.get(stamp)
            if evidence is None:
                continue
            for col, symbol in enumerate(market.symbols):
                fact = evidence.fact(symbol, name)
                if name == LISTING_SESSIONS_LOWER_BOUND and fact.reason == "not_requested":
                    fact = evidence.fact(symbol, "listing_sessions")
                if fact.reason is None and type(fact.value) in (int, float, bool):
                    arr[row, col] = fact.value
        arr.flags.writeable = False
        values[name] = arr
    if derives_turnover:
        shares = values["float_shares"]
        turnover = np.full(market.shape, np.nan, dtype=np.float32)
        np.divide(market.volume * 10_000.0, shares, out=turnover, where=shares > 0)
        turnover.flags.writeable = False
        values["turnover_rate"] = turnover
    strict = replace(market, fields=MappingProxyType(values))
    # A plugin's field() and matrix_feature() must see the same rebuilt
    # dependency closure. Unsupported dependencies become absent observations.
    for name in required - {"open", "high", "low", "close", "volume"} - values.keys():
        arr = _value(strict, name)
        arr.flags.writeable = False
        values[name] = arr
    return strict


def _availability(market, required, inputs):
    available = np.isfinite(market.close)
    reasons = np.full(market.shape, "", dtype=object)
    reasons[~available] = "missing_bar"
    for name in sorted(required):
        finite = np.isfinite(_value(market, name))
        reasons[available & ~finite] = f"missing_field:{name}"
        if name in PIT_FIELDS:
            for row, col in zip(*np.nonzero(available & ~finite), strict=True):
                evidence = inputs.get(market.timestamp_labels[row])
                if evidence is not None:
                    reason = evidence.fact(market.symbols[col], name).reason
                    if reason is not None:
                        reasons[row, col] = f"{reason}:{name}"
        available &= finite
    return available, reasons


def _combine_plugin_availability(available, reasons, signals, phase):
    provided = getattr(signals, f"{phase}_available")
    if provided is None:
        return
    missing = available & ~provided
    supplied_reasons = getattr(signals, f"{phase}_reason")
    reasons[missing] = "strategy_input_unavailable"
    if supplied_reasons is not None:
        named = missing & (supplied_reasons != "")
        reasons[named] = supplied_reasons[named]
    available &= provided


def _overlay_matrix(market, expression):
    rows, columns = np.nonzero(np.isfinite(market.close))
    # The existing overlay expression groups temporal offsets by symbol on
    # actual bars, so a suspended session adds no fake observation.
    panel = pl.DataFrame({
        "symbol": np.asarray(market.symbols)[columns],
        "date": np.asarray(market.timestamp_labels, dtype="datetime64[D]")[rows],
        **{
            name: pl.Series(_value(market, name)[rows, columns], nan_to_null=True)
            for name in expression.meta.root_names() if name not in {"symbol", "date"}
        },
    })
    predicate = panel.select(expression.alias("_predicate"))["_predicate"].cast(pl.Boolean)
    matched = np.zeros(market.shape, dtype=bool)
    known = np.zeros(market.shape, dtype=bool)
    matched[rows, columns] = predicate.fill_null(False).to_numpy()
    known[rows, columns] = predicate.is_not_null().to_numpy()
    return matched, known


def _score_matrix(market, values, weights, pool, directions):
    dependencies = factor_calculation_order(weights)
    composites = {name for name in dependencies if (spec := get_factor(name)) is not None and spec.kind == "composite"}
    if not composites:
        return complete_pool_score(values, weights, pool, directions)
    rows, columns = np.nonzero(np.isfinite(market.close))
    panel = pl.DataFrame({
        "symbol": np.asarray(market.symbols)[columns],
        "date": np.asarray(market.timestamp_labels)[rows],
        **{
            name: pl.Series(_value(market, name)[rows, columns], nan_to_null=True)
            for name in dependencies if name not in composites | {"date", "symbol"}
        },
    })
    for name in values.keys() - composites:
        panel = panel.with_columns(pl.Series(name, values[name][rows, columns], nan_to_null=True))
    scored = complete_pool_frame_score(panel, weights, directions, pool[rows, columns])
    scores = np.zeros(market.shape, dtype=np.float32)
    available = np.zeros(market.shape, dtype=bool)
    reasons = np.full(market.shape, "missing_bar", dtype=object)
    scores[rows, columns] = scored["score"].fill_null(0).to_numpy()
    available[rows, columns] = scored["score_available"].to_numpy()
    reasons[rows, columns] = scored["score_reason"].to_numpy()
    return scores, available, reasons


def evaluate_quant_matrix(strategy, market, params, config):
    from app.strategy.custom_signals import build_overlay_expr

    contract = config.quant_contract
    weights = {name: float(weight) for name, weight in config.scoring.items() if float(weight)}
    basic = dict(config.basic_filter)
    bounds = {}
    if basic.get("enabled", True):
        for prefix, name in (
            ("market_cap", "market_cap"), ("float_cap", "float_cap"),
            ("amount", "amount"), ("turnover", "turnover_rate"),
        ):
            low, high = basic.get(f"{prefix}_min"), basic.get(f"{prefix}_max")
            if low is not None or high is not None:
                bounds[name] = low, high
        exclude_st = basic.pop("exclude_st", False)
    else:
        exclude_st = False
    entry_fields = contract.required_entry_fields | bounds.keys()
    if exclude_st:
        entry_fields |= {"is_st"}
    exit_fields = contract.required_exit_fields
    overlay_expr = build_overlay_expr(config.quant_overlay) if config.quant_overlay else None
    overlay_fields = set(overlay_expr.meta.root_names()) - {"symbol", "date"} if overlay_expr is not None else set()
    required = entry_fields | exit_fields | weights.keys() | strategy.required_fields() | overlay_fields
    market = _strict_market(market, config.quant_inputs, required)

    entry_available, entry_reason = _availability(market, entry_fields, config.quant_inputs)
    exit_available, exit_reason = _availability(market, exit_fields, config.quant_inputs)
    # Preserve the legacy non-PIT price/board filters. Bounds below use the
    # cutoff-aware raw capitalization producers, never adjusted close times
    # the current share count.
    for prefix in ("market_cap", "float_cap", "amount", "turnover"):
        for suffix in ("min", "max"):
            basic.pop(f"{prefix}_{suffix}", None)
    basic_mask = build_pipeline_filter_mask(market, replace(config, basic_filter=basic)).copy()
    if exclude_st:
        basic_mask &= market.field("is_st") == 0
    for name, (low, high) in bounds.items():
        values = market.field(name)
        if low is not None:
            basic_mask &= values >= float(low)
        if high is not None:
            basic_mask &= values <= float(high)

    def score_inputs(name):
        spec = get_factor(name)
        if spec is not None and spec.kind == "composite":
            for member, weight in spec.components:
                if weight:
                    yield from score_inputs(member)
        else:
            yield name

    values = {name: _value(market, name) for name in {name for factor in weights for name in score_inputs(factor)}}
    eligible = entry_available & basic_mask
    complete = eligible.copy()
    for value in values.values():
        complete &= np.isfinite(value)
    complete.flags.writeable = False
    signals = strategy.compute_signals(replace(market, eligible_universe=complete), params)
    validate_signal_matrix(signals, market.shape)
    _combine_plugin_availability(entry_available, entry_reason, signals, "entry")
    _combine_plugin_availability(exit_available, exit_reason, signals, "exit")

    eligible &= entry_available
    if signals.eligible_universe is not None:
        eligible &= signals.eligible_universe
        complete = complete & signals.eligible_universe
    condition = eligible & signals.entry.astype(bool)
    if overlay_expr is not None:
        overlay, overlay_available = _overlay_matrix(market, overlay_expr)
        entry_reason[entry_available & ~overlay_available] = "overlay_input_unavailable"
        entry_available &= overlay_available
        condition &= overlay & overlay_available
    exits = exit_available & signals.exit.astype(bool)
    score_pool = eligible if contract.scoring_scope == "eligible_universe" else condition
    score, score_available, score_reason = _score_matrix(
        market, values, weights, score_pool, config.scoring_directions,
    )
    entry = condition & score_available
    return make_signal_matrix(
        market.shape, entry=entry, exit=exits, score=np.clip(score, 0, 100),
        entry_signal_code=np.where(entry, signals.entry_signal_code, -1),
        exit_signal_code=np.where(exits, signals.exit_signal_code, -1),
        entry_signal_ids=signals.entry_signal_ids, exit_signal_ids=signals.exit_signal_ids,
        entry_available=entry_available, exit_available=exit_available, score_available=score_available,
        entry_reason=entry_reason, exit_reason=exit_reason, score_reason=score_reason,
        eligible_universe=complete, entry_condition=condition,
    )
