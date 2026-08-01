# -*- coding: utf-8 -*-
"""
BrokerAdapter 标准接口 (实盘预留, 文档 12.1)
============================================
V1 模拟盘已实现 PaperBroker 同构接口; 实盘接入时实现本抽象类,
替换 config.broker.adapter = qmt/ptrade 后无缝切换。

实盘演进路径(文档23, 不可跳级):
  只读同步 → 撮合校准 → 半自动 → 小额度自动
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class BrokerAdapter(ABC):
    """券商适配器标准接口。所有实现必须保证:
    1. 幂等: 同一 order_intent_id 只能提交一次
    2. 状态未知先查询: 超时/UNKNOWN 状态禁止重复提交
    3. 失败不静默: 抛异常由上层熔断
    """

    broker_name: str = "base"

    @abstractmethod
    def connect(self) -> bool:
        """建立连接/登录。"""

    @abstractmethod
    def disconnect(self):
        """断开连接。"""

    @abstractmethod
    def get_account(self) -> Dict[str, Any]:
        """账户资金: {cash, frozen_cash, market_value, total_asset, ...}"""

    @abstractmethod
    def get_positions(self) -> List[Dict[str, Any]]:
        """持仓: [{symbol, name, total_qty, available_qty, cost_price, ...}]"""

    @abstractmethod
    def get_orders(self) -> List[Dict[str, Any]]:
        """当日委托: [{order_id, symbol, side, price, qty, status, ...}]"""

    @abstractmethod
    def get_trades(self) -> List[Dict[str, Any]]:
        """当日成交: [{trade_id, order_id, symbol, side, price, qty, ...}]"""

    @abstractmethod
    def place_order(self, order_request: Dict[str, Any]) -> Dict[str, Any]:
        """
        下单。order_request 必须包含 order_intent_id(幂等键)。
        返回券商订单信息; 超时或未知 → 抛 OrderUnknownError, 由上层查询确认。
        """

    @abstractmethod
    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """撤单。"""

    @abstractmethod
    def query_order(self, order_id: str) -> Dict[str, Any]:
        """查询订单状态(未知状态时必须先查询, 绝不重复下单)。"""


class OrderUnknownError(RuntimeError):
    """订单状态未知(API超时/回报丢失): 必须查询确认, 禁止重复提交。"""


class OrderRejectedError(RuntimeError):
    """券商明确拒绝订单(资金不足/涨跌停/风控拦截等)。"""
