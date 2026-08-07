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
            source=order_request.get("source", "agent"),
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
             "name": t.name or "", "side": t.side, "price": t.price,
             "qty": t.qty, "fee": t.fee, "pnl": t.pnl,
             "trade_time": str(t.trade_time)}
            # 修复: 必须按账户过滤 —— 测试套件/其他账户的成交都在全局 trades 表
            for t in repo.get_trades(symbol=symbol, limit=limit,
                                     account_id=self.account_id)
        ]

    def match_order(self, order_id: str, bar: dict, mode: str = "simple",
                    slippage: float = 0.0005) -> List[dict]:
        """K线撮合(盘中/回测调用)。"""
        return self.orders.match_order(order_id, bar, slippage, mode)

    def cancel_stale_orders(self):
        return self.orders.cancel_stale_orders()

    # ---------------- 组合 ----------------
    def rebalance_plan(self, target_weights: Dict[str, float]) -> List[dict]:
        """根据目标权重生成调仓订单意图列表(交易员用)。
        修复: 原实现不传 prices(默认{}), 所有订单因 price<=0 被跳过, 功能不可用。
        价格从行情服务获取(失败回退持仓最新价)。"""
        from data_service.market_data_service import get_market_service
        from database import repository as repo
        prices: Dict[str, float] = {}
        names: Dict[str, str] = {}
        try:
            for w in repo.get_watchlist():
                names[w["symbol"]] = w["name"]
        except Exception:
            pass
        for sym in target_weights:
            try:
                q, _ = get_market_service().get_realtime_quote(sym, "etf")
                p = float((q or {}).get("latest_price", 0) or 0)
                if p > 0:
                    prices[sym] = p
            except Exception:
                continue
        # 目标外持仓的价格回退: 持仓最新价
        for pos in self.get_positions():
            if pos["symbol"] not in prices and pos.get("latest_price", 0) > 0:
                prices[pos["symbol"]] = float(pos["latest_price"])
        return self.portfolio.generate_rebalance_orders(target_weights, prices, names)

    def mark_to_market(self, prices: Dict[str, float]):
        """按最新价刷新持仓市值。"""
        self.account.update_prices(prices)

    def snapshot(self):
        self.account.snapshot_to_db()
