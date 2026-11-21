from datetime import date, datetime

import polars as pl
import pytest

from app.tickflow.repository import DataStore, KlineRepository

_DAY = date(2026, 11, 13)
_BAR = {"open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0, "volume": 1000, "amount": 11000.0}
_DAILY = {**_BAR, "date": _DAY}
_MINUTE = {**_BAR, "datetime": datetime(2026, 11, 13, 14, 55)}
_INSTRUMENT = {"name": "fixture", "code": "fixture", "exchange": "SZSE"}


def _write(root, directory, symbol, columns):
    path = root / directory / f"{symbol}.parquet"
    path.parent.mkdir(exist_ok=True)
    pl.DataFrame([{"symbol": symbol, **columns}]).write_parquet(path)


@pytest.mark.parametrize(
    ("view", "stock_dir", "etf_dir", "columns"),
    [
        ("kline_daily_all", "kline_daily", "kline_etf_daily", _DAILY),
        ("kline_enriched_all", "kline_daily_enriched", "kline_etf_enriched", _DAILY),
        ("kline_minute_all", "kline_minute", "kline_etf_minute", _MINUTE),
        ("instruments_all", "instruments", "instruments_etf", _INSTRUMENT),
    ],
)
def test_unified_view_keeps_row_provenance_across_legacy_partitions(
    tmp_path, view, stock_dir, etf_dir, columns,
):
    _write(tmp_path, stock_dir, "000001.SZ", {**columns, "source": "explicit-feed", "asset_type": "stock"})
    _write(tmp_path, stock_dir, "000002.SZ", columns)
    _write(tmp_path, etf_dir, "159001.SZ", columns)
    store = DataStore.readonly(tmp_path)
    try:
        schema = [row[0] for row in store.db.execute(f"DESCRIBE {view}").fetchall()]
        assert [name for name in schema if name.startswith("source")] == ["source"]
        assert [name for name in schema if name.startswith("asset_type")] == ["asset_type"]
        rows = store.db.execute(f"SELECT symbol, asset_type, source FROM {view} ORDER BY symbol").fetchall()
        assert rows == [
            ("000001.SZ", "stock", "explicit-feed"),
            ("000002.SZ", "stock", None),
            ("159001.SZ", "etf", None),
        ]
    finally:
        store.db.close()


def test_unified_instrument_view_excludes_legacy_etfs_from_index_partition(tmp_path):
    _write(tmp_path, "instruments_index", "000001.SH", {**_INSTRUMENT, "source": "explicit-index"})
    _write(tmp_path, "instruments_index", "510001.SH", {**_INSTRUMENT, "asset_type": "etf"})
    store = DataStore.readonly(tmp_path)
    try:
        assert store.db.execute("SELECT symbol, source FROM instruments_all").fetchall() == [
            ("000001.SH", "explicit-index"),
        ]
    finally:
        store.db.close()


def test_rebuild_updates_unified_view_when_a_provider_adds_source_column(tmp_path):
    _write(tmp_path, "kline_daily", "000001.SZ", _DAILY)
    store = DataStore.readonly(tmp_path)
    try:
        assert store.db.execute("SELECT source FROM kline_daily_all").fetchall() == [(None,)]
        _write(tmp_path, "kline_daily", "000002.SZ", {**_DAILY, "source": "explicit-feed"})
        KlineRepository(store).rebuild_views()
        rows = store.db.execute("SELECT symbol, source FROM kline_daily_all ORDER BY symbol").fetchall()
        assert rows == [("000001.SZ", None), ("000002.SZ", "explicit-feed")]
    finally:
        store.db.close()
