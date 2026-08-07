# -*- coding: utf-8 -*-
"""
实盘轮动策略执行 (回测策略的落地应用)
======================================
回测中的 ETF 动量轮动策略(rotation_executor.signal_fn)与实盘共用同一套
信号函数 —— 保证"回测怎么测, 实盘怎么做"。

调度: agent_schedule.yaml 的 strategy_rotation 任务(默认 14:40 收盘前),
手动: python main.py rotate
流程:
  1. 取监控池(enabled)标的的日K与实时价
  2. 用与回测相同的信号函数计算轮动信号(排名/止损/止盈/市场过滤/冷却期)
  3. 买入前做"Agent分歧检查"(修复): 该标的最近Agent首席结论≠BUY_CANDIDATE
     时, 视为策略与Agent分歧 → 创建人工确认+邮件, 2分钟超时按策略自动执行
  4. 提交订单(经 PaperBroker, 与人工确认/风控共用同一流程; 受熔断保护)
  5. 邮件发送轮动计划摘要
配置: config.yaml strategies.live_rotation (enabled/max_orders_per_day)
"""
import logging
import threading
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from core.config import get_settings
from database import repository as repo
from strategies.rotation_executor import build_rotation_signal_fn

logger = logging.getLogger("strategy.live")


class _LiveBrokerView:
    """信号函数需要的 broker 视图(回测引擎传的是 mock, 实盘包一层真实broker)。"""

    def __init__(self, broker):
        self._broker = broker
        try:
            self.cash = float(broker.get_account().get("cash", 0) or 0)
        except Exception:
            self.cash = 0.0
        self.positions: Dict[str, Dict[str, Any]] = {}
        try:
            for p in broker.get_positions():
                bd = None
                if p.get("buy_date"):
                    try:
                        bd = date.fromisoformat(str(p["buy_date"])[:10])
                    except ValueError:
                        bd = None
                self.positions[p["symbol"]] = {
                    "qty": int(p.get("total_qty", 0) or 0),
                    "cost": float(p.get("cost_price", 0) or 0),
                    "peak": float(p.get("peak_price", 0) or 0)
                    or float(p.get("cost_price", 0) or 0),
                    "buy_date": bd,
                }
        except Exception as exc:
            logger.warning("轮动持仓视图构建失败: %s", exc)

    def position_value(self, prices: Dict[str, float]) -> float:
        return sum(self.positions[s]["qty"] * float(prices.get(s, 0) or 0)
                   for s in self.positions)


