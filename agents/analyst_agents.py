# -*- coding: utf-8 -*-
"""
分析师 Agent 组 (并行分析层)
=============================
技术分析师 / ETF专项分析师 / 基本面分析师 / 新闻公告分析师 /
情绪分析师 / 资金流分析师 / 宏观分析师

共同模式: 特征层(脚本)已计算好数字 → 组装市场状态摘要 →
         调用 LLM 给出结构化观点 → Pydantic 校验。
模拟模式: 基类规则化输出(基于特征中性判断)。
"""
from typing import Any, Dict, List, Optional

from agents.base_agent import AgentInput, BaseAgent
from pydantic import BaseModel, Field


class TechnicalOutput(BaseModel):
    agent: str = "technical_analyst"
    view: str = Field(..., pattern="^(bullish|bearish|neutral)$")
    score: float = Field(..., ge=0, le=100)
    confidence: float = Field(..., ge=0, le=1)
    signals: List[str] = []
    invalid_condition: str = ""
    key_points: List[str] = []
    risks: List[str] = []


class TechnicalAnalystAgent(BaseAgent):
    """技术分析师: 趋势/均线/MACD/RSI/成交量/波动率/支撑压力/突破。"""

    name = "technical_analyst"
    task_route = "fast"
    output_schema = TechnicalOutput

    async def _run_impl(self, input_data: AgentInput) -> Dict[str, Any]:
        summary = input_data.context.get("summary", "")
        if self.llm.is_mock():
            return self.mock_output(input_data)
        result = await self.call_llm(self.build_messages(summary), schema=TechnicalOutput)
        result["agent"] = self.name
        return result

    def mock_output(self, input_data: AgentInput) -> Dict[str, Any]:
        tech = input_data.context.get("technical") or {}
        view, score = "neutral", 50.0
        signals: List[str] = []
        risks: List[str] = []
        if tech.get("bull_align"):
            view, score = "bullish", 70.0
            signals.append("均线多头排列")
        elif tech.get("bear_align"):
            view, score = "bearish", 30.0
            signals.append("均线空头排列")
        if tech.get("macd_gold_cross"):
            view = "bullish" if view != "bearish" else view
            score = min(score + 10, 90)
            signals.append("MACD金叉")
        if tech.get("macd_dead_cross"):
            view = "bearish"
            score = max(score - 10, 10)
            signals.append("MACD死叉")
        if tech.get("breakout_20d"):
            view, score = "bullish", max(score, 75)
            signals.append("突破20日高点")
        if tech.get("breakdown_20d"):
            view, score = "bearish", min(score, 25)
            signals.append("跌破20日低点")
        if tech.get("rsi_overbought"):
            risks.append(f"RSI={tech['rsi']:.0f} 超买")
        if tech.get("rsi_oversold"):
            risks.append(f"RSI={tech['rsi']:.0f} 超卖")
        return {
            "agent": self.name, "view": view, "score": round(score, 1),
            "confidence": 0.5, "signals": signals,
            "invalid_condition": "",
            "key_points": [f"20日动量{tech.get('momentum_20d', 0):+.2%}",
                           f"波动率{tech.get('volatility_20d', 0):.1%}"],
            "risks": risks or ["模拟模式: 结论仅供参考"],
        }


class EtfOutput(BaseModel):
    agent: str = "etf_analyst"
    view: str = Field(..., pattern="^(bullish|bearish|neutral)$")
    score: float = Field(..., ge=0, le=100)
    confidence: float = Field(..., ge=0, le=1)
    liquidity_score: float = Field(..., ge=0, le=100)
    premium_risk: str = Field(..., pattern="^(none|low|high)$")
    key_points: List[str] = []
    risks: List[str] = []


