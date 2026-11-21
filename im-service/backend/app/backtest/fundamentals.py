"""财务因子: 基于本地财务快照的点时 (point-in-time) 无未来函数接入。

数据契约:
- 输入为 data/financials/metrics/part.parquet, 每行是一份保留的报告修订;
- ``announce_date`` 是公告日。因子只在 **严格晚于公告日的交易日** 才有值
  (公告多在盘后发布, 保守取 T+1 生效), 此前保持 null;
- 先按交易日选择已可用的最高报告期，再使用该期已可用的修订;
- 无财务数据的标的/日期一律为 null, 绝不填 0 (填 0 会污染截面排名,
  例如资产负债率 0 会被当成最优杠杆)。下游 IC/分层/评分对 null 自动剔除。

性能:
- 财务表约数千行, join_asof 按 symbol 分组回填, 对百万行面板的代价是
  毫秒级; 矩阵路径每个因子只物化一张 float32 TxN 矩阵 (T~900, N~5500
  约 20MB), 且仅在策略/挖掘请求该因子时才构建。
"""
from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Any

import numpy as np
import polars as pl

from app.financial_history import PROVENANCE, point_in_time_report_timeline, report_date
from app.services.financial_sync import get_financial_history

# 财务因子名 -> (metrics 表列名, 是否需要除以收盘价)
# pb_latest 单列声明为 bps 倒数口径: 因子值 = close / bps。
FUNDAMENTAL_FACTORS: dict[str, dict[str, Any]] = {
    "pb_latest": {"column": "bps", "price_ratio": True},
    "roe_latest": {"column": "roe", "price_ratio": False},
    "gross_margin_latest": {"column": "gross_margin", "price_ratio": False},
    "net_margin_latest": {"column": "net_margin", "price_ratio": False},
    "revenue_yoy_latest": {"column": "revenue_yoy", "price_ratio": False},
    "net_income_yoy_latest": {"column": "net_income_yoy", "price_ratio": False},
    "debt_ratio_latest": {"column": "debt_to_asset_ratio", "price_ratio": False},
}

FUNDAMENTAL_FACTOR_NAMES = frozenset(FUNDAMENTAL_FACTORS)


def load_fundamental_snapshot(data_dir: Path | None) -> pl.DataFrame | None:
    """读取财务指标快照; 文件缺失或无有效行时返回 None。

    返回有效公告的原始修订，保留报告期和已有来源证据。
    """
    if data_dir is None:
        return None
    frame = get_financial_history(data_dir, "metrics")
    required = {"symbol", "period_end", "announce_date"}
    metric_columns = {spec["column"] for spec in FUNDAMENTAL_FACTORS.values()}
    if not required <= set(frame.columns) or not metric_columns.intersection(frame.columns):
        return None
    snapshot = (
        frame.select(sorted((required | metric_columns | set(PROVENANCE)).intersection(frame.columns)))
        .filter(
            pl.col("symbol").is_not_null()
            & report_date(pl.col("announce_date")).is_not_null()
            & report_date(pl.col("period_end")).is_not_null()
        )
    )
    if snapshot.is_empty():
        return None
    return snapshot


def _factor_timeline(snapshot: pl.DataFrame | None) -> pl.DataFrame:
    if snapshot is None:
        return pl.DataFrame()
    fields = sorted({spec["column"] for spec in FUNDAMENTAL_FACTORS.values()})
    return point_in_time_report_timeline(snapshot, fields)


