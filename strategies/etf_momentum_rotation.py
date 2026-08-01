# -*- coding: utf-8 -*-
"""
ETF 动量轮动策略 (V1 核心参考策略)
==================================
按 20 日动量排名, 结合流动性过滤(成交额/波动率), 输出 top 排名。
信号只作为 Agent 参考, 最终决策由多 Agent 工作流完成。
"""
import logging
from typing import Any, Dict, List, Optional

from strategies.base_strategy import BaseStrategy

logger = logging.getLogger("strategy.momentum")


class EtfMomentumRotationStrategy(BaseStrategy):
    """ETF动量轮动: 动量排名 + 流动性/波动率过滤。"""

    strategy_id = "etf_momentum_rotation"

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        super().__init__(params or {})
        self.top_n = int(self.params.get("top_n", 3))
        self.mom_window = int(self.params.get("mom_window", 20))
        self.min_amount = float(self.params.get("min_amount", 3e7))     # 3000万
        self.max_vol = float(self.params.get("max_vol", 0.50))          # 年化波动率上限(极端行情放开, 风控层仍会拦截)

    def generate_signals(self, universe: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        candidates = []
        for item in universe:
            f = item.get("features") or {}
            symbol = item["symbol"]
            amount_ma = float(f.get("amount_ma20", 0) or 0)
            vol = float(f.get("volatility_20d", 0) or 0)
            mom = float(f.get(f"momentum_{self.mom_window}d", 0) or 0)
            # 流动性过滤
            if amount_ma < self.min_amount:
                continue
            if vol > self.max_vol:
                continue
            candidates.append({"symbol": symbol, "name": item.get("name", symbol),
                               "momentum": mom, "amount": amount_ma,
                               "score": mom})

        candidates.sort(key=lambda x: x["momentum"], reverse=True)
        signals = []
        now = None
        for i, c in enumerate(candidates[:self.top_n]):
            signals.append({
                "strategy_id": self.strategy_id,
                "symbol": c["symbol"],
                "signal": "BUY",
                "score": c["momentum"],
                "reason": f"动量排名第{i+1}(20日动量{c['momentum']:+.2%}, "
                          f"20日均额{c['amount']/1e4:.0f}万)",
            })
        return signals
