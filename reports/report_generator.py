# -*- coding: utf-8 -*-
"""
报告生成器 / 图表生成器
=======================
- 日报(HTML+MD) / 周报 / 月报 / 回测报告
- 图表: 净值曲线/回撤曲线/月度收益热力图
"""
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import matplotlib
matplotlib.use("Agg")   # 无界面后端(服务端绘图)
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd

# 中文字体(Windows)
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

from core.config import ROOT_DIR
from database import repository as repo

logger = logging.getLogger("reports")


class ChartGenerator:
    """图表生成: 保存 PNG 到 data/charts/。"""

    def __init__(self):
        self.out_dir = ROOT_DIR / "data" / "charts"
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def equity_curve(self, equity: List[float], dates: List[str],
                     benchmark: Optional[List[float]] = None,
                     title: str = "净值曲线") -> str:
        """净值曲线+基准对比。返回文件路径。"""
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.plot(dates, equity, label="策略净值", linewidth=1.5)
        if benchmark and len(benchmark) == len(equity):
            ax.plot(dates, benchmark, label="基准", linewidth=1.2, alpha=0.7)
        ax.set_title(title)
        ax.legend()
        ax.grid(alpha=0.3)
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        fig.autofmt_xdate()
        path = self.out_dir / f"{datetime.now():%Y%m%d%H%M%S}_equity.png"
        fig.savefig(path, dpi=100, bbox_inches="tight")
        plt.close(fig)
        return str(path)

    def drawdown_curve(self, drawdown: List[float], dates: List[str]) -> str:
        fig, ax = plt.subplots(figsize=(10, 3.5))
        ax.fill_between(dates, [d * 100 for d in drawdown], 0, alpha=0.4, color="#c0392b")
        ax.set_title("回撤曲线")
        ax.grid(alpha=0.3)
        fig.autofmt_xdate()
        path = self.out_dir / f"{datetime.now():%Y%m%d%H%M%S}_dd.png"
        fig.savefig(path, dpi=100, bbox_inches="tight")
        plt.close(fig)
        return str(path)


