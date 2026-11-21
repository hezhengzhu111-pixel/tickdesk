from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.services.tickdesk_regime_evidence import (
    INDUSTRY_FIELD,
    MARKET_FIELD,
    RULE_ID,
    ObservedData,
    evaluate_for_decision,
    index_direction,
)

CN = ZoneInfo("Asia/Shanghai")
SYMBOL = "300502.SZ"
MARKET = "000300.SH"
INDUSTRY = "881129.TI"
DAY = date(2026, 11, 17)
CUTOFF = datetime.combine(DAY, time(15), CN)
SESSION_DAYS = [
    DAY - timedelta(days=distance) for distance in range(30, 0, -1)
    if (DAY - timedelta(days=distance)).weekday() < 5
][-20:]


def bar(day, close=100):
    return {
        "date_ms": int(datetime.combine(day, time.min, CN).timestamp() * 1000),
        "open_price": close, "high_price": close, "low_price": close, "close_price": close,
        "volume": 12, "turnover": 100,
    }


def series(last=100):
    return [bar(day, last if day == SESSION_DAYS[-1] else 100) for day in SESSION_DAYS]


def observed(data, when, char="a"):
    return ObservedData(data, when, char * 64)


def indices(*groups, when=None):
    return observed({"interval": "1d", "adjust": None,
                     "item": [{"thscode": code, "item": rows} for code, rows in groups]},
                    when if when is not None else CUTOFF.replace(hour=9), "c")


def membership(*codes, when=None):
    return observed({"item": [{"thscode": SYMBOL, "index_list": [
        {"thscode": code, "type": "industry"} for code in codes
    ]}]}, when if when is not None else CUTOFF.replace(hour=10), "d")


def calendar(*, include_day=True, when=None):
    days = [*SESSION_DAYS, DAY] if include_day else SESSION_DAYS
    return observed([day.isoformat() for day in days],
                    when if when is not None else CUTOFF.replace(hour=8), "b")


@pytest.mark.parametrize(("close", "expected"), [(101, 1), (100, 0), (99, 0), ("100.0000000001", 1)])
def test_strict_sma_threshold_without_rounding(close, expected):
    assert index_direction(series(close), SESSION_DAYS) == expected


def test_complete_independent_facts_preserve_their_observation_clocks():
    result = evaluate_for_decision(
        SYMBOL, CUTOFF, calendar=calendar(),
        indices=indices((MARKET, series(101)), (INDUSTRY, series(99))), membership=membership(INDUSTRY),
    )
    market, industry = result[MARKET_FIELD], result[INDUSTRY_FIELD]
    assert (market.value, market.index_code, market.available_at) == (1, MARKET, CUTOFF.replace(hour=9))
    assert (industry.value, industry.index_code, industry.available_at) == (0, INDUSTRY, CUTOFF.replace(hour=10))
    assert market.rule_id == industry.rule_id == RULE_ID
    assert market.evidence_hash != industry.evidence_hash


def test_membership_at_cutoff_visible_but_future_or_yesterdays_membership_unusable():
    for clock, result_value in [(CUTOFF, 1), (CUTOFF + timedelta(milliseconds=1), None),
                                (CUTOFF - timedelta(days=1), None)]:
        result = evaluate_for_decision(
            SYMBOL, CUTOFF, calendar=calendar(),
            indices=indices((MARKET, series(101)), (INDUSTRY, series(101))),
            membership=membership(INDUSTRY, when=clock),
        )
        assert result[MARKET_FIELD].value == 1
        assert result[INDUSTRY_FIELD].value == result_value


def test_missing_or_ambiguous_membership_and_historical_market_only():
    for source in [None, membership(), membership(INDUSTRY, "881155.TI"), membership(INDUSTRY, INDUSTRY)]:
        result = evaluate_for_decision(
            SYMBOL, CUTOFF, calendar=calendar(),
            indices=indices((MARKET, series(99)), (INDUSTRY, series(101))), membership=source,
        )
        assert result[MARKET_FIELD].value == 0
        assert result[INDUSTRY_FIELD].value is None


@pytest.mark.parametrize("rows", [
    series()[1:], series()[:-1], [*series(), bar(SESSION_DAYS[-1])], [*series(), bar(DAY)],
    [*series()[:-1], {**series()[-1], "high_price": 0}],
    [*series()[:-1], {**series()[-1], "close_price": "NaN"}],
    [*series()[:-1], {**series()[-1], "date_ms": series()[-1]["date_ms"] + 1}],
])
def test_broken_or_future_index_data_does_not_become_a_direction(rows):
    assert index_direction(rows, SESSION_DAYS) is None
    result = evaluate_for_decision(SYMBOL, CUTOFF, calendar=calendar(),
        indices=indices((MARKET, rows), (INDUSTRY, series(101))), membership=membership(INDUSTRY))
    assert result[MARKET_FIELD].value is None
    assert result[INDUSTRY_FIELD].value == 1


def test_calendar_requires_a_verified_decision_session_and_past_availability():
    for source in [calendar(include_day=False), calendar(when=CUTOFF + timedelta(seconds=1))]:
        result = evaluate_for_decision(SYMBOL, CUTOFF, calendar=source,
            indices=indices((MARKET, series(101)), (INDUSTRY, series(101))), membership=membership(INDUSTRY))
        assert all(fact.value is None for fact in result.values())


def test_index_observation_must_follow_finality_buffer_and_precede_decision():
    latest = datetime.combine(SESSION_DAYS[-1], time(15, 30), CN)
    for clock in [latest - timedelta(seconds=1), CUTOFF + timedelta(milliseconds=1)]:
        result = evaluate_for_decision(SYMBOL, CUTOFF, calendar=calendar(),
            indices=indices((MARKET, series(101)), (INDUSTRY, series(101)), when=clock),
            membership=membership(INDUSTRY))
        assert all(fact.value is None for fact in result.values())
    exact = evaluate_for_decision(SYMBOL, CUTOFF, calendar=calendar(),
        indices=indices((MARKET, series(101)), (INDUSTRY, series(101)), when=latest),
        membership=membership(INDUSTRY))
    assert exact[MARKET_FIELD].value == exact[INDUSTRY_FIELD].value == 1


def test_source_conflict_and_revision_remain_visible_in_selected_evidence():
    source = indices((MARKET, series(101)), (MARKET, series(102)), (INDUSTRY, series(101)))
    facts = evaluate_for_decision(SYMBOL, CUTOFF, calendar=calendar(), indices=source,
        membership=membership(INDUSTRY))
    assert facts[MARKET_FIELD].reason == "incomplete_or_invalid_index_window"
    assert facts[INDUSTRY_FIELD].value == 1

    revised = evaluate_for_decision(SYMBOL, CUTOFF, calendar=calendar(),
        indices=indices((MARKET, series(101)), (INDUSTRY, series(102))), membership=membership(INDUSTRY))
    assert revised[INDUSTRY_FIELD].value == facts[INDUSTRY_FIELD].value
    assert revised[INDUSTRY_FIELD].evidence_hash != facts[INDUSTRY_FIELD].evidence_hash
