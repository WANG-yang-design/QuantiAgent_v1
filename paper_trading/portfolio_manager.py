# -*- coding: utf-8 -*-
"""
组合管理器
==========
目标权重 → 调仓订单意图(交易员 Agent 引用), 支持 T+1 可用数量约束。
"""
import logging
from typing import Any, Dict, List, Optional

from paper_trading.paper_account import PaperAccount

logger = logging.getLogger("paper.portfolio")


class PortfolioManager:
    """组合管理: 目标权重→订单意图。"""

    def __init__(self, account: PaperAccount):
        self.account = account

    def total_asset(self) -> float:
        return self.account.get_snapshot()["total_asset"]

    def generate_rebalance_orders(self, target_weights: Dict[str, float],
                                  prices: Dict[str, float],
                                  name_map: Optional[Dict[str, str]] = None) -> List[dict]:
        """
        根据目标权重生成调仓订单意图(整数100股单位)。
        返回: [{symbol, side, qty, price, reason}]
        """
        total = self.total_asset()
        positions = {p["symbol"]: p for p in self.account.get_positions()}
        name_map = name_map or {}
        orders: List[dict] = []

        for symbol, weight in target_weights.items():
            price = prices.get(symbol, 0)
            if price <= 0:
                continue
            target_value = total * weight
            target_qty = int(target_value / price // 100 * 100)
            cur = positions.get(symbol)
            cur_qty = cur["total_qty"] if cur else 0
            diff = target_qty - cur_qty
            if diff >= 100:
                orders.append({
                    "symbol": symbol, "side": "BUY", "qty": diff,
                    "price": price, "reason": f"调仓至目标权重{weight:.0%}",
                    "name": (name_map.get(symbol) or cur.get("name") if cur else name_map.get(symbol, "")) or "",
                })
            elif diff <= -100:
                # 卖出不能超过可用数量(T+1)
                avail = cur["available_qty"] if cur else 0
                sell_qty = min(-diff, avail)
                if sell_qty >= 100:
                    orders.append({
                        "symbol": symbol, "side": "SELL", "qty": sell_qty,
                        "price": price, "reason": f"调仓至目标权重{weight:.0%}",
                        "name": cur.get("name", "") if cur else "",
                    })
        return orders