def run_live_rotation(broker=None, notify: bool = True) -> Dict[str, Any]:
    """执行一轮实盘轮动: 返回 {signals, orders, skipped, errors}。"""
    from workflows.intraday_monitor_workflow import get_broker as _gb
    from data_service.market_data_service import get_market_service
    from risk.circuit_breaker import CircuitBreaker
    broker = broker or _gb()
    cfg = get_settings().get("strategies.live_rotation", {}) or {}
    if not cfg.get("enabled", True):
        return {"skipped": ["live_rotation.enabled=false, 未执行"]}
    if CircuitBreaker.instance().is_paused():
        return {"skipped": [f"系统熔断中: {CircuitBreaker.instance().paused_reason()}"]}

    max_orders = int(cfg.get("max_orders_per_day", 6))
    today = date.today()
    # 当日已提交的轮动订单数(幂等键 INTENT-ROT-*)
    try:
        placed = [o for o in broker.get_orders()
                  if str(o.get("order_intent_id", "")).startswith("INTENT-ROT-")
                  and str(o.get("submit_time", ""))[:10] == str(today)]
    except Exception:
        placed = []
    if len(placed) >= max_orders:
        return {"skipped": [f"当日轮动订单已达上限({max_orders})"]}

    svc = get_market_service()
    watch = repo.get_watchlist(enabled_only=True)
    symbols = [w["symbol"] for w in watch][:20]
    if not symbols:
        return {"skipped": ["监控池为空"]}

    start = today - timedelta(days=150)
    asof: Dict[str, List[dict]] = {}
    prices: Dict[str, float] = {}
    names: Dict[str, str] = {}
    for w in watch[:20]:
        names[w["symbol"]] = w.get("name", "")
    for sym in symbols:
        try:
            bars, _ = svc.get_daily_bars(sym, start, today, "etf")
            if bars:
                asof[sym] = bars
            q, _ = svc.get_realtime_quote(sym, "etf")
            p = float((q or {}).get("latest_price", 0) or 0)
            if p > 0:
                prices[sym] = p
        except Exception as exc:
            logger.warning("轮动数据获取失败 %s: %s", sym, exc)
    if not asof:
        return {"errors": ["无标的K线数据"]}

    view = _LiveBrokerView(broker)
    signal_fn = build_rotation_signal_fn(
        initial_cash=float(view.cash or 100000),
        params=cfg.get("params") or {})
    signals = signal_fn(asof, prices, today, broker=view) or {}

    orders = []
    skipped = []
    for sym, sig in signals.items():
        if len(placed) + len(orders) >= max_orders:
            skipped.append(f"{sym}: 订单数达上限")
            break
        price = prices.get(sym, 0)
        if price <= 0:
            skipped.append(f"{sym}: 无有效价格")
            continue
        qty = int(sig.get("qty", 0) or 0)
        if qty <= 0 or qty % 100 != 0:
            skipped.append(f"{sym}: 数量非法({qty})")
            continue
        side = sig.get("action")
        if side not in ("BUY", "SELL"):
            continue
        # 修复: 策略买入的"Agent分歧检查" —— 策略要买但Agent首席结论
        # 不是BUY_CANDIDATE时, 创建人工确认+邮件, 2分钟超时按策略自动执行。
        # (原实现策略直接下单, 不检查Agent, 分歧单无任何确认/邮件)
        if side == "BUY":
            view = _latest_agent_view(sym, hours=4)
            if view is not None and view != "BUY_CANDIDATE":
                confirm_id = _create_divergence_confirm(
                    broker, sym, names.get(sym, ""), qty, price,
                    sig.get("reason", ""), view, today)
                orders.append({"symbol": sym, "side": "BUY", "qty": qty,
                               "price": price, "order_id": "",
                               "confirm_id": confirm_id,
                               "reason": f"[分歧确认] Agent结论={view}, "
                                         f"2分钟超时按策略自动执行"})
                skipped.append(f"{sym}: 策略与Agent分歧(Agent={view}), 已发确认邮件"
                               f"(2分钟无响应将按策略买入)")
                logger.info("轮动分歧确认 %s: 策略BUY vs Agent=%s (确认单%s)",
                            sym, view, confirm_id)
                continue
        # 限价单: 市价附近(买入略高于现价, 卖出略低于现价, 确保成交)
        limit = round(price * (1.001 if side == "BUY" else 0.999), 4)
        try:
            order = broker.place_order({
                "symbol": sym, "side": side, "qty": qty,
                "order_type": "LIMIT", "price": limit,
                "plan_id": f"PLAN-ROT-{today:%Y%m%d}-{sym}",
                # 幂等: 同一标的同一天只报一次单
                "order_intent_id": f"INTENT-ROT-{sym}-{today:%Y%m%d}",
                "name": names.get(sym, ""),
                "source": "rotation",
            })
            orders.append({"symbol": sym, "side": side, "qty": qty,
                           "price": limit, "order_id": order.get("order_id"),
                           "reason": sig.get("reason", "")})
            logger.info("轮动下单 %s %s %d份 @%.3f: %s",
                        side, sym, qty, limit, sig.get("reason", ""))
        except Exception as exc:
            skipped.append(f"{sym}: {exc}")
            logger.warning("轮动下单失败 %s: %s", sym, exc)

    if notify:
        try:
            _send_rotation_email(orders, signals, skipped)
        except Exception as exc:
            logger.warning("轮动通知发送失败: %s", exc)
    return {"signals": signals, "orders": orders, "skipped": skipped}


def _latest_agent_view(symbol: str, hours: int = 4) -> Optional[str]:
    """该标的最近一次首席研究员结论(近 hours 小时内, 无则 None)。"""
    try:
        from database.models import AgentRun, AgentOutput
        from database.db_session import get_session
        with get_session() as s:
            r = s.query(AgentRun).filter(
                AgentRun.agent_name == "chief_researcher",
                AgentRun.symbol == symbol,
                AgentRun.status == "OK",
                AgentRun.start_time >= datetime.now() - timedelta(hours=hours),
            ).order_by(AgentRun.start_time.desc()).first()
            if not r:
                return None
            out = s.query(AgentOutput).filter_by(run_id=r.run_id).first()
            return (out.output_json or {}).get("research_decision") if out else None
    except Exception as exc:
        logger.warning("Agent结论读取失败 %s: %s", symbol, exc)
        return None


