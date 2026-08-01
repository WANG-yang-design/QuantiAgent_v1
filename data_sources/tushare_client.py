# -*- coding: utf-8 -*-
"""
Tushare Pro 客户端 (预留)
==========================
V1 主源为 AkShare/东方财富(免费)。此客户端为接入 Tushare 预留,
配置 TUSHARE_TOKEN 后自动启用, 作为日K/交易日历/财务的备源。
"""
import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from core.config import get_settings
from data_sources.base import BaseDataSource
from data_sources.akshare_client import _safe_float, _safe_str

logger = logging.getLogger("data.tushare")


class TushareClient(BaseDataSource):
    """Tushare Pro 客户端(预留)。无 token 时所有方法抛异常, 由 Hub 跳过。"""

    name = "tushare"

    def __init__(self):
        token = get_settings().get("tushare.token", "")
        self._pro = None
        if token:
            try:
                import tushare as ts
                ts.set_token(token)
                self._pro = ts.pro_api()
            except Exception as e:
                logger.warning("Tushare 初始化失败: %s", e)

    def _check(self):
        if self._pro is None:
            raise RuntimeError("Tushare 未配置 token, 请设置 TUSHARE_TOKEN")

    def get_daily_bars(self, symbol: str, start: date, end: date,
                       asset_type: str = "etf") -> List[Dict[str, Any]]:
        self._check()
        df = self._pro.daily(ts_code=_ts_code(symbol, asset_type),
                             start_date=start.strftime("%Y%m%d"),
                             end_date=end.strftime("%Y%m%d"))
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "symbol": symbol,
                "trade_date": datetime.strptime(str(r["trade_date"]), "%Y%m%d").date(),
                "open": _safe_float(r["open"]),
                "high": _safe_float(r["high"]),
                "low": _safe_float(r["low"]),
                "close": _safe_float(r["close"]),
                "volume": _safe_float(r["vol"]) * 100,
                "amount": _safe_float(r["amount"]) * 1000,   # tushare amount 单位:千元
                "source": self.name,
            })
        return rows

    def get_trade_calendar(self, start: date, end: date) -> List[date]:
        self._check()
        df = self._pro.trade_cal(exchange="SSE",
                                 start_date=start.strftime("%Y%m%d"),
                                 end_date=end.strftime("%Y%m%d"),
                                 is_open="1")
        return [datetime.strptime(str(v), "%Y%m%d").date() for v in df["cal_date"].tolist()]


def _ts_code(symbol: str, asset_type: str = "etf") -> str:
    """A股 6 位代码 → tushare ts_code (600519.SH / 510300.SH / 000001.SZ)。"""
    if symbol.startswith(("5", "6", "9")):
        return f"{symbol}.SH"
    return f"{symbol}.SZ"
