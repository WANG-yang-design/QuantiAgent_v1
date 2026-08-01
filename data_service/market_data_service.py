# -*- coding: utf-8 -*-
"""
统一行情数据服务
================
为 Agent/策略/回测提供统一数据入口:
  数据源(多源容灾) → 质量校验 → 落库 → 缓存 → 返回
所有 Agent 只调这里, 不直接碰数据源。
"""
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from data_service.cache_service import (
    daily_cache, etf_spot_cache, minute_cache, order_book_cache, quote_cache,
)
from data_service.data_quality import (
    ALLOWED_QUALITY, DataQualityReport, get_quality_checker,
)
from data_sources.hub import DataSourceHub, get_hub
from database import repository as repo

logger = logging.getLogger("data.market_service")


class MarketDataService:
    """统一行情/新闻/账户数据服务。"""

    def __init__(self, hub: Optional[DataSourceHub] = None):
        self.hub = hub or get_hub()
        self.qc = get_quality_checker()

    # ================================================================
    # 日K (带缓存 + 质量校验 + 落库)
    # ================================================================
    def get_daily_bars(self, symbol: str, start: Optional[date] = None,
                       end: Optional[date] = None, asset_type: str = "etf",
                       use_cache: bool = True, save_db: bool = True
                       ) -> Tuple[List[Dict[str, Any]], DataQualityReport]:
        """返回 (bars列表, 质量报告)。bars 升序。"""
        end = end or date.today()
        start = start or (end - timedelta(days=400))
        key = f"daily:{symbol}:{start}:{end}:{asset_type}"
        if use_cache:
            hit = daily_cache.get(key)
            if hit is not None:
                return hit, DataQualityReport(symbol, "daily_bar")

        try:
            bars, source = self.hub.get_daily_bars(symbol, start, end, asset_type)
        except Exception as exc:
            logger.error("日K获取失败 %s: %s", symbol, exc)
            return [], self._failed_report(symbol, "daily_bar", str(exc))

        rep = self.qc.check_daily_bars(symbol, bars)
        if rep.status in ALLOWED_QUALITY and save_db:
            try:
                for b in bars:
                    b["quality_status"] = rep.status
                repo.upsert_daily_bars(bars)
            except Exception as exc:
                logger.error("日K落库失败 %s: %s", symbol, exc)

        daily_cache.set(key, bars)
        return bars, rep

    # ================================================================
    # 分钟K
    # ================================================================
    def get_minute_bars(self, symbol: str, start: Optional[datetime] = None,
                        end: Optional[datetime] = None, freq: str = "5m",
                        asset_type: str = "etf") -> Tuple[List[Dict[str, Any]], DataQualityReport]:
        end = end or datetime.now()
        start = start or (end - timedelta(days=7))
        key = f"minute:{symbol}:{start}:{end}:{freq}"
        hit = minute_cache.get(key)
        if hit is not None:
            return hit, DataQualityReport(symbol, "minute_bar")
        try:
            bars, source = self.hub.get_minute_bars(symbol, start, end, freq, asset_type)
        except Exception as exc:
            logger.error("分钟K获取失败 %s: %s", symbol, exc)
            return [], self._failed_report(symbol, "minute_bar", str(exc))
        rep = self.qc.check_minute_bars(symbol, bars, freq)
        if rep.status in ALLOWED_QUALITY:
            try:
                for b in bars:
                    b["quality_status"] = rep.status
                repo.upsert_minute_bars(bars)
            except Exception:
                pass
        minute_cache.set(key, bars)
        return bars, rep

    # ================================================================
    # 实时行情 (缓存6秒)
    # ================================================================
    def get_realtime_quote(self, symbol: str, asset_type: str = "etf") -> Tuple[Dict[str, Any], DataQualityReport]:
        key = f"quote:{symbol}"
        hit = quote_cache.get(key)
        if hit is not None:
            return hit, self.qc.check_realtime_quote(symbol, hit)
        try:
            quote, source = self.hub.get_realtime_quote(symbol, asset_type)
        except Exception as exc:
            logger.error("实时行情失败 %s: %s", symbol, exc)
            return {}, self._failed_report(symbol, "realtime_quote", str(exc))
        rep = self.qc.check_realtime_quote(symbol, quote)
        if rep.status in ALLOWED_QUALITY:
            try:
                repo.save_realtime_quote(quote)
            except Exception:
                pass
        quote_cache.set(key, quote)
        return quote, rep

    # ================================================================
    # 五档盘口 (缓存15秒)
    # ================================================================
    def get_order_book(self, symbol: str, asset_type: str = "etf") -> Tuple[Dict[str, Any], DataQualityReport]:
        key = f"ob:{symbol}"
        hit = order_book_cache.get(key)
        if hit is not None:
            return hit, self.qc.check_order_book(symbol, hit)
        try:
            ob, source = self.hub.get_order_book(symbol, asset_type)
        except Exception as exc:
            logger.error("盘口获取失败 %s: %s", symbol, exc)
            return {}, self._failed_report(symbol, "order_book", str(exc))
        rep = self.qc.check_order_book(symbol, ob)
        if rep.status in ALLOWED_QUALITY:
            try:
                repo.save_order_book(ob)
            except Exception:
                pass
        order_book_cache.set(key, ob)
        return ob, rep

    # ================================================================
    # ETF 全市场 / 资金流 / 指数
    # ================================================================
    def get_etf_spot(self) -> List[Dict[str, Any]]:
        hit = etf_spot_cache.get("all")
        if hit is not None:
            return hit
        try:
            spot, source = self.hub.get_etf_spot()
            etf_spot_cache.set("all", spot)
            return spot
        except Exception as exc:
            logger.error("ETF全市场获取失败: %s", exc)
            return []

    def get_etf_info(self, symbol: str) -> Dict[str, Any]:
        try:
            info, source = self.hub.get_etf_info(symbol)
            repo.upsert_etf_info([info])
            return info
        except Exception as exc:
            logger.warning("ETF信息获取失败 %s: %s", symbol, exc)
            return {"symbol": symbol}

    def get_money_flow(self, symbol: str, asset_type: str = "etf") -> Dict[str, Any]:
        try:
            mf, source = self.hub.get_money_flow(symbol, asset_type)
            repo.save_money_flow(mf)
            return mf
        except Exception as exc:
            logger.warning("资金流获取失败 %s: %s", symbol, exc)
            return {}

    def get_index_bars(self, index_code: str, start: date, end: date) -> List[Dict[str, Any]]:
        try:
            bars, source = self.hub.get_index_bars(index_code, start, end)
            return bars
        except Exception as exc:
            logger.warning("指数获取失败 %s: %s", index_code, exc)
            return []

    def get_trade_calendar(self, start: date, end: date) -> List[date]:
        try:
            dates, source = self.hub.get_trade_calendar(start, end)
            return dates
        except Exception as exc:
            logger.warning("交易日历获取失败: %s", exc)
            return []

    def is_trade_day(self, d: Optional[date] = None) -> bool:
        d = d or date.today()
        if d.weekday() >= 5:
            return False
        dates = self.get_trade_calendar(d - timedelta(days=10), d + timedelta(days=1))
        return d in dates

    def get_fundamentals(self, symbol: str) -> Dict[str, Any]:
        try:
            fund, source = self.hub.get_fundamentals(symbol)
            return fund
        except Exception as exc:
            logger.warning("基本面获取失败 %s: %s", symbol, exc)
            return {}

    # ================================================================
    # 工具
    # ================================================================
    @staticmethod
    def _failed_report(symbol: str, category: str, reason: str) -> DataQualityReport:
        rep = DataQualityReport(symbol, category)
        rep.block(f"数据源全部失败: {reason}")
        return rep


_service: Optional[MarketDataService] = None


def get_market_service() -> MarketDataService:
    global _service
    if _service is None:
        _service = MarketDataService()
    return _service