class EtfAnalystAgent(BaseAgent):
    """ETF专项分析师: 流动性/规模/折溢价/IOPV/跟踪指数/替代关系。"""

    name = "etf_analyst"
    task_route = "fast"
    output_schema = EtfOutput

    async def _run_impl(self, input_data: AgentInput) -> Dict[str, Any]:
        summary = input_data.context.get("summary", "")
        if self.llm.is_mock():
            return self.mock_output(input_data)
        result = await self.call_llm(self.build_messages(summary), schema=EtfOutput)
        result["agent"] = self.name
        return result

    def mock_output(self, input_data: AgentInput) -> Dict[str, Any]:
        etf = input_data.context.get("etf") or {}
        premium = float(etf.get("premium_rate", 0) or 0)
        liquidity = float(etf.get("liquidity_score", 0) or 0)
        premium_risk = "high" if premium > 0.03 else ("low" if premium > 0.01 else "none")
        view = "bullish" if (premium < 0.01 and liquidity >= 60) else "neutral"
        if premium > 0.03:
            view = "bearish"
        risks = []
        if premium_risk == "high":
            risks.append(f"溢价率{premium:.2%}超阈值, 禁止买入")
        if liquidity < 40:
            risks.append(f"流动性评分{liquidity:.0f}, 成交额不足")
        return {
            "agent": self.name, "view": view,
            "score": round(min(90.0, liquidity - premium * 1000), 1),
            "confidence": 0.5, "liquidity_score": liquidity,
            "premium_risk": premium_risk,
            "key_points": [f"溢价率{premium:+.2%}", f"IOPV偏离{etf.get('iopv_deviation', 0):+.2%}"],
            "risks": risks or ["模拟模式: 结论仅供参考"],
        }


class FundamentalOutput(BaseModel):
    agent: str = "fundamental_analyst"
    view: str = Field(..., pattern="^(bullish|bearish|neutral)$")
    score: float = Field(..., ge=0, le=100)
    confidence: float = Field(..., ge=0, le=1)
    valuation_score: float = Field(0, ge=0, le=100)
    growth_score: float = Field(0, ge=0, le=100)
    key_points: List[str] = []
    risks: List[str] = []


class FundamentalAnalystAgent(BaseAgent):
    """基本面分析师(股票为主, ETF 弱化)。P1 简化实现。"""

    name = "fundamental_analyst"
    task_route = "fast"
    output_schema = FundamentalOutput

    async def _run_impl(self, input_data: AgentInput) -> Dict[str, Any]:
        fund = input_data.context.get("fundamental") or {}
        if not fund:
            return {
                "agent": self.name, "view": "neutral", "score": 50.0,
                "confidence": 0.2, "valuation_score": 50, "growth_score": 50,
                "key_points": ["ETF标的: 基本面数据不适用(弱化处理)"],
                "risks": ["无基本面数据"],
            }
        summary = input_data.context.get("summary", "")
        if self.llm.is_mock():
            return self.mock_output(input_data)
        result = await self.call_llm(self.build_messages(summary), schema=FundamentalOutput)
        result["agent"] = self.name
        return result

    def mock_output(self, input_data: AgentInput) -> Dict[str, Any]:
        fund = input_data.context.get("fundamental") or {}
        if not fund:
            return {
                "agent": self.name, "view": "neutral", "score": 50.0,
                "confidence": 0.2, "valuation_score": 50, "growth_score": 50,
                "key_points": ["ETF: 基本面弱化"], "risks": [],
            }
        pe = float(fund.get("pe", 0) or 0)
        roe = float(fund.get("roe", 0) or 0)
        growth = float(fund.get("profit_growth", 0) or 0)
        val_score = 70 if 0 < pe < 25 else 50
        growth_score = min(90.0, 50 + growth)
        score = (val_score + growth_score) / 2
        view = "bullish" if score >= 60 else "neutral"
        return {
            "agent": self.name, "view": view, "score": round(score, 1),
            "confidence": 0.4, "valuation_score": val_score, "growth_score": growth_score,
            "key_points": [f"PE={pe}", f"ROE={roe}%", f"利润增速={growth}%"],
            "risks": ["基本面数据可能滞后"],
        }


class NewsOutput(BaseModel):
    agent: str = "news_analyst"
    overall_view: str = Field(..., pattern="^(bullish|bearish|neutral)$")
    risk_level: str = Field(..., pattern="^(none|low|medium|high)$")
    score: float = Field(..., ge=0, le=100)
    confidence: float = Field(..., ge=0, le=1)
    event_summary: List[dict] = []
    key_points: List[str] = []
    risks: List[str] = []


