# -*- coding: utf-8 -*-
"""
交易工作流 (计划→风控→合规→执行)
=================================
首席结论 → 交易员生成计划 → 风控(五层) → 合规 → 人工确认分级 → 模拟盘执行 → 执行监督

Agent 调用关系(关键):
  trader → risk_manager(风控优先级最高) → compliance →
  确认策略(自动/邮件界面/禁止) → paper_broker 下单 → execution_supervisor 监控
"""
import asyncio
import logging
import threading
from typing import Any, Dict, List, Optional

from agents.base_agent import AgentInput
from agents.execution_agents import ComplianceAgent, ExecutionSupervisorAgent
from agents.trader_agent import TraderAgent
from core.config import get_settings
from core.ids import gen_plan_id
from database import repository as repo
from memory.audit_log import AuditLogger
from notification.notification_service import get_notification_service
from paper_trading.paper_broker import PaperBroker
from risk.circuit_breaker import CircuitBreaker
from workflows.graph import WorkflowGraph, WorkflowState

logger = logging.getLogger("workflow.trading")

_TRADER = TraderAgent()
_COMPLIANCE = ComplianceAgent()
_EXEC_SUPERVISOR = ExecutionSupervisorAgent()


# ================================================================
# 节点
# ================================================================

async def node_trader(state: WorkflowState) -> Dict[str, Any]:
    """交易员: 生成交易计划。"""
    ctx = {
        "chief": state.get("chief") or {},
        "account": state.get("account_snapshot") or {},
        "position": state.get("position"),
        "technical": state.get("technical") or {},
        "quote": state.get("quote") or {},
        "summary": state.get("summary"),
        "broker": state.get("broker"),
    }
    plan = await _TRADER.run(AgentInput(symbol=state.symbol, context=ctx))
    plan["plan_id"] = gen_plan_id()
    plan["decision_id"] = (state.get("chief") or {}).get("decision_id", "")
    plan["name"] = state.get("name", "")
    state.record("trader", f"交易计划: {plan.get('action')} {plan.get('estimated_quantity')}股", plan)
    repo.save_trade_plan(plan)
    return {"plan": plan}


async def node_risk_manager(state: WorkflowState) -> Dict[str, Any]:
    """风控审核(五层硬规则, 风控 Agent 封装)。"""
    from agents.execution_agents import RiskManagerAgent
    rm = RiskManagerAgent()
    result = await rm.run(AgentInput(symbol=state.symbol, context={
        "plan": state.get("plan") or {},
        "account": state.get("account_snapshot") or {},
        "technical": state.get("technical") or {},
        "etf": state.get("etf") or {},
        "broker": state.get("broker"),
    }))
    state.record("risk_manager",
                 f"风控结果: {result.get('risk_decision')} level={result.get('risk_level')}",
                 result)
    return {"risk": result}


async def node_compliance(state: WorkflowState) -> Dict[str, Any]:
    """合规审计。"""
    result = await _COMPLIANCE.run(AgentInput(symbol=state.symbol, context={
        "plan": state.get("plan") or {},
        "enforce_trading_hours": state.get("enforce_trading_hours", True),
    }))
    state.record("compliance", f"合规: {result.get('compliance_status')}", result)
    return {"compliance": result}


