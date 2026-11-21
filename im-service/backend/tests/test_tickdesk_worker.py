import time
from types import SimpleNamespace

import pytest

from app.services.tickdesk_worker import run_bounded


def slow():
    time.sleep(30)


def answer():
    return {"value": 7}


def test_worker_returns_result_and_terminates_overdue_process():
    assert run_bounded(answer, (), timeout=5) == {"value": 7}
    started=time.monotonic()
    with pytest.raises(TimeoutError):
        run_bounded(slow, (), timeout=0.3)
    assert time.monotonic()-started < 3


def test_on_demand_research_has_its_own_budget_and_preserves_scheduled_worker(tmp_path, monkeypatch):
    from tickdesk_quant_fixture import CUTOFF_MS, build_fixture

    from app.services import tickdesk_worker
    from app.services.tickdesk_adapter import MarketAdapter
    from app.services.tickdesk_research import read_research_config

    api = build_fixture(tmp_path / "kernel")
    adapter = MarketAdapter(api.state.repo, api.state.strategy_engine)
    config = read_research_config(adapter.repo.store.data_dir)
    budgets = []
    request = {"requestedAt": CUTOFF_MS}
    scheduled = {"version": 3, "decisionCutoffMs": CUTOFF_MS, "observations": [{"origin": "scheduled"}]}
    on_demand = {**scheduled, "observations": [{"origin": "on_demand"}]}

    def run(_target, _args, *, timeout):
        budgets.append(timeout)
        if len(budgets) == 1:
            assert tickdesk_worker.research_in_worker(adapter, request, config) == scheduled
            return on_demand
        return scheduled

    monkeypatch.setattr(tickdesk_worker, "run_bounded", run)
    try:
        assert tickdesk_worker.research_in_worker(adapter, request, config, analysis_symbols=config["symbols"]) == on_demand
        assert len(budgets) == 2 and budgets[0] > 5 and budgets[1] == 5
        assert tickdesk_worker.research_in_worker(adapter, request, config) == scheduled
        assert len(budgets) == 2  # The on-demand result did not replace the scheduled cache.
    finally:
        adapter.repo.store.db.close()


@pytest.mark.parametrize(
    ("calendar", "error", "reason"),
    [
        (False, "SCAN_OUTSIDE_WINDOW", None),
        (True, "DATA_UNAVAILABLE", "NO_PRODUCTION_APPROVAL"),
        (None, "DATA_UNAVAILABLE", "TRADING_CALENDAR_UNAVAILABLE"),
    ],
)
def test_prepare_distinguishes_closed_calendar_and_unapproved_strategy(tmp_path, calendar, error, reason):
    from app.services.tickdesk_adapter import MarketAdapter

    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    adapter = MarketAdapter(repo, None, calendar=lambda day: calendar)
    result = adapter.prepare({"data": {"cmd": "scan_now"}, "requestedAt": 1794794400000})
    assert result["error"] == error
    assert result.get("productionReason") == reason
    assert "frozen" not in result
