# -*- coding: utf-8 -*-
"""
Baostock 数据源: 历史日K(含当日) / 分钟K(5/15/30/60) / 指数 / 交易日历
=====================================================================
免费稳定, 无需token; 是日K/分钟K的主源。
修复: 长驻进程(Web/调度器)中 baostock 连接会断, 断后所有查询报
"网络接收错误", 前端K线/分时只剩当天数据 —— 查询失败时强制重新登录重试一次。
"""
import logging
import threading
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from data_sources.base import BaseDataSource

logger = logging.getLogger("data.baostock")

_bs_lock = threading.RLock()
_bs_logged_in = False
_bs_last_fail = 0.0


def _ensure_login():
    """baostock 全局登录(线程安全, 失败 120s 内不重试)。"""
    global _bs_logged_in, _bs_last_fail
    if _bs_logged_in:
        return
    if _bs_last_fail and time.time() - _bs_last_fail < 120:
        raise RuntimeError("baostock 登录失败, 120s 内不重试")
    import baostock as bs
    with _bs_lock:
        if _bs_logged_in:
            return
        _bs_last_fail = time.time()
        try:
            lg = bs.login()
        except (OSError, ConnectionError) as exc:
            raise RuntimeError(f"baostock 网络错误: {exc}") from exc
        if lg.error_code != "0":
            raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")
        _bs_logged_in = True
        _bs_last_fail = 0.0
        logger.info("baostock 登录成功")


def _symbol_to_bs(symbol: str) -> str:
    """A股/ETF → baostock 代码: sh.510300 / sz.159915 / sh.600519"""
    if symbol.startswith(("43", "83", "87", "92", "8", "4")):
        return f"bj.{symbol}"
    if symbol.startswith(("6", "5", "9")):
        return f"sh.{symbol}"
    return f"sz.{symbol}"


def _to_float(v) -> float:
    try:
        f = float(v)
        return f if f == f else 0.0
    except (TypeError, ValueError):
        return 0.0


def _force_relogin():
    """baostock 连接异常后强制重新登录(长驻进程内连接会断,
    不重登录则后续查询全部"网络接收错误", 前端K线/分时只剩当天数据)。"""
    global _bs_logged_in, _bs_last_fail
    try:
        import baostock as bs
        with _bs_lock:
            try:
                bs.logout()
            except Exception:
                pass
            _bs_logged_in = False
            _bs_last_fail = 0.0
            lg = bs.login()
            if lg.error_code != "0":
                raise RuntimeError(f"baostock 重登录失败: {lg.error_msg}")
            _bs_logged_in = True
            logger.info("baostock 已强制重新登录")
    except Exception as exc:
        logger.warning("baostock 重登录失败: %s", exc)
        raise


def _query_retry(fn, *args, **kwargs):
    """查询封装: 失败(网络断连/登录失效)时强制重登录后重试一次。"""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        logger.warning("baostock 查询失败, 尝试重登录重试: %s", exc)
        _force_relogin()
        return fn(*args, **kwargs)


