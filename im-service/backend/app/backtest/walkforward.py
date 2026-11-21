"""Walk-forward 优化 — 滚动窗口的样本内优化 + 样本外验证。

每折在训练区间用参数网格优化选出最优参数, 再在紧邻的测试区间用该参数做样本外(OOS)
回测。各折的日期、收益和退化保持可见；仅完整、互不重叠的折可报告收益乘积。

依赖 PR2a 的 StrategyOptimizer 做每折训练区间的网格优化。
"""
from __future__ import annotations

import logging
import math
import time
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from statistics import mean

from app.backtest.guards import trades_ok

logger = logging.getLogger(__name__)
CALENDAR_DAYS = "calendar_days"
TRADING_BARS_V1 = "trading_bars_v1"


@dataclass
class Fold:
    index: int
    train_start: date
    train_end: date
    test_start: date
    test_end: date


def generate_folds(
    start: date,
    end: date,
    train_days: int,
    test_days: int,
    step_days: int,
) -> list[Fold]:
    """滚动窗口 fold 切分: 训练窗口固定长度, 测试窗口紧接其后, 按 step 前移。

    测试区间超出 end 即停止。数据区间放不下一折则抛错。
    """
    if train_days <= 0 or test_days <= 0 or step_days <= 0:
        raise ValueError("train_days / test_days / step_days 必须为正")

    folds: list[Fold] = []
    i = 0
    train_start = start
    while True:
        train_end = train_start + timedelta(days=train_days)
        # 测试区间从训练末日的次日开始: 回测区间是闭区间, 若 test_start==train_end 则
        # 该日 K 线同时进训练优化与 OOS 首日, 构成前视泄漏。后移一天隔断。
        test_start = train_end + timedelta(days=1)
        test_end = test_start + timedelta(days=test_days)
        if test_end > end:
            break
        folds.append(Fold(i, train_start, train_end, test_start, test_end))
        i += 1
        train_start = train_start + timedelta(days=step_days)

    if not folds:
        raise ValueError(
            f"数据区间不足以切出至少一折 (需 train+test={train_days + test_days}天, "
            f"实有 {(end - start).days}天)"
        )
    return folds


def plan_walkforward_folds(
    start: date,
    end: date,
    train_days: int,
    test_days: int,
    step_days: int,
    window_basis: str,
    *,
    data_dir: Path | None = None,
    asset_type: str = "stock",
) -> tuple[list[Fold], dict | None]:
    if window_basis == CALENDAR_DAYS:
        return generate_folds(start, end, train_days, test_days, step_days), None
    if window_basis != TRADING_BARS_V1:
        raise ValueError(f"无效的窗口单位: {window_basis}")
    if min(train_days, test_days, step_days) <= 0:
        raise ValueError("train_days / test_days / step_days 必须为正")
    if data_dir is None:
        raise ValueError("交易日窗口缺少本地日线数据目录")

    from app.services.mining_preflight import enriched_partition_dates

    labels = enriched_partition_dates(data_dir, asset_type, start, end)
    span = train_days + test_days
    if len(labels) < span:
        raise ValueError(f"本地日线标签不足以切出至少一折 (需 {span} 个, 实有 {len(labels)} 个)")
    folds = [
        Fold(index, labels[offset], labels[offset + train_days - 1],
             labels[offset + train_days], labels[offset + span - 1])
        for index, offset in enumerate(range(0, len(labels) - span + 1, step_days))
    ]
    return folds, {
        "source": "local_enriched_daily_partitions",
        "coverage": "unverified_exchange_calendar",
        "asset_type": asset_type,
        "count": len(labels),
        "start": labels[0].isoformat(),
        "end": labels[-1].isoformat(),
    }


def _norm(v: float, direction: str) -> float:
    """把目标值归一到"越大越好"空间, 以便跨目标一致地算退化 (min 类目标取负)。"""
    return -v if direction == "min" else v


