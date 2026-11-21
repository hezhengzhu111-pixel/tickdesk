from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from threading import Barrier

import numpy as np
import polars as pl
import pytest

from app.backtest.fundamentals import (
    attach_fundamental_factors,
    build_fundamental_matrices,
    load_fundamental_snapshot,
)
from app.backtest.matrix import build_market_data_matrix
from app.services import financial_sync as fs
from app.share_capital import apply_historical_float_shares
from app.tickflow.capabilities import Cap, CapabilitySet
from app.tickflow.repository import DataStore, KlineRepository

SYMBOL = "600000.SH"


def _report(period, announced, roe, *, bps=None, source=None, observed=None):
    return pl.DataFrame({
        "symbol": [SYMBOL],
        "period_end": [period],
        "announce_date": [announced],
        "roe": [roe],
        "bps": [bps],
        "gross_margin": [30.0],
        "net_margin": [5.0],
        "revenue_yoy": [8.0],
        "net_income_yoy": [6.0],
        "debt_to_asset_ratio": [40.0],
        "_financial_source": [source],
        "_financial_observed_at": [observed],
    })


def _panel(*dates):
    return pl.DataFrame({
        "symbol": [SYMBOL] * len(dates),
        "date": [date.fromisoformat(value) for value in dates],
        "open": [10.0] * len(dates),
        "high": [11.0] * len(dates),
        "low": [9.0] * len(dates),
        "close": [10.0] * len(dates),
        "volume": [1000.0] * len(dates),
    })


def _assert_factors(root, panel, *, roe, pb):
    snapshot = load_fundamental_snapshot(root)
    names = ["roe_latest", "pb_latest"]
    attached = attach_fundamental_factors(panel, snapshot, names)
    market = build_market_data_matrix(panel)
    matrices = build_fundamental_matrices(market, snapshot, names)
    for name, expected in zip(names, (roe, pb), strict=True):
        assert attached[name].to_list() == expected
        np.testing.assert_allclose(
            matrices[name][:, 0], [np.nan if value is None else value for value in expected]
        )


def _assert_current(root, expected):
    columns = ["roe", "bps", "financial_conflicting_fields"]
    assert fs.get_financial_df(root, "metrics").select(columns).rows() == [expected]
    db = DataStore.readonly(root).db
    try:
        assert db.execute(
            "SELECT roe, bps, financial_conflicting_fields FROM financials_metrics"
        ).fetchall() == [expected]
    finally:
        db.close()


@pytest.mark.parametrize("write_order", ["combined", "split", "reversed"])
@pytest.mark.parametrize("other_source", ["tickflow", "other"])
def test_current_conflict_masks_only_disputed_fields(tmp_path, write_order, other_source):
    original = _report(
        "2026-03-31", "2026-04-01", 11.0, bps=5.0,
        source="tickflow", observed="2026-04-02T08:00:00+00:00",
    )
    observation = dict(observed="2026-04-20T08:00:00+00:00")
    first = _report("2026-03-31", "2026-04-01", 11.0, source="tickflow", **observation)
    second = _report("2026-03-31", "2026-04-01", 22.0, bps=4.0, source=other_source, **observation)
    if write_order == "split":
        fs._write_table("metrics", fs._merge_report_history(original, first), tmp_path)
        initial = fs.get_financial_history(tmp_path, "metrics")
        history = fs._merge_report_history(initial, second, second)
    elif write_order == "reversed":
        history = fs._merge_report_history(second, first, second, original)
    else:
        history = fs._merge_report_history(original, pl.concat([first, second], how="diagonal_relaxed"), second)
    fs._write_table("metrics", history, tmp_path)

    # Projection must keep the compatible BPS and withhold the disputed ROE.
    assert fs.get_financial_df(tmp_path, "metrics")["roe"].to_list() == [None]
    _assert_current(tmp_path, (None, 4.0, ["roe"]))
    assert sorted(history["roe"].to_list()) == [11.0, 11.0, 22.0]
    assert fs.get_financial_history(tmp_path, "metrics").to_dicts() == history.to_dicts()
    _assert_factors(tmp_path, _panel("2026-04-20", "2026-04-21"), roe=[11.0, 11.0], pb=[2.0, 2.0])


