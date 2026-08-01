# -*- coding: utf-8 -*-
"""
回测指标计算 (文档 10.3: 19项 + 文档22.1改进: 基准对比/超额收益)
==============================================================
总收益/年化/最大回撤/夏普/卡玛/胜率/盈亏比/换手率/交易次数/平均持仓天数/
最大连续亏损/手续费/滑点/月度收益/持仓曲线/净值曲线/回撤曲线/交易明细/
Agent建议对比 + 基准超额/信息比率/追踪误差
"""
import math
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd


def compute_metrics(equity_curve: List[float],
                    trade_details: List[Dict[str, Any]],
                    benchmark_curve: Optional[List[float]] = None,
                    trading_days: int = 252) -> Dict[str, Any]:
    """计算回测指标集。equity_curve: 每日净值序列(含初始)。"""
    eq = pd.Series(equity_curve, dtype=float)
    if len(eq) < 2:
        return {"error": "净值序列不足"}

    total_return = eq.iloc[-1] / eq.iloc[0] - 1
    n_days = max(len(eq) - 1, 1)
    annual_return = (1 + total_return) ** (252 / n_days) - 1 if total_return > -1 else -1.0

    # 最大回撤
    peak = eq.cummax()
    drawdown = eq / peak - 1
    max_drawdown = float(drawdown.min())

    # 日收益率
    ret = eq.pct_change().dropna()
    if ret.std() > 0:
        sharpe = float(ret.mean() / ret.std() * math.sqrt(252))
    else:
        sharpe = 0.0
    calmar = float(annual_return / abs(max_drawdown)) if max_drawdown < 0 else 0.0

    # 交易统计
    wins = [t for t in trade_details if t.get("pnl", 0) > 0]
    losses = [t for t in trade_details if t.get("pnl", 0) <= 0]
    win_rate = len(wins) / len(trade_details) if trade_details else 0.0
    avg_win = np.mean([t["pnl"] for t in wins]) if wins else 0.0
    avg_loss = abs(np.mean([t["pnl"] for t in losses])) if losses else 0.0
    profit_factor = float(avg_win / avg_loss) if avg_loss > 0 else float("inf")

    # 连续亏损次数
    max_consec_loss, cur = 0, 0
    for t in trade_details:
        if t.get("pnl", 0) <= 0:
            cur += 1
            max_consec_loss = max(max_consec_loss, cur)
        else:
            cur = 0

    # 换手率与持仓天数
    turnover = sum(t.get("amount", 0) for t in trade_details if t.get("side") == "BUY") / max(eq.iloc[0], 1)
    hold_days = [t.get("hold_days", 0) for t in trade_details]
    avg_hold_days = float(np.mean(hold_days)) if hold_days else 0.0

    # 手续费/滑点
    fee_total = sum(t.get("fee", 0) for t in trade_details)
    slip_total = sum(t.get("slippage_cost", 0) for t in trade_details)

    # 月度收益
    if len(eq) >= 2:
        dates = pd.date_range("2020-01-01", periods=len(eq), freq="B")
        monthly = pd.Series(eq.values).groupby(dates.month).last().pct_change().dropna().to_dict()
    else:
        monthly = {}

    # 基准对比
    benchmark = {}
    if benchmark_curve and len(benchmark_curve) == len(eq):
        b_eq = pd.Series(benchmark_curve, dtype=float)
        b_ret = total_return - (b_eq.iloc[-1] / b_eq.iloc[0] - 1)   # 超额收益
        b_daily = b_eq.pct_change().dropna()
        # 信息比率
        diff = ret - b_daily.reindex(ret.index).fillna(0)
        ir = float(diff.mean() / diff.std() * math.sqrt(252)) if diff.std() > 0 else 0.0
        te = float(diff.std() * math.sqrt(252))
        benchmark = {
            "benchmark_return": float(b_eq.iloc[-1] / b_eq.iloc[0] - 1),
            "excess_return": float(b_ret),
            "information_ratio": ir,
            "tracking_error": te,
        }

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
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 4) if profit_factor != float("inf") else None,
        "turnover": round(turnover, 4),
        "trade_count": len(trade_details),
        "avg_hold_days": round(avg_hold_days, 2),
        "max_consecutive_loss": max_consec_loss,
        "fee_total": round(fee_total, 2),
        "slippage_total": round(slip_total, 2),
        "monthly_returns": {str(k): round(v, 4) for k, v in monthly.items()},
        "equity_curve": eq.round(4).tolist(),
        "drawdown_curve": drawdown.round(4).tolist(),
        "trade_details": trade_details,
        "benchmark": benchmark,
    }
