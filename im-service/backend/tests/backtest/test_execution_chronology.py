from __future__ import annotations

import json
from datetime import date
from threading import Event

import polars as pl
import pytest

from app.backtest.engine import MatcherConfig
from tests.backtest.test_engine_portfolio import _engine, _mask, _panel


@pytest.mark.parametrize("legacy", [False, True], ids=["matrix", "legacy"])
@pytest.mark.parametrize("exit_phase", ["close", "intraday", "open", "open-before-intraday", "opening-gap"])
def test_replacement_open_entry_uses_only_open_exit_proceeds(legacy, exit_phase):
    stop = exit_phase in {"intraday", "open-before-intraday", "opening-gap"}
    scheduled_at_open = exit_phase in {"open", "open-before-intraday"}
    prices = {"low": 9, "high": 11, "close": 9.25} if stop else {}
    if exit_phase == "opening-gap":
        prices["open"] = 9.25
    panel = _panel(
        ["A", "B"], days=4, overrides={("A", 2): prices},
    )
    engine = _engine()
    simulate = engine.simulate_portfolio_legacy if legacy else engine.simulate_portfolio
    result = simulate(
        panel,
        _mask(panel, {("A", 0), ("B", 1)}),
        _mask(panel, {("A", 1 if scheduled_at_open else 2)} if exit_phase == "close" or scheduled_at_open else set()),
        MatcherConfig(
            entry_fill="open_t+1",
            exit_fill="open_t+1" if scheduled_at_open else "close_t",
            stop_loss_pct=0.05 if stop else None,
            take_profit_pct=0.05 if stop else None,
            max_positions=1, fees_pct=0, slippage_bps=0,
        ),
    )

    assert result.trades[0].symbol == "A"
    assert result.trades[0].exit_date == "2024-01-03"
    assert result.trades[0].exit_reason == ("stop_loss" if exit_phase in {"intraday", "opening-gap"} else "signal")
    if scheduled_at_open or exit_phase == "opening-gap":
        assert [(trade.symbol, trade.entry_date) for trade in result.trades[1:]] == [("B", "2024-01-03")]
        assert result.stats["execution"]["buy_no_slot"] == 0
    else:
        assert len(result.trades) == 1
        assert result.stats["execution"]["buy_no_slot"] == 1


@pytest.mark.parametrize("legacy", [False, True], ids=["matrix", "legacy"])
@pytest.mark.parametrize("portfolio", [False, True], ids=["candidate", "portfolio"])
@pytest.mark.parametrize(
    ("terminal", "mark_price", "mark_date", "block_reason"),
    [
        ({"open": 9, "high": 9, "low": 9, "close": 9, "signal_limit_down": True},
         9, "2024-01-03", "sell_limit_down"),
        ({"volume": 0}, 10, "2024-01-02", "sell_suspended"),
        ({"close": None}, 10, "2024-01-02", "sell_invalid_price"),
        (None, 10, "2024-01-02", "sell_suspended"),
        ({}, 10, "2024-01-03", "sell_t_plus_one"),
    ],
    ids=["one-price-limit-down", "suspended", "invalid-close", "missing-bar", "same-day-entry"],
)
def test_blocked_terminal_exit_preserves_open_position(
    legacy, portfolio, terminal, mark_price, mark_date, block_reason,
):
    panel = _panel(["A", "B"], days=3, overrides={("A", 2): terminal or {}})
    if terminal is None:
        panel = panel.filter((pl.col("symbol") != "A") | (pl.col("date") != date(2024, 1, 3)))
    entry_day = 2 if block_reason == "sell_t_plus_one" else 0
    engine = _engine()
    method = "simulate_portfolio" if portfolio else "simulate_independent_candidates"
    simulate = getattr(engine, f"{method}_legacy" if legacy else method)
    result = simulate(
        panel, _mask(panel, {("A", entry_day)}), _mask(panel, set()),
        MatcherConfig(max_positions=1, initial_capital=2_000, fees_pct=0.01, slippage_bps=0),
    )

    assert result.trades == []
    assert result.stats["n_trades"] == 0
    assert result.stats["win_rate"] is None
    assert result.stats["execution"][block_reason] == 1
    assert result.stats["execution"]["pending_exit"] == 1
    assert result.stats["pending_exit_positions"] == 1
    [position] = result.stats["open_positions"]
    assert position["symbol"] == "A"
    assert position["shares"] == 100
    assert position["entry_date"] == ("2024-01-03" if entry_day == 2 else "2024-01-01")
    assert position["entry_value"] == 1_010
    assert position["mark_price"] == mark_price
    assert position["mark_date"] == mark_date
    assert position["mark_is_stale"] is (mark_date != "2024-01-03")
    assert position["market_value"] == mark_price * 100
    assert position["exit_block_reason"] == block_reason
    assert position["pending_exit_reason"] == "end"
    assert position["blocked_exit_days"] == 1
    if portfolio:
        final = result.equity_curve[-1]
        assert final["positions"] == 1
        assert final["cash"] == 990
        assert final["market_value"] == position["market_value"]
        assert final["value"] == result.stats["final_equity"] == 990 + final["market_value"]
    else:
        assert result.equity_curve == []
    json.dumps(result.stats, allow_nan=False)


