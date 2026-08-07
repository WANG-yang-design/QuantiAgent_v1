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
        修复: ①目标组合外的持仓 → 清仓(原实现只遍历 target_weights, 被剔除的持仓永远不卖);
              ②BUY 总量受可用现金约束(防超买)。
        """
        total = self.total_asset()
        positions = {p["symbol"]: p for p in self.account.get_positions()}
        name_map = name_map or {}
        orders: List[dict] = []
        available_cash = self.account.get_available_cash()

        # 目标外持仓清仓(轮动/减仓场景: 从目标组合移除的标的必须能卖出)
        for symbol, cur in positions.items():
            if symbol not in target_weights and cur["total_qty"] > 0:
                avail = cur["available_qty"]
                if avail >= 100:
                    price = prices.get(symbol) or cur.get("latest_price") or 0
                    if price > 0:
                        orders.append({
                            "symbol": symbol, "side": "SELL", "qty": avail,
                            "price": price,
                            "reason": "目标组合外持仓, 清仓",
                            "name": cur.get("name", "") or name_map.get(symbol, ""),
                        })

        # 目标组合内调仓
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
                # 现金校验: 累计买入金额不能超过可用现金
                amount = diff * price
                if amount > available_cash:
                    diff = int(available_cash / price // 100 * 100)
                    if diff < 100:
                        continue
                available_cash -= diff * price
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
