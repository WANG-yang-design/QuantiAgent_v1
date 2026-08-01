# -*- coding: utf-8 -*-
"""
回测数据回放器
==============
按时间顺序提供K线数据, 严格保证无未来函数:
- load_all_daily(symbol, start, end): 一次加载, 由引擎按 asof 截取
- 交易日历来自数据库/数据源
"""
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List

from data_service.market_data_service import get_market_service
from database import repository as repo

logger = logging.getLogger("backtest.replayer")


class DataReplayer:
    """回测数据回放: 全部从数据库读取(先由采集任务入库)。"""

    def __init__(self, universe: List[str], asset_type: str = "etf"):
        self.universe_list = universe
        self.asset_type = asset_type
        self._cal: List[date] = []
        self._daily_cache: Dict[str, List[dict]] = {}
        self._minute_cache: Dict[str, Dict[date, List[dict]]] = {}

    def universe(self) -> List[str]:
        return self.universe_list

    # ------------------------------------------------------------------
    def trade_dates(self, start: date, end: date) -> List[date]:
        if not self._cal:
            self._cal = get_market_service().get_trade_calendar(start, end)
            if not self._cal:   # 数据源失败兜底: 工作日
                self._cal = []
                d = start
                while d <= end:
                    if d.weekday() < 5:
                        self._cal.append(d)
                    d += timedelta(days=1)
        return [d for d in self._cal if start <= d <= end]

    # ------------------------------------------------------------------
    def load_all_daily(self, symbol: str, start: date, end: date) -> List[dict]:
        """一次加载全部日K(引擎按 asof 截取, 防止未来函数)。"""
        key = f"{symbol}:{start}:{end}"
        if key in self._daily_cache:
            return self._daily_cache[key]
        bars = repo.get_daily_bars(symbol, start, end)
        if not bars:
            # 数据库无数据时在线补齐(回测前建议先执行 fetch 任务)
            try:
                fetched, rep = get_market_service().get_daily_bars(
                    symbol, start, end, self.asset_type)
                bars = repo.get_daily_bars(symbol, start, end)
                logger.info("回测在线补齐 %s: %d 条", symbol, len(bars))
            except Exception as exc:
                logger.warning("回测数据加载失败 %s: %s", symbol, exc)
        norm = []
        for b in bars:
            norm.append({
                "symbol": b.symbol, "trade_date": b.trade_date,
                "open": b.open, "high": b.high, "low": b.low, "close": b.close,
                "volume": b.volume, "amount": b.amount,
            })
        self._daily_cache[key] = norm
        return norm

    # ------------------------------------------------------------------
    def load_all_minute(self, symbol: str, day: date, freq: str = "5m") -> List[dict]:
        """加载单日分钟K。"""
        key = f"{symbol}:{day}"
        if key in self._minute_cache:
            return self._minute_cache[key]
        start = datetime.combine(day, datetime.min.time())
        end = start + timedelta(days=1)
        rows = repo.get_minute_bars(symbol, start, end, freq)
        norm = [{
            "symbol": r.symbol, "bar_time": r.bar_time,
            "open": r.open, "high": r.high, "low": r.low, "close": r.close,
            "volume": r.volume, "amount": r.amount,
        } for r in rows]
        self._minute_cache[key] = norm
        return norm

    # ------------------------------------------------------------------
    def load_benchmark(self, symbol: str, start: date, end: date) -> List[dict]:
        """基准指数日K(沪深300), 无未来函数(纯行情序列)。"""
        key = f"bench:{symbol}:{start}:{end}"
        if key in self._daily_cache:
            return self._daily_cache[key]
        try:
            bars = get_market_service().get_index_bars(symbol, start, end)
        except Exception as exc:
            logger.warning("基准指数获取失败 %s: %s", symbol, exc)
            bars = []
        self._daily_cache[key] = bars
        return bars
