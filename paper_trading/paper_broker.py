# -*- coding: utf-8 -*-
"""
模拟券商 (PaperBroker)
======================
对外统一入口: 账户/持仓/委托/成交/下单/撤单/查询。
模拟盘与回测共用此接口, 换实盘时替换为 BrokerAdapter 实现。
"""
import logging
from typing import Any, Dict, List, Optional

from paper_trading.order_manager import OrderManager
from paper_trading.paper_account import PaperAccount
from paper_trading.position_manager import PositionManager
from paper_trading.portfolio_manager import PortfolioManager

logger = logging.getLogger("paper.broker")


class PaperBroker:
    """模拟券商: 组合账户+订单+持仓+组合管理。"""

    def __init__(self, account_id: str = "PA-001"):
        self.account_id = account_id
        self.account = PaperAccount(account_id)
        self.orders = OrderManager(self.account)
        self.positions = PositionManager(self.account)
        self.portfolio = PortfolioManager(self.account)

    # ---------------- 账户 ----------------
    def get_account(self) -> Dict[str, Any]:
        return self.account.get_snapshot()

    def get_balance(self) -> Dict[str, Any]:
        return self.account.get_snapshot()

    # ---------------- 持仓 ----------------
    def get_positions(self) -> List[Dict[str, Any]]:
        return self.account.get_positions()

    def get_available_quantity(self, symbol: str) -> int:
        return self.account.get_available_qty(symbol)

    # ---------------- 订单 ----------------
    def place_order(self, order_request: Dict[str, Any]) -> dict:
        """下单入口: 生成订单意图(幂等) → 提交。"""
        intent = order_request.get("order_intent_id")
        return self.orders.submit_order(
            symbol=order_request["symbol"],
            side=order_request["side"],
            qty=int(order_request["qty"]),
            order_type=order_request.get("order_type", "LIMIT"),
            price=float(order_request.get("price", 0)),
            order_intent_id=intent,
            plan_id=order_request.get("plan_id", ""),
            name=order_request.get("name", ""),
        )

    def cancel_order(self, order_id: str, reason: str = "manual") -> dict:
        return self.orders.cancel_order(order_id, reason)

    def query_order(self, order_id: str) -> dict:
        return self.orders.query_order(order_id)

    def get_orders(self, status: Optional[str] = None) -> List[dict]:
        return self.orders.list_orders(status)

    def get_trades(self, symbol: Optional[str] = None, limit: int = 200) -> List[dict]:
        from database import repository as repo
        return [
            {"trade_id": t.trade_id, "order_id": t.order_id, "symbol": t.symbol,
             "side": t.side, "price": t.price, "qty": t.qty, "fee": t.fee,
             "trade_time": str(t.trade_time)}
            for t in repo.get_trades(symbol=symbol, limit=limit)
        ]

    def match_order(self, order_id: str, bar: dict, mode: str = "simple",
                    slippage: float = 0.0005) -> List[dict]:
        """K线撮合(盘中/回测调用)。"""
        return self.orders.match_order(order_id, bar, slippage, mode)

    def cancel_stale_orders(self):
        return self.orders.cancel_stale_orders()

    # ---------------- 组合 ----------------
    def rebalance_plan(self, target_weights: Dict[str, float]) -> List[dict]:
        """根据目标权重生成调仓订单意图列表(交易员用)。"""
        return self.portfolio.generate_rebalance_orders(target_weights)

    def mark_to_market(self, prices: Dict[str, float]):
        """按最新价刷新持仓市值。"""
        self.account.update_prices(prices)

    def snapshot(self):
        self.account.snapshot_to_db()
