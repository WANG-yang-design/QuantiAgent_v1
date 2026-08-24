# -*- coding: utf-8 -*-
"""
模拟盘轮动策略执行 (回测策略的落地应用)
======================================
回测中的 ETF 动量轮动策略(rotation_executor.signal_fn)与实盘共用同一套
信号函数 —— 保证"回测怎么测, 实盘怎么做"。

调度: agent_schedule.yaml 的 strategy_rotation 任务(默认 14:40 收盘前),
手动: python main.py rotate
流程:
  1. 取监控池(enabled)标的的日K与实时价
  2. 用与回测相同的信号函数计算轮动信号(排名/止损/止盈/市场过滤/冷却期)
  3. 先过硬风控/合规闸门，再由策略直接提交订单
  4. Agent 观点仅与策略信号配对留样，不确认、不延迟、不改变订单
  5. 邮件发送轮动计划摘要
配置: config.yaml strategies.live_rotation (enabled/max_orders_per_day)
"""
import logging
import hashlib
import json
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from core.config import get_settings
from core.timeutil import today as business_today
from database import repository as repo
from strategies.rotation_executor import (
    active_rotation_preset_name,
    build_rotation_signal_fn,
    resolve_rotation_params,
)
from strategies.dynamic_etf_universe import (
    build_current_paper_snapshot,
    dynamic_pool_config,
    latest_paper_universe,
    paper_snapshot_is_current,
)

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
                    "available": int(p.get("available_qty", 0) or 0),
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


def _paper_rotation_universe(watch: List[Dict[str, Any]],
                             held_symbols: List[str]) -> tuple[List[str], Dict[str, str], Dict[str, str]]:
    """Build the exact tradable paper universe; retained holdings stay sellable."""
    from core.symbol_utils import infer_asset_type
    tradable = [w for w in watch
                if (w.get("asset_type") or infer_asset_type(w["symbol"])) in ("etf", "stock")]
    symbols = [w["symbol"] for w in tradable]
    names = {w["symbol"]: w.get("name", "") for w in tradable}
    asset_types = {
        w["symbol"]: w.get("asset_type") or infer_asset_type(w["symbol"])
        for w in tradable
    }
    for sym in held_symbols:
        if sym not in symbols:
            symbols.append(sym)
        asset_types.setdefault(sym, infer_asset_type(sym))
    return symbols, names, asset_types