async def node_execute(state: WorkflowState) -> Dict[str, Any]:
    """执行节点: 依据风控/合规结论执行或挂起人工确认。"""
    plan = state.get("plan") or {}
    risk = state.get("risk") or {}
    compliance = state.get("compliance") or {}
    broker: PaperBroker = state.get("broker")
    audit = AuditLogger.instance()

    # 合规 BLOCKED → 拒绝
    if compliance.get("compliance_status") == "BLOCKED":
        repo.update_plan_status(plan.get("plan_id", ""), "REJECTED")
        state.record("execute", f"合规拦截: {compliance.get('reason')}", compliance)
        return {"execution": {"status": "REJECTED", "reason": compliance.get("reason")}}

    risk_decision = risk.get("risk_decision")
    if risk_decision == "REJECT":
        repo.update_plan_status(plan.get("plan_id", ""), "REJECTED")
        audit.log("plan_rejected", "risk_manager",
                  {"plan_id": plan.get("plan_id"), "reason": risk.get("blocked_reason")})
        return {"execution": {"status": "REJECTED",
                              "reason": risk.get("blocked_reason")}}

    # 修复: 策略与Agent买入分歧 → 强制人工确认(并邮件通知, 2分钟超时按策略执行)。
    # 轮动策略未给出买入信号的 BUY 计划, 一律进人工确认队列:
    #   人工批准 → 按Agent计划执行; 2分钟无人处理 → 自动拒绝(跟随策略)。
    if plan.get("action") == "BUY" and risk_decision not in ("CONFIRM_REQUIRED",):
        strategy_signal = state.get("strategy_signal") or {}
        if strategy_signal.get("signal") != "BUY":
            risk = {**risk, "risk_decision": "CONFIRM_REQUIRED",
                    "blocked_reason": risk.get("blocked_reason")
                    or "策略与Agent买入分歧(策略未给出买入信号), 需人工确认"}
            risk_decision = "CONFIRM_REQUIRED"
            state.record("execute",
                         f"策略与Agent买入分歧(策略信号={strategy_signal.get('signal', '无')}), 需人工确认",
                         {"plan_id": plan.get("plan_id"), "strategy_signal": strategy_signal})

    if risk_decision in ("CONFIRM_REQUIRED", "REDUCE"):
        return await _handle_confirm(state, plan, risk, broker)

    # APPROVE → 模拟执行
    return await _submit_order(state, plan, risk, broker)


async def _handle_confirm(state: WorkflowState, plan, risk, broker) -> Dict[str, Any]:
    """人工确认分级: 创建确认记录 + 发送确认邮件/通知。
    修复: 确认单 reason 只存"中风险交易, 需要人工确认"一句, 人工在界面上
    看不到任何分析依据。现在把首席结论/交易理由/风控提示拼进 reason。
    修复: 创建确认单后自动安排"超时按策略执行"(默认120秒) ——
    人工未在时限内处理时, 按轮动策略方向自动批准/拒绝。"""
    plan_id = plan.get("plan_id", "")
    repo.update_plan_status(plan_id, "PENDING_CONFIRM")

    chief = state.get("chief") or {}
    parts = []
    if plan.get("name"):
        parts.append(f"标的: {plan.get('symbol')} {plan.get('name')}")
    if chief.get("research_decision"):
        conf = chief.get("confidence")
        parts.append(f"首席结论: {chief.get('research_decision')}"
                     f"({'置信 ' + str(round(conf * 100)) + '%' if conf is not None else ''})")
    strategy_signal = state.get("strategy_signal") or {}
    if plan.get("action") == "BUY":
        parts.append(f"轮动策略信号: {strategy_signal.get('signal', '无(与Agent分歧)')}"
                     f"{' - ' + strategy_signal.get('reason', '')[:60] if strategy_signal.get('reason') else ''}")
    plan_reasons = plan.get("reasons") or []
    if plan_reasons:
        parts.append("交易理由: " + "；".join(str(x) for x in plan_reasons[:5]))
    risk_warnings = risk.get("risk_warnings") or []
    if risk_warnings:
        parts.append("风控提示: " + "；".join(str(x) for x in risk_warnings[:5]))
    plan_info = "\n".join(parts) if parts else ""

    confirm_id = repo.save_human_confirmation({
        "plan_id": plan_id,
        "symbol": plan.get("symbol", ""),
        "action": plan.get("action", ""),
        "amount": float(plan.get("order_amount", 0) or 0),
        "risk_level": risk.get("risk_level", "MEDIUM"),
        "reason": (risk.get("blocked_reason") or "中风险交易, 需要人工确认")
                  + ("\n" + plan_info if plan_info else ""),
        "status": "PENDING",
    })
    audit = AuditLogger.instance()
    audit.log("human_confirm_required", "workflow",
              {"confirm_id": confirm_id, "plan_id": plan_id,
               "reason": risk.get("blocked_reason"),
               "analysis": plan_info})
    # 邮件通知(交易计划邮件: 文档15.2 全字段)
    # 修复: 附带 confirm_id 与完整确认原因 —— 邮件里能直接看到"为什么需要确认",
    # 并带"批准/拒绝"签名链接, 点开即可处理, 无需登录网页。
    try:
        get_notification_service().send_trade_plan_email(
            plan, risk, confirm_id=confirm_id,
            reason=(risk.get("blocked_reason") or "中风险交易, 需要人工确认")
                   + ("\n" + plan_info if plan_info else ""))
    except Exception as exc:
        logger.warning("确认邮件发送失败: %s", exc)
    # 超时自动决定(按策略方向, 修复: 无人处理不再无限挂起)
    _schedule_auto_decide(confirm_id, plan, strategy_signal, broker)
    return {"execution": {"status": "PENDING_CONFIRM", "confirm_id": confirm_id,
                          "reason": "等待人工确认"}}


