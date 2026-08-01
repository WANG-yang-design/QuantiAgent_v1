# -*- coding: utf-8 -*-
"""
PTrade 适配器 (预留)
====================
V1 不启用。PTrade 为恒生极速交易平台, 通过 API 服务对接。
接入流程与 QMT 相同: 先只读验证(文档23.1), 再逐步放量。
"""
import logging
from typing import Any, Dict, List

from core.config import get_settings
from live_trading.broker_adapter import BrokerAdapter

logger = logging.getLogger("live.ptrade")


class PtradeAdapter(BrokerAdapter):
    """PTrade 券商API适配(预留桩)。"""

    broker_name = "ptrade"

    def __init__(self):
        self.cfg = get_settings().section("broker")

    def connect(self) -> bool:
        raise NotImplementedError("PTrade 适配器为实盘预留组件, V1 不启用")

    def disconnect(self):
        raise NotImplementedError

    def get_account(self) -> Dict[str, Any]:
        raise NotImplementedError

    def get_positions(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def get_orders(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def get_trades(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def place_order(self, order_request: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        raise NotImplementedError

    def query_order(self, order_id: str) -> Dict[str, Any]:
        raise NotImplementedError
