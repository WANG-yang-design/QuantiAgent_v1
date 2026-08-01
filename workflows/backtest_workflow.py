# -*- coding: utf-8 -*-
"""
回测工作流 (facade)
===================
把回测引擎与 Agent 投研链路对接: 关键节点调用研究工作流。
日线回测主入口见 backtest.engine; 本模块提供 Agent 模式的便捷封装。
"""
import asyncio
import logging
from datetime import date
from typing import Any, Dict, List, Optional

logger = logging.getLogger("workflow.backtest")


def run_backtest_with_agents(symbols: List[str], start: date, end: date,
                             agent_interval_days: int = 5,
                             initial_cash: float = 100000.0) -> Dict[str, Any]:
    """
    Agent 模式日线回测: 每 agent_interval_days 个交易日,
    对动量居前的标的调用完整研究工作流(15 Agent), 其余时间用规则信号。
    注意: 成本较高(每个关键节点消耗 token), 先小样本验证。
    """
    from backtest.engine import BacktestEngine
    from backtest.data_replayer import DataReplayer
    from strategies.rotation_executor import build_rotation_signal_fn

    signal_fn = build_rotation_signal_fn(initial_cash=initial_cash)
    engine = BacktestEngine(start, end, initial_cash=initial_cash,
                            use_agents=True, agent_interval_days=agent_interval_days)
    replayer = DataReplayer(symbols)
    return engine.run_daily(replayer, signal_fn)
