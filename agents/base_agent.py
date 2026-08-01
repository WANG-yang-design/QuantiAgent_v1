# -*- coding: utf-8 -*-
"""
基础 Agent 类
==============
所有 15 个 Agent 的基类, 提供:
- LLM 调用(快/深模型路由 + 结构化输出)
- Prompt 加载(版本化)
- 工具权限校验(Skill 管理)
- 运行记录与输出落库(审计)
- 模拟模式下的确定性输出

Agent 之间的调用关系(关键):
  数据管理员(闸门) → 分析师们(并行) → 看多/看空研究员(辩论) →
  首席研究员(汇总) → 交易员(计划) → 风控(审核) → 合规(审计) → 执行监督(监控)
"""
import logging
import traceback
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Type

from pydantic import BaseModel

from core.ids import gen_run_id
from core.llm import get_llm
from core.logging import get_logger, get_trace_id
from core.prompt_manager import get_prompt_manager
from database import repository as repo


class AgentInput(BaseModel):
    """Agent 标准输入: 标的 + 上下文(市场摘要/特征/其他Agent输出)。"""
    symbol: str = ""
    context: Dict[str, Any] = {}


class AgentOutput(BaseModel):
    """Agent 标准输出 (分析师通用)。"""
    agent_name: str = ""
    view: str = "neutral"            # bullish / bearish / neutral
    score: float = 50.0              # 0-100
    confidence: float = 0.5          # 0-1
    key_points: List[str] = []
    risks: List[str] = []


class BaseAgent(ABC):
    """Agent 基类。"""

    name: str = "base"
    task_route: str = "fast"         # fast / deep (模型路由)
    output_schema: Type[BaseModel] = AgentOutput

    def __init__(self):
        self.logger = get_logger(f"agent.{self.name}")
        self.llm = get_llm()
        self.prompt = get_prompt_manager().get(self.name)

    # ---------------------------------------------------------------
    # 主入口: 带审计/日志/权限的 run 包装
    # ---------------------------------------------------------------
    async def run(self, input_data: AgentInput) -> Dict[str, Any]:
        """执行 Agent: 记录 run_id, 校验工具权限, 调用业务逻辑, 落库。"""
        trace_id = get_trace_id() or gen_run_id()
        run = repo.start_agent_run(self.name, input_data.symbol, trace_id,
                                   model_name=self.llm.get_model(self.task_route))
        # 注意: run_id 必须用 start_agent_run 返回对象里的(数据库实际入库的),
        # 不能另生成, 否则 finish/save 查不到记录(此前审计表一直为空的原因)
        run_id = run.run_id
        try:
            # 工具权限预检: 每个 Agent 的方法即"工具", 由权限表控制
            self.check_tool_access("run")
            output = await self._run_impl(input_data)
            repo.finish_agent_run(run_id, "OK")
            repo.save_agent_output(
                run_id=run_id, agent_name=self.name,
                view=output.get("view", output.get("research_decision",
                      output.get("risk_decision", ""))),
                score=float(output.get("score", 0) or 0),
                confidence=float(output.get("confidence", 0) or 0),
                output_json=output,
            )
            self.logger.info("[%s] %s 完成 → %s",
                             run_id, self.name, str(output)[:200])
            return output
        except Exception as exc:
            repo.finish_agent_run(run_id, "FAILED", str(exc))
            self.logger.error("[%s] %s 失败: %s\n%s",
                              run_id, self.name, exc, traceback.format_exc())
            raise

    @abstractmethod
    async def _run_impl(self, input_data: AgentInput) -> Dict[str, Any]:
        """子类实现: 真正的 Agent 逻辑。"""

    # ---------------------------------------------------------------
    # LLM 调用封装
    # ---------------------------------------------------------------
    async def call_llm(self, user_content: str,
                       schema: Optional[Type[BaseModel]] = None) -> Dict[str, Any]:
        """调用 LLM: 系统提示=Agent prompt, 用户内容=上下文摘要。"""
        messages = [{"role": "user", "content": user_content}]
        return await self.llm.complete(messages, task=self.task_route, schema=schema)

    def build_messages(self, summary: str) -> str:
        """把市场状态摘要组装为用户消息(含 Agent prompt 引导)。"""
        return (f"{self.prompt}\n\n以下为市场状态摘要(脚本计算, 可信):\n{summary}\n\n"
                f"请基于摘要给出你的结构化结论(严格JSON)。")

    # ---------------------------------------------------------------
    # 工具权限 (Skill 管理)
    # ---------------------------------------------------------------
    def check_tool_access(self, tool_name: str):
        perms = repo.get_tool_permission(self.name)
        if tool_name in perms and perms[tool_name] == "deny":
            raise PermissionError(
                f"Agent[{self.name}] 无工具权限: {tool_name}(已按 Skill 权限表拒绝)")

    # ---------------------------------------------------------------
    # 模拟模式: 规则化确定性输出(无 LLM 时全链路可跑)
    # ---------------------------------------------------------------
    def mock_output(self, input_data: AgentInput) -> Dict[str, Any]:
        """默认模拟输出: 依据上下文中的特征中性判断。
        关键执行链 Agent(交易员/风控)会覆写此方法做规则化决策。"""
        ctx = input_data.context or {}
        tech = ctx.get("technical") or {}
        score = 50.0
        view = "neutral"
        pts = ["模拟模式: 未配置LLM, 依据特征中性判断"]
        if tech:
            score = 50.0 + float(tech.get("momentum_20d", 0) or 0) * 100
            if float(tech.get("momentum_20d", 0) or 0) > 0.05 and tech.get("price_above_ma20"):
                view, score = "bullish", min(80.0, score)
            if tech.get("macd_dead_cross") or tech.get("breakdown_20d"):
                view = "bearish"
                score = max(20.0, score - 25)
            pts = [f"模拟依据: 20日动量{tech.get('momentum_20d', 0):+.2%}, "
                   f"站上MA20={'是' if tech.get('price_above_ma20') else '否'}"]
        out = {
            "agent_name": self.name,
            "view": view,
            "score": round(score, 1),
            "confidence": 0.5,
            "key_points": pts,
            "risks": ["模拟模式: 结论仅供参考, 配置LLM后获得真实分析"],
        }
        return out
