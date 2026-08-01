# -*- coding: utf-8 -*-
"""
新浪财经客户端 (实时行情备源)
============================
hq.sinajs.cn 接口, 免费, 需带 Referer 头。返回 GBK 编码。
"""
import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import httpx

from data_sources.base import BaseDataSource
from data_sources.akshare_client import _safe_float, _safe_str

logger = logging.getLogger("data.sina")

_HEADERS = {"Referer": "https://finance.sina.com.cn/"}


def _sina_symbol(symbol: str) -> str:
    """sh510300 / sz159919"""
    if symbol.startswith(("5", "6", "9")):
        return "sh" + symbol
    return "sz" + symbol


class SinaClient(BaseDataSource):
    """新浪行情客户端: 实时行情 + ETF日K(备源)。"""

    name = "sina"

    def __init__(self):
        self.client = httpx.Client(headers=_HEADERS, timeout=10)
        self._ak = None

    def _ak_module(self):
        if self._ak is None:
            import akshare as ak
            self._ak = ak
        return self._ak

    # ---------------- ETF 日K (备源, 免费完整历史) ----------------
    def get_daily_bars(self, symbol: str, start: date, end: date,
                       asset_type: str = "etf") -> List[Dict[str, Any]]:
        if asset_type != "etf":
            raise NotImplementedError("新浪日K仅支持ETF")
        df = self._ak_module().fund_etf_hist_sina(symbol=_sina_symbol(symbol))
        if df is None or df.empty:
            raise RuntimeError(f"新浪无 {symbol} 日K")
        rows = []
        for _, r in df.iterrows():
            d = r.get("date")
            if isinstance(d, str):
                d = datetime.strptime(d[:10], "%Y-%m-%d").date()
            if not (start <= d <= end):
                continue
            rows.append({
                "symbol": symbol,
                "trade_date": d,
                "open": _safe_float(r.get("open")),
                "high": _safe_float(r.get("high")),
                "low": _safe_float(r.get("low")),
                "close": _safe_float(r.get("close")),
                "volume": _safe_float(r.get("volume")),   # 新浪已是股单位
                "amount": _safe_float(r.get("amount")),
                "source": self.name,
            })
        return rows

    # ---------------- 指数日K (基准对比用) ----------------
    def get_index_bars(self, index_code: str, start: date, end: date) -> List[Dict[str, Any]]:
        code_map = {"000300": "sh000300", "000001": "sh000001", "000905": "sh000905",
                    "399006": "sz399006"}
        sina_code = code_map.get(index_code, "sh" + index_code)
        try:
            df = self._ak_module().stock_zh_index_daily(symbol=sina_code)
        except Exception as exc:
            raise RuntimeError(f"新浪指数失败 {index_code}: {exc}") from exc
        if df is None or df.empty:
            raise RuntimeError(f"新浪无指数 {index_code} 数据")
        rows = []
        for _, r in df.iterrows():
            d = r.get("date")
            if isinstance(d, str):
                d = datetime.strptime(d[:10], "%Y-%m-%d").date()
            if not (start <= d <= end):
                continue
            rows.append({
                "symbol": index_code, "trade_date": d,
                "open": _safe_float(r.get("open")),
                "high": _safe_float(r.get("high")),
                "low": _safe_float(r.get("low")),
                "close": _safe_float(r.get("close")),
                "volume": _safe_float(r.get("volume")) * 100,
                "amount": _safe_float(r.get("amount") or r.get("value")),
                "source": self.name,
            })
        return rows

    def get_realtime_quote(self, symbol: str, asset_type: str = "etf") -> Dict[str, Any]:
        resp = self.client.get(f"https://hq.sinajs.cn/list={_sina_symbol(symbol)}")
        resp.raise_for_status()
        text = resp.content.decode("gbk", errors="ignore")
        if '="' not in text:
            raise RuntimeError(f"新浪无 {symbol} 行情")
        parts = text.split('="')[1].rstrip('";').split(",")
        # 字段: 名称,今开,昨收,现价,最高,最低,买一,卖一,成交量(股),成交额,...
        if len(parts) < 10:
            raise RuntimeError(f"新浪 {symbol} 行情字段不足")
        return {
            "symbol": symbol,
            "quote_time": datetime.now(),
            "name": _safe_str(parts[0]),
            "latest_price": _safe_float(parts[3]),
            "prev_close": _safe_float(parts[2]),
            "open": _safe_float(parts[1]),
            "high": _safe_float(parts[4]),
            "low": _safe_float(parts[5]),
            "volume": _safe_float(parts[8]),
            "amount": _safe_float(parts[9]),
            "change_pct": ((_safe_float(parts[3]) / _safe_float(parts[2])) - 1) * 100
            if _safe_float(parts[2]) > 0 else 0.0,
            "source": self.name,
        }