class ReportGenerator:
    """报告生成: MD + HTML。"""

    def __init__(self):
        self.charts = ChartGenerator()
        self.dir = ROOT_DIR / "reports"
        self.dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    def generate_daily_report(self, stats: Dict[str, Any], review: Dict[str, Any],
                              date_: Optional[date] = None) -> str:
        """日终复盘日报 → reports/daily_YYYYMMDD.md。"""
        date_ = date_ or date.today()
        md = [
            f"# 量化交易日报 {date_}",
            "",
            "## 账户概览",
            f"- 总资产: ¥{stats.get('total_asset', 0):,.2f}",
            f"- 当日盈亏: ¥{stats.get('day_pnl', 0):+,.2f}",
            f"- 成交笔数: {stats.get('trade_count', 0)}",
            f"- 手续费: ¥{stats.get('fee_total', 0):.2f}",
            "",
            "## 复盘总结",
            f"{review.get('review_summary', '')}",
            "",
            "## 改进建议",
        ]
        for i in review.get("improvement", []) or []:
            md.append(f"- {i}")
        md += ["", "## 持仓", ""]
        for p in stats.get("positions", []) or []:
            md.append(f"- {p.get('symbol')} {p.get('name', '')}: "
                      f"{p.get('total_qty', 0)}份 成本{p.get('cost_price', 0):.3f} "
                      f"浮盈{p.get('pnl_pct', 0):+.2%}")

        path = self.dir / f"daily_{date_:%Y%m%d}.md"
        path.write_text("\n".join(md), encoding="utf-8")
        repo.save_report({
            "report_type": "daily", "title": f"日报 {date_}",
            "file_path": str(path), "summary": review.get("review_summary", ""),
        })
        return str(path)

    # ------------------------------------------------------------------
    def generate_backtest_report(self, metrics: Dict[str, Any],
                                 charts: bool = True) -> str:
        """回测报告 → reports/backtest_<run_id>.md (+图表)。"""
        run_id = metrics.get("run_id", datetime.now().strftime("%H%M%S"))
        eq = metrics.get("equity_curve", [])
        dd = metrics.get("drawdown_curve", [])
        dates = [str(i) for i in range(len(eq))] if eq else []
        chart_lines = []
        if charts and eq:
            path = self.charts.equity_curve(
                eq, dates,
                benchmark=metrics.get("benchmark", {}).get("benchmark_return")
                and None,
                title=f"回测净值 {run_id}")
            chart_lines.append(f"![净值曲线]({path})")
            if dd:
                path2 = self.charts.drawdown_curve(dd, dates)
                chart_lines.append(f"![回撤曲线]({path2})")

        b = metrics.get("benchmark", {}) or {}
        md = [
            f"# 回测报告 {run_id}",
            "",
            "## 核心指标",
            f"- 总收益: {metrics.get('total_return', 0):+.2%}",
            f"- 年化收益: {metrics.get('annual_return', 0):+.2%}",
            f"- 最大回撤: {metrics.get('max_drawdown', 0):.2%}",
            f"- 夏普比率: {metrics.get('sharpe', 0):.2f}",
            f"- 卡玛比率: {metrics.get('calmar', 0):.2f}",
            f"- 胜率: {metrics.get('win_rate', 0):.1%}",
            f"- 盈亏比: {metrics.get('profit_factor', '-')}",
            f"- 交易次数: {metrics.get('trade_count', 0)}",
            f"- 平均持仓天数: {metrics.get('avg_hold_days', 0):.1f}",
            f"- 最大连续亏损: {metrics.get('max_consecutive_loss', 0)}次",
            f"- 手续费: ¥{metrics.get('fee_total', 0):.2f}",
            f"- 滑点成本: ¥{metrics.get('slippage_total', 0):.2f}",
            "",
            "## 基准对比",
            f"- 基准收益: {b.get('benchmark_return', 0):+.2%}",
            f"- 超额收益: {b.get('excess_return', 0):+.2%}",
            f"- 信息比率: {b.get('information_ratio', 0):.2f}",
            f"- 追踪误差: {b.get('tracking_error', 0):.2%}",
            "",
            "## 交易明细",
        ]
        for t in metrics.get("trade_details", [])[:100]:
            md.append(f"- [{t.get('date')}] {t.get('side')} {t.get('symbol')} "
                      f"{t.get('qty')}份 @ {t.get('price', 0):.3f} "
                      f"盈亏{t.get('pnl', 0):+.2f}")
        md += ["", "## 图表", ""] + chart_lines

        path = self.dir / f"backtest_{run_id}.md"
        path.write_text("\n".join(md), encoding="utf-8")
        repo.save_report({
            "report_type": "backtest", "title": f"回测报告 {run_id}",
            "file_path": str(path),
            "summary": f"总收益{metrics.get('total_return', 0):+.2%}",
        })
        return str(path)

    # ------------------------------------------------------------------
    def generate_weekly_report(self) -> str:
        """周报(简化: 一周账户/交易统计)。"""
        today = date.today()
        week_start = today - __import__("datetime").timedelta(days=today.weekday())
        trades = repo.get_trades(start=__import__("datetime").datetime.combine(
            week_start, __import__("datetime").datetime.min.time()))
        pnl = sum(t.fee for t in trades) * -1
        md = [
            f"# 量化交易周报 (第{today.isocalendar()[1]}周)",
            f"## 本周成交 {len(trades)} 笔",
        ]
        for t in trades[-50:]:
            md.append(f"- [{t.trade_time:%m-%d}] {t.side} {t.symbol} {t.qty}份 @ {t.price:.3f}")
        path = self.dir / f"weekly_{today:%Y%m%d}.md"
        path.write_text("\n".join(md), encoding="utf-8")
        repo.save_report({"report_type": "weekly", "title": f"周报 {today}",
                          "file_path": str(path), "summary": f"成交{len(trades)}笔"})
        return str(path)


_generator: Optional[ReportGenerator] = None


def get_report_generator() -> ReportGenerator:
    global _generator
    if _generator is None:
        _generator = ReportGenerator()
    return _generator
