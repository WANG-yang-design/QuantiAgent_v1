# -*- coding: utf-8 -*-
"""
Web API V2: 行情盯盘 / 标的详情 / K线 / 新闻公告 / 异步回测 / 工作流trace / 运行模式
===============================================================================
供前端(React)调用, 全部封装现有 service/repository, 不涉及核心逻辑。
"""
import json
import logging
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException

from core.config import get_settings
from database import repository as repo
from database.db_session import get_session
from data_service.market_data_service import get_market_service
from data_service.news_service import get_news_service
from features.technical_indicators import compute_technical_features, to_frame

logger = logging.getLogger("web.v2")

router = APIRouter(prefix="/api")

# ---------------------------------------------------------------
# JSON 安全清洗: 数据源异常(单根K线/字段缺失)会让技术指标算出 NaN/Inf,
# FastAPI 序列化时直接 500("Out of range float values are not JSON compliant")。
# 所有透传外部数据的响应在返回前统一清洗(修复: /api/symbol 偶发 500)。
# ---------------------------------------------------------------
def _json_safe(obj):
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    try:
        import numpy as np
        if isinstance(obj, (np.floating, np.integer, np.bool_)):
            return obj.item()
    except Exception:
        pass
    return obj


def _clean(payload):
    """返回 JSON 安全的深拷贝。"""
    return _json_safe(payload)

# ---------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------
def _check_token(authorization: Optional[str] = Header(None)):
    import hmac
    token = get_settings().get("web.admin_token", "quantiagent-admin")
    if not authorization or not hmac.compare_digest(authorization, f"Bearer {token}"):
        raise HTTPException(status_code=401, detail="无效令牌")


def require_auth(authorization: Optional[str] = Header(None)):
    _check_token(authorization)


# ================================================================
# 1. 批量实时行情(盯盘轮询, 用全市场spot缓存过滤, 避免逐只请求)
# ================================================================
@router.get("/quotes", dependencies=[Depends(require_auth)])
def get_quotes(symbols: str = "", limit: int = 100):
    """批量行情: /api/quotes?symbols=510300,159915 或 不传symbols返回成交额Top。
    修复: 原实现只过滤 ETF 现货缓存 —— 自选池里的股票(6/0/3开头)永远查不到
    行情和中文名, 且现货列表(akshare)在服务器上被东财限流后长期冻结, 盯盘/
    监控页永远显示旧价。显式请求的标的改为走腾讯批量实时行情(与详情页同源),
    现货缓存只作为兜底/名称补充。"""
    svc = get_market_service()
    spot = svc.get_etf_spot()
    want = [s.strip().upper() for s in symbols.split(",") if s.strip()] if symbols else []
    if want:
        # 实时行情优先(修复: 现货缓存可能冻结/缺失, 列表不再用旧价)
        # 注意: 指数代码(000001等)不能用批量接口(会被当股票), 走逐只显式代码
        idx_want = [s for s in want if s in _INDEX_CODES]
        etf_want = [s for s in want if s not in _INDEX_CODES]
        try:
            from data_sources.tencent_client import TencentClient
            live = TencentClient().get_realtime_quotes_batch(etf_want) if etf_want else {}
            for sym in idx_want:
                try:
                    q = TencentClient().get_realtime_quote(
                        sym, "etf", tx_code=_INDEX_TX[sym])
                    if q and q.get("latest_price"):
                        live[sym] = q
                except Exception as exc:
                    logger.debug("指数行情获取失败 %s: %s", sym, exc)
        except Exception as exc:
            logger.warning("批量实时行情获取失败, 回退现货缓存: %s", exc)
            live = {}
        spot_map = {s.get("symbol"): s for s in spot}
        rows = []
        for sym in want:
            q = live.get(sym)
            if q and q.get("latest_price"):
                rows.append(q)
            elif sym in spot_map:
                rows.append(spot_map[sym])
            else:
                # 现货未命中(股票等): 逐只回退
                try:
                    q, _ = svc.get_realtime_quote(sym, "etf")
                    if not q or not q.get("latest_price"):
                        continue
                    rows.append(q)
                except Exception as exc:
                    logger.debug("行情回退获取失败 %s: %s", sym, exc)
        rows = rows[:limit]
    else:
        rows = sorted(spot, key=lambda x: x.get("amount", 0) or 0, reverse=True)[:limit]
        # 成交额Top也刷新为实时行情(修复: 现货缓存冻结时 Top 列表同样旧价)
        try:
            from data_sources.tencent_client import TencentClient
            live = TencentClient().get_realtime_quotes_batch(
                [r.get("symbol", "") for r in rows if r.get("symbol")])
            for r in rows:
                q = live.get(r.get("symbol"))
                if q and q.get("latest_price"):
                    r["latest_price"] = q.get("latest_price")
                    r["change_pct"] = q.get("change_pct", r.get("change_pct", 0))
                    r["volume"] = q.get("volume", r.get("volume", 0) or 0)
        except Exception as exc:
            logger.debug("Top列表实时行情刷新失败(沿用现货): %s", exc)
    from core.symbol_names import resolve_symbol_name, INDEX_CODES as _IDX
    out = []
    for s in rows:
        chg = float(s.get("change_pct", 0) or 0)
        name = s.get("name", "") or resolve_symbol_name(s.get("symbol", ""))
        sym = str(s.get("symbol", ""))
        if sym in _IDX:
            atype = "index"
        elif sym[:1] in ("6", "0", "3", "4", "8"):
            atype = "stock"
        else:
            atype = "etf"
        out.append({
            "symbol": sym,
            "name": name,
            "asset_type": atype,
            "latest_price": s.get("latest_price", 0),
            "change_pct": chg,
            "amount": s.get("amount", 0) or 0,
            "volume": s.get("volume", 0) or 0,
            "premium_rate": s.get("premium_rate", 0) or 0,
            "iopv": s.get("iopv", 0) or 0,
            "color": "up" if chg > 0.05 else ("down" if chg < -0.05 else "flat"),
            "quote_time": datetime.now().strftime("%H:%M:%S"),
        })
    return {"quotes": out, "total": len(out), "time": datetime.now().strftime("%H:%M:%S")}


# ================================================================
# 2. K线(蜡烛图+均线+成交量), 支持指数代码(000001/000300/000905/399006)
# ================================================================
_INDEX_CODES = {"000001", "000300", "000905", "399006"}


@router.get("/kline/{symbol}", dependencies=[Depends(require_auth)])
def get_kline(symbol: str, days: int = 250):
    end = date.today()
    start = end - timedelta(days=int(days * 1.6))
    if symbol in _INDEX_CODES:
        # 指数K线(新浪源)
        bars = get_market_service().get_index_bars(symbol, start, end)
        quality = "VALID"
    else:
        bars, rep = get_market_service().get_daily_bars(symbol, start, end, "etf")
        quality = rep.status
    df = to_frame(bars)
    if df.empty:
        # 修复: 实时源(腾讯当日合成)只能给1根K线; 主源故障时回退分钟K库/日K库,
        # 避免"日K图只剩当天一根"。
        try:
            from database.models import DailyBar
            with get_session() as s:
                rows = s.query(DailyBar).filter(
                    DailyBar.symbol == symbol,
                    DailyBar.trade_date >= start,
                    DailyBar.trade_date <= end,
                ).order_by(DailyBar.trade_date).all()
            if rows:
                df = to_frame([{
                    "symbol": r.symbol, "trade_date": r.trade_date,
                    "open": r.open, "high": r.high, "low": r.low,
                    "close": r.close, "volume": r.volume, "amount": r.amount,
                } for r in rows])
        except Exception as exc:
            logger.debug("日K库回退失败 %s: %s", symbol, exc)
    if df.empty:
        raise HTTPException(status_code=404, detail=f"无 {symbol} K线数据(先执行 fetch-daily)")
    # 均线
    for w in (5, 20, 60):
        df[f"ma{w}"] = df["close"].rolling(w).mean()
    candles = []
    for _, r in df.iterrows():
        candles.append({
            "date": str(r["trade_date"].date() if hasattr(r["trade_date"], "date") else r["trade_date"])[:10],
            "open": round(float(r["open"]), 4),
            "high": round(float(r["high"]), 4),
            "low": round(float(r["low"]), 4),
            "close": round(float(r["close"]), 4),
            "volume": float(r["volume"]),
            "amount": float(r["amount"]),
            "ma5": round(float(r["ma5"]), 4) if r["ma5"] == r["ma5"] else None,
            "ma20": round(float(r["ma20"]), 4) if r["ma20"] == r["ma20"] else None,
            "ma60": round(float(r["ma60"]), 4) if r["ma60"] == r["ma60"] else None,
            "is_live": False,
        })
    # 当日K线实时化: 若最后一根K线就是今天, 用实时行情覆盖收盘价/最高/最低
    # (修复: 盘中日K可能止于昨日/数据源当日K线滞后, K线图看不到当天)
    # (修复: 指数代码也被排除在实时化之外, 四个指数K线永远停在昨天 ——
    #  指数同样用腾讯实时行情(sh000001/sz399006 显式代码)刷新当日K线)
    if candles:
        last_date = candles[-1]["date"]
        today_str = date.today().isoformat()
        if last_date < today_str:
            try:
                if symbol in _INDEX_CODES:
                    from data_sources.tencent_client import TencentClient
                    q = TencentClient().get_realtime_quote(
                        symbol, "etf", tx_code=_INDEX_TX[symbol])
                else:
                    q, qrep = get_market_service().get_realtime_quote(symbol, "etf")
                price = float((q or {}).get("latest_price", 0) or 0)
                if price > 0:
                    candles.append({
                        "date": today_str,
                        "open": float(q.get("open", 0) or 0) or price,
                        "high": max(float(q.get("high", 0) or 0), price),
                        "low": min(float(q.get("low", 0) or 0) or price, price),
                        "close": price,
                        "volume": float(q.get("volume", 0) or 0),
                        "amount": float(q.get("amount", 0) or 0),
                        "ma5": None, "ma20": None, "ma60": None,
                        "is_live": True,
                    })
            except Exception as exc:
                logger.debug("当日K线实时化失败 %s: %s", symbol, exc)
        else:
            # 最后一根就是今天: 用实时行情刷新 close/high/low(盘中数据更实时)
            try:
                if symbol in _INDEX_CODES:
                    from data_sources.tencent_client import TencentClient
                    q = TencentClient().get_realtime_quote(
                        symbol, "etf", tx_code=_INDEX_TX[symbol])
                else:
                    q, _ = get_market_service().get_realtime_quote(symbol, "etf")
                price = float((q or {}).get("latest_price", 0) or 0)
                if price > 0:
                    c = candles[-1]
                    c["close"] = round(price, 4)
                    c["high"] = round(max(c["high"], price), 4)
                    if c["low"] == 0 or price < c["low"]:
                        c["low"] = round(price, 4)
                    c["is_live"] = True
            except Exception as exc:
                logger.debug("当日K线刷新失败 %s: %s", symbol, exc)
    return _clean({"symbol": symbol, "quality": quality, "is_index": symbol in _INDEX_CODES,
                   "candles": candles})


