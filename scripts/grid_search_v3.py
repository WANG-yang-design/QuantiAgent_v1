# -*- coding: utf-8 -*-
"""
大规模参数网格回测 V3 (500 组, 动态ETF池, 2026-01-01 至今)
============================================================
设计要点:
1. 候选生成: 5个已保存策略(锚点) + 结构化角点 + 拉丁超立方(LHS)采样,
   覆盖 20+ 参数的扩大区间, 共 N 组(默认500); 固定随机种子, 可复现。
2. 公平性: 动态ETF池按"每个调仓周期"预计算一次并冻结, 所有候选使用完全
   相同的标的池与行情快照; 特征缓存跨候选共享。
3. 不污染 Web 回测历史: BacktestEngine(persist=False), 结果写入
   data/backtest_grid/v3/results.jsonl (可断点续跑) + 汇总 JSON/CSV。
4. 多进程并行(--workers), Windows 下用 spawn, 每进程独立加载行情。

用法:
  python -m scripts.grid_search_v3 --workers 6                 # 500组
  python -m scripts.grid_search_v3 --workers 6 --limit 20      # 冒烟
  python -m scripts.grid_search_v3 --workers 6 --resume        # 续跑
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "data" / "backtest_grid" / "v3"
UNIVERSE_SALT = "GRIDV3"          # 所有候选共享同一套池快照(公平比较)

# ----------------------------------------------------------------------
# 参数空间(扩大区间, 基于此前实测的关键维度)
# ----------------------------------------------------------------------
SPACE: Dict[str, List[Any]] = {
    "top_n": [3, 4, 5, 6, 8],
    "mom_window": [5, 10, 15, 20, 30, 60],
    "max_vol": [0.40, 0.55, 0.70],
    "min_amount": [10_000_000, 30_000_000, 50_000_000, 100_000_000],
    "max_total_position": [0.70, 0.85, 0.95],
    "target_scale": [0.80, 0.90, 1.00],          # 目标仓位 = min(0.28, 总仓位上限/top_n*scale)
    "hard_stop_pct": [0.05, 0.08, 0.10, 0.12, 0.15],
    "trailing_stop_pct": [0.04, 0.06, 0.08, 0.10, 0.15],
    "trailing_stop_activation": [0.02, 0.04, 0.06, 0.08],
    "market_filter": [False, True],
    "market_exit_threshold": [-0.05, -0.03, -0.01],
    "market_enter_threshold": [-0.02, -0.01, 0.0],
    "rebalance_interval_days": [1, 2, 3, 5, 10],
    "min_hold_days": [1, 3, 5, 10],
    "hold_buffer": [0, 1, 2, 4],
    "max_buy_momentum": [0.10, 0.15, 0.25, 0.40, 1.0],
    "min_momentum": [-0.02, 0.0, 0.01, 0.03, 0.05],
    "require_above_ma20": [True, False],
    "max_distance_from_ma20": [0.05, 0.10, 0.15, 0.30],
    "fresh_stop_mult": [1.0, 1.5, 2.0],
    "initial_ratio": [0.30, 0.50, 0.80, 1.00],
    "trend_ma_window": [10, 20, 60],
}

BASE = {
    "min_amount": 30_000_000, "max_vol": 0.55,
    "rebalance_threshold": 0.15, "max_total_position": 0.85,
    "market_exit_threshold": -0.03, "market_enter_threshold": -0.01,
    "cool_down_days": 5, "min_order_amount": 500, "warmup_days": 20,
    "bottom_ratio": 0.0, "max_position_in_recent_range": 1.0,
    "low_rebound_bonus": 0.015, "low_rebound_from_low_pct": 0.10,
    "require_above_ma20": True, "trend_ma_window": 20,
}


def _target_weight(top_n: int, max_total: float, scale: float) -> float:
    return round(min(0.28, max_total / max(top_n, 1) * scale), 4)


def _materialize(sample: Dict[str, Any]) -> Dict[str, Any]:
    """把采样值展开成完整策略参数(含派生量与约束修正)。"""
    p = dict(BASE)
    p.update({k: v for k, v in sample.items() if k != "target_scale"})
    top_n = int(p.get("top_n", 4))
    max_total = float(p.get("max_total_position", 0.85))
    scale = float(sample.get("target_scale", 0.9))
    p["target_weight"] = _target_weight(top_n, max_total, scale)
    # 约束: 启用盈利 < 移动止损(否则移动止盈永不触发)
    act = float(p.get("trailing_stop_activation", 0.04))
    trail = float(p.get("trailing_stop_pct", 0.06))
    if act >= trail:
        p["trailing_stop_activation"] = round(max(0.01, trail - 0.02), 4)
    # 约束: 重新入场阈值 >= 离场阈值
    exit_t = float(p.get("market_exit_threshold", -0.03))
    enter_t = float(p.get("market_enter_threshold", -0.01))
    if enter_t < exit_t:
        p["market_enter_threshold"] = exit_t
    # 市场过滤关闭时阈值无意义, 保持默认以免干扰分析
    if not p.get("market_filter"):
        p["market_exit_threshold"] = -0.03
        p["market_enter_threshold"] = -0.01
    p["initial_ratio"] = float(p.get("initial_ratio", 0.5))
    p["hold_buffer"] = int(p.get("hold_buffer", 2))
    p["min_hold_days"] = int(p.get("min_hold_days", 3))
    p["rebalance_interval_days"] = int(p.get("rebalance_interval_days", 1))
    p["top_n"] = top_n
    return p


def _signature(p: Dict[str, Any]) -> str:
    keys = sorted(k for k in p if not str(k).startswith("_"))
    return json.dumps({k: p[k] for k in keys}, sort_keys=True,
                      ensure_ascii=False, separators=(",", ":"))


def generate_candidates(n: int, seed: int = 20260924) -> List[Tuple[str, Dict[str, Any]]]:
    """5个已保存策略 + 结构化角点 + LHS 采样, 去重后共 n 组。"""
    from scripts.strategy_grid_search import _load_saved_presets

    out: List[Tuple[str, Dict[str, Any]]] = []
    seen: set = set()

    def add(name: str, params: Dict[str, Any]):
        sig = _signature(params)
        if sig in seen:
            return
        seen.add(sig)
        out.append((name, params))

    # 1) 已保存的 5 个策略(动态池) 作为锚点
    for pname, raw in _load_saved_presets("dynamic_etf").items():
        add(f"P-{pname}", {**BASE, **{k: v for k, v in raw.items()
                                      if k in SPACE or k == "target_weight"}})

    # 2) 结构化角点: 市场过滤 × 持有数 × 动量窗口 × 止损档
    stop_profiles = [(0.08, 0.06, 0.04), (0.10, 0.08, 0.04),
                     (0.12, 0.10, 0.06), (0.15, 0.12, 0.08)]
    for market in (False, True):
        for top_n in (3, 4, 5):
            for mom in (10, 20, 30):
                for hard, trail, act in stop_profiles:
                    add(f"C-M{int(market)}-N{top_n}-W{mom}-S{int(hard*100)}",
                        _materialize({**BASE, "market_filter": market,
                                      "top_n": top_n, "mom_window": mom,
                                      "hard_stop_pct": hard,
                                      "trailing_stop_pct": trail,
                                      "trailing_stop_activation": act}))

    # 3) LHS 采样补齐
    rng = random.Random(seed)
    keys = list(SPACE.keys())
    need = max(0, n - len(out))
    if need:
        # 每个维度按 [0,1) 均匀分层, 再随机排列(标准 LHS)
        strata: Dict[str, List[int]] = {
            k: list(range(need)) for k in keys
        }
        for k in keys:
            rng.shuffle(strata[k])
        for i in range(need):
            sample: Dict[str, Any] = {}
            for k in keys:
                values = SPACE[k]
                idx = strata[k][i]
                pos = (idx + rng.random()) / need
                sample[k] = values[min(int(pos * len(values)), len(values) - 1)]
            params = _materialize({**BASE, **sample})
            add(f"L{i:04d}", params)

    return out[:n]


# ----------------------------------------------------------------------
# 动态池预计算(所有候选共享)
# ----------------------------------------------------------------------
class StaticUniverseProvider:
    """冻结的动态池: 按周期返回预计算的成员集合, 保证候选间完全一致。"""

    def __init__(self, period_sets: Dict[str, set], snapshots: List[dict]):
        self.period_sets = period_sets
        self.snapshots = snapshots
        self.selector_config = {"refresh": "monthly"}

    def __call__(self, d: date, asof: Dict[str, List[dict]]) -> set:
        period = f"{d.year:04d}-{d.month:02d}"
        return set(self.period_sets.get(period, set()))


def build_universe(replayer, start: date, end: date) -> StaticUniverseProvider:
    from datetime import timedelta

    from strategies.dynamic_etf_universe import (
        DynamicEtfUniverseSelector, HistoricalUniverseProvider,
    )
    symbols = replayer.universe()
    # 与回测引擎一致: 起点前多加载250天(动量/波动率/覆盖率需要历史)
    load_start = start - timedelta(days=250)
    asof = {s: replayer.load_all_daily(s, load_start, end) for s in symbols}
    provider = HistoricalUniverseProvider(
        DynamicEtfUniverseSelector(), source_mode="grid",
        snapshot_salt=UNIVERSE_SALT)
    dates = replayer.trade_dates(start, end)
    for d in dates:
        provider(d, asof)
    return StaticUniverseProvider(
        _period_sets_from_snapshots(provider.snapshots),
        list(provider.snapshots))


def _period_sets_from_snapshots(snapshots: List[dict]) -> Dict[str, set]:
    out: Dict[str, set] = {}
    for snap in snapshots:
        eff = str(snap.get("effective_date") or "")[:10]
        if not eff:
            continue
        try:
            d = date.fromisoformat(eff)
        except ValueError:
            continue
        period = f"{d.year:04d}-{d.month:02d}"
        out[period] = {str(m["symbol"]) for m in snap.get("members", [])}
    return out


# ----------------------------------------------------------------------
# 工作进程
# ----------------------------------------------------------------------
_CTX: Dict[str, Any] = {}


def _init_worker(start_iso: str, end_iso: str, cash: float, min_coverage: float):
    try:
        _init_worker_inner(start_iso, end_iso, cash, min_coverage)
        _CTX["init_error"] = ""
    except Exception as exc:  # noqa: BLE001
        import traceback
        _CTX["init_error"] = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-800:]}"


def _init_worker_inner(start_iso: str, end_iso: str, cash: float, min_coverage: float):
    from backtest.data_replayer import DataReplayer
    from database import repository as repo
    start, end = date.fromisoformat(start_iso), date.fromisoformat(end_iso)
    symbols = repo.get_etf_history_symbols(end, min_bars=20, recent_days=15)
    metadata = repo.get_symbol_metadata(symbols)
    asset_types = {s: "etf" for s in symbols}
    names = {s: str((metadata.get(s) or {}).get("name") or "") for s in symbols}
    replayer = DataReplayer(symbols, asset_types=asset_types,
                            min_coverage=min_coverage, online_fill=False)
    universe = build_universe(replayer, start, end)
    _CTX.update({"replayer": replayer, "universe": universe, "names": names,
                 "asset_types": asset_types, "symbols": symbols,
                 "start": start, "end": end, "cash": cash})


def _run_one(task: Tuple[str, Dict[str, Any]]) -> Dict[str, Any]:
    from backtest.engine import BacktestEngine
    from core.ids import gen_backtest_id
    from scripts.strategy_grid_search import _compact_metrics
    from strategies.rotation_executor import (
        build_rotation_signal_fn, resolve_rotation_params,
    )

    name, raw_params = task
    ctx = _CTX
    if ctx.get("init_error"):
        return {"candidate": name, "family": "grid_v3", "params": raw_params,
                "error": f"worker init failed: {ctx['init_error']}"}
    t0 = time.time()
    try:
        resolved = resolve_rotation_params(raw_params, use_live_preset=False)
    except Exception as exc:  # noqa: BLE001
        return {"candidate": name, "family": "grid_v3", "params": raw_params,
                "error": f"参数非法: {type(exc).__name__}: {exc}",
                "elapsed_seconds": round(time.time() - t0, 1)}
    resolved["universe_mode"] = "dynamic_etf"
    engine = BacktestEngine(
        ctx["start"], ctx["end"], initial_cash=ctx["cash"],
        mode="daily", use_agents=False, name=f"GRIDV3:{name}"[:64],
        run_id=gen_backtest_id(), asset_type="etf",
        asset_types=ctx["asset_types"], persist=False)
    engine.params = resolved
    engine.name_map = ctx["names"]
    signal_fn = build_rotation_signal_fn(
        initial_cash=ctx["cash"], params=resolved, use_live_preset=False,
        feature_cache=ctx.setdefault("feature_cache", {}),
        universe_provider=ctx["universe"])
    try:
        metrics = engine.run_daily(ctx["replayer"], signal_fn)
        compact = _compact_metrics(metrics)
        compact.update({"candidate": name, "family": "grid_v3",
                        "params": resolved, "universe_mode": "dynamic_etf",
                        "elapsed_seconds": round(time.time() - t0, 1)})
        return compact
    except Exception as exc:  # noqa: BLE001
        return {"candidate": name, "family": "grid_v3", "params": resolved,
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_seconds": round(time.time() - t0, 1)}


# ----------------------------------------------------------------------
def run(args) -> Path:
    from scripts.strategy_grid_search import _rank

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results_path = OUT_DIR / "results.jsonl"
    done: Dict[str, Dict[str, Any]] = {}
    if results_path.exists():
        for line in results_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                done[str(row.get("candidate"))] = row
            except ValueError:
                continue
    candidates = generate_candidates(args.count, seed=args.seed)
    if args.limit:
        candidates = candidates[:args.limit]
    todo = [(n, p) for n, p in candidates
            if not args.resume or n not in done]
    print(f"候选总数 {len(candidates)}, 已完成 {len(done)}, 待跑 {len(todo)}",
          flush=True)

    t0 = time.time()
    with open(results_path, "a", encoding="utf-8") as fh:
        with ProcessPoolExecutor(
                max_workers=args.workers,
                initializer=_init_worker,
                initargs=(args.start, args.end, args.initial_cash,
                          args.min_coverage)) as pool:
            futures = {pool.submit(_run_one, t): t[0] for t in todo}
            for idx, fut in enumerate(as_completed(futures), 1):
                row = fut.result()
                done[str(row.get("candidate"))] = row
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
                if row.get("error"):
                    print(f"[{idx}/{len(todo)}] {row['candidate']} FAILED: "
                          f"{row['error'][:100]}", flush=True)
                else:
                    print(f"[{idx}/{len(todo)}] {row['candidate']:<10} "
                          f"ret={row['total_return']:+.2%} "
                          f"dd={row['max_drawdown']:.2%} "
                          f"sharpe={row['sharpe']:.2f} "
                          f"trades={row['trade_count']} "
                          f"({row['elapsed_seconds']}s)", flush=True)

    results = list(done.values())
    _rank(results)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary = OUT_DIR / f"summary_{stamp}.json"
    payload = {
        "experiment": "GRIDV3-500",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "period": {"start": args.start, "end": args.end},
        "initial_cash": args.initial_cash,
        "universe_mode": "dynamic_etf",
        "universe_salt": UNIVERSE_SALT,
        "count": len(results),
        "elapsed_seconds": round(time.time() - t0, 1),
        "top20": [r for r in results if r.get("eligible")][:20],
        "results": results,
    }
    summary.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    print(f"完成 {len(results)} 组, 汇总: {summary}", flush=True)
    return summary


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2026-01-01")
    p.add_argument("--end", default=date.today().isoformat())
    p.add_argument("--initial-cash", type=float, default=100_000.0)
    p.add_argument("--count", type=int, default=500)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--min-coverage", type=float, default=0.98)
    p.add_argument("--seed", type=int, default=20260924)
    p.add_argument("--resume", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    run(parse_args())
