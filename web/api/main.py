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
import os
import threading
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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

# CORS: 白名单可配置(config.yaml web.cors_origins), 收紧默认范围(安全)
_web_cfg = get_settings().section("web")
_cors_origins = [str(o) for o in (_web_cfg.get("cors_origins") or [])
                 if str(o).strip()] or ["*"]
if _cors_origins == ["*"]:
    logger.warning("CORS 允许所有来源(未配置 web.cors_origins), 生产环境应配置白名单")
app.add_middleware(CORSMiddleware,
                   allow_origins=_cors_origins,
                   allow_methods=["*"], allow_headers=["*"])

# V2 API(行情盯盘/标的详情/异步回测/工作流trace/运行模式)
from web.api.extra_api import router as v2_router
app.include_router(v2_router)

# ---------------------------------------------------------------
# 全局异常兜底: 任何未捕获异常返回结构化 JSON, 避免裸 500/全站崩溃
# (修复: 原实现无全局异常处理器, 数据源超时/DB断连直接裸 500,
#  前端所有页面同时报错, 全站状态条变红)
# ---------------------------------------------------------------
@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception):
    import traceback
    logger.error("未捕获异常 %s %s: %s\n%s",
                 request.method, request.url.path, exc,
                 traceback.format_exc())
    return JSONResponse(status_code=500, content={
        "detail": f"服务器内部错误: {exc}",
        "path": request.url.path,
    })

_cb = CircuitBreaker.instance()
_broker: Any = None
_broker_lock = threading.Lock()

# 持仓现价读取前刷新(节流10秒): 修复持仓页/账户页长年停在买入价
_pos_refresh_lock = threading.Lock()
_last_pos_refresh_ts = 0.0


def _refresh_positions_prices(broker: Any):
    """读取持仓/账户前, 用腾讯批量实时行情刷新持仓现价(节流10s)。
    修复: 持仓 latest_price 原本只靠调度器快照(30分钟)更新, 页面10秒轮询
    却长时间停在买入价(现价==成本价、浮盈亏+0.00), 与详情页实时行情对不上。"""
    global _last_pos_refresh_ts
    now = time.time()
    with _pos_refresh_lock:
        if now - _last_pos_refresh_ts < 10.0:
            return
        _last_pos_refresh_ts = now
    try:
        positions = broker.get_positions()
        syms = [p.get("symbol", "") for p in positions
                if int(p.get("total_qty", 0) or 0) > 0]
        if not syms:
            return
        from data_sources.tencent_client import TencentClient
        live = TencentClient().get_realtime_quotes_batch(syms)
        prices = {sym: float(q["latest_price"])
                  for sym, q in live.items()
                  if float(q.get("latest_price", 0) or 0) > 0}
        if prices:
            broker.mark_to_market(prices)
    except Exception as exc:
        logger.debug("持仓现价刷新失败(继续显示缓存价): %s", exc)


def _get_broker():
    """统一 PaperBroker 单例(懒加载, 与工作流侧共用同一实例)。
    注意: 必须在 DB 可用后才首次访问; 模块导入时不初始化, 避免 DB 故障时整站不可用。"""
    global _broker
    if _broker is None:
        with _broker_lock:
            if _broker is None:
                from workflows.intraday_monitor_workflow import get_broker
                _broker = get_broker()
    return _broker


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
    # 修复: uvicorn reload 模式下 worker 是独立进程, 不会执行 start_web 里的
    # setup_logging —— 应用日志(数据源失败/调度任务)全部丢进隐藏控制台,
    # 日志文件里只剩 reloader 的条目。worker 启动时重新挂载文件日志。
    try:
        from core.logging import setup_logging
        setup_logging()
    except Exception:
        pass
    # 安全提示: 默认令牌在任何拿到网络访问权的人手里都能操作账户(暂停/撤单/批准)
    token = get_settings().get("web.admin_token", "quantiagent-admin")
    if not token or token == "quantiagent-admin":
        logger.warning(
            "Web 使用默认管理令牌 'quantiagent-admin' —— 任何能访问该端口的人 "
            "都可暂停/恢复/撤单/批准交易。生产环境请在 config/config.yaml "
            "web.admin_token 中配置随机令牌。")
    threading.Thread(target=_warmup, daemon=True, name="web-warmup").start()
    # 防自动睡眠(修复: 电脑休眠会把整个系统冻结, 调度器/Web 全停)
    try:
        from core.power_guard import prevent_sleep
        prevent_sleep()
    except Exception as exc:
        logger.warning("防睡眠设置失败: %s", exc)
    # 修复: 调度器常被遗忘启动(窗口被关/重启只起 web), 导致"跑一天没有一条
    # 决策链"。web 启动时内嵌拉起调度器(单例锁保证全系统只有一个实例)。
    if get_settings().get("web.embed_scheduler", True):
        def _embed_scheduler():
            try:
                from scheduler.apscheduler_app import QuantScheduler
                sched = QuantScheduler()
                if sched.start():
                    logger.info("Web 已内嵌启动调度器(单例锁 data/scheduler.pid)")
            except Exception as exc:
                logger.warning("内嵌调度器启动失败: %s", exc, exc_info=True)
        threading.Thread(target=_embed_scheduler, daemon=True,
                         name="embedded-scheduler").start()


