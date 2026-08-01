# -*- coding: utf-8 -*-
"""
券商行情客户端 (实盘预留)
==========================
后续接 QMT/MiniQMT 行情时的适配位置。V1 不启用, 仅占位。
"""
import logging
from typing import Any, Dict, List

from data_sources.base import BaseDataSource

logger = logging.getLogger("data.broker_market")


class BrokerMarketClient(BaseDataSource):
    """券商行情 API 客户端(预留, 接 QMT/PTrade 行情时实现)。"""

    name = "broker_market"

    def __init__(self):
        raise NotImplementedError(
            "券商行情客户端为实盘预留组件, V1 不启用。接入 QMT 后在此实现 get_realtime_quote 等。")
