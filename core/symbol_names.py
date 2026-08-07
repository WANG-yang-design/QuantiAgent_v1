# -*- coding: utf-8 -*-
"""
标的名称解析器 (股票/ETF 中文名)
================================
前端多处只显示代码看不到中文名(监控标的/决策链路/搜索页), 根因是各表
名称字段缺失且无人补全。本模块提供统一解析入口, 带进程内 TTL 缓存:

解析顺序:
  1. DB: symbols 表 / watchlist 表 / trade_plans 表(最近计划名)
  2. 全市场 ETF 现货列表(东财磁盘缓存, 名称即全量)
  3. baostock 个股基础信息(轻量, 股票名)
  4. akshare 全市场股票实时列表(兜底, 慢且限流, 有1小时缓存)
"""
import logging
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger("core.symbol_names")

_CACHE_TTL = 6 * 3600          # 名称 6 小时刷新一次
_cache: Dict[str, Tuple[float, str]] = {}
_stock_spot_cache: Optional[Dict[str, str]] = None   # symbol -> name (akshare兜底)
_stock_spot_ts: float = 0.0
_STOCK_SPOT_TTL = 3600.0

# 指数代码(修复: 000001 等既是指数又是股票代码, 必须先判指数,
# 否则 000001 会被解析成"平安银行", 加入自选/名称显示全部错位)
INDEX_CODES = {"000001", "000300", "000905", "399006"}
INDEX_NAMES = {"000001": "上证指数", "000300": "沪深300",
               "000905": "中证500", "399006": "创业板指"}


def infer_asset_type(symbol: str) -> str:
    """按代码段推断资产类型(stock/etf/index)。"""
    symbol = str(symbol or "")
    if symbol in INDEX_CODES:
        return "index"
    if symbol[:1] in ("6", "0", "3", "4", "8"):
        return "stock"
    return "etf"


def _db_lookup(symbol: str) -> Optional[str]:
    """查库: symbols → watchlist → trade_plans(最近一次的计划名称)。"""
    from database import repository as repo
    try:
        sy = repo.get_symbol(symbol)
        if sy and sy.name:
            return sy.name
    except Exception:
        pass
    try:
        for w in repo.get_watchlist():
            if w["symbol"] == symbol and w.get("name"):
                return w["name"]
    except Exception:
        pass
    try:
        from database.models import TradePlan
        from database.db_session import get_session
        from sqlalchemy import desc
        with get_session() as s:
            row = s.query(TradePlan).filter_by(symbol=symbol).filter(
                TradePlan.name != "").order_by(desc(TradePlan.created_at)).first()
            if row and row.name:
                return row.name
    except Exception:
        pass
    return None


def _etf_spot_lookup(symbol: str) -> Optional[str]:
    try:
        from data_service.market_data_service import get_market_service
        spot = get_market_service().get_etf_spot()
        for s in spot:
            if s.get("symbol") == symbol and s.get("name"):
                return s["name"]
    except Exception:
        pass
    return None


def _quote_lookup(symbol: str) -> Optional[str]:
    """腾讯实时行情带中文名(股票/ETF通用, 轻量直连)。"""
    try:
        from data_service.market_data_service import get_market_service
        q, _ = get_market_service().get_realtime_quote(symbol, "etf")
        name = (q or {}).get("name", "") or ""
        return name if name.strip() and not name.startswith("v_") else None
    except Exception:
        return None


def _baostock_lookup(symbol: str) -> Optional[str]:
    """baostock 个股基础信息(股票代码 sh.600519 / sz.000001, 需带点)。"""
    try:
        import baostock as bs
        from data_sources.baostock_client import _ensure_login
        _ensure_login()
        code = ("sh." if symbol.startswith("6") else "sz.") + symbol
        rs = bs.query_stock_basic(code=code)
        if rs.error_code != "0":
            return None
        while rs.next():
            row = rs.get_row_data()
            # fields: code, code_name, ipoDate, outDate, type, status
            if len(row) >= 2 and row[1]:
                return row[1]
    except Exception as exc:
        logger.debug("baostock 名称查询失败 %s: %s", symbol, exc)
    return None


def _akshare_spot_lookup(symbol: str) -> Optional[str]:
    """akshare 全市场A股实时列表(慢, 兜底, 1小时进程内缓存)。"""
    global _stock_spot_cache, _stock_spot_ts
    now = time.time()
    if _stock_spot_cache is None or now - _stock_spot_ts > _STOCK_SPOT_TTL:
        try:
            import akshare as ak
            df = ak.stock_zh_a_spot_em()
            _stock_spot_cache = {
                str(r["代码"]): str(r["名称"]) for _, r in df.iterrows()}
            _stock_spot_ts = now
            logger.info("akshare 全市场股票名称缓存刷新: %d 只", len(_stock_spot_cache))
        except Exception as exc:
            logger.warning("akshare 股票列表获取失败: %s", exc)
            _stock_spot_cache = {}
            _stock_spot_ts = now
    return _stock_spot_cache.get(symbol)


def resolve_symbol_name(symbol: str) -> str:
    """解析标的中文名(带 TTL 缓存), 失败返回空串。"""
    symbol = str(symbol or "").upper()
    if not symbol:
        return ""
    if symbol in INDEX_NAMES:
        return INDEX_NAMES[symbol]
    hit = _cache.get(symbol)
    if hit and hit[0] > time.time():
        return hit[1]
    name = _db_lookup(symbol) or _etf_spot_lookup(symbol) \
        or _quote_lookup(symbol) or _baostock_lookup(symbol) \
        or _akshare_spot_lookup(symbol) or ""
    _cache[symbol] = (time.time() + _CACHE_TTL, name)
    if name and len(_cache) > 4096:
        # 防内存无限增长: 清理过期项
        expired = [k for k, v in _cache.items() if v[0] <= time.time()]
        for k in expired:
            _cache.pop(k, None)
    return name


def resolve_symbol_info(symbol: str) -> Dict[str, str]:
    """一次解析出 {name, asset_type}, 供列表/详情接口批量使用。"""
    return {"symbol": symbol,
            "name": resolve_symbol_name(symbol),
            "asset_type": infer_asset_type(symbol)}