def _schedule_auto_decide(confirm_id: str, plan: Dict[str, Any],
                          strategy_signal: Dict[str, Any], broker: PaperBroker):
    """人工确认超时自动决定(默认120秒, 修复: 需确认单无人处理时无限挂起)。
    按策略方向执行: BUY 需策略给出买入信号才批准, 否则拒绝(跟随策略);
    SELL 默认批准(持仓保护优先), 策略明确看多时拒绝。"""
    try:
        timeout = float(get_settings().get(
            "risk.confirmation_policy.auto_decide_timeout_seconds", 120))
    except Exception:
        timeout = 120.0
    action = str(plan.get("action", "")).upper()
    if action == "BUY":
        approved = (strategy_signal or {}).get("signal") == "BUY"
    else:
        approved = (strategy_signal or {}).get("signal") != "BUY"

    def _run():
        import asyncio
        import time as _t
        _t.sleep(max(timeout, 10))
        try:
            c = repo.get_confirmation(confirm_id)
            if c is None or c.status != "PENDING":
                return
            result = asyncio.run(resume_confirmed_plan(
                confirm_id, approved, broker, by="auto-timeout"))
            logger.info("确认单 %s 超时自动%s(按策略): %s",
                        confirm_id, "批准" if approved else "拒绝",
                        result.get("status"))
        except Exception as exc:
            logger.error("确认单自动决定失败 %s: %s", confirm_id, exc)

    threading.Thread(target=_run, daemon=True,
                     name=f"confirm-auto-{confirm_id[:8]}").start()


