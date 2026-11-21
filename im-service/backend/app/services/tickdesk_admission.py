"""Shared production admission preflight for the TickDesk catalog and scan."""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

from app.services.tickdesk_adapter import CN, _finite_decimal, _wire_decimal, fingerprint, timestamp

_STRATEGY = re.compile(r"[a-zA-Z0-9_-]{1,64}")
_SYMBOL = re.compile(r"[0-9]{6}\.(?:SZ|SH)")
_MAX_APPROVAL_BYTES = 1024 * 1024


class AdmissionBlockedError(ValueError):
    def __init__(self, reason, strategies=None):
        super().__init__(f"strategy approval blocked: {reason}")
        self.reason = reason
        self.strategies = strategies or {}

    def diagnostic(self):
        return {"reason": self.reason, "strategies": self.strategies}


def _unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicated key")
    return result


def _invalid_constant(_):
    raise ValueError("non-finite approval constant")


def read_approval(root):
    try:
        with (Path(root) / "tickdesk_approvals.json").open("rb") as stream:
            raw = stream.read(_MAX_APPROVAL_BYTES + 1)
    except FileNotFoundError as exc:
        raise AdmissionBlockedError("NO_PRODUCTION_APPROVAL") from exc
    if len(raw) > _MAX_APPROVAL_BYTES:
        raise AdmissionBlockedError("APPROVAL_INVALID")
    try:
        approval = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise AdmissionBlockedError("APPROVAL_INVALID") from exc
    if not isinstance(approval, dict):
        raise AdmissionBlockedError("APPROVAL_INVALID")
    return approval


def approval_policy(profiles, profile):
    policy = profiles.get(profile) if isinstance(profiles, dict) else None
    if not isinstance(policy, dict):
        raise AdmissionBlockedError("APPROVAL_POLICY_INVALID")
    score = _finite_decimal(policy.get("minScore"))
    amount = _finite_decimal(policy.get("minimumAmount"))
    if score is None or not 0 <= score <= 100 or amount is None or amount <= 0:
        raise AdmissionBlockedError("APPROVAL_POLICY_INVALID")
    min_score, minimum_amount = _wire_decimal(score), _wire_decimal(amount)
    if min_score is None or minimum_amount is None:
        raise AdmissionBlockedError("APPROVAL_POLICY_INVALID")
    return {**policy, "minScore": min_score, "minimumAmount": minimum_amount}


def strategy_sources(definition):
    path = definition.file_path
    code = path.read_bytes()
    helpers = {helper: helper.read_bytes() for helper in sorted(path.parent.glob("_*.py"))}
    helper_hash = fingerprint({helper.name: fingerprint(body) for helper, body in helpers.items()})
    return {path: code, **helpers}, fingerprint(code), helper_hash


def _v3_cutoff(approved_at, requested_at):
    approved = timestamp(approved_at)
    if approved is None or not 0 < approved <= requested_at:
        raise AdmissionBlockedError("APPROVAL_TIME_INVALID")
    return approved


def _v3_blockers(engine, root, spec, definition, overrides, approved, sources, code_hash, helper_hash):
    from app.backtest.evidence import definition_versions, json_digest, load_evidence

    contract = definition.quant_contract
    if (contract is None or contract.group != "daily_swing" or contract.decision_timeframe != "1d"
            or definition.execution_backend != "matrix_native"
            or "stock" not in definition.meta.get("asset_types", ["stock"])):
        return ["STRATEGY_OUTSIDE_V3_PRODUCTION_SCOPE"]
    params = engine.resolve_params(definition, None, overrides)
    if (spec.get("parametersHash") != fingerprint({"params": params, "overrides": overrides})
            or spec.get("dependenciesHash") != helper_hash):
        return ["REVIEWED_STRATEGY_IDENTITY_CHANGED"]
    evidence = spec.get("evidence")
    causal = evidence.get("causal") if isinstance(evidence, dict) else None
    if not isinstance(causal, dict) or not isinstance(causal.get("runId"), str):
        return ["V3_CAUSAL_ARCHIVE_REQUIRED"]
    try:
        report = load_evidence(root, causal["runId"])
        if json_digest(report) != causal.get("sha256"):
            return ["CAUSAL_ARCHIVE_HASH_MISMATCH"]
        recorded = timestamp(report["created_at"])
        config = report["config"]
        reviewed_params = engine.resolve_params(definition, config["params"], config.get("overrides"))
        version = report["strategy"]
        data = report["data"]
        symbols = spec.get("symbols")
        if (not isinstance(symbols, list) or not 0 < len(symbols) <= 500
                or any(not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol) for symbol in symbols)
                or sorted(set(symbols)) != sorted(symbols)
                or sorted(config["symbols"]) != sorted(symbols)
                or config.get("strategy_id") != definition.meta["id"]
                or config.get("asset_type") != "stock"
                or report["config_sha256"] != json_digest(config)
                or data["asset_type"] != "stock" or data["daily_price_basis"] != "qfq"
                or reviewed_params != params or (config.get("overrides") or {}) != overrides
                or config["start"] >= config["end"]
                or not isinstance(recorded, int) or recorded >= approved
                or date.fromisoformat(config["end"]) > datetime.fromtimestamp(recorded / 1000, CN).date()
                or version["id"] != definition.meta["id"] or version["backend"] != definition.execution_backend
                or version["changed_during_run"] is not False
                or version["definitions"] != definition_versions(root, definition)
                or version["definitions"]["files"].get("strategy") != code_hash):
            return ["CAUSAL_ARCHIVE_IDENTITY_MISMATCH"]
    except (OSError, TypeError, ValueError, KeyError, OverflowError, RecursionError):
        return ["CAUSAL_ARCHIVE_UNREADABLE"]

    blockers = []
    audit = report.get("lookahead")
    checks = audit.get("checks") if isinstance(audit, dict) else None
    if (not isinstance(checks, list) or not checks or audit.get("status") != "passed" or audit.get("differences") != 0
            or not any(isinstance(check, dict) and check.get("status") == "completed"
                       and type(check.get("compared_values")) is int and check["compared_values"] > 0 for check in checks)):
        blockers.append("CAUSAL_AUDIT_INCONCLUSIVE")
    from app.services.tickdesk_admission_oos import inspect_oos
    from app.services.tickdesk_admission_shadow import review_shadow

    shadow = review_shadow(root, evidence.get("shadow"), spec, sources, approved)
    oos = inspect_oos(root, causal, evidence.get("outOfSample"), approved)
    # The independently reviewed clocks and historical source versions remain mandatory.
    return [*blockers, "STRICT_EXECUTION_EVIDENCE_UNAVAILABLE", "PIT_ADMISSION_ARCHIVE_UNAVAILABLE",
            *oos["blockers"], *shadow["blockers"]]


