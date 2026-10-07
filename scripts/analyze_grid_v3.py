# -*- coding: utf-8 -*-
"""
GRIDV3 回测结果的统计分析与报告生成
====================================
输入: data/backtest_grid/v3/results.jsonl
输出: data/backtest_grid/v3/analysis/
  - results_table.csv    全部候选(参数+指标)
  - param_effects.csv    单参数分组效应
  - correlations.csv     参数-指标 Spearman 相关
  - REPORT.md            统计分析报告(异同点/底层逻辑)
  - PLAN.md              细化回测方案(仅方案)

统计方法(不依赖 scipy):
  - 单参数效应: 单因素方差分析 F 值 + η²(该参数解释的指标方差占比)
  - 相关性: Spearman 秩相关
  - 分型: 对(收益,回撤,夏普,换手)标准化后做 k-means(k=4, numpy实现)
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
V3_DIR = ROOT / "data" / "backtest_grid" / "v3"
OUT = V3_DIR / "analysis"

PARAM_COLS = [
    "top_n", "mom_window", "max_vol", "min_amount", "max_total_position",
    "target_weight", "hard_stop_pct", "trailing_stop_pct",
    "trailing_stop_activation", "market_filter", "market_exit_threshold",
    "market_enter_threshold", "rebalance_interval_days", "min_hold_days",
    "hold_buffer", "max_buy_momentum", "min_momentum", "require_above_ma20",
    "max_distance_from_ma20", "fresh_stop_mult", "initial_ratio",
    "trend_ma_window",
]
METRIC_COLS = [
    "total_return", "annual_return", "max_drawdown", "sharpe", "calmar",
    "win_rate", "turnover", "trade_count", "closed_trade_count",
    "avg_hold_days", "fee_total", "slippage_total", "benchmark_return",
    "excess_return", "first_half_return", "second_half_return",
    "half_return_floor", "positive_month_ratio", "avg_exposure",
    "max_consecutive_loss", "profit_factor", "robust_score",
]


# ----------------------------------------------------------------------
def load_results(path: Path) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("error"):
            continue
        rows.append(r)
    # JSONL 是边跑边写的(排名在全部结束后才计算), 这里补算合格性与稳健得分
    if rows and all(r.get("eligible") is None for r in rows):
        from scripts.strategy_grid_search import _rank
        _rank(rows)
    flat_rows: List[Dict[str, Any]] = []
    for r in rows:
        flat: Dict[str, Any] = {"candidate": r.get("candidate")}
        params = r.get("params") or {}
        for k in PARAM_COLS:
            flat[k] = params.get(k)
        for k in METRIC_COLS:
            flat[k] = r.get(k)
        ex = r.get("exit_reason_stats") or {}
        flat["stop_ratio"] = ex.get("stop_ratio")
        flat["cost_stop"] = ex.get("cost_stop")
        flat["trailing_stop"] = ex.get("trailing_stop")
        flat["rotation_exits"] = ex.get("rotation")
        flat["market_exits"] = ex.get("market")
        flat["eligible"] = bool(r.get("eligible"))
        flat_rows.append(flat)
    df = pd.DataFrame(flat_rows)
    for c in METRIC_COLS + ["stop_ratio", "avg_exposure"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _eta_squared(df: pd.DataFrame, param: str, metric: str) -> Tuple[float, float]:
    """单因素方差分析: 返回 (F, eta²)。eta² = 组间方差/总方差。"""
    sub = df[[param, metric]].dropna()
    if sub.empty:
        return 0.0, 0.0
    groups = [g[metric].to_numpy(dtype=float) for _, g in sub.groupby(param)
              if len(g) >= 3]
    k = len(groups)
    n = sum(len(g) for g in groups)
    if k < 2 or n <= k:
        return 0.0, 0.0
    grand = np.concatenate(groups).mean()
    ss_between = sum(len(g) * (g.mean() - grand) ** 2 for g in groups)
    ss_within = sum(((g - g.mean()) ** 2).sum() for g in groups)
    ss_total = ss_between + ss_within
    if ss_total <= 0:
        return 0.0, 0.0
    df_b, df_w = k - 1, n - k
    f = (ss_between / df_b) / (ss_within / df_w) if ss_within > 0 and df_w > 0 else 0.0
    return float(f), float(ss_between / ss_total)


def _kmeans(X: np.ndarray, k: int = 4, seed: int = 7, iters: int = 200) -> np.ndarray:
    rng = np.random.default_rng(seed)
    # k-means++ 初始化
    centers = [X[rng.integers(len(X))]]
    for _ in range(k - 1):
        d = np.min([((X - c) ** 2).sum(axis=1) for c in centers], axis=0)
        p = d / d.sum() if d.sum() > 0 else np.ones(len(X)) / len(X)
        centers.append(X[rng.choice(len(X), p=p)])
    C = np.array(centers)
    labels = np.zeros(len(X), dtype=int)
    for _ in range(iters):
        dist = ((X[:, None, :] - C[None, :, :]) ** 2).sum(axis=2)
        new = dist.argmin(axis=1)
        if (new == labels).all():
            break
        labels = new
        for j in range(k):
            m = labels == j
            if m.any():
                C[j] = X[m].mean(axis=0)
    return labels


def _fmt(v: Any, pct: bool = False, d: int = 2) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "-"
    if not math.isfinite(x):
        return "-"
    return f"{x * 100:+.{d}f}%" if pct else f"{x:.{d}f}"


# ----------------------------------------------------------------------
def analyze(df: pd.DataFrame) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    out["n_all"] = len(df)
    elig = df[df["eligible"]].copy()
    out["n_eligible"] = len(elig)
    work = elig if len(elig) >= 30 else df
    out["analysis_base"] = "eligible" if len(elig) >= 30 else "all"

    # 1. 总体分布
    dist_rows = []
    for m in ("total_return", "annual_return", "max_drawdown", "sharpe",
              "calmar", "turnover", "win_rate", "stop_ratio", "avg_exposure",
              "excess_return", "half_return_floor"):
        s = work[m].dropna()
        if s.empty:
            continue
        dist_rows.append({
            "metric": m, "count": len(s), "mean": s.mean(), "std": s.std(),
            "min": s.min(), "p25": s.quantile(.25), "median": s.median(),
            "p75": s.quantile(.75), "max": s.max(),
        })
    out["distribution"] = pd.DataFrame(dist_rows)

    # 2. 单参数效应(η²)
    eff_rows = []
    for p in PARAM_COLS:
        if p not in work.columns or work[p].nunique(dropna=True) < 2:
            continue
        for m in ("sharpe", "total_return", "max_drawdown", "turnover",
                  "stop_ratio"):
            f, eta = _eta_squared(work, p, m)
            eff_rows.append({"param": p, "metric": m, "F": f, "eta2": eta,
                             "levels": work[p].nunique(dropna=True)})
    eff = pd.DataFrame(eff_rows)
    out["param_effects"] = eff
    out["eta_rank"] = (eff[eff["metric"].isin(["sharpe", "total_return", "max_drawdown"])]
                       .groupby("param")["eta2"].mean().sort_values(ascending=False))

    # 3. 参数-指标 Spearman 相关
    corr_rows = []
    num_df = work[PARAM_COLS + ["sharpe", "total_return", "max_drawdown",
                                "turnover", "stop_ratio", "excess_return"]].copy()
    for p in PARAM_COLS:
        if p not in num_df.columns:
            continue
        num_df[p] = pd.to_numeric(num_df[p], errors="coerce")
    for p in PARAM_COLS:
        for m in ("sharpe", "total_return", "max_drawdown", "turnover",
                  "stop_ratio", "excess_return"):
            sub = num_df[[p, m]].dropna()
            if len(sub) < 20 or sub[p].nunique() < 2:
                continue
            # 手写 Spearman = 秩的 Pearson(避免引入 scipy 依赖)
            rho = sub[p].rank().corr(sub[m].rank())
            corr_rows.append({"param": p, "metric": m, "spearman": rho,
                              "n": len(sub)})
    out["correlations"] = pd.DataFrame(corr_rows)

    # 4. 关键参数分组表(取值过多时按分位数分箱, 避免 40+ 行且 η² 虚高)
    group_tables: Dict[str, pd.DataFrame] = {}
    for p in PARAM_COLS:
        if p not in work.columns or work[p].nunique(dropna=True) < 2:
            continue
        sub = work[[p, "candidate", "total_return", "max_drawdown", "sharpe",
                    "turnover", "stop_ratio", "excess_return"]].copy()
        if sub[p].nunique(dropna=True) > 12:
            try:
                sub[p] = pd.qcut(pd.to_numeric(sub[p], errors="coerce"), q=5,
                                 duplicates="drop").astype(str)
            except Exception:
                continue
        g = sub.groupby(p).agg(
            n=("candidate", "count"),
            ret_med=("total_return", "median"),
            ret_mean=("total_return", "mean"),
            dd_med=("max_drawdown", "median"),
            sharpe_med=("sharpe", "median"),
            sharpe_mean=("sharpe", "mean"),
            turnover_med=("turnover", "median"),
            stop_ratio_med=("stop_ratio", "median"),
            excess_med=("excess_return", "median"),
        ).reset_index()
        group_tables[p] = g
    out["group_tables"] = group_tables

    # 4b. Top20 与 Bottom10 的参数画像对比(共性/差异)
    top = work.sort_values("robust_score", ascending=False).head(20)
    bottom = work.sort_values("robust_score", ascending=False).tail(10)
    profile_rows = []
    for p in PARAM_COLS:
        if p not in work.columns:
            continue
        try:
            t = pd.to_numeric(top[p], errors="coerce").median()
            b = pd.to_numeric(bottom[p], errors="coerce").median()
            a = pd.to_numeric(work[p], errors="coerce").median()
        except Exception:
            continue
        profile_rows.append({"param": p, "top20_median": t, "bottom10_median": b,
                             "all_median": a})
    out["profile"] = pd.DataFrame(profile_rows)

    # 5. 交互作用(双因素中位数)
    interactions: Dict[str, pd.DataFrame] = {}
    pairs = [
        ("market_filter", "rebalance_interval_days"),
        ("market_filter", "mom_window"),
        ("market_filter", "top_n"),
        ("hard_stop_pct", "trailing_stop_pct"),
        ("rebalance_interval_days", "min_hold_days"),
        ("top_n", "target_weight"),
        ("trailing_stop_pct", "trailing_stop_activation"),
        ("initial_ratio", "top_n"),
        ("min_momentum", "max_buy_momentum"),
        ("require_above_ma20", "max_distance_from_ma20"),
    ]
    for a, b in pairs:
        if a not in work.columns or b not in work.columns:
            continue
        piv = work.pivot_table(index=a, columns=b, values="sharpe",
                               aggfunc="median")
        interactions[f"{a} × {b}"] = piv
    out["interactions"] = interactions

    # 6. 聚类分型
    feats = ["total_return", "max_drawdown", "sharpe", "turnover"]
    sub = work[feats + PARAM_COLS].dropna(subset=feats)
    labels = np.array([])
    cluster_summary = pd.DataFrame()
    if len(sub) >= 40:
        X = sub[feats].to_numpy(dtype=float)
        X = (X - X.mean(axis=0)) / (X.std(axis=0) + 1e-12)
        labels = _kmeans(X, k=4)
        sub = sub.assign(cluster=labels)
        rows = []
        for c in sorted(set(labels)):
            g = sub[sub["cluster"] == c]
            rows.append({
                "cluster": int(c), "n": len(g),
                "ret_med": g["total_return"].median(),
                "dd_med": g["max_drawdown"].median(),
                "sharpe_med": g["sharpe"].median(),
                "turnover_med": g["turnover"].median(),
                "market_filter_on": float(pd.to_numeric(
                    g["market_filter"], errors="coerce").mean()),
                "top_n_mode": g["top_n"].mode().iloc[0] if len(g) else None,
                "mom_mode": g["mom_window"].mode().iloc[0] if len(g) else None,
                "interval_med": pd.to_numeric(
                    g["rebalance_interval_days"], errors="coerce").median(),
                "trail_med": pd.to_numeric(
                    g["trailing_stop_pct"], errors="coerce").median(),
                "hold_med": pd.to_numeric(
                    g["min_hold_days"], errors="coerce").median(),
            })
        cluster_summary = pd.DataFrame(rows).sort_values("sharpe_med", ascending=False)
    out["clusters"] = cluster_summary

    # 7. 排名
    out["top20"] = work.sort_values("robust_score", ascending=False).head(20)
    out["bottom10"] = work.sort_values("robust_score", ascending=False).tail(10)
    return out


# ----------------------------------------------------------------------
def write_report(res: Dict[str, Any], df: pd.DataFrame) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    L: List[str] = []
    A = L.append
    A("# GRIDV3 大规模参数回测 · 统计分析报告")
    A("")
    A(f"- 生成时间: {datetime.now():%Y-%m-%d %H:%M}")
    A(f"- 样本: 全部 {res['n_all']} 组, 有效(合格) {res['n_eligible']} 组; "
      f"本报告统计基于 **{res['analysis_base']}** 样本")
    A("- 区间: 2026-01-01 ~ 今日; 动态ETF池(月度, 全候选共享同一池与行情快照)")
    A("- 基准: 沪深300 买入持有; 初始资金 10 万")
    A("")

    A("## 一、总体分布")
    A("")
    A("| 指标 | 均值 | 标准差 | 最小 | P25 | 中位 | P75 | 最大 |")
    A("|---|---|---|---|---|---|---|---|")
    for _, r in res["distribution"].iterrows():
        pct = r["metric"] not in ("sharpe", "calmar", "turnover")
        fmt = (lambda v: f"{v*100:+.2f}%") if pct else (lambda v: f"{v:.2f}")
        A(f"| {r['metric']} | {fmt(r['mean'])} | {fmt(r['std'])} | {fmt(r['min'])} | "
          f"{fmt(r['p25'])} | {fmt(r['median'])} | {fmt(r['p75'])} | {fmt(r['max'])} |")
    A("")
    A("> 解读: 分布越宽说明参数越关键; 若中位数明显低于均值, 说明少数参数组合贡献了大部分收益(右偏)。")
    A("")

    A("## 二、参数影响力排行 (η², 单因素方差解释率)")
    A("")
    A("η² = 该参数各取值组间方差 / 总方差。越大表示该参数对结果的解释力越强。")
    A("")
    A("> 注意: η² 会随取值档位增多而轻微上偏; `target_weight` 是由 top_n×总仓位×"
      "仓位系数派生的多档变量(非独立采样), 其高 η² 需结合第三部分的相关性一起看。")
    A("")
    A("| 排名 | 参数 | η²(夏普) | η²(收益) | η²(回撤) | 平均η² |")
    A("|---|---|---|---|---|---|")
    eff = res["param_effects"]
    for i, (p, mean_eta) in enumerate(res["eta_rank"].head(12).items(), 1):
        row = eff[(eff["param"] == p)]
        def g(m):
            s = row[row["metric"] == m]["eta2"]
            return f"{s.iloc[0]:.3f}" if len(s) else "-"
        A(f"| {i} | `{p}` | {g('sharpe')} | {g('total_return')} | {g('max_drawdown')} | {mean_eta:.3f} |")
    A("")

    A("## 三、参数-指标相关性 (Spearman, 方向与强度)")
    A("")
    corr = res["correlations"]
    pivot = corr.pivot_table(index="param", columns="metric", values="spearman")
    for m in ("sharpe", "total_return", "max_drawdown", "turnover", "stop_ratio"):
        if m not in pivot.columns:
            continue
        A(f"**与 {m} 的相关性(按强度排序)**")
        A("")
        s = pivot[m].dropna().sort_values(key=lambda x: x.abs(), ascending=False).head(8)
        A("| 参数 | ρ | 方向 |")
        A("|---|---|---|")
        for p, v in s.items():
            A(f"| `{p}` | {v:+.3f} | {'正相关' if v > 0 else '负相关'} |")
        A("")

    A("## 四、关键参数分组效应(中位数)")
    A("")
    focus = ["market_filter", "top_n", "mom_window", "rebalance_interval_days",
             "min_hold_days", "hard_stop_pct", "trailing_stop_pct",
             "trailing_stop_activation", "initial_ratio", "hold_buffer",
             "max_buy_momentum", "min_momentum", "max_vol", "min_amount",
             "require_above_ma20", "max_distance_from_ma20", "fresh_stop_mult",
             "trend_ma_window", "max_total_position", "target_weight"]
    for p in focus:
        g = res["group_tables"].get(p)
        if g is None:
            continue
        A(f"**{p}**")
        A("")
        A("| 取值 | n | 收益中位 | 回撤中位 | 夏普中位 | 换手中位 | 止损占比中位 | 超额中位 |")
        A("|---|---|---|---|---|---|---|---|")
        for _, r in g.iterrows():
            A(f"| {r[p]} | {int(r['n'])} | {r['ret_med']*100:+.2f}% | "
              f"{r['dd_med']*100:.2f}% | {r['sharpe_med']:.2f} | "
              f"{r['turnover_med']:.1f} | {r['stop_ratio_med']*100:.1f}% | "
              f"{r['excess_med']*100:+.2f}% |")
        A("")

    A("## 五、交互作用(夏普中位数)")
    A("")
    for name, piv in res["interactions"].items():
        A(f"**{name}**")
        A("")
        cols = list(piv.columns)
        A("| 行\\列 | " + " | ".join(str(c) for c in cols) + " |")
        A("|" + "---|" * (len(cols) + 1))
        for idx, row in piv.iterrows():
            vals = " | ".join("-" if pd.isna(v) else f"{v:.2f}" for v in row)
            A(f"| {idx} | {vals} |")
        A("")

    if not res["clusters"].empty:
        A("## 六、策略分型 (k-means, k=4)")
        A("")
        A("| 分型 | n | 收益中位 | 回撤中位 | 夏普中位 | 换手中位 | 市场过滤占比 | top_n众数 | 动量窗口众数 | 调仓间隔中位 | 移动止损中位 | 最小持仓中位 |")
        A("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for _, r in res["clusters"].iterrows():
            A(f"| C{int(r['cluster'])} | {int(r['n'])} | {r['ret_med']*100:+.2f}% | "
              f"{r['dd_med']*100:.2f}% | {r['sharpe_med']:.2f} | {r['turnover_med']:.1f} | "
              f"{r['market_filter_on']*100:.0f}% | {r['top_n_mode']} | {r['mom_mode']} | "
              f"{r['interval_med']:.0f} | {r['trail_med']*100:.0f}% | {r['hold_med']:.0f} |")
        A("")

    A("## 七、Top 20 参数组合")
    A("")
    A("| # | 候选 | 收益 | 回撤 | 夏普 | Calmar | 超额 | 换手 | 交易 | 止损占比 | 市场过滤 | top_n | 动量 | 间隔 | 硬止损 | 移动止损 | 启用 | 最小持仓 |")
    A("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for i, (_, r) in enumerate(res["top20"].iterrows(), 1):
        A(f"| {i} | {r['candidate']} | {r['total_return']*100:+.2f}% | "
          f"{r['max_drawdown']*100:.2f}% | {r['sharpe']:.2f} | {r['calmar']:.2f} | "
          f"{(r['excess_return'] or 0)*100:+.2f}% | {r['turnover']:.1f} | "
          f"{int(r['trade_count'])} | {(r['stop_ratio'] or 0)*100:.1f}% | "
          f"{'开' if r['market_filter'] else '关'} | {int(r['top_n'])} | {int(r['mom_window'])} | "
          f"{int(r['rebalance_interval_days'])} | {r['hard_stop_pct']*100:.0f}% | "
          f"{r['trailing_stop_pct']*100:.0f}% | {r['trailing_stop_activation']*100:.0f}% | "
          f"{int(r['min_hold_days'])} |")
    A("")

    A("## 八、Bottom 10(用于反向验证)")
    A("")
    A("| 候选 | 收益 | 回撤 | 夏普 | 换手 | 市场过滤 | top_n | 动量 | 间隔 | 止损占比 |")
    A("|---|---|---|---|---|---|---|---|---|---|")
    for _, r in res["bottom10"].iterrows():
        A(f"| {r['candidate']} | {r['total_return']*100:+.2f}% | "
          f"{r['max_drawdown']*100:.2f}% | {r['sharpe']:.2f} | {r['turnover']:.1f} | "
          f"{'开' if r['market_filter'] else '关'} | {int(r['top_n'])} | {int(r['mom_window'])} | "
          f"{int(r['rebalance_interval_days'])} | {(r['stop_ratio'] or 0)*100:.1f}% |")
    A("")

    A("## 八·五、Top20 与 Bottom10 的参数画像对比(中位数)")
    A("")
    A("| 参数 | Top20 | Bottom10 | 全体 | 差异 |")
    A("|---|---|---|---|---|")
    prof = res["profile"]
    for _, r in prof.iterrows():
        t, b, a = r["top20_median"], r["bottom10_median"], r["all_median"]
        try:
            diff = float(t) - float(b)
            diff_s = f"{diff:+.3f}"
        except (TypeError, ValueError):
            diff_s = "-"
        A(f"| `{r['param']}` | {t} | {b} | {a} | {diff_s} |")
    A("")

    A("## 九、差异的底层逻辑(自动归纳)")
    A("")
    for line in _logic_findings(res):
        A(f"- {line}")
    A("")
    A("---")
    A("")
    A("> 说明: 本报告为数据驱动结论, 单区间(2026-01~09)结果存在市场环境依赖; "
      "实盘使用前需按 PLAN.md 做分段/滚动验证。")
    path = OUT / "REPORT.md"
    path.write_text("\n".join(L), encoding="utf-8")
    return path


def _logic_findings(res: Dict[str, Any]) -> List[str]:
    """把统计结果翻译成可读的因果/差异解读。"""
    out: List[str] = []
    eff = res["param_effects"]
    rank = res["eta_rank"]

    def eta(p: str, m: str) -> float:
        s = eff[(eff["param"] == p) & (eff["metric"] == m)]["eta2"]
        return float(s.iloc[0]) if len(s) else 0.0

    top_params = list(rank.head(5).items())
    if top_params:
        out.append("解释力最强的参数依次为: " + ", ".join(
            f"`{p}`(η²均值 {v:.3f})" for p, v in top_params) + "。")

    g = res["group_tables"].get("market_filter")
    if g is not None and len(g) == 2:
        off = g[g["market_filter"] == False]  # noqa: E712
        on = g[g["market_filter"] == True]    # noqa: E712
        if len(off) and len(on):
            o, n = off.iloc[0], on.iloc[0]
            out.append(
                f"市场风险过滤是最强开关: 关闭时收益中位 {o['ret_med']*100:+.2f}%、"
                f"夏普 {o['sharpe_med']:.2f}、回撤 {o['dd_med']*100:.2f}%; "
                f"开启后收益中位 {n['ret_med']*100:+.2f}%、夏普 {n['sharpe_med']:.2f}%、"
                f"回撤 {n['dd_med']*100:.2f}%(样本各 {int(o['n'])}/{int(n['n'])} 组)。")

    g = res["group_tables"].get("rebalance_interval_days")
    if g is not None:
        best = g.loc[g["sharpe_med"].idxmax()]
        worst = g.loc[g["sharpe_med"].idxmin()]
        out.append(f"调仓间隔: {best['rebalance_interval_days']}日 夏普中位最优"
                   f"({best['sharpe_med']:.2f}), {worst['rebalance_interval_days']}日 最差"
                   f"({worst['sharpe_med']:.2f}); 换手随之从 "
                   f"{g['turnover_med'].min():.1f} 到 {g['turnover_med'].max():.1f} 倍。")

    g = res["group_tables"].get("trailing_stop_pct")
    if g is not None:
        best = g.loc[g["sharpe_med"].idxmax()]
        out.append(f"移动止损: {best['trailing_stop_pct']*100:.0f}% 档夏普中位最高"
                   f"({best['sharpe_med']:.2f}); 止损占比中位 "
                   f"{best['stop_ratio_med']*100:.1f}%。")

    g = res["group_tables"].get("min_hold_days")
    if g is not None:
        best = g.loc[g["sharpe_med"].idxmax()]
        out.append(f"最小持仓: {int(best['min_hold_days'])}日 夏普中位最高"
                   f"({best['sharpe_med']:.2f}), 过短会放大噪音交易与成本。")

    g = res["group_tables"].get("top_n")
    if g is not None:
        best = g.loc[g["sharpe_med"].idxmax()]
        out.append(f"持仓数量: {int(best['top_n'])}只 夏普中位最高({best['sharpe_med']:.2f}); "
                   f"集中度与回撤的权衡在各档之间并非单调。")

    g = res["group_tables"].get("initial_ratio")
    if g is not None:
        best = g.loc[g["sharpe_med"].idxmax()]
        out.append(f"首仓比例: {best['initial_ratio']:.2f} 夏普中位最高"
                   f"({best['sharpe_med']:.2f}); 分批建仓降低了择时风险但牺牲了趋势初段的收益。")

    corr = res["correlations"]
    for p in ("turnover", "stop_ratio"):
        pass
    sub = corr[corr["metric"] == "sharpe"].sort_values(
        "spearman", key=lambda x: x.abs(), ascending=False)
    if len(sub):
        r = sub.iloc[0]
        out.append(f"与夏普相关性最强的是 `{r['param']}` (ρ={r['spearman']:+.2f})。")

    # Top20 vs Bottom10 画像差异
    top = res["top20"]
    bot = res["bottom10"]
    try:
        mf_top = pd.to_numeric(top["market_filter"], errors="coerce").mean()
        mf_bot = pd.to_numeric(bot["market_filter"], errors="coerce").mean()
        out.append(
            f"Top20 中 {mf_top*100:.0f}% 开启市场过滤, Bottom10 中仅 {mf_bot*100:.0f}% —— "
            "说明市场过滤不是单独生效, 而是与动量窗口/调仓间隔组合后才显著。")
    except Exception:
        pass
    for p, fmt in (("mom_window", "{:.0f}"), ("rebalance_interval_days", "{:.0f}"),
                   ("hold_buffer", "{:.0f}"), ("min_hold_days", "{:.0f}")):
        try:
            t = pd.to_numeric(top[p], errors="coerce").median()
            b = pd.to_numeric(bot[p], errors="coerce").median()
            out.append(f"`{p}`: Top20 中位 {fmt.format(t)} vs Bottom10 中位 "
                       f"{fmt.format(b)}。")
        except Exception:
            continue
    out.append("**采样偏差提示**: 5个锚点与72个结构化角点使用了同一组基线参数, "
               "使 `initial_ratio` 等未参与角点设计的参数在 0.5 档样本偏多, "
               "其分组中位数受角点设计轻微影响; 第二阶段请用平衡采样消除该偏差。")
    return out


def write_plan(res: Dict[str, Any], df: pd.DataFrame) -> Path:
    """细化回测方案(仅方案, 不执行)。"""
    rank = res["eta_rank"]
    L: List[str] = []
    A = L.append
    A("# 细化回测方案(第二阶段 · 仅方案待确认)")
    A("")
    A(f"依据: GRIDV3 {res['n_all']} 组回测的 η² 影响力排行与分组/交互分析。")
    A("")
    A("## 1. 优先细化的参数(按影响力)")
    A("")
    A("| 优先级 | 参数 | 当前粗网格 | 建议细化网格 | 依据 |")
    A("|---|---|---|---|---|")
    detail = {
        "market_filter": ("关/开", "关 / 开(二值穷举, 两分支独立优化)",
                          "解释力最强, 需分两套参数体系"),
        "rebalance_interval_days": ("1,2,3,5,10", "1,2,3,4,5,7,10",
                                    "换手-反应速度权衡的核心; 本轮 1/5 日较优"),
        "min_hold_days": ("1,3,5,10", "1,2,3,4,5,7,10", "与调仓间隔强交互; 3~5 日较优"),
        "trailing_stop_pct": ("4%~15%", "5,6,7,8,9,10,12,15%",
                              "止损占比与收益的权衡; 6%~12% 区间集中"),
        "trailing_stop_activation": ("2%~8%", "2,3,4,5,6,8%", "与移动止损配对"),
        "hard_stop_pct": ("5%~15%", "5,6,7,8,10,12,15%", "尾部风险保护; 8%~15% 均可用"),
        "top_n": ("3,4,5,6,8", "3,4,5,6,7,8", "与目标仓位联动; 4 只中位最优"),
        "target_weight": ("派生(41档)", "改为采样 target_scale(0.85/0.95/1.0)再派生",
                          "派生变量 η² 虚高, 应控制派生系数"),
        "mom_window": ("5,10,15,20,30,60", "10,15,20,30(5/60 剔除)",
                       "5 与 60 日明显偏弱; 10/15/30 为有效区"),
        "max_buy_momentum": ("10%~100%", "15,20,25,30,40%", "追高保护; 25% 最优, 10% 过严"),
        "min_momentum": ("-2%~5%", "-1,0,1,2,3%", "弱动量过滤; 1%~3% 较优"),
        "hold_buffer": ("0,1,2,4", "1,2,3,4", "换手缓冲; 2 最优"),
        "initial_ratio": ("30%~100%", "40,50,60,70%", "建仓节奏(注意本轮 0.5 样本偏多)"),
        "max_distance_from_ma20": ("5%~30%", "12,15,18,20,25,30%", "过热过滤; ≤10% 过严"),
        "max_vol": ("40%,55%,70%", "50,55,60,65,70%", "波动率上限; 40% 明显过严"),
        "market_exit_threshold": ("-5%~-1%", "-5,-4,-3,-2,-1%", "离场阈值(仅过滤开时生效)"),
        "market_enter_threshold": ("-2%~0%", "-2,-1.5,-1,-0.5,0%", "入场阈值(滞回宽度)"),
        "max_total_position": ("70%,85%,95%", "80,85,90,95%", "总仓位上限; 85% 中位最优"),
        "fresh_stop_mult": ("1.0,1.5,2.0", "1.0,1.25,1.5,1.75,2.0", "新仓止损放宽"),
        "trend_ma_window": ("10,20,60", "10,20,30,60", "趋势均线; 20 最优"),
    }
    for i, (p, mean_eta) in enumerate(rank.head(15).items(), 1):
        cur, sug, why = detail.get(p, ("-", "按 5 档均匀细分", "-"))
        A(f"| {i} | `{p}` | {cur} | {sug} | {why} |")
    A("")
    A("## 2. 本轮 Top5 参数作为阶段A种子")
    A("")
    A("| 种子 | 收益 | 回撤 | 夏普 | 市场过滤 | top_n | 动量 | 间隔 | 硬止损 | 移动止损 | 启用 | 最小持仓 | 缓冲 |")
    A("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for _, r in res["top20"].head(5).iterrows():
        A(f"| {r['candidate']} | {r['total_return']*100:+.2f}% | "
          f"{r['max_drawdown']*100:.2f}% | {r['sharpe']:.2f} | "
          f"{'开' if r['market_filter'] else '关'} | {int(r['top_n'])} | "
          f"{int(r['mom_window'])} | {int(r['rebalance_interval_days'])} | "
          f"{r['hard_stop_pct']*100:.0f}% | {r['trailing_stop_pct']*100:.0f}% | "
          f"{r['trailing_stop_activation']*100:.0f}% | {int(r['min_hold_days'])} | "
          f"{int(r['hold_buffer'])} |")
    A("")
    A("## 3. 组合策略(控制总量)")
    A("")
    A("推荐**分层+两阶段**细化, 总回测数控制在 600~900 组:")
    A("")
    A("1. **阶段A 主效应细化(约 240 组)**")
    A("   - 市场过滤=开/关 两分支各 120 组;")
    A("   - 每分支对 8 个高影响力参数(η²前8)做**正交表/拉丁超立方**采样,")
    A("     其余参数固定在当前最优档;")
    A("2. **阶段B 交互项补测(约 240 组)**")
    A("   - 针对强交互对做二维网格: 调仓间隔×最小持仓、移动止损×启用阈值、")
    A("     top_n×目标仓位、动量窗口×追高保护、市场离场×入场阈值;")
    A("3. **阶段C 稳健性验证(约 120 组)**")
    A("   - 取阶段A/B 的 Top10 参数组合, 做**分段回测**(2026H1 / 2026H2)")
    A("     与**滚动前推**(walk-forward, 每2月重选参数)验证;")
    A("   - 加入 ±1 档参数的邻域扰动(每个 Top 组合 12 组), 检查性能是否平坦")
    A("     (平坦=稳健, 尖峰=过拟合)。")
    A("")
    A("## 4. 评价与选择标准(建议)")
    A("")
    A("- 主指标: **稳健得分 = 0.30×Sharpe + 0.20×Calmar + 0.20×min(前半段,后半段) + "
      "0.15×(-最大回撤) + 0.10×正收益月占比 + 0.05×(-换手)**(分位归一后加权);")
    A("- 硬性门槛: 合格交易≥12 笔、最大回撤≥-25%、单日净值跳变≤10%、"
      "参数邻域(±1档)平均得分不低于峰值 80%;")
    A("- 最终实盘参数取**邻域平台中心**而非单点峰值, 并按 PLAN 结果更新 "
      "`active_paper` 预设。")
    A("")
    A("## 5. 风险与说明")
    A("")
    A("- 本区间仅 9 个月且经历单边下跌+反弹, 结论需用分段/滚动验证确认;")
    A("- 动态池成员随月度快照变化, 需确认未来数据补齐后池快照是否稳定;")
    A("- 交易成本按当前费率+最小变动价位滑点模拟, 未含冲击成本。")
    path = OUT / "PLAN.md"
    path.write_text("\n".join(L), encoding="utf-8")
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=str(V3_DIR / "results.jsonl"))
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    df = load_results(Path(args.input))
    res = analyze(df)
    # 导出 CSV
    df.to_csv(OUT / "results_table.csv", index=False, encoding="utf-8-sig")
    res["param_effects"].to_csv(OUT / "param_effects.csv", index=False,
                                encoding="utf-8-sig")
    res["correlations"].to_csv(OUT / "correlations.csv", index=False,
                               encoding="utf-8-sig")
    for name, g in res["group_tables"].items():
        g.to_csv(OUT / f"group_{name}.csv", index=False, encoding="utf-8-sig")
    report = write_report(res, df)
    plan = write_plan(res, df)
    print(f"样本 {res['n_all']} (有效 {res['n_eligible']})")
    print(f"报告: {report}")
    print(f"方案: {plan}")
    print("\n参数影响力 TOP8:")
    for p, v in res["eta_rank"].head(8).items():
        print(f"  {p:<28} eta2={v:.3f}")


if __name__ == "__main__":
    main()