# ================================================================
# 2.5 分时图(多日: 今日1分钟腾讯实时 + 历史5分钟 baostock/DB)
# ================================================================
@router.get("/intraday/{symbol}", dependencies=[Depends(require_auth)])
def get_intraday(symbol: str, days: int = 5):
    """多日分时: 返回最近 N 个交易日的分时数据, 每日独立昨收/均价线。
    今日: 腾讯1分钟(实时); 历史: 先查分钟K库, 缺失用 baostock 5分钟补齐并落库。
    days 取 1~10。非交易时段今日可能无数据(仅返回历史日)。"""
    from core.symbol_names import resolve_symbol_name
    svc = get_market_service()
    days = min(max(int(days), 1), 10)

    # 最近 N 个交易日
    trade_days = svc.get_trade_calendar(
        date.today() - timedelta(days=days * 2 + 5), date.today())
    trade_days = [d for d in trade_days if d <= date.today()]
    if len(trade_days) > days:
        trade_days = trade_days[-days:]
    if not trade_days:
        trade_days = [date.today()]

    # 历史日K(算每日昨收; 指数用指数日K, 否则 000001 会被当成股票平安银行)
    d_end = date.today()
    d_start = trade_days[0] - timedelta(days=20)
    dmap = {}
    try:
        if symbol in _INDEX_CODES:
            dbars = svc.get_index_bars(symbol, d_start, d_end)
            closes = [float(b["close"] or 0) for b in dbars]
            dates = [b["trade_date"] for b in dbars]
        else:
            dbars, _ = svc.get_daily_bars(symbol, d_start, d_end, "etf")
            closes = [float(b["close"] or 0) for b in dbars]
            dates = [b["trade_date"] for b in dbars]
        for i, d in enumerate(dates):
            dmap[str(d)] = closes[i - 1] if i > 0 else 0.0
    except Exception:
        pass

    # 今日: 腾讯 1 分钟(实时; 指数用显式 sh000001/sz399006 代码)
    # 注: 腾讯分时量单位为"手"(×100=股), 历史(新浪/baostock)为"股",
    # 统一为股后再聚合成图, 否则今天的量柱只有历史的千分之一(几乎看不见)。
    today_points = []
    now = datetime.now()
    if date.today() == trade_days[-1]:
        try:
            if symbol in _INDEX_CODES:
                from data_sources.tencent_client import TencentClient
                rows = TencentClient().get_minute_bars(
                    symbol, datetime.combine(now.date(), datetime.min.time()),
                    now, "1m", "etf", tx_code=_INDEX_TX[symbol])
            else:
                bars, _ = svc.get_minute_bars(symbol,
                                              datetime.combine(now.date(), datetime.min.time()),
                                              now, "1m", "etf")
                rows = bars
            today_points = _to_points(rows, vol_scale=100)   # 手→股
        except Exception as exc:
            logger.debug("今日分时获取失败 %s: %s", symbol, exc)

    # 历史: 分钟K库(1m优先, 5m兜底) → 新浪5m(指数/ETF通用, 免费直连) → baostock
    history_points = []
    if len(trade_days) > 1:
        past = trade_days[:-1]
        start_dt = datetime.combine(past[0], datetime.min.time())
        end_dt = datetime.combine(past[-1], datetime.max.time())
        for freq in ("1m", "5m"):
            try:
                rows = repo.get_minute_bars(symbol, start_dt, end_dt, freq)
                if rows:
                    history_points = [{"bar_time": r.bar_time, "price": r.close,
                                       "volume": r.volume} for r in rows]
                    break
            except Exception:
                pass
        if not history_points:
            try:
                from data_sources.sina_client import SinaClient
                # 新浪代码格式: sh000001 / sz399006 / sh510300(不带点)
                if symbol in _INDEX_CODES:
                    sina_code = _INDEX_TX[symbol]
                else:
                    sina_code = ("sh" if symbol.startswith(("5", "6", "9")) else "sz") + symbol
                rows = SinaClient().get_hist_minute_bars(sina_code, scale=5,
                                                         datalen=days * 50)
                if rows:
                    history_points = [{"bar_time": r["bar_time"], "price": r["close"],
                                       "volume": r["volume"]} for r in rows
                                      if start_dt <= r["bar_time"] <= end_dt]
                    try:
                        repo.upsert_minute_bars(rows)
                    except Exception:
                        pass
            except Exception as exc:
                logger.debug("新浪历史分钟K获取失败 %s: %s", symbol, exc)
        if not history_points:
            try:
                from data_sources.baostock_client import BaostockClient
                rows = BaostockClient().get_minute_bars(
                    symbol, start_dt, end_dt, "5m", "etf",
                    bs_code=_INDEX_BS.get(symbol) if symbol in _INDEX_CODES else None)
                if rows:
                    history_points = [{"bar_time": r["bar_time"], "price": r["close"],
                                       "volume": r["volume"]} for r in rows]
                    try:
                        repo.upsert_minute_bars(rows)
                    except Exception:
                        pass
            except Exception as exc:
                logger.debug("历史分钟K获取失败 %s: %s", symbol, exc)

    # 按日聚合
    day_map = {}
    for p in today_points:
        day_map.setdefault(str(p["bar_time"].date()), []).append(p)
    for p in history_points:
        day_map.setdefault(str(p["bar_time"].date()), []).append(p)

    result_days = []
    for d in trade_days:
        key = str(d)
        pts = day_map.get(key, [])
        if not pts:
            continue
        pts.sort(key=lambda x: x["bar_time"])
        prev = dmap.get(key) or (pts[0]["price"] if pts else 0)
        points, _last = _build_day_points(pts)
        result_days.append({"date": key, "prev_close": round(prev, 4),
                            "points": points})
    if not result_days:
        raise HTTPException(status_code=404,
                            detail=f"{symbol} 无分时数据(非交易时段或数据源不可用)")
    last_price = result_days[-1]["points"][-1]["price"]
    prev_close = result_days[-1]["prev_close"]
    return _clean({
        "symbol": symbol,
        "name": resolve_symbol_name(symbol),
        "days": result_days,
        "latest_price": last_price,
        "change_pct": round((last_price / prev_close - 1) * 100, 2) if prev_close else 0.0,
        "time": datetime.now().strftime("%H:%M:%S"),
    })


