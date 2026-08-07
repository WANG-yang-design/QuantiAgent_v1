# -*- coding: utf-8 -*-
"""
报告生成器 / 图表生成器
=======================
- 日报(HTML+MD) / 周报 / 月报 / 回测报告
- 图表: 净值曲线/回撤曲线/月度收益热力图
"""
import logging
import os
from datetime import date, datetime, timedelta
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
    """报告生成: MD + HTML。
    修复: 原实现周报只有简单成交列表且盈亏计算错误(手续费取负),
    无月报/年报/基准对比/单标的统计 —— 用户无法复盘账户表现。
    现在日报/周报/月报/年报统一包含: 账户概览、净值vs沪深300、回撤、
    单标的统计、交易明细。"""

    def __init__(self):
        self.charts = ChartGenerator()
        self.dir = ROOT_DIR / "reports"
        self.dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 公共统计(账户/基准/单标的)
    # ------------------------------------------------------------------
    def _benchmark_curve(self, start: date, end: date):
        """沪深300买入持有净值(区间首日收盘归一为1)。DB日K优先, 缺失用实时源。
        进程内缓存: 网络源拉取一次约30秒, 报告/分析页高频调用不能反复拉。"""
        cache = getattr(self, "_bench_cache", {})
        key = (str(start), str(end))
        if key in cache:
            return cache[key]
        try:
            rows = repo.get_daily_bars("000300", start, end)
        except Exception:
            rows = []
        if not rows:
            try:
                from data_service.market_data_service import get_market_service
                bars = get_market_service().get_index_bars("000300", start, end)
                rows = [type("B", (), {"trade_date": b["trade_date"], "close": b["close"]})()
                        for b in bars]
            except Exception:
                rows = []
        closes = [(str(r.trade_date)[:10], float(r.close or 0)) for r in rows
                  if (r.close or 0) > 0]
        if len(closes) < 2:
            return None, None
        base = closes[0][1]
        result = ([c / base for _, c in closes], [d for d, _ in closes])
        cache[key] = result
        if len(cache) > 32:
            cache.clear()
        return result

    def _symbol_stats(self, start_dt, end_dt, account_id: str = "PA-001",
                      total_asset: float = 0.0) -> List[Dict[str, Any]]:
        """区间单标的统计: 交易次数/买卖笔数/已实现盈亏/当前持仓/区间涨跌幅。"""
        from core.symbol_names import resolve_symbol_name
        trades = repo.get_trades(start=start_dt, end=end_dt,
                                 account_id=account_id)
        by: Dict[str, Dict[str, Any]] = {}
        for t in trades:
            st = by.setdefault(t.symbol, {
                "symbol": t.symbol, "name": t.name or resolve_symbol_name(t.symbol),
                "buy_count": 0, "sell_count": 0, "trade_count": 0,
                "buy_amount": 0.0, "sell_amount": 0.0,
                "realized_pnl": 0.0, "fee": 0.0, "wins": 0, "losses": 0,
            })
            st["trade_count"] += 1
            st["fee"] += float(t.fee or 0)
            if t.side == "BUY":
                st["buy_count"] += 1
                st["buy_amount"] += float(t.price or 0) * int(t.qty or 0)
            else:
                st["sell_count"] += 1
                st["sell_amount"] += float(t.price or 0) * int(t.qty or 0)
                pnl = t.pnl
                if pnl is not None:
                    st["realized_pnl"] += float(pnl)
                    if pnl > 0:
                        st["wins"] += 1
                    elif pnl < 0:
                        st["losses"] += 1
        # 当前持仓(接口层附加) + 区间涨跌幅
        pos_map = {}
        try:
            pos_map = {p.symbol: p for p in repo.get_positions(account_id)}
        except Exception:
            pass
        start = start_dt.date()
        end = end_dt.date()
        for sym, st in by.items():
            p = pos_map.get(sym)
            if p:
                st["position"] = {
                    "total_qty": p.total_qty, "available_qty": p.available_qty,
                    "cost_price": round(float(p.cost_price or 0), 4),
                    "latest_price": round(float(p.latest_price or 0), 4),
                    "pnl": round(float(p.pnl or 0), 2),
                    "pnl_pct": round(float(p.pnl_pct or 0), 4),
                }
            else:
                st["position"] = None
            try:
                bars = repo.get_daily_bars(sym, start, end)
                closes = [float(b.close or 0) for b in bars if (b.close or 0) > 0]
                if len(closes) >= 2:
                    st["price_return"] = round(closes[-1] / closes[0] - 1, 4)
                else:
                    st["price_return"] = None
            except Exception:
                st["price_return"] = None
        # 市值占比(需要总资产)
        if total_asset > 0:
            for st in by.values():
                if st.get("position"):
                    st["weight"] = round(
                        st["position"]["total_qty"] * st["position"]["latest_price"]
                        / total_asset, 4)
        return sorted(by.values(), key=lambda x: x["realized_pnl"], reverse=True)

    @staticmethod
    def _period_bounds(period: str, today: Optional[date] = None) -> tuple:
        """period: day/week/month/year → (start_date, end_date)"""
        today = today or date.today()
        if period == "day":
            return today, today
        if period == "week":
            return today - timedelta(days=today.weekday()), today
        if period == "month":
            return today.replace(day=1), today
        if period == "year":
            return today.replace(month=1, day=1), today
        raise ValueError(f"未知周期: {period}")

    # ------------------------------------------------------------------
    def _account_snapshot_stats(self, start: date, end: date, account_id: str = "PA-001"):
        """区间账户统计: 期初/期末总资产、最大回撤。"""
        snaps = repo.get_account_snapshots(account_id=account_id, limit=100000)
        before = [s for s in snaps if s.snapshot_time.date() < start]
        in_range = [s for s in snaps if start <= s.snapshot_time.date() <= end]
        start_asset = float(before[-1].total_asset) if before else None
        if start_asset is None and in_range:
            start_asset = float(in_range[0].total_asset)
        end_asset = float(in_range[-1].total_asset) if in_range else None
        peak = 0.0
        max_dd = 0.0
        for s in in_range:
            v = float(s.total_asset or 0)
            peak = max(peak, v)
            if peak > 0:
                max_dd = max(max_dd, (peak - v) / peak)
        return {
            "start_asset": start_asset,
            "end_asset": end_asset,
            "max_drawdown": round(max_dd, 4),
        }

    # ------------------------------------------------------------------
    def _generate_period_report(self, period: str, title: str,
                                account_id: str = "PA-001",
                                start: Optional[date] = None,
                                end: Optional[date] = None) -> str:
        """周报/月报/年报通用模板。start/end 缺省时按 period 取自然周期。"""
        today = date.today()
        if start is None or end is None:
            start, end = self._period_bounds(period, today)
        start_dt = datetime.combine(start, datetime.min.time())
        end_dt = datetime.combine(end, datetime.max.time())
        trades = repo.get_trades(start=start_dt, end=end_dt, account_id=account_id)
        fee_total = sum(float(t.fee or 0) for t in trades)
        realized = sum(float(t.pnl or 0) for t in trades if t.pnl is not None)
        wins = sum(1 for t in trades if t.pnl is not None and t.pnl > 0)
        losses = sum(1 for t in trades if t.pnl is not None and t.pnl < 0)
        positions = repo.get_positions(account_id)
        mv = sum(int(p.total_qty or 0) * float(p.latest_price or 0) for p in positions)
        acc = repo.get_account(account_id)
        total_asset = float((acc.cash or 0) + (acc.frozen_cash or 0) + mv) if acc else 0.0
        total_pnl = total_asset - float(acc.init_cash or 0) if acc else 0.0
        snap = self._account_snapshot_stats(start, end, account_id)
        bench_curve, bench_dates = self._benchmark_curve(start, end)
        bench_ret = (bench_curve[-1] - 1) if bench_curve else None
        stats = self._symbol_stats(start_dt, end_dt, account_id, total_asset)

        md = [
            f"# {title} ({start} ~ {end})",
            "",
            "## 账户概览",
            f"- 总资产: ¥{total_asset:,.2f}",
            f"- 累计盈亏: ¥{total_pnl:+,.2f}",
            f"- 区间成交: {len(trades)} 笔 (胜 {wins} / 负 {losses})",
            f"- 区间已实现盈亏: ¥{realized:+,.2f}",
            f"- 手续费: ¥{fee_total:.2f}",
            f"- 最大回撤: {snap.get('max_drawdown', 0):.2%}",
        ]
        if bench_ret is not None:
            md.append(f"- 沪深300同期: {bench_ret:+.2%}")
            if snap.get("start_asset"):
                period_ret = total_asset / float(snap["start_asset"]) - 1
                md.append(f"- 区间收益: {period_ret:+.2%} / 超额: {period_ret - bench_ret:+.2%}")
        md += ["", "## 单标的统计", ""]
        if stats:
            md.append("| 标的 | 名称 | 买卖(次) | 已实现盈亏 | 手续费 | 当前持仓 | 区间涨跌幅 |")
            md.append("| --- | --- | --- | --- | --- | --- | --- |")
            for st in stats:
                pos = st.get("position")
                pos_txt = (f"{pos['total_qty']}份 盈亏{pos['pnl']:+.2f}"
                           if pos else "已清仓")
                ret = f"{st['price_return']:+.2%}" if st.get("price_return") is not None else "-"
                md.append(f"| {st['symbol']} | {st['name']} | "
                          f"{st['buy_count']}/{st['sell_count']} | "
                          f"{st['realized_pnl']:+.2f} | {st['fee']:.2f} | "
                          f"{pos_txt} | {ret} |")
        else:
            md.append("区间内无成交")
        md += ["", "## 交易明细", ""]
        for t in trades[-100:]:
            pnl_txt = f"盈亏{t.pnl:+.2f}" if t.pnl is not None else ""
            md.append(f"- [{t.trade_time:%m-%d %H:%M}] {t.side} {t.symbol} "
                      f"{t.name or ''} {t.qty}份 @ {t.price:.3f} {pnl_txt}")
        # 图表
        chart_lines = []
        try:
            snap_rows = repo.get_account_snapshots(account_id=account_id, limit=100000)
            in_snap = [s for s in snap_rows if start <= s.snapshot_time.date() <= end]
            if len(in_snap) >= 2:
                eq = [float(s.total_asset or 0) for s in in_snap]
                eq = [v / eq[0] for v in eq]
                dd = [(max(eq[:i + 1]) - eq[i]) / max(eq[:i + 1])
                      if max(eq[:i + 1]) else 0 for i in range(len(eq))]
                p = self.charts.equity_curve(
                    eq, [str(s.snapshot_time.date()) for s in in_snap],
                    benchmark=bench_curve if bench_curve and len(bench_curve) == len(eq) else None,
                    title=f"{title} 净值")
                chart_lines.append(f"![净值曲线]({p})")
                if any(d > 0.001 for d in dd):
                    p2 = self.charts.drawdown_curve(
                        dd, [str(s.snapshot_time.date()) for s in in_snap])
                    chart_lines.append(f"![回撤曲线]({p2})")
        except Exception as exc:
            logger.warning("报告图表生成失败: %s", exc)
        md += ["", "## 图表", ""] + chart_lines

        fname = f"{period}_{end:%Y%m%d}.md"
        if period == "month":
            fname = f"monthly_{end:%Y%m}.md"
        elif period == "year":
            fname = f"annual_{end:%Y}.md"
        path = self.dir / fname
        path.write_text("\n".join(md), encoding="utf-8")
        repo.save_report({
            "report_type": period, "title": title,
            "file_path": str(path),
            "summary": f"成交{len(trades)}笔 已实现{realized:+.2f} "
                       f"总资产{total_asset:,.0f}",
        })
        return str(path)

    # ------------------------------------------------------------------
    def generate_daily_report(self, stats: Dict[str, Any], review: Dict[str, Any],
                              date_: Optional[date] = None) -> str:
        """日终复盘日报 → reports/daily_YYYYMMDD.md。
        修复: 原日报无单标的统计/基准对比。"""
        date_ = date_ or date.today()
        start_dt = datetime.combine(date_, datetime.min.time())
        end_dt = datetime.combine(date_, datetime.max.time())
        account_id = stats.get("account_id", "PA-001")
        trades = repo.get_trades(start=start_dt, end=end_dt, account_id=account_id)
        realized = sum(float(t.pnl or 0) for t in trades if t.pnl is not None)
        fee_total = stats.get("fee_total", sum(float(t.fee or 0) for t in trades))
        sym_stats = self._symbol_stats(start_dt, end_dt, account_id)
        bench_curve, bench_dates = self._benchmark_curve(date_, date_)
        bench_ret = (bench_curve[-1] - 1) if bench_curve else None
        md = [
            f"# 量化交易日报 {date_}",
            "",
            "## 账户概览",
            f"- 总资产: ¥{stats.get('total_asset', 0):,.2f}",
            f"- 当日盈亏: ¥{stats.get('day_pnl', 0):+,.2f}",
            f"- 成交笔数: {stats.get('trade_count', 0)}",
            f"- 已实现盈亏: ¥{realized:+,.2f}",
            f"- 手续费: ¥{fee_total:.2f}",
        ]
        if bench_ret is not None:
            md.append(f"- 沪深300当日: {bench_ret:+.2%}")
        md += ["", "## 复盘总结", f"{review.get('review_summary', '')}", "", "## 改进建议"]
        for i in review.get("improvement", []) or []:
            md.append(f"- {i}")
        md += ["", "## 单标的统计", ""]
        if sym_stats:
            md.append("| 标的 | 名称 | 买卖(次) | 已实现盈亏 | 当前持仓 |")
            md.append("| --- | --- | --- | --- | --- |")
            for st in sym_stats:
                pos = st.get("position")
                pos_txt = (f"{pos['total_qty']}份" if pos else "已清仓")
                md.append(f"| {st['symbol']} | {st['name']} | "
                          f"{st['buy_count']}/{st['sell_count']} | "
                          f"{st['realized_pnl']:+.2f} | {pos_txt} |")
        md += ["", "## 成交明细", ""]
        for t in trades[-100:]:
            pnl_txt = f"盈亏{t.pnl:+.2f}" if t.pnl is not None else ""
            md.append(f"- [{t.trade_time:%H:%M}] {t.side} {t.symbol} "
                      f"{t.name or ''} {t.qty}份 @ {t.price:.3f} {pnl_txt}")
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
        dates = metrics.get("dates") or [str(i) for i in range(len(eq))]
        chart_lines = []
        if charts and eq:
            # 修复: 原实现 benchmark 参数恒为 None(`x and None` 表达式),
            # 基准曲线分支永不生效, 回测报告图表缺失基准线。
            bench_curve = metrics.get("benchmark_curve") or None
            path = self.charts.equity_curve(
                eq, dates,
                benchmark=bench_curve if bench_curve and len(bench_curve) == len(eq) else None,
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
    def generate_weekly_report(self, account_id: str = "PA-001") -> str:
        """周报(修复: 原实现盈亏=手续费取负, 且无单标的统计/基准对比)。"""
        return self._generate_period_report(
            "week", f"量化交易周报 (第{date.today().isocalendar()[1]}周)",
            account_id)

    # ------------------------------------------------------------------
    def generate_monthly_report(self, year: Optional[int] = None,
                                month: Optional[int] = None,
                                account_id: str = "PA-001") -> str:
        """月报: 当前月取月初至今, 历史月份取整月。"""
        today = date.today()
        year = year or today.year
        month = month or today.month
        start = date(year, month, 1)
        if (year, month) == (today.year, today.month):
            end = today
        else:
            end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        return self._generate_period_report(
            "month", f"量化交易月报 {year}年{month}月", account_id,
            start=start, end=end)

    # ------------------------------------------------------------------
    def generate_annual_report(self, year: Optional[int] = None,
                               account_id: str = "PA-001") -> str:
        """年报: 当前年取年初至今, 历史年份取整年。"""
        today = date.today()
        year = year or today.year
        start = date(year, 1, 1)
        end = today if year == today.year else date(year, 12, 31)
        return self._generate_period_report(
            "year", f"量化交易年报 {year}年", account_id, start=start, end=end)


_generator: Optional[ReportGenerator] = None


def get_report_generator() -> ReportGenerator:
    global _generator
    if _generator is None:
        _generator = ReportGenerator()
    return _generator
