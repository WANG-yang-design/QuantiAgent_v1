# -*- coding: utf-8 -*-
"""
回测指标计算 (文档 10.3: 19项 + 文档22.1改进: 基准对比/超额收益)
==============================================================
- 净值/回撤/持仓/月度收益全部使用真实回测日期(不再伪造)
- 胜率/平均持仓天数只统计已平仓(SELL)记录, 避免 BUY 记录污染分母
- 滑点成本 = 每股滑点 × 数量(总成本), 换手率 = 买入成交额 / 初始资金
"""
import math
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd


def compute_metrics(equity_curve: List[float],
                    trade_details: List[Dict[str, Any]],
                    benchmark_curve: Optional[List[float]] = None,
                    dates: Optional[List[str]] = None,
                    trading_days: int = 252) -> Dict[str, Any]:
    """
    计算回测指标集。
    equity_curve: 每日净值序列(含初始); dates: 与净值序列一一对应的日期字符串。
    """
    eq = pd.Series(equity_curve, dtype=float)
    if len(eq) < 2:
        return {"error": "净值序列不足"}
    n_days = max(len(eq) - 1, 1)

    total_return = eq.iloc[-1] / eq.iloc[0] - 1
    annual_return = (1 + total_return) ** (trading_days / n_days) - 1 if total_return > -1 else -1.0

    # 最大回撤
    peak = eq.cummax()
    drawdown = eq / peak - 1
    max_drawdown = float(drawdown.min())

    # 日收益率
    ret = eq.pct_change().dropna()
    sharpe = float(ret.mean() / ret.std() * math.sqrt(trading_days)) if ret.std() > 0 else 0.0
    calmar = float(annual_return / abs(max_drawdown)) if max_drawdown < 0 else 0.0

    # ---- 交易统计: 只统计已平仓(SELL)的完整交易 ----
    closed = [t for t in trade_details if t.get("side") == "SELL"]
    wins = [t for t in closed if t.get("pnl", 0) > 0]
    losses = [t for t in closed if t.get("pnl", 0) <= 0]
    win_rate = len(wins) / len(closed) if closed else None        # 无平仓记录 → None(前端显示样本不足)
    avg_win = np.mean([t["pnl"] for t in wins]) if wins else 0.0
    avg_loss = abs(np.mean([t["pnl"] for t in losses])) if losses else 0.0
    profit_factor = float(avg_win / avg_loss) if avg_loss > 0 else None

    # 连续亏损(按平仓记录)
    max_consec_loss, cur = 0, 0
    for t in closed:
        if t.get("pnl", 0) <= 0:
            cur += 1
            max_consec_loss = max(max_consec_loss, cur)
        else:
            cur = 0

    # 换手率(买入成交额/初始资金) 与 平均持仓天数(只统计平仓)
    buy_amount = sum(t.get("amount", 0) for t in trade_details if t.get("side") == "BUY")
    turnover = buy_amount / max(eq.iloc[0], 1)
    hold_days = [t.get("hold_days", 0) for t in closed]
    avg_hold_days = float(np.mean(hold_days)) if hold_days else 0.0

    # 手续费/滑点(总成本)
    fee_total = sum(t.get("fee", 0) for t in trade_details)
    slip_total = sum(t.get("slippage_cost", 0) for t in trade_details)

    # ---- 月度收益: 按真实日期分组(每自然月最后值 vs 上月末) ----
    monthly = {}
    if dates and len(dates) == len(eq):
        try:
            idx = pd.to_datetime([str(x)[:10] for x in dates])
            s = pd.Series(eq.values, index=idx)
            m = s.groupby([s.index.year, s.index.month]).last()
            m_ret = m.pct_change().dropna()
            monthly = {f"{y}-{mo:02d}": round(v, 4) for (y, mo), v in m_ret.items()}
        except Exception:
            monthly = {}

    # 基准对比
    benchmark = {}
    if benchmark_curve and len(benchmark_curve) == len(eq):
        b_eq = pd.Series(benchmark_curve, dtype=float)
        b_daily = b_eq.pct_change().dropna()
        diff = ret - b_daily.reindex(ret.index).fillna(0)
        ir = float(diff.mean() / diff.std() * math.sqrt(trading_days)) if diff.std() > 0 else 0.0
        benchmark = {
            "benchmark_return": round(float(b_eq.iloc[-1] / b_eq.iloc[0] - 1), 6),
            "excess_return": round(float(total_return - (b_eq.iloc[-1] / b_eq.iloc[0] - 1)), 6),
            "information_ratio": round(ir, 4),
            "tracking_error": round(float(diff.std() * math.sqrt(trading_days)), 6),
        }
        benchmark_curve_out = [round(float(v), 4) for v in benchmark_curve]
    else:
        benchmark_curve_out = None

    # 交易明细 JSON 序列化(日期转字符串)
    trade_details = [
        {k: (str(v) if isinstance(v, (date, datetime)) else v) for k, v in t.items()}
        for t in trade_details
    ]

    return {
        "total_return": round(total_return, 6),
        "annual_return": round(annual_return, 6),
        "max_drawdown": round(max_drawdown, 6),
        "sharpe": round(sharpe, 4),
        "calmar": round(calmar, 4),
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
        "turnover": round(turnover, 4),
        "trade_count": len(trade_details),
        "closed_trade_count": len(closed),
        "avg_hold_days": round(avg_hold_days, 2),
        "max_consecutive_loss": max_consec_loss,
        "fee_total": round(fee_total, 2),
        "slippage_total": round(slip_total, 2),
        "monthly_returns": monthly,
        "equity_curve": eq.round(4).tolist(),
        "drawdown_curve": drawdown.round(4).tolist(),
        "dates": [str(x)[:10] for x in dates] if dates else None,
        "benchmark_curve": benchmark_curve_out,
        "trade_details": trade_details,
        "benchmark": benchmark,
    }
