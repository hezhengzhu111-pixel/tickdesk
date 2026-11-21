import json
from datetime import datetime
from hashlib import sha256

import pytest

from app.backtest.evidence import json_digest, save_evidence
from app.services import tickdesk_admission_oos as oos
from app.services.tickdesk_adapter import timestamp
from app.services.tickdesk_regime_archive import INDEX_ARCHIVE, import_calendar

REGISTRATION = datetime.fromisoformat("2024-01-05T18:00:00+08:00")
REVIEW = timestamp("2024-01-11T19:00:00+08:00")
DAYS = ("2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05",
        "2024-01-08", "2024-01-09", "2024-01-10", "2024-01-11")


def calendar(root, session_count):
    sessions = DAYS[:session_count]
    body = json.dumps({"source": "fuyao", "through": sessions[-1], "sessions": sessions,
                       "observedAt": timestamp(f"{sessions[-1]}T16:00:00+08:00")}).encode()
    path = root / INDEX_ARCHIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    import_calendar(path, body, sha256(body).hexdigest())


def native_report(root, key, start, end, at, *, score=21, trades=0, strict=False, missing=False):
    config = {"strategy_id": "daily", "symbols": ["600000.SH"], "start": start, "end": end,
              "mode": "position", "asset_type": "stock", "params": {"score": score, "max_hold_bars": 2},
              "holding_days": 1, "entry_fill": "open_t+1", "exit_fill": "open_t+1", "slippage_bps": 4,
              "entry_open_timing": {"model": "minute", "response_delay_ms": 60000, "admission_eligible": strict}}
    record = {
        "schema_version": 1, "run_id": key, "created_at": at, "config": config, "config_sha256": json_digest(config),
        "runtime": {"code_sha256": "a" * 64}, "strategy": {"id": "daily", "backend": "matrix_native",
                                                               "changed_during_run": False, "sha256": "b" * 64},
        "data": {"datasets": {"daily_matrix": {"start": start, "end": end,
                                               "sessions": [day for day in DAYS if start <= day <= end
                                                            and not (missing and day == start)]}}},
        "execution_timing": {"status": "passed" if trades else "insufficient",
                             "minute_evidence": {"admission_eligible": strict, "matcher_ran": bool(trades),
                                                 "entry": {"count": trades}, "exit": {"count": trades}}},
        "lookahead": {"status": "passed", "differences": 0},
        "result_summary": {"n_trades": trades},
    }
    save_evidence(root, record)
    return {"runId": key, "sha256": json_digest(record)}


@pytest.fixture
def registered(tmp_path, monkeypatch):
    calendar(tmp_path, 4)
    train = native_report(tmp_path, "1212121212", *DAYS[:2], "2024-01-04T18:00:00+08:00")
    monkeypatch.setattr(oos, "_now", lambda: REGISTRATION)
    plan = oos.register_oos(tmp_path, train, gap_sessions=2, test_sessions=2, min_trades=2)
    return tmp_path, train, plan


def test_forward_window_ignores_unobserved_calendar_and_requires_real_gap(registered):
    root, selection, plan = registered
    request = {"planSha256": plan["sha256"]}
    waiting = oos.inspect_oos(root, selection, request, REVIEW)
    assert waiting["blockers"] == ["OOS_SESSIONS_PENDING"] and waiting["observedTestSessions"] == 0
    calendar(root, 8)
    # The completed calendar archive was received after this review cutoff.
    early = oos.inspect_oos(root, selection, request, timestamp("2024-01-11T12:00:00+08:00"))
    assert early["blockers"] == ["OOS_SESSIONS_PENDING"]
    result = oos.inspect_oos(root, selection, request, REVIEW)
    assert result["gap"] == list(DAYS[4:6])
    assert result["window"] == list(DAYS[6:])
    assert result["blockers"] == ["OOS_RESULT_REQUIRED"]
    with pytest.raises(ValueError, match="holding"):
        oos.register_oos(root, selection, gap_sessions=1, test_sessions=2, min_trades=2)


@pytest.mark.parametrize(("change", "blocker"), [
    ("overlap", "OOS_RESULT_IDENTITY_MISMATCH"),
    ("changed_params", "OOS_RESULT_IDENTITY_MISMATCH"),
    ("missing_session", "OOS_RESULT_IDENTITY_MISMATCH"),
    ("too_early", "OOS_RESULT_IDENTITY_MISMATCH"),
    ("empty", "OOS_TRADE_SAMPLE_INSUFFICIENT"),
    ("unqualified", "OOS_EXECUTION_UNVERIFIED"),
    ("none", None),
])
def test_frozen_configuration_and_strict_execution_are_verified(registered, change, blocker):
    root, selection, plan = registered
    calendar(root, 8)
    report = native_report(root, "2121212121", DAYS[5] if change == "overlap" else DAYS[6], DAYS[7],
                           "2024-01-11T12:00:00+08:00" if change == "too_early" else "2024-01-11T18:00:00+08:00",
                           score=22 if change == "changed_params" else 21, trades=0 if change == "empty" else 2,
                           strict=change != "unqualified", missing=change == "missing_session")
    request = {"planSha256": plan["sha256"], "result": report}
    actual = oos.inspect_oos(root, selection, request, REVIEW)
    assert actual["blockers"] == ([blocker] if blocker else [])


def test_plan_identity_is_bound_to_original_training_archive(registered):
    root, selection, plan = registered
    assert oos.inspect_oos(root, {**selection, "sha256": "0" * 64}, {"planSha256": plan["sha256"]},
                           REVIEW)["blockers"] == ["OOS_PLAN_SELECTION_MISMATCH"]
