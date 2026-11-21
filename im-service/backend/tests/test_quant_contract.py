from copy import deepcopy

import pytest

from app.strategy.engine import StrategyEngine

CONTRACT = dict(version=3, group="daily_swing", family="trend", decision_timeframe="1d",
                required_fields={"entry": ["roe_latest"], "exit": ["raw_close"]},
                price_basis="raw", entry_description="trend entry", exit_description="risk exit",
                risk_clocks={"stop_loss": "1d"})


def source(meta):
    return "import polars as pl\nMETA = " + repr(meta) + "\ndef filter(df, params):\n    return pl.lit(True)\n"


def test_actual_loader_contract_and_legacy(tmp_path):
    path = tmp_path / "custom_quant.py"
    path.write_text(source({"quant_contract": CONTRACT}), encoding="utf-8")
    definition = StrategyEngine._load_file(path)
    assert {"roe_latest", "raw_close"} <= definition.required_features
    assert definition.quant_contract.required_entry_fields == frozenset({"roe_latest"})
    assert definition.quant_contract.required_exit_fields == frozenset({"raw_close"})
    assert definition.quant_contract.required_fields == frozenset({"roe_latest", "raw_close"})
    path.write_text(source({}), encoding="utf-8")
    assert StrategyEngine._load_file(path).quant_contract is None


@pytest.mark.parametrize("value", [None, {}, {**CONTRACT, "version": 4}, {**CONTRACT, "group": "mixed"},
                                  {**CONTRACT, "required_fields": {"entry": "roe", "exit": []}}])
def test_invalid_explicit_never_legacy(tmp_path, value):
    path = tmp_path / "custom_invalid.py"
    path.write_text(source({"quant_contract": value}), encoding="utf-8")
    with pytest.raises(ValueError, match="quant_contract"):
        StrategyEngine._load_file(path)


def test_persisted_source_and_overrides(tmp_path):
    from app.api.strategy import StrategyCodeSaveRequest, save_strategy_code_core
    from app.strategy.config import load_override, save_override
    engine = StrategyEngine(strategy_dirs=[tmp_path / "strategies" / "custom"])
    code = source({"id": "custom_quant", "name": "quant", "description": "quant", "quant_contract": CONTRACT})
    save_strategy_code_core(StrategyCodeSaveRequest(strategy_id="custom_quant", code=code,
                            mode="create", target_source="custom"), engine, tmp_path)
    save_override(tmp_path, "custom_quant", {"params": {"window": 10}})
    engine.reload()
    assert engine.get("custom_quant").quant_contract.version == 3
    assert load_override(tmp_path, "custom_quant")["params"]["window"] == 10
    bad = deepcopy(CONTRACT)
    bad["version"] = 99
    with pytest.raises(ValueError, match="quant_contract"):
        save_strategy_code_core(StrategyCodeSaveRequest(strategy_id="custom_quant", mode="update",
            code=source({"id": "custom_quant", "quant_contract": bad})), engine, tmp_path)
    assert engine.get("custom_quant").quant_contract.version == 3


def test_contract_is_source_owned_not_silent_override(tmp_path):
    import json

    from app.strategy.config import load_override, save_override
    with pytest.raises(ValueError, match="quant_contract"):
        save_override(tmp_path, "custom_quant", {"quant_contract": None})
    path = tmp_path / "user_data" / "strategy_overrides" / "custom_quant.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"quant_contract": {"version": 99}}), encoding="utf-8")
    with pytest.raises(ValueError, match="quant_contract"):
        load_override(tmp_path, "custom_quant")
