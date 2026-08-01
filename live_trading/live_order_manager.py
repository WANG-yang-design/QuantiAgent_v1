# -*- coding: utf-8 -*-
"""
实盘订单管理器 (预留)
=====================
实盘下的订单状态机/幂等控制:
- order_intent_id 幂等提交
- 状态未知 → 查询优先, 绝不重复下单
- 失败订单 → 熔断计数
V1 模拟盘复用 paper_trading.order_manager; 实盘启用时替换。
"""
import logging
from typing import Any, Dict, Optional

from live_trading.broker_adapter import BrokerAdapter, OrderUnknownError
from risk.circuit_breaker import CircuitBreaker

logger = logging.getLogger("live.order")


class LiveOrderManager:
    """实盘订单管理(预留桩, 接口已定义)。"""

    def __init__(self, adapter: BrokerAdapter):
        self.adapter = adapter
        self.cb = CircuitBreaker.instance()

    def submit(self, order_request: Dict[str, Any]) -> Dict[str, Any]:
        """幂等提交: 相同 intent 不重复下单。"""
        intent = order_request.get("order_intent_id")
        if not intent:
            raise ValueError("实盘下单必须提供 order_intent_id(幂等键)")
        if self.cb.is_paused():
            raise RuntimeError(f"系统熔断中, 禁止下单: {self.cb.paused_reason()}")
        try:
            result = self.adapter.place_order(order_request)
            self.cb.on_order_success()
            return result
        except OrderUnknownError as exc:
            # 状态未知: 查询确认, 绝不重复提交
            logger.error("订单状态未知, 查询确认: %s", exc)
            orders = self.adapter.get_orders()
            for o in orders:
                if o.get("order_intent_id") == intent:
                    return o
            raise
        except Exception as exc:
            self.cb.on_order_failure()
            raise