def _to_points(bars, vol_scale: float = 1.0):
    """原始分钟K → 统一结构(vol_scale: 量单位换算, 腾讯"手"→股 乘100)。"""
    out = []
    for b in bars:
        t = b.get("bar_time")
        if isinstance(t, str):
            try:
                t = datetime.strptime(t[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
        out.append({"bar_time": t, "price": float(b.get("close", 0) or 0),
                    "volume": float(b.get("volume", 0) or 0) * vol_scale})
    return out


def _build_day_points(rows):
    """单日: 计算均价线(VWAP, 腾讯无成交额用 Σ价×量/Σ量 近似)。"""
    cum_vol = 0.0
    cum_turn = 0.0
    points = []
    for r in rows:
        price = float(r.get("price", 0) or 0)
        vol = float(r.get("volume", 0) or 0)
        if price <= 0:
            continue
        cum_vol += vol
        cum_turn += price * vol
        points.append({
            "time": str(r["bar_time"])[11:16],
            "price": round(price, 4),
            "avg": round(cum_turn / cum_vol, 4) if cum_vol else round(price, 4),
            "volume": round(vol, 0),
        })
    return points, (points[-1]["price"] if points else 0)


# ================================================================
# 3. 标的详情(行情/盘口/技术指标/ETF/新闻/公告)
# ================================================================
_symbol_cache: Dict[str, tuple] = {}   # symbol -> (expire_ts, data)
_SYMBOL_CACHE_TTL = 20.0               # 详情整体缓存20秒(东财限流时避免接口超时)


_INDEX_TX = {"000001": "sh000001", "000300": "sh000300", "000905": "sh000905",
             "399006": "sz399006"}
_INDEX_BS = {"000001": "sh.000001", "000300": "sh.000300", "000905": "sh.000905",
             "399006": "sz.399006"}


@router.get("/symbol/{symbol}", dependencies=[Depends(require_auth)])
def get_symbol_detail(symbol: str):
    now = time.time()
    hit = _symbol_cache.get(symbol)
    if hit and hit[0] > now:
        return hit[1]
    svc = get_market_service()
    # 指数代码 → 指数详情(简化: 无盘口/ETF/新闻)
    if symbol in _INDEX_CODES:
        bars = svc.get_index_bars(symbol, date.today() - timedelta(days=400), date.today())
        tech = compute_technical_features(bars) if bars else {}
        # 修复: 原实现用历史日K的收盘价当"最新价"(索引详情永远显示昨天收盘)。
        # 指数用腾讯实时行情(sh000001/sz399006 显式代码, 避免被当成股票代码)。
        quote = {"symbol": symbol, "latest_price": tech.get("close", 0),
                 "change_pct": tech.get("change_pct", 0), "quote_time": datetime.now()}
        try:
            from data_sources.tencent_client import TencentClient
            q = TencentClient().get_realtime_quote(symbol, "etf",
                                                   tx_code=_INDEX_TX[symbol])
            if q and float(q.get("latest_price", 0) or 0) > 0:
                quote = q
        except Exception as exc:
            logger.debug("指数实时行情获取失败 %s: %s", symbol, exc)
        name = {"000001": "上证指数", "000300": "沪深300", "000905": "中证500",
                "399006": "创业板指"}.get(symbol, symbol)
        data = {
            "symbol": symbol, "name": name, "is_index": True,
            "quote": quote, "quote_quality": "VALID",
            "order_book": {}, "order_book_quality": "none",
            "etf_info": {}, "technical": {k: v for k, v in tech.items()
                                          if not isinstance(v, (list, dict))},
            "news": [], "announcements": [],
            "time": datetime.now().strftime("%H:%M:%S"),
        }
        data = _clean(data)
        _symbol_cache[symbol] = (now + _SYMBOL_CACHE_TTL, data)
        return data
    # 修复: 原实现串行调用 6+ 个上游接口, 冷缓存时首屏可达 10-60s。
    # 改为并行拉取(各接口含超时保护, 单个失败不影响整体)。
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=6) as ex:
        fut_q = ex.submit(svc.get_realtime_quote, symbol, "etf")
        fut_ob = ex.submit(svc.get_order_book, symbol, "etf")
        fut_etf = ex.submit(svc.get_etf_info, symbol)
        fut_bars = ex.submit(svc.get_daily_bars, symbol,
                             date.today() - timedelta(days=400),
                             date.today(), "etf")
        fut_news = ex.submit(get_news_service().get_recent_news,
                             symbol, 72, 15)
        fut_anns = ex.submit(get_news_service().get_recent_announcements,
                             symbol, 7, 10)

        def _safe(fut, default):
            try:
                return fut.result(timeout=30)
            except Exception as exc:
                logger.warning("标的详情并行获取失败 %s: %s", symbol, exc)
                return default

        quote, qrep = _safe(fut_q, ({}, None)) or ({}, None)
        if qrep is None:
            qrep = svc._failed_report(symbol, "realtime_quote", "并行获取失败")
        ob, obrep = _safe(fut_ob, ({}, None)) or ({}, None)
        if obrep is None:
            obrep = svc._failed_report(symbol, "order_book", "并行获取失败")
        etf_info = _safe(fut_etf, {"symbol": symbol}) or {"symbol": symbol}
        bars, _ = _safe(fut_bars, ([], None)) or ([], None)
        news = _safe(fut_news, []) or []
        anns = _safe(fut_anns, []) or []
    tech = compute_technical_features(bars) if bars else {}
    name = quote.get("name") or etf_info.get("name") or ""
    data = {
        "symbol": symbol,
        "name": name,
        "quote": quote,
        "quote_quality": qrep.status,
        "order_book": ob,
        "order_book_quality": obrep.status,
        "etf_info": etf_info,
        "technical": {k: v for k, v in tech.items()
                      if not isinstance(v, (list, dict))},
        "news": [{"title": n.title, "content": (n.content or "")[:300],
                  "publish_time": str(n.publish_time)[:16] if n.publish_time else None,
                  "sentiment": n.sentiment_score} for n in news],
        "announcements": [{"title": a.title,
                           "publish_time": str(a.publish_time)[:16] if a.publish_time else None,
                           "risk_level": a.risk_level, "event_type": a.event_type}
                          for a in anns],
        "time": datetime.now().strftime("%H:%M:%S"),
    }
    # 修复: 数据源故障时技术指标可能含 NaN(如只有1根K线), 直接透传会让
    # FastAPI 序列化 500(详情页打不开)。统一清洗为 null。
    data = _clean(data)
    _symbol_cache[symbol] = (now + _SYMBOL_CACHE_TTL, data)
    return data


# ================================================================
# 4. 新闻/公告
# ================================================================
@router.get("/news/{symbol}", dependencies=[Depends(require_auth)])
def get_news_web(symbol: str, limit: int = 30):
    news = get_news_service().get_recent_news(symbol, hours=168, limit=limit)
    return {"symbol": symbol,
            "news": [{"title": n.title, "content": (n.content or "")[:400],
                      "publish_time": str(n.publish_time)[:16] if n.publish_time else None,
                      "sentiment": n.sentiment_score, "url": n.url} for n in news]}


@router.get("/announcements/{symbol}", dependencies=[Depends(require_auth)])
def get_announcements_web(symbol: str, limit: int = 30):
    anns = get_news_service().get_recent_announcements(symbol, days=30, limit=limit)
    return {"symbol": symbol,
            "announcements": [{"title": a.title,
                               "publish_time": str(a.publish_time)[:16] if a.publish_time else None,
                               "risk_level": a.risk_level, "event_type": a.event_type,
                               "url": a.url} for a in anns]}


@router.get("/backtest/{run_id}/kline/{symbol}", dependencies=[Depends(require_auth)])
def get_backtest_kline(run_id: str, symbol: str):
    """
    回测买卖点K线: 标的历史K线 + 回测中该标的的买卖点 + 策略包络线。
    envelope: 每笔买入的成本价与止损价(虚线), 便于观察策略进出场位置。
    """
    result = repo.get_backtest_result(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="回测结果不存在")
    metrics = result.metrics_json or {}
    trades = [t for t in (metrics.get("trade_details") or []) if t.get("symbol") == symbol]

    # K线(回测区间): 修复: 原实现硬编码最近400天, 回测区间超出时买卖点落在范围外
    params = (metrics.get("params") or {})
    try:
        start = date.fromisoformat(str(params.get("start", "")))
        end = date.fromisoformat(str(params.get("end", "")))
    except (ValueError, TypeError):
        end = date.today()
        start = end - timedelta(days=int(250 * 1.6))
    bars, _ = get_market_service().get_daily_bars(symbol, start, end, "etf")
    df = to_frame(bars)
    if df.empty:
        raise HTTPException(status_code=404, detail=f"无 {symbol} K线数据")
    for w in (5, 20, 60):
        df[f"ma{w}"] = df["close"].rolling(w).mean()
    candles = []
    for _, r in df.iterrows():
        candles.append({
            "date": str(r["trade_date"].date() if hasattr(r["trade_date"], "date") else r["trade_date"])[:10],
            "open": round(float(r["open"]), 4), "high": round(float(r["high"]), 4),
            "low": round(float(r["low"]), 4), "close": round(float(r["close"]), 4),
            "volume": float(r["volume"]),
            "ma5": round(float(r["ma5"]), 4) if r["ma5"] == r["ma5"] else None,
            "ma20": round(float(r["ma20"]), 4) if r["ma20"] == r["ma20"] else None,
            "ma60": round(float(r["ma60"]), 4) if r["ma60"] == r["ma60"] else None,
        })

    # 买卖点标记
    marks = []
    for t in trades:
        d = str(t.get("date"))[:10]
        # 修复: 数据源 NULL/缺字段导致 float(None) 裸 500
        marks.append({"date": d, "price": round(float(t.get("price", 0) or 0), 4),
                      "side": t.get("side"), "qty": int(t.get("qty", 0) or 0),
                      "pnl": round(float(t.get("pnl", 0) or 0), 2)})

    # 完整交易区间(round-trip): 每笔 BUY→SELL 的区间, 用于包络带(盈亏着色)
    round_trips = []
    cur_buy = None   # {date, qty, cost}
    for t in trades:
        d = str(t.get("date"))[:10]
        price = float(t.get("price", 0) or 0)
        qty = int(t.get("qty", 0) or 0)
        if t.get("side") == "BUY":
            if cur_buy is None:
                cur_buy = {"date": d, "qty": qty, "cost": price}
            else:
                # 合并补仓: 摊薄成本
                total_q = cur_buy["qty"] + qty
                cur_buy["cost"] = (cur_buy["cost"] * cur_buy["qty"] + price * qty) / total_q
                cur_buy["qty"] = total_q
        else:  # SELL
            if cur_buy is not None:
                sell_price = price
                pnl = float(t.get("pnl", 0) or 0)
                round_trips.append({
                    "buy_date": cur_buy["date"], "sell_date": d,
                    "cost": round(cur_buy["cost"], 4),
                    "stop": round(cur_buy["cost"] * (1 - 0.08), 4),
                    "sell_price": round(sell_price, 4),
                    "pnl": round(pnl, 2),
                    "profit": pnl > 0,
                })
                cur_buy = None
    if cur_buy is not None:
        # 期末未平仓
        round_trips.append({
            "buy_date": cur_buy["date"], "sell_date": None,
            "cost": round(cur_buy["cost"], 4),
            "stop": round(cur_buy["cost"] * (1 - 0.08), 4),
            "sell_price": None, "pnl": None, "profit": None,
        })
    # 名字
    name_map: Dict[str, str] = {}
    try:
        from database.models import WatchItem as _WI
        with get_session() as s:
            for w in s.query(_WI).all():
                name_map[w.symbol] = w.name
    except Exception:
        pass
    return _clean({"symbol": symbol, "name": name_map.get(symbol, ""),
                   "candles": candles, "marks": marks, "round_trips": round_trips,
                   "trade_count": len(trades)})


# ================================================================
# 5. 运行模式状态(模拟盘/实盘)
# ================================================================
@router.get("/system/mode", dependencies=[Depends(require_auth)])
def get_system_mode():
    """运行模式聚合。修复: 原实现无异常兜底, DB/行情故障时直接 500,
    全站顶部状态条变红; 改为各子项独立降级。"""
    from risk.circuit_breaker import CircuitBreaker
    from workflows.intraday_monitor_workflow import get_broker
    cb = CircuitBreaker.instance()
    cfg = get_settings()
    today = date.today()
    acc_cfg = cfg.get("risk.account_level", {})
    risk_cfg = cfg.get("risk", {})

    # 账户信息(失败时返回空账户, 不拖垮整站)
    # 修复: 读取前刷新持仓现价 —— 原实现持仓 latest_price 只靠调度器快照更新,
    # 监控页顶部"账户总资产/市值"长时间停在买入价。
    try:
        from web.api.main import _refresh_positions_prices
        broker = get_broker()
        _refresh_positions_prices(broker)
        acc = broker.get_account()
    except Exception as exc:
        logger.warning("system/mode 账户读取失败: %s", exc)
        acc = {}

    # 今日订单(失败时降级为0)
    try:
        orders_today = repo.get_orders_today(today)
        today_info = {
            "order_count": len(orders_today),
            "order_amount": round(sum((o.price or 0) * (o.qty or 0) for o in orders_today), 2),
            "max_order_count": int(acc_cfg.get("max_daily_trade_count", 20)),
            "max_order_amount": float(acc_cfg.get("max_daily_trade_amount", 50000)),
        }
    except Exception as exc:
        logger.warning("system/mode 订单读取失败: %s", exc)
        today_info = {"order_count": 0, "order_amount": 0.0,
                      "max_order_count": 0, "max_order_amount": 0.0}

    # 待确认列表(失败时降级为空)
    # 修复: 附标的中文名与完整分析原因(原 reason 是"中风险交易, 需要人工确认",
    # 人工在确认队列里看不到任何分析依据)
    try:
        from core.symbol_names import resolve_symbol_name
        confirms = [
            {"confirm_id": c.confirm_id, "symbol": c.symbol,
             "name": resolve_symbol_name(c.symbol),
             "action": c.action, "amount": c.amount,
             "risk_level": c.risk_level, "reason": c.reason,
             "created_at": str(c.created_at)[:16]}
            for c in repo.list_pending_confirmations()
        ]
    except Exception as exc:
        logger.warning("system/mode 确认单读取失败: %s", exc)
        confirms = []

    return {
        "trade_mode": cfg.get("system.trade_mode", "paper"),          # paper/live/backtest
        "broker_adapter": cfg.get("broker.adapter", "paper"),         # paper/qmt/ptrade
        "live_connected": False,                                       # V1 实盘未接入
        "account_status": acc.get("status", "normal"),                 # normal/paused/readonly
        "circuit": {"paused": cb.is_paused(), "reason": cb.paused_reason()},
        "today": today_info,
        "account": {
            "total_asset": acc.get("total_asset"),
            "cash": acc.get("cash"),
            "market_value": acc.get("market_value"),
            "day_pnl": acc.get("day_pnl"),
            "total_pnl": acc.get("total_pnl"),
            "total_return": acc.get("total_return"),
        },
        "confirmations": confirms,
    }


# ================================================================
# 6. 异步回测任务
# ================================================================
_backtest_tasks: Dict[str, Dict[str, Any]] = {}
_backtest_lock = threading.Lock()
_BACKTEST_MAX_CONCURRENT = 3          # 同时最多 3 个回测(防线程/资源耗尽)
_KEEP_DONE_TASKS = 10                 # 内存只保留最近 N 个已完成任务(防内存增长)


def _cleanup_done_tasks(tasks: Dict[str, Dict[str, Any]], lock: threading.Lock,
                        keep_done: int = _KEEP_DONE_TASKS):
    """清理已完成任务(结果已落库, 可从 DB 恢复)。"""
    with lock:
        done = [(k, v) for k, v in tasks.items()
                if v.get("status") in ("DONE", "FAILED")]
        if len(done) > keep_done:
            for k, _ in sorted(done, key=lambda x: x[1].get("created", 0))[:len(done) - keep_done]:
                tasks.pop(k, None)


def _count_active_tasks(tasks: Dict[str, Dict[str, Any]]) -> int:
    return sum(1 for t in tasks.values()
               if t.get("status") in ("PENDING", "RUNNING"))


def _run_backtest_task(run_id: str, body: Dict[str, Any]):
    """后台线程执行回测(不阻塞HTTP)。"""
    from backtest.engine import BacktestEngine
    from backtest.data_replayer import DataReplayer
    from strategies.rotation_executor import build_rotation_signal_fn
    from database import repository as _repo

    params = body.get("params") or {}
    signal_fn = build_rotation_signal_fn(
        initial_cash=float(body.get("initial_cash", 100000)),
        params=params)

    # 标的名称映射(交易明细显示中文名)
    name_map: Dict[str, str] = {}
    symbols = body.get("symbols") or []
    try:
        for w in _repo.get_watchlist():
            name_map[w["symbol"]] = w["name"]
        from database.models import Symbol as _Sym
        with get_session() as s:
            for sy in s.query(_Sym).all():
                name_map.setdefault(sy.symbol, sy.name)
    except Exception:
        pass

    def progress_cb(done: int, total: int):
        # 修复: total=0 时除零崩溃, 整次回测被误判 FAILED
        pct = int(done / max(total or 1, 1) * 100)
        with _backtest_lock:
            t = _backtest_tasks.get(run_id)
            if t:
                t["progress"] = f"回测进行中 {pct}% ({done}/{total or 0} 个交易日)"

    try:
        with _backtest_lock:
            _backtest_tasks[run_id]["status"] = "RUNNING"
            _backtest_tasks[run_id]["progress"] = "数据准备中..."
        engine = BacktestEngine(
            date.fromisoformat(body["start"]),
            date.fromisoformat(body["end"]),
            initial_cash=float(body.get("initial_cash", 100000)),
            mode=body.get("mode", "daily"),
            use_agents=bool(body.get("use_agents", False)),
            name=body.get("name", ""),
            run_id=run_id,          # 复用提交时的 run_id(避免产生重复回测记录)
        )
        engine.progress_cb = progress_cb
        engine.params = params
        engine.name_map = name_map
        replayer = DataReplayer(symbols or
                                ["510300", "159915", "588000", "512100", "159949"])
        if body.get("mode") == "minute":
            metrics = engine.run_minute(replayer, signal_fn)
        else:
            metrics = engine.run_daily(replayer, signal_fn)
        from reports.report_generator import get_report_generator
        report_path = get_report_generator().generate_backtest_report(metrics)
        metrics["report_path"] = report_path
        with _backtest_lock:
            _backtest_tasks[run_id]["status"] = "DONE"
            _backtest_tasks[run_id]["progress"] = "完成"
            _backtest_tasks[run_id]["metrics"] = metrics
    except Exception as exc:  # noqa: BLE001
        logger.error("异步回测失败 %s: %s", run_id, exc, exc_info=True)
        with _backtest_lock:
            _backtest_tasks[run_id]["status"] = "FAILED"
            _backtest_tasks[run_id]["progress"] = f"失败: {exc}"
            repo.update_backtest_run(run_id, "FAILED")


@router.post("/backtest/submit", dependencies=[Depends(require_auth)])
def submit_backtest(body: dict):
    from core.ids import gen_backtest_id
    # 参数校验(非法输入返回 400, 修复裸 500)
    required = ("start", "end")
    for k in required:
        if k not in body:
            raise HTTPException(status_code=400, detail=f"缺少参数 {k}")
    try:
        start = date.fromisoformat(str(body["start"]))
        end = date.fromisoformat(str(body["end"]))
        initial_cash = float(body.get("initial_cash", 100000))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="日期格式应为 YYYY-MM-DD, 资金应为数字")
    if start >= end:
        raise HTTPException(status_code=400, detail="开始日期必须早于结束日期")
    if initial_cash <= 0:
        raise HTTPException(status_code=400, detail="初始资金必须大于 0")
    # 并发上限(防线程/资源耗尽)
    _cleanup_done_tasks(_backtest_tasks, _backtest_lock)
    if _count_active_tasks(_backtest_tasks) >= _BACKTEST_MAX_CONCURRENT:
        raise HTTPException(status_code=429,
                            detail="回测任务并发数已达上限, 请等待进行中的任务完成")
    run_id = gen_backtest_id()
    with _backtest_lock:
        _backtest_tasks[run_id] = {"status": "PENDING", "progress": "排队中...",
                                   "body": body, "metrics": None,
                                   "created": time.time()}
    repo.save_backtest_run({
        "run_id": run_id,
        "name": body.get("name") or f"回测{body['start']}-{body['end']}",
        "start_date": start,
        "end_date": end,
        "mode": body.get("mode", "daily"),
        "status": "PENDING",
        "config_json": body,
    })
    t = threading.Thread(target=_run_backtest_task, args=(run_id, body), daemon=True)
    t.start()
    return {"run_id": run_id, "status": "PENDING"}


