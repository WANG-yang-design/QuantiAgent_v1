# -*- coding: utf-8 -*-
"""
数据质量校验
============
为每条关键数据打质量标签:
  VALID      有效
  MISSING    缺失
  DELAYED    延迟
  SUSPICIOUS 疑似异常
  CONFLICT   多源冲突
  ESTIMATED  估算数据

交易和回测默认只使用 VALID 数据。盘中数据存在 DELAYED/CONFLICT/SUSPICIOUS
时, 数据管理员 Agent 必须中止交易计划生成或进入人工确认。
"""
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from core.config import get_settings

logger = logging.getLogger("data.quality")

QUALITY_VALID = "VALID"
QUALITY_MISSING = "MISSING"
QUALITY_DELAYED = "DELAYED"
QUALITY_SUSPICIOUS = "SUSPICIOUS"
QUALITY_CONFLICT = "CONFLICT"
QUALITY_ESTIMATED = "ESTIMATED"

# 可进入投研流程的质量标签
ALLOWED_QUALITY = {QUALITY_VALID, QUALITY_ESTIMATED}


class DataQualityReport:
    """一次数据质量检查的结果汇总。"""

    def __init__(self, symbol: str, category: str):
        self.symbol = symbol
        self.category = category
        self.status = QUALITY_VALID
        self.warnings: List[str] = []
        self.blocked_reason: Optional[str] = None

    def add_warning(self, level: str, msg: str):
        self.warnings.append(f"[{level}] {msg}")

    def block(self, reason: str):
        self.blocked_reason = reason
        self.status = QUALITY_MISSING if self.status == QUALITY_VALID else self.status

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "category": self.category,
            "quality_status": self.status,
            "warnings": self.warnings,
            "blocked_reason": self.blocked_reason,
        }


class DataQualityChecker:
    """数据质量检查器 (规则为主, 不依赖 LLM)。"""

    def __init__(self):
        self.cfg = get_settings().section("data_sources")
        self.freshness = self.cfg.get("freshness", {})
        self.cross = self.cfg.get("cross_source", {})

    # ------------------------------------------------------------------
    def check_daily_bars(self, symbol: str, bars: List[dict], expect_trade_date=None) -> DataQualityReport:
        rep = DataQualityReport(symbol, "daily_bar")
        if not bars:
            rep.block("日K数据为空(MISSING)")
            return rep
        # 异常价格检查
        for b in bars:
            if b["close"] <= 0 or b["open"] <= 0:
                rep.add_warning("SUSPICIOUS", f"日期{b['trade_date']} 价格异常(<=0)")
                rep.status = QUALITY_SUSPICIOUS
                break
            if b["high"] < b["low"] or b["high"] < b["close"] or b["low"] > b["close"]:
                rep.add_warning("SUSPICIOUS", f"日期{b['trade_date']} OHLC 矛盾")
                rep.status = QUALITY_SUSPICIOUS
                break
            if b["high"] > b["low"] * 1.21:      # 单日振幅>20%(股票/ETF几乎不可能)
                rep.add_warning("SUSPICIOUS", f"日期{b['trade_date']} 振幅异常 {b['high']/b['low']-1:.1%}")
                rep.status = QUALITY_SUSPICIOUS
        # 最新交易日缺失(收盘后更新检查)
        if expect_trade_date is not None:
            last = bars[-1]["trade_date"]
            if last < expect_trade_date:
                rep.add_warning("DELAYED", f"最新数据止于{last}, 预期{expect_trade_date}")
                rep.status = QUALITY_DELAYED
        return rep

    def check_minute_bars(self, symbol: str, bars: List[dict], freq: str = "5m") -> DataQualityReport:
        rep = DataQualityReport(symbol, "minute_bar")
        if not bars:
            rep.block("分钟K数据为空(MISSING)")
            return rep
        # 时间连续性: 检查相邻K线间隔
        expect = {"1m": 1, "5m": 5, "15m": 15}.get(freq, 5)
        prev = None
        gaps = 0
        for b in bars:
            if prev and (b["bar_time"] - prev).total_seconds() > expect * 60 * 3:
                gaps += 1
            prev = b["bar_time"]
        if gaps > 5:
            rep.add_warning("DELAYED", f"分钟K存在{gaps}处时间缺口")
            rep.status = QUALITY_DELAYED
        return rep

    def check_realtime_quote(self, symbol: str, quote: dict, now: Optional[datetime] = None) -> DataQualityReport:
        rep = DataQualityReport(symbol, "realtime_quote")
        if not quote or quote.get("latest_price", 0) <= 0:
            rep.block("实时行情缺失或价格异常(MISSING)")
            return rep
        now = now or datetime.now()
        stale_seconds = int(self.freshness.get("realtime_quote", 60))
        qtime = quote.get("quote_time")
        if qtime and (now - qtime).total_seconds() > stale_seconds:
            rep.add_warning("DELAYED", f"行情时间 {qtime} 已超过 {stale_seconds}s")
            rep.status = QUALITY_DELAYED
        # 涨跌幅合理性(±21%内)
        chg = abs(quote.get("change_pct", 0))
        if chg > 21:
            rep.add_warning("SUSPICIOUS", f"涨跌幅 {chg:.1f}% 异常")
            rep.status = QUALITY_SUSPICIOUS
        return rep

    def check_order_book(self, symbol: str, ob: dict) -> DataQualityReport:
        rep = DataQualityReport(symbol, "order_book")
        if not ob or ob.get("bid1", 0) <= 0 or ob.get("ask1", 0) <= 0:
            rep.block("盘口数据缺失(MISSING)")
            return rep
        if ob.get("spread", 0) > 0.10:
            rep.add_warning("SUSPICIOUS", f"盘口价差 {ob['spread']:.2%} 过大(可能停牌或流动性枯竭)")
            rep.status = QUALITY_SUSPICIOUS
        return rep

    def check_news(self, symbol: str, news: List[dict]) -> DataQualityReport:
        rep = DataQualityReport(symbol, "news")
        if not news:
            rep.add_warning("VALID", "无新闻(正常)")
        return rep

    def cross_source_conflict(self, symbol: str, category: str,
                              items_a: List[dict], items_b: List[dict]) -> DataQualityReport:
        """
        多源比对: 同一天收盘价偏差超过阈值 → CONFLICT。
        items_a/b 为两个源返回的行情列表。
        """
        rep = DataQualityReport(symbol, category)
        if not items_a or not items_b:
            rep.add_warning("VALID", "仅单源数据, 无法比对")
            return rep
        threshold = float(self.cross.get("close_price_diff_pct", 0.01))
        map_a = {str(b["trade_date"]): b["close"] for b in items_a}
        conflicts = 0
        for b in items_b:
            d = str(b["trade_date"])
            if d in map_a and map_a[d] > 0:
                diff = abs(b["close"] - map_a[d]) / map_a[d]
                if diff > threshold:
                    conflicts += 1
        if conflicts >= 3:
            rep.add_warning("CONFLICT", f"{conflicts}天收盘价多源冲突> {threshold:.1%}")
            rep.status = QUALITY_CONFLICT
        return rep


_checker: Optional[DataQualityChecker] = None


def get_quality_checker() -> DataQualityChecker:
    global _checker
    if _checker is None:
        _checker = DataQualityChecker()
    return _checker