class NewsAnalystAgent(BaseAgent):
    """新闻公告分析师: 抽取事件/利好利空分类/识别公告风险(深模型任务)。"""

    name = "news_analyst"
    task_route = "deep"
    output_schema = NewsOutput

    async def _run_impl(self, input_data: AgentInput) -> Dict[str, Any]:
        news_ctx = input_data.context.get("news") or {}
        summary = input_data.context.get("summary", "")
        news_text = news_ctx.get("raw", "无新闻")
        if self.llm.is_mock():
            return self.mock_output(input_data)
        content = (f"{self.build_messages(summary)}\n"
                   f"近期新闻与公告(标题列表):\n{news_text}")
        result = await self.call_llm(content, schema=NewsOutput)
        result["agent"] = self.name
        return result

    def mock_output(self, input_data: AgentInput) -> Dict[str, Any]:
        news_ctx = input_data.context.get("news") or {}
        count = int(news_ctx.get("count", 0))
        risk_ann = int(news_ctx.get("risk_announcements", 0))
        avg = float(news_ctx.get("avg_sentiment", 0) or 0)
        risk_level = "none" if risk_ann == 0 else ("medium" if risk_ann <= 2 else "high")
        view = "neutral"
        if risk_ann >= 1:
            view = "bearish"
        elif avg > 0.3:
            view = "bullish"
        return {
            "agent": self.name, "overall_view": view, "risk_level": risk_level,
            "score": round(50 + avg * 30 - risk_ann * 10, 1),
            "confidence": 0.5,
            "event_summary": [{"type": "公告", "sentiment": view, "impact": risk_level,
                               "summary": f"近期{count}条新闻, {risk_ann}条风险公告"}],
            "key_points": [f"新闻{count}条 平均情绪{avg:+.2f}"],
            "risks": [] if risk_level == "none" else [f"存在{risk_ann}条风险公告"],
        }


class SentimentOutput(BaseModel):
    agent: str = "sentiment_analyst"
    view: str = Field(..., pattern="^(bullish|bearish|neutral)$")
    score: float = Field(..., ge=0, le=100)
    confidence: float = Field(..., ge=0, le=1)
    heat: float = Field(0, ge=0, le=100)
    negative_ratio: float = Field(0, ge=0, le=1)
    extreme_flag: bool = False
    key_points: List[str] = []
    risks: List[str] = []


class SentimentAnalystAgent(BaseAgent):
    """情绪分析师: 舆情热度/情绪分(辅助信号, 不作单独买卖依据)。P1 简化。"""

    name = "sentiment_analyst"
    task_route = "fast"
    output_schema = SentimentOutput

    async def _run_impl(self, input_data: AgentInput) -> Dict[str, Any]:
        st = input_data.context.get("sentiment") or {}
        summary = input_data.context.get("summary", "")
        if not st or not st.get("count"):
            return {
                "agent": self.name, "view": "neutral", "score": 50.0,
                "confidence": 0.2, "heat": 0, "negative_ratio": 0, "extreme_flag": False,
                "key_points": ["无舆情数据(辅助信号缺失, 不影响主流程)"], "risks": [],
            }
        if self.llm.is_mock():
            return self.mock_output(input_data)
        result = await self.call_llm(self.build_messages(summary), schema=SentimentOutput)
        result["agent"] = self.name
        return result

    def mock_output(self, input_data: AgentInput) -> Dict[str, Any]:
        st = input_data.context.get("sentiment") or {}
        avg = float(st.get("avg_score", 0) or 0)
        neg = float(st.get("negative_ratio", 0) or 0)
        heat = float(st.get("heat", 0) or 0)
        extreme = bool(heat > 50000 or avg > 0.7 or avg < -0.7)
        view = "bullish" if avg > 0.15 else ("bearish" if avg < -0.15 else "neutral")
        return {
            "agent": self.name, "view": view,
            "score": round(50 + avg * 40, 1), "confidence": 0.4,
            "heat": min(100.0, heat / 1000), "negative_ratio": neg,
            "extreme_flag": extreme,
            "key_points": [f"舆情{st.get('count', 0)}条 平均{avg:+.2f} 负向{neg:.0%}"],
            "risks": ["情绪信号仅辅助, 不作为买卖依据"] + (["舆情过热, 警惕拥挤"] if extreme else []),
        }