@router.get("/backtest/list", dependencies=[Depends(require_auth)])
def list_backtests(limit: int = 20):
    """注意: 必须定义在 /backtest/{run_id} 之前, 否则会被路径参数抢先匹配。
    只列出有结果的回测(排除历史上重复提交残留的 PENDING 空记录)。"""
    from database.models import BacktestRun
    with get_session() as s:
        runs = s.query(BacktestRun).filter(
            BacktestRun.status.in_(["DONE", "FAILED"])) \
            .order_by(BacktestRun.created_at.desc()).limit(limit).all()
        return [{"run_id": r.run_id, "name": r.name, "start": str(r.start_date),
                 "end": str(r.end_date), "mode": r.mode, "status": r.status,
                 "created_at": str(r.created_at)[:16]} for r in runs]


@router.get("/backtest/{run_id}", dependencies=[Depends(require_auth)])
def get_backtest_status(run_id: str):
    with _backtest_lock:
        task = _backtest_tasks.get(run_id)
    if task is None:
        # 进程重启后从DB恢复状态
        result = repo.get_backtest_result(run_id)
        if result:
            return {"run_id": run_id, "status": "DONE", "progress": "完成",
                    "metrics": result.metrics_json}
        from database.models import BacktestRun
        with get_session() as s:
            run = s.query(BacktestRun).filter_by(run_id=run_id).first()
        if run:
            # 修复: 重启后 PENDING 的旧任务永不会被执行, 原实现返回 PENDING
            # 让前端无限轮询到超时。直接返回 FAILED 供前端提示重跑。
            if run.status in ("PENDING", "RUNNING"):
                return {"run_id": run_id, "status": "FAILED",
                        "progress": "任务已失效(服务重启), 请重新提交",
                        "metrics": None}
            return {"run_id": run_id, "status": run.status,
                    "progress": "任务已提交(服务重启后需重新执行)", "metrics": None}
        raise HTTPException(status_code=404, detail="回测任务不存在")
    return {"run_id": run_id, "status": task["status"],
            "progress": task["progress"], "metrics": task.get("metrics")}