def validate_approval(engine, root, approval, overrides, *, requested_at, profile=None):
    """Return the exact executable definitions, or block the whole manifest."""
    if (not isinstance(approval, dict) or type(approval.get("version")) is not int
            or approval["version"] not in (1, 3) or not approval.get("approvedBy") or not approval.get("approvedAt")):
        raise AdmissionBlockedError("APPROVAL_INVALID")
    version = approval["version"]
    if version == 3:
        approved = _v3_cutoff(approval["approvedAt"], requested_at)
        reviewer = approval["approvedBy"]
        if not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 120:
            raise AdmissionBlockedError("APPROVAL_INVALID")
    else:
        evidence = approval.get("evidence")
        if not isinstance(evidence, dict) or not all(evidence.get(phase) for phase in ("causal", "outOfSample", "shadow")):
            raise AdmissionBlockedError("LEGACY_APPROVAL_EVIDENCE_MISSING")
    profiles = approval.get("profiles")
    if not isinstance(profiles, dict) or not any(name in profiles for name in ("conservative", "aggressive")):
        raise AdmissionBlockedError("APPROVAL_POLICY_INVALID")
    for name in ("conservative", "aggressive"):
        if name in profiles:
            approval_policy(profiles, name)
    if profile is not None and profile not in profiles:
        raise AdmissionBlockedError("APPROVAL_POLICY_INVALID")
    specs = approval.get("strategies")
    if (not isinstance(specs, list) or not 1 <= len(specs) <= 100
            or any(not isinstance(spec, dict) or not isinstance(spec.get("id"), str)
                   or not _STRATEGY.fullmatch(spec["id"]) for spec in specs)
            or len({spec["id"] for spec in specs}) != len(specs)):
        raise AdmissionBlockedError("APPROVAL_STRATEGY_LIST_INVALID")
    accepted = {}
    blocked = {}
    for spec in specs:
        sid = spec["id"]
        weight = _finite_decimal(spec.get("weight"))
        try:
            definition = engine.get(sid)
            if (not definition.file_path or definition.source == "ai" or definition.execution_backend == "composite"
                    or definition.meta.get("research_only") or spec.get("direction") not in ("buy", "sell")
                    or weight is None or weight <= 0):
                blocked[sid] = ["STRATEGY_NOT_ELIGIBLE_FOR_PRODUCTION"]
                continue
            if version == 1 and definition.quant_contract is not None:
                blocked[sid] = ["V3_EVIDENCE_REQUIRED"]
                continue
            sources, code_hash, helper_hash = strategy_sources(definition)
        except (OSError, ValueError, KeyError):
            blocked[sid] = ["STRATEGY_SOURCE_UNAVAILABLE"]
            continue
        if code_hash != spec.get("codeHash") or (len(sources) > 1 and helper_hash != spec.get("dependenciesHash")):
            blocked[sid] = ["REVIEWED_STRATEGY_IDENTITY_CHANGED"]
        elif version == 3:
            findings = _v3_blockers(engine, Path(root), spec, definition, overrides.get(sid, {}),
                                    approved, sources, code_hash, helper_hash)
            if findings:
                blocked[sid] = findings
            else:
                accepted[sid] = definition, weight, sources
        elif fingerprint(overrides.get(sid, {})) != spec.get("parametersHash"):
            blocked[sid] = ["REVIEWED_STRATEGY_IDENTITY_CHANGED"]
        else:
            accepted[sid] = definition, weight, sources
    if blocked:
        raise AdmissionBlockedError("STRATEGY_APPROVAL_WITHHELD", blocked)
    return accepted