def _create_divergence_confirm(broker, symbol: str, name: str, qty: int,
                               price: float, sig_reason: str, agent_view: str,
                               today: date) -> str:
    """策略与Agent买入分歧: 建计划+确认单+邮件, 2分钟超时按策略自动执行。"""
    from workflows.trading_workflow import resume_confirmed_plan
    from notification.notification_service import get_notification_service
    plan_id = f"PLAN-ROT-CFM-{datetime.now():%Y%m%d%H%M%S%f}"
    amount = round(price * qty, 2)
    plan = {
        "plan_id": plan_id, "decision_id": "", "trace_id": "",
        "symbol": symbol, "name": name, "action": "BUY",
        "target_weight": 0.0, "order_amount": amount,
        "estimated_quantity": qty, "order_type": "LIMIT",
        "limit_price": round(price, 4), "confidence": 0.6,
        "reasons": [f"[策略轮动] {sig_reason[:120]}"],
        "risks": [f"Agent观点: {agent_view}(分歧, 需人工确认)"],
        "fallback": "", "human_confirm_required": True,
    }
    try:
        repo.save_trade_plan(plan)
    except Exception as exc:
        logger.warning("分歧计划落库失败 %s: %s", symbol, exc)
    reason = (f"策略与Agent买入分歧: 轮动策略要求买入({sig_reason[:100]}), "
              f"但Agent最近首席结论为{agent_view}。"
              f"2分钟内无人处理将按策略自动执行买入。")
    confirm_id = repo.save_human_confirmation({
        "plan_id": plan_id, "symbol": symbol, "action": "BUY",
        "amount": amount, "risk_level": "MEDIUM",
        "reason": reason, "status": "PENDING",
    })
    try:
        get_notification_service().send_trade_plan_email(
            plan, {"risk_decision": "CONFIRM_REQUIRED", "risk_level": "MEDIUM",
                   "blocked_reason": reason},
            confirm_id=confirm_id, reason=reason)
    except Exception as exc:
        logger.warning("分歧确认邮件失败 %s: %s", symbol, exc)
    # 2分钟超时: 按策略自动执行(批准)
    try:
        timeout = float(get_settings().get(
            "risk.confirmation_policy.auto_decide_timeout_seconds", 120))
    except Exception:
        timeout = 120.0

    def _run():
        import asyncio
        import time as _t
        _t.sleep(max(timeout, 10))
        try:
            c = repo.get_confirmation(confirm_id)
            if c is None or c.status != "PENDING":
                return
            result = asyncio.run(resume_confirmed_plan(
                confirm_id, True, broker, by="auto-timeout"))
            logger.info("轮动分歧确认 %s 超时按策略执行: %s",
                        confirm_id, result.get("status"))
        except Exception as exc:
            logger.error("轮动分歧自动执行失败 %s: %s", confirm_id, exc)

    threading.Thread(target=_run, daemon=True,
                     name=f"rot-confirm-{confirm_id[:8]}").start()
    return confirm_id


def _send_rotation_email(orders: List[Dict[str, Any]], signals, skipped):
    from notification.notification_service import get_notification_service
    lines = []
    for o in orders:
        lines.append(f"- {o['side']} {o['symbol']} {o['qty']}份 @ {o['price']:.3f} — {o['reason']}")
    if not lines:
        lines.append("本次无调仓信号")
    for s in skipped or []:
        lines.append(f"- (跳过) {s}")
    body = ("<div style='font-family:sans-serif'>"
            "<h3>ETF动量轮动 · 当日调仓计划</h3>"
            + "<br/>".join(lines) + "</div>")
    svc = get_notification_service()
    svc.mail.send_email("【量化轮动】当日调仓计划 " + str(date.today()),
                        body, dedup_key=f"rotation:{date.today()}", dedup_minutes=1440)