class BaostockClient(BaseDataSource):
    """Baostock: 历史日K(含当日) / 分钟K / 指数 / 交易日历。"""

    name = "baostock"

    # ------------------------------------------------------------------
    def get_daily_bars(self, symbol: str, start: date, end: date,
                       asset_type: str = "etf") -> List[Dict[str, Any]]:
        _ensure_login()
        import baostock as bs

        def _q():
            with _bs_lock:
                rs = bs.query_history_k_data_plus(
                    _symbol_to_bs(symbol),
                    "date,open,high,low,close,volume,amount",
                    start_date=start.strftime("%Y-%m-%d"),
                    end_date=end.strftime("%Y-%m-%d"),
                    frequency="d", adjustflag="2",   # 前复权
                )
                if rs.error_code != "0":
                    raise RuntimeError(f"baostock 日K失败 {symbol}: {rs.error_msg}")
                df = rs.get_data()
            if df is None or df.empty:
                raise RuntimeError(f"baostock 无 {symbol} 日K")
            return df

        df = _query_retry(_q)
        rows: List[Dict[str, Any]] = []
        for _, r in df.iterrows():
            try:
                d = datetime.strptime(str(r["date"])[:10], "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
            if not (start <= d <= end):
                continue
            rows.append({
                "symbol": symbol,
                "trade_date": d,
                "open": _to_float(r.get("open")),
                "high": _to_float(r.get("high")),
                "low": _to_float(r.get("low")),
                "close": _to_float(r.get("close")),
                "volume": _to_float(r.get("volume")),      # 股/份
                "amount": _to_float(r.get("amount")),
                "source": self.name,
            })
        return rows

    # ------------------------------------------------------------------
    def get_minute_bars(self, symbol: str, start: datetime, end: datetime,
                        freq: str = "5m", asset_type: str = "etf",
                        bs_code: Optional[str] = None) -> List[Dict[str, Any]]:
        _ensure_login()
        import baostock as bs
        bs_freq = {"1m": "5", "5m": "5", "15m": "15", "30m": "30", "60m": "60"}.get(freq, "5")
        code = bs_code or _symbol_to_bs(symbol)

        def _q():
            with _bs_lock:
                rs = bs.query_history_k_data_plus(
                    code,
                    "time,open,high,low,close,volume,amount",
                    start_date=start.strftime("%Y-%m-%d"),
                    end_date=end.strftime("%Y-%m-%d"),
                    frequency=bs_freq, adjustflag="2",
                )
                if rs.error_code != "0":
                    raise RuntimeError(f"baostock 分钟K失败 {symbol}: {rs.error_msg}")
                df = rs.get_data()
            if df is None or df.empty:
                raise RuntimeError(f"baostock 无 {symbol} 分钟K")
            return df

        df = _query_retry(_q)
        rows: List[Dict[str, Any]] = []
        for _, r in df.iterrows():
            try:
                t = datetime.strptime(str(r["time"]).strip(), "%Y%m%d%H%M%S%f")
            except (ValueError, TypeError):
                try:
                    t = datetime.strptime(str(r["time"]).strip()[:14], "%Y%m%d%H%M%S")
                except (ValueError, TypeError):
                    continue
            if not (start <= t <= end):
                continue
            rows.append({
                "symbol": symbol,
                "bar_time": t,
                "freq": freq,
                "open": _to_float(r.get("open")),
                "high": _to_float(r.get("high")),
                "low": _to_float(r.get("low")),
                "close": _to_float(r.get("close")),
                "volume": _to_float(r.get("volume")),
                "amount": _to_float(r.get("amount")),
                "source": self.name,
            })
        return rows

    # ------------------------------------------------------------------
    def get_index_bars(self, index_code: str, start: date, end: date) -> List[Dict[str, Any]]:
        """指数日K: 000300→sh.000300, 000001→sh.000001, 000905→sh.000905, 399006→sz.399006"""
        _ensure_login()
        import baostock as bs
        bs_code = f"sh.{index_code}" if index_code.startswith("000") else f"sz.{index_code}"

        def _q():
            with _bs_lock:
                rs = bs.query_history_k_data_plus(
                    bs_code,
                    "date,open,high,low,close,volume,amount",
                    start_date=start.strftime("%Y-%m-%d"),
                    end_date=end.strftime("%Y-%m-%d"),
                    frequency="d", adjustflag="3",   # 指数不复权
                )
                if rs.error_code != "0":
                    raise RuntimeError(f"baostock 指数失败 {index_code}: {rs.error_msg}")
                df = rs.get_data()
            if df is None or df.empty:
                raise RuntimeError(f"baostock 无指数 {index_code} 数据")
            return df

        df = _query_retry(_q)
        rows: List[Dict[str, Any]] = []
        for _, r in df.iterrows():
            try:
                d = datetime.strptime(str(r["date"])[:10], "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
            if not (start <= d <= end):
                continue
            rows.append({
                "symbol": index_code, "trade_date": d,
                "open": _to_float(r.get("open")),
                "high": _to_float(r.get("high")),
                "low": _to_float(r.get("low")),
                "close": _to_float(r.get("close")),
                "volume": _to_float(r.get("volume")),
                "amount": _to_float(r.get("amount")),
                "source": self.name,
            })
        return rows

    # ------------------------------------------------------------------
    def get_trade_calendar(self, start: date, end: date) -> List[date]:
        _ensure_login()
        import baostock as bs

        def _q():
            with _bs_lock:
                rs = bs.query_trade_dates(
                    start_date=start.strftime("%Y-%m-%d"),
                    end_date=end.strftime("%Y-%m-%d"))
                if rs.error_code != "0":
                    raise RuntimeError(f"baostock 交易日历失败: {rs.error_msg}")
                df = rs.get_data()
            if df is None or df.empty:
                raise RuntimeError("baostock 无交易日历")
            return df

        df = _query_retry(_q)
        dates = []
        for _, r in df.iterrows():
            if str(r.get("is_trading_day", "")).strip() != "1":
                continue
            try:
                dates.append(datetime.strptime(str(r["calendar_date"])[:10], "%Y-%m-%d").date())
            except (ValueError, TypeError):
                continue
        dates.sort()
        return [d for d in dates if start <= d <= end]
