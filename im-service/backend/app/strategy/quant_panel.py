"""Shared strict decisions for Polars strategies and the panel backtest."""
from __future__ import annotations

import polars as pl

from app.factors.registry import factor_dependencies, get_factor
from app.quant_evidence import PIT_FIELDS, basic_evidence_fields
from app.strategy.scoring import (
    complete_pool_frame_score,
    effective_scoring,
    effective_scoring_directions,
    materialize_scoring_columns,
)


def _known(frame, name):
    if name not in frame.columns:
        return pl.lit(False)
    return pl.col(name).is_finite().fill_null(False) if frame.schema[name].is_numeric() else pl.col(name).is_not_null()


def _with_pit_values(panel, quant_inputs, required):
    relevant = set(panel.columns) | factor_dependencies(required) | required
    pits = PIT_FIELDS & relevant
    if "turnover_rate" in pits:
        pits |= {"float_shares"}
    stale = {name for name in panel.columns if name not in pits and not factor_dependencies([name]).isdisjoint(pits)}
    fixed = panel.drop(stale, strict=False)
    if pits:
        clock = "datetime" if "datetime" in fixed.columns else "date"
        keys = [(quant_inputs.get(str(at)), str(symbol)) for at, symbol in fixed.select(clock, "symbol").iter_rows()]
        columns = []
        for name in sorted(pits):
            cells = []
            for evidence, symbol in keys:
                fact = evidence.fact(symbol, name) if evidence is not None else None
                value = fact.value if fact is not None and fact.reason is None else None
                cells.append(value if isinstance(value, (str, float, int, bool)) else None)
            dtype = pl.String if name in {"name", "trading_status", "listing_date"} else pl.Float64
            if name == "is_st":
                dtype = pl.Boolean
            columns.append(pl.Series(name, cells, dtype=dtype, strict=False))
        fixed = fixed.with_columns(columns)
    if "turnover_rate" in pits and "volume" in fixed.columns:
        fixed = fixed.with_columns(
            pl.when(pl.col("float_shares") > 0).then(pl.col("volume") * 10_000.0 / pl.col("float_shares"))
            .otherwise(None).alias("turnover_rate"),
        )
    return fixed


def _phase_reason(panel, fields):
    reason = pl.lit("")
    for name in reversed(sorted(fields)):
        reason = pl.when(_known(panel, name)).then(reason).otherwise(pl.lit(f"missing_field:{name}"))
    return reason


def _signals(frame, signals):
    columns = [key if key.startswith(("signal_", "csg_", "csgi_")) else f"signal_{key}" for key in signals]
    if not columns:
        return None
    return pl.any_horizontal([
        pl.col(name).cast(pl.Boolean) if name in frame.columns else pl.lit(None, dtype=pl.Boolean)
        for name in columns
    ])


def _basic_filter(frame, basic):
    from app.strategy.engine import StrategyEngine

    if not basic.get("enabled", True):
        return pl.lit(True)
    settings = dict(basic)
    predicates = []
    for field in ("market_cap", "float_cap"):
        for suffix in ("min", "max"):
            threshold = settings.pop(f"{field}_{suffix}", None)
            if threshold is not None:
                predicate = pl.col(field) >= threshold if suffix == "min" else pl.col(field) <= threshold
                predicates.append(predicate)
    if settings.pop("exclude_st", False):
        predicates.append(~pl.col("is_st"))
    legacy = StrategyEngine._basic_filter_expr(frame, settings)
    if legacy is not None:
        predicates.append(legacy)
    return pl.all_horizontal(predicates) if predicates else pl.lit(True)


def _condition(frame, strategy, params, daily_history, daily_inputs):
    if strategy.execution_backend == "minute_filter":
        evaluator = strategy.filter_minute_history_fn
        keys = ["symbol", "datetime"]
        if strategy.minute_daily_bars > 0:
            if daily_history is None:
                raise ValueError("required daily auxiliary history unavailable")
            daily = _with_pit_values(daily_history, daily_inputs, strategy.quant_contract.required_fields)
            findings = evaluator(frame, params, daily=daily)
        else:
            findings = evaluator(frame, params)
    elif strategy.filter_history_fn is not None:
        findings = strategy.filter_history_fn(frame, params)
        keys = ["symbol", "date"]
    else:
        return frame, (strategy.filter_fn(frame, params) if strategy.filter_fn is not None else pl.lit(True))

    if findings is None:
        raise ValueError("strategy returned no decision frame")
    if findings.is_empty():
        return frame, pl.lit(False)
    if "datetime" in keys and "datetime" not in findings.columns and "last_datetime" in findings.columns:
        findings = findings.rename({"last_datetime": "datetime"})
    hits = findings.select(keys).unique().with_columns(pl.lit(True).alias("_strategy_hit"))
    frame = frame.join(hits, on=keys, how="left", maintain_order="left")
    return frame, pl.col("_strategy_hit").fill_null(False)


