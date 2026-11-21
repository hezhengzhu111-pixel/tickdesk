import json
from dataclasses import asdict, replace
from datetime import datetime

import pytest

from app.backtest.engine import MatcherConfig
from tests.backtest.test_engine_portfolio import _engine, _mask, _panel


def _ms(value):
    return int(datetime.fromisoformat(f"{value}+08:00").timestamp() * 1000)


def _model(*, available=True):
    from app.backtest.execution_timing import (
        ContinuousSession,
        DecisionPublication,
        EntryOpenTiming,
        RawMinuteOpen,
    )

    return EntryOpenTiming(
        publications=(DecisionPublication(
            "A", _ms("2024-01-01T15:00:00"), _ms("2024-01-01T15:20:00"),
            _ms("2024-01-02T09:30:08"), "synthetic-daily-publication",
        ),),
        sessions=(ContinuousSession(
            _ms("2024-01-02T09:30:00"), _ms("2024-01-02T11:30:00"), "synthetic-calendar",
        ),),
        minute_opens=tuple(RawMinuteOpen("A", _ms(f"2024-01-02T{clock}"), price, status, "synthetic-minute")
                           for clock, price, status in (
                               ("09:30:00", "7.50", "executable"),
                               ("09:31:00", "9.25", "blocked"),
                               ("09:32:00", "12.00", "executable" if available else "unknown"),
                           )),
        response_delay_ms=15_000,
    )


@pytest.mark.parametrize("available", [True, False])
def test_q28_only_later_proven_raw_minute_open_can_spend_cash(available):
    panel = _panel(["A"], days=3, overrides={
        ("A", 1): {"open": 110, "high": 111, "low": 109, "close": 110},
        ("A", 2): {"open": 110, "high": 111, "low": 109, "close": 110},
    })
    result = _engine().simulate_portfolio(
        panel, _mask(panel, {("A", 0)}), _mask(panel, {("A", 1)}), MatcherConfig(
            entry_fill="open_t+1", exit_fill="open_t+1", initial_capital=2_000,
            max_positions=1, fees_pct=0, slippage_bps=0, entry_open_timing=_model(available=available),
        ),
    )
    evidence = result.stats["entry_open_timing"]
    assert evidence["admission_eligible"] is False
    assert result.trades == []
    if not available:
        assert evidence["fills"] == []
        assert result.stats["execution"]["buy_entry_clock_unavailable"] == 1
        assert result.stats["open_positions"] == []
        assert result.equity_curve[-1]["cash"] == 2_000
        return

    [fill] = evidence["fills"]
    assert fill["bar_end_ms"] == _ms("2024-01-01T15:00:00")
    assert fill["available_at_ms"] == _ms("2024-01-01T15:20:00")
    assert fill["published_at_ms"] == _ms("2024-01-02T09:30:08")
    assert fill["ready_at_ms"] == _ms("2024-01-02T09:30:23")
    assert fill["fill_at_ms"] == _ms("2024-01-02T09:32:00")
    assert fill["raw_open_price"] == "12.00"
    assert result.equity_curve[-1]["cash"] == 800
    [position] = result.stats["open_positions"]
    assert position["shares"] == 100
    assert position["mark_price"] is None
    assert position["pending_exit_reason"] == "signal"
    assert position["exit_block_reason"] == "sell_exit_clock_unavailable"


def test_minute_clock_precedes_score_when_candidates_compete_for_one_slot():
    model = _model()
    first = replace(model.minute_opens[-1], symbol="B", at_ms=_ms("2024-01-02T09:31:00"), price="10.00")
    model = replace(
        model, publications=(*model.publications, replace(model.publications[0], symbol="B")),
        minute_opens=(*model.minute_opens, first),
    )
    panel = _panel(["A", "B"], days=3)
    result = _engine().simulate_portfolio(
        panel, _mask(panel, {("A", 0), ("B", 0)}), None, MatcherConfig(
            matching="open_t+1", initial_capital=2_000, max_positions=1,
            fees_pct=0, slippage_bps=0, entry_open_timing=model,
        ),
    )
    [fill] = result.stats["entry_open_timing"]["fills"]
    assert fill["symbol"] == "B"
    assert result.stats["execution"]["buy_no_slot"] == 1
    assert result.stats["ending_cash"] == 0


def test_entry_timing_evidence_round_trips_with_complete_digest():
    from app.backtest.execution_timing import EntryOpenTiming

    model = _model()
    payload = json.loads(json.dumps(asdict(model)))
    parsed = EntryOpenTiming.from_dict(payload)
    assert parsed.summary() == model.summary()
    assert parsed.resolve("A", "2024-01-01", "2024-01-02") == model.resolve("A", "2024-01-01", "2024-01-02")
    payload["minute_opens"][-1]["price"] = "12.01"
    assert EntryOpenTiming.from_dict(payload).summary()["evidence_sha256"] != model.summary()["evidence_sha256"]