@app.on_event("shutdown")
def _shutdown():
    # 若调度器锁是本进程持有, 由进程退出自然释放; 心跳文件保留供查状态
    pass


# ---------------------------------------------------------------
# 鉴权(简单 token, 实盘前应替换为正式认证)
# ---------------------------------------------------------------
def _check_token(authorization: Optional[str] = Header(None)):
    import hmac
    token = get_settings().get("web.admin_token", "quantiagent-admin")
    if not authorization or not hmac.compare_digest(authorization, f"Bearer {token}"):
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
    broker = _get_broker()
    _refresh_positions_prices(broker)
    return broker.get_account()


@app.get("/api/positions", dependencies=[Depends(require_auth)])
def positions():
    broker = _get_broker()
    _refresh_positions_prices(broker)
    return broker.get_positions()


@app.get("/api/orders", dependencies=[Depends(require_auth)])
def orders(status: Optional[str] = None, limit: int = 50):
    orders_ = _get_broker().get_orders(status)
    return orders_[:limit]


@app.get("/api/trades", dependencies=[Depends(require_auth)])
def trades(limit: int = 100):
    return _get_broker().get_trades(limit=limit)


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
    for o in _get_broker().get_orders():
        if o.get("status") in ("SUBMITTED", "ACCEPTED", "PARTIALLY_FILLED"):
            try:
                _get_broker().cancel_order(o["order_id"], reason="web紧急撤单")
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
    """待人工确认列表(附标的中文名与完整分析原因)。"""
    from core.symbol_names import resolve_symbol_name
    return [
        {"confirm_id": c.confirm_id, "plan_id": c.plan_id, "symbol": c.symbol,
         "name": resolve_symbol_name(c.symbol),
         "action": c.action, "amount": c.amount, "risk_level": c.risk_level,
         "reason": c.reason, "created_at": str(c.created_at)}
        for c in repo.list_pending_confirmations()
    ]


@app.post("/api/confirmations/{confirm_id}/decide", dependencies=[Depends(require_auth)])
async def decide_confirmation(confirm_id: str, body: ConfirmBody):
    """人工确认: 批准 → 重新过风控并恢复下单; 拒绝 → 计划拒绝。
    幂等: 确认单已处理时直接返回, 不重复下单。"""
    from workflows.intraday_monitor_workflow import get_broker
    from workflows.trading_workflow import resume_confirmed_plan
    result = await resume_confirmed_plan(confirm_id, body.approved, get_broker(), by="web")
    logger.info("人工确认 %s → %s: %s", confirm_id, result.get("status"),
                result.get("reason"))
    return {"status": result.get("status"), "reason": result.get("reason")}