def evaluate_quant_panel(
    panel, strategy, params, overrides, basic, entry_signals, exit_signals, quant_inputs,
    *, pool=None, daily_history=None, daily_inputs=None,
):
    from app.strategy.custom_signals import build_overlay_expr

    contract = strategy.quant_contract
    entry_fields = contract.required_entry_fields | basic_evidence_fields(basic)
    exit_fields = contract.required_exit_fields
    scoring = effective_scoring(strategy.meta.get("scoring"), overrides)
    conditions = overrides.get("overlay_filter")
    overlay = build_overlay_expr(conditions) if conditions else None
    dependencies = set(overlay.meta.root_names()) if overlay is not None else set()
    frame = _with_pit_values(panel, quant_inputs, entry_fields | exit_fields | dependencies | scoring.keys())
    phase_fields = entry_fields | exit_fields | dependencies
    frame = materialize_scoring_columns(frame, phase_fields)
    # A null argument remains a usable typed column for strategy expressions.
    for name in (entry_fields | exit_fields | dependencies) - set(frame.columns):
        spec = get_factor(name)
        dtype = pl.Boolean if spec is not None and spec.id.startswith("signal_") else pl.Float64
        frame = frame.with_columns(pl.lit(None, dtype=dtype).alias(name))

    frame, formula = _condition(frame, strategy, params, daily_history, daily_inputs or {})
    allowed = pl.col("symbol").is_in(pool) if pool else pl.lit(True)
    raw_entry = _signals(frame, entry_signals)
    raw_exit = _signals(frame, exit_signals)
    candidate = formula & _basic_filter(frame, basic) & allowed
    if raw_entry is not None:
        candidate &= raw_entry
    if overlay is not None:
        candidate &= overlay
    if raw_exit is None:
        raw_exit = pl.lit(False)
    frame = frame.with_columns(
        _phase_reason(frame, entry_fields).alias("_quant_entry_inputs"),
        _phase_reason(frame, exit_fields).alias("_quant_exit_inputs"),
        candidate.alias("_quant_condition"),
        raw_exit.alias("_quant_exit"),
    )
    frame = frame.with_columns(
        pl.when(pl.col("_quant_entry_inputs") != "").then("_quant_entry_inputs")
        .when(pl.col("_quant_condition").is_null()).then(pl.lit("entry_condition_unavailable"))
        .otherwise(pl.lit("")).alias("_quant_entry_reason"),
        pl.when(pl.col("_quant_exit_inputs") != "").then("_quant_exit_inputs")
        .when(pl.col("_quant_exit").is_null()).then(pl.lit("exit_condition_unavailable"))
        .otherwise(pl.lit("")).alias("_quant_exit_reason"),
    )
    entry_available = frame["_quant_entry_reason"] == ""
    exit_available = frame["_quant_exit_reason"] == ""
    condition = entry_available & frame["_quant_condition"].fill_null(False)
    exits = exit_available & frame["_quant_exit"].fill_null(False)
    if contract.scoring_scope == "eligible_universe":
        eligible = frame.select((_basic_filter(frame, basic) & allowed).alias("_eligible"))["_eligible"].fill_null(False)
        score_pool = entry_available & eligible
    else:
        score_pool = condition
    clock = "datetime" if "datetime" in frame.columns else "date"
    if clock == "datetime":
        # Normalize each completed minute across its actual contemporaneous
        # securities, rather than averaging all samples within the date.
        frame = frame.drop("date", strict=False).rename({"datetime": "date"})
    frame = complete_pool_frame_score(frame, scoring, effective_scoring_directions(overrides), score_pool)
    if clock == "datetime":
        frame = frame.rename({"date": "datetime"})
    frame = frame.with_columns(
        entry_available.alias("_quant_entry_available"),
        exit_available.alias("_quant_exit_available"), condition.alias("_quant_entry_condition"),
        exits.alias("_quant_exit_condition"),
    ).drop("_quant_entry_inputs", "_quant_exit_inputs", "_quant_condition", "_quant_exit", "_strategy_hit", strict=False)
    return frame, entry_available, condition & frame["score_available"], exits
