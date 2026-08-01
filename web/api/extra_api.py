# -*- coding: utf-8 -*-
"""
Web API V2: 行情盯盘 / 标的详情 / K线 / 新闻公告 / 异步回测 / 工作流trace / 运行模式
===============================================================================
供前端(React)调用, 全部封装现有 service/repository, 不涉及核心逻辑。
"""
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
# 鉴权
# ---------------------------------------------------------------
def _check_token(authorization: Optional[str] = Header(None)):
    token = get_settings().get("web.admin_token", "quantiagent-admin")
    if authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="无效令牌")


def require_auth(authorization: Optional[str] = Header(None)):
    _check_token(authorization)


# ================================================================
# 1. 批量实时行情(盯盘轮询, 用全市场spot缓存过滤, 避免逐只请求)
# ================================================================
@router.get("/quotes", dependencies=[Depends(require_auth)])
def get_quotes(symbols: str = "", limit: int = 100):
    """批量行情: /api/quotes?symbols=510300,159915 或 不传symbols返回成交额Top。"""
    svc = get_market_service()
    spot = svc.get_etf_spot()
    want = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else []
    if want:
        rows = [s for s in spot if s["symbol"] in want]
    else:
        rows = sorted(spot, key=lambda x: x.get("amount", 0) or 0, reverse=True)[:limit]
    out = []
    for s in rows:
        chg = float(s.get("change_pct", 0) or 0)
        out.append({
            "symbol": s.get("symbol", ""),
            "name": s.get("name", ""),
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
# 2. K线(蜡烛图+均线+成交量)
# ================================================================
@router.get("/kline/{symbol}", dependencies=[Depends(require_auth)])
def get_kline(symbol: str, days: int = 250):
    end = date.today()
    start = end - timedelta(days=int(days * 1.6))
    bars, rep = get_market_service().get_daily_bars(symbol, start, end, "etf")
    df = to_frame(bars)
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
        })
    return {"symbol": symbol, "quality": rep.status, "candles": candles}


# ================================================================
# 3. 标的详情(行情/盘口/技术指标/ETF/新闻/公告)
# ================================================================
_symbol_cache: Dict[str, tuple] = {}   # symbol -> (expire_ts, data)
_SYMBOL_CACHE_TTL = 20.0               # 详情整体缓存20秒(东财限流时避免接口超时)


@router.get("/symbol/{symbol}", dependencies=[Depends(require_auth)])
def get_symbol_detail(symbol: str):
    now = time.time()
    hit = _symbol_cache.get(symbol)
    if hit and hit[0] > now:
        return hit[1]
    svc = get_market_service()
    quote, qrep = svc.get_realtime_quote(symbol, "etf")
    ob, obrep = svc.get_order_book(symbol, "etf")
    etf_info = svc.get_etf_info(symbol)
    bars, _ = svc.get_daily_bars(symbol, date.today() - timedelta(days=400), date.today(), "etf")
    tech = compute_technical_features(bars) if bars else {}
    news = get_news_service().get_recent_news(symbol, hours=72, limit=15)
    anns = get_news_service().get_recent_announcements(symbol, days=7, limit=10)
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
                  "publish_time": str(n.publish_time)[:16],
                  "sentiment": n.sentiment_score} for n in news],
        "announcements": [{"title": a.title, "publish_time": str(a.publish_time)[:16],
                           "risk_level": a.risk_level, "event_type": a.event_type}
                          for a in anns],
        "time": datetime.now().strftime("%H:%M:%S"),
    }
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
                      "publish_time": str(n.publish_time)[:16],
                      "sentiment": n.sentiment_score, "url": n.url} for n in news]}


@router.get("/announcements/{symbol}", dependencies=[Depends(require_auth)])
def get_announcements_web(symbol: str, limit: int = 30):
    anns = get_news_service().get_recent_announcements(symbol, days=30, limit=limit)
    return {"symbol": symbol,
            "announcements": [{"title": a.title, "publish_time": str(a.publish_time)[:16],
                               "risk_level": a.risk_level, "event_type": a.event_type,
                               "url": a.url} for a in anns]}


# ================================================================
# 5. 运行模式状态(模拟盘/实盘)
# ================================================================
@router.get("/system/mode", dependencies=[Depends(require_auth)])
def get_system_mode():
    from risk.circuit_breaker import CircuitBreaker
    from workflows.intraday_monitor_workflow import get_broker
    from database.models import Account
    cb = CircuitBreaker.instance()
    broker = get_broker()
    acc = broker.get_account()
    cfg = get_settings()
    today = date.today()
    orders_today = repo.get_orders_today(today)
    acc_cfg = cfg.get("risk.account_level", {})
    risk_cfg = cfg.get("risk", {})
    return {
        "trade_mode": cfg.get("system.trade_mode", "paper"),          # paper/live/backtest
        "broker_adapter": cfg.get("broker.adapter", "paper"),         # paper/qmt/ptrade
        "live_connected": False,                                       # V1 实盘未接入
        "account_status": acc.get("status", "normal"),                 # normal/paused/readonly
        "circuit": {"paused": cb.is_paused(), "reason": cb.paused_reason()},
        "today": {
            "order_count": len(orders_today),
            "order_amount": round(sum((o.price or 0) * (o.qty or 0) for o in orders_today), 2),
            "max_order_count": int(acc_cfg.get("max_daily_trade_count", 20)),
            "max_order_amount": float(acc_cfg.get("max_daily_trade_amount", 50000)),
        },
        "account": {
            "total_asset": acc.get("total_asset"),
            "cash": acc.get("cash"),
            "market_value": acc.get("market_value"),
            "day_pnl": acc.get("day_pnl"),
            "total_pnl": acc.get("total_pnl"),
            "total_return": acc.get("total_return"),
        },
        "confirmations": [
            {"confirm_id": c.confirm_id, "symbol": c.symbol, "action": c.action,
             "amount": c.amount, "risk_level": c.risk_level, "reason": c.reason,
             "created_at": str(c.created_at)[:16]}
            for c in repo.list_pending_confirmations()
        ],
    }


