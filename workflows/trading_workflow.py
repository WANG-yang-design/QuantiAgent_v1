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

    if risk_decision in ("CONFIRM_REQUIRED", "REDUCE"):
        return await _handle_confirm(state, plan, risk, broker)

    # APPROVE → 模拟执行
    return await _submit_order(state, plan, risk, broker)


async def _handle_confirm(state: WorkflowState, plan, risk, broker) -> Dict[str, Any]:
    """人工确认分级: 创建确认记录 + 发送确认邮件/通知。"""
    plan_id = plan.get("plan_id", "")
    repo.update_plan_status(plan_id, "PENDING_CONFIRM")
    confirm_id = repo.save_human_confirmation({
        "plan_id": plan_id,
        "symbol": plan.get("symbol", ""),
        "action": plan.get("action", ""),
        "amount": float(plan.get("order_amount", 0) or 0),
        "risk_level": risk.get("risk_level", "MEDIUM"),
        "reason": risk.get("blocked_reason") or "中风险交易, 需要人工确认",
        "status": "PENDING",
    })
    audit = AuditLogger.instance()
    audit.log("human_confirm_required", "workflow",
              {"confirm_id": confirm_id, "plan_id": plan_id,
               "reason": risk.get("blocked_reason")})
    # 邮件通知(交易计划邮件: 文档15.2 全字段)
    try:
        get_notification_service().send_trade_plan_email(plan, risk)
    except Exception as exc:
        logger.warning("确认邮件发送失败: %s", exc)
    return {"execution": {"status": "PENDING_CONFIRM", "confirm_id": confirm_id,
                          "reason": "等待人工确认"}}


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
    order_type = "LIMIT" if order_type == "MARKET" and price <= 0 else order_type

    try:
        order = broker.place_order({
            "symbol": plan.get("symbol", ""),
            "side": action,
            "qty": qty,
            "order_type": order_type,
            "price": price,
            "plan_id": plan_id,
            "order_intent_id": f"INTENT-{plan_id}",
            "name": plan.get("name", ""),
        })
        repo.update_plan_status(plan_id, "ORDERED")
        audit.log("order_placed", "workflow",
                  {"plan_id": plan_id, "order_id": order.get("order_id"),
                   "symbol": order.get("symbol"), "side": action,
                   "qty": qty, "price": price, "risk_decision": risk.get("risk_decision")})
        return {"execution": {"status": "ORDERED", "order_id": order.get("order_id"),
                              "order": order}}
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
def build_trading_graph() -> WorkflowGraph:
    g = WorkflowGraph("trading")
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
                               enforce_trading_hours: bool = True) -> WorkflowState:
    """交易工作流入口(投研工作流产出 chief 后调用)。"""
    state.set("account_snapshot", account_snapshot)
    state.set("broker", broker)
    state.set("enforce_trading_hours", enforce_trading_hours)
    graph = build_trading_graph()
    return await graph.run(state)
