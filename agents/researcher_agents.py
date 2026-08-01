# -*- coding: utf-8 -*-
"""
研究员 Agent 组 (辩论层 + 投研委员会)
=====================================
看多研究员(构造买入理由) ↔ 看空研究员(专门反对)
→ 首席研究员(综合多空结论, 不直接下单)

辩论规则(防流于形式, 对应文档 22.3 改进项):
- 辩论最少 1 轮: 看空必须先看完多理由, 逐一反驳; 看多再回应
- 僵局处理: 双方观点僵持且无新增论据 → 首席按置信度权重裁决
- 首席必须同时引用双方最有力论据, 禁止只取一边
"""
import json
from typing import Any, Dict, List

from agents.base_agent import AgentInput, BaseAgent
from pydantic import BaseModel, Field


class BullOutput(BaseModel):
    agent: str = "bull_researcher"
    view: str = Field(..., pattern="^(bullish|neutral)$")
    score: float = Field(..., ge=0, le=100)
    confidence: float = Field(..., ge=0, le=1)
    bull_points: List[str] = []
    upside_reason: str = ""
    valid_conditions: List[str] = []
    key_points: List[str] = []
    risks: List[str] = []


class BullResearcherAgent(BaseAgent):
    """看多研究员: 基于分析师输出构造买入/持有理由。"""

    name = "bull_researcher"
    task_route = "deep"
    output_schema = BullOutput

    async def _run_impl(self, input_data: AgentInput) -> Dict[str, Any]:
        ctx = input_data.context or {}
        analyst_outputs = ctx.get("analyst_outputs") or {}
        if self.llm.is_mock():
            return self.mock_output(input_data)
        content = self._material(analyst_outputs, bear_rebuttal=ctx.get("bear_rebuttal"))
        result = await self.call_llm(content, schema=BullOutput)
        result["agent"] = self.name
        return result

    def mock_output(self, input_data: AgentInput) -> Dict[str, Any]:
        outputs = input_data.context.get("analyst_outputs") or {}
        bulls = [o for o in outputs.values()
                 if isinstance(o, dict) and o.get("view") == "bullish"]
        bears = [o for o in outputs.values()
                 if isinstance(o, dict) and o.get("view") == "bearish"]
        points = [o.get("key_points", [""])[0] for o in bulls if o.get("key_points")]
        score = 50.0 + min(len(bulls) * 8, 30) - len(bears) * 5
        view = "bullish" if score >= 55 else "neutral"
        return {
            "agent": self.name, "view": view, "score": round(score, 1),
            "confidence": 0.5, "bull_points": points,
            "upside_reason": "看多Agent数量占优(模拟)" if points else "暂无看多依据",
            "valid_conditions": [], "key_points": points, "risks": [],
        }

    @staticmethod
    def _material(outputs: Dict[str, Any], bear_rebuttal: str = "") -> str:
        """把各分析师结论序列化为辩论材料。"""
        lines = ["以下是各分析师结论(JSON):", json.dumps(outputs, ensure_ascii=False)]
        if bear_rebuttal:
            lines.append(f"看空研究员的反驳意见(必须正面回应):\n{bear_rebuttal}")
        lines.append("请基于以上材料, 构造最有力的买入/持有理由(严格JSON)。")
        return "\n".join(lines)


class BearOutput(BaseModel):
    agent: str = "bear_researcher"
    view: str = Field(..., pattern="^(bearish|neutral)$")
    score: float = Field(..., ge=0, le=100)
    confidence: float = Field(..., ge=0, le=1)
    bear_points: List[str] = []
    downside_risk: str = ""
    key_points: List[str] = []
    risks: List[str] = []