def test_current_conflict_requires_later_confirmation_of_each_affected_field(tmp_path):
    def capture(roe, bps, observed):
        return _report("2026-03-31", "2026-04-01", roe, bps=bps, source="tickflow", observed=observed)

    # Equivalent UTC offsets represent one clock instant. A later sparse
    # payload confirms BPS alone and cannot resolve the ROE conflict.
    history = fs._merge_report_history(
        capture(11.0, 4.0, "2026-04-20T08:00:00+00:00"),
        capture(22.0, 5.0, "2026-04-20T16:00:00+08:00"),
        capture(None, 6.0, "2026-04-24T08:00:00+00:00"),
    )
    fs._write_table("metrics", history, tmp_path)
    _assert_current(tmp_path, (None, 6.0, ["roe"]))

    confirmation = capture(33.0, None, "2026-04-25T08:00:00+00:00")
    fs._write_table("metrics", fs._merge_report_history(history, confirmation), tmp_path)
    _assert_current(tmp_path, (33.0, 6.0, []))


async def test_financial_consumers_receive_conflict_as_unknown(tmp_path):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import financials
    from app.custom.assistant import tools as assistant_tools
    from app.services.financial_analyzer import _load_stock_financials
    from app.services.stock_analyzer import _load_financials

    history = fs._merge_report_history(
        _report("2026-03-31", "2026-04-01", 11.0, bps=5.0),
        _report("2026-03-31", "2026-04-01", 22.0, bps=5.0),
    )
    fs._write_table("metrics", history, tmp_path)
    app = FastAPI()
    app.include_router(financials.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.capabilities = CapabilitySet()
    app.state.capabilities.grant(Cap.FINANCIAL)
    with TestClient(app) as client:
        response = client.get("/api/financials/metrics", params={"symbol": SYMBOL})
        assert response.status_code == 200
    for rows in (
        response.json()["data"],
        _load_stock_financials(tmp_path, SYMBOL)["metrics"],
        _load_financials(tmp_path, SYMBOL)["metrics"],
    ):
        assert rows[0]["roe"] is None
        assert rows[0]["bps"] == 5.0
        assert rows[0]["financial_conflicting_fields"] == ["roe"]

    result = await assistant_tools.execute_assistant_tool(
        "get_financials", {"symbol": SYMBOL}, assistant_tools.ToolContext.build(data_dir=tmp_path),
    )
    assert result["ok"] is True
    row = result["result"]["rows"][0]
    assert row.get("roe") is None
    assert row["bps"] == 5.0
    assert row["financial_conflicting_fields"] == ["roe"]


def test_revisions_are_visible_only_after_their_own_announcement(tmp_path):
    history = fs._merge_report_history(
        _report("2026-03-31", "2026-04-01", 11.0, bps=5.0),
        _report("2026-03-31", "2026-04-20", 22.0),
    )
    fs._write_table("metrics", history, tmp_path)

    assert history["roe"].to_list() == [11.0, 22.0]
    assert history["bps"].to_list() == [5.0, None]
    _assert_factors(
        tmp_path, _panel("2026-04-01", "2026-04-02", "2026-04-20", "2026-04-21"),
        roe=[None, 11.0, 11.0, 22.0], pb=[None, 2.0, 2.0, 2.0],
    )
    assert fs.get_financial_df(tmp_path, "metrics").select("roe", "bps").rows() == [
        (22.0, 5.0)
    ]


def test_pit_ignores_unknown_announcement_and_late_old_period(tmp_path):
    # Direct write deliberately exercises historical files without new metadata.
    history = pl.concat([
        _report("2025-12-31", "2026-04-01", 11.0, bps=5.0),
        _report("2026-03-31", "2026-04-15", 22.0),
        _report("2025-12-31", "2026-04-20", 33.0, bps=2.0),
        _report("2026-03-31", None, 99.0, bps=10.0),
        _report("2026-03-31", "2026-05-01", 44.0, bps=4.0),
    ], how="diagonal_relaxed").drop("_financial_source", "_financial_observed_at")
    fs._write_table("metrics", history, tmp_path)

    _assert_factors(
        tmp_path, _panel("2026-04-02", "2026-04-16", "2026-04-21", "2026-05-04"),
        roe=[11.0, 22.0, 22.0, 44.0], pb=[2.0, None, None, 2.5],
    )


def test_same_announcement_revision_uses_when_system_observed_it(tmp_path):
    original = _report(
        "2026-03-31", "2026-04-01", 11.0,
        source="tickflow", observed="2026-04-02T08:00:00+00:00",
    )
    revision = _report(
        "2026-03-31", "2026-04-01", 22.0,
        source="tickflow", observed="2026-04-20T16:30:00+00:00",
    )
    fs._write_table("metrics", fs._merge_report_history(original, revision), tmp_path)

    # 16:30 UTC is already April 21 in Shanghai. Eligibility is strictly later.
    _assert_factors(
        tmp_path, _panel("2026-04-15", "2026-04-20", "2026-04-21", "2026-04-22"),
        roe=[11.0, 11.0, 11.0, 22.0], pb=[None] * 4,
    )


@pytest.mark.parametrize(
    ("write_order", "correction_source"),
    [((0, 1, 2), "tickflow"), ((0, 2, 1), "tickflow"), ((0, 1, 2), "other")],
    ids=["in-order", "out-of-order", "cross-source"],
)
def test_same_announcement_reversion_is_an_observed_revision(
    tmp_path, monkeypatch, write_order, correction_source,
):
    def capture(roe, observed, source="tickflow"):
        return _report("2026-03-31", "2026-04-01", roe, source=source, observed=observed)

    captures = [
        capture(11.0, "2026-04-02T08:00:00+00:00"),
        capture(22.0, "2026-04-20T08:00:00+00:00", correction_source),
        capture(11.0, "2026-04-25T08:00:00+00:00"),
        capture(11.0, "2026-04-25T08:00:00+00:00"),
        capture(11.0, "2026-04-27T08:00:00+00:00"),
    ]
    responses = iter([*(captures[index] for index in write_order), *captures[3:]])
    monkeypatch.setattr(fs, "_fetch_table", lambda *args, **kwargs: next(responses))
    counts = [
        fs._sync_history_table_for_symbols("metrics", [SYMBOL], tmp_path, CapabilitySet())
        for _ in write_order
    ]

    assert fs.get_financial_df(tmp_path, "metrics")["roe"].to_list() == [11.0]
    db = DataStore.readonly(tmp_path).db
    try:
        assert db.execute("SELECT roe FROM financials_metrics").fetchall() == [(11.0,)]
    finally:
        db.close()
    _assert_factors(
        tmp_path, _panel("2026-04-02", "2026-04-20", "2026-04-24", "2026-04-25", "2026-04-27"),
        roe=[11.0, 11.0, 22.0, 22.0, 11.0], pb=[None] * 5,
    )
    counts.extend(
        fs._sync_history_table_for_symbols("metrics", [SYMBOL], tmp_path, CapabilitySet())
        for _ in captures[3:]
    )
    history = fs.get_financial_history(tmp_path, "metrics")
    assert counts == [1, 2, 3, 3, 4]
    assert history["roe"].to_list() == [11.0, 22.0, 11.0, 11.0]
    assert history["_financial_content_id"].n_unique() == 2
    assert history["_financial_revision_id"].n_unique() == 4
    assert history["_financial_observed_at"].to_list() == [
        "2026-04-02T08:00:00+00:00", "2026-04-20T08:00:00+00:00",
        "2026-04-25T08:00:00+00:00", "2026-04-27T08:00:00+00:00",
    ]
    assert fs._merge_report_history(*reversed(captures)).to_dicts() == history.to_dicts()
    assert fs._merge_report_history(history, *captures).to_dicts() == history.to_dicts()


@pytest.mark.parametrize("competing_source", ["tickflow", "other"])
@pytest.mark.parametrize("split_write", [False, True], ids=["together", "split"])
def test_conflicting_capture_batch_does_not_confirm_a_revision(tmp_path, competing_source, split_write):
    def capture(roe, observed, source="tickflow"):
        return _report("2026-03-31", "2026-04-01", roe, source=source, observed=observed)

    original = capture(11.0, "2026-04-02T08:00:00+00:00")
    batch_time = "2026-04-20T08:00:00+00:00"
    unchanged = capture(11.0, batch_time)
    conflict = capture(22.0, batch_time, competing_source)
    confirmed = capture(22.0, "2026-04-25T08:00:00+00:00")
    if split_write:
        fs._write_table("metrics", fs._merge_report_history(original, unchanged), tmp_path)
        seed = fs.get_financial_history(tmp_path, "metrics")
        history = fs._merge_report_history(seed, conflict, confirmed)
    else:
        history = fs._merge_report_history(original, pl.concat([unchanged, conflict]), confirmed)
    fs._write_table("metrics", history, tmp_path)

    _assert_factors(
        tmp_path, _panel("2026-04-21", "2026-04-25", "2026-04-27"),
        roe=[11.0, 11.0, 22.0], pb=[None] * 3,
    )
    assert history.height == 4
    assert history.to_dicts() == fs._merge_report_history(
        confirmed, conflict, unchanged, original,
    ).to_dicts()
    assert fs.get_financial_df(tmp_path, "metrics")["roe"].to_list() == [22.0]
    db = DataStore.readonly(tmp_path).db
    try:
        assert db.execute("SELECT roe FROM financials_metrics").fetchall() == [(22.0,)]
    finally:
        db.close()


def test_same_announcement_with_unknown_revision_order_fails_closed(tmp_path):
    history = fs._merge_report_history(
        _report("2026-03-31", "2026-04-01", 11.0),
        _report("2026-03-31", "2026-04-01", 22.0),
    )
    fs._write_table("metrics", history, tmp_path)
    _assert_factors(tmp_path, _panel("2026-04-21"), roe=[None], pb=[None])
    assert history.height == 2


def test_derived_provider_snapshot_obeys_observation_time(tmp_path, monkeypatch):
    from app.data_providers import custom
    from app.services import preferences

    class Provider:
        def get_financials(self, table, symbols, latest_only):
            return _report("2026-03-31", "2026-04-01", 11.0, bps=5.0).with_columns(
                pl.lit(True).alias("_financial_observation_only")
            )

    class ObservationClock:
        @staticmethod
        def now(tz):
            return datetime(2026, 4, 20, 16, 30, tzinfo=tz)

    monkeypatch.setattr(fs, "_financial_is_custom", lambda: True)
    monkeypatch.setattr(preferences, "get_financial_provider", lambda: "fake-provider")
    monkeypatch.setattr(custom, "get_provider", lambda name: Provider())
    monkeypatch.setattr(fs, "datetime", ObservationClock)
    fs._sync_history_table_for_symbols("metrics", [SYMBOL], tmp_path, CapabilitySet())

    _assert_factors(
        tmp_path, _panel("2026-04-02", "2026-04-21", "2026-04-22"),
        roe=[None, None, 11.0], pb=[None, None, 2.0],
    )
    stored = pl.read_parquet(tmp_path / "financials" / "metrics" / "part.parquet")
    assert stored["_financial_source"].item() == "fake-provider"
    assert stored["_financial_observed_at"].item() == "2026-04-20T16:30:00+00:00"


def test_revision_identity_is_stable_across_source_order_schema_and_retries():
    original = _report(
        "2026-03-31", date(2026, 4, 1), 11.0,
        source="tickflow", observed="2026-04-02T08:00:00+00:00",
    )
    retry = _report(
        "2026-03-31", "2026-04-01", 11.0,
        source="tickflow", observed="2026-04-02T08:00:00+00:00",
    ).with_columns(pl.lit(None, dtype=pl.Float64).alias("extra_metric"))
    other_source = _report("2026-03-31", "2026-04-01", 11.0, source="other")
    history = fs._merge_report_history(original, retry, other_source)

    assert history.height == 2
    assert history["_financial_revision_id"].n_unique() == 2
    assert history["_financial_content_id"].n_unique() == 1
    assert history.filter(pl.col("_financial_source") == "tickflow")[
        "_financial_observed_at"
    ].item() == "2026-04-02T08:00:00+00:00"
    assert history.to_dicts() == fs._merge_report_history(other_source, retry, original).to_dicts()


def test_sync_keeps_archived_revisions_and_current_sql_view(tmp_path, monkeypatch):
    original = _report("2026-03-31", "2026-04-01", 11.0, bps=5.0)
    revision = _report("2026-03-31", "2026-04-20", 22.0)
    later_period = _report("2026-06-30", "2026-07-04", 33.0, bps=4.0)
    observed_tables = iter([original, revision, later_period, revision])
    monkeypatch.setattr(fs, "_fetch_table", lambda *args, **kwargs: next(observed_tables))

    for _ in range(4):
        fs._sync_history_table_for_symbols("metrics", [SYMBOL], tmp_path, CapabilitySet())

    history = pl.read_parquet(tmp_path / "financials" / "metrics" / "part.parquet")
    assert history["roe"].to_list() == [11.0, 22.0, 33.0]
    expected = [("2026-03-31", 22.0, 5.0), ("2026-06-30", 33.0, 4.0)]
    assert fs.get_financial_df(tmp_path, "metrics").select("period_end", "roe", "bps").rows() == expected
    db = DataStore.readonly(tmp_path).db
    try:
        assert db.execute(
            "SELECT period_end, roe, bps FROM financials_metrics ORDER BY period_end"
        ).fetchall() == expected
    finally:
        db.close()


def test_sql_current_view_hides_revisions_in_legacy_schema(tmp_path):
    history = pl.concat([
        _report("2026-03-31", "2026-04-01", 11.0, bps=5.0),
        _report("2026-03-31", "2026-04-20", 22.0),
    ], how="diagonal_relaxed").drop("_financial_source", "_financial_observed_at")
    fs._write_table("metrics", history, tmp_path)
    db = DataStore.readonly(tmp_path).db
    try:
        assert db.execute("SELECT roe, bps FROM financials_metrics").fetchall() == [(22.0, 5.0)]
    finally:
        db.close()


def test_scheduler_rebinds_current_sql_view_after_schema_growth(tmp_path, monkeypatch):
    from app.services import preferences

    existing = _report("2026-03-31", "2026-04-01", 11.0, bps=5.0).drop(
        "_financial_source", "_financial_observed_at",
    )
    fs._write_table("metrics", existing, tmp_path)
    revised = _report("2026-03-31", "2026-04-20", 22.0).with_columns(
        pl.lit(3.0).alias("new_metric")
    )
    monkeypatch.setattr(fs, "_get_symbols", lambda *_: [SYMBOL])
    monkeypatch.setattr(fs, "_fetch_table", lambda *args, **kwargs: revised)
    monkeypatch.setattr(preferences, "get_financial_sync_times", lambda: {"metrics": "previous"})
    monkeypatch.setattr(preferences, "set_financial_sync_time", lambda *_: None)
    repo = KlineRepository(DataStore.readonly(tmp_path))
    try:
        assert repo.execute_all("SELECT roe FROM financials_metrics") == [(11.0,)]
        scheduler = fs.FinancialScheduler()
        caps = CapabilitySet()
        caps.grant(Cap.FINANCIAL)
        scheduler.start(tmp_path, caps, refresh_view=repo.refresh_financial_view)
        assert scheduler.run_now("metrics") == {"metrics": 2}
        assert repo.execute_all(
            "SELECT roe, bps, new_metric FROM financials_metrics"
        ) == [(22.0, 5.0, 3.0)]
    finally:
        repo.db.close()


def test_overlapping_syncs_keep_both_provider_responses(tmp_path, monkeypatch):
    original = _report("2026-03-31", "2026-04-01", 11.0, bps=5.0)
    fs._write_table("metrics", original, tmp_path)
    both_fetched = Barrier(2, timeout=10)
    responses = iter([("2026-04-20", 22.0), ("2026-04-30", 33.0)])

    def fetch(table, symbols, capset, latest_only):
        day, roe = next(responses)
        both_fetched.wait()
        return _report("2026-03-31", day, roe)

    monkeypatch.setattr(fs, "_fetch_table", fetch)
    with ThreadPoolExecutor(max_workers=2) as executor:
        syncs = [
            executor.submit(
                fs._sync_history_table_for_symbols, "metrics", [SYMBOL], tmp_path, CapabilitySet(),
            )
            for _ in range(2)
        ]
        assert sorted(sync.result() for sync in syncs) == [2, 3]

    assert fs.get_financial_history(tmp_path, "metrics")["roe"].to_list() == [11.0, 22.0, 33.0]


def test_share_history_ignores_unknown_announcement_and_late_old_period():
    shares = pl.DataFrame({
        "symbol": [SYMBOL] * 4,
        "period_end": ["2025-12-31", "2026-03-31", "2025-12-31", "2026-06-30"],
        "announce_date": ["2026-04-01", "2026-04-15", "2026-04-20", None],
        "float_shares": [11.0, 22.0, 33.0, 99.0],
    })
    panel = _panel("2026-04-02", "2026-04-16", "2026-04-21", "2026-07-01")
    panel = panel.with_columns(pl.lit(100.0).alias("float_shares"))
    resolved = apply_historical_float_shares(panel, shares, today=date(2026, 11, 15))

    assert resolved["float_shares"].to_list() == [11.0, 22.0, 22.0, 22.0]


def test_share_correction_requires_later_session_after_actual_observation():
    shares = pl.DataFrame({
        "symbol": [SYMBOL] * 2,
        "period_end": ["2026-03-31"] * 2,
        "announce_date": ["2026-04-01"] * 2,
        "_financial_observed_at": ["2026-04-01T07:00:00+00:00", "2026-04-20T16:30:00+00:00"],
        "float_shares": [11.0, 22.0],
    })
    panel = _panel("2026-04-01", "2026-04-21", "2026-04-22")
    panel = panel.with_columns(pl.lit(100.0).alias("float_shares"))
    resolved = apply_historical_float_shares(panel, shares, today=date(2026, 11, 15))

    assert resolved["float_shares"].to_list() == [11.0, 11.0, 22.0]


def test_share_history_uses_older_eligible_period_if_new_report_has_no_positive_value():
    shares = pl.DataFrame({
        "symbol": [SYMBOL] * 2,
        "period_end": ["2025-12-31", "2026-03-31"],
        "announce_date": ["2026-04-01", "2026-04-20"],
        "float_shares": [11.0, 0.0],
    })
    panel = _panel("2026-04-02", "2026-04-21").with_columns(pl.lit(100.0).alias("float_shares"))
    resolved = apply_historical_float_shares(panel, shares, today=date(2026, 11, 15))

    assert resolved["float_shares"].to_list() == [11.0, 11.0]


@pytest.mark.parametrize("announced", [None, "unknown", "2026-02-30"])
def test_invalid_announcement_remains_unavailable(tmp_path, announced):
    fs._write_table(
        "metrics", _report("2026-03-31", announced, 11.0, bps=5.0), tmp_path,
    )
    assert load_fundamental_snapshot(tmp_path) is None