async def _submit_order(state: WorkflowState, plan, risk, broker) -> Dict[str, Any]:
    """提交模拟盘订单(幂等 order_intent_id)。"""
    audit = AuditLogger.instance()
    plan_id = plan.get("plan_id", "")
    action = plan.get("action", "HOLD")
    if action in ("HOLD", "CANCEL"):
        repo.update_plan_status(plan_id, "SKIPPED")
        return {"execution": {"status": "SKIPPED", "reason": f"动作 {action} 不执行"}}

    qty = int(risk.get("approved_quantity", 0) or 0) or int(plan.get("estimated_quantity", 0) or 0)
    if qty <= 0:
        repo.update_plan_status(plan_id, "SKIPPED")
        return {"execution": {"status": "SKIPPED", "reason": "数量为0"}}

    order_type = plan.get("order_type", "LIMIT")
    price = float(plan.get("limit_price", 0) or 0)
    if order_type == "MARKET" or price <= 0:
        order_type = "MARKET"
        price = float((state.get("quote") or {}).get("latest_price", 0) or 0)
    # 行情缺失时不能以 0 价提交(LIMIT 0 会冻结 0 资金且永不成交)
    if price <= 0:
        repo.update_plan_status(plan_id, "SKIPPED")
        return {"execution": {"status": "SKIPPED", "reason": "无有效行情价格, 跳过下单"}}

    try:
        order = broker.place_order({
            "symbol": plan.get("symbol", ""),
            "side": action,
            "qty": qty,
            "order_type": order_type,
            "price": price,
            "plan_id": plan_id,
            # 幂等键由 decision_id 派生: 同一决策重复执行(崩溃恢复/重跑)不会重复下单
            "order_intent_id": f"INTENT-{plan.get('decision_id') or plan_id}",
            "name": plan.get("name", ""),
        })
        repo.update_plan_status(plan_id, "ORDERED")
        audit.log("order_placed", "workflow",
                  {"plan_id": plan_id, "order_id": order.get("order_id"),
                   "symbol": order.get("symbol"), "side": action,
                   "qty": qty, "price": price, "risk_decision": risk.get("risk_decision")})
        return {"execution": {"status": "ORDERED", "order_id": order.get("order_id"),
                              "order": order}}
    except ValueError as exc:
        # 修复: 资金不足/可用持仓不足是可恢复的业务性失败(并发扫描竞态、
        # 过期账户快照导致), 不应计入熔断 —— 原实现一律 on_order_failure,
        # 正常交易日的并发下单会累积误熔断, 系统莫名停摆
        repo.update_plan_status(plan_id, "FAILED")
        audit.log("order_failed", "workflow",
                  {"plan_id": plan_id, "error": str(exc), "recoverable": True})
        logger.warning("下单失败(可恢复): %s", exc)
        return {"execution": {"status": "FAILED", "reason": str(exc),
                              "recoverable": True}}
    except Exception as exc:
        repo.update_plan_status(plan_id, "FAILED")
        CircuitBreaker.instance().on_order_failure()
        audit.log("order_failed", "workflow",
                  {"plan_id": plan_id, "error": str(exc)})
        logger.error("下单失败: %s", exc)
        return {"execution": {"status": "FAILED", "reason": str(exc)}}


async def node_supervise(state: WorkflowState) -> Dict[str, Any]:
    """执行监督: 检查订单状态。"""
    execution = state.get("execution") or {}
    order_id = execution.get("order_id")
    if not order_id:
        return {"supervision": {"action": "WAIT", "reason": "无订单"}}
    order = state.get("broker").query_order(order_id)
    result = await _EXEC_SUPERVISOR.run(AgentInput(symbol=state.symbol, context={"order": order}))
    state.record("execution_supervisor", f"订单 {order_id}: {result.get('order_status')}", result)
    return {"supervision": result, "order": order}


# ================================================================
# 图
# ================================================================
def build_trading_graph(progress_cb=None) -> WorkflowGraph:
    g = WorkflowGraph("trading")
    if progress_cb:
        g.progress_cb = progress_cb
    g.add_node("trader", node_trader)
    g.add_node("risk_manager", node_risk_manager)
    g.add_node("compliance", node_compliance)
    g.add_node("execute", node_execute)
    g.add_node("supervise", node_supervise)
    g.add_edge("trader", "risk_manager")
    g.add_edge("risk_manager", "compliance")
    g.add_edge("compliance", "execute")
    g.add_edge("execute", "supervise")
    return g


async def run_trading_workflow(state: WorkflowState,
                               account_snapshot: Dict[str, Any],
                               broker: PaperBroker,
                               enforce_trading_hours: bool = True,
                               progress_cb=None) -> WorkflowState:
    """交易工作流入口(投研工作流产出 chief 后调用)。"""
    state.set("account_snapshot", account_snapshot)
    state.set("broker", broker)
    state.set("enforce_trading_hours", enforce_trading_hours)
    graph = build_trading_graph(progress_cb=progress_cb)
    return await graph.run(state)


