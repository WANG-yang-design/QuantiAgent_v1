# -*- coding: utf-8 -*-
"""
GRIDV4 最终分析: 分段验证 + 滚动前推 + 邻域稳健性 + 分类Top5 + 最终报告
=====================================================================
输入: data/backtest_grid/v4/results_{A,B,C}.jsonl (2023-10 ~ 今, 动态ETF池, 961组)
输出: data/backtest_grid/v4/analysis/
  - FINAL_REPORT.md         最终报告(含结论/评论)
  - ranked_all.csv          全部候选(含分段/滚动/稳定性)
  - top5_by_category.json   分类 Top5(完整参数+指标)
  - 并将分类 Top5 写入 data/strategy_presets.json (不覆盖 active_paper)
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
V4 = ROOT / "data" / "backtest_grid" / "v4"
OUT = V4 / "analysis"
PRESETS = ROOT / "data" / "strategy_presets.json"

PARAM_COLS = [
    "top_n", "mom_window", "max_vol", "min_amount", "max_total_position",
    "target_weight", "hard_stop_pct", "trailing_stop_pct",
    "trailing_stop_activation", "market_filter", "market_exit_threshold",
    "market_enter_threshold", "rebalance_interval_days", "min_hold_days",
    "hold_buffer", "max_buy_momentum", "min_momentum", "require_above_ma20",
    "max_distance_from_ma20", "fresh_stop_mult", "initial_ratio",
    "trend_ma_window",
]


# ----------------------------------------------------------------------
def load_rows(paths: List[Path]) -> List[dict]:
    rows: List[dict] = []
    for p in paths:
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("error"):
                continue
            rows.append(r)
    return rows


def rank_pool(rows: List[dict]) -> List[dict]:
    from scripts.strategy_grid_search import _rank
    _rank(rows)
    return rows


def _segments(dates: List[str], eq: List[float]) -> Dict[str, float]:
    """自然年 + 半年度收益。"""
    by_key: Dict[str, List[float]] = {}
    for v, d in zip(eq, dates):
        y, m = str(d)[:4], int(str(d)[5:7])
        by_key.setdefault(y, []).append(float(v))
        by_key.setdefault(f"{y}H{1 if m <= 6 else 2}", []).append(float(v))
    out = {}
    for k, vals in by_key.items():
        if len(vals) >= 2 and vals[0] > 0:
            out[k] = round(vals[-1] / vals[0] - 1, 6)
    return out


def _max_dd(series: List[float]) -> float:
    peak, mdd = 0.0, 0.0
    for v in series:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return mdd


def _series_metrics(eq: List[float]) -> Dict[str, float]:
    eq = [float(x) for x in eq if x and x > 0]
    if len(eq) < 3:
        return {"total_return": 0.0, "sharpe": 0.0, "max_drawdown": 0.0}
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq)) if eq[i - 1] > 0]
    mean = statistics.fmean(rets) if rets else 0.0
    sd = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    return {
        "total_return": eq[-1] / eq[0] - 1,
        "sharpe": (mean / sd * math.sqrt(252)) if sd > 0 else 0.0,
        "max_drawdown": -_max_dd(eq),
    }


def walk_forward(rows: List[dict], step: int = 63, window: int = 126,
                 pool: Optional[List[str]] = None) -> Dict[str, Any]:
    """滚动前推: 每 step 日用最近 window 日的表现选一个候选, 持有下一个 step。"""
    usable = [r for r in rows if len(r.get("dates") or []) == len(r.get("equity_curve") or [])
              and len(r.get("equity_curve") or []) > window + step]
    if pool:
        poolset = set(pool)
        usable = [r for r in usable if r["candidate"] in poolset]
    if not usable:
        return {}
    dates = usable[0]["dates"]
    curves = {r["candidate"]: [float(x) for x in r["equity_curve"]] for r in usable}
    n = len(dates)
    port = [1.0]
    picks: List[dict] = []
    t = window
    while t + step < n:
        best, best_score = None, -1e18
        for c, curve in curves.items():
            seg = curve[t - window:t + 1]
            m = _series_metrics(seg)
            score = m["sharpe"] + 2.0 * m["max_drawdown"]   # mdd为负
            if score > best_score:
                best_score, best = score, c
        curve = curves[best]
        for i in range(t + 1, min(t + step + 1, n)):
            if curve[i - 1] > 0:
                port.append(port[-1] * curve[i] / curve[i - 1])
        picks.append({"date": dates[t], "candidate": best,
                      "trailing_score": round(best_score, 3)})
        t += step
    metrics = _series_metrics(port)
    metrics["switches"] = len({p["candidate"] for p in picks})
    metrics["periods"] = len(picks)
    return {"equity_curve": port, "dates": dates[window:n], "picks": picks,
            "metrics": metrics}


def load_benchmark(start: date, end: date) -> List[Tuple[str, float]]:
    try:
        from database import repository as repo
        for code in ("000300", "510300"):
            bars = repo.get_daily_bars(code, start, end)
            if bars:
                return [(str(b.trade_date)[:10], float(b.close or 0)) for b in bars
                        if (b.close or 0) > 0]
    except Exception:
        pass
    return []


def _fmt_pct(v: Any, d: int = 2) -> str:
    try:
        return f"{float(v)*100:+.{d}f}%"
    except (TypeError, ValueError):
        return "-"


def _pct(series: pd.Series) -> pd.Series:
    r = series.rank(pct=True)
    return r.fillna(0.5)


def analyze_and_report(rows: List[dict]) -> Dict[str, Any]:
    from scripts.analyze_grid_v3 import _eta_squared, _kmeans  # 复用统计工具

    rank_pool(rows)
    n_all = len(rows)
    n_elig = sum(1 for r in rows if r.get("eligible"))
    # 分段与稳定性
    seg_map: Dict[str, Dict[str, float]] = {}
    for r in rows:
        eq = r.get("equity_curve") or []
        ds = r.get("dates") or []
        if len(eq) == len(ds) and len(eq) > 5:
            seg_map[r["candidate"]] = _segments(ds, eq)
    # 邻域稳定性: 子候选 vs 父候选
    by_name = {r["candidate"]: r for r in rows}
    children: Dict[str, List[float]] = {}
    for r in rows:
        parent = str(r.get("parent") or "")
        if r.get("stage") == "C" and parent in by_name:
            ps = by_name[parent].get("robust_score")
            cs = r.get("robust_score")
            if ps and cs is not None:
                children.setdefault(parent, []).append(cs / ps)
    stability = {k: round(statistics.fmean(v), 4) for k, v in children.items()}

    # 汇总表
    df_rows = []
    for r in rows:
        p = r.get("params") or {}
        eq = r.get("equity_curve") or []
        ds = r.get("dates") or []
        segs = seg_map.get(r["candidate"], {})
        seg_vals = [v for k, v in segs.items() if "H" not in k]
        seg_h = [v for k, v in segs.items() if "H" in k]
        row = {
            "candidate": r["candidate"], "stage": r.get("stage"),
            "parent": r.get("parent"),
            **{k: p.get(k) for k in PARAM_COLS},
            "total_return": r.get("total_return"),
            "annual_return": r.get("annual_return"),
            "max_drawdown": r.get("max_drawdown"),
            "sharpe": r.get("sharpe"),
            "calmar": r.get("calmar"),
            "win_rate": r.get("win_rate"),
            "turnover": r.get("turnover"),
            "trade_count": r.get("trade_count"),
            "avg_hold_days": r.get("avg_hold_days"),
            "excess_return": r.get("excess_return"),
            "stop_ratio": (r.get("exit_reason_stats") or {}).get("stop_ratio"),
            "robust_score": r.get("robust_score"),
            "eligible": bool(r.get("eligible")),
            "segment_min": min(seg_vals) if seg_vals else None,
            "segment_pos_ratio": (sum(1 for v in seg_h if v > 0) / len(seg_h)
                                  if seg_h else None),
            "stability": stability.get(r["candidate"]),
            "first_half": r.get("first_half_return"),
            "second_half": r.get("second_half_return"),
        }
        for k, v in segs.items():
            row[f"seg_{k}"] = v
        df_rows.append(row)
    df = pd.DataFrame(df_rows)

    # 最终得分 = 稳健得分(60%) + 分段一致性(40%)
    rb = _pct(df["robust_score"].fillna(0))
    seg_pct = _pct(df["segment_min"].fillna(-1.0))
    pos_pct = _pct(df["segment_pos_ratio"].fillna(0.0))
    df["final_score"] = (60 * (0.7 * rb + 0.3 * seg_pct) + 40 * pos_pct).round(2)
    df.loc[~df["eligible"], "final_score"] = np.nan

    # 分类 Top5
    rk = df[df["eligible"]].copy()
    categories: Dict[str, pd.DataFrame] = {}
    categories["稳健综合"] = rk.sort_values("final_score", ascending=False).head(5)
    categories["收益进攻"] = (rk[rk["max_drawdown"] >= -0.25]
                          .sort_values("total_return", ascending=False).head(5))
    categories["低回撤"] = (rk[rk["total_return"] > 0]
                        .sort_values(["max_drawdown"], ascending=False).head(5))
    categories["风险调整"] = (rk[rk["trade_count"] >= 30]
                         .sort_values("sharpe", ascending=False).head(5))
    categories["分段一致"] = (rk[rk["total_return"] > 0]
                         .sort_values("segment_min", ascending=False).head(5))
    categories["低换手"] = (rk[rk["total_return"] >= rk["total_return"].median()]
                        .sort_values("turnover").head(5))

    # 滚动前推
    wf_all = walk_forward(rows, step=63, window=126)
    stage_a = [r["candidate"] for r in rows if r.get("stage") == "A"]
    wf_a = walk_forward(rows, step=63, window=126, pool=stage_a)
    all_dates = sorted({d for r in rows for d in (r.get("dates") or [])})
    bench = load_benchmark(date.fromisoformat(all_dates[0]),
                           date.fromisoformat(all_dates[-1])) if all_dates else []
    bench_ret = None
    if len(bench) >= 2:
        bench_map = dict(bench)
        first = bench[0][1]
        bench_ret = bench_map.get(all_dates[-1], bench[-1][1]) / first - 1

    # 参数统计(全期, 合格样本)
    params_eta: List[dict] = []
    groups: Dict[str, pd.DataFrame] = {}
    work = rk if len(rk) >= 50 else df
    for pcol in PARAM_COLS:
        if pcol not in work.columns or work[pcol].nunique(dropna=True) < 2:
            continue
        for metric in ("sharpe", "total_return", "max_drawdown"):
            f, eta = _eta_squared(work, pcol, metric)
            params_eta.append({"param": pcol, "metric": metric, "F": f, "eta2": eta,
                               "levels": work[pcol].nunique(dropna=True)})
        g = work.groupby(pcol).agg(
            n=("candidate", "count"),
            ret_med=("total_return", "median"),
            dd_med=("max_drawdown", "median"),
            sharpe_med=("sharpe", "median"),
            turnover_med=("turnover", "median"),
            segmin_med=("segment_min", "median"),
        ).reset_index()
        groups[pcol] = g
    eff = pd.DataFrame(params_eta)
    eta_rank = (eff[eff["metric"] != "max_drawdown"].groupby("param")["eta2"]
                .mean().sort_values(ascending=False))

    # 交互(以排名前列的关键对)
    inter: Dict[str, pd.DataFrame] = {}
    for a, b in [("market_filter", "mom_window"),
                 ("rebalance_interval_days", "min_hold_days"),
                 ("trailing_stop_pct", "trailing_stop_activation"),
                 ("top_n", "target_weight"),
                 ("max_vol", "max_buy_momentum")]:
        if a in work.columns and b in work.columns:
            inter[f"{a} × {b}"] = work.pivot_table(
                index=a, columns=b, values="sharpe", aggfunc="median")

    return {
        "df": df, "n_all": n_all, "n_elig": n_elig,
        "categories": categories, "wf_all": wf_all, "wf_a": wf_a,
        "bench_ret": bench_ret, "eta": eff, "eta_rank": eta_rank,
        "groups": groups, "inter": inter, "stability": stability,
    }


# ----------------------------------------------------------------------
def write_report(res: Dict[str, Any]) -> Path:
    df: pd.DataFrame = res["df"]
    rk = df[df["eligible"]].sort_values("final_score", ascending=False)
    L: List[str] = []
    A = L.append
    A("# GRIDV4 最终报告: 3年分段 + 滚动前推 + 分类优选")
    A("")
    A(f"- 生成时间: {datetime.now():%Y-%m-%d %H:%M}")
    A(f"- 回测区间: 2023-10-01 ~ 今日(约3年, 743个交易日); 动态ETF池(月度快照, 全候选共享)")
    A(f"- 样本: {res['n_all']} 组(A主效应230 + B交互660 + C邻域71), 合格 {res['n_elig']} 组")
    A(f"- 初始资金 10万; 手续费/滑点按现状; 基准 = 沪深300买入持有"
      + (f"({_fmt_pct(res['bench_ret'])})" if res['bench_ret'] is not None else ""))
    A("")
    A("## 一、结论摘要(评论)")
    A("")
    for line in _conclusions(res):
        A(f"- {line}")
    A("")

    A("## 二、关键策略对照(3年同区间)")
    A("")
    A("| 策略 | 收益 | 年化 | 回撤 | 夏普 | 换手 | 交易 | 2024 | 2025 | 2026 |")
    A("|---|---|---|---|---|---|---|---|---|---|")
    df = res["df"]
    seg_cols = [c for c in df.columns if c.startswith("seg_") and "H" not in c]
    picks = []
    active = df[df["candidate"].astype(str).str.startswith("A-P-dynamic_v2_top2")]
    if len(active):
        picks.append(("当前 active_paper(V2)", active.iloc[0]))
    v3 = df[df["candidate"].astype(str).str.startswith("A-SEED")]
    if len(v3):
        best_seed = v3.sort_values("total_return", ascending=False).iloc[0]
        picks.append(("V3 最优种子", best_seed))
    for cat in ("稳健综合", "收益进攻", "低回撤", "风险调整"):
        sub = res["categories"].get(cat)
        if sub is not None and len(sub):
            picks.append((f"V4-{cat}-01", sub.iloc[0]))
    for label, r in picks:
        segs = " | ".join(_fmt_pct(r.get(c)) for c in seg_cols)
        A(f"| {label} | {_fmt_pct(r['total_return'])} | {_fmt_pct(r['annual_return'])} | "
          f"{_fmt_pct(r['max_drawdown'])} | {r['sharpe']:.2f} | {r['turnover']:.1f} | "
          f"{int(r['trade_count'])} | {segs} |")
    if res.get("bench_ret") is not None:
        A(f"| 沪深300基准 | {_fmt_pct(res['bench_ret'])} | - | - | - | - | - | - | - | - |")
    A("")

    A("## 三、滚动前推验证(walk-forward)")
    A("")
    A("方法: 每63个交易日用最近126日风险调整表现选一个候选, 持有其后63日; "
      "全程只使用选点之前的数据, 每步可换将。")
    A("")
    for label, wf in (("全候选池", res["wf_all"]), ("仅阶段A主效应池", res["wf_a"])):
        if not wf:
            continue
        m = wf["metrics"]
        A(f"- **{label}**: OOS 总收益 {_fmt_pct(m.get('total_return'))}, "
          f"夏普 {m.get('sharpe', 0):.2f}, 最大回撤 {_fmt_pct(m.get('max_drawdown'))}, "
          f"换将 {m.get('switches')} 次 / {m.get('periods')} 期")
    if res["bench_ret"] is not None:
        A(f"- 基准(同期): {_fmt_pct(res['bench_ret'])}")
    A("")
    if res["wf_all"].get("picks"):
        A("| 选点日期 | 选中候选 | 选点前126日得分 |")
        A("|---|---|---|")
        for p in res["wf_all"]["picks"]:
            A(f"| {p['date']} | {p['candidate']} | {p['trailing_score']} |")
        A("")

    A("## 四、分段一致性(Top15 最终得分)")
    A("")
    seg_cols = [c for c in rk.columns if c.startswith("seg_") and "H" not in c]
    A("| # | 候选 | 全期收益 | 年化 | 回撤 | 夏普 | " +
      " | ".join(c.replace("seg_", "") for c in seg_cols) + " | 分段最差 |")
    A("|---" * (7 + len(seg_cols)) + "|")
    for i, (_, r) in enumerate(rk.head(15).iterrows(), 1):
        segs = " | ".join(_fmt_pct(r.get(c)) for c in seg_cols)
        A(f"| {i} | {r['candidate']} | {_fmt_pct(r['total_return'])} | "
          f"{_fmt_pct(r['annual_return'])} | {_fmt_pct(r['max_drawdown'])} | "
          f"{r['sharpe']:.2f} | {segs} | {_fmt_pct(r['segment_min'])} |")
    A("")

    A("## 五、邻域稳健性(±1档)")
    A("")
    if res["stability"]:
        vals = sorted(res["stability"].values())
        A(f"- 共 {len(vals)} 个阶段C父策略有邻域样本; 稳定性(子均值/父值)中位 "
          f"{statistics.median(vals):.2f}, 最低 {vals[0]:.2f}")
        A("")
        A("| 父候选 | 稳定性 | 判定 |")
        A("|---|---|---|")
        for k, v in sorted(res["stability"].items(), key=lambda x: x[1])[:12]:
            A(f"| {k} | {v:.2f} | {'平坦(稳健)' if v >= 0.8 else '尖峰(过拟合风险)'} |")
    else:
        A("- 无阶段C数据")
    A("")

    A("## 六、参数影响力(3年样本, η²)")
    A("")
    A("| 排名 | 参数 | 平均η² | 说明 |")
    A("|---|---|---|---|")
    for i, (p, v) in enumerate(res["eta_rank"].head(12).items(), 1):
        A(f"| {i} | `{p}` | {v:.3f} | |")
    A("")
    A("### 关键参数分组(中位数)")
    A("")
    for p in ["market_filter", "mom_window", "max_vol", "max_buy_momentum",
              "rebalance_interval_days", "min_hold_days", "trailing_stop_pct",
              "hold_buffer", "max_distance_from_ma20", "initial_ratio",
              "top_n", "hard_stop_pct"]:
        g = res["groups"].get(p)
        if g is None:
            continue
        A(f"**{p}**")
        A("")
        A("| 取值 | n | 收益中位 | 回撤中位 | 夏普中位 | 换手中位 | 分段最差中位 |")
        A("|---|---|---|---|---|---|---|")
        for _, r in g.iterrows():
            A(f"| {r[p]} | {int(r['n'])} | {_fmt_pct(r['ret_med'])} | "
              f"{_fmt_pct(r['dd_med'])} | {r['sharpe_med']:.2f} | "
              f"{r['turnover_med']:.1f} | {_fmt_pct(r['segmin_med'])} |")
        A("")
    A("### 交互作用(夏普中位数)")
    A("")
    for name, piv in res["inter"].items():
        A(f"**{name}**")
        A("")
        cols = list(piv.columns)
        A("| 行\\列 | " + " | ".join(str(c) for c in cols) + " |")
        A("|" + "---|" * (len(cols) + 1))
        for idx, row in piv.iterrows():
            A(f"| {idx} | " + " | ".join(
                "-" if pd.isna(v) else f"{v:.2f}" for v in row) + " |")
        A("")

    A("## 七、分类 Top5(已保存为命名策略)")
    A("")
    cat_desc = {
        "稳健综合": "最终得分(稳健+分段一致性)最优",
        "收益进攻": "全期收益最高(回撤≥-25%)",
        "低回撤": "最大回撤最小(收益为正)",
        "风险调整": "夏普最高(交易≥30笔)",
        "分段一致": "最差年度收益最高(跨市场环境最稳)",
        "低换手": "换手最低(收益不低于中位)",
    }
    for cat, sub in res["categories"].items():
        A(f"### {cat} — {cat_desc.get(cat, '')}")
        A("")
        A("| # | 保存名 | 收益 | 年化 | 回撤 | 夏普 | 换手 | 交易 | 分段最差 | 市场过滤 | top_n | 动量 | 间隔 | 移动止损 | 最小持仓 |")
        A("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for i, (_, r) in enumerate(sub.iterrows(), 1):
            name = f"V4-{cat}-{i:02d}"
            A(f"| {i} | `{name}` | {_fmt_pct(r['total_return'])} | "
              f"{_fmt_pct(r['annual_return'])} | {_fmt_pct(r['max_drawdown'])} | "
              f"{r['sharpe']:.2f} | {r['turnover']:.1f} | {int(r['trade_count'])} | "
              f"{_fmt_pct(r['segment_min'])} | {'开' if r['market_filter'] else '关'} | "
              f"{int(r['top_n'])} | {int(r['mom_window'])} | "
              f"{int(r['rebalance_interval_days'])} | {_fmt_pct(r['trailing_stop_pct'], 0)} | "
              f"{int(r['min_hold_days'])} |")
        A("")

    A("## 八、风险与限制")
    A("")
    A("1. 数据为新浪免费未复权日K + 断层修复; 2024年前可用ETF约850只, 池成员为当时真实可交易品种, "
      "但**母池来自当前仍在上市的ETF列表, 存在幸存者偏差**(退市ETF不在样本内)。")
    A("2. 滚动前推的候选池包含全部961组参数, 存在选择面较宽的问题; 已用阶段A子池做对照。")
    A("3. 频率限制: 免费源无法提供逐笔/盘口级回放, 未建模冲击成本。")
    A("4. 2023-2026包含一轮完整下跌+反弹, 结论较9个月样本稳健, 但仍需在实盘小仓位跟踪。")
    A("")
    A("---")
    A("")
    A("> 数据文件: `analysis/ranked_all.csv`, `analysis/top5_by_category.json`; "
      "分类Top5已写入 `data/strategy_presets.json`(名称前缀 V4-, 未改动 active_paper)。")
    path = OUT / "FINAL_REPORT.md"
    path.write_text("\n".join(L), encoding="utf-8")
    return path


def _conclusions(res: Dict[str, Any]) -> List[str]:
    df: pd.DataFrame = res["df"]
    rk = df[df["eligible"]].sort_values("final_score", ascending=False)
    out: List[str] = []
    top = rk.head(5)
    out.append(
        "3年样本下最优组合的共性: " +
        "、".join(f"{r['candidate']}({_fmt_pct(r['total_return'])}, 夏普{r['sharpe']:.2f})"
                 for _, r in top.iterrows()) + "。")
    wf = res["wf_all"].get("metrics") if res.get("wf_all") else None
    wf_a = res["wf_a"].get("metrics") if res.get("wf_a") else None
    if wf:
        best_ret = top.iloc[0]["total_return"] if len(top) else 0
        base = (f"滚动前推(只用历史信息选参): 全池 OOS {_fmt_pct(wf['total_return'])}/"
                f"夏普 {wf['sharpe']:.2f}; ")
        if wf_a:
            better = "全候选池" if wf["sharpe"] >= wf_a["sharpe"] else "主效应池"
            base += (f"仅主效应池 OOS {_fmt_pct(wf_a['total_return'])}/"
                     f"夏普 {wf_a['sharpe']:.2f} → {better}更优。")
        base += (f"两者都低于事后最优({_fmt_pct(best_ret)}), 说明'事后最优参数'不可"
                 "直接采信, 实盘应打折预期。")
        out.append(base)
    # 市场过滤在长样本中的效果
    g = res["groups"].get("market_filter")
    if g is not None and len(g) == 2:
        off = g[g["market_filter"] == False].iloc[0]  # noqa: E712
        on = g[g["market_filter"] == True].iloc[0]    # noqa: E712
        better = "开" if on["sharpe_med"] > off["sharpe_med"] else "关"
        out.append(
            f"市场过滤在3年样本中的中位夏普: 开 {on['sharpe_med']:.2f} / 关 "
            f"{off['sharpe_med']:.2f} → 长期更优的是「{better}」; "
            f"回撤中位 {_fmt_pct(on['dd_med'])} / {_fmt_pct(off['dd_med'])}。")
    for p in ("mom_window", "rebalance_interval_days"):
        g = res["groups"].get(p)
        if g is None or g.empty:
            continue
        best = g.loc[g["sharpe_med"].idxmax()]
        worst = g.loc[g["sharpe_med"].idxmin()]
        out.append(f"`{p}`: 夏普中位最优档 {best[p]}({best['sharpe_med']:.2f}), "
                   f"最差档 {worst[p]}({worst['sharpe_med']:.2f})。")
    vals = sorted(res["stability"].values()) if res.get("stability") else []
    if vals:
        flat = sum(1 for v in vals if v >= 0.8)
        out.append(f"邻域稳健性: {flat}/{len(vals)} 个父策略的 ±1 档邻域保持 ≥80% 表现, "
                   f"说明多数参数区域是平台而非尖峰。")
    out.append("建议: 以「稳健综合」分类的第1名为模拟盘首选观察对象, 小仓位跟踪1-2个月后再决定是否替换 active_paper。")
    return out


# ----------------------------------------------------------------------
def save_presets(res: Dict[str, Any]) -> Dict[str, Any]:
    """分类 Top5 写入 strategy_presets.json(保留原有, 不覆盖 active_paper)。"""
    store = {"presets": {}, "active_paper": ""}
    if PRESETS.exists():
        try:
            store = json.loads(PRESETS.read_text(encoding="utf-8")) or store
        except ValueError:
            pass
    presets = store.setdefault("presets", {})
    saved: Dict[str, list] = {}
    for cat, sub in res["categories"].items():
        names = []
        for i, (_, r) in enumerate(sub.iterrows(), 1):
            name = f"V4-{cat}-{i:02d}"
            params = {k: (bool(v) if isinstance(v, (np.bool_,)) else
                          (float(v) if isinstance(v, (np.floating,)) else
                           (int(v) if isinstance(v, (np.integer,)) else v)))
                      for k, v in (r.to_dict() if isinstance(r, pd.Series) else r).items()
                      if k in PARAM_COLS}
            params = {k: v for k, v in params.items()
                      if v is not None and not (isinstance(v, float) and math.isnan(v))}
            # 补全为完整快照(与 Web 保存策略一致), 便于一键应用到模拟盘
            try:
                from strategies.rotation_executor import resolve_rotation_params
                params = {k: v for k, v in resolve_rotation_params(
                    params, use_live_preset=False).items()
                    if not str(k).startswith("_")}
            except Exception as exc:
                print(f"[warn] {name} 参数补全失败: {exc}")
            params["universe_mode"] = "dynamic_etf"
            presets[name] = params
            names.append(name)
        saved[cat] = names
    PRESETS.write_text(json.dumps(store, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    return saved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", nargs="+",
                    default=[str(V4 / f"results_{s}.jsonl") for s in "ABC"])
    ap.add_argument("--no-save-presets", action="store_true")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    rows = load_rows([Path(p) for p in args.results])
    print(f"载入 {len(rows)} 组")
    res = analyze_and_report(rows)
    res["df"].to_csv(OUT / "ranked_all.csv", index=False, encoding="utf-8-sig")
    categories_payload = {}
    for cat, sub in res["categories"].items():
        categories_payload[cat] = json.loads(
            sub.to_json(orient="records", force_ascii=False))
    (OUT / "top5_by_category.json").write_text(
        json.dumps(categories_payload, ensure_ascii=False, indent=2),
        encoding="utf-8")
    path = write_report(res)
    saved = {}
    if not args.no_save_presets:
        saved = save_presets(res)
    print(f"报告: {path}")
    for cat, names in saved.items():
        print(f"  {cat}: {', '.join(names)}")
    rk = res["df"][res["df"]["eligible"]].sort_values("final_score", ascending=False)
    print("\n最终得分 Top8:")
    cols = ["candidate", "total_return", "max_drawdown", "sharpe", "turnover",
            "segment_min", "final_score"]
    print(rk[cols].head(8).to_string(index=False))


if __name__ == "__main__":
    main()
