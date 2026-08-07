# -*- coding: utf-8 -*-
"""
腾讯行情客户端 (CDN 直连, 极速无代理)
======================================
- 实时行情: qt.gtimg.cn/q=sh510300 (含名称/盘口字段, 绕过系统代理直连)
- 今日分时: web.ifzq.gtimg.cn/appstock/app/minute/query (1分钟价量, 交易时段实时)
- 当日日K: 用分时数据聚合合成(盘中即有当天K线)
- 五档盘口: qt.gtimg.cn 响应内置买卖五档字段

修复背景: 原 akshare/eastmoney 依赖东财接口, 用户网络环境走代理时
"Unable to connect to proxy" 导致数据获取几乎全部失败。腾讯接口
httpx.Client(proxy=None, trust_env=False) 直连, 稳定快速。
"""
import json
import logging
import re
import threading
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Dict, List, Optional

import httpx

from data_sources.base import BaseDataSource

logger = logging.getLogger("data.tencent")

# 全局复用客户端: proxy=None 绕过系统代理(修复代理导致的数据获取失败)
_tx_client = httpx.Client(proxy=None, trust_env=False, timeout=8)
_lock = threading.RLock()


def _tx_symbol(symbol: str) -> str:
    """sh510300 / sz159915"""
    if symbol.startswith(("6", "5", "9")):
        return "sh" + symbol
    return "sz" + symbol


