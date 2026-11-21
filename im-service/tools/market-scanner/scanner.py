"""TickFlow 盘中市场扫描器 — 每 5 分钟扫描全市场, 判断买卖点并推送微信.

工作方式:
  1. 交易日盘中 (默认每 300 秒) 拉取大盘概览 (指数/涨跌家数/涨停跌停/情绪/成交额)
  2. 并行运行多个内置选股策略 (趋势突破/均线金叉/放量新高/量价齐升/MACD/均线多头/超跌反弹/回踩)
  3. 聚合成"多策略共振买点" + "单策略买点" + "关注池卖点/风险预警"
  4. 通过本机通知中转 (relay.py) 推送到微信 (PushPlus)

用法:
  python scanner.py --once          立即扫描一次并推送 (盘后也可测, 用最近交易日数据)
  python scanner.py --once --no-push  只生成消息写日志, 不推送
  python scanner.py                 常驻循环 (仅交易日交易时段扫描)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # noqa: BLE001 — 系统 Python 可能缺 tzdata, 上海无夏令时, 固定 +08:00 等价
    TZ = timezone(timedelta(hours=8), "CST")

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "scanner_config.json"
STATE_PATH = HERE / "state.json"
LOG_PATH = HERE / "scanner.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("scanner")

DEFAULT_CONFIG = {
    "portal": "http://127.0.0.1:3018",
    "relay": "http://127.0.0.1:8899/tsp",
    "interval_seconds": 300,
    "sessions": [["09:25", "11:35"], ["12:55", "15:05"]],
    "buy_strategies": [
        "trend_breakout",
        "ma_golden_cross",
        "breakout_new_high_60d",
        "volume_price_surge",
        "macd_golden",
        "bullish_alignment",
        "oversold_bounce",
        "pullback_ma20_bounce",
    ],
    "rows_per_strategy": 30,
    "consensus_min": 2,
    "top_consensus": 5,
    "top_single": 5,
    "track_ttl_scans": 40,
    "max_sell_alerts": 5,
    "max_exit_scan": 3000,
}


# ---------------------------------------------------------------- HTTP helpers
def http_json(url: str, method: str = "GET", body: dict | None = None, timeout: float = 60.0):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取配置失败, 使用默认: %s", exc)
    return cfg


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"scan_index": 0, "candidates": {}, "last_scan_at": None}


def save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("保存状态失败: %s", exc)


# ---------------------------------------------------------------- data fetch
def fetch_overview(portal: str) -> dict:
    return http_json(portal + "/api/overview/market", timeout=60)


def fetch_snapshot(portal: str) -> tuple[dict, dict]:
    d = http_json(portal + "/api/screener/market-snapshot", timeout=90)
    rows = d.get("rows") or []
    return d, {r.get("symbol"): r for r in rows}


def fetch_strategy_meta(portal: str) -> dict:
    try:
        d = http_json(portal + "/api/strategies", timeout=60)
        return {s.get("id"): s for s in (d.get("strategies") or [])}
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取策略清单失败: %s", exc)
        return {}


def fetch_signal_labels(portal: str) -> dict:
    labels = {}
    try:
        d = http_json(portal + "/api/monitor-rules/options", timeout=60)
        for key in ("builtin_signals", "custom_signals"):
            for item in d.get(key) or []:
                if item.get("key"):
                    labels[item["key"]] = item.get("label") or item["key"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取信号字典失败: %s", exc)
    return labels


def fetch_watchlist(portal: str) -> list[str]:
    try:
        d = http_json(portal + "/api/watchlist", timeout=30)
        return [r.get("symbol") for r in (d.get("symbols") or []) if r.get("symbol")]
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取自选失败: %s", exc)
        return []


def run_one_strategy(portal: str, strategy_id: str, rows_limit: int) -> dict | None:
    try:
        return http_json(
            portal + "/api/strategies/run",
            method="POST",
            body={"strategy_id": strategy_id, "asset_type": "stock"},
            timeout=120,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("策略 %s 运行失败: %s", strategy_id, exc)
        return None


def run_strategies(portal: str, strategy_ids: list[str], rows_limit: int) -> dict:
    results: dict[str, dict] = {}
    if not strategy_ids:
        return results
    workers = min(4, len(strategy_ids))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run_one_strategy, portal, sid, rows_limit): sid for sid in strategy_ids}
        for fut in as_completed(futures):
            sid = futures[fut]
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001
                logger.warning("策略 %s 异常: %s", sid, exc)
                continue
            if res:
                results[sid] = res
    return results


# ---------------------------------------------------------------- analysis
def pct_text(v) -> str:
    """个股涨跌幅: 接口返回小数 (0.0237 = +2.37%)。"""
    if v is None:
        return "-"
    return f"{v * 100:+.2f}%"


def idx_pct_text(v) -> str:
    """指数涨跌幅: 概览接口直接返回百分数 (-0.587 = -0.587%)。"""
    if v is None:
        return "-"
    return f"{v:+.2f}%"


def price_text(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, (int, float)):
        return f"{v:.2f}"
    return str(v)


SIGNAL_LABEL_FALLBACK = {
    "signal_ma20_breakdown": "跌破MA20",
    "signal_ma10_breakdown": "跌破MA10",
    "signal_ma5_breakdown": "跌破MA5",
    "signal_break_ma20_lose": "跌破MA20",
    "signal_ma_dead_5_20": "MA5下穿MA20",
    "signal_ma_dead_20_60": "MA20下穿MA60",
    "signal_macd_dead": "MACD死叉",
    "signal_boll_breakdown_lower": "跌破布林下轨",
    "signal_ma10_lose": "丢失MA10",
    "signal_ma5_lose_after_shadow": "长下影后丢MA5",
}


def signal_label(signal_id: str, labels: dict) -> str:
    return labels.get(signal_id) or SIGNAL_LABEL_FALLBACK.get(signal_id) or signal_id.removeprefix("signal_")


def build_scan(cfg: dict, state: dict, push: bool, force: bool = False) -> dict:
    portal = cfg["portal"]
    started = time.time()
    overview = fetch_overview(portal)
    snap, snap_map = fetch_snapshot(portal)
    as_of = snap.get("as_of") or overview.get("as_of")
    phase = (overview.get("quote_status") or {}).get("market_phase")
    today = datetime.now(TZ).date().isoformat()
    if not force and push and str(as_of) != today and phase == "closed":
        logger.info("非交易日/无盘中数据 (as_of=%s, phase=%s), 本轮不推送", as_of, phase)
        return {"scan_index": state.get("scan_index", 0), "title": "", "body": "", "pushed": False}
    strategy_meta = fetch_strategy_meta(portal)
    signal_labels = fetch_signal_labels(portal)
    watchlist = fetch_watchlist(portal)

    results = run_strategies(portal, cfg["buy_strategies"], cfg["rows_per_strategy"])

    # ---- 聚合每个策略的命中行
    hits: dict[str, dict] = {}          # symbol -> {strategies: [], scores: [], strategy_scores:{}, row}
    exit_hits: dict[str, set] = {}      # symbol -> signals
    exit_market_count = 0
    for sid, res in results.items():
        sname = (strategy_meta.get(sid) or {}).get("name") or sid
        rows = (res.get("rows") or [])[: cfg["rows_per_strategy"]]
        scores = res.get("scores") or {}
        for row in rows:
            sym = row.get("symbol")
            if not sym:
                continue
            item = hits.setdefault(sym, {"strategies": [], "scores": [], "row": row})
            item["strategies"].append(sname)
            item["scores"].append(float(scores.get(sym) or row.get("score") or 0))
        # 卖点信号: 全市场聚合
        for ev in (res.get("exit_signal_hits") or [])[: cfg["max_exit_scan"]]:
            sym = ev.get("symbol")
            if not sym:
                continue
            signals = [signal_label(s, signal_labels) for s in (ev.get("signals") or [])]
            exit_hits.setdefault(sym, set()).update(signals)
    exit_market_count = len(exit_hits)

    # ---- 共振买点 / 单策略买点 (过滤明显下跌中的"买点", 避免接飞刀)
    consensus, single = [], []
    for sym, item in hits.items():
        snap_row = snap_map.get(sym) or {}
        chg = snap_row.get("change_pct")
        if chg is not None and chg <= -0.05:
            continue
        n = len(item["strategies"])
        avg_score = sum(item["scores"]) / len(item["scores"])
        detail = {
            "symbol": sym,
            "strategies": item["strategies"],
            "count": n,
            "score": avg_score,
            "snap": snap_map.get(sym) or {},
            "row": item["row"],
        }
        if n >= cfg["consensus_min"]:
            consensus.append(detail)
        else:
            single.append(detail)
    consensus.sort(key=lambda x: (-x["count"], -x["score"]))
    single.sort(key=lambda x: -x["score"])
    consensus = consensus[: cfg["top_consensus"]]
    single = single[: cfg["top_single"]]

    # ---- 关注池 & 卖点
    scan_index = int(state.get("scan_index", 0)) + 1
    candidates = state.get("candidates") or {}
    for sym, item in hits.items():
        entry = candidates.setdefault(sym, {"first_scan": scan_index, "strategies": []})
        entry["last_scan"] = scan_index
        entry["strategies"] = sorted(set(entry.get("strategies", [])) | set(item["strategies"]))
    ttl = int(cfg.get("track_ttl_scans", 40))
    for sym in list(candidates):
        if scan_index - int(candidates[sym].get("last_scan", 0)) > ttl:
            candidates.pop(sym, None)
    watch_symbols = set(watchlist)
    tracked_symbols = set(candidates) | watch_symbols
    sell_alerts = []
    for sym in tracked_symbols:
        sigs = exit_hits.get(sym)
        if not sigs:
            continue
        row = snap_map.get(sym) or {}
        sell_alerts.append({
            "symbol": sym,
            "name": row.get("name") or "",
            "close": row.get("close"),
            "change_pct": row.get("change_pct"),
            "signals": sorted(sigs),
            "watch": sym in watch_symbols,
        })
    sell_alerts.sort(key=lambda x: (not x["watch"], x["symbol"]))
    sell_alerts = sell_alerts[: cfg["max_sell_alerts"]]

    message = compose_message(cfg, overview, consensus, single, sell_alerts, results, strategy_meta,
                              as_of, scan_index, exit_market_count)
    logger.info("扫描 #%s 完成: %d 策略, 共振 %d, 单策略 %d, 卖点 %d, 耗时 %.1fs",
                scan_index, len(results), len(consensus), len(single), len(sell_alerts),
                time.time() - started)

    payload = {
        "event": "market_scan",
        "timestamp": int(time.time()),
        "title": message["title"],
        "body": message["body"],
        "data": {
            "scan_index": scan_index,
            "as_of": str(as_of),
            "consensus": [c["symbol"] for c in consensus],
            "single": [s["symbol"] for s in single],
            "sell": [s["symbol"] for s in sell_alerts],
        },
    }
    pushed = False
    if push:
        try:
            resp = http_json(cfg["relay"], method="POST", body=payload, timeout=20)
            pushed = True
            logger.info("推送完成: %s", json.dumps(resp, ensure_ascii=False)[:300])
        except Exception as exc:  # noqa: BLE001
            logger.error("推送失败: %s", exc)

    state["scan_index"] = scan_index
    state["candidates"] = candidates
    state["last_scan_at"] = datetime.now(TZ).isoformat(timespec="seconds")
    save_state(state)
    return {"scan_index": scan_index, "title": message["title"], "body": message["body"], "pushed": pushed}


# ---------------------------------------------------------------- message
def compose_message(cfg, overview, consensus, single, sell_alerts, results, strategy_meta,
                    as_of, scan_index, exit_market_count) -> dict:
    now = datetime.now(TZ)
    indices = overview.get("indices") or []
    idx_main = indices[0] if indices else {}
    idx2 = indices[1] if len(indices) > 1 else {}
    idx3 = indices[2] if len(indices) > 2 else {}
    breadth = overview.get("breadth") or {}
    limit = overview.get("limit") or {}
    trend = overview.get("trend") or {}
    emotion = overview.get("emotion") or {}
    amount = overview.get("amount") or {}
    amount_yi = (amount.get("total") or 0) / 1e8

    title = (f"【盘中扫描 {now:%H:%M}】{idx_main.get('name', '上证')} "
             f"{idx_pct_text(idx_main.get('change_pct'))} · 情绪{emotion.get('label', '')}{emotion.get('score', '')}")

    lines = []
    lines.append(f"**数据基准**: {as_of}　扫描 #{scan_index}　{now:%Y-%m-%d %H:%M}")
    lines.append("")

    lines.append("**一、大盘**")
    idx_txt = "　".join(
        f"{i.get('name', '')} {price_text(i.get('last_price'))} ({idx_pct_text(i.get('change_pct'))})"
        for i in indices[:3]
    )
    lines.append(idx_txt or "指数数据缺失")
    lines.append(
        f"两市成交 {amount_yi:.0f} 亿｜上涨 {breadth.get('up', '-')} / 下跌 {breadth.get('down', '-')}"
        f"｜涨停 {limit.get('limit_up', '-')} / 炸板 {limit.get('broken', '-')} / 跌停 {limit.get('limit_down', '-')}"
    )
    lines.append(
        f"情绪分 {emotion.get('score', '-')}（{emotion.get('label', '-')}）"
        f"｜MA20上方 {trend.get('above_ma20_pct', 0):.0f}%"
        f"｜新高 {trend.get('new_high', '-')} / 新低 {trend.get('new_low', '-')}"
    )
    if limit.get("max_boards"):
        lines.append(f"连板高度 {limit['max_boards']} 板｜封板率 {limit.get('seal_rate', 0):.0f}%")
    lines.append("")

    def stock_line(detail, prefix=""):
        snap = detail["snap"] or {}
        name = snap.get("name") or ""
        close = snap.get("close")
        chg = snap.get("change_pct")
        return (f"{prefix}{detail['symbol']} {name} {price_text(close)}"
                f" {pct_text(chg)} | 评分 {detail['score']:.0f}")

    if consensus:
        lines.append("**二、共振买点（多策略同时命中）**")
        for i, c in enumerate(consensus, 1):
            lines.append(stock_line(c, f"{i}. "))
            lines.append(f"　　→ {' + '.join(c['strategies'])}")
        lines.append("")
    else:
        lines.append("**二、共振买点**：本轮无多策略共振信号")
        lines.append("")

    if single:
        lines.append("**三、单策略买点**")
        for i, s in enumerate(single, 1):
            lines.append(stock_line(s, f"{i}. "))
            lines.append(f"　　→ {s['strategies'][0]}")
        lines.append("")
    else:
        lines.append("**三、单策略买点**：无")
        lines.append("")

    lines.append("**四、卖点 / 风险预警**")
    if sell_alerts:
        for s in sell_alerts:
            tag = "【自选】" if s["watch"] else "【关注池】"
            lines.append(
                f"- {tag} {s['symbol']} {s['name']} {price_text(s['close'])} {pct_text(s['change_pct'])}"
                f" → {'、'.join(s['signals'])}"
            )
    else:
        lines.append("- 关注池未出现卖点信号")
    lines.append(f"- 全市场共 {exit_market_count} 只触发策略卖出信号（破位类），可作情绪参考")
    lines.append("")
    lines.append("**参考信息，非投资建议；买卖决策请自行判断。**")

    return {"title": title, "body": "\n".join(lines)}


# ---------------------------------------------------------------- loop
def in_session(sessions, now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    hm = now.strftime("%H:%M")
    for start, end in sessions:
        if start <= hm <= end:
            return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="TickFlow 盘中市场扫描器")
    parser.add_argument("--once", action="store_true", help="立即扫描一次后退出")
    parser.add_argument("--no-push", action="store_true", help="只生成消息, 不推送")
    args = parser.parse_args()

    cfg = load_config()
    state = load_state()
    push = not args.no_push

    if args.once:
        out = build_scan(cfg, state, push)
        print("\n==== TITLE ====\n" + out["title"])
        print("\n==== BODY ====\n" + out["body"])
        print(f"\n==== PUSHED: {out['pushed']} ====")
        return

    logger.info("扫描器启动: 间隔 %ss, 时段 %s", cfg["interval_seconds"], cfg["sessions"])
    last_scan = 0.0
    while True:
        try:
            now = datetime.now(TZ)
            if in_session(cfg["sessions"], now):
                if time.time() - last_scan >= float(cfg["interval_seconds"]):
                    build_scan(cfg, state, push)
                    last_scan = time.time()
            time.sleep(20)
        except KeyboardInterrupt:
            logger.info("收到中断, 退出")
            break
        except Exception as exc:  # noqa: BLE001
            logger.error("扫描异常: %s", exc, exc_info=True)
            time.sleep(30)


if __name__ == "__main__":
    main()
