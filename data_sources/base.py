# -*- coding: utf-8 -*-
"""
数据源基类
==========
所有数据源实现统一接口, 供 DataSourceHub 多源容灾调用。
V1 必须支持: 日K/分钟K/实时行情/盘口/资金流/新闻/公告/舆情/交易日历/指数/ETF信息。
"""
from abc import ABC
from datetime import date, datetime
from typing import Any, Dict, List, Optional


class BaseDataSource(ABC):
    """数据源抽象基类。所有方法失败时抛异常, 由 Hub 切换备源。
    未实现的方法默认抛 NotImplementedError, 允许部分实现(如新浪只做实时行情)。"""

    name: str = "base"

    # ---------------- 行情 ----------------
    def get_daily_bars(self, symbol: str, start: date, end: date,
                       asset_type: str = "etf") -> List[Dict[str, Any]]:
        """
        返回日K列表, 每项: {
          symbol, trade_date(date), open, high, low, close,
          volume(股/份), amount(元), source
        }
        """
        raise NotImplementedError(f"{self.name} 未实现 get_daily_bars")

    def get_minute_bars(self, symbol: str, start: datetime, end: datetime,
                        freq: str = "5m", asset_type: str = "etf") -> List[Dict[str, Any]]:
        """分钟K: {symbol, bar_time, freq, open, high, low, close, volume, amount, source}"""
        raise NotImplementedError(f"{self.name} 未实现 get_minute_bars")

    def get_realtime_quote(self, symbol: str, asset_type: str = "etf") -> Dict[str, Any]:
        """实时行情: {symbol, quote_time, latest_price, change_pct, volume, amount, high, low, open, prev_close, source}"""
        raise NotImplementedError(f"{self.name} 未实现 get_realtime_quote")

    def get_order_book(self, symbol: str, asset_type: str = "etf") -> Dict[str, Any]:
        """五档盘口: {symbol, snapshot_time, bid1, ask1, bid_vol1, ask_vol1, spread, order_book_json, source}"""
        raise NotImplementedError(f"{self.name} 未实现 get_order_book")

    # ---------------- 辅助数据 ----------------
    def get_money_flow(self, symbol: str, asset_type: str = "etf") -> Dict[str, Any]:
        """个股/ETF 资金流: {symbol, record_time, main_inflow, net_inflow, ...}"""
        raise NotImplementedError(f"{self.name} 未实现 get_money_flow")

    def get_news(self, symbol: str, limit: int = 20) -> List[Dict[str, Any]]:
        """个股新闻: [{news_id, symbol, title, content, publish_time, source, url}]"""
        raise NotImplementedError(f"{self.name} 未实现 get_news")

    def get_announcements(self, symbol: str, limit: int = 20) -> List[Dict[str, Any]]:
        """公司公告: [{announcement_id, symbol, title, url, publish_time}]"""
        raise NotImplementedError(f"{self.name} 未实现 get_announcements")

    def get_sentiment(self, symbol: str, limit: int = 50) -> List[Dict[str, Any]]:
        """股吧舆情: [{record_id, symbol, platform, content, score, heat, publish_time}]"""
        raise NotImplementedError(f"{self.name} 未实现 get_sentiment")

    def get_trade_calendar(self, start: date, end: date) -> List[date]:
        """交易日历(仅交易日): [date,...]"""
        raise NotImplementedError(f"{self.name} 未实现 get_trade_calendar")

    def get_index_bars(self, index_code: str, start: date, end: date) -> List[Dict[str, Any]]:
        """指数日K (000001=上证, 000300=沪深300, 000905=中证500)"""
        raise NotImplementedError(f"{self.name} 未实现 get_index_bars")

    def get_etf_spot(self) -> List[Dict[str, Any]]:
        """全市场ETF实时列表: [{symbol, name, latest_price, change_pct, amount, iopv, ...}]"""
        raise NotImplementedError(f"{self.name} 未实现 get_etf_spot")

    def get_etf_info(self, symbol: str) -> Dict[str, Any]:
        """ETF基础信息: {symbol, name, tracking_index, scale, fee_rate, fund_company, is_qdii}"""
        raise NotImplementedError(f"{self.name} 未实现 get_etf_info")

    def get_fundamentals(self, symbol: str) -> Dict[str, Any]:
        """股票基本面: {symbol, report_date, pe, pb, roe, revenue_growth, profit_growth, ...}"""
        raise NotImplementedError(f"{self.name} 未实现 get_fundamentals")