def _in_trading_hours(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return (dtime(9, 25) <= t <= dtime(11, 30) or
            dtime(13, 0) <= t <= dtime(15, 5))


def _last_trading_date() -> date:
    """最近已完成的交易日(仅排除周末, 节假日略有偏差)。"""
    now = datetime.now()
    if now.weekday() < 5 and now.time() >= dtime(15, 5):
        return now.date()
    d = now.date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _safe_float(v, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if f == f else default
    except (TypeError, ValueError):
        return default


class TencentClient(BaseDataSource):
    """腾讯行情: 实时行情 / 今日分时分钟K / 当日日K合成 / 五档盘口。"""

    name = "tencent"

    # ------------------------------------------------------------------
    def get_realtime_quotes_batch(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """批量实时行情: qt.gtimg.cn/q=sh510300,sz159915 一次请求多只。
        返回 {symbol: quote_dict}; 网络/单只失败不影响整体, 缺失的代码跳过。
        修复: 盯盘/监控页列表原来用 akshare 全市场 spot 缓存 —— 服务器上
        该接口被东财限流后列表数据永久冻结; 改为腾讯批量实时行情(与详情页同源)。"""
        if not symbols:
            return {}
        code_map: Dict[str, str] = {}
        for s in symbols:
            code_map.setdefault(_tx_symbol(s), s)
        url = "http://qt.gtimg.cn/q=" + ",".join(code_map.keys())
        try:
            resp = _tx_client.get(url)
            text = resp.text
        except Exception as exc:
            raise RuntimeError(f"腾讯批量行情网络失败: {exc}") from exc
        out: Dict[str, Dict[str, Any]] = {}
        # 响应格式: v_sh510300="1~510300~沪深300ETF~3.540~...";\n
        for m in re.finditer(r'v_(\w+)="([^"]*)"', text):
            code = m.group(1)
            raw = m.group(2)
            symbol = code_map.get(code)
            if not symbol or not raw.strip():
                continue
            parts = raw.split("~")
            if len(parts) < 35:
                continue
            price = _safe_float(parts[3])
            prev_close = _safe_float(parts[4])
            if price <= 0:
                price = prev_close
            if price <= 0:
                continue
            out[symbol] = {
                "symbol": symbol,
                "name": parts[1].strip() if len(parts) > 1 else "",
                "quote_time": datetime.now(),
                "latest_price": price,
                "change_pct": _safe_float(parts[32]),
                "volume": _safe_float(parts[6]) * 100,
                "amount": 0.0,
                "high": _safe_float(parts[33]),
                "low": _safe_float(parts[34]),
                "open": _safe_float(parts[5]),
                "prev_close": prev_close,
                "source": self.name,
            }
        return out

    # ------------------------------------------------------------------
    def get_realtime_quote(self, symbol: str, asset_type: str = "etf",
                           tx_code: Optional[str] = None) -> Dict[str, Any]:
        """qt.gtimg.cn 实时行情。
        字段(0-based, split '~'): 1=名称 3=最新 4=昨收 5=今开 6=成交量(手)
        30=时间戳(YYYYMMDDHHmmss) 31=涨额 32=涨跌幅% 33=最高 34=最低
        tx_code: 指数等需要显式指定代码(sh000001/sz399006)时传入(默认按代码推断)。"""
        code = tx_code or _tx_symbol(symbol)
        try:
            resp = _tx_client.get(f"http://qt.gtimg.cn/q={code}")
            text = resp.text
        except Exception as exc:
            raise RuntimeError(f"腾讯行情网络失败 {symbol}: {exc}") from exc
        m = re.search(r'"([^"]*)"', text)
        if not m or not m.group(1).strip():
            raise RuntimeError(f"腾讯无 {symbol} 行情")
        parts = m.group(1).split("~")
        if len(parts) < 35:
            raise RuntimeError(f"腾讯 {symbol} 行情字段不足")
        price = _safe_float(parts[3])
        prev_close = _safe_float(parts[4])
        if price <= 0:
            price = prev_close
        if price <= 0:
            raise RuntimeError(f"腾讯 {symbol} 行情价格为0")
        quote_time = datetime.now()
        # 盘口: 9~18=买一价量..买五价量, 19~28=卖一价量..卖五价量
        bid1, bid_vol1 = _safe_float(parts[9]), _safe_float(parts[10])
        ask1, ask_vol1 = _safe_float(parts[19]), _safe_float(parts[20])
        return {
            "symbol": symbol,
            "name": parts[1].strip() if len(parts) > 1 else "",
            "quote_time": quote_time,
            "latest_price": price,
            "change_pct": _safe_float(parts[32]),      # 涨跌幅%
            "volume": _safe_float(parts[6]) * 100,     # 手→股
            "amount": 0.0,
            "high": _safe_float(parts[33]),
            "low": _safe_float(parts[34]),
            "open": _safe_float(parts[5]),
            "prev_close": prev_close,
            "bid1": bid1, "ask1": ask1,
            "bid_vol1": bid_vol1, "ask_vol1": ask_vol1,
            "source": self.name,
        }

    # ------------------------------------------------------------------
    def get_order_book(self, symbol: str, asset_type: str = "etf") -> Dict[str, Any]:
        """五档盘口(腾讯行情字段 9~28: 买一~买五价量, 卖一~卖五价量)。"""
        code = _tx_symbol(symbol)
        try:
            resp = _tx_client.get(f"http://qt.gtimg.cn/q={code}")
            text = resp.text
        except Exception as exc:
            raise RuntimeError(f"腾讯盘口网络失败 {symbol}: {exc}") from exc
        m = re.search(r'"([^"]*)"', text)
        if not m or not m.group(1).strip():
            raise RuntimeError(f"腾讯无 {symbol} 盘口")
        parts = m.group(1).split("~")
        if len(parts) < 29:
            raise RuntimeError(f"腾讯 {symbol} 盘口字段不足")
        # 腾讯: 买一价=9 买一量=10, 卖一价=19 卖一量=20 (按 5 价量交替)
        bids, asks = [], []
        for i in range(5):
            p, v = _safe_float(parts[9 + i * 2]), _safe_float(parts[10 + i * 2])
            ap, av = _safe_float(parts[19 + i * 2]), _safe_float(parts[20 + i * 2])
            if p > 0:
                bids.append({"price": p, "volume": v * 100})
            if ap > 0:
                asks.append({"price": ap, "volume": av * 100})
        bid1 = bids[0]["price"] if bids else 0.0
        ask1 = asks[0]["price"] if asks else 0.0
        spread = (ask1 - bid1) / ask1 if ask1 > 0 else 0.0
        return {
            "symbol": symbol,
            "snapshot_time": datetime.now(),
            "bid1": bid1,
            "ask1": ask1,
            "bid_vol1": bids[0]["volume"] if bids else 0.0,
            "ask_vol1": asks[0]["volume"] if asks else 0.0,
            "spread": spread,
            "order_book_json": {"买盘": bids, "卖盘": asks},
            "source": self.name,
        }

    # ------------------------------------------------------------------
    def _minute_rows(self, symbol: str, tx_code: Optional[str] = None) -> List[tuple]:
        """腾讯分时: 返回 [(datetime, price, vol)] 1分钟价量列表(全天可用)。
        tx_code: 指数(sh000001)等显式代码。"""
        code = tx_code or _tx_symbol(symbol)
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/minute/query"
               f"?_var=min_data_{code}&code={code}")
        try:
            resp = _tx_client.get(url)
            text = resp.text
        except Exception as exc:
            logger.debug("腾讯分时网络失败 %s: %s", symbol, exc)
            return []
        m = re.search(r'=(\{.*\})', text, re.DOTALL)
        if not m:
            return []
        try:
            obj = json.loads(m.group(1))
            data_node = obj["data"][code]["data"]
            date_str = data_node.get("date", "")
            raw = data_node.get("data", [])
        except (KeyError, TypeError, ValueError):
            return []
        if len(date_str) != 8:
            return []
        try:
            y, mo, d = int(date_str[:4]), int(date_str[4:6]), int(date_str[6:])
        except ValueError:
            return []
        rows = []
        prev_vol = 0.0
        for item in raw:
            parts = item.split()
            if len(parts) < 3:
                continue
            t_str = parts[0]
            if len(t_str) != 4:
                continue
            try:
                price = float(parts[1])
                cum_vol = float(parts[2])
                vol = max(0.0, cum_vol - prev_vol)
                prev_vol = cum_vol
                dt = datetime(y, mo, d, int(t_str[:2]), int(t_str[2:]), 0)
                rows.append((dt, price, vol))
            except (ValueError, IndexError):
                continue
        return rows

    # ------------------------------------------------------------------
    def get_minute_bars(self, symbol: str, start: datetime, end: datetime,
                        freq: str = "5m", asset_type: str = "etf",
                        tx_code: Optional[str] = None) -> List[Dict[str, Any]]:
        """今日分时 → N分钟K线重采样(盘中实时; 非交易日/休市返回空由备源兜底)。
        tx_code: 指数等需要显式指定代码(sh000001/sz399006)时传入。"""
        rule_map = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}
        period = rule_map.get(freq, 5)
        rows = self._minute_rows(symbol, tx_code=tx_code)
        if not rows:
            raise RuntimeError(f"腾讯无 {symbol} 今日分时")
        rows = [(dt, p, v) for dt, p, v in rows if start <= dt <= end]
        if not rows:
            return []
        # 按 period 分钟聚合 OHLC
        buckets: Dict[datetime, Dict[str, Any]] = {}
        for dt, price, vol in rows:
            aligned = dt.replace(minute=dt.minute // period * period, second=0, microsecond=0)
            b = buckets.setdefault(aligned, {"open": price, "high": price,
                                             "low": price, "close": price,
                                             "volume": 0.0, "amount": 0.0})
            b["high"] = max(b["high"], price)
            b["low"] = min(b["low"], price)
            b["close"] = price
            b["volume"] += vol
        out = []
        for t in sorted(buckets):
            b = buckets[t]
            out.append({
                "symbol": symbol,
                "bar_time": t,
                "freq": freq,
                "open": round(b["open"], 4),
                "high": round(b["high"], 4),
                "low": round(b["low"], 4),
                "close": round(b["close"], 4),
                "volume": round(b["volume"], 0),
                "amount": 0.0,
                "source": self.name,
            })
        return out

    # ------------------------------------------------------------------
    def get_daily_bars(self, symbol: str, start: date, end: date,
                       asset_type: str = "etf") -> List[Dict[str, Any]]:
        """当日日K合成: 用今日分时聚合一根当日K线(盘中即有当天数据)。
        历史日K由 baostock 主源提供, 本方法仅在请求区间包含当日时补充当日一根。"""
        today = datetime.now().date()
        if not (start <= today <= end):
            raise RuntimeError("腾讯日K仅支持当日合成")
        rows = self._minute_rows(symbol)
        if not rows:
            raise RuntimeError(f"腾讯无 {symbol} 今日分时, 无法合成日K")
        opens = [r[1] for r in rows if r[1] > 0]
        if not opens:
            raise RuntimeError(f"腾讯 {symbol} 当日价格为0")
        first = rows[0]
        price = first[1]
        hi = max(r[1] for r in rows)
        lo = min(r[1] for r in rows)
        vol = sum(r[2] for r in rows)
        prev_close = self._prev_close(symbol)
        change_pct = (price / prev_close - 1) * 100 if prev_close > 0 else 0.0
        return [{
            "symbol": symbol,
            "trade_date": today,
            "open": round(opens[0], 4),
            "high": round(hi, 4),
            "low": round(lo, 4),
            "close": round(price, 4),
            "volume": round(vol, 0),
            "amount": 0.0,
            "change_pct": round(change_pct, 4),
            "source": self.name,
            "is_live": True,
        }]

    def _prev_close(self, symbol: str) -> float:
        try:
            q = self.get_realtime_quote(symbol)
            return float(q.get("prev_close", 0) or 0)
        except Exception:
            return 0.0

    # ------------------------------------------------------------------
    def get_etf_spot(self) -> List[Dict[str, Any]]:
        """批量行情(多代码一次请求)。供前端批量盯盘, 空代码列表返回空。"""
        raise NotImplementedError("tencent 不提供全市场ETF列表(用 akshare 磁盘缓存)")
