# -*- coding: utf-8 -*-
"""
策略信号层
==========
脚本策略只生成"参考信号", 供 Agent 决策参考; 最终交易计划由 Agent 层综合生成。
"""
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.ids import gen_id
from database import repository as repo


class BaseStrategy(ABC):
    """策略基类: 输入特征/行情, 输出信号。"""

    strategy_id: str = "base"

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        self.params = params or {}

    @abstractmethod
    def generate_signals(self, universe: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        universe: [{symbol, name, asset_type, features: dict}]
        返回: [{signal_id, strategy_id, symbol, signal_time, signal, score, reason}]
        signal: BUY/SELL/HOLD/EXCLUDE
        """

    def save_signals(self, signals: List[Dict[str, Any]]):
        for sig in signals:
            sig.setdefault("signal_id", gen_id("SIG"))
            sig.setdefault("signal_time", datetime.now())
            repo.save_strategy_signal(sig)