def run_live_rotation(broker=None, notify: bool = True,
                      dry_run: bool = False) -> Dict[str, Any]:
    """执行一轮模拟盘轮动。

    ``dry_run`` 只计算与 14:40 正式任务完全相同的候选信号，不落策略状态、
    不下单、不发邮件，供 14:30 Agent 定向观察使用。这样候选分析不会提前
    消耗再平衡日计数，也不会改变市场风险状态。
    """
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
    today = business_today()
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
    broker_view = _LiveBrokerView(broker)
    dynamic_cfg = dynamic_pool_config()
    dynamic_snapshot = None
    if dynamic_cfg["enabled"]:
        dynamic_snapshot = latest_paper_universe(today)
        if not paper_snapshot_is_current(dynamic_snapshot, today, dynamic_cfg):
            try:
                dynamic_snapshot = build_current_paper_snapshot(today, dynamic_cfg)
            except Exception as exc:
                logger.error("动态ETF池生成失败，禁止新开仓: %s", exc, exc_info=True)
                dynamic_snapshot = None
        members = list((dynamic_snapshot or {}).get("members") or [])
        symbols = [str(x["symbol"]) for x in members]
        names = {str(x["symbol"]): str(x.get("name") or "") for x in members}
        asset_types = {str(x["symbol"]): "etf" for x in members}
        from core.symbol_utils import infer_asset_type
        for sym in broker_view.positions:
            if sym not in symbols:
                symbols.append(sym)
            asset_types.setdefault(sym, infer_asset_type(sym))
    else:
        symbols, names, asset_types = _paper_rotation_universe(
            watch, list(broker_view.positions))
    if not symbols:
        return {"skipped": ["动态ETF池为空，且当前无持仓；已禁止开仓"]}

    start = today - timedelta(days=150)
    asof: Dict[str, List[dict]] = {}
    prices: Dict[str, float] = {}
    from core.symbol_utils import infer_asset_type
    for sym in symbols:
        try:
            asset_type = asset_types.get(sym, infer_asset_type(sym))
            bars, _ = svc.get_daily_bars(sym, start, today, asset_type)
            if bars:
                asof[sym] = bars
            q, _ = svc.get_realtime_quote(sym, asset_type)
            p = float((q or {}).get("latest_price", 0) or 0)
            if p > 0:
                prices[sym] = p
        except Exception as exc:
            logger.warning("轮动数据获取失败 %s: %s", sym, exc)
    if not asof:
        return {"errors": ["无标的K线数据"]}

    stored = repo.get_system_state("rotation_risk_state") or {}
    risk_state = {
        "off": bool(stored.get("off", False)),
        "off_since": None,
    }
    if stored.get("off_since"):
        try:
            risk_state["off_since"] = date.fromisoformat(str(stored["off_since"])[:10])
        except ValueError:
            pass
    # A selected named strategy is authoritative. Optional config params are a
    # fallback only when no named paper preset is active.
    preset_name = active_rotation_preset_name()
    if dynamic_cfg["enabled"] and not preset_name:
        return {"skipped": [
            "动态ETF池已启用，但尚未选择经动态池重新回测的兼容策略；已禁止自动开仓"
        ], "universe_snapshot": dynamic_snapshot}
    signal_params = ({} if preset_name else dict(cfg.get("params") or {}))
    effective_params = resolve_rotation_params(
        signal_params, use_live_preset=True)
    param_signature = hashlib.sha256(json.dumps(
        effective_params, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
    stored_rebalance = repo.get_system_state("rotation_rebalance_state") or {}
    if stored_rebalance.get("strategy_signature") == param_signature:
        rebalance_state = {
            "signal_day_no": int(stored_rebalance.get("signal_day_no", 0) or 0),
            "last_signal_date": str(stored_rebalance.get("last_signal_date") or ""),
        }
    else:
        rebalance_state = {"signal_day_no": 0, "last_signal_date": ""}
    effective_params["_risk_state"] = risk_state
    effective_params["_rebalance_state"] = rebalance_state
    eligible_symbols = {
        str(x["symbol"]) for x in (dynamic_snapshot or {}).get("members", [])
    } if dynamic_cfg["enabled"] else set(symbols)
    signal_fn = build_rotation_signal_fn(
        initial_cash=float(broker_view.cash or 100000),
        params=effective_params, use_live_preset=False,
        universe_provider=lambda _d, _asof: eligible_symbols)
    signals = signal_fn(asof, prices, today, broker=broker_view) or {}
    persisted_risk_state = {
        "off": bool(risk_state.get("off", False)),
        "off_since": (risk_state["off_since"].isoformat()
                      if isinstance(risk_state.get("off_since"), date) else None),
    }
    if not dry_run:
        repo.update_system_state(
            "rotation_risk_state", lambda state: (
                state.clear(), state.update(persisted_risk_state)))
    persisted_rebalance_state = {
        "strategy_signature": param_signature,
        "strategy_name": preset_name or "__config__",
        "signal_day_no": int(rebalance_state.get("signal_day_no", 0) or 0),
        "last_signal_date": str(rebalance_state.get("last_signal_date") or ""),
    }
    if not dry_run:
        repo.update_system_state(
            "rotation_rebalance_state", lambda state: (
                state.clear(), state.update(persisted_rebalance_state)))

    if dry_run:
        # 正式信号优先；持仓随后补入。即使持仓本轮无交易，也需要在收盘前
        # 留下新鲜 Agent 观点，便于后续评价持有/卖出判断。
        candidate_symbols = list(signals)
        candidate_symbols.extend(
            sym for sym in broker_view.positions if sym not in signals)
        return {
            "signals": signals,
            "candidate_symbols": candidate_symbols,
            "names": {sym: names.get(sym, "") for sym in candidate_symbols},
            "asset_types": {
                sym: asset_types.get(sym, infer_asset_type(sym))
                for sym in candidate_symbols
            },
            "prices": {sym: prices.get(sym, 0) for sym in candidate_symbols},
            "skipped": [],
            "universe_snapshot": dynamic_snapshot,
            "dry_run": True,
        }

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
        if side == "SELL":
            qty = min(qty, int(broker_view.positions.get(sym, {}).get("available", 0) or 0))
            if qty <= 0:
                skipped.append(f"{sym}: T+1 可卖数量为0")
                continue
        # 限价单: 市价附近(买入略高于现价, 卖出略低于现价, 确保成交)
        limit = round(price * (1.001 if side == "BUY" else 0.999), 4)
        order_id = ""
        execution_status = "FAILED"
        allowed, checked_qty, blocked_reason = _strategy_hard_gate(
            broker, sym, names.get(sym, ""), side, qty, limit,
            sig.get("reason", ""), asof.get(sym, []), today)
        if not allowed:
            skipped.append(f"{sym}: 硬风控/合规拦截 - {blocked_reason}")
            execution_status = "RISK_REJECTED"
        else:
            qty = checked_qty
        try:
            if not allowed:
                raise RuntimeError(blocked_reason)
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
            order_id = order.get("order_id") or ""
            execution_status = order.get("status") or "SUBMITTED"
            logger.info("轮动下单 %s %s %d份 @%.3f: %s",
                        side, sym, qty, limit, sig.get("reason", ""))
        except Exception as exc:
            if allowed:
                skipped.append(f"{sym}: {exc}")
            logger.warning("轮动下单失败 %s: %s", sym, exc)

        # Agent 只做影子观察：赞同、反对或无观点均不改变策略订单。
        try:
            from analytics.agent_shadow import record_strategy_observation
            observation = record_strategy_observation(
                strategy_id="etf_momentum_rotation",
                strategy_name=preset_name or "__config__",
                universe_snapshot_id=str((dynamic_snapshot or {}).get("snapshot_id") or ""),
                symbol=sym, name=names.get(sym, ""), signal_date=today,
                strategy_action=side, strategy_reason=sig.get("reason", ""),
                signal_price=price, quantity=qty, order_id=order_id,
                execution_status=execution_status,
                max_agent_age_hours=int(get_settings().get(
                    "agents.shadow.max_age_hours", 24) or 24),
            )
            logger.info("Agent影子样本 %s %s: %s (Agent=%s)",
                        side, sym, observation.get("agreement"),
                        observation.get("agent_decision"))
        except Exception as exc:
            logger.error("Agent影子样本落库失败 %s: %s", sym, exc, exc_info=True)

    if notify:
        try:
            _send_rotation_email(orders, signals, skipped)
        except Exception as exc:
            logger.warning("轮动通知发送失败: %s", exc)
    return {"signals": signals, "orders": orders, "skipped": skipped,
            "universe_snapshot": dynamic_snapshot}


def _strategy_hard_gate(broker, symbol: str, name: str, side: str, qty: int,
                        price: float, reason: str, bars: List[dict],
                        today: date) -> tuple[bool, int, str]:
    """正式策略的硬风控/合规闸门；不读取任何 Agent 观点。"""
    from agents.execution_agents import ComplianceAgent
    from features.technical_indicators import compute_technical_features
    from risk.risk_engine import get_risk_engine

    plan_id = f"PLAN-ROT-{today:%Y%m%d}-{symbol}"
    plan = {
        "plan_id": plan_id, "decision_id": "", "trace_id": "",
        "symbol": symbol, "name": name, "action": side,
        "target_weight": 0.0, "order_amount": round(price * qty, 2),
        "estimated_quantity": qty, "order_type": "LIMIT", "limit_price": price,
        "confidence": 1.0, "reasons": [reason or "正式轮动策略信号"],
        "risks": [], "fallback": "", "human_confirm_required": False,
    }
    account = broker.get_account()
    technical = compute_technical_features(bars or [])
    risk = get_risk_engine().check_plan(plan, account, technical, {}, broker)
    if risk.result == "REJECT":
        return False, qty, risk.blocked_reason or "硬风控拒绝"
    # 这是确定性策略单；模型置信度/人工确认分级不应反向夺取策略交易权。
    # 只有硬规则产生的 REJECT/REDUCE 才改变订单。
    if risk.result == "REDUCE" and risk.approved_quantity > 0:
        qty = int(risk.approved_quantity)
        plan["estimated_quantity"] = qty
        plan["order_amount"] = round(price * qty, 2)
    positions = account.get("positions") or []
    holding = next((int(p.get("total_qty", 0) or 0) for p in positions
                    if p.get("symbol") == symbol), 0)
    compliance = ComplianceAgent()._rules(plan, {
        "enforce_trading_hours": True,
        "holding_qty": holding,
        "account_id": account.get("account_id", "PA-001"),
    })
    if compliance.get("compliance_status") == "BLOCKED":
        return False, qty, compliance.get("reason") or "合规拒绝"
    return True, qty, ""


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
    today = business_today()
    svc.mail.send_email("【量化轮动】当日调仓计划 " + str(today),
                        body, dedup_key=f"rotation:{today}", dedup_minutes=1440)