@app.get("/api/confirmations/{confirm_id}/email", include_in_schema=False)
async def email_decide_confirmation(confirm_id: str, decision: str = "", sig: str = ""):
    """邮件一键确认(修复: 邮件里点"批准/拒绝"按钮直接处理, 无需登录网页)。
    链接带 HMAC 签名(web.admin_token 派生), 防止伪造。"""
    import hashlib
    import hmac as _hmac
    from fastapi.responses import HTMLResponse
    token = get_settings().get("web.admin_token", "quantiagent-admin")
    if decision not in ("approve", "reject"):
        return HTMLResponse(_email_result_html("参数错误", "链接缺少 decision 参数"), status_code=400)
    expected = _hmac.new(token.encode(), f"{confirm_id}:{decision}".encode(),
                         hashlib.sha256).hexdigest()[:16]
    if not _hmac.compare_digest(expected, sig or ""):
        return HTMLResponse(_email_result_html("签名无效", "链接可能已被篡改, 请到网页端处理"), status_code=400)
    from workflows.intraday_monitor_workflow import get_broker
    from workflows.trading_workflow import resume_confirmed_plan
    try:
        result = await resume_confirmed_plan(confirm_id, decision == "approve",
                                             get_broker(), by="email")
    except Exception as exc:
        logger.error("邮件确认失败 %s: %s", confirm_id, exc)
        return HTMLResponse(_email_result_html("处理失败", str(exc)), status_code=500)
    status = result.get("status", "")
    ok = status in ("ORDERED", "APPROVED", "REJECTED", "SKIPPED")
    label = {
        "ORDERED": "✅ 已批准并提交订单", "APPROVED": "✅ 已批准并提交订单",
        "REJECTED": "❌ 已拒绝该交易计划",
        "SKIPPED": "⚠️ 该确认单已被处理过(请到网页端查看)",
        "FAILED": "❌ 下单失败", "NOT_FOUND": "⚠️ 确认单不存在",
    }.get(status, f"结果: {status}")
    reason = str(result.get("reason") or "")[:200]
    return HTMLResponse(_email_result_html(label, reason), status_code=200 if ok else 400)


def _email_result_html(title: str, detail: str) -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>人工确认结果</title></head>
<body style="font-family:sans-serif;background:#f2f2f7;padding:40px">
<div style="max-width:420px;margin:auto;background:#fff;border-radius:12px;padding:24px;text-align:center;
            box-shadow:0 2px 8px rgba(0,0,0,.1)">
  <h2 style="margin:0 0 8px">{title}</h2>
  <p style="color:#666;font-size:13px">{detail}</p>
  <p style="color:#aaa;font-size:11px;margin-top:16px">多Agent量化交易系统 · 模拟盘</p>
