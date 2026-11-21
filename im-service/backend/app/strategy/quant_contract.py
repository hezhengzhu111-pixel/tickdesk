"""Explicit versioned research contract; absence alone means legacy."""
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class QuantContract:
    version: int
    group: str
    family: str
    decision_timeframe: str
    required_entry_fields: frozenset[str]
    required_exit_fields: frozenset[str]
    price_basis: str
    entry_description: str
    exit_description: str
    risk_clocks: tuple[tuple[str, str], ...]
    scoring_scope: str = "entry_candidates"

    @property
    def required_fields(self) -> frozenset[str]:
        """Loading dependencies only; admission is phase specific."""
        return self.required_entry_fields | self.required_exit_fields


def parse_quant_contract(meta: Mapping) -> QuantContract | None:
    if "quant_contract" not in meta:
        return None
    raw = meta["quant_contract"]
    def invalid(message):
        raise ValueError("quant_contract: " + message)
    if not isinstance(raw, Mapping):
        invalid("must be a mapping")
    expected = {"version", "group", "family", "decision_timeframe", "required_fields",
                "price_basis", "entry_description", "exit_description", "risk_clocks"}
    if set(raw) - {"scoring_scope"} != expected:
        invalid("missing or unknown fields")
    if type(raw["version"]) is not int or raw["version"] != 3:
        invalid("unsupported version")
    if raw["group"] not in ("daily_swing", "short_term", "long_term"):
        invalid("unsupported group")
    for name in ("family", "decision_timeframe", "price_basis", "entry_description", "exit_description"):
        if not isinstance(raw[name], str) or not raw[name].strip():
            invalid(name + " must be nonempty text")
    if raw["decision_timeframe"] not in ("1d", "1m", "5m", "1w", "1mo"):
        invalid("unsupported decision_timeframe")
    if raw["price_basis"] not in ("raw", "forward_adjusted"):
        invalid("unsupported price_basis")
    scoring_scope = raw.get("scoring_scope", "entry_candidates")
    if scoring_scope not in ("entry_candidates", "eligible_universe"):
        invalid("unsupported scoring_scope")
    fields = raw["required_fields"]
    if not isinstance(fields, Mapping) or set(fields) != {"entry", "exit"}:
        invalid("required_fields must contain entry and exit")
    for phase in ("entry", "exit"):
        values = fields[phase]
        if not isinstance(values, (list, tuple)) or any(not isinstance(v, str) or not v.strip() for v in values):
            invalid("invalid " + phase + " fields")
        if "financial_conflicting_fields" in values:
            invalid("financial_conflicting_fields is quality metadata")
    clocks = raw["risk_clocks"]
    if not isinstance(clocks, Mapping) or not clocks or any(
        not isinstance(k, str) or not k.strip() or not isinstance(v, str) or not v.strip()
        for k, v in clocks.items()
    ):
        invalid("risk_clocks must be a nonempty text mapping")
    return QuantContract(3, raw["group"], raw["family"], raw["decision_timeframe"],
                         frozenset(fields["entry"]), frozenset(fields["exit"]), raw["price_basis"],
                         raw["entry_description"], raw["exit_description"], tuple(sorted(clocks.items())), scoring_scope)
