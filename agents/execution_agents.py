# -*- coding: utf-8 -*-
"""
风险管理 Agent / 合规审计 Agent / 执行监督 Agent / 复盘总结 Agent
================================================================
- 风控: 调用五层风控硬规则引擎 + LLM 补充解释(风控结论以硬规则为准)
- 合规: 纯规则(交易时段/重复下单/次数限额/参数合法性)
- 执行监督: 纯规则(订单状态机监控/超时撤单建议/未知状态先查询)
- 复盘: 规则统计 + LLM 总结(P1)
"""
import json
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from agents.base_agent import AgentInput, BaseAgent
from pydantic import BaseModel, Field
from risk.risk_engine import get_risk_engine


# ================================================================
# 风险管理 Agent
# ================================================================
class RiskOutput(BaseModel):
    risk_decision: str = Field(..., pattern="^(APPROVE|REJECT|REDUCE|CONFIRM_REQUIRED)$")
    approved_weight: float = Field(0, ge=0, le=1)
    approved_amount: float = Field(0, ge=0)
    approved_quantity: int = Field(0, ge=0)
    risk_level: str = Field(..., pattern="^(LOW|MEDIUM|HIGH)$")
    blocked_reason: str | None = None
    risk_warnings: List[str] = []


class RiskManagerAgent(BaseAgent):
    """风控 Agent: 五层硬规则为主, LLM 只负责解释。"""

    name = "risk_manager"
    task_route = "deep"
    output_schema = RiskOutput

    async def _run_impl(self, input_data: AgentInput) -> Dict[str, Any]:
        ctx = input_data.context or {}
        plan = ctx.get("plan") or {}
        account = ctx.get("account") or {}
        features = ctx.get("technical") or {}
        etf = ctx.get("etf") or {}
        broker = ctx.get("broker")

        # 硬规则审核(权威)
        engine = get_risk_engine()
        r = engine.check_plan(plan, account, features, etf, broker)
        rv = r.to_dict()

        if self.llm.is_mock():
            return {
                "risk_decision": rv["result"],
                "approved_weight": 0.0,
                "approved_amount": rv["approved_amount"],
                "approved_quantity": rv["approved_quantity"],
                "risk_level": rv["risk_level"],
                "blocked_reason": rv["blocked_reason"],
                "risk_warnings": rv["warnings"],
            }

        # 真实模式: LLM 补充解释(不影响硬规则结论)
        content = (
            f"交易计划: {json.dumps(plan, ensure_ascii=False)}\n"
            f"硬规则风控结果: {json.dumps(rv, ensure_ascii=False)}\n"
            f"请以硬规则结论为准, 输出最终风控意见(严格JSON), "
            f"risk_warnings 用中文说明风险点。")
        result = await self.call_llm(content, schema=RiskOutput)
        # 强制以硬规则为准(风控优先级最高)
        result["risk_decision"] = rv["result"]
        result["approved_amount"] = rv["approved_amount"]
        result["approved_quantity"] = rv["approved_quantity"]
        result["risk_level"] = rv["risk_level"]
        result["blocked_reason"] = rv["blocked_reason"] or result.get("blocked_reason")
        return result

    def mock_output(self, input_data: AgentInput) -> Dict[str, Any]:
        # 修复: 原实现 asyncio.run() 在异步上下文中被调用时抛
        # "cannot be called from a running event loop"。风控规则为同步计算, 直接内联。
        ctx = input_data.context or {}
        plan = ctx.get("plan") or {}
        account = ctx.get("account") or {}
        features = ctx.get("technical") or {}
        etf = ctx.get("etf") or {}
        broker = ctx.get("broker")
        engine = get_risk_engine()
        rv = engine.check_plan(plan, account, features, etf, broker).to_dict()
        return {
            "risk_decision": rv["result"],
            "approved_weight": 0.0,
            "approved_amount": rv["approved_amount"],
            "approved_quantity": rv["approved_quantity"],
            "risk_level": rv["risk_level"],
            "blocked_reason": rv["blocked_reason"],
            "risk_warnings": rv["warnings"],
        }


# ================================================================
# 合规审计 Agent (纯规则)
# ================================================================
class ComplianceOutput(BaseModel):
    compliance_status: str = Field(..., pattern="^(PASS|BLOCKED)$")
    reason: str | None = None
    warnings: List[str] = []