# ================================================================
# 7. 工作流 trace(Agent决策链路)
# ================================================================
@router.get("/workflow/traces", dependencies=[Depends(require_auth)])
def list_workflow_traces(limit: int = 20, offset: int = 0, date: str = "",
                         decision: str = "", holding: int = 0):
    """最近工作流: 按 trace_id 聚合 Agent 运行记录(状态: DONE/FAILED/RUNNING)。
    优化: 原实现全表扫描 + 逐 run 查询 AgentOutput(N+1), 数据量大时接口耗时不可控。
    修复: 支持 limit(≤500)/offset 分页, 每条附带失败原因(last_error);
    date=YYYY-MM-DD 时只返回该日的工作流(前端"按天查看");
    decision=BUY_CANDIDATE|SELL_CANDIDATE|HOLD|EXCLUDE|FAILED 按首席结论筛选;
    holding=1 只看当前有持仓的标的, holding=2 只看未持仓的(前端"持仓标记")。"""
    from database.models import AgentRun, AgentOutput, WatchItem, Symbol as SymModel
    from core.symbol_names import resolve_symbol_name
    limit = min(max(int(limit or 20), 1), 500)
    offset = max(int(offset or 0), 0)
    decision = (decision or "").upper()
    holding = int(holding or 0)
    date_start = None
    if date:
        try:
            date_start = datetime.strptime(date[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="date 格式应为 YYYY-MM-DD")
    with get_session() as s:
        # 代码→名称映射(优先监控列表, 其次标的表, 最后名称解析器兜底)
        name_map: Dict[str, str] = {}
        for w in s.query(WatchItem).all():
            name_map[w.symbol] = w.name
        for sy in s.query(SymModel).all():
            name_map.setdefault(sy.symbol, sy.name)
        # 筛选时放宽取数上限(过滤在内存完成, 避免漏取)
        fetch_mult = 16 if (date_start or decision or holding) else 8
        q = s.query(AgentRun).filter(AgentRun.trace_id != "")
        if date_start:
            q = q.filter(AgentRun.start_time >= date_start,
                         AgentRun.start_time < date_start + timedelta(days=1))
        rows = q.order_by(AgentRun.start_time.desc()).limit(
            (offset + limit) * fetch_mult).all()
        # 批量加载 AgentOutput(消除 N+1)
        run_ids = [r.run_id for r in rows]
        outputs: Dict[str, AgentOutput] = {}
        if run_ids:
            for out in s.query(AgentOutput).filter(AgentOutput.run_id.in_(run_ids)).all():
                outputs[out.run_id] = out
        by_trace: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            t = by_trace.setdefault(r.trace_id, {
                "trace_id": r.trace_id, "symbol": r.symbol, "name": name_map.get(r.symbol, ""),
                "start": str(r.start_time)[:19],
                "runs": [], "failed": False, "chief": None, "latest_status": "DONE",
                "last_error": "",
            })
            t["runs"].append({"agent": r.agent_name, "status": r.status,
                              "time": str(r.start_time)[:19]})
            if r.status == "FAILED":
                t["failed"] = True
                t["latest_status"] = "FAILED"
                # 失败原因(修复: 列表看不到失败原因, 只能逐个点开链路)
                if not t["last_error"]:
                    t["last_error"] = (r.error or "")[:300]
            if r.status == "RUNNING":
                # 有未完成节点 → 整条 trace 为运行中
                t["latest_status"] = "RUNNING"
            if r.agent_name == "chief_researcher" and r.end_time:
                out = outputs.get(r.run_id)
                if out:
                    t["chief"] = {
                        "decision": out.output_json.get("research_decision"),
                        "confidence": out.output_json.get("confidence"),
                        "score": out.output_json.get("score"),
                    }
        traces = sorted(by_trace.values(), key=lambda x: x["start"], reverse=True)
        # 名称兜底: 库内没有的中文名(股票)用解析器补全(修复: 决策链路只显示代码)
        for t in traces:
            if not t.get("name"):
                t["name"] = resolve_symbol_name(t.get("symbol", ""))
        # 按首席结论/失败状态筛选(修复: 前端要能筛出"买入/卖出/持有/排除/失败")
        if decision:
            if decision == "FAILED":
                traces = [t for t in traces if t.get("failed")]
            elif decision == "RUNNING":
                traces = [t for t in traces if t.get("latest_status") == "RUNNING"]
            else:
                traces = [t for t in traces
                          if (t.get("chief") or {}).get("decision") == decision]
        # 持仓标记 + 按持仓筛选(修复: 前端要特别标记有持仓的标的)
        held: set = set()
        try:
            from workflows.intraday_monitor_workflow import get_broker
            held = {p["symbol"] for p in get_broker().get_positions()}
        except Exception:
            pass
        for t in traces:
            t["has_position"] = t.get("symbol", "") in held
        if holding == 1:
            traces = [t for t in traces if t.get("has_position")]
        elif holding == 2:
            traces = [t for t in traces if not t.get("has_position")]
        return {"traces": traces[offset:offset + limit], "total": len(traces)}


@router.get("/agents/latest_decisions", dependencies=[Depends(require_auth)])
def get_latest_decisions(days: int = 7):
    """每个监控标的最近一次首席研究员结论(监控标的页展示, 点击跳转决策链路)。
    返回: {symbol, name, trace_id, decision, confidence, score, time}"""
    from database.models import AgentRun, AgentOutput
    from core.symbol_names import resolve_symbol_name
    days = min(max(int(days or 7), 1), 30)
    start = datetime.now() - timedelta(days=days)
    with get_session() as s:
        rows = s.query(AgentRun).filter(
            AgentRun.agent_name == "chief_researcher",
            AgentRun.symbol != "",
            AgentRun.status == "OK",
            AgentRun.start_time >= start,
        ).order_by(AgentRun.start_time.desc()).limit(800).all()
        best: Dict[str, AgentRun] = {}
        for r in rows:
            if r.symbol not in best:
                best[r.symbol] = r
        run_ids = [r.run_id for r in best.values()]
        outputs: Dict[str, dict] = {}
        if run_ids:
            for out in s.query(AgentOutput).filter(
                    AgentOutput.run_id.in_(run_ids)).all():
                outputs[out.run_id] = out.output_json or {}
        items = []
        for sym, r in best.items():
            oj = outputs.get(r.run_id, {})
            items.append({
                "symbol": sym,
                "name": resolve_symbol_name(sym),
                "trace_id": r.trace_id,
                "decision": oj.get("research_decision"),
                "confidence": oj.get("confidence"),
                "score": oj.get("score"),
                "time": str(r.start_time)[:19],
            })
        items.sort(key=lambda x: x["time"], reverse=True)
        return {"items": items, "days": days}


@router.get("/workflow/trace/{trace_id}", dependencies=[Depends(require_auth)])
def get_workflow_trace(trace_id: str):
    """单个 trace 的完整决策链路: 每节点 Agent 输出 + 审计事件 + 总耗时。
    修复: 为每个节点注入标的中文名(交易员节点输出 name 常为空, 前端看不到股票名)。"""
    from database.models import AgentRun, AgentOutput, AuditLog
    from core.symbol_names import resolve_symbol_name
    with get_session() as s:
        runs = s.query(AgentRun).filter_by(trace_id=trace_id) \
            .order_by(AgentRun.start_time).all()
        symbol = runs[0].symbol if runs else ""
        name = resolve_symbol_name(symbol) if symbol else ""
        nodes = []
        start_min = None
        end_max = None
        for r in runs:
            out = s.query(AgentOutput).filter_by(run_id=r.run_id).first()
            cost = round((r.end_time - r.start_time).total_seconds(), 2) \
                if r.end_time else None
            if r.start_time and (start_min is None or r.start_time < start_min):
                start_min = r.start_time
            if r.end_time and (end_max is None or r.end_time > end_max):
                end_max = r.end_time
            nodes.append({
                "agent": r.agent_name,
                "status": r.status,
                "start": str(r.start_time)[:19],
                "cost": cost,
                "model": r.model_name,
                "symbol": symbol,
                "symbol_name": name,
                "output": out.output_json if out else None,
                "error": r.error,
            })
        events = s.query(AuditLog).filter_by(trace_id=trace_id) \
            .order_by(AuditLog.created_at).all()
        # 总耗时: 从首个审计事件(含数据采集)到最后一个节点结束
        duration = None
        start_ts = start_min
        end_ts = end_max
        if events and (start_ts is None or events[0].created_at < start_ts):
            start_ts = events[0].created_at
        if events and (end_ts is None or events[-1].created_at > end_ts):
            end_ts = events[-1].created_at
        if start_ts and end_ts:
            duration = round((end_ts - start_ts).total_seconds(), 1)
        return {
            "trace_id": trace_id,
            "duration": duration,
            "nodes": nodes,
            "events": [{"event_type": e.event_type, "actor": e.actor,
                        "time": str(e.created_at)[:19], "payload": e.payload_json}
                       for e in events],
        }


# ================================================================
# 7.5 异步盘中分析任务(Web触发, 完整分析需1-3分钟, 不能同步阻塞)
# ================================================================
_scan_tasks: Dict[str, Dict[str, Any]] = {}
_scan_lock = threading.Lock()
_SCAN_MAX_CONCURRENT = 3              # 同时最多 3 个完整分析(1个约1-3分钟, 防线程耗尽)


def _run_scan_task(task_id: str, symbol: str, name: str = ""):
    """后台线程执行完整投研+交易链路。带节点级进度上报(修复: 原实现只有
    RUNNING/DONE 两态, 前端看不到任何进度)。"""
    import asyncio
    from workflows.intraday_monitor_workflow import (
        run_intraday_scan, set_scan_progress_cb, NODE_LABELS,
    )

    def progress_cb(node: str, status: str, idx: int, total: int,
                    cost: float = 0.0, error: str = ""):
        with _scan_lock:
            t = _scan_tasks.get(task_id)
            if not t:
                return
            label = NODE_LABELS.get(node, node)
            done_cnt = idx + 1 if status in ("done", "failed") else idx
            pct = int(done_cnt / max(total, 1) * 100)
            t["progress_pct"] = pct
            t["current_node"] = (f"{label} 执行中..." if status == "running"
                                 else f"{label} 完成({cost:.1f}s)")
            if status == "failed":
                t["current_node"] = f"{label} 失败: {error}"
            t["node_log"] = t.get("node_log", [])[-50:] + [{
                "node": node, "label": label, "status": status,
                "cost": round(cost, 1), "error": error[:200],
            }]

    try:
        set_scan_progress_cb(progress_cb)
        result = asyncio.run(run_intraday_scan(symbol, name, "etf", force=True))
        with _scan_lock:
            _scan_tasks[task_id]["status"] = "DONE"
            _scan_tasks[task_id]["result"] = result
            _scan_tasks[task_id]["trace_id"] = result.get("trace_id", "")
            _scan_tasks[task_id]["progress_pct"] = 100
            _scan_tasks[task_id]["current_node"] = "完成"
    except Exception as exc:  # noqa: BLE001
        logger.error("异步分析失败 %s: %s", task_id, exc, exc_info=True)
        with _scan_lock:
            _scan_tasks[task_id]["status"] = "FAILED"
            _scan_tasks[task_id]["error"] = str(exc)
            _scan_tasks[task_id]["current_node"] = f"失败: {exc}"
    finally:
        set_scan_progress_cb(None)


@router.post("/scan/{symbol}", dependencies=[Depends(require_auth)])
def submit_scan(symbol: str):
    """提交异步分析任务, 立即返回 task_id, 前端轮询 /api/scan/status。"""
    from core.ids import gen_run_id
    # 修复: 原实现无代码格式校验, 非法代码直达上游数据源(超时/报错)
    import re as _re
    if not _re.fullmatch(r"\d{6}", symbol or ""):
        raise HTTPException(status_code=400, detail="标的代码必须为6位数字")
    # 并发上限 + 已完成任务清理(防内存增长)
    _cleanup_done_tasks(_scan_tasks, _scan_lock)
    if _count_active_tasks(_scan_tasks) >= _SCAN_MAX_CONCURRENT:
        raise HTTPException(status_code=429,
                            detail="分析任务并发数已达上限, 请等待进行中的分析完成")
    task_id = gen_run_id()
    with _scan_lock:
        _scan_tasks[task_id] = {"status": "RUNNING", "symbol": symbol,
                                "trace_id": "", "result": None, "error": "",
                                "progress_pct": 0,
                                "current_node": "排队启动中...",
                                "node_log": [],
                                "created": time.time()}
    t = threading.Thread(target=_run_scan_task, args=(task_id, symbol), daemon=True)
    t.start()
    return {"task_id": task_id, "status": "RUNNING", "symbol": symbol}


@router.get("/scan/status/{task_id}", dependencies=[Depends(require_auth)])
def get_scan_status(task_id: str):
    with _scan_lock:
        task = _scan_tasks.get(task_id)
    if task is None:
        # 修复: 服务重启后任务丢失, 原实现直接 404, 前端停留在 RUNNING
        # 无限轮询卡死。返回终态 FAILED 供前端清理。
        return {"task_id": task_id, "status": "FAILED", "symbol": "",
                "trace_id": "", "result": None, "progress_pct": 0,
                "current_node": "", "node_log": [],
                "error": "任务不存在(服务重启, 任务已失效)"}
    return {"task_id": task_id, "status": task["status"], "symbol": task["symbol"],
            "trace_id": task.get("trace_id", ""), "result": task.get("result"),
            "progress_pct": task.get("progress_pct", 0),
            "current_node": task.get("current_node", ""),
            "node_log": task.get("node_log", []),
            "error": task.get("error", "")}


# ================================================================
# 9. 监控标的 API(持仓/热门/主动/股票/ETF 分类)
# ================================================================
@router.get("/watchlist", dependencies=[Depends(require_auth)])
def get_watchlist():
    """监控列表(按分类聚合)。
    修复: 1) 股票类标的名称字段常为空(添加时只查了ETF现货), 现统一用名称
    解析器补全并写回; 2) 持仓类标的附上当前持仓信息(数量/成本/现价/盈亏),
    前端"监控标的"页可直接看到持仓详情。"""
    from core.symbol_names import resolve_symbol_name
    items = repo.get_watchlist()
    # 同步持仓分类(持仓自动加入监控)。
    # 修复: 原实现每次轮询都 upsert 全部持仓(读请求产生持续写放大),
    # 改为仅当持仓集合发生变化时才写库。
    from workflows.intraday_monitor_workflow import get_broker
    position_map = {}
    try:
        positions = get_broker().get_positions()
        position_map = {p["symbol"]: p for p in positions}
        holding_syms = set(position_map.keys())
        existing = {i["symbol"] for i in items}
        missing = holding_syms - existing
        if missing:
            from database import repository as _repo
            for p in positions:
                if p["symbol"] in missing:
                    _repo.upsert_watch_item(p["symbol"], p.get("name", ""), "etf",
                                            categories=["holding"], enabled=True,
                                            priority=100)
            items = repo.get_watchlist()
    except Exception:
        pass
    # 修复: 已存在监控列表中的标的(如策略刚买入的)此前永远不会被加上
    # "holding"分类 —— 同步逻辑只给"新出现的持仓"建项, 已存在的标的
    # 买入后仍留在原分类, 监控标的页"持仓"分组看不到新买入的股票。
    # 现在: 有持仓但分类缺 holding 的 → 补上; 已清仓且含 holding 的 → 移除。
    try:
        from database import repository as _repo
        changed = False
        for i in items:
            cats = list(i.get("categories") or [])
            has_pos = i["symbol"] in position_map
            if has_pos and "holding" not in cats:
                cats = ["holding"] + [c for c in cats if c != "holding"]
                _repo.set_watch_categories(i["symbol"], cats)
                changed = True
            elif not has_pos and "holding" in cats:
                _repo.set_watch_categories(i["symbol"],
                                           [c for c in cats if c != "holding"])
                changed = True
        if changed:
            items = repo.get_watchlist()
    except Exception:
        pass
    # 名称补全(股票看不到中文名的根因) + 持仓信息附加
    for i in items:
        if not i.get("name"):
            name = resolve_symbol_name(i["symbol"])
            if name:
                i["name"] = name
                try:
                    repo.upsert_watch_item(i["symbol"], name, i.get("asset_type", "etf"),
                                           categories=i.get("categories"))
                except Exception:
                    pass
        pos = position_map.get(i["symbol"])
        i["position"] = {
            "total_qty": pos.get("total_qty", 0),
            "available_qty": pos.get("available_qty", 0),
            "cost_price": pos.get("cost_price", 0),
            "latest_price": pos.get("latest_price", 0),
            "market_value": pos.get("market_value", 0),
            "pnl": pos.get("pnl", 0),
            "pnl_pct": pos.get("pnl_pct", 0),
        } if pos else None
    return {"items": items, "total": len(items)}


@router.post("/watchlist", dependencies=[Depends(require_auth)])
def add_watch_item(body: dict):
    """添加监控标的: {symbol, name?, asset_type?, categories?}
    修复: 指数代码(000001/000300/000905/399006)与股票代码冲突,
    原逻辑把 000001 判成股票"平安银行"。指数走独立名称/类型映射。"""
    from core.symbol_names import INDEX_NAMES, INDEX_CODES
    symbol = str(body.get("symbol", "")).upper()
    if not symbol or len(symbol) < 6:
        raise HTTPException(status_code=400, detail="标代码无效")
    if symbol in INDEX_CODES:
        asset_type = "index"
        name = body.get("name", "") or INDEX_NAMES.get(symbol, "")
    else:
        asset_type = body.get("asset_type", "etf" if symbol[0] in "15" else "stock")
        # 尝试补全名称(从ETF现货列表)
        name = body.get("name", "")
        if not name:
            try:
                spot = get_market_service().get_etf_spot()
                hit = next((x for x in spot if x["symbol"] == symbol), None)
                name = hit.get("name", "") if hit else ""
            except Exception:
                pass
    cats = body.get("categories") or ["watched"]
    repo.upsert_watch_item(symbol, name, asset_type, cats, enabled=True)
    return {"ok": True, "symbol": symbol, "name": name}


@router.delete("/watchlist/{symbol}", dependencies=[Depends(require_auth)])
def remove_watch_item_api(symbol: str):
    repo.remove_watch_item(symbol.upper())
    return {"ok": True}


@router.post("/watchlist/{symbol}/enable", dependencies=[Depends(require_auth)])
def set_watch_enable(symbol: str, body: dict):
    # 修复: 原用 bool(body.get("enabled", True)), 字符串 "false" 会被转成 True
    raw = body.get("enabled", True)
    if not isinstance(raw, bool):
        raise HTTPException(status_code=400, detail="enabled 必须为布尔值")
    repo.set_watch_enabled(symbol.upper(), raw)
    return {"ok": True}


@router.post("/watchlist/{symbol}/categories", dependencies=[Depends(require_auth)])
def set_watch_cats(symbol: str, body: dict):
    repo.set_watch_categories(symbol.upper(), body.get("categories") or ["watched"])
    return {"ok": True}


# ================================================================
# 10. 大盘指数概览 + 牛熊诊断
# ================================================================
_INDEX_MAP = {
    "sh000001": "上证指数", "sh000300": "沪深300",
    "sh000905": "中证500", "sz399006": "创业板指",
}


@router.get("/index/overview", dependencies=[Depends(require_auth)])
def get_index_overview():
    """大盘指数实时概览(新浪行情)。"""
    import httpx
    codes = list(_INDEX_MAP.keys())
    try:
        resp = httpx.get(f"https://hq.sinajs.cn/list={','.join(codes)}",
                         headers={"Referer": "https://finance.sina.com.cn/"}, timeout=10)
        text = resp.content.decode("gbk", errors="ignore")
    except Exception as exc:
        logger.warning("指数行情获取失败: %s", exc)
        return {"indexes": [], "time": datetime.now().strftime("%H:%M:%S")}
    indexes = []
    for line in text.strip().splitlines():
        if '="' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        parts = line.split('="')[1].rstrip('";').split(",")
        if len(parts) < 32:
            continue
        name = _INDEX_MAP.get(key, parts[0])
        try:
            price = float(parts[3])
            prev = float(parts[2])
            chg = (price / prev - 1) * 100 if prev else 0.0
        except (ValueError, IndexError):
            continue
        indexes.append({"code": key, "name": name, "price": round(price, 2),
                        "change_pct": round(chg, 2),
                        "color": "up" if chg > 0.05 else ("down" if chg < -0.05 else "flat")})
    return {"indexes": indexes, "time": datetime.now().strftime("%H:%M:%S")}


@router.get("/market/diagnosis", dependencies=[Depends(require_auth)])
def get_market_diagnosis(refresh: int = 0):
    """
    牛熊诊断(规则为主, 结果落库, 默认1小时更新):
    基于四大指数 20日动量/均线排列/回撤 → 判断 risk_on/neutral/risk_off + 建议。
    refresh=1 时强制重新计算; 否则优先返回库内1小时内的诊断。
    """
    from database import repository as _repo
    if refresh != 1:
        cached = _repo.get_latest_market_diagnostic(max_age_minutes=60)
        if cached:
            return cached

    from datetime import timedelta as _td
    svc = get_market_service()
    end = date.today()
    start = end - _td(days=160)
    detail = []
    scores = []
    for code in ["000300", "000905", "000001"]:
        bars = svc.get_index_bars(code, start, end)
        if len(bars) < 21:
            continue
        closes = [b["close"] for b in bars]
        mom20 = closes[-1] / closes[-21] - 1
        ma20 = sum(closes[-20:]) / 20
        ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else ma20
        above_ma = closes[-1] > ma20
        peak = max(closes[-60:]) if len(closes) >= 60 else max(closes)
        drawdown = closes[-1] / peak - 1
        s = 0
        if mom20 > 0.02: s += 1
        elif mom20 < -0.02: s -= 1
        if above_ma: s += 1
        else: s -= 1
        if drawdown < -0.10: s -= 1
        scores.append(s)
        detail.append({"code": code, "mom20": round(mom20, 4),
                       "above_ma20": above_ma, "drawdown60": round(drawdown, 4),
                       "score": s})
    total = sum(scores)
    if total >= 2:
        state, label, advice = "risk_on", "偏牛/进攻", "指数趋势向上, 可保持较高仓位, 优先强势板块(动量排名靠前)。"
    elif total <= -2:
        state, label, advice = "risk_off", "偏熊/防守", "指数趋势向下, 建议降低总仓位、提高现金比例, 只参与超跌反弹并严设止损。"
    else:
        state, label, advice = "neutral", "震荡/观望", "多空信号交织, 建议中性仓位, 等待方向明确, 谨慎追涨杀跌。"
    result = {"state": state, "label": label, "advice": advice,
              "score": total, "detail": detail,
              "time": datetime.now().strftime("%Y-%m-%d %H:%M")}
    # 落库(历史可查)
    try:
        _repo.save_market_diagnostic({
            "state": state, "label": label, "advice": advice,
            "score": total, "detail": detail,
        })
    except Exception as exc:
        logger.warning("市场诊断落库失败: %s", exc)
    return result


# ================================================================
# 11. 股票池(可选启用)
# ================================================================
_stock_spot_cache: Dict[str, Any] = {}
_stock_spot_cache_ts: float = 0.0


@router.get("/stocks/spot", dependencies=[Depends(require_auth)])
def get_stock_spot(limit: int = 100):
    """A股实时列表(供添加股票到监控/搜索页热门股票)。
    修复: 原实现每次调用都拉全市场(慢/限流), 且取前N行(按代码排序,
    热门榜毫无意义)。改为1小时进程内缓存 + 按成交额倒序;
    akshare 因代理不可用时回退 baostock 代码名称列表(无行情)。"""
    import time as _t
    global _stock_spot_cache, _stock_spot_cache_ts
    now = _t.time()
    if _stock_spot_cache and now - _stock_spot_cache_ts < 3600:
        return {"stocks": _stock_spot_cache[:limit],
                "cached": True}
    import akshare as ak
    try:
        df = ak.stock_zh_a_spot_em()
        if "成交额" in df.columns:
            df = df.sort_values("成交额", ascending=False)
        stocks = [
            {"symbol": str(r["代码"]), "name": str(r["名称"]),
             "asset_type": "stock",
             "latest_price": float(r["最新价"] or 0),
             "change_pct": float(r["涨跌幅"] or 0),
             "amount": float(r["成交额"] or 0) if "成交额" in df.columns else 0}
            for _, r in df.head(500).iterrows()]
        _stock_spot_cache = stocks
        _stock_spot_cache_ts = now
        return {"stocks": stocks[:limit], "cached": False}
    except Exception as exc:
        logger.warning("股票列表获取失败(回退baostock): %s", exc)
        # 兜底: baostock 全量代码+名称(无行情, 仅供搜索/看名称)
        fallback = _stock_spot_cache or _baostock_stock_names()
        if fallback:
            _stock_spot_cache = fallback
            _stock_spot_cache_ts = now
        return {"stocks": fallback[:limit] if fallback else [],
                "error": str(exc), "fallback": True}


def _baostock_stock_names() -> List[Dict[str, Any]]:
    """baostock 全市场股票代码+名称(轻量, 无行情字段)。
    query_all_stock 只返回已完成交易日的列表, 盘中/盘前用最近交易日回退。"""
    from datetime import date as _date, timedelta as _td
    try:
        import baostock as bs
        from data_sources.baostock_client import _ensure_login
        _ensure_login()
        d = _date.today()
        while True:
            rs = bs.query_all_stock(day=d.isoformat())
            if rs.error_code == "0":
                rows = []
                while rs.next():
                    row = rs.get_row_data()  # [code(如 sh.600000/sz.000001), tradeStatus, code_name]
                    if len(row) >= 3 and row[1] == "1":
                        code = row[0]
                        name = row[2]
                        # 只留 A 股个股: sh.60xxxx/688xxx(沪市) + sz.000-003xxx/002xxx/30xxxx(深市)。
                        # 剔除指数(sh.000xxx 上证系列/sz.399xxx 深证系列)、ETF(sh.51/56/58x, sz.15/16x)等。
                        if not name or "指数" in name or "板块" in name \
                                or code.startswith("sh.000") or code.startswith("sh.88") \
                                or code.startswith("sh.5") or code.startswith("sz.1") \
                                or code.startswith("sz.399"):
                            continue
                        rows.append({"symbol": code.split(".")[-1], "name": name,
                                     "asset_type": "stock",
                                     "latest_price": 0, "change_pct": 0,
                                     "amount": 0})
                if rows:
                    return rows
            d -= _td(days=1)
            if (_date.today() - d).days > 10:
                break
        return []
    except Exception as exc:
        logger.warning("baostock 股票列表失败: %s", exc)
        return []


# ================================================================
# 12.5 Agent 开关 (成本控制, 设置页可关闭不重要的分析师)
# ================================================================
@router.get("/agents/config", dependencies=[Depends(require_auth)])
def get_agent_config():
    """Agent 启用状态列表(必须启用的不可关闭)。"""
    from core.agent_switch import all_agent_states
    return {"agents": all_agent_states()}


@router.post("/agents/config", dependencies=[Depends(require_auth)])
def set_agent_config(body: dict):
    """切换 Agent 启用状态(运行时生效, 无需重启)。"""
    from core.agent_switch import set_agent_enabled, all_agent_states
    name = str(body.get("agent", ""))
    raw = body.get("enabled")
    if not name or not isinstance(raw, bool):
        raise HTTPException(status_code=400, detail="agent 与 enabled(布尔) 必填")
    if not set_agent_enabled(name, raw):
        raise HTTPException(status_code=400,
                            detail=f"{name} 为必须启用Agent, 不可关闭")
    return {"ok": True, "agents": all_agent_states()}


@router.get("/agents/usage", dependencies=[Depends(require_auth)])
def get_agent_usage(days: int = 7):
    """各 Agent 真实 LLM token 用量(按天聚合, 修复: 输出token是成本大头, 必须有统计)。"""
    from database.models import AgentRun
    days = min(max(int(days or 7), 1), 30)
    start = datetime.now() - timedelta(days=days)
    with get_session() as s:
        rows = s.query(AgentRun).filter(
            AgentRun.start_time >= start,
            AgentRun.prompt_tokens > 0).all()
        by_day: Dict[str, Dict[str, Any]] = {}
        total_in = total_out = 0
        for r in rows:
            d = str(r.start_time)[:10]
            dg = by_day.setdefault(d, {"date": d, "calls": 0,
                                       "prompt_tokens": 0, "completion_tokens": 0})
            dg["calls"] += 1
            dg["prompt_tokens"] += int(r.prompt_tokens or 0)
            dg["completion_tokens"] += int(r.completion_tokens or 0)
            total_in += int(r.prompt_tokens or 0)
            total_out += int(r.completion_tokens or 0)
        by_agent: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            ag = by_agent.setdefault(r.agent_name, {"agent": r.agent_name,
                                                    "calls": 0,
                                                    "prompt_tokens": 0,
                                                    "completion_tokens": 0})
            ag["calls"] += 1
            ag["prompt_tokens"] += int(r.prompt_tokens or 0)
            ag["completion_tokens"] += int(r.completion_tokens or 0)
        return {
            "days": days,
            "total": {"calls": len(rows), "prompt_tokens": total_in,
                      "completion_tokens": total_out},
            "by_day": sorted(by_day.values(), key=lambda x: x["date"]),
            "by_agent": sorted(by_agent.values(),
                               key=lambda x: -x["completion_tokens"]),
        }


# ================================================================
# 12. Agent 准确率归因统计
# ================================================================
@router.get("/agents/accuracy", dependencies=[Depends(require_auth)])
def get_agent_accuracy(days: int = 90, horizon_days: int = 5):
    """
    Agent 准确率归因: 每个 Agent 的历史结论 vs 标的后续 horizon_days 个交易日收益。
    - chief_researcher: BUY_CANDIDATE→涨 命中; SELL_CANDIDATE→跌 命中; HOLD 不计
    - 分析师: bullish→涨 命中; bearish→跌 命中; neutral 不计
    """
    from database.models import AgentRun, AgentOutput, DailyBar
    from datetime import timedelta as _td
    from collections import defaultdict
    start = datetime.now() - timedelta(days=days)

    with get_session() as s:
        runs = s.query(AgentRun).filter(
            AgentRun.start_time >= start,
            AgentRun.status == "OK",
            AgentRun.symbol != "").all()
        # 标的结论日之后的日K(一次拉取)
        symbols = {r.symbol for r in runs}
        bar_map = defaultdict(list)
        if symbols:
            bars = s.query(DailyBar).filter(
                DailyBar.symbol.in_(symbols),
                DailyBar.trade_date >= (date.today() - timedelta(days=days + 60))).all()
            for b in bars:
                bar_map[b.symbol].append(b)

        stats: Dict[str, Dict[str, Any]] = {}
        for r in runs:
            out = s.query(AgentOutput).filter_by(run_id=r.run_id).first()
            if not out or not out.output_json:
                continue
            st = stats.setdefault(r.agent_name, {"agent": r.agent_name, "count": 0,
                                                 "hit": 0, "neutral": 0, "avg_confidence": 0.0})
            # 结论方向
            o = out.output_json
            if r.agent_name == "chief_researcher":
                d = o.get("research_decision")
                direction = "BUY" if d == "BUY_CANDIDATE" else ("SELL" if d == "SELL_CANDIDATE" else None)
            else:
                v = o.get("view")
                direction = "BUY" if v == "bullish" else ("SELL" if v == "bearish" else None)
            if direction is None:
                st["neutral"] += 1
                continue
            # 结论日之后第 horizon 个交易日收益
            bars = bar_map.get(r.symbol, [])
            if not bars:
                continue
            b0 = None
            for b in bars:
                if b.trade_date >= r.start_time.date():
                    b0 = b
                    break
            if b0 is None:
                continue
            later = [b for b in bars if b.trade_date > b0.trade_date]
            if len(later) < horizon_days:
                continue
            ret = later[horizon_days - 1].close / b0.close - 1
            st["count"] += 1
            st["avg_confidence"] += float(o.get("confidence", 0) or 0)
            if (direction == "BUY" and ret > 0) or (direction == "SELL" and ret < 0):
                st["hit"] += 1
        result = []
        for agent, st in stats.items():
            st["accuracy"] = round(st["hit"] / st["count"], 4) if st["count"] else 0.0
            st["avg_confidence"] = round(st["avg_confidence"] / st["count"], 3) if st["count"] else 0.0
            result.append(st)
        result.sort(key=lambda x: x["count"], reverse=True)
        return {"agents": result, "horizon_days": horizon_days,
                "window_days": days,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M")}


# ================================================================
# 13. 账户手动快照(净值曲线)
# ================================================================
@router.post("/account/snapshot", dependencies=[Depends(require_auth)])
def manual_snapshot():
    from workflows.intraday_monitor_workflow import get_broker
    broker = get_broker()
    broker.snapshot()
    acc = broker.get_account()
    return {"ok": True, "total_asset": acc.get("total_asset"),
            "time": datetime.now().strftime("%H:%M:%S")}


# ================================================================
# 12.8 命名策略 (轮动参数保存/应用, 修复: 参数改完不想再被覆盖/丢失)
# 存储: data/strategy_presets.json, 运行时生效(免重启)
#   保存某次策略 → 一键应用到回测表单(前端加载) / 一键应用到实盘(active_live)
# ================================================================
_STRATEGY_PRESET_FILE = None


def _preset_store() -> dict:
    global _STRATEGY_PRESET_FILE
    if _STRATEGY_PRESET_FILE is None:
        from core.config import ROOT_DIR as _ROOT
        _STRATEGY_PRESET_FILE = _ROOT / "data" / "strategy_presets.json"
    try:
        if _STRATEGY_PRESET_FILE.exists():
            return json.loads(_STRATEGY_PRESET_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {"presets": {}, "active_live": ""}


def _save_preset_store(store: dict):
    _STRATEGY_PRESET_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STRATEGY_PRESET_FILE.write_text(json.dumps(store, ensure_ascii=False, indent=2),
                                     encoding="utf-8")


@router.get("/strategies/presets", dependencies=[Depends(require_auth)])
def list_strategy_presets():
    """命名策略列表(含当前实盘生效的策略)。"""
    store = _preset_store()
    active = store.get("active_live", "")
    presets = []
    for name, params in (store.get("presets") or {}).items():
        presets.append({
            "name": name, "params": params,
            "active_live": (name == active),
        })
    return {"presets": presets, "active_live": active}


@router.post("/strategies/presets", dependencies=[Depends(require_auth)])
def save_strategy_preset(body: dict):
    """保存/覆盖一个命名策略(参数=回测表单 params)。"""
    name = str(body.get("name", "")).strip()
    params = body.get("params") or {}
    if not name:
        raise HTTPException(status_code=400, detail="策略名称不能为空")
    if not isinstance(params, dict) or not params:
        raise HTTPException(status_code=400, detail="params 不能为空")
    store = _preset_store()
    store.setdefault("presets", {})[name] = params
    _save_preset_store(store)
    return {"ok": True, "name": name, "active_live": store.get("active_live", "")}


@router.delete("/strategies/presets/{name}", dependencies=[Depends(require_auth)])
def delete_strategy_preset(name: str):
    store = _preset_store()
    if name in (store.get("presets") or {}):
        del store["presets"][name]
        if store.get("active_live") == name:
            store["active_live"] = ""
        _save_preset_store(store)
    return {"ok": True}


@router.post("/strategies/presets/{name}/apply_live", dependencies=[Depends(require_auth)])
def apply_strategy_preset_live(name: str):
    """一键应用到实盘: 实盘轮动立即使用该策略参数(运行时生效, 免重启)。
    name="__default__" 时清除实盘覆盖, 恢复 config.yaml 默认参数。"""
    store = _preset_store()
    if name == "__default__":
        store["active_live"] = ""
        _save_preset_store(store)
        return {"ok": True, "active_live": "", "note": "已恢复 config.yaml 默认参数"}
    if name not in (store.get("presets") or {}):
        raise HTTPException(status_code=404, detail=f"策略 {name} 不存在")
    store["active_live"] = name
    _save_preset_store(store)
    return {"ok": True, "active_live": name,
            "note": "实盘轮动(strategy_rotation)与回测默认参数已切换, 回测表单仍可单独覆盖"}


# ================================================================
# 13.5 账户分析(日报/周报/月报/年报 + 单标的统计 + 基准对比)
# ================================================================
@router.get("/account/analysis", dependencies=[Depends(require_auth)])
def get_account_analysis(period: str = "week", account_id: str = "PA-001"):
    """账户分析: period=day/week/month/year。
    返回区间净值曲线、沪深300基准、账户统计、单标的统计、成交明细。"""
    from reports.report_generator import ReportGenerator
    rg = ReportGenerator()
    if period not in ("day", "week", "month", "year"):
        raise HTTPException(status_code=400, detail="period 应为 day/week/month/year")
    start, end = rg._period_bounds(period, date.today())
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end, datetime.max.time())

    # 净值曲线(区间快照, 追加当前实时资产)
    from workflows.intraday_monitor_workflow import get_broker
    broker = get_broker()
    snaps = [s for s in repo.get_account_snapshots(
        account_id=account_id, limit=100000)
        if start <= s.snapshot_time.date() <= end]
    try:
        cur = broker.get_account()
        cur_asset = float(cur.get("total_asset", 0) or 0)
        if snaps and abs(float(snaps[-1].total_asset or 0) - cur_asset) > 0.01:
            snaps.append(type("S", (), {
                "snapshot_time": datetime.now(), "total_asset": cur_asset}))
    except Exception:
        pass
    eq_curve = [{"time": str(s.snapshot_time)[:16],
                 "total_asset": round(float(s.total_asset or 0), 2)}
                for s in snaps]

    # 沪深300基准(同一时点: 以区间首根K线收盘归一)
    bench_curve, bench_dates = rg._benchmark_curve(start, end)
    bench_map = dict(zip(bench_dates or [], bench_curve or []))
    bench_points = [{"time": t["time"][:10], "value": bench_map.get(t["time"][:10])}
                    for t in eq_curve]

    # 账户统计
    snap_stats = rg._account_snapshot_stats(start, end, account_id)
    trades = repo.get_trades(start=start_dt, end=end_dt, account_id=account_id)
    realized = sum(float(t.pnl or 0) for t in trades if t.pnl is not None)
    wins = sum(1 for t in trades if t.pnl is not None and t.pnl > 0)
    losses = sum(1 for t in trades if t.pnl is not None and t.pnl < 0)
    acc = repo.get_account(account_id)
    positions = broker.get_positions()
    total_asset = float(acc.total_asset or 0) if acc else 0.0
    period_ret = None
    if snap_stats.get("start_asset"):
        period_ret = round(total_asset / float(snap_stats["start_asset"]) - 1, 4)
    bench_ret = (bench_curve[-1] - 1) if bench_curve else None

    symbol_stats = rg._symbol_stats(start_dt, end_dt, account_id, total_asset)
    # 区间外但当前持有的标的也纳入(账户全貌)
    held_syms = {p["symbol"] for p in positions}
    for sym in held_syms - {st["symbol"] for st in symbol_stats}:
        try:
            bars = repo.get_daily_bars(sym, start, end)
            closes = [float(b.close or 0) for b in bars if (b.close or 0) > 0]
            st = {
                "symbol": sym, "name": next(
                    (p.get("name", "") for p in positions if p["symbol"] == sym), ""),
                "buy_count": 0, "sell_count": 0, "trade_count": 0,
                "buy_amount": 0.0, "sell_amount": 0.0,
                "realized_pnl": 0.0, "fee": 0.0, "wins": 0, "losses": 0,
                "position": None, "price_return": round(closes[-1] / closes[0] - 1, 4)
                if len(closes) >= 2 else None,
            }
            p = next((x for x in positions if x["symbol"] == sym), None)
            if p:
                st["position"] = {
                    "total_qty": p.get("total_qty", 0),
                    "available_qty": p.get("available_qty", 0),
                    "cost_price": p.get("cost_price", 0),
                    "latest_price": p.get("latest_price", 0),
                    "pnl": p.get("pnl", 0), "pnl_pct": p.get("pnl_pct", 0),
                }
                if total_asset > 0:
                    st["weight"] = round(
                        p.get("total_qty", 0) * p.get("latest_price", 0) / total_asset, 4)
            symbol_stats.append(st)
        except Exception:
            continue

    return _clean({
        "period": period,
        "range": {"start": str(start), "end": str(end)},
        "stats": {
            "start_asset": snap_stats.get("start_asset"),
            "end_asset": snap_stats.get("end_asset"),
            "total_asset": round(total_asset, 2),
            "total_pnl": round(float(acc.total_pnl or 0), 2) if acc else 0.0,
            "period_return": period_ret,
            "benchmark_return": bench_ret,
            "excess_return": (period_ret - bench_ret) if (period_ret is not None and bench_ret is not None) else None,
            "max_drawdown": snap_stats.get("max_drawdown"),
            "trade_count": len(trades),
            "realized_pnl": round(realized, 2),
            "win_rate": round(wins / (wins + losses), 4) if (wins + losses) else None,
            "fee_total": round(sum(float(t.fee or 0) for t in trades), 2),
        },
        "equity_curve": eq_curve,
        "benchmark_curve": bench_points,
        "symbol_stats": symbol_stats,
        "trades": [{"trade_time": str(t.trade_time)[:19], "symbol": t.symbol,
                    "name": t.name or "", "side": t.side, "price": t.price,
                    "qty": t.qty, "fee": t.fee, "pnl": t.pnl}
                   for t in trades[-100:]],
        "positions": positions,
    })


@router.get("/reports/list", dependencies=[Depends(require_auth)])
def list_reports_api(report_type: str = "", limit: int = 30):
    """已生成报告列表(日报/周报/月报/年报/回测)。"""
    from database.models import ReportRecord
    with get_session() as s:
        q = s.query(ReportRecord)
        if report_type:
            q = q.filter(ReportRecord.report_type == report_type)
        rows = q.order_by(ReportRecord.created_at.desc()).limit(
            min(max(int(limit), 1), 100)).all()
        return [{"report_id": r.report_id, "type": r.report_type,
                 "title": r.title, "file_name": str(r.file_path).split("\\")[-1].split("/")[-1],
                 "summary": r.summary, "created_at": str(r.created_at)[:16]}
                for r in rows]


@router.post("/reports/generate", dependencies=[Depends(require_auth)])
def generate_report_api(report_type: str = "monthly", year: int = 0, month: int = 0):
    """按需生成月报/年报(日报由日终复盘自动生成, 周报由调度器生成)。"""
    from reports.report_generator import get_report_generator
    rg = get_report_generator()
    today = date.today()
    if report_type == "monthly":
        path = rg.generate_monthly_report(year or today.year, month or today.month)
    elif report_type == "annual":
        path = rg.generate_annual_report(year or today.year)
    elif report_type == "weekly":
        path = rg.generate_weekly_report()
    elif report_type == "daily":
        from workflows.daily_review_workflow import run_daily_review
        import asyncio
        result = asyncio.run(run_daily_review(send_email=False))
        path = result.get("report_path", "")
    else:
        raise HTTPException(status_code=400, detail="report_type 应为 daily/weekly/monthly/annual")
    return {"ok": True, "path": path,
            "file_name": str(path).split("\\")[-1].split("/")[-1]}


# ================================================================
# 14. 持仓风控巡检
# ================================================================
@router.get("/risk/position-monitor", dependencies=[Depends(require_auth)])
def position_monitor_view():
    """巡检配置与最近状态。"""
    from risk.position_monitor import get_position_monitor
    pm = get_position_monitor()
    return {"enabled": pm.enabled(), "config": pm.config_view()}


@router.post("/risk/position-monitor/run", dependencies=[Depends(require_auth)])
def position_monitor_run():
    """手动触发一轮持仓风控巡检(立即执行)。"""
    from risk.position_monitor import get_position_monitor
    result = get_position_monitor().check_once()
    return result


# ================================================================
# 8. 风控限额配置(展示用)
# ================================================================
@router.get("/risk/limits", dependencies=[Depends(require_auth)])
def get_risk_limits():
    cfg = get_settings().get("risk", {})
    return {
        "account_level": cfg.get("account_level", {}),
        "position_level": cfg.get("position_level", {}),
        "order_level": cfg.get("order_level", {}),
        "confirmation_policy": cfg.get("confirmation_policy", {}),
        "position_monitor": cfg.get("position_monitor", {}),
    }
