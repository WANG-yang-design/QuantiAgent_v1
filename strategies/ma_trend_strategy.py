# -*- coding: utf-8 -*-
"""
均线趋势策略 / 信号聚合器
=========================
- MA趋势: 价格站上MA20且MA20向上 → 偏多参考
- 信号聚合: 汇总多策略信号供 Agent 参考
(网格策略已独立到 grid_strategy.py)
"""
from typing import Any, Dict, List, Optional

from strategies.base_strategy import BaseStrategy


class MaTrendStrategy(BaseStrategy):
    """均线趋势策略: 站上MA20+MA20斜率>0 → BUY候选。"""

    strategy_id = "ma_trend"

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        super().__init__(params or {})

    def generate_signals(self, universe: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        signals = []
        for item in universe:
            f = item.get("features") or {}
            if not f:
                continue
            above_ma20 = bool(f.get("price_above_ma20"))
            bull = bool(f.get("bull_align"))
            if above_ma20 and bull:
                signals.append({
                    "strategy_id": self.strategy_id,
                    "symbol": item["symbol"],
                    "signal": "BUY",
                    "score": 0.7,
                    "reason": "价格站上MA20且均线多头排列",
                })
            elif bool(f.get("bear_align")):
                signals.append({
                    "strategy_id": self.strategy_id,
                    "symbol": item["symbol"],
                    "signal": "SELL",
                    "score": 0.3,
                    "reason": "均线空头排列",
                })
            else:
                signals.append({
                    "strategy_id": self.strategy_id,
                    "symbol": item["symbol"],
                    "signal": "HOLD",
                    "score": 0.5,
                    "reason": "均线缠绕, 方向不明",
                })
        return signals


class SignalAggregator:
    """信号聚合: 多策略信号按 symbol 汇总, 提供统一的参考信号视图。"""

    def aggregate(self, signals_by_strategy: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for strategy_id, signals in signals_by_strategy.items():
            for sig in signals:
                sym = sig["symbol"]
                agg = out.setdefault(sym, {
                    "symbol": sym, "signals": [], "avg_score": 0.0, "best": "HOLD",
                })
                agg["signals"].append({"strategy_id": strategy_id,
                                       "signal": sig["signal"], "score": sig["score"],
                                       "reason": sig.get("reason", "")})
                agg["avg_score"] = sum(s["score"] for s in agg["signals"]) / len(agg["signals"])
                agg["best"] = max(agg["signals"], key=lambda s: s["score"])["signal"]
        return out