class ComplianceAgent(BaseAgent):
    """合规审计: 交易时段/重复下单/次数金额限额/参数合法性。"""

    name = "compliance"
    task_route = "fast"
    output_schema = ComplianceOutput

    async def _run_impl(self, input_data: AgentInput) -> Dict[str, Any]:
        ctx = input_data.context or {}
        plan = ctx.get("plan") or {}
        return self._rules(plan, ctx)

    def _rules(self, plan: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        warnings: List[str] = []
        from core.config import get_settings
        rules = get_settings().section("trading_rules")
        risk_cfg = get_settings().get("risk", {})

        # 1. 交易时段(修复: 原实现用服务器本地时区, UTC 服务器上合规闸门
        #    错判 —— 与调度器一致统一 Asia/Shanghai)
        try:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo("Asia/Shanghai"))
            now = now.replace(tzinfo=None)
        except Exception:
            now = datetime.now()
        if ctx.get("enforce_trading_hours", True) and not self._in_trading_hours(now):
            return {"compliance_status": "BLOCKED",
                    "reason": f"非交易时段({now:%H:%M}), 禁止下单",
                    "warnings": warnings}

        # 2. 重复下单: 同 symbol+side+qty+price 的活跃订单
        from database import repository as repo
        open_orders = repo.get_open_orders()
        limit_price = float(plan.get("limit_price", 0) or 0)
        for o in open_orders:
            if o.symbol == plan.get("symbol") and o.side == plan.get("action"):
                # 修复: 市价单(price=0)同标的同方向全部互判重复 —— 价格0时跳过比对
                if o.price == 0 or limit_price == 0 or abs(o.price - limit_price) < 1e-6:
                    warnings.append(f"存在同方向活跃订单 {o.order_id}, 视为重复下单风险")

        # 3. 每日次数/金额(修复: 只统计非终态的当日订单 —— 原实现把
        #    CANCELLED/REJECTED 也计入, 多次撤单后当日被锁死)
        today = date.today()
        orders_today = [o for o in repo.get_orders_today(today)
                        if o.status not in ("CANCELLED", "REJECTED", "FAILED")]
        daily_count = len(orders_today)
        daily_amount = sum(o.price * o.qty for o in orders_today if o.qty and o.price)
        acc_cfg = risk_cfg.get("account_level", {})
        max_count = int(acc_cfg.get("max_daily_trade_count", 20))
        max_amount = float(acc_cfg.get("max_daily_trade_amount", 50000))
        if daily_count >= max_count:
            return {"compliance_status": "BLOCKED",
                    "reason": f"今日已交易{daily_count}次, 达到上限{max_count}",
                    "warnings": warnings}
        if daily_amount + float(plan.get("order_amount", 0)) > max_amount:
            return {"compliance_status": "BLOCKED",
                    "reason": f"今日交易金额超限({daily_amount:.0f}+{plan.get('order_amount', 0):.0f} > {max_amount:.0f})",
                    "warnings": warnings}

        # 4. 参数合法性
        qty = int(plan.get("estimated_quantity", 0) or 0)
        lot = int(rules.get("lot", {}).get("etf", 100))
        if qty % lot != 0:
            # A股规则: 持仓碎股(非100整数倍)必须一次性全部卖出 —— 这是合法订单。
            # 修复: 原实现一律拦截, 导致含碎股持仓的止损/止盈单永远无法执行。
            # 补充: 卖出后剩余持仓为整手时同样合法(如持350卖250剩100)。
            holding = int(ctx.get("holding_qty") or 0)
            if plan.get("action") != "SELL" or (
                    qty != holding and (holding - qty) % lot != 0):
                return {"compliance_status": "BLOCKED",
                        "reason": f"数量{qty}不是交易单位{lot}的整数倍",
                        "warnings": warnings}
        if qty <= 0 and plan.get("action") in ("BUY", "SELL"):
            return {"compliance_status": "BLOCKED",
                    "reason": "订单数量为0", "warnings": warnings}

        return {"compliance_status": "PASS", "reason": None, "warnings": warnings}

    @staticmethod
    def _in_trading_hours(now: datetime) -> bool:
        if now.weekday() >= 5:
            return False
        t = now.time()
        from datetime import time as dtime
        return (dtime(9, 30) <= t <= dtime(11, 30)) or (dtime(13, 0) <= t <= dtime(15, 0))


# ================================================================
# 执行监督 Agent (纯规则)
# ================================================================
class ExecutionOutput(BaseModel):
    order_status: str = ""
    fill_status: str = ""
    action: str = Field(..., pattern="^(WAIT|CANCEL|QUERY|ALERT)$")
    reason: str = ""
    warnings: List[str] = []