def test_old_clock_only_evidence_keeps_its_model_and_sha256():
    from app.backtest.execution_timing import EntryOpenTiming

    payload = asdict(_model())
    payload.pop("require_capacity")
    for field in ("minute_opens", "exit_minute_opens"):
        for row in payload[field]:
            row.pop("available_shares")
    summary = EntryOpenTiming.from_dict(payload).summary()
    assert summary["model"] == "continuous_raw_minute_open_v2"
    assert summary["evidence_sha256"] == "9c8dcd4ef1a586153bbb653b91b299a28084cebb0aafe6629ac5a03529e17ab0"


@pytest.mark.parametrize("missing", [True, False])
def test_gap_or_unknown_minute_cannot_be_passed_over_to_claim_a_later_entry(missing):
    model = _model()
    rows = list(model.minute_opens)
    if missing:
        rows.pop(1)
    else:
        rows[1] = replace(rows[1], status="unknown")
    unsafe = replace(model, minute_opens=tuple(rows))
    assert unsafe.resolve("A", "2024-01-01", "2024-01-02") is None


@pytest.mark.parametrize("buy_later", [False, True])
def test_evidenced_exit_after_t_plus_one_funds_only_a_strictly_later_buy(buy_later):
    from app.backtest.execution_timing import (
        ContinuousSession,
        DecisionPublication,
        EntryOpenTiming,
        RawMinuteOpen,
    )

    model = _model()
    publish = DecisionPublication(
        "B", _ms("2024-01-02T15:00:00"), _ms("2024-01-02T15:20:00"),
        _ms("2024-01-03T09:30:08"), "synthetic-publication",
    )
    buy_minute = RawMinuteOpen("B", _ms("2024-01-03T09:31:00"), "10.00",
                              "blocked" if buy_later else "executable", "synthetic-buy-minute")
    serialized = json.loads(json.dumps(asdict(model)))
    serialized["publications"].append(asdict(publish))
    serialized["sessions"].append(asdict(ContinuousSession(
        _ms("2024-01-03T09:30:00"), _ms("2024-01-03T11:30:00"), "synthetic-calendar",
    )))
    serialized["minute_opens"].extend([asdict(buy_minute), asdict(replace(
        buy_minute, at_ms=_ms("2024-01-03T09:32:00"), status="executable",
    ))])
    serialized["exit_publications"] = [asdict(replace(publish, symbol="A"))]
    serialized["exit_minute_opens"] = [asdict(RawMinuteOpen(
        "A", _ms("2024-01-03T09:31:00"), "13.00", "executable", "synthetic-sell-minute",
    ))]
    timing = EntryOpenTiming.from_dict(serialized)
    panel = _panel(["A", "B"], days=3)
    result = _engine().simulate_portfolio(
        panel, _mask(panel, {("A", 0), ("B", 1)}), _mask(panel, {("A", 1)}), MatcherConfig(
            entry_fill="open_t+1", exit_fill="open_t+1", initial_capital=2000,
            max_positions=1, fees_pct=0, stamp_tax_pct=0.01, slippage_bps=0, entry_open_timing=timing,
        ),
    )
    [sale] = result.trades
    assert sale.entry_date == "2024-01-02" and sale.exit_date == "2024-01-03"
    assert sale.exit_signal_date == "2024-01-02" and sale.exit_price == 13
    assert sale.exit_value == 1287 and result.stats["n_trades"] == 1
    evidence = result.stats["entry_open_timing"]
    assert evidence["admission_eligible"] is False
    [exit_fill] = evidence["exit_fills"]
    assert exit_fill["fill_at_ms"] == _ms("2024-01-03T09:31:00") and exit_fill["shares"] == 100
    assert [row["symbol"] for row in evidence["fills"]] == (["A", "B"] if buy_later else ["A"])
    assert result.stats["ending_cash"] == (87 if buy_later else 2087)


def test_same_day_exit_is_pending_and_missing_exit_evidence_never_uses_daily_close():
    panel = _panel(["A"], days=3, overrides={
        ("A", 1): {"close": 25},
        ("A", 2): {"close": 40},
    })
    result = _engine().simulate_portfolio(
        panel, _mask(panel, {("A", 0)}), _mask(panel, {("A", 1)}), MatcherConfig(
            entry_fill="open_t+1", exit_fill="close_t", initial_capital=2000,
            max_positions=1, fees_pct=0, slippage_bps=0, entry_open_timing=_model(),
        ),
    )
    assert result.trades == []
    assert result.stats["execution"]["sell_t_plus_one"] == 1
    assert result.stats["execution"]["sell_exit_clock_unavailable"] == 1
    assert result.stats["entry_open_timing"]["exit_fills"] == []
    [position] = result.stats["open_positions"]
    assert position["pending_exit_signal_date"] == "2024-01-02" and position["mark_price"] is None
    assert result.stats["ending_cash"] == 800