def _finite_number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _label_date(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _evaluation_end(result, *, mode: str, test_end: date) -> date | None:
    dates = [_label_date(point.get("date")) for point in result.equity_curve]
    exits = [_label_date(trade.get("exit_date")) for trade in result.trades]
    if mode == "full" and (not exits or len(exits) != result.stats.get("n_trades") or None in exits):
        return None
    observed = [day for day in (*dates, *exits) if day is not None]
    # Position mode is bounded by test_end even when a legacy result omits its curve.
    return max(observed, default=test_end)


def _fold_return(fold: dict) -> dict:
    stats = fold.get("oos_stats") or {}
    value = _finite_number(stats.get("total_return"))
    count = _finite_number(stats.get("n_trades"))
    positions = stats.get("open_positions")
    if positions:
        status = "open_positions"
    elif value is None:
        status = "missing_return"
    elif value < -1:
        status = "invalid_return"
    elif not isinstance(positions, list):
        status = "unknown_terminal_positions"
    elif count is None or count < 0 or not count.is_integer():
        status = "missing_trade_count"
    elif count == 0:
        equity = _finite_number(stats.get("final_equity"))
        status = "observed_no_trades" if value == 0 and equity is not None and equity > 0 else "no_closed_sample"
    else:
        status = "observed"
    return {"index": fold["index"], "return": value, "status": status}


def aggregate_oos(
    fold_records: list[dict],
    objective: str,
    direction: str = "max",
    *,
    planned_folds: int | None = None,
    requested_end: date | None = None,
) -> dict:
    """Summarize independent OOS folds. The product is a fold-level statistic."""
    records = sorted(fold_records, key=lambda f: (_label_date(f.get("test_start")) or date.max, f["index"]))
    fold_returns = [_fold_return(fold) for fold in records]
    observed = [f["return"] for f in fold_returns if f["status"] in {"observed", "observed_no_trades"}]
    reasons: list[str] = []
    if not records:
        reasons.append("no_evaluated_folds")
    if planned_folds is not None and len(records) != planned_folds:
        reasons.append("incomplete_folds")
    reasons.extend(sorted({f["status"] for f in fold_returns if f["status"] not in {"observed", "observed_no_trades"}}))

    latest_end: date | None = None
    overlap = False
    unknown_span = False
    past_requested_end = False
    ends: list[date] = []
    for fold in records:
        start = _label_date(fold.get("test_start"))
        planned_end = _label_date(fold.get("test_end"))
        actual_end = _label_date(fold.get("evaluation_end", fold.get("test_end")))
        if start is None or planned_end is None or planned_end < start or actual_end is None or actual_end < start:
            unknown_span = True
        if start is None or planned_end is None or planned_end < start:
            continue
        end = max(planned_end, actual_end or planned_end)
        ends.append(end)
        if latest_end is not None:
            overlap |= start <= latest_end
        latest_end = max(latest_end, end) if latest_end is not None else end
        past_requested_end |= requested_end is not None and end > requested_end
    if overlap:
        reasons.append("overlapping_windows")
    if unknown_span:
        reasons.append("unknown_evaluation_span")
    if past_requested_end:
        reasons.append("evaluation_past_requested_end")
    if any(f.get("incomplete_observed_window") for f in records):
        reasons.append("incomplete_observed_window")

    product = 1.0
    curve = []
    if not reasons:
        for fold, end, value in zip(records, ends, observed, strict=True):
            product *= 1 + value
            curve.append({"fold": fold["index"], "date": str(end), "value": round(product, 4)})
        if not math.isfinite(product):
            reasons.append("nonfinite_product")
            curve = []

    pairs = [(_finite_number(f.get("is_score")), _finite_number(f.get("oos_objective"))) for f in records]
    is_values = [left for left, _ in pairs if left is not None]
    oos_values = [right for _, right in pairs if right is not None]
    comparable = [_norm(left, direction) - _norm(right, direction)
                  for left, right in pairs if left is not None and right is not None]
    return {
        "n_folds": len(records),
        "aggregation_scope": "independent_folds",
        "aggregation_eligible": not reasons,
        "aggregation_reasons": reasons,
        "overlapping_oos": True if overlap else None if unknown_span or not records else False,
        "fold_returns": fold_returns,
        "compounded_oos_return": round(product - 1.0, 4) if not reasons else None,
        "avg_is_objective": round(mean(is_values), 4) if is_values else None,
        "avg_oos_objective": round(mean(oos_values), 4) if oos_values else None,
        "degradation": round(mean(comparable), 4) if comparable else None,
        "consistency": round(sum(value > 0 for value in observed) / len(observed), 4) if observed else None,
        "n_observed_returns": len(observed),
        "oos_equity_curve": curve,
    }


@dataclass
class WalkForwardConfig:
    strategy_id: str
    symbols: list[str] | None
    start: date
    end: date
    param_grid: dict
    objective: str = "sortino"
    train_days: int = 252
    test_days: int = 63
    step_days: int = 63
    direction: str | None = None
    min_trades_oos: int = 10            # 每折 OOS 段最小交易数 (<=0 关闭)
    max_workers: int = 4
    base_params: dict = field(default_factory=dict)
    overrides: dict | None = None
    backtest_kwargs: dict = field(default_factory=dict)
    matrix_cache_max_mb: int = 512
    window_basis: str = CALENDAR_DAYS


class WalkForwardService:
    """滚动窗口 walk-forward: 每折训练区间优化 -> 测试区间 OOS 验证 -> 聚合。"""

    def __init__(self, optimizer, service, strategy_engine) -> None:
        self.optimizer = optimizer
        self.service = service
        self.strategy_engine = strategy_engine

    def _prepare_shared_matrix(self, cfg: WalkForwardConfig, folds: list[Fold]):
        """Build one immutable superset matrix for every matrix-native fold.

        返回 None 时 run() 走通用路径: 每折独立优化 + OOS 回测, 正确但无共享矩阵加速。
        python_history_legacy (filter_history) 与 polars_expr (内置) 策略无法装入
        共享矩阵, 走通用路径; composite / minute_filter 仍不支持, 保持 fail-closed。
        """
        if self.strategy_engine is None or not folds:
            return None
        strategy = self.strategy_engine.get(cfg.strategy_id)
        if strategy.execution_backend != "matrix_native":
            if strategy.execution_backend in ("python_history_legacy", "polars_expr"):
                return None
            raise ValueError(
                f"步进优化暂仅支持矩阵(matrix_native)/日线历史(python_history_legacy/"
                f"polars_expr)策略; {cfg.strategy_id} 是 {strategy.execution_backend}"
            )

        from app.backtest.optimizer import expand_param_grid
        from app.backtest.strategy import StrategyBacktestConfig

        combos = expand_param_grid(strategy.meta.get("params", []), cfg.param_grid)
        shared_start = min(fold.train_start for fold in folds)
        shared_end = max(fold.test_end for fold in folds)
        configs = [
            StrategyBacktestConfig(
                strategy_id=cfg.strategy_id,
                symbols=cfg.symbols,
                start=shared_start,
                end=shared_end,
                params={**cfg.base_params, **combo},
                overrides=cfg.overrides,
                **cfg.backtest_kwargs,
            )
            for combo in combos
        ]
        return self.service.prepare_matrix_optimization(
            configs,
            matrix_cache_max_bytes=int(cfg.matrix_cache_max_mb) * 1024 * 1024,
        )

    def run(
        self,
        cfg: WalkForwardConfig,
        progress_cb=None,
        cancel_event=None,
    ) -> dict:
        from app.backtest.optimizer import OptimizeConfig, default_direction
        from app.backtest.strategy import StrategyBacktestConfig

        t0 = time.perf_counter()
        direction = cfg.direction or default_direction(cfg.objective)
        data_dir = self.service.engine.repo.store.data_dir if cfg.window_basis == TRADING_BARS_V1 else None
        folds, window_labels = plan_walkforward_folds(
            cfg.start, cfg.end, cfg.train_days, cfg.test_days, cfg.step_days, cfg.window_basis,
            data_dir=data_dir, asset_type=cfg.backtest_kwargs.get("asset_type", "stock"),
        )
        n_total = len(folds)

        shared_prepared = self._prepare_shared_matrix(cfg, folds)
        shared_market_data = (
            shared_prepared.market_data if shared_prepared is not None else None
        )
        shared_date_labels = (
            tuple(label[:10] for label in shared_market_data.timestamp_labels)
            if shared_market_data is not None
            else ()
        )

        def _shared_window_bar_count(window_start: date, window_end: date) -> int:
            return (bisect_right(shared_date_labels, window_end.isoformat())
                    - bisect_left(shared_date_labels, window_start.isoformat()))

        # 遥测: 首尾快照 PanelCache, 量化跨折重叠区间重复扫盘的 IO 占比 (是否值得进一步优化)。
        cache_before = self.service.engine.cache_stats()

        valid_records: list[dict] = []   # IS 与 OOS 都成功, 计入聚合
        skipped: list[dict] = []          # 无优化结果 或 OOS 失败, 不计入聚合 (避免伪装成有效折)
        done = 0

        # IS 训练区间强制 position 模式: full 模式会让训练折未平仓持仓用 train_end 之后
        # (即 OOS 区间) 的真实 K 线平仓, IS 分数被未来数据污染 -> 优化选参乐观偏移, 使过拟合
        # 被掩盖。OOS 回测保留用户所选 mode。参数扫描优化只看正式区间内的表现即可。
        is_backtest_kwargs = {**cfg.backtest_kwargs, "mode": "position"}

        for f in folds:
            if cancel_event is not None and cancel_event.is_set():
                break

            base = {
                "index": f.index,
                "train_start": str(f.train_start),
                "train_end": str(f.train_end),
                "test_start": str(f.test_start),
                "test_end": str(f.test_end),
            }
            missing_window = None
            if shared_market_data is not None:
                train_bars = _shared_window_bar_count(f.train_start, f.train_end)
                test_bars = _shared_window_bar_count(f.test_start, f.test_end)
                if not train_bars:
                    missing_window = "训练区间无可用行情数据"
                elif not test_bars:
                    missing_window = "测试区间无可用行情数据"
                elif window_labels is not None and (train_bars != cfg.train_days or test_bars != cfg.test_days):
                    missing_window = f"标的行情未覆盖折窗口 (训练 {train_bars}/{cfg.train_days}，测试 {test_bars}/{cfg.test_days})"
            if missing_window is not None:
                skipped.append({**base, "reason": missing_window})
                done += 1
                if progress_cb is not None:
                    progress_cb({"type": "walkforward_progress", "done": done, "total": n_total, "fold": f.index})
                continue

            # 训练区间: 网格优化选最优参数
            opt_cfg = OptimizeConfig(
                strategy_id=cfg.strategy_id,
                symbols=cfg.symbols,
                start=f.train_start,
                end=f.train_end,
                param_grid=cfg.param_grid,
                objective=cfg.objective,
                direction=cfg.direction,
                max_workers=cfg.max_workers,
                base_params=cfg.base_params,
                overrides=cfg.overrides,
                backtest_kwargs=is_backtest_kwargs,  # IS 强制 position, 堵前视泄漏
            )
            if shared_market_data is None:
                opt_res = self.optimizer.optimize(opt_cfg, cancel_event=cancel_event)
            else:
                opt_res = self.optimizer.optimize(
                    opt_cfg,
                    cancel_event=cancel_event,
                    prepared_market_data=shared_market_data,
                )
            best_params = opt_res.get("best_params")
            is_score = _finite_number(opt_res.get("best_score"))
            done += 1

            # 训练区间没优化出参数 (全组失败/取消) -> 跳过, 不用默认参数硬跑 OOS 伪装成有效折
            if best_params is None:
                skipped.append({**base, "reason": "训练区间未优化出参数"})
                if progress_cb is not None:
                    progress_cb({"type": "walkforward_progress", "done": done, "total": n_total, "fold": f.index})
                continue

            # 测试区间: 用最优参数做样本外回测
            merged = {**cfg.base_params, **best_params}
            oos_cfg = StrategyBacktestConfig(
                strategy_id=cfg.strategy_id,
                symbols=cfg.symbols,
                start=f.test_start,
                end=f.test_end,
                params=merged,
                overrides=cfg.overrides,
                **cfg.backtest_kwargs,
            )
            oos_prepared = None
            try:
                if shared_market_data is not None:
                    oos_prepared = self.service.prepare_matrix_optimization(
                        [oos_cfg],
                        matrix_cache_max_bytes=int(cfg.matrix_cache_max_mb) * 1024 * 1024,
                        market_data_override=shared_market_data,
                    )
                if oos_prepared is None:
                    oos_res = self.service.run(
                        oos_cfg,
                        cancel_event=cancel_event,
                    )
                else:
                    oos_res = self.service.run(
                        oos_cfg,
                        cancel_event=cancel_event,
                        prepared=oos_prepared,
                    )
            finally:
                if oos_prepared is not None:
                    oos_prepared.compute_cache.close()

            # OOS 失败 (含 cancelled) -> 跳过, 不把空/0 收益混入复利曲线
            if oos_res.error:
                skipped.append({**base, "best_params": best_params, "reason": f"OOS 回测失败: {oos_res.error}"})
                if progress_cb is not None:
                    progress_cb({"type": "walkforward_progress", "done": done, "total": n_total, "fold": f.index})
                continue

            oos_objective = _finite_number(oos_res.stats.get(cfg.objective))
            evaluation_end = _evaluation_end(oos_res, mode=oos_cfg.mode, test_end=f.test_end)
            incomplete_window = window_labels is not None and oos_cfg.mode == "position" and (
                len(oos_res.equity_curve) != cfg.test_days or evaluation_end != f.test_end
            )
            # 该折 OOS 是否较 IS 退化 (方向感知: min 类目标数值更大才是退化)
            oos_degraded = (
                _norm(oos_objective, direction) < _norm(is_score, direction)
                if (oos_objective is not None and is_score is not None) else None
            )
            record = {
                **base,
                "best_params": best_params,
                "is_score": is_score,
                "oos_objective": oos_objective,
                "oos_degraded": oos_degraded,
                "oos_stats": oos_res.stats,
                "evaluation_end": evaluation_end.isoformat() if evaluation_end is not None else None,
                "incomplete_observed_window": incomplete_window,
            }
            if trades_ok(oos_res.stats, cfg.min_trades_oos):
                valid_records.append(record)
            else:
                count = oos_res.stats.get("n_trades")
                reason = "OOS 交易数未知" if count is None else f"OOS 交易数不足 ({count} < {cfg.min_trades_oos})"
                skipped.append({**record, "reason": reason})

            if progress_cb is not None:
                progress_cb({"type": "walkforward_progress", "done": done, "total": n_total, "fold": f.index})

        summary = aggregate_oos(
            valid_records, cfg.objective, direction, planned_folds=n_total, requested_end=cfg.end,
        )

        shared_matrix_bytes = (
            shared_prepared.market_data.nbytes if shared_prepared is not None else 0
        )
        shared_matrix_status = (
            shared_prepared.market_data.cache_status if shared_prepared is not None else "none"
        )
        if shared_prepared is not None:
            shared_prepared.compute_cache.close()

        # 遥测收尾: 本次 WF 累计扫盘耗时 / 命中 / 复用, 与总耗时对比得出 load_panel 占比。
        cache_after = self.service.engine.cache_stats()
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        io_seconds = round(cache_after["compute_seconds"] - cache_before["compute_seconds"], 4)
        io_pct = round(io_seconds * 1000 / elapsed_ms * 100, 1) if elapsed_ms > 0 else 0.0
        cache_telemetry = {
            "load_panel_seconds": io_seconds,
            "load_panel_pct": io_pct,  # 扫盘耗时 / WF 总耗时
            "scans": cache_after["compute_count"] - cache_before["compute_count"],
            "hits": cache_after["hit_count"] - cache_before["hit_count"],
            "single_flight_reuses": cache_after["reuse_count"] - cache_before["reuse_count"],
        }
        logger.info(
            "walk-forward IO 占比: load_panel %.3fs (%.1f%% of %.1fms) | 扫盘 %d 次 命中 %d 复用 %d",
            io_seconds, io_pct, elapsed_ms,
            cache_telemetry["scans"], cache_telemetry["hits"], cache_telemetry["single_flight_reuses"],
        )

        return {
            "objective": cfg.objective,
            "direction": direction,
            "window_basis": cfg.window_basis,
            "window_labels": window_labels,
            "n_folds": len(valid_records),
            "n_skipped": len(skipped),
            "n_planned_folds": n_total,
            "folds": valid_records,
            "skipped": skipped,
            "summary": summary,
            "cache_telemetry": cache_telemetry,
            "shared_market_data": shared_prepared is not None,
            "shared_market_data_bytes": shared_matrix_bytes,
            "shared_market_data_status": shared_matrix_status,
            "elapsed_ms": elapsed_ms,
        }