class MoneyFlowOutput(BaseModel):
    agent: str = "money_flow_analyst"
    view: str = Field(..., pattern="^(bullish|bearish|neutral)$")
    score: float = Field(..., ge=0, le=100)
    confidence: float = Field(..., ge=0, le=1)
    flow_score: float = Field(0, ge=0, le=100)
    key_points: List[str] = []
    risks: List[str] = []


class MoneyFlowAnalystAgent(BaseAgent):
    """资金流分析师: 主力资金方向/强弱(短期参考)。P1 简化。"""

    name = "money_flow_analyst"
    task_route = "fast"
    output_schema = MoneyFlowOutput

    async def _run_impl(self, input_data: AgentInput) -> Dict[str, Any]:
        mf = input_data.context.get("money_flow") or {}
        summary = input_data.context.get("summary", "")
        if not mf:
            return {
                "agent": self.name, "view": "neutral", "score": 50.0,
                "confidence": 0.2, "flow_score": 50,
                "key_points": ["无资金流数据(ETF 资金流接口有限)"], "risks": [],
            }
        if self.llm.is_mock():
            return self.mock_output(input_data)
        result = await self.call_llm(self.build_messages(summary), schema=MoneyFlowOutput)
        result["agent"] = self.name
        return result

    def mock_output(self, input_data: AgentInput) -> Dict[str, Any]:
        mf = input_data.context.get("money_flow") or {}
        main = float(mf.get("main_inflow", 0) or 0)
        flow_score = 80.0 if main > 0 else 20.0
        view = "bullish" if main > 0 else "bearish"
        return {
            "agent": self.name, "view": view, "score": flow_score,
            "confidence": 0.4, "flow_score": flow_score,
            "key_points": [f"主力净流入{main/1e4:+.0f}万"],
            "risks": ["资金流仅短期参考"],
        }


class MacroOutput(BaseModel):
    agent: str = "macro_analyst"
    view: str = Field(..., pattern="^(bullish|bearish|neutral)$")
    score: float = Field(..., ge=0, le=100)
    confidence: float = Field(..., ge=0, le=1)
    market_state: str = Field(..., pattern="^(risk_on|neutral|risk_off)$")
    key_points: List[str] = []
    risks: List[str] = []


class MacroAnalystAgent(BaseAgent):
    """宏观与市场环境分析师: 大盘状态/风险偏好/攻防判断。"""

    name = "macro_analyst"
    task_route = "fast"
    output_schema = MacroOutput

    async def _run_impl(self, input_data: AgentInput) -> Dict[str, Any]:
        env = input_data.context.get("market_env") or {}
        summary = input_data.context.get("summary", "")
        if not env:
            return {
                "agent": self.name, "view": "neutral", "score": 50.0,
                "confidence": 0.3, "market_state": "neutral",
                "key_points": ["无市场环境数据"], "risks": [],
            }
        if self.llm.is_mock():
            return self.mock_output(input_data)
        result = await self.call_llm(self.build_messages(summary), schema=MacroOutput)
        result["agent"] = self.name
        return result

    def mock_output(self, input_data: AgentInput) -> Dict[str, Any]:
        env = input_data.context.get("market_env") or {}
        mom = float(env.get("index_momentum_20d", 0) or 0)
        if mom > 0.03:
            view, state, score = "bullish", "risk_on", 70.0
        elif mom < -0.03:
            view, state, score = "bearish", "risk_off", 30.0
        else:
            view, state, score = "neutral", "neutral", 50.0
        return {
            "agent": self.name, "view": view, "score": score,
            "confidence": 0.5, "market_state": state,
            "key_points": [f"沪深300 20日动量{mom:+.2%} → {state}"],
            "risks": ["宏观环境仅控制整体仓位环境"],
        }
