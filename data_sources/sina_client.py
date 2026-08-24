# -*- coding: utf-8 -*-
"""
新浪财经客户端 (实时行情备源 + ETF日K备源)
==========================================
hq.sinajs.cn 接口, 免费, 需带 Referer 头。返回 GBK 编码。
注意: 原实现全局 monkey-patch requests.get 强制 verify=False, 会关闭整个进程
所有 requests 调用(含 akshare 东财接口)的 SSL 证书校验, 存在 MITM 风险。
已移除: 新浪自身接口走 httpx.Client(verify=True 默认)。
"""
import logging
import re
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


def _parse_sina_quote(symbol: str, raw: str) -> Dict[str, Any]:
    """Normalize one ``hq.sinajs.cn`` record and keep exchange time."""
    parts = raw.split(",")
    if len(parts) < 10:
        raise RuntimeError(f"新浪 {symbol} 行情字段不足")
    quote_time = None
    if len(parts) > 31 and parts[30] and parts[31]:
        try:
            quote_time = datetime.strptime(
                f"{parts[30]} {parts[31]}", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            quote_time = None
    price = _safe_float(parts[3])
    prev_close = _safe_float(parts[2])
    return {
        "symbol": symbol,
        "quote_time": quote_time or datetime.now(),
        "name": _safe_str(parts[0]),
        "latest_price": price,
        "prev_close": prev_close,
        "open": _safe_float(parts[1]),
        "high": _safe_float(parts[4]),
        "low": _safe_float(parts[5]),
        "volume": _safe_float(parts[8]),
        "amount": _safe_float(parts[9]),
        "change_pct": (price / prev_close - 1) * 100 if prev_close > 0 else 0.0,
        "source": "sina",
    }


class SinaClient(BaseDataSource):
    """新浪行情客户端: 实时行情 + ETF日K(备源) + 历史分钟K(指数/ETF)。"""

    name = "sina"

    def __init__(self):
        # 修复: 绕过系统代理直连(与腾讯/东财客户端一致, 防 ProxyError)
        self.client = httpx.Client(headers=_HEADERS, timeout=10,
                                   proxy=None, trust_env=False)
        self._ak = None

    # ---------------- 历史分钟K (指数/ETF/股票通用, 免费直连) ----------------
    def get_hist_minute_bars(self, symbol: str, scale: int = 5,
                             datalen: int = 240) -> List[Dict[str, Any]]:
        """新浪分钟K线(近 N 根, 5/15/30/60分钟)。
        symbol 传 sh000001/sz399006/sh510300 等带交易所前缀的代码。
        返回 [{bar_time, open, high, low, close, volume, amount, source}]"""
        import json
        url = ("https://quotes.sina.cn/cn/api/json_v2.php/"
               "CN_MarketDataService.getKLineData"
               f"?symbol={symbol}&scale={scale}&ma=no&datalen={int(datalen)}")
        try:
            resp = self.client.get(url)
            data = json.loads(resp.text)
        except Exception as exc:
            raise RuntimeError(f"新浪分钟K失败 {symbol}: {exc}") from exc
        if not isinstance(data, list) or not data:
            raise RuntimeError(f"新浪无 {symbol} 分钟K")
        rows = []
        for r in data:
            try:
                t = datetime.strptime(str(r.get("day", ""))[:19], "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                continue
            rows.append({
                "symbol": symbol,
                "bar_time": t,
                "freq": f"{scale}m",
                "open": _safe_float(r.get("open")),
                "high": _safe_float(r.get("high")),
                "low": _safe_float(r.get("low")),
                "close": _safe_float(r.get("close")),
                "volume": _safe_float(r.get("volume")),
                "amount": _safe_float(r.get("amount")),
                "source": self.name,
            })
        return rows

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
            elif hasattr(d, "date"):
                # 修复: pandas Timestamp 与 date 直接比较抛 TypeError, 导致整源被 Hub 判失败
                d = d.date()
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
            elif hasattr(d, "date"):
                d = d.date()
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

    def get_realtime_quotes_batch(self, symbols: List[str],
                                  code_overrides: Optional[Dict[str, str]] = None
                                  ) -> Dict[str, Dict[str, Any]]:
        """Fetch multiple public quotes in one request for source consensus."""
        if not symbols:
            return {}
        code_overrides = code_overrides or {}
        code_map = {
            code_overrides.get(symbol, _sina_symbol(symbol)): symbol
            for symbol in dict.fromkeys(symbols)
        }
        resp = self.client.get(
            "https://hq.sinajs.cn/list=" + ",".join(code_map))
        resp.raise_for_status()
        text = resp.content.decode("gbk", errors="ignore")
        out: Dict[str, Dict[str, Any]] = {}
        for match in re.finditer(r'var hq_str_(\w+)="([^"]*)"', text):
            symbol = code_map.get(match.group(1))
            if not symbol or not match.group(2).strip():
                continue
            quote = _parse_sina_quote(symbol, match.group(2))
            if quote["latest_price"] > 0:
                out[symbol] = quote
        return out

    def get_realtime_quote(self, symbol: str, asset_type: str = "etf",
                           sina_code: Optional[str] = None) -> Dict[str, Any]:
        quotes = self.get_realtime_quotes_batch(
            [symbol], {symbol: sina_code} if sina_code else None)
        if symbol not in quotes:
            raise RuntimeError(f"新浪无 {symbol} 行情")
        return quotes[symbol]

