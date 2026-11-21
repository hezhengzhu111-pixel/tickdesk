"""历史股本解析。

财务股本按公告日可用，历史缺失时回退 instruments 最新流通股本。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from app.financial_history import point_in_time_report_timeline


def load_share_history(data_dir: Path) -> pl.DataFrame:
    """读取本地财务股本表；未同步或损坏时返回空表。"""
    path = data_dir / "financials" / "shares" / "part.parquet"
    if not path.exists():
        return pl.DataFrame()
    try:
        shares = pl.read_parquet(path)
        if not {"symbol", "period_end", "float_shares"} <= set(shares.columns):
            return pl.DataFrame()
        return shares
    except Exception:
        return pl.DataFrame()


def apply_historical_float_shares(
    rows: pl.DataFrame,
    shares: pl.DataFrame | None,
    *,
    today: date,
) -> pl.DataFrame:
    """为行情行解析有效流通股本。

    当日保留 rows.float_shares；历史日期使用公告日不晚于交易日的最新股本，
    找不到已公告的历史记录时继续使用 rows.float_shares。
    """
    required = {"symbol", "date", "float_shares"}
    if (
        rows.is_empty()
        or not required <= set(rows.columns)
        or shares is None
        or shares.is_empty()
        or not {"symbol", "period_end", "float_shares"} <= set(shares.columns)
    ):
        return rows

    numeric = pl.col("float_shares").cast(pl.Float64, strict=False)
    positive_shares = shares.with_columns(
        pl.when(numeric > 0).then(numeric).alias("float_shares")
    )
    history = point_in_time_report_timeline(
        positive_shares, ["float_shares"], include_announcement_day=True, require_field_value=True,
    )
    if history.is_empty():
        return rows
    history = history.select(
        "symbol", pl.col("_available").alias("_share_available_date"),
        pl.col("float_shares").alias("_historical_float_shares"),
    )

    resolved = (
        rows
        .with_row_index("_share_row_order")
        .with_columns(
            pl.col("symbol").cast(pl.Utf8),
            pl.col("date").cast(pl.Date, strict=False).alias("_share_trade_date"),
        )
        .sort(["symbol", "_share_trade_date"])
        .join_asof(
            history,
            left_on="_share_trade_date",
            right_on="_share_available_date",
            by="symbol",
            strategy="backward",
            check_sortedness=False,
        )
        .with_columns(
            pl.when(pl.col("_share_trade_date") == pl.lit(today))
            .then(pl.col("float_shares"))
            .otherwise(
                pl.coalesce("_historical_float_shares", "float_shares")
            )
            .alias("float_shares")
        )
        .sort("_share_row_order")
    )
    return resolved.drop(
        "_share_row_order",
        "_share_trade_date",
        "_share_available_date",
        "_historical_float_shares",
    )
