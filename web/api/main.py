# -*- coding: utf-8 -*-
"""
FastAPI Web 管理台
==================
功能:
- 仪表盘(账户/持仓/订单/风控状态)
- 人工确认工作台(PENDING 确认 → 批准/拒绝)
- 紧急按钮(一键暂停/恢复/撤单)
- Agent 决策可视化(最近运行记录/输出)
- 回测/扫描触发入口
启动: uvicorn web.api.main:app --port 8080
"""
import asyncio
import logging
import threading
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from core.config import get_settings
from core.logging import setup_logging
from database import repository as repo
from database.db_session import db_health
from notification.notification_service import get_notification_service
from paper_trading.paper_broker import PaperBroker
from risk.circuit_breaker import CircuitBreaker

logger = logging.getLogger("web.api")

app = FastAPI(title="多Agent量化交易系统 V1", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

# V2 API(行情盯盘/标的详情/异步回测/工作流trace/运行模式)
from web.api.extra_api import router as v2_router
app.include_router(v2_router)

_broker = PaperBroker()
_cb = CircuitBreaker.instance()


# ---------------------------------------------------------------
# 启动预热: 后台拉取全市场ETF列表(东财限流时分页需1分钟, 预热后首次请求即快)
# ---------------------------------------------------------------
def _warmup():
    import time as _t
    from data_service.market_data_service import get_market_service
    from database import repository as repo
    from datetime import datetime, timedelta
    try:
        # 1. 清理卡死的 Agent 运行记录(进程被杀导致的 RUNNING)
        from database.models import AgentRun
        with repo.get_session() as s:
            stale = s.query(AgentRun).filter(
                AgentRun.status == "RUNNING",
                AgentRun.start_time < datetime.now() - timedelta(minutes=10)).all()
            for r in stale:
                r.status = "FAILED"
                r.error = "进程中断(服务重启), 标记为失败"
                r.end_time = datetime.now()
            if stale:
                logger.info("已清理 %d 条卡死的Agent运行记录", len(stale))
        # 2. 行情预热
        _t.sleep(2)
        spot = get_market_service().get_etf_spot()
        logger.info("行情预热完成: %d 只ETF", len(spot))
    except Exception as exc:
        logger.warning("启动预热失败(不影响使用): %s", exc)


@app.on_event("startup")
def _startup():
    threading.Thread(target=_warmup, daemon=True).start()


# ---------------------------------------------------------------
# 鉴权(简单 token, 实盘前应替换为正式认证)
# ---------------------------------------------------------------
def _check_token(authorization: Optional[str] = Header(None)):
    token = get_settings().get("web.admin_token", "quantiagent-admin")
    if authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="无效令牌")


def require_auth(authorization: Optional[str] = Header(None)):
    _check_token(authorization)


# ---------------------------------------------------------------
# 仪表盘
# ---------------------------------------------------------------
@app.get("/api/health")
def health():
    return {"status": "ok", "db": db_health(), "paused": _cb.is_paused(),
            "paused_reason": _cb.paused_reason()}


@app.get("/api/account", dependencies=[Depends(require_auth)])
def account():
    return _broker.get_account()


@app.get("/api/positions", dependencies=[Depends(require_auth)])
def positions():
    return _broker.get_positions()


@app.get("/api/orders", dependencies=[Depends(require_auth)])
def orders(status: Optional[str] = None, limit: int = 50):
    orders_ = _broker.get_orders(status)
    return orders_[:limit]


@app.get("/api/trades", dependencies=[Depends(require_auth)])
def trades(limit: int = 100):
    return _broker.get_trades(limit=limit)


@app.get("/api/equity", dependencies=[Depends(require_auth)])
def equity(limit: int = 1000):
    snaps = repo.get_account_snapshots(limit=limit)
    return [{"time": str(s.snapshot_time), "total_asset": s.total_asset} for s in snaps]


# ---------------------------------------------------------------
# 紧急按钮 (文档: 一键暂停/恢复/撤单)
# ---------------------------------------------------------------
@app.post("/api/emergency/pause", dependencies=[Depends(require_auth)])
def emergency_pause(reason: str = "web紧急暂停"):
    _cb.pause(reason)
    return {"status": "paused", "reason": _cb.paused_reason()}


@app.post("/api/emergency/resume", dependencies=[Depends(require_auth)])
def emergency_resume():
    _cb.resume()
    return {"status": "resumed"}


@app.post("/api/emergency/cancel_all", dependencies=[Depends(require_auth)])
def emergency_cancel_all():
    """一键撤单全部未成交委托。"""
    cancelled = []
    for o in _broker.get_orders():
        if o.get("status") in ("SUBMITTED", "ACCEPTED", "PARTIALLY_FILLED"):
            try:
                _broker.cancel_order(o["order_id"], reason="web紧急撤单")
                cancelled.append(o["order_id"])
            except Exception as exc:
                logger.warning("紧急撤单失败 %s: %s", o["order_id"], exc)
    return {"cancelled": cancelled}


# ---------------------------------------------------------------
# 人工确认工作台
# ---------------------------------------------------------------
class ConfirmBody(BaseModel):
    approved: bool
    note: str = ""


@app.get("/api/confirmations/pending", dependencies=[Depends(require_auth)])
def pending_confirmations():
    return [
        {"confirm_id": c.confirm_id, "plan_id": c.plan_id, "symbol": c.symbol,
         "action": c.action, "amount": c.amount, "risk_level": c.risk_level,
         "reason": c.reason, "created_at": str(c.created_at)}
        for c in repo.list_pending_confirmations()
    ]


