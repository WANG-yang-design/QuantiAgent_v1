# -*- coding: utf-8 -*-
"""
数据源集线器 (多源容灾)
=======================
按 data_sources.yaml 配置: 主源失败 → 依次切换备源。
每个数据类别独立的 failover 链。全部失败抛 DataSourceError,
由数据质量层标记 MISSING 并阻断交易。
"""
import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Type

from core.config import get_settings
from data_sources.base import BaseDataSource

logger = logging.getLogger("data.hub")


class DataSourceError(RuntimeError):
    """所有数据源均失败。"""


class DataSourceHub:
    """数据源集合与多源容灾。"""

    # 类别最小调用间隔(秒) —— 防免费接口限流
    _RATE_LIMIT = {
        "daily_bar": 2.0, "minute_bar": 2.0, "realtime_quote": 3.0,
        "order_book": 3.0, "money_flow": 2.0, "news": 2.0,
        "announcement": 2.0, "sentiment": 2.0, "trade_calendar": 1.0,
        "etf_info": 2.0,
    }

    def __init__(self):
        self.cfg = get_settings().section("data_sources")
        self._clients: Dict[str, BaseDataSource] = {}
        self.failover_stats: Dict[str, int] = {}   # (category,source) -> fail count
        self._last_call: Dict[str, float] = {}     # (category,source) -> timestamp

    def _throttle(self, category: str, source_name: str):
        """按类别限流: 距上次调用不足最小间隔则等待。"""
        import time as _time
        key = (category, source_name)
        interval = self._RATE_LIMIT.get(category, 1.0)
        last = self._last_call.get(key, 0.0)
        wait = last + interval - _time.time()
        if wait > 0:
            _time.sleep(wait)
        self._last_call[key] = _time.time()

    # ------------------------------------------------------------------
    def _get_client(self, source_name: str) -> BaseDataSource:
        """按名字惰性实例化客户端。"""
        if source_name in self._clients:
            return self._clients[source_name]
        from data_sources.akshare_client import AkShareClient
        from data_sources.baostock_client import BaostockClient
        from data_sources.cninfo_client import CninfoClient
        from data_sources.eastmoney_client import EastMoneyClient
        from data_sources.sina_client import SinaClient
        from data_sources.tencent_client import TencentClient
        from data_sources.tushare_client import TushareClient

        classes: Dict[str, Type[BaseDataSource]] = {
            "akshare": AkShareClient,
            "baostock": BaostockClient,
            "eastmoney": EastMoneyClient,
            "sina": SinaClient,
            "tencent": TencentClient,
            "cninfo": CninfoClient,
            "tushare": TushareClient,
        }
        cls = classes.get(source_name)
        if cls is None:
            raise DataSourceError(f"未知数据源: {source_name}")
        client = cls()
        self._clients[source_name] = client
        return client

    # ------------------------------------------------------------------
    def _chain(self, category: str) -> List[str]:
        """获取某类别的主源+备源列表。"""
        spec = self.cfg.get("sources", {}).get(category, {})
        chain = [spec.get("primary", "")]
        chain += spec.get("backups", [])
        return [c for c in chain if c]

    def _call(self, category: str, method: str, *args, **kwargs):
        """按容灾链调用, 返回 (结果, 成功源)。整体失败后重试一轮(防瞬时限流)。
        修复: 空结果视为失败 —— 原实现 akshare 限流返回空 DataFrame 时被当作
        成功返回, 备源(sina/tushare)永远不会被尝试, 全部标的数据被静默标 MISSING。"""
        # 允许空结果的类别: 新闻/公告/舆情本身可能确实没有内容(不算失败)
        empty_ok = {"news", "announcement", "sentiment", "trade_calendar"}
        errors = []
        for attempt in range(2):
            for source_name in self._chain(category):
                self._throttle(category, source_name)
                try:
                    client = self._get_client(source_name)
                    fn = getattr(client, method, None)
                    if fn is None:
                        continue
                    result = fn(*args, **kwargs)
                    if result is None or (
                        isinstance(result, (list, tuple)) and not empty_ok
                        and len(result) == 0
                    ):
                        raise RuntimeError(
                            f"{source_name} 返回空结果({category})")
                    if isinstance(result, dict) and not empty_ok and not result:
                        raise RuntimeError(
                            f"{source_name} 返回空结果({category})")
                    key = (category, source_name)
                    self.failover_stats[key] = 0
                    return result, source_name
                except NotImplementedError:
                    continue
                except Exception as exc:  # noqa: BLE001
                    key = (category, source_name)
                    self.failover_stats[key] = self.failover_stats.get(key, 0) + 1
                    errors.append(f"{source_name}: {exc}")
                    logger.warning("[%s] 源 %s 失败: %s", category, source_name, exc)
            if attempt == 0:
                logger.info("[%s] 首轮全部失败, 3秒后重试", category)
                import time
                time.sleep(3)
        raise DataSourceError(
            f"数据类别 {category} 所有数据源均失败: " + "; ".join(errors))

    # ------------------------------------------------------------------
    # 对外统一接口 (类别→方法映射, 与 config/data_sources.yaml 对应)
    # ------------------------------------------------------------------
    def get_daily_bars(self, symbol: str, start: date, end: date,
                       asset_type: str = "etf") -> tuple:
        return self._call("daily_bar", "get_daily_bars", symbol, start, end, asset_type)

    def get_minute_bars(self, symbol: str, start: datetime, end: datetime,
                        freq: str = "5m", asset_type: str = "etf") -> tuple:
        return self._call("minute_bar", "get_minute_bars", symbol, start, end, freq, asset_type)

    def get_realtime_quote(self, symbol: str, asset_type: str = "etf") -> tuple:
        return self._call("realtime_quote", "get_realtime_quote", symbol, asset_type)

    def get_order_book(self, symbol: str, asset_type: str = "etf") -> tuple:
        return self._call("order_book", "get_order_book", symbol, asset_type)

    def get_money_flow(self, symbol: str, asset_type: str = "etf") -> tuple:
        return self._call("money_flow", "get_money_flow", symbol, asset_type)

    def get_news(self, symbol: str, limit: int = 20) -> tuple:
        return self._call("news", "get_news", symbol, limit)

    def get_announcements(self, symbol: str, limit: int = 20) -> tuple:
        return self._call("announcement", "get_announcements", symbol, limit)

    def get_sentiment(self, symbol: str, limit: int = 50) -> tuple:
        return self._call("sentiment", "get_sentiment", symbol, limit)

    def get_trade_calendar(self, start: date, end: date) -> tuple:
        return self._call("trade_calendar", "get_trade_calendar", start, end)

    def get_index_bars(self, index_code: str, start: date, end: date) -> tuple:
        return self._call("daily_bar", "get_index_bars", index_code, start, end)

    def get_etf_spot(self) -> tuple:
        return self._call("etf_info", "get_etf_spot")

    def get_etf_info(self, symbol: str) -> tuple:
        return self._call("etf_info", "get_etf_info", symbol)

    def get_fundamentals(self, symbol: str) -> tuple:
        return self._call("daily_bar", "get_fundamentals", symbol)


_hub: Optional[DataSourceHub] = None


def get_hub() -> DataSourceHub:
    global _hub
    if _hub is None:
        _hub = DataSourceHub()
    return _hub