@pytest.mark.parametrize("legacy", [False, True], ids=["matrix", "legacy"])
def test_cancelled_portfolio_values_open_lot_on_last_processed_session(legacy):
    cancel = Event()

    def request_cancel(progress):
        if progress["day"] == 1:
            cancel.set()

    panel = _panel(["A"], days=25)
    engine = _engine()
    simulate = engine.simulate_portfolio_legacy if legacy else engine.simulate_portfolio
    result = simulate(
        panel, _mask(panel, {("A", 0)}), None, MatcherConfig(),
        progress_cb=request_cancel, cancel_event=cancel,
    )

    assert result.trades == []
    [position] = result.stats["open_positions"]
    assert position["mark_date"] == result.equity_curve[-1]["date"]
    assert position["mark_is_stale"] is False
    assert position["pending_exit_reason"] is None


@pytest.mark.parametrize("legacy", [False, True], ids=["matrix", "legacy"])
@pytest.mark.parametrize("portfolio", [False, True], ids=["candidate", "portfolio"])
def test_final_open_entry_cannot_close_on_the_same_session(legacy, portfolio):
    panel = _panel(["A"], days=3, overrides={("A", 2): {"open": 9, "close": None}})
    engine = _engine()
    method = "simulate_portfolio" if portfolio else "simulate_independent_candidates"
    simulate = getattr(engine, f"{method}_legacy" if legacy else method)
    result = simulate(
        panel, _mask(panel, {("A", 1)}), _mask(panel, {("A", 2)}),
        MatcherConfig(entry_fill="open_t+1", exit_fill="close_t"),
    )

    assert result.trades == []
    assert result.stats["execution"]["sell_t_plus_one"] == 1
    [position] = result.stats["open_positions"]
    assert position["pending_exit_reason"] == "signal"
    assert position["entry_price"] == 9
    assert position["mark_price"] == 9
    assert position["mark_is_stale"] is True
    assert position["mark_date"] is None


@pytest.mark.parametrize("legacy", [False, True], ids=["matrix", "legacy"])
@pytest.mark.parametrize("portfolio", [False, True], ids=["candidate", "portfolio"])
def test_eligible_terminal_liquidation_records_fees_in_final_equity(legacy, portfolio):
    panel = _panel(["A"], days=3)
    engine = _engine()
    method = "simulate_portfolio" if portfolio else "simulate_independent_candidates"
    simulate = getattr(engine, f"{method}_legacy" if legacy else method)
    result = simulate(
        panel, _mask(panel, {("A", 0)}), _mask(panel, set()),
        MatcherConfig(max_positions=1, initial_capital=2_000, fees_pct=0.01, slippage_bps=0),
    )

    [trade] = result.trades
    assert trade.exit_reason == "end"
    assert trade.exit_date == "2024-01-03"
    assert trade.entry_value == 1_010
    assert trade.exit_value == 990
    assert result.stats["n_trades"] == 1
    assert result.stats["open_positions"] == []
    if portfolio:
        final = result.equity_curve[-1]
        assert final["positions"] == final["market_value"] == 0
        assert final["cash"] == final["value"] == result.stats["final_equity"] == 1_980
    else:
        assert result.equity_curve[-1]["value"] == 0.9802
