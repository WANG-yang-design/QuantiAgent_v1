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
            cal = get_market_service().get_trade_calendar(start, end)
            if cal:
                self._cal = cal
            else:
                # 修复: 数据源失败时的"工作日"兜底不再写入永久缓存 ——
                # 原实现节假日被当成交易日, asof 切片无新K线 → 相同数据重复
                # 产生信号(超量建仓), 且数据源恢复后依然用错误日历。
                logger.warning("交易日历获取失败, 本次按工作日近似")
                return [d for d in self._weekdays(start, end)]
        return [d for d in self._cal if start <= d <= end]

    @staticmethod
    def _weekdays(start: date, end: date) -> List[date]:
        out = []
        d = start
        while d <= end:
            if d.weekday() < 5:
                out.append(d)
            d += timedelta(days=1)
        return out

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
        norm = self._smooth_ex_dividend(norm)
        self._daily_cache[key] = norm
        return norm

    @staticmethod
    def _smooth_ex_dividend(bars: List[dict], jump_threshold: float = None) -> List[dict]:
        """
        除息平滑: 修正前复权(qfq)数据漏处理的 ETF 分红除息跳变。
        阈值语义: 单日跳空幅度超过 ±(1-jump_threshold) 判定为除息跳变并平滑。
        修复: 原硬编码 0.78(±22%) 会把 ±20% 涨跌幅品种(588xxx/159915等)的
        真实涨跌停/复牌跳空误判为除息并重算整个后续序列。默认 0.88(±12%)
        对 10% 品种安全(极限涨跌 0.90/1.111 不触发), 可在 config.yaml
        backtest.ex_dividend_jump_ratio 调整。
        """
        if jump_threshold is None:
            try:
                from core.config import get_settings
                jump_threshold = float(get_settings().get(
                    "backtest.ex_dividend_jump_ratio", 0.88))
            except Exception:
                jump_threshold = 0.88
        if not bars:
            return bars
        out: List[dict] = []
        factor = 1.0
        for i, b in enumerate(bars):
            if out:
                prev_close = out[-1]["close"]
                if prev_close > 0:
                    ratio = b["open"] / prev_close
                    # 修复: 阈值按标的涨跌幅动态取 —— ±20% 品种(588/159915等)
                    # 真实涨跌停跳空 ratio≈0.80/1.20, 原 0.88 阈值会误判为除息,
                    # 整个后续序列被错误缩放, 系统性污染回测数据。
                    thr = jump_threshold
                    try:
                        from core.symbol_utils import price_limit_pct
                        limit = price_limit_pct(b.get("symbol", ""), "etf")
                        if limit >= 0.20:
                            thr = min(jump_threshold, 0.80)
                    except Exception:
                        pass
                    if ratio < thr or ratio > (2 - thr):
                        # 除息跳变: 按开盘跳空比例调整(把分红算回持仓)
                        factor = prev_close / b["open"]
                        logger.info("检测到除息跳变 %s %s: 昨收%.4f→今开%.4f (%.1f%%), 已平滑",
                                    b["symbol"], b["trade_date"], prev_close, b["open"],
                                    (ratio - 1) * 100)
            if factor != 1.0:
                b = {**b,
                     "open": round(b["open"] * factor, 4),
                     "high": round(b["high"] * factor, 4),
                     "low": round(b["low"] * factor, 4),
                     "close": round(b["close"] * factor, 4)}
            out.append(b)
        return out

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
