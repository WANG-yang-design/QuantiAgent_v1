# -*- coding: utf-8 -*-
"""Create an auditable Markdown analysis from a grid-search summary."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List


KEY_PARAMS = (
    "top_n", "mom_window", "market_filter", "rebalance_interval_days",
    "hard_stop_pct", "trailing_stop_pct", "trailing_stop_activation",
    "target_weight", "max_total_position", "min_hold_days", "hold_buffer",
    "max_buy_momentum", "min_momentum", "initial_ratio", "bottom_ratio",
    "require_above_ma20", "trend_ma_window", "max_distance_from_ma20",
)


def pct(value: Any) -> str:
    return f"{float(value or 0):+.2%}"


def _group_stats(rows: List[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    grouped: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row.get("params") or {}).get(key)].append(row)
    return [
        {
            "value": value,
            "count": len(items),
            "avg_score": mean(float(x["robust_score"]) for x in items),
            "avg_return": mean(float(x["total_return"]) for x in items),
            "avg_drawdown": mean(float(x["max_drawdown"]) for x in items),
            "avg_sharpe": mean(float(x["sharpe"]) for x in items),
        }
        for value, items in grouped.items()
    ]


def analyze(path: Path) -> Path:
    data = json.loads(path.read_text(encoding="utf-8"))
    results = data["results"]
    valid = [r for r in results if r.get("eligible")]
    top5 = valid[:5]
    # Enrich compact search rows from the normal result table.  This checks
    # whether one symbol or a price-regime repair dominates a winning result.
    if str(path.resolve().parent.parent) not in sys.path:
        sys.path.insert(0, str(path.resolve().parent.parent))
    from database import repository as repo
    top5_details = {}
    for row in top5:
        stored = repo.get_backtest_result(str(row.get("run_id")))
        metrics = stored.metrics_json if stored is not None else {}
        stats = metrics.get("symbol_stats") or []
        positive = [x for x in stats if float(x.get("realized_pnl") or 0) > 0]
        gross_positive = sum(float(x.get("realized_pnl") or 0) for x in positive)
        leader_share = (
            float(positive[0].get("realized_pnl") or 0) / gross_positive
            if positive and gross_positive > 0 else 0.0
        )
        top5_details[row["run_id"]] = {
            "leader_share": leader_share,
            "contributors": stats[:5],
        }
    presets = sorted(
        (r for r in results if r.get("family") == "saved_preset" and not r.get("error")),
        key=lambda r: r.get("robust_score") or -1,
        reverse=True,
    )
    grid = [r for r in valid if r.get("family") == "grid"]
    top_values = {
        key: Counter((r.get("params") or {}).get(key) for r in top5)
        for key in KEY_PARAMS
    }
    common = {key: counts.most_common(1)[0] for key, counts in top_values.items()}
    sensitivity = {
        key: sorted(_group_stats(grid, key), key=lambda x: x["avg_score"], reverse=True)
        for key in ("top_n", "mom_window", "market_filter",
                    "rebalance_interval_days", "hard_stop_pct")
    }

    lines = [
        "# 监控池轮动策略参数网格回测分析",
        "",
        f"- 实验：`{data['experiment']}`",
        f"- 区间：{data['period']['start']} 至 {data['period']['end']}，初始资金 {data['initial_cash']:.2f} 元",
        f"- 标的：{len(data['symbols'])} 只可交易 ETF/股票；指数已排除",
        f"- 候选：{len(results)} 组（8 个保存策略 + {data['grid_count']} 个网格点）",
        f"- 有效同口径样本：{len(valid)}；失败/剔除：{sum(1 for r in results if r.get('error') or not r.get('eligible'))}",
        f"- 冻结行情哈希：`{data.get('snapshot_hash', '')}`",
        "- 排名不是按总收益单项，而是夏普、Calmar、年化、前后半段较差收益、回撤、月度胜率、盈亏比及低换手的加权百分位。",
        "",
        "## Top 5",
        "",
        "|排名|候选|稳健分|总收益|年化|最大回撤|夏普|Calmar|前半段|后半段|成交笔数|",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in top5:
        lines.append(
            f"|{r['rank']}|`{r['candidate']}`|{r['robust_score']:.2f}|{pct(r['total_return'])}|"
            f"{pct(r['annual_return'])}|{pct(r['max_drawdown'])}|{r['sharpe']:.2f}|"
            f"{r['calmar']:.2f}|{pct(r['first_half_return'])}|{pct(r['second_half_return'])}|"
            f"{r['trade_count']}|"
        )
    lines += ["", "### Top5 参数异同", ""]
    for key in KEY_PARAMS:
        values = [str((r.get("params") or {}).get(key)) for r in top5]
        mode, count = common[key]
        label = "共同" if count == len(top5) else "差异"
        lines.append(f"- {label} `{key}`：{', '.join(values)}（众数 {mode}，{count}/5）")

    lines += [
        "",
        "### Top5 收益质量",
        "",
        "|排名|平均仓位|换手|费用+滑点|最大单日净值变化|最大正贡献标的占正贡献|前五贡献标的|",
        "|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in top5:
        detail = top5_details[r["run_id"]]
        contributors = "、".join(
            f"{x.get('symbol')}({float(x.get('realized_pnl') or 0):+.0f})"
            for x in detail["contributors"]
        )
        lines.append(
            f"|{r['rank']}|{pct(r['avg_exposure'])}|{r['turnover']:.1f}x|"
            f"{r['fee_total'] + r['slippage_total']:.2f}|{pct(r['max_daily_jump'])}|"
            f"{pct(detail['leader_share'])}|{contributors}|"
        )

    lines += [
        "",
        "## 八个已保存策略复核",
        "",
        "|稳健排名|策略|稳健分|总收益|最大回撤|夏普|前半段|后半段|成交笔数|",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in presets:
        lines.append(
            f"|{r.get('rank', '-')}|`{r['candidate'][2:]}`|{r.get('robust_score', 0):.2f}|"
            f"{pct(r['total_return'])}|{pct(r['max_drawdown'])}|{r['sharpe']:.2f}|"
            f"{pct(r['first_half_return'])}|{pct(r['second_half_return'])}|{r['trade_count']}|"
        )

    lines += ["", "## 参数敏感性（网格组平均）", ""]
    for key, groups in sensitivity.items():
        lines += [f"### `{key}`", "", "|取值|组数|平均稳健分|平均收益|平均回撤|平均夏普|",
                  "|---|---:|---:|---:|---:|---:|"]
        for g in groups:
            lines.append(
                f"|{g['value']}|{g['count']}|{g['avg_score']:.2f}|{pct(g['avg_return'])}|"
                f"{pct(g['avg_drawdown'])}|{g['avg_sharpe']:.2f}|"
            )
        lines.append("")

    lines += [
        "## 结论与边界",
        "",
        "- 本次区间只有约 13 个月，且当前监控池带有事后选择偏差；Top5 是这份样本中的稳健候选，不是未来收益保证。",
        "- 前后半段收益都纳入排名，可抑制单一阶段暴涨造成的虚假最优，但仍需做滚动样本外与参数扰动验证。",
        "- 数据库保存了每组完整成交、净值、参数、费用、滑点、覆盖率和行情快照；本文件只保留便于审阅的汇总。",
    ]
    output = path.with_suffix(".md")
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("summary", type=Path)
    print(analyze(parser.parse_args().summary))
