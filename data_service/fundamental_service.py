# -*- coding: utf-8 -*-
"""
基本面数据服务 (facade)
=======================
V1 实现位于 data_service.market_data_service.MarketDataService.get_fundamentals
(数据源→清洗→入库→缓存)。本模块保持目录结构完整性, 提供面向 Agent 的薄封装。
"""
from typing import Any, Dict

from data_service.market_data_service import get_market_service


def get_fundamental(symbol: str) -> Dict[str, Any]:
    """获取标的最新基本面数据(股票; ETF 返回空)。"""
    return get_market_service().get_fundamentals(symbol)


def get_pe_pb_roe(symbol: str) -> Dict[str, Any]:
    """估值快照: PE/PB/ROE(供基本面分析师)。"""
    f = get_fundamental(symbol)
    if not f:
        return {}
    return {
        "symbol": symbol, "pe": f.get("pe", 0), "pb": f.get("pb", 0),
        "roe": f.get("roe", 0),
        "revenue_growth": f.get("revenue_growth", 0),
        "profit_growth": f.get("profit_growth", 0),
    }
