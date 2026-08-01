# -*- coding: utf-8 -*-
"""
持仓管理器
==========
T+1 可用数量、成本、市值查询与调仓辅助。
"""
import logging
from typing import Any, Dict, List, Optional

from paper_trading.paper_account import PaperAccount

logger = logging.getLogger("paper.position")


class PositionManager:
    """持仓管理: 提供 Agent 需要的持仓视图。"""

    def __init__(self, account: PaperAccount):
        self.account = account

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        return self.account.get_position(symbol)

    def get_available_qty(self, symbol: str) -> int:
        """T+1 可用数量(今日买入不可卖)。"""
        return self.account.get_available_qty(symbol)

    def holding_summary(self) -> str:
        """持仓摘要文本(供交易员 Agent 摘要使用)。"""
        positions = self.account.get_positions()
        if not positions:
            return "当前无持仓"
        lines = ["当前持仓:"]
        for p in positions:
            lines.append(
                f"{p['symbol']} {p['name']} {p['total_qty']}股(可用{p['available_qty']}) "
                f"成本{p['cost_price']:.3f} 现价{p['latest_price']:.3f} 浮盈{p['pnl_pct']:+.2%}")
        return "；".join(lines)