class BearResearcherAgent(BaseAgent):
    """看空研究员: 专门找问题, 防止系统自嗨。"""

    name = "bear_researcher"
    task_route = "deep"
    output_schema = BearOutput

    async def _run_impl(self, input_data: AgentInput) -> Dict[str, Any]:
        ctx = input_data.context or {}
        analyst_outputs = ctx.get("analyst_outputs") or {}
        if self.llm.is_mock():
            return self.mock_output(input_data)
        content = self._material(analyst_outputs)
        result = await self.call_llm(content, schema=BearOutput)
        result["agent"] = self.name
        return result

    def mock_output(self, input_data: AgentInput) -> Dict[str, Any]:
        outputs = input_data.context.get("analyst_outputs") or {}
        risks = []
        for o in outputs.values():
            if isinstance(o, dict):
                risks += o.get("risks") or []
        bears = [o for o in outputs.values()
                 if isinstance(o, dict) and o.get("view") == "bearish"]
        score = 50.0 - len(bears) * 8 - min(len(risks) * 3, 15)
        view = "bearish" if score <= 45 else "neutral"
        return {
            "agent": self.name, "view": view, "score": round(score, 1),
            "confidence": 0.5,
            "bear_points": list(dict.fromkeys(risks))[:5] or ["未发现明显风险(模拟)"],
            "downside_risk": "追高风险与情绪过热需警惕(模拟)" if risks else "暂无",
            "key_points": [], "risks": risks[:5],
        }

    @staticmethod
    def _material(outputs: Dict[str, Any]) -> str:
        lines = ["以下是各分析师结论(JSON), 请扮演首席反对者找出最严重的问题:",
                 json.dumps(outputs, ensure_ascii=False),
                 "要求: 必须提出至少2条实质性质疑(数据缺陷/假设错误/风险遗漏), 严禁敷衍(严格JSON)。"]
        return "\n".join(lines)


class ChiefOutput(BaseModel):
    research_decision: str = Field(..., pattern="^(BUY_CANDIDATE|SELL_CANDIDATE|HOLD|EXCLUDE)$")
    confidence: float = Field(..., ge=0, le=1)
    expected_holding_period: str = ""
    upside_reason: str = ""
    downside_risk: str = ""
    key_monitoring_points: List[str] = []
    bull_summary: str = ""
    bear_summary: str = ""
    score: float = Field(..., ge=0, le=100)


class ChiefResearcherAgent(BaseAgent):
    """首席研究员: 综合多空, 形成研究结论(不下单)。"""

    name = "chief_researcher"
    task_route = "deep"
    output_schema = ChiefOutput

    async def _run_impl(self, input_data: AgentInput) -> Dict[str, Any]:
        ctx = input_data.context or {}
        bull = ctx.get("bull") or {}
        bear = ctx.get("bear") or {}
        if self.llm.is_mock():
            return self.mock_output(input_data)
        content = (
            "看多研究员结论:\n" + json.dumps(bull, ensure_ascii=False) +
            "\n看空研究员结论:\n" + json.dumps(bear, ensure_ascii=False) +
            "\n请综合双方观点给出最终研究结论(严格JSON)。"
            "要求: bull_summary 和 bear_summary 都要写, 不得只取一边。")
        result = await self.call_llm(content, schema=ChiefOutput)
        return result

    def mock_output(self, input_data: AgentInput) -> Dict[str, Any]:
        ctx = input_data.context or {}
        bull = ctx.get("bull") or {}
        bear = ctx.get("bear") or {}
        bull_score = float(bull.get("score", 50) or 50)
        bear_score = float(bear.get("score", 50) or 50)
        # 综合: 多空分差 + 置信度加权
        net = bull_score - bear_score
        confidence = max(0.3, min(0.8, 0.5 + net / 200))
        if net >= 15:
            decision = "BUY_CANDIDATE"
        elif net <= -15:
            decision = "SELL_CANDIDATE"
        else:
            decision = "HOLD"
        if bear.get("risks"):
            for r in bear["risks"][:2]:
                if "禁止" in str(r) or "退市" in str(r):
                    decision = "EXCLUDE"
        return {
            "research_decision": decision,
            "confidence": round(confidence, 2),
            "expected_holding_period": "5-20个交易日",
            "upside_reason": bull.get("upside_reason", ""),
            "downside_risk": bear.get("downside_risk", ""),
            "key_monitoring_points": [],
            "bull_summary": "看多: " + str(bull.get("bull_points", [""])[0] if bull.get("bull_points") else ""),
            "bear_summary": "看空: " + str(bear.get("bear_points", [""])[0] if bear.get("bear_points") else ""),
            "score": round(50 + net / 2, 1),
        }
