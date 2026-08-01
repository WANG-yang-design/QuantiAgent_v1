# -*- coding: utf-8 -*-
"""
交易员 Agent (执行链核心)
=========================
把研究结论转化为可执行交易计划:
  结合账户资金/持仓/可用股数/T+1/手续费/最小交易单位/盘口价格
→ 输出完整订单计划(TradePlan)

模拟模式: 规则化生成(研究结论→目标仓位→资金约束→整数股)
真实模式: LLM 生成 + 规则层校验修正(数量整数化/资金约束)
"""
import json
import math
from typing import Any, Dict, List, Optional

from agents.base_agent import AgentInput, BaseAgent
from pydantic import BaseModel, Field


class TradePlanOutput(BaseModel):
    action: str = Field(..., pattern="^(BUY|SELL|HOLD|CANCEL)$")
    symbol: str = ""
    name: str = ""
    target_weight: float = Field(..., ge=0, le=1)
    order_amount: float = Field(..., ge=0)
    estimated_quantity: int = Field(..., ge=0)
    order_type: str = Field(..., pattern="^(LIMIT|MARKET)$")
    limit_price: float | None = None
    confidence: float = Field(..., ge=0, le=1)
    reasons: List[str] = []
    risks: List[str] = []
    fallback: str = ""
    human_confirm_required: bool = False


class TraderAgent(BaseAgent):
    """交易员: 研究结论 → 交易计划。"""

    name = "trader"
    task_route = "deep"
    output_schema = TradePlanOutput

    # 默认目标仓位(无研究结论时)
    DEFAULT_WEIGHT = 0.2

    async def _run_impl(self, input_data: AgentInput) -> Dict[str, Any]:
        ctx = input_data.context or {}
        chief = ctx.get("chief") or {}
        account = ctx.get("account") or {}
        position = ctx.get("position")
        features = ctx.get("technical") or {}
        price = float(features.get("close", 0) or 0)
        if price <= 0:
            price = float((ctx.get("quote") or {}).get("latest_price", 0) or 0)

        # 规则层先行: 决策倾向
        decision = chief.get("research_decision", "HOLD")
        confidence = float(chief.get("confidence", 0.5) or 0.5)

        if self.llm.is_mock():
            return self._rule_plan(input_data, decision, confidence, price, account, position, chief)

        # 真实模式: LLM 生成 + 规则修正
        summary = ctx.get("summary", "")
        content = (
            f"{self.build_messages(summary)}\n"
            f"首席研究员结论: {json.dumps(chief, ensure_ascii=False)}\n"
            f"账户: {json.dumps({k: v for k, v in account.items() if k != 'positions'}, ensure_ascii=False)}\n"
            f"当前持仓: {json.dumps(position or {}, ensure_ascii=False)}\n"
            f"请生成交易计划(严格JSON)。")
        plan = await self.call_llm(content, schema=TradePlanOutput)
        plan = self._rule_correct(plan, input_data, price, account, position, decision)
        return plan

    # ---------------------------------------------------------------
    def _rule_plan(self, input_data, decision, confidence, price, account, position, chief) -> Dict[str, Any]:
        """规则化生成交易计划(模拟模式 + 真实模式兜底)。"""
        symbol = input_data.symbol
        name = (input_data.context.get("name") or "").strip()
        total_asset = float(account.get("total_asset", 0) or 0)
        cash = float(account.get("cash", 0) or 0)
        positions = account.get("positions") or []
        cur = next((p for p in positions if p.get("symbol") == symbol), None)

        if decision == "BUY_CANDIDATE" and confidence >= 0.5:
            action = "BUY"
        elif decision == "SELL_CANDIDATE" and confidence >= 0.5:
            action = "SELL"
        else:
            return {
                "action": "HOLD", "symbol": symbol, "name": name,
                "target_weight": 0.0, "order_amount": 0.0, "estimated_quantity": 0,
                "order_type": "LIMIT", "limit_price": None,
                "confidence": confidence, "reasons": ["研究结论为持有/观望"],
                "risks": [], "fallback": "", "human_confirm_required": False,
            }

        if action == "BUY":
            # 目标金额: 保守取总资产20%, 不超过可用现金
            target = total_asset * self.DEFAULT_WEIGHT
            # 若已有持仓, 只买差额
            cur_value = float(cur.get("market_value", 0) or 0) if cur else 0.0
            target = max(0.0, target - cur_value)
            target = min(target, cash * 0.9)
            lot = 100
            qty = int(target / price // lot * lot) if price > 0 else 0
            amount = qty * price
            return {
                "action": "BUY", "symbol": symbol, "name": name,
                "target_weight": round(self.DEFAULT_WEIGHT, 3),
                "order_amount": round(amount, 2),
                "estimated_quantity": qty,
                "order_type": "LIMIT",
                "limit_price": round(price * 1.005, 3),      # 限价略高于现价
                "confidence": round(confidence, 2),
                "reasons": chief.get("upside_reason") or "研究结论看多",
                "risks": chief.get("downside_risk") or "",
                "fallback": f"若价格高于{price*1.02:.3f}则取消",
                "human_confirm_required": False,
            }
        else:  # SELL
            avail = int(cur.get("available_qty", 0) or 0) if cur else 0
            qty = avail  # 全卖(可用部分)
            return {
                "action": "SELL", "symbol": symbol, "name": name,
                "target_weight": 0.0,
                "order_amount": round(qty * price, 2),
                "estimated_quantity": qty,
                "order_type": "LIMIT",
                "limit_price": round(price * 0.995, 3),
                "confidence": round(confidence, 2),
                "reasons": chief.get("downside_risk") or "研究结论看空",
                "risks": [],
                "fallback": f"若价格低于{price*0.98:.3f}则市价卖出",
                "human_confirm_required": False,
            }

    def _rule_correct(self, plan: Dict[str, Any], input_data, price, account,
                      position, decision) -> Dict[str, Any]:
        """规则修正 LLM 输出: 数量整数化/资金持仓约束/决策方向约束。"""
        symbol = input_data.symbol
        # 数量修正为100股整数
        qty = int(plan.get("estimated_quantity", 0) or 0)
        qty = qty // 100 * 100
        if plan.get("action") == "BUY":
            cash = float(account.get("cash", 0) or 0)
            max_qty = int(cash / price // 100 * 100) if price > 0 else 0
            qty = min(qty, max_qty)
            if qty < 100:
                plan["action"] = "HOLD"
                plan["estimated_quantity"] = 0
                plan["order_amount"] = 0.0
                return plan
        elif plan.get("action") == "SELL":
            positions = account.get("positions") or []
            cur = next((p for p in positions if p.get("symbol") == symbol), None)
            avail = int(cur.get("available_qty", 0) or 0) if cur else 0
            qty = min(qty, avail)
            if qty < 100:
                plan["action"] = "HOLD"
                plan["estimated_quantity"] = 0
                plan["order_amount"] = 0.0
                return plan
        # 决策方向约束: 研究结论非多头时不允许BUY
        if decision == "HOLD" and plan.get("action") == "BUY":
            plan["action"] = "HOLD"
            plan["estimated_quantity"] = 0
            plan["order_amount"] = 0.0
            return plan
        plan["estimated_quantity"] = qty
        plan["order_amount"] = round(qty * price, 2)
        return plan