class ExecutionSupervisorAgent(BaseAgent):
    """执行监督: 监控订单状态/部分成交/超时/未知状态。"""

    name = "execution_supervisor"
    task_route = "fast"
    output_schema = ExecutionOutput

    async def _run_impl(self, input_data: AgentInput) -> Dict[str, Any]:
        ctx = input_data.context or {}
        order = ctx.get("order") or {}
        if not order:
            return {"order_status": "", "fill_status": "",
                    "action": "QUERY", "reason": "订单为空, 需查询", "warnings": []}
        status = order.get("status", "")
        filled = int(order.get("filled_qty", 0) or 0)
        qty = int(order.get("qty", 0) or 0)
        from core.config import get_settings
        timeout = int(get_settings().get("trading_rules.cancel.unfilled_timeout_seconds", 300))

        if status == "FILLED":
            return {"order_status": status, "fill_status": "FULL",
                    "action": "WAIT", "reason": "全部成交", "warnings": []}
        if status == "PARTIALLY_FILLED":
            return {"order_status": status, "fill_status": f"PARTIAL {filled}/{qty}",
                    "action": "WAIT", "reason": "部分成交, 继续等待",
                    "warnings": ["剩余部分可能无法成交, 可考虑撤单"]}
        if status == "CANCELLED":
            return {"order_status": status, "fill_status": "NONE",
                    "action": "WAIT", "reason": "已撤单", "warnings": []}
        if status in ("REJECTED", "FAILED"):
            return {"order_status": status, "fill_status": "NONE",
                    "action": "ALERT", "reason": f"订单{status}: {order.get('reject_reason', '')}",
                    "warnings": []}
        if status == "UNKNOWN":
            return {"order_status": status, "fill_status": "UNKNOWN",
                    "action": "QUERY",
                    "reason": "订单状态未知, 必须先查询, 禁止重复提交",
                    "warnings": ["未知状态禁止重复下单(幂等规则)"]}
        # SUBMITTED/ACCEPTED: 超时检查
        submit_time = order.get("submit_time")
        if submit_time:
            elapsed = (datetime.now() - submit_time).total_seconds()
            if elapsed > timeout:
                return {"order_status": status, "fill_status": "NONE",
                        "action": "CANCEL",
                        "reason": f"挂单超时{elapsed:.0f}s未成交, 建议撤单",
                        "warnings": ["撤单后如需重下必须重新过风控"]}
        return {"order_status": status, "fill_status": "NONE",
                "action": "WAIT", "reason": "等待成交", "warnings": []}


# ================================================================
# 复盘总结 Agent
# ================================================================
class ReviewOutput(BaseModel):
    review_summary: str = ""
    pnl_source: List[str] = []
    agent_quality: dict = {}
    improvement: List[str] = []
    key_points: List[str] = []
    risks: List[str] = []


class ReviewAgent(BaseAgent):
    """复盘总结: 日终复盘(P1, 规则统计+LLM总结)。"""

    name = "review"
    task_route = "deep"
    output_schema = ReviewOutput

    async def _run_impl(self, input_data: AgentInput) -> Dict[str, Any]:
        ctx = input_data.context or {}
        day_stats = ctx.get("day_stats") or {}
        if self.llm.is_mock():
            return self.mock_output(input_data)
        content = (
            f"当日交易与账户统计:\n{json.dumps(day_stats, ensure_ascii=False)}\n"
            f"请做日终复盘(严格JSON): review_summary 总结当天表现, "
            f"pnl_source 分析盈亏来源, improvement 给出改进建议。")
        result = await self.call_llm(content, schema=ReviewOutput)
        return result

    def mock_output(self, input_data: AgentInput) -> Dict[str, Any]:
        stats = input_data.context.get("day_stats") or {}
        day_pnl = float(stats.get("day_pnl", 0) or 0)
        trades = stats.get("trade_count", 0)
        return {
            "review_summary": f"当日盈亏{day_pnl:+.0f}元, 交易{trades}笔"
                              f"(模拟复盘, 配置LLM后生成深度分析)",
            "pnl_source": ["持仓浮盈浮动" if day_pnl >= 0 else "持仓浮亏"],
            "agent_quality": {"review": "模拟模式未评估"},
            "improvement": ["配置真实LLM后启用深度复盘"],
            "key_points": [f"当日{day_pnl:+.2%}"],
            "risks": [],
        }
