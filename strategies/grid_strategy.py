# -*- coding: utf-8 -*-
"""
网格策略 (独立模块, 与 ma_trend_strategy 分离)
============================================
支撑压力区间高抛低吸参考信号(仅参考, 决策在Agent层)。
"""
from typing import Any, Dict, List, Optional

from strategies.base_strategy import BaseStrategy


class GridStrategy(BaseStrategy):
    """网格策略(简化): 靠近支撑做多参考, 靠近压力减仓参考。"""

    strategy_id = "grid"

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        super().__init__(params or {})
        self.ratio = float((params or {}).get("ratio", 0.02))   # 网格宽度2%

    def generate_signals(self, universe: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        signals = []
        for item in universe:
            f = item.get("features") or {}
            if not f:
                continue
            close = float(f.get("close", 0) or 0)
            support = float(f.get("support_20d", 0) or 0)
            resistance = float(f.get("resistance_20d", 0) or 0)
            if close and support and (close - support) / support <= self.ratio:
                signals.append({
                    "strategy_id": self.strategy_id,
                    "symbol": item["symbol"],
                    "signal": "BUY",
                    "score": 0.6,
                    "reason": f"价格贴近20日支撑({support:.3f}), 网格买入参考",
                })
            elif resistance and close and (resistance - close) / close <= self.ratio:
                signals.append({
                    "strategy_id": self.strategy_id,
                    "symbol": item["symbol"],
                    "signal": "SELL",
                    "score": 0.6,
                    "reason": f"价格贴近20日压力({resistance:.3f}), 网格减仓参考",
                })
        return signals