@app.post("/api/confirmations/{confirm_id}/decide", dependencies=[Depends(require_auth)])
def decide_confirmation(confirm_id: str, body: ConfirmBody):
    c = repo.decide_confirmation(confirm_id, body.approved, by="web")
    logger.info("人工确认 %s → %s", confirm_id, "批准" if body.approved else "拒绝")
    return {"status": "APPROVED" if body.approved else "REJECTED"}


# ---------------------------------------------------------------
# Agent 决策可视化
# ---------------------------------------------------------------
@app.get("/api/agents/recent_runs", dependencies=[Depends(require_auth)])
def recent_agent_runs(limit: int = 50):
    from database.models import AgentRun
    with repo.get_session() as s:
        runs = s.query(AgentRun).order_by(AgentRun.start_time.desc()).limit(limit).all()
        return [{"run_id": r.run_id, "agent": r.agent_name, "symbol": r.symbol,
                 "status": r.status, "model": r.model_name,
                 "time": str(r.start_time)} for r in runs]


@app.get("/api/agents/outputs/{run_id}", dependencies=[Depends(require_auth)])
def agent_output(run_id: str):
    from database.models import AgentOutput
    with repo.get_session() as s:
        out = s.query(AgentOutput).filter_by(run_id=run_id).first()
        return {"output": out.output_json if out else {}}


@app.get("/api/traces/{trace_id}", dependencies=[Depends(require_auth)])
def trace(trace_id: str):
    return {"logs": repo.get_audit_logs(trace_id=trace_id)}


# ---------------------------------------------------------------
# 扫描/回测触发
# 说明: 扫描已改为异步任务(POST /api/scan/{symbol} 立即返回, 轮询 /api/scan/status/{task_id})
# ---------------------------------------------------------------


@app.post("/api/backtest", dependencies=[Depends(require_auth)])
def run_backtest(body: dict):
    """触发日线回测: {start, end, symbols: [], name}"""
    from backtest.engine import BacktestEngine
    from backtest.data_replayer import DataReplayer
    from strategies.etf_momentum_rotation import EtfMomentumRotationStrategy
    from features.technical_indicators import compute_technical_features

    start = date.fromisoformat(body["start"])
    end = date.fromisoformat(body["end"])
    symbols = body.get("symbols") or []

    def signal_fn(asof, prices, d):
        signals = {}
        features = {s: compute_technical_features(bs) for s, bs in asof.items() if bs}
        strat = EtfMomentumRotationStrategy()
        uni = [{"symbol": s, "name": "", "features": features[s]}
               for s in features if features[s]]
        for sig in strat.generate_signals(uni):
            price = prices.get(sig["symbol"], 0)
            if price > 0:
                qty = int(100000 * 0.2 / price // 100 * 100)
                if qty > 0:
                    signals[sig["symbol"]] = {"action": "BUY", "qty": qty,
                                              "reason": sig.get("reason", "")}
        return signals

    engine = BacktestEngine(start, end, name=body.get("name", ""))
    replayer = DataReplayer(symbols or ["510300", "159915", "512100", "159949", "588000"])
    metrics = engine.run_daily(replayer, signal_fn)
    return metrics


# ---------------------------------------------------------------
# 静态仪表盘
# ---------------------------------------------------------------
from pathlib import Path
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

_DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"
_FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"


@app.get("/", include_in_schema=False)
def dashboard():
    # 优先托管 React 构建产物, 不存在时退回旧静态页
    if _FRONTEND_DIST.exists() and (_FRONTEND_DIST / "index.html").exists():
        return FileResponse(_FRONTEND_DIST / "index.html")
    return FileResponse(_DASHBOARD)


if _FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=_FRONTEND_DIST / "assets"),
              name="assets")

# 报告静态目录(回测报告/日报文件可直接访问)
_REPORTS_DIR = Path(__file__).resolve().parent.parent.parent / "reports"
if _REPORTS_DIR.exists():
    app.mount("/reports", StaticFiles(directory=_REPORTS_DIR), name="reports")


# ---------------------------------------------------------------
# 风控状态
# ---------------------------------------------------------------
@app.get("/api/risk/status", dependencies=[Depends(require_auth)])
def risk_status():
    return {"paused": _cb.is_paused(), "reason": _cb.paused_reason(),
            "daily_orders": len(repo.get_orders_today(date.today()))}


@app.post("/api/email/test", dependencies=[Depends(require_auth)])
def test_email(subject: str = "多Agent量化系统测试邮件"):
    from notification.notification_service import NotificationService
    svc = NotificationService()
    ok = svc.mail._send_sync(subject, "<h2>测试邮件</h2><p>如果收到此邮件, 邮件系统正常。</p>")
    return {"sent": ok}


@app.get("/api/universe/top", dependencies=[Depends(require_auth)])
def universe_top(limit: int = 20):
    from data_service.market_data_service import get_market_service
    spot = get_market_service().get_etf_spot()
    top = sorted(spot, key=lambda x: x.get("amount", 0), reverse=True)[:limit]
    return [{"symbol": s["symbol"], "name": s["name"],
             "latest_price": s.get("latest_price", 0),
             "change_pct": s.get("change_pct", 0),
             "amount": s.get("amount", 0)} for s in top]


def start_web(host: str = "0.0.0.0", port: int = 8080):
    import uvicorn
    setup_logging()
    cfg = get_settings().section("web")
    uvicorn.run(app, host=cfg.get("host", host), port=int(cfg.get("port", port)))


if __name__ == "__main__":
    start_web()
