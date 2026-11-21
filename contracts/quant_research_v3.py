"""Bounded, read-only research observations; no actionable tasks or evidence blobs."""
from quant_holding_v3 import ref, obj, enum, nullable, array


def research_contract(definitions, timestamp):
    time = {"type": "integer", "minimum": 0, "maximum": 9_007_199_254_740_991}
    reason = {"type": "string", "minLength": 1, "maxLength": 256, "pattern": r"\S"}
    verdict = {"oneOf": [
        obj({"value": {"type": "boolean"}, "reason": enum(None)}),
        obj({"value": enum(None), "reason": reason}),
    ]}
    scope = enum("entry_candidates", "eligible_universe")
    definitions.update(
        QuantVerdictV3=verdict,
        QuantScoreV3={"oneOf": [
            obj({"value": ref("Decimal"), "reason": enum(None), "scope": scope}),
            obj({"value": enum(None), "reason": reason, "scope": scope}),
        ]},
        QuantBarV3={"oneOf": [
            obj({"status": enum("complete"), "endMs": time, "availableAtMs": time}),
            obj({"status": enum("unknown"), "endMs": nullable(time), "availableAtMs": enum(None)}),
        ]},
        QuantObservationV3=obj({
            "symbol": ref("Symbol"), "strategyId": ref("Id"),
            "group": enum("daily_swing", "short_term", "long_term"),
            "timeframe": enum("1d", "1m", "5m", "1w", "1mo"),
            "admission": enum("research", "causal_checked", "oos_checked", "shadow"),
            "bar": ref("QuantBarV3"), "entry": ref("QuantVerdictV3"),
            "exit": ref("QuantVerdictV3"), "score": ref("QuantScoreV3"),
        }),
    )
    cursor = {"type": "string", "minLength": 1, "maxLength": 512}
    result = {
        "version": enum(3), "scanId": nullable(ref("Id")),
        "decisionCutoffMs": nullable(time), "status": enum("missing", "available", "failed"),
        "error": enum(None, "DATA_UNAVAILABLE"),
        "observations": array(ref("QuantObservationV3"), 20),
        "historical": {"type": "boolean"}, "nextCursor": nullable(cursor), "hasMore": {"type": "boolean"},
    }
    entries = {"get_quant_observations": ({"scanId": ref("Id"), "cursor": cursor,
        "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, result,
        "read", ("scanId", "cursor", "limit"))}
    row = {"symbol": "300502.SZ", "strategyId": "research-swing", "group": "daily_swing",
        "timeframe": "1d", "admission": "research",
        "bar": {"status": "complete", "endMs": timestamp - 1000, "availableAtMs": timestamp - 500},
        "entry": {"value": None, "reason": "missing_fundamental"},
        "exit": {"value": False, "reason": None},
        "score": {"value": "0", "reason": None, "scope": "eligible_universe"}}
    samples = {"get_quant_observations": ({}, {"version": 3, "scanId": "research_scan",
        "decisionCutoffMs": timestamp, "status": "available", "error": None,
        "observations": [row], "historical": False, "nextCursor": None, "hasMore": False})}
    negatives = [("quant_read_limit_is_bounded", "get_quant_observations", {"limit": 21}),
        ("quant_read_has_no_write_fields", "get_quant_observations", {"enableProduction": True})]
    return entries, samples, negatives
