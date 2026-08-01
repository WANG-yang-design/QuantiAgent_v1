# -*- coding: utf-8 -*-
"""
QMT / MiniQMT 适配器 (预留)
===========================
V1 不启用。接入时:
1. pip 安装 xtquant (券商QMT环境自带)
2. 在 config.yaml broker 段配置 qmt_path / qmt_account
3. 实现本文件各方法, 遵循 BrokerAdapter 幂等与状态查询规范
"""
import logging
from typing import Any, Dict, List

from core.config import get_settings
from live_trading.broker_adapter import BrokerAdapter

logger = logging.getLogger("live.qmt")


class QmtAdapter(BrokerAdapter):
    """QMT 量化交易终端适配(预留桩)。"""

    broker_name = "qmt"

    def __init__(self):
        self.cfg = get_settings().section("broker")
        self.connected = False

    def connect(self) -> bool:
        raise NotImplementedError(
            "QMT 适配器为实盘预留组件。接入步骤:\n"
            "1. 安装 QMT 客户端并登录\n"
            "2. pip install xtquant (QMT 自带)\n"
            "3. 在本文件实现 get_account/get_positions/place_order 等\n"
            "4. 先在 config.yaml 设置 broker.adapter=qmt 并做只读验证(文档23.1)")

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