def attach_fundamental_factors(
    panel: pl.DataFrame,
    snapshot: pl.DataFrame | None,
    names: Any,
) -> pl.DataFrame:
    """把财务因子列按公告日门控地并入日频面板。

    - snapshot 为 None (本地无财务数据): 产出全 null 列, 保持面板形状,
      由上层决定是否报"无财务数据"错误;
    - 面板必须已按 (symbol, date) 排序 (存储与挖掘路径均满足)。
    """
    requested = [str(name) for name in names if str(name) in FUNDAMENTAL_FACTOR_NAMES]
    missing_columns = [name for name in requested if name not in panel.columns]
    if not missing_columns:
        return panel

    timeline = _factor_timeline(snapshot)
    if timeline.is_empty():
        return panel.with_columns([
            pl.lit(None, dtype=pl.Float64).alias(name)
            for name in missing_columns
        ])

    columns = sorted(
        {FUNDAMENTAL_FACTORS[name]["column"] for name in missing_columns}
    )
    # 排除相同日期的可得性边界，公告当天继续沿用上一份有效记录。
    right = timeline.select(["symbol", "_available", "_announce", *columns])
    joined = panel.join_asof(
        right,
        left_on="date",
        right_on="_available",
        by="symbol",
        strategy="backward",
        allow_exact_matches=False,  # date-only evidence is usable on a later trading date
        check_sortedness=False,
    )
    expressions = []
    for name in missing_columns:
        spec = FUNDAMENTAL_FACTORS[name]
        source = pl.col(spec["column"])
        if spec["price_ratio"]:
            value = (
                pl.when(source > 0)
                .then(pl.col("close") / source)
                .otherwise(None)
            )
        else:
            value = source
        expressions.append(value.alias(name))
    return joined.with_columns(expressions).drop("_available")


def build_fundamental_matrices(
    market: Any,
    snapshot: pl.DataFrame | None,
    names: Any,
) -> dict[str, np.ndarray]:
    """为 MarketDataMatrix 构建财务因子 TxN float32 字段。

    与 attach_fundamental_factors 同一口径: 公告日次一交易日起前向填充,
    无数据为 NaN。pb 类因子在矩阵侧用 close / bps 现算。
    """
    requested = [str(name) for name in names if str(name) in FUNDAMENTAL_FACTOR_NAMES]
    if not requested:
        return {}

    shape = market.shape
    result: dict[str, np.ndarray] = {}
    timeline = _factor_timeline(snapshot)
    if timeline.is_empty():
        for name in requested:
            result[name] = np.full(shape, np.nan, dtype=np.float32)
        return result

    asset_index = {symbol: index for index, symbol in enumerate(market.symbols)}
    labels = market.timestamp_labels
    label_dates = np.array([label[:10] for label in labels], dtype="datetime64[D]")

    raw_columns = {
        FUNDAMENTAL_FACTORS[name]["column"]: np.full(shape, np.nan, dtype=np.float32)
        for name in requested
    }
    timeline = timeline.select("symbol", "_available", *raw_columns)
    for symbol, available, *values in timeline.iter_rows():
        column_index = asset_index.get(symbol)
        if column_index is None:
            continue
        # 公告日之后 (严格大于) 的首个时间行索引
        start = int(np.searchsorted(label_dates, np.datetime64(available, "D"), side="right"))
        if start >= shape[0]:
            continue
        for target, value in zip(raw_columns.values(), values, strict=True):
            target[start:, column_index] = np.nan if value is None else value

    for name in requested:
        spec = FUNDAMENTAL_FACTORS[name]
        source = raw_columns[spec["column"]]
        if spec["price_ratio"]:
            with np.errstate(divide="ignore", invalid="ignore"):
                matrix = (market.close / source).astype(np.float32)
            matrix[~(source > 0)] = np.nan
            matrix[np.isinf(matrix)] = np.nan
        else:
            matrix = source
        result[name] = matrix
    return result


def attach_matrix_fundamental_fields(market: Any, data_dir: Path | None, names: Any, *, evidence=None) -> Any:
    """把财务因子作为 matrix fields 附加到 (frozen) MarketDataMatrix 副本。"""
    import dataclasses

    requested = [str(name) for name in names if str(name) in FUNDAMENTAL_FACTOR_NAMES]
    if not requested:
        return market
    snapshot = load_fundamental_snapshot(data_dir)
    if evidence is not None and snapshot is not None:
        evidence.record_frame("financial_announcements", snapshot)
    extra = build_fundamental_matrices(market, snapshot, requested)
    if not extra:
        return market
    merged = {**dict(market.fields), **extra}
    for array in extra.values():
        array.flags.writeable = False
    return dataclasses.replace(market, fields=MappingProxyType(merged))
