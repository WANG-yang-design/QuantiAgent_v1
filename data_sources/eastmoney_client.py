# -*- coding: utf-8 -*-
"""
东方财富直连客户端 (主源: 实时行情/盘口; 备源: 资金流)
=====================================================
使用 push2 行情接口(免费, 需请求头 UA), 备源用 akshare 的东方财富封装。
"""
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from data_sources.base import BaseDataSource
from data_sources.akshare_client import _safe_float, _safe_str

logger = logging.getLogger("data.eastmoney")

_PUSH2 = "https://push2.eastmoney.com/api/qt/stock/get"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
}

# 五档盘口字段映射 (f31~f50)
_OB_FIELDS = {
    "f31": "卖五价", "f32": "卖五量", "f33": "卖四价", "f34": "卖四量",
    "f35": "卖三价", "f36": "卖三量", "f37": "卖二价", "f38": "卖二量",
    "f39": "卖一价", "f40": "卖一量", "f41": "买一价", "f42": "买一量",
    "f43": "买二价", "f44": "买二量", "f45": "买三价", "f46": "买三量",
    "f47": "买四价", "f48": "买四量", "f49": "买五价", "f50": "买五量",
}


def secid(symbol: str) -> str:
    """东财 secid: 1=沪, 0=深。"""
    if symbol.startswith(("5", "6", "9")):
        return f"1.{symbol}"
    return f"0.{symbol}"


class EastMoneyClient(BaseDataSource):
    """东方财富直连客户端。"""

    name = "eastmoney"

    def __init__(self):
        # 修复: 绕过系统代理直连 —— 用户网络环境有代理时,
        # 东财接口报 ProxyError 导致数据获取失败(数据源容灾链全灭)
        self.client = httpx.Client(headers=_HEADERS, timeout=10,
                                   proxy=None, trust_env=False)

    # ---------------- 实时行情 ----------------
    def get_realtime_quote(self, symbol: str, asset_type: str = "etf") -> Dict[str, Any]:
        params = {
            "secid": secid(symbol), "invt": 2, "fltt": 2,
            "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f107,f162,f168,f170",
        }
        resp = self.client.get(_PUSH2, params=params)
        resp.raise_for_status()
        d = resp.json().get("data") or {}
        if not d:
            raise RuntimeError(f"东方财富无 {symbol} 实时行情")
        return {
            "symbol": symbol,
            "quote_time": datetime.now(),
            "latest_price": _safe_float(d.get("f43")),       # 最新价
            "change_pct": _safe_float(d.get("f170")),        # 涨跌幅%
            "volume": _safe_float(d.get("f47")),             # 成交量(股)
            "amount": _safe_float(d.get("f48")),             # 成交额
            "high": _safe_float(d.get("f44")),
            "low": _safe_float(d.get("f45")),
            "open": _safe_float(d.get("f46")),
            "prev_close": _safe_float(d.get("f60")),
            "iopv": _safe_float(d.get("f162")),              # IOPV
            "source": self.name,
        }

    # ---------------- 五档盘口 ----------------
    def get_order_book(self, symbol: str, asset_type: str = "etf") -> Dict[str, Any]:
        params = {
            "secid": secid(symbol), "invt": 2, "fltt": 2,
            "fields": ",".join(_OB_FIELDS.keys()),
        }
        resp = self.client.get(_PUSH2, params=params)
        resp.raise_for_status()
        d = resp.json().get("data") or {}
        if not d:
            raise RuntimeError(f"东方财富无 {symbol} 盘口")
        ob_json = {k: _safe_float(d.get(f)) for k, f in _OB_FIELDS.items()}
        bid1 = _safe_float(d.get("f41"))
        ask1 = _safe_float(d.get("f39"))
        spread = (ask1 - bid1) / ask1 if ask1 > 0 else 0.0
        return {
            "symbol": symbol,
            "snapshot_time": datetime.now(),
            "bid1": bid1,
            "ask1": ask1,
            "bid_vol1": _safe_float(d.get("f42")),
            "ask_vol1": _safe_float(d.get("f40")),
            "spread": spread,
            "order_book_json": ob_json,
            "source": self.name,
        }

    # ---------------- 资金流 (备用) ----------------
    def get_money_flow(self, symbol: str, asset_type: str = "etf") -> Dict[str, Any]:
        # 直接调用 akshare 的东财资金流接口, 保证列名稳定
        from data_sources.akshare_client import AkShareClient
        return AkShareClient().get_money_flow(symbol, asset_type)

    # ---------------- 新闻 (备用) ----------------
    def get_news(self, symbol: str, limit: int = 20) -> List[Dict[str, Any]]:
        from data_sources.akshare_client import AkShareClient
        return AkShareClient().get_news(symbol, limit)