</div></body></html>"""


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
    """兼容端点: 转发到异步回测任务(web/api/extra_api.py 的 /backtest/submit)。
    修复: 原实现在请求线程同步跑完整回测(分钟级阻塞)且无参数校验, 现统一走异步任务。"""
    from web.api.extra_api import submit_backtest as _submit_async
    return _submit_async(body)


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

# 报告下载: 原来 /reports 静态目录无鉴权挂载(回测报告/日报含策略细节与账户数据),
# 改为带 token 的下载端点(安全加固)


@app.get("/api/scheduler/status", dependencies=[Depends(require_auth)])
def scheduler_status():
    """调度器存活状态: 单例锁 pid 是否存活 + 心跳时间 + 已注册任务。
    修复: 用户看不到调度器是否在运行, 曾经一整天没有任何决策链产出。"""
    import json
    from core.config import ROOT_DIR as _ROOT
    from scheduler.apscheduler_app import _process_alive, _LOCK_FILE
    running = False
    pid = 0
    if _LOCK_FILE.exists():
        try:
            pid = int(_LOCK_FILE.read_text(encoding="utf-8").strip() or "0")
            running = _process_alive(pid)
        except Exception:
            pass
    heartbeat = None
    try:
        hp = _ROOT / "data" / "scheduler_heartbeat.json"
        if hp.exists():
            heartbeat = json.loads(hp.read_text(encoding="utf-8"))
    except Exception:
        pass
    # 心跳超过 3 分钟视为失活
    alive = running
    if alive and heartbeat and heartbeat.get("ts"):
        try:
            from datetime import datetime as _dt
            hb_ts = _dt.strptime(heartbeat["ts"], "%Y-%m-%d %H:%M:%S")
            if (datetime.now() - hb_ts).total_seconds() > 180:
                alive = False
        except Exception:
            pass
    return {"running": alive, "pid": pid, "heartbeat": heartbeat,
            "note": "web 已内嵌调度器; 电脑休眠会暂停所有进程, 唤醒后自动补跑"
                    if alive else "调度器未运行(启动 web 会自动拉起; 也可执行 python main.py scheduler)"}


@app.post("/api/scheduler/start", dependencies=[Depends(require_auth)])
def scheduler_start():
    """手动拉起调度器(独立进程)。单例锁保证不会重复运行。"""
    import subprocess
    from core.config import ROOT_DIR as _ROOT
    cur = scheduler_status()
    if cur.get("running"):
        return {"ok": True, "already_running": True, "pid": cur.get("pid")}
    py = _ROOT / ".venv" / "Scripts" / "python.exe"
    if not py.exists():
        py = "python"
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    try:
        subprocess.Popen([str(py), "main.py", "scheduler"],
                         cwd=str(_ROOT),
                         creationflags=creationflags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL)
        return {"ok": True, "already_running": False}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"调度器启动失败: {exc}")


@app.get("/api/settings/info", dependencies=[Depends(require_auth)])
def settings_info():
    """设置页动态状态(修复: 原前端硬编码"真实模型(DeepSeek)/邮件已启用(QQ 465)"。
    实际是否配置 LLM/邮件随 .env 变化, 硬编码显示会误导用户)。"""
    from core.config import get_settings as _gs
    s = _gs()
    llm = s.section("llm") or {}
    email = s.section("email") or {}
    db = s.section("database") or {}
    data_src = s.section("data_sources") or {}
    sources = data_src.get("sources", {}) or {}
    return {
        "version": s.get("system.version", "1.0.0"),
        "trade_mode": s.get("system.trade_mode", "paper"),
        "timezone": s.get("system.timezone", "Asia/Shanghai"),
        "llm": {
            "configured": s.llm_configured(),
            "mock_mode": s.mock_mode(),
            "fast_model": llm.get("fast_model", ""),
            "deep_model": llm.get("deep_model", ""),
            "embedding_model": llm.get("embedding_model", ""),
            "base_url": str(llm.get("base_url", "")).split("//")[-1].split("@")[-1],
        },
        "email": {
            "enabled": bool(email.get("enabled") or (email.get("sender") and email.get("sender_pass"))),
            "smtp_host": email.get("smtp_host", ""),
            "smtp_port": email.get("smtp_port", ""),
            "sender": email.get("sender", ""),
            "receiver": email.get("receiver", ""),
        },
        "database": {"host": db.get("host", ""), "port": db.get("port", ""),
                     "name": db.get("name", ""), "user": db.get("user", "")},
        "data_sources": {
            cat: {"primary": (v or {}).get("primary", ""),
                  "backups": (v or {}).get("backups", [])}
            for cat, v in sources.items()
        },
        "initial_cash": s.get("paper_account.initial_cash", 100000),
        "admin_token_default": (s.get("web.admin_token", "") or "") in ("", "quantiagent-admin"),
    }


@app.get("/api/reports/{filename}")
def download_report(filename: str, token: Optional[str] = None,
                    authorization: Optional[str] = Header(None)):
    """下载报告。修复: 前端 <a href target=_blank> 新标签页不会携带
    Authorization 头 → 下载报"无效令牌"。改为 header 或 ?token= 二选一。"""
    import hmac as _hmac
    expect = get_settings().get("web.admin_token", "quantiagent-admin")
    ok_header = authorization and _hmac.compare_digest(
        authorization, f"Bearer {expect}")
    ok_query = token and _hmac.compare_digest(token, expect)
    if not (ok_header or ok_query):
        raise HTTPException(status_code=401, detail="无效令牌")
    from fastapi.responses import FileResponse
    from pathlib import Path as _Path
    # 防路径穿越: 只允许文件名(不含路径分隔符)
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="非法文件名")
    _REPORTS_DIR = _Path(__file__).resolve().parent.parent.parent / "reports"
    f = _REPORTS_DIR / filename
    if not f.exists() or not f.is_file():
        raise HTTPException(status_code=404, detail="报告不存在")
    return FileResponse(f, filename=filename)


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
    # 修复: 原实现访问私有 _send_sync, 邮件未配置时返回 None → AttributeError 500
    try:
        ok = svc.mail._send_sync(subject, "<h2>测试邮件</h2><p>如果收到此邮件, 邮件系统正常。</p>")
        return {"sent": bool(ok)}
    except Exception as exc:
        logger.warning("测试邮件发送失败: %s", exc)
        return {"sent": False, "error": str(exc)}


@app.get("/api/universe/top", dependencies=[Depends(require_auth)])
def universe_top(limit: int = 20):
    """成交额Top ETF(带名称/类型, 供标的搜索页"热门标的"动态展示)。
    修复: 排名用现货缓存, 价格/涨跌幅用腾讯批量实时行情(现货缓存可能冻结)。"""
    from data_service.market_data_service import get_market_service
    spot = get_market_service().get_etf_spot()
    top = sorted(spot, key=lambda x: x.get("amount", 0) or 0, reverse=True)[:limit]
    try:
        from data_sources.tencent_client import TencentClient
        live = TencentClient().get_realtime_quotes_batch(
            [s["symbol"] for s in top if s.get("symbol")])
        for s in top:
            q = live.get(s.get("symbol"))
            if q and q.get("latest_price"):
                s["latest_price"] = q.get("latest_price")
                s["change_pct"] = q.get("change_pct", s.get("change_pct", 0))
    except Exception as exc:
        logger.debug("热门标的价格刷新失败(沿用现货): %s", exc)
    return [{"symbol": s["symbol"], "name": s.get("name", ""),
             "asset_type": "etf",
             "latest_price": s.get("latest_price", 0),
             "change_pct": s.get("change_pct", 0),
             "amount": s.get("amount", 0)} for s in top]


# SPA 路由回退: 必须定义在 /assets 静态挂载之后(Starlette 按注册顺序匹配,
# 若在 mount 之前注册, catch-all 会拦截 /assets/* 导致资源 404 白屏)。
@app.get("/{full_path:path}", include_in_schema=False)
def spa_fallback(full_path: str):
    """直接访问/刷新 /agents /watchlist 等深层路由时, BrowserRouter 的历史
    路由在后端无匹配 → 404。非 /api 路径统一返回前端入口。"""
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not Found")
    if _FRONTEND_DIST.exists() and (_FRONTEND_DIST / "index.html").exists():
        return FileResponse(_FRONTEND_DIST / "index.html")
    return FileResponse(_DASHBOARD)


def start_web(host: str = "0.0.0.0", port: int = 8080, reload: bool = False):
    import uvicorn
    setup_logging()
    cfg = get_settings().section("web")
    host = cfg.get("host", host)
    port = int(cfg.get("port", port))
    # log_config=None: 禁用 uvicorn 自带的 logging 配置。
    # 修复: uvicorn 默认 dictConfig 会把根 logger 的 handler 替换成控制台,
    # 我们的 system.log/error.log 文件 handler 在 worker 里全部丢失 ——
    # 表现为"日志文件里只有 watchfiles/uvicorn 条目, 应用日志(数据源失败、
    # 调度任务)全部消失", 排查问题全靠猜。禁用后全部日志走统一配置。
    if reload:
        # 注意: uvicorn 的 reload 模式必须传 import 字符串(传 app 对象会直接报错)
        # 只监听 .py 变更 —— 调度器心跳/pid、akshare 磁盘缓存等运行时
        # 写入 data/ 的 JSON 文件如果也触发 reload, 会形成"心跳→重启→心跳"
        # 的无限重载循环, 服务永远处于重启窗口(表现为页面一直超时)。
        from core.config import ROOT_DIR
        logger.info("Web 服务以热重载模式启动(reload=True): 仅 .py 变更触发重载")
        uvicorn.run("web.api.main:app", host=host, port=port,
                    reload=True, reload_dirs=[str(ROOT_DIR)],
                    reload_includes=["*.py"],
                    reload_excludes=["data/*", "logs/*", "reports/*",
                                     "frontend/dist/*", "__pycache__/*"],
                    log_config=None)
    else:
        uvicorn.run(app, host=host, port=port, log_config=None)


if __name__ == "__main__":
    start_web()