# ================================================================
# 6. 异步回测任务
# ================================================================
_backtest_tasks: Dict[str, Dict[str, Any]] = {}
_backtest_lock = threading.Lock()


def _run_backtest_task(run_id: str, body: Dict[str, Any]):
    """后台线程执行回测(不阻塞HTTP)。"""
    from backtest.engine import BacktestEngine
    from backtest.data_replayer import DataReplayer
    from strategies.rotation_executor import build_rotation_signal_fn

    signal_fn = build_rotation_signal_fn(initial_cash=float(body.get("initial_cash", 100000)))

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
        )
        replayer = DataReplayer(body.get("symbols") or
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
    run_id = gen_backtest_id()
    required = ("start", "end")
    for k in required:
        if k not in body:
            raise HTTPException(status_code=400, detail=f"缺少参数 {k}")
    with _backtest_lock:
        _backtest_tasks[run_id] = {"status": "PENDING", "progress": "排队中...",
                                   "body": body, "metrics": None}
    repo.save_backtest_run({
        "run_id": run_id,
        "name": body.get("name", f"回测{body['start']}-{body['end']}"),
        "start_date": date.fromisoformat(body["start"]),
        "end_date": date.fromisoformat(body["end"]),
        "mode": body.get("mode", "daily"),
        "status": "PENDING",
        "config_json": body,
    })
    t = threading.Thread(target=_run_backtest_task, args=(run_id, body), daemon=True)
    t.start()
    return {"run_id": run_id, "status": "PENDING"}


@router.get("/backtest/list", dependencies=[Depends(require_auth)])
def list_backtests(limit: int = 20):
    """注意: 必须定义在 /backtest/{run_id} 之前, 否则会被路径参数抢先匹配。"""
    from database.models import BacktestRun
    with get_session() as s:
        runs = s.query(BacktestRun).order_by(BacktestRun.created_at.desc()).limit(limit).all()
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
            return {"run_id": run_id, "status": run.status,
                    "progress": "任务已提交(服务重启后需重新执行)", "metrics": None}
        raise HTTPException(status_code=404, detail="回测任务不存在")
    return {"run_id": run_id, "status": task["status"],
            "progress": task["progress"], "metrics": task.get("metrics")}


# ================================================================
# 7. 工作流 trace(Agent决策链路)
# ================================================================
@router.get("/workflow/traces", dependencies=[Depends(require_auth)])
def list_workflow_traces(limit: int = 20):
    """最近工作流: 按 trace_id 聚合 Agent 运行记录(状态: DONE/FAILED/RUNNING)。"""
    from database.models import AgentRun, AgentOutput
    with get_session() as s:
        rows = s.query(AgentRun).filter(AgentRun.trace_id != "") \
            .order_by(AgentRun.start_time.desc()).all()
        by_trace: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            t = by_trace.setdefault(r.trace_id, {
                "trace_id": r.trace_id, "symbol": r.symbol, "start": str(r.start_time)[:19],
                "runs": [], "failed": False, "chief": None, "latest_status": "DONE",
            })
            t["runs"].append({"agent": r.agent_name, "status": r.status,
                              "time": str(r.start_time)[:19]})
            if r.status == "FAILED":
                t["failed"] = True
                t["latest_status"] = "FAILED"
            if r.status == "RUNNING":
                # 有未完成节点 → 整条 trace 为运行中
                t["latest_status"] = "RUNNING"
            if r.agent_name == "chief_researcher" and r.end_time:
                out = s.query(AgentOutput).filter_by(run_id=r.run_id).first()
                if out:
                    t["chief"] = {
                        "decision": out.output_json.get("research_decision"),
                        "confidence": out.output_json.get("confidence"),
                        "score": out.output_json.get("score"),
                    }
        traces = sorted(by_trace.values(), key=lambda x: x["start"], reverse=True)
        return traces[:limit]


@router.get("/workflow/trace/{trace_id}", dependencies=[Depends(require_auth)])
def get_workflow_trace(trace_id: str):
    """单个 trace 的完整决策链路: 每节点 Agent 输出 + 审计事件。"""
    from database.models import AgentRun, AgentOutput, AuditLog
    with get_session() as s:
        runs = s.query(AgentRun).filter_by(trace_id=trace_id) \
            .order_by(AgentRun.start_time).all()
        nodes = []
        for r in runs:
            out = s.query(AgentOutput).filter_by(run_id=r.run_id).first()
            nodes.append({
                "agent": r.agent_name,
                "status": r.status,
                "start": str(r.start_time)[:19],
                "cost": round((r.end_time - r.start_time).total_seconds(), 2)
                if r.end_time else None,
                "model": r.model_name,
                "output": out.output_json if out else None,
                "error": r.error,
            })
        events = s.query(AuditLog).filter_by(trace_id=trace_id) \
            .order_by(AuditLog.created_at).all()
        return {
            "trace_id": trace_id,
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


def _run_scan_task(task_id: str, symbol: str, name: str = ""):
    """后台线程执行完整投研+交易链路。"""
    import asyncio
    from workflows.intraday_monitor_workflow import run_intraday_scan
    try:
        result = asyncio.run(run_intraday_scan(symbol, name, "etf", force=True))
        with _scan_lock:
            _scan_tasks[task_id]["status"] = "DONE"
            _scan_tasks[task_id]["result"] = result
            _scan_tasks[task_id]["trace_id"] = result.get("trace_id", "")
    except Exception as exc:  # noqa: BLE001
        logger.error("异步分析失败 %s: %s", task_id, exc, exc_info=True)
        with _scan_lock:
            _scan_tasks[task_id]["status"] = "FAILED"
            _scan_tasks[task_id]["error"] = str(exc)


@router.post("/scan/{symbol}", dependencies=[Depends(require_auth)])
def submit_scan(symbol: str):
    """提交异步分析任务, 立即返回 task_id, 前端轮询 /api/scan/status。"""
    from core.ids import gen_run_id
    task_id = gen_run_id()
    with _scan_lock:
        _scan_tasks[task_id] = {"status": "RUNNING", "symbol": symbol,
                                "trace_id": "", "result": None, "error": ""}
    t = threading.Thread(target=_run_scan_task, args=(task_id, symbol), daemon=True)
    t.start()
    return {"task_id": task_id, "status": "RUNNING", "symbol": symbol}


@router.get("/scan/status/{task_id}", dependencies=[Depends(require_auth)])
def get_scan_status(task_id: str):
    with _scan_lock:
        task = _scan_tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="分析任务不存在")
    return {"task_id": task_id, "status": task["status"], "symbol": task["symbol"],
            "trace_id": task.get("trace_id", ""), "result": task.get("result"),
            "error": task.get("error", "")}


# ================================================================
# 9. 监控标的 API(持仓/热门/主动/股票/ETF 分类)
# ================================================================
@router.get("/watchlist", dependencies=[Depends(require_auth)])
def get_watchlist():
    """监控列表(按分类聚合)。"""
    items = repo.get_watchlist()
    # 同步持仓分类(持仓自动加入监控)
    from workflows.intraday_monitor_workflow import get_broker
    try:
        for p in get_broker().get_positions():
            repo.upsert_watch_item(p["symbol"], p.get("name", ""), "etf",
                                   categories=["holding"], enabled=True, priority=100)
    except Exception:
        pass
    items = repo.get_watchlist()
    return {"items": items, "total": len(items)}


@router.post("/watchlist", dependencies=[Depends(require_auth)])
def add_watch_item(body: dict):
    """添加监控标的: {symbol, name?, asset_type?, categories?}"""
    symbol = str(body.get("symbol", "")).upper()
    if not symbol or len(symbol) < 6:
        raise HTTPException(status_code=400, detail="标代码无效")
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
    repo.set_watch_enabled(symbol.upper(), bool(body.get("enabled", True)))
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
    牛熊诊断(规则为主, 30分钟缓存):
    基于四大指数 20日动量/均线排列/回撤 → 判断 risk_on/neutral/risk_off + 建议。
    refresh=1 时强制重新计算。
    """
    global _diagnosis_cache
    now_ts = time.time()
    if refresh != 1 and _diagnosis_cache and now_ts - _diagnosis_cache[0] < 1800:
        return _diagnosis_cache[1]

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
    _diagnosis_cache = (now_ts, result)
    return result


_diagnosis_cache: Optional[tuple] = None


# ================================================================
# 11. 股票池(可选启用)
# ================================================================
@router.get("/stocks/spot", dependencies=[Depends(require_auth)])
def get_stock_spot(limit: int = 100):
    """A股实时列表(供添加股票到监控)。"""
    import akshare as ak
    try:
        df = ak.stock_zh_a_spot_em()
        sub = df.head(limit)
        return {"stocks": [
            {"symbol": str(r["代码"]), "name": str(r["名称"]),
             "latest_price": float(r["最新价"] or 0),
             "change_pct": float(r["涨跌幅"] or 0)}
            for _, r in sub.iterrows()]}
    except Exception as exc:
        logger.warning("股票列表获取失败: %s", exc)
        return {"stocks": [], "error": str(exc)}


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
    }