# ================================================================
# 人工确认闭环: 批准/拒绝后恢复执行
# ================================================================
def _plan_to_dict(plan) -> Dict[str, Any]:
    """TradePlan ORM 对象 → dict(供风控/下单使用)。"""
    keys = ("plan_id", "decision_id", "trace_id", "symbol", "name", "action",
            "target_weight", "order_amount", "estimated_quantity", "order_type",
            "limit_price", "confidence", "reasons", "risks", "human_confirm_required")
    return {k: getattr(plan, k, None) for k in keys if getattr(plan, k, None) is not None}


async def resume_confirmed_plan(confirm_id: str, approved: bool,
                                broker: PaperBroker, by: str = "web") -> Dict[str, Any]:
    """
    人工确认闭环: 批准 → 重新过风控(批准到成交有时间差, 行情已变化) → 幂等下单。
    拒绝 → 计划标记 REJECTED。
    幂等: 确认单状态非 PENDING 时直接返回, 防止重复审批/重复下单。
    """
    audit = AuditLogger.instance()
    c = repo.get_confirmation(confirm_id)
    if c is None:
        return {"status": "NOT_FOUND", "reason": "确认单不存在"}
    if c.status != "PENDING":
        return {"status": "SKIPPED", "reason": f"确认单已处理({c.status})"}
    repo.decide_confirmation(confirm_id, approved, by=by)

    if not approved:
        repo.update_plan_status(c.plan_id, "REJECTED")
        audit.log("human_confirm_rejected", "workflow",
                  {"confirm_id": confirm_id, "plan_id": c.plan_id, "by": by})
        return {"status": "REJECTED", "reason": "人工拒绝"}

    plan_row = repo.get_trade_plan(c.plan_id)
    if plan_row is None:
        repo.update_plan_status(c.plan_id, "FAILED")
        return {"status": "FAILED", "reason": "交易计划不存在"}
    plan = _plan_to_dict(plan_row)

    # 重新过风控(硬规则为主, 行情/资金已变化)。
    # 修复: 去掉 human_confirm_required 标记 —— 人工已批准, 复检不应再次
    # 因该标记(或高风险卖出)落入 CONFIRM_REQUIRED, 造成批准后永远无法下单。
    recheck_plan = {k: v for k, v in plan.items() if k != "human_confirm_required"}
    from agents.execution_agents import RiskManagerAgent
    risk = await RiskManagerAgent().run(AgentInput(symbol=plan.get("symbol", ""), context={
        "plan": recheck_plan,
        "account": broker.get_account(),
        "broker": broker,
    }))
    rd = risk.get("risk_decision")
    if rd == "REJECT":
        repo.update_plan_status(c.plan_id, "REJECTED")
        audit.log("plan_rejected", "risk_manager",
                  {"plan_id": c.plan_id, "reason": risk.get("blocked_reason")})
        return {"status": "REJECTED", "reason": risk.get("blocked_reason")}
    # 人工已批准: CONFIRM_REQUIRED/REDUCE/APPROVE 均按已确认继续执行
    if rd == "CONFIRM_REQUIRED":
        risk["risk_decision"] = "APPROVE"
        logger.info("确认单 %s 复检为需确认, 因人工已批准直接放行", confirm_id)

    state = WorkflowState(symbol=plan.get("symbol", ""))
    state.set("broker", broker)
    state.set("plan", plan)
    state.set("risk", risk)
    # 取最新行情供市价单定价
    try:
        from data_service.market_data_service import get_market_service
        quote, _ = get_market_service().get_realtime_quote(plan.get("symbol", ""), "etf")
        state.set("quote", quote)
    except Exception as exc:
        logger.warning("确认恢复执行取行情失败 %s: %s", plan.get("symbol"), exc)
        state.set("quote", {})
    exec_ = await _submit_order(state, plan, risk, broker)
    execution = exec_.get("execution") or {}
    audit.log("human_confirm_approved", "workflow",
              {"confirm_id": confirm_id, "plan_id": c.plan_id,
               "execution": execution.get("status"), "by": by})
    return {"status": execution.get("status", "ORDERED"),
            "reason": execution.get("reason", ""), "execution": execution}
