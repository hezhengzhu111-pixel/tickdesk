from pathlib import Path

import pytest
from tickdesk_quant_fixture import DAY, NEXT_DAY, STRATEGY_ID, SYMBOLS, build_fixture

from app.backtest.engine import BacktestEngine
from app.backtest.execution_timing import EntryOpenTiming
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService
from app.strategy.engine import StrategyEngine


@pytest.mark.parametrize("explicit, expected", [(None, 3), ({"comparison_bars": 7}, 7)])
def test_v3_backtest_uses_live_parameter_precedence(tmp_path, explicit, expected):
    app = build_fixture(tmp_path / "kernel")
    source = Path(app.state.repo.store.data_dir) / "strategies" / f"{STRATEGY_ID}.py"
    source.write_text(source.read_text(encoding="utf-8").replace(
        '"quant_contract": {', '"params":{"comparison_bars":{"type":"int","default":None}}, "quant_contract": {',
    ).replace("return 1\n", 'value = params.get("comparison_bars")\n'
                             '        if not value or value < 1:\n'
                             '            raise ValueError("comparison_bars absent")\n'
                             '        return value\n'),
                      encoding="utf-8")
    try:
        service = StrategyBacktestService(BacktestEngine(app.state.repo), StrategyEngine([source.parent]))
        result = service.run_with_evidence(StrategyBacktestConfig(
            STRATEGY_ID, [SYMBOLS[0]], DAY, NEXT_DAY,
            params=explicit, overrides={"params": {"comparison_bars": 3}},
            entry_open_timing=EntryOpenTiming((), (), (), 5000),
        ))
        assert result.error is None
        assert result.evidence["config"]["params"] == {"comparison_bars": expected}
    finally:
        app.state.repo.store.db.close()


def test_v3_backtest_reports_unavailable_entry_but_preserves_exit_without_external_history(tmp_path):
    app = build_fixture(tmp_path / "kernel")
    source = Path(app.state.repo.store.data_dir) / "strategies" / f"{STRATEGY_ID}.py"
    source.write_text(source.read_text(encoding="utf-8").replace('"entry": ["roe_latest"]',
                      '"entry": ["roe_latest", "ext_industry_regime"]').replace(
                          '{"roe_latest", "raw_close"}', '{"roe_latest", "raw_close", "ext_industry_regime"}',
                      ).replace('entry=market.field("roe_latest") > 0',
                                'entry=(market.field("roe_latest") > 0) & (market.field("ext_industry_regime") == 1)'),
                      encoding="utf-8")
    try:
        service = StrategyBacktestService(BacktestEngine(app.state.repo), StrategyEngine([source.parent]))
        result = service.run_with_evidence(StrategyBacktestConfig(
            STRATEGY_ID, [SYMBOLS[3]], DAY, DAY, entry_open_timing=EntryOpenTiming((), (), (), 5000),
        ))
        assert result.error is None and not result.trades
        assert result.stats["status"] == "no_executable_entries"
        assert result.stats["quant_observations"] == {
            "entryAvailable": 0, "entryTrue": 0, "exitAvailable": 1, "exitTrue": 1, "scoreAvailable": 0,
        }
        assert result.evidence["config"]["entry_open_timing"]["admission_eligible"] is False
        clocks = result.evidence["execution_timing"]["minute_evidence"]
        assert clocks["entry"]["count"] == clocks["exit"]["count"] == 0
        assert clocks["sha256"] == result.evidence["config"]["entry_open_timing"]["evidence_sha256"]
    finally:
        app.state.repo.store.db.close()