def _capacity_replay(days):
    from app.backtest.execution_timing import (
        ContinuousSession,
        DecisionPublication,
        EntryOpenTiming,
        RawMinuteOpen,
    )

    clock = _model()
    exit_publish = DecisionPublication(
        "A", _ms("2024-01-02T15:00:00"), _ms("2024-01-02T15:20:00"),
        _ms("2024-01-03T09:30:08"), "test-exit-publication",
    )
    evidence = json.loads(json.dumps(asdict(clock)))
    evidence.update(require_capacity=True, exit_publications=[asdict(exit_publish)])
    evidence["minute_opens"][-1]["available_shares"] = 350
    evidence["exit_minute_opens"] = []
    for day, price, shares in [(3, "13.00", 100), (4, "14.00", 200)]:
        evidence["sessions"].append(asdict(ContinuousSession(
            _ms(f"2024-01-0{day}T09:30:00"), _ms(f"2024-01-0{day}T11:30:00"), "test-calendar",
        )))
        evidence["exit_minute_opens"].append({
            **asdict(RawMinuteOpen("A", _ms(f"2024-01-0{day}T09:31:00"), price, "executable", "test-sell-capacity")),
            "available_shares": shares,
        })
    evidence["exit_minute_opens"].insert(1, {
        **evidence["exit_minute_opens"][1], "at_ms": _ms("2024-01-04T09:30:00"), "available_shares": 0,
    })
    timing = EntryOpenTiming.from_dict(evidence)
    panel = _panel(["A"], days=days)
    panel = panel.with_columns(
        _mask(panel, {("A", 1)}).alias("signal_exit_original"),
        _mask(panel, {("A", 3)}).alias("signal_exit_later"),
    )
    return _engine().simulate_portfolio(
        panel, _mask(panel, {("A", 0)}), panel["signal_exit_original"] | panel["signal_exit_later"], MatcherConfig(
            entry_fill="open_t+1", exit_fill="close_t", initial_capital=6000, max_positions=1,
            fees_pct=0.01, stamp_tax_pct=0.005, slippage_bps=0, entry_open_timing=timing,
        ),
        exit_signal_ids=["signal_exit_original", "signal_exit_later"],
    )


@pytest.mark.parametrize("days", [3, 4])
def test_capacity_limits_fills_and_preserves_the_unsold_position_and_original_signal(days):
    result = _capacity_replay(days)
    recorded = result.stats["entry_open_timing"]
    assert recorded["admission_eligible"] is False
    [buy] = recorded["fills"]
    assert (buy["available_shares"], buy["requested_shares"], buy["shares"]) == (350, 400, 300)
    assert result.stats["execution"]["sell_t_plus_one"] == 1
    first, *later = result.trades
    assert (first.exit_date, first.exit_signal_date, first.shares) == ("2024-01-03", "2024-01-02", 100)
    assert first.exit_signal_id == "signal_exit_original"
    assert (first.entry_value, first.exit_value, first.pnl_amount) == (1212, 1280.5, 68.5)
    assert recorded["exit_fills"][0]["remaining_shares"] == 200
    if days == 3:
        assert not later
        [position] = result.stats["open_positions"]
        assert (position["shares"], position["entry_value"], position["pending_exit_signal_date"]) == (200, 2424, "2024-01-02")
        assert result.stats["ending_cash"] == 3644.5
    else:
        [last] = later
        assert (last.exit_date, last.exit_signal_date, last.shares) == ("2024-01-04", "2024-01-02", 200)
        assert last.exit_signal_id == "signal_exit_original"
        assert (last.entry_value, last.exit_value, last.pnl_amount) == (2424, 2758, 334)
        assert recorded["exit_fills"][1]["remaining_shares"] == 0
        assert result.stats["open_positions"] == [] and result.stats["ending_cash"] == 6402.5


@pytest.mark.parametrize(("capacity", "resolved"), [(None, False), (0, True), (99, True)])
def test_unknown_capacity_stops_matching_but_proven_sub_lot_capacity_can_be_skipped(capacity, resolved):
    from app.backtest.execution_timing import EntryOpenTiming

    model = _model()
    evidence = json.loads(json.dumps(asdict(model)))
    evidence["require_capacity"] = True
    evidence["minute_opens"][-1]["available_shares"] = capacity
    evidence["minute_opens"].append({
        **evidence["minute_opens"][-1], "at_ms": _ms("2024-01-02T09:33:00"), "available_shares": 100,
    })
    timing = EntryOpenTiming.from_dict(evidence)
    fill = timing.resolve("A", "2024-01-01", "2024-01-02")
    if resolved:
        assert fill["fill_at_ms"] == _ms("2024-01-02T09:33:00") and fill["available_shares"] == 100
    else:
        assert fill is None


@pytest.mark.parametrize("capacity", [-1, 10.0, True, "100"])
def test_capacity_must_be_an_integer_share_quantity(capacity):
    with pytest.raises(ValueError, match="nonnegative integer number of shares"):
        replace(_model().minute_opens[-1], available_shares=capacity)
