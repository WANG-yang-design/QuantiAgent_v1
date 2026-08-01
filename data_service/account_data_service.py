# -*- coding: utf-8 -*-
"""
账户数据服务 (facade)
=====================
面向 Agent 的账户/持仓/订单统一入口。
实现位于 paper_trading(模拟盘) 与 live_trading(实盘预留)。
"""
from typing import Any, Dict, List, Optional

from database import repository as repo


def get_account_view(account_id: str = "PA-001") -> Dict[str, Any]:
    """账户视图(供交易员/风控 Agent)。"""
    from paper_trading.paper_account import PaperAccount
    return PaperAccount(account_id).get_snapshot()


def get_position_view(symbol: str, account_id: str = "PA-001") -> Optional[Dict[str, Any]]:
    from paper_trading.paper_account import PaperAccount
    return PaperAccount(account_id).get_position(symbol)


def get_positions_view(account_id: str = "PA-001") -> List[Dict[str, Any]]:
    from paper_trading.paper_account import PaperAccount
    return PaperAccount(account_id).get_positions()


def get_available_quantity(symbol: str, account_id: str = "PA-001") -> int:
    """T+1 可用数量。"""
    from paper_trading.paper_account import PaperAccount
    return PaperAccount(account_id).get_available_qty(symbol)


def get_recent_trades(symbol: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    return [
        {"trade_id": t.trade_id, "symbol": t.symbol, "side": t.side,
         "price": t.price, "qty": t.qty, "fee": t.fee,
         "trade_time": str(t.trade_time)}
        for t in repo.get_trades(symbol=symbol, limit=limit)
    ]
