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
                # 命中时重新做质量检查(坏数据不再以 VALID 标签进入决策链)
                return hit, self.qc.check_daily_bars(symbol, hit)

        try:
            bars, source = self.hub.get_daily_bars(symbol, start, end, asset_type)
        except Exception as exc:
            logger.error("日K获取失败 %s: %s", symbol, exc)
            return [], self._failed_report(symbol, "daily_bar", str(exc))
        # 修复: 主源(baostock)故障时回退源只返回当日1根K线, 直接透传会
        # 导致前端K线图只剩当天、AI特征全 NaN。少于20根视为采集失败,
        # 回退到本地日K库(历史多次采集已落库)。
        if len(bars) < 20:
            logger.warning("日K仅 %d 根(%s), 回退本地日K库", len(bars), symbol)
            try:
                from database.models import DailyBar
                from database.db_session import get_session
                with get_session() as s:
                    rows = s.query(DailyBar).filter(
                        DailyBar.symbol == symbol,
                        DailyBar.trade_date >= start,
                        DailyBar.trade_date <= end,
                    ).order_by(DailyBar.trade_date).all()
                if len(rows) > len(bars):
                    bars = [{
                        "symbol": r.symbol, "trade_date": r.trade_date,
                        "open": r.open, "high": r.high, "low": r.low,
                        "close": r.close, "volume": r.volume, "amount": r.amount,
                    } for r in rows]
            except Exception as exc2:
                logger.warning("日K库回退失败 %s: %s", symbol, exc2)

        rep = self.qc.check_daily_bars(symbol, bars)
        if rep.status in ALLOWED_QUALITY:
            if save_db:
                try:
                    for b in bars:
                        b["quality_status"] = rep.status
                    repo.upsert_daily_bars(bars)
                except Exception as exc:
                    logger.error("日K落库失败 %s: %s", symbol, exc)
            # 只缓存合格数据: SUSPICIOUS/DELAYED 不缓存, 保证下次请求重新校验
            # 修复: 数据源故障时(如 baostock 断连)回退源只返回当日1根K线, 质量
            # 检查仍判 VALID 并被缓存1小时 -> 前端K线图一整天只显示当天一根。
            # 少于20根视为采集失败, 不缓存(下次重新拉取, 直到主源恢复)。
            if len(bars) >= 20:
                daily_cache.set(key, bars, ttl=600)   # 10分钟TTL(原1小时, 失败数据滞留过久)
            else:
                logger.warning("日K仅 %d 根(%s), 疑似数据源降级, 不缓存", len(bars), symbol)
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
            except Exception as exc:
                logger.error("分钟K落库失败 %s: %s", symbol, exc)
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
            except Exception as exc:
                # 修复: 原实现静默吞异常, 实时行情入库失败无任何日志
                logger.error("实时行情落库失败 %s: %s", symbol, exc)
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
            except Exception as exc:
                logger.error("盘口落库失败 %s: %s", symbol, exc)
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
        """交易日历: 优先读本地缓存(data/trade_calendar.json, 由调度任务
        每日更新), 缓存缺失/覆盖不足时回源拉取。"""
        import json
        from core.config import ROOT_DIR
        try:
            path = ROOT_DIR / "data" / "trade_calendar.json"
            if path.exists():
                cached = json.loads(path.read_text(encoding="utf-8"))
                cached = [datetime.strptime(x, "%Y-%m-%d").date() for x in cached]
                # 缓存覆盖请求区间才直接用(避免旧缓存误判交易日)
                if cached and cached[0] <= start and cached[-1] >= end:
                    return [d for d in cached if start <= d <= end]
        except Exception:
            pass
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
        # 修复: 交易日历获取失败(返回[])与"确实非交易日"要区分。
        # 失败时按工作日近似判定并告警, 避免真实交易日被静默跳过。
        if not dates:
            logger.warning("交易日历获取失败, 按工作日近似判定 %s", d)
            return True
        return d in dates

    def get_fundamentals(self, symbol: str) -> Dict[str, Any]:
        """基本面数据(拉取并落库, 供DB查询/回放)。"""
        try:
            fund, source = self.hub.get_fundamentals(symbol)
            if fund:
                try:
                    repo.upsert_fundamentals([fund])
                except Exception as exc:
                    logger.warning("基本面落库失败 %s: %s", symbol, exc)
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
