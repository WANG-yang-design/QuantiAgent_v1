# -*- coding: utf-8 -*-
"""
第二阶段细化回测 V4 (2023-10 ~ 今, 动态ETF池, 分段/滚动验证)
=============================================================
按 PLAN(GRIDV3 分析)执行:
  阶段A 主效应细化: 市场过滤开/关两分支, 对 η² 前8参数做平衡采样(各110组)
  阶段B 交互补测:   区间×持仓、移动止损×启用、top_n×仓位、动量×追高、离场×入场
  阶段C 稳健性验证: A/B Top10 的 ±1 档邻域扰动(每个12组)

工程要点:
  - 动态池按周期在父进程预计算一次并冻结, 通过 initargs 传给各worker;
  - BacktestEngine(persist=False), 结果写 data/backtest_grid/v4/{results}.jsonl
    (含 equity_curve/dates, 供分段与滚动验证);
  - 提升引擎的 asof 切片性能(bisect)后支持千级标的×3年区间的批量回测。

用法:
  python -m scripts.grid_search_v4 gen-a
  python -m scripts.grid_search_v4 run --candidates candidates_A.json --results results_A.jsonl
  python -m scripts.grid_search_v4 gen-b --results results_A.jsonl
  python -m scripts.grid_search_v4 run --candidates candidates_B.json --results results_B.jsonl
  python -m scripts.grid_search_v4 gen-c --results results_A.jsonl results_B.jsonl
  python -m scripts.grid_search_v4 run --candidates candidates_C.json --results results_C.jsonl
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

V4_DIR = ROOT / "data" / "backtest_grid" / "v4"
V3_RESULTS = ROOT / "data" / "backtest_grid" / "v3" / "results.jsonl"
UNIVERSE_SALT = "GRIDV4"

# ----------------------------------------------------------------------
# 参数空间(PLAN 细化网格)
# ----------------------------------------------------------------------
REFINED: Dict[str, List[Any]] = {
    "max_vol": [0.50, 0.55, 0.60, 0.65, 0.70],
    "max_distance_from_ma20": [0.12, 0.15, 0.18, 0.20, 0.25, 0.30],
    "max_buy_momentum": [0.15, 0.20, 0.25, 0.30, 0.40],
    "mom_window": [10, 15, 20, 30],
    "rebalance_interval_days": [1, 2, 3, 4, 5],
    "trailing_stop_pct": [0.05, 0.06, 0.07, 0.08, 0.09, 0.10, 0.12],
    "hold_buffer": [1, 2, 3, 4],
    "target_scale": [0.85, 0.95, 1.00],
}
FIXED: Dict[str, Any] = {
    "top_n": 4,
    "max_total_position": 0.85,
    "hard_stop_pct": 0.08,
    "trailing_stop_activation": 0.04,
    "min_hold_days": 3,
    "initial_ratio": 0.50,
    "min_momentum": 0.01,
    "min_amount": 30_000_000,
    "max_vol": 0.55,
    "max_distance_from_ma20": 0.15,
    "max_buy_momentum": 0.25,
    "mom_window": 15,
    "rebalance_interval_days": 3,
    "trailing_stop_pct": 0.08,
    "hold_buffer": 2,
    "target_scale": 0.95,
    "require_above_ma20": True,
    "trend_ma_window": 20,
    "fresh_stop_mult": 1.5,
    "max_position_in_recent_range": 1.0,
    "low_rebound_bonus": 0.015,
    "low_rebound_from_low_pct": 0.10,
    "rebalance_threshold": 0.15,
    "min_order_amount": 500,
    "warmup_days": 20,
    "bottom_ratio": 0.0,
    "market_filter": True,
    "market_exit_threshold": -0.03,
    "market_enter_threshold": -0.01,
    "cool_down_days": 5,
}
# 邻域扰动候选(阶段C)
NEIGHBOR_AXES = [
    ("mom_window", REFINED["mom_window"]),
    ("max_vol", REFINED["max_vol"]),
    ("max_distance_from_ma20", REFINED["max_distance_from_ma20"]),
    ("rebalance_interval_days", REFINED["rebalance_interval_days"]),
    ("trailing_stop_pct", REFINED["trailing_stop_pct"]),
    ("hold_buffer", REFINED["hold_buffer"]),
]


def _target_weight(top_n: int, max_total: float, scale: float) -> float:
    return round(min(0.28, max_total / max(top_n, 1) * scale), 4)


def _materialize(sample: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(FIXED)
    p.update({k: v for k, v in sample.items() if k != "target_scale"})
    top_n = int(p.get("top_n", 4))
    max_total = float(p.get("max_total_position", 0.85))
    scale = float(sample.get("target_scale", p.get("target_scale", 0.95)))
    p["target_weight"] = _target_weight(top_n, max_total, scale)
    act = float(p.get("trailing_stop_activation", 0.04))
    trail = float(p.get("trailing_stop_pct", 0.08))
    if act >= trail:
        p["trailing_stop_activation"] = round(max(0.01, trail - 0.02), 4)
    exit_t = float(p.get("market_exit_threshold", -0.03))
    enter_t = float(p.get("market_enter_threshold", -0.01))
    if enter_t < exit_t:
        p["market_enter_threshold"] = exit_t
    if not p.get("market_filter"):
        p["market_exit_threshold"] = -0.03
        p["market_enter_threshold"] = -0.01
    p["top_n"] = top_n
    return p


def _signature(p: Dict[str, Any]) -> str:
    return json.dumps({k: v for k, v in sorted(p.items())
                       if not str(k).startswith("_")},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class _Dedup:
    def __init__(self):
        self.seen: set = set()

    def add(self, out: List[dict], name: str, params: Dict[str, Any],
            stage: str, parent: str = ""):
        sig = _signature(params)
        if sig in self.seen:
            return
        self.seen.add(sig)
        out.append({"name": name, "params": params, "stage": stage,
                    "parent": parent})


def _lhs(keys: List[str], grids: Dict[str, List[Any]], n: int,
         rng: random.Random) -> Iterable[Dict[str, Any]]:
    strata = {k: list(range(n)) for k in keys}
    for k in keys:
        rng.shuffle(strata[k])
    for i in range(n):
        sample = {}
        for k in keys:
            values = grids[k]
            pos = (strata[k][i] + rng.random()) / n
            sample[k] = values[min(int(pos * len(values)), len(values) - 1)]
        yield sample


def _load_saved_presets() -> Dict[str, Dict[str, Any]]:
    f = ROOT / "data" / "strategy_presets.json"
    data = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    return {str(k): dict(v) for k, v in (data.get("presets") or {}).items()
            if (v or {}).get("universe_mode") == "dynamic_etf"}


def _v3_top(n: int = 5) -> List[Dict[str, Any]]:
    if not V3_RESULTS.exists():
        return []
    rows = [json.loads(l) for l in V3_RESULTS.read_text(encoding="utf-8").splitlines()
            if l.strip()]
    rows = [r for r in rows if not r.get("error")]
    if not rows:
        return []
    from scripts.strategy_grid_search import _rank
    _rank(rows)
    return [r["params"] for r in rows if r.get("eligible")][:n]


# ----------------------------------------------------------------------
def gen_a(path: Path, seed: int) -> Path:
    out: List[dict] = []
    dd = _Dedup()
    keys = list(REFINED.keys())
    for branch in (True, False):
        rng = random.Random(seed + (1 if branch else 0))
        for i, sample in enumerate(_lhs(keys, REFINED, 110, rng)):
            sample = {**sample, "market_filter": branch}
            params = _materialize(sample)
            dd.add(out, f"A-{'ON' if branch else 'OFF'}-{i:03d}", params, "A")
    # 锚点: V3 Top5(开过滤) 与 已保存策略
    for i, p in enumerate(_v3_top(5)):
        dd.add(out, f"A-SEED{i+1}", _materialize(dict(p)), "A", "v3")
    for name, p in _load_saved_presets().items():
        dd.add(out, f"A-P-{name[:16]}", _materialize(dict(p)), "A", "preset")
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return path


def _load_results(paths: List[Path]) -> List[dict]:
    rows: List[dict] = []
    for p in paths:
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if not r.get("error"):
                    rows.append(r)
    return rows


def _segment_min_return(r: dict) -> float:
    """从净值曲线取自然年分段收益的最小值(衡量跨环境一致性)。"""
    eq = r.get("equity_curve") or []
    dates = r.get("dates") or []
    if len(eq) < 3 or len(dates) != len(eq):
        return -1.0
    by_year: Dict[str, List[float]] = {}
    for v, d in zip(eq, dates):
        by_year.setdefault(str(d)[:4], []).append(float(v))
    rets = []
    for year, vals in by_year.items():
        if len(vals) >= 2 and vals[0] > 0:
            rets.append(vals[-1] / vals[0] - 1)
    return min(rets) if rets else -1.0


def _rank_pool(rows: List[dict]) -> List[dict]:
    from scripts.strategy_grid_search import _rank
    _rank(rows)
    floors = {r["candidate"]: _segment_min_return(r) for r in rows}
    vals = sorted(v for v in floors.values() if v > -1)
    def pct(v: float) -> float:
        if not vals:
            return 0.0
        return sum(1 for x in vals if x <= v) / len(vals)
    for r in rows:
        r["segment_min_return"] = round(floors.get(r["candidate"], -1.0), 6)
        rs = r.get("robust_score")
        if rs is None:
            r["v4_score"] = None
        else:
            p = pct(floors.get(r["candidate"], -1.0)) * 100
            r["v4_score"] = round(0.6 * rs + 0.4 * p, 4)
    rows.sort(key=lambda r: (r.get("v4_score") is not None,
                             r.get("v4_score") or -1), reverse=True)
    return rows


def gen_b(path: Path, result_files: List[Path], seed: int) -> Path:
    rows = _rank_pool(_load_results(result_files))
    on = [r for r in rows if (r["params"] or {}).get("market_filter")]
    off = [r for r in rows if not (r["params"] or {}).get("market_filter")]
    parents: List[Tuple[str, Dict[str, Any]]] = []
    for i, r in enumerate(on[:2]):
        parents.append((f"A-ON#{i+1}", dict(r["params"])))
    if off:
        parents.append(("A-OFF#1", dict(off[0]["params"])))
    # 保持与当前模拟盘/上轮最优的连续性(只取 active_paper 一个, 控制总量)
    try:
        store = json.loads((ROOT / "data" / "strategy_presets.json")
                           .read_text(encoding="utf-8")) or {}
        act = str(store.get("active_paper") or "")
        if act and act in (store.get("presets") or {}):
            p = dict(store["presets"][act])
            if p.get("universe_mode") == "dynamic_etf":
                parents.append((f"P-{act[:12]}", _materialize(p)))
    except Exception:
        pass
    for i, p in enumerate(_v3_top(1)):
        parents.append((f"V3-BEST", _materialize(dict(p))))

    out: List[dict] = []
    dd = _Dedup()
    rng = random.Random(seed)
    pairs = [
        ("rebalance_interval_days", REFINED["rebalance_interval_days"]),
        ("min_hold_days", [1, 2, 3, 5, 10]),
        ("trailing_stop_pct", [0.05, 0.06, 0.08, 0.10, 0.12, 0.15]),
        ("trailing_stop_activation", [0.02, 0.04, 0.06, 0.08]),
        ("top_n", [3, 4, 5, 6]),
        ("target_scale", [0.85, 0.95, 1.0]),
        ("mom_window", [10, 15, 20, 30]),
        ("max_buy_momentum", [0.15, 0.20, 0.25, 0.30]),
        ("market_exit_threshold", [-0.05, -0.04, -0.03, -0.02]),
        ("market_enter_threshold", [-0.02, -0.01, 0.0]),
    ]
    pair_index = {k: v for k, v in pairs}
    grids = [
        ("int_x_hold", "rebalance_interval_days", "min_hold_days"),
        ("trail_x_act", "trailing_stop_pct", "trailing_stop_activation"),
        ("n_x_scale", "top_n", "target_scale"),
        ("mom_x_buy", "mom_window", "max_buy_momentum"),
        ("mexit_x_menter", "market_exit_threshold", "market_enter_threshold"),
    ]
    for pname, base in parents:
        for gname, a, b in grids:
            if a.startswith("market") and not base.get("market_filter"):
                continue
            for va in pair_index[a]:
                for vb in pair_index[b]:
                    params = _materialize({**base, a: va, b: vb})
                    dd.add(out, f"B-{pname}-{gname}-{va}-{vb}", params, "B", pname)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def gen_c(path: Path, result_files: List[Path], top_n: int = 10) -> Path:
    rows = _rank_pool(_load_results(result_files))
    out: List[dict] = []
    dd = _Dedup()
    chosen = [r for r in rows if r.get("v4_score") is not None][:top_n]
    for i, r in enumerate(chosen):
        base = dict(r["params"])
        for axis, values in NEIGHBOR_AXES:
            cur = base.get(axis)
            try:
                idx = values.index(cur)
            except ValueError:
                continue
            for j in (idx - 1, idx + 1):
                if 0 <= j < len(values):
                    params = _materialize({**base, axis: values[j]})
                    dd.add(out, f"C-{r['candidate']}-{axis}-{values[j]}",
                           params, "C", r["candidate"])
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


# ----------------------------------------------------------------------
# 运行器
# ----------------------------------------------------------------------
_CTX: Dict[str, Any] = {}


def _init_worker(symbols, asset_types, names, period_sets, snapshots,
                 start_iso, end_iso, cash, min_coverage):
    try:
        from backtest.data_replayer import DataReplayer
        _CTX.update({
            "symbols": symbols, "asset_types": asset_types, "names": names,
            "start": date.fromisoformat(start_iso),
            "end": date.fromisoformat(end_iso), "cash": cash,
            "period_sets": {k: set(v) for k, v in period_sets.items()},
            "snapshots": snapshots,
        })
        _CTX["replayer"] = DataReplayer(
            symbols, asset_types=asset_types, min_coverage=min_coverage,
            online_fill=False)
        _CTX["init_error"] = ""
    except Exception as exc:  # noqa: BLE001
        import traceback
        _CTX["init_error"] = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-600:]}"


def _run_one(task: Dict[str, Any]) -> Dict[str, Any]:
    from backtest.engine import BacktestEngine
    from core.ids import gen_backtest_id
    from scripts.strategy_grid_search import _compact_metrics
    from strategies.rotation_executor import (
        build_rotation_signal_fn, resolve_rotation_params,
    )
    from scripts.grid_search_v4 import StaticUniverseProvider

    name = str(task["name"])
    raw = task.get("params") or {}
    ctx = _CTX
    if ctx.get("init_error"):
        return {"candidate": name, "stage": task.get("stage"),
                "parent": task.get("parent"), "params": raw,
                "error": f"worker init failed: {ctx['init_error']}"}
    t0 = time.time()
    try:
        resolved = resolve_rotation_params(raw, use_live_preset=False)
    except Exception as exc:  # noqa: BLE001
        return {"candidate": name, "stage": task.get("stage"),
                "parent": task.get("parent"), "params": raw,
                "error": f"参数非法: {exc}"}
    resolved["universe_mode"] = "dynamic_etf"
    engine = BacktestEngine(
        ctx["start"], ctx["end"], initial_cash=ctx["cash"],
        mode="daily", use_agents=False, name=f"GRIDV4:{name}"[:64],
        run_id=gen_backtest_id(), asset_type="etf",
        asset_types=ctx["asset_types"], persist=False)
    engine.params = resolved
    engine.name_map = ctx["names"]
    universe = StaticUniverseProvider(ctx["period_sets"], ctx["snapshots"])
    signal_fn = build_rotation_signal_fn(
        initial_cash=ctx["cash"], params=resolved, use_live_preset=False,
        feature_cache=ctx.setdefault("feature_cache", {}),
        universe_provider=universe)
    try:
        metrics = engine.run_daily(ctx["replayer"], signal_fn)
        compact = _compact_metrics(metrics)
        compact.update({
            "candidate": name, "stage": task.get("stage"),
            "parent": task.get("parent"), "params": resolved,
            "universe_mode": "dynamic_etf",
            # 分段/滚动验证需要逐日净值
            "equity_curve": [round(float(x), 2)
                             for x in (metrics.get("equity_curve") or [])],
            "dates": [str(x)[:10] for x in (metrics.get("dates") or [])],
            "elapsed_seconds": round(time.time() - t0, 1),
        })
        return compact
    except Exception as exc:  # noqa: BLE001
        return {"candidate": name, "stage": task.get("stage"),
                "parent": task.get("parent"), "params": resolved,
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_seconds": round(time.time() - t0, 1)}


def build_period_universe(start: date, end: date, min_coverage: float,
                          mother_top: int = 0):
    """父进程: 构建动态池快照(所有候选共享), 返回可传给worker的轻量数据。

    mother_top>0 时把母池限制为"当前成交额Top N"(仅用于对照/排障, 存在
    用今日流动性挑历史标的的前视偏差, 默认0=全市场)。
    """
    from backtest.data_replayer import DataReplayer
    from database import repository as repo
    from strategies.dynamic_etf_universe import (
        DynamicEtfUniverseSelector, HistoricalUniverseProvider,
    )
    symbols = repo.get_etf_history_symbols(end, min_bars=20, recent_days=15)
    if mother_top > 0:
        try:
            from data_service.market_data_service import get_market_service
            spot = get_market_service().get_etf_spot()
            top = {str(s.get("symbol")) for s in sorted(
                spot, key=lambda x: x.get("amount", 0) or 0,
                reverse=True)[:mother_top]}
            symbols = [s for s in symbols if s in top]
            print(f"[mother-top] 限制母池为成交额Top{mother_top}: {len(symbols)} 只")
        except Exception as exc:
            print(f"[mother-top] 获取失败, 使用全市场: {exc}")
    metadata = repo.get_symbol_metadata(symbols)
    asset_types = {s: "etf" for s in symbols}
    names = {s: str((metadata.get(s) or {}).get("name") or "") for s in symbols}
    replayer = DataReplayer(symbols, asset_types=asset_types,
                            min_coverage=min_coverage, online_fill=False)
    load_start = start - timedelta(days=250)
    asof = {s: replayer.load_all_daily(s, load_start, end) for s in symbols}
    provider = HistoricalUniverseProvider(
        DynamicEtfUniverseSelector(), source_mode="grid",
        snapshot_salt=UNIVERSE_SALT)
    for d in replayer.trade_dates(start, end):
        provider(d, asof)
    period_sets = _period_sets_from_snapshots(provider.snapshots)
    snapshots = list(provider.snapshots)
    del replayer, asof
    import gc
    gc.collect()
    return symbols, asset_types, names, period_sets, snapshots


def _period_sets_from_snapshots(snapshots: List[dict]) -> Dict[str, list]:
    out: Dict[str, list] = {}
    for snap in snapshots:
        eff = str(snap.get("effective_date") or "")[:10]
        if not eff:
            continue
        try:
            d = date.fromisoformat(eff)
        except ValueError:
            continue
        period = f"{d.year:04d}-{d.month:02d}"
        out[period] = [str(m["symbol"]) for m in snap.get("members", [])]
    return out


class StaticUniverseProvider:
    def __init__(self, period_sets: Dict[str, set], snapshots: List[dict]):
        self.period_sets = period_sets
        self.snapshots = snapshots
        self.selector_config = {"refresh": "monthly"}

    def __call__(self, d: date, asof: Dict[str, List[dict]]) -> set:
        return set(self.period_sets.get(f"{d.year:04d}-{d.month:02d}", set()))


def run(args) -> Path:
    V4_DIR.mkdir(parents=True, exist_ok=True)
    candidates = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
    results_path = Path(args.results)
    done: Dict[str, dict] = {}
    if results_path.exists():
        for line in results_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                done[str(row.get("candidate"))] = row
            except ValueError:
                continue
    todo = [c for c in candidates if not args.resume or c["name"] not in done]
    print(f"候选 {len(candidates)}, 已完成 {len(done)}, 待跑 {len(todo)}", flush=True)
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)

    t0 = time.time()
    print("构建动态池快照(父进程, 一次)...", flush=True)
    symbols, asset_types, names, period_sets, snapshots = build_period_universe(
        start, end, args.min_coverage, getattr(args, "mother_top", 0))
    print(f"母池 {len(symbols)} 只, 月度快照 {len(snapshots)} 期, "
          f"耗时 {time.time()-t0:.0f}s", flush=True)

    with open(results_path, "a", encoding="utf-8") as fh:
        with ProcessPoolExecutor(
                max_workers=args.workers, initializer=_init_worker,
                initargs=(symbols, asset_types, names, period_sets, snapshots,
                          args.start, args.end, args.initial_cash,
                          args.min_coverage)) as pool:
            futures = {pool.submit(_run_one, c): c["name"] for c in todo}
            for idx, fut in enumerate(as_completed(futures), 1):
                row = fut.result()
                done[str(row.get("candidate"))] = row
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
                if row.get("error"):
                    print(f"[{idx}/{len(todo)}] {row['candidate']} FAILED: "
                          f"{row['error'][:120]}", flush=True)
                else:
                    print(f"[{idx}/{len(todo)}] {row['candidate']:<34} "
                          f"ret={row['total_return']:+.2%} "
                          f"dd={row['max_drawdown']:.2%} "
                          f"sharpe={row['sharpe']:.2f} "
                          f"trades={row['trade_count']} "
                          f"({row['elapsed_seconds']}s)", flush=True)
    print(f"完成 {len(done)} 组, 耗时 {time.time()-t0:.0f}s", flush=True)
    return results_path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen-a"); g.add_argument("--out", default=str(V4_DIR / "candidates_A.json"))
    g.add_argument("--seed", type=int, default=20260925)
    g = sub.add_parser("gen-b"); g.add_argument("--out", default=str(V4_DIR / "candidates_B.json"))
    g.add_argument("--results", nargs="+", default=[str(V4_DIR / "results_A.jsonl")])
    g.add_argument("--seed", type=int, default=20260926)
    g = sub.add_parser("gen-c"); g.add_argument("--out", default=str(V4_DIR / "candidates_C.json"))
    g.add_argument("--results", nargs="+",
                   default=[str(V4_DIR / "results_A.jsonl"), str(V4_DIR / "results_B.jsonl")])
    r = sub.add_parser("run")
    r.add_argument("--candidates", required=True)
    r.add_argument("--results", required=True)
    r.add_argument("--start", default="2023-10-01")
    r.add_argument("--end", default=date.today().isoformat())
    r.add_argument("--initial-cash", type=float, default=100_000.0)
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--min-coverage", type=float, default=0.95)
    r.add_argument("--mother-top", type=int, default=0,
                   help="对照用: 母池限制为成交额Top N(0=全市场, 有前视偏差)")
    r.add_argument("--resume", action="store_true")
    args = p.parse_args()
    return args


def main():
    args = parse_args()
    V4_DIR.mkdir(parents=True, exist_ok=True)
    if args.cmd == "gen-a":
        print("生成阶段A:", gen_a(Path(args.out), args.seed))
    elif args.cmd == "gen-b":
        print("生成阶段B:", gen_b(Path(args.out),
                                 [Path(x) for x in args.results], args.seed))
    elif args.cmd == "gen-c":
        print("生成阶段C:", gen_c(Path(args.out),
                                 [Path(x) for x in args.results]))
    elif args.cmd == "run":
        run(args)


if __name__ == "__main__":
    main()
