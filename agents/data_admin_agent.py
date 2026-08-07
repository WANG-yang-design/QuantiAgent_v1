# -*- coding: utf-8 -*-
"""
数据管理员 Agent (投研流程闸门)
================================
职责: 检查数据完整性/新鲜度/多源冲突, 决定是否允许进入投研流程。
实现: 规则为主(数据质量报告), LLM 仅生成警告说明。
规则优先级(硬性):
  关键行情缺失/延迟超阈值/多源冲突 → BLOCKED
  其余 → PASS + warnings
"""
from typing import Any, Dict, List

from agents.base_agent import AgentInput, BaseAgent
from pydantic import BaseModel, Field


class DataAdminOutput(BaseModel):
    data_status: str = Field(..., pattern="^(PASS|BLOCKED)$")
    warnings: List[str] = []
    blocked_reason: str | None = None


class DataAdminAgent(BaseAgent):
    name = "data_admin"
    task_route = "fast"
    output_schema = DataAdminOutput

    async def _run_impl(self, input_data: AgentInput) -> Dict[str, Any]:
        # 规则部分: 依据上下文里的质量报告
        # 关键数据类别(缺失/延迟/冲突 → 阻断); 其余(盘口/新闻等)仅警告
        critical = {"daily_bar", "minute_bar", "realtime_quote"}
        ctx = input_data.context or {}
        quality_reports = ctx.get("quality_reports") or []
        warnings: List[str] = []
        blocked: List[str] = []

        for rep in quality_reports:
            if not isinstance(rep, dict):
                continue
            status = rep.get("quality_status", "VALID")
            symbol = rep.get("symbol", "")
            category = rep.get("category", "")
            if category not in critical:
                # 非关键数据(盘口/新闻等)缺失只给警告, 不阻断投研
                if status in ("MISSING", "DELAYED"):
                    warnings.append(f"{symbol} {category} 数据不可用(非关键, 仅警告)")
                continue
            if status == "MISSING":
                blocked.append(f"{symbol} {category} 数据缺失")
            elif status == "DELAYED":
                blocked.append(f"{symbol} {category} 数据延迟")
            elif status == "CONFLICT":
                blocked.append(f"{symbol} {category} 多源冲突")
            elif status == "SUSPICIOUS":
                warnings.append(f"{symbol} {category} 疑似异常: {rep.get('warnings')}")
            for w in rep.get("warnings") or []:
                warnings.append(f"{symbol} {category}: {w}")

        # 全局开关(熔断/人工暂停)
        if ctx.get("system_paused"):
            blocked.append("系统已暂停(熔断/人工操作), 禁止交易")

        # ---- 硬规则结论(数据闸门边界由规则决定, LLM 不得推翻) ----
        if blocked:
            return {
                "data_status": "BLOCKED",
                "warnings": warnings,
                "blocked_reason": "；".join(blocked),
            }

        # 模拟/真实模式: LLM 只负责把警告整理成可读说明, 不能改变 PASS/BLOCKED 结论
        if self.llm.is_mock():
            return {"data_status": "PASS", "warnings": warnings, "blocked_reason": None}

        try:
            result = await self.call_llm(
                f"数据质量检查结果:\n" + "\n".join(str(w) for w in quality_reports),
                schema=DataAdminOutput)
            # 硬规则兜底: 以规则结论为准(文档: 硬规则负责风控边界)
            result["data_status"] = "PASS"
            result["blocked_reason"] = None
            if not warnings:
                result["warnings"] = result.get("warnings") or []
            return result
        except Exception as exc:
            # 修复: LLM 故障不应拖死整个投研/交易链路 —— 结论由硬规则决定,
            # LLM 只负责警告润色, 失败时降级返回规则结论
            logger = __import__("logging").getLogger("agent.data_admin")
            logger.warning("数据闸门 LLM 润色失败, 降级为规则结论: %s", exc)
            return {"data_status": "PASS", "warnings": warnings, "blocked_reason": None}

    # 模拟输出: 直接复用规则结果, 不让 LLM 参与
    def mock_output(self, input_data: AgentInput) -> Dict[str, Any]:
        return self._run_impl_blocking(input_data)

    def _run_impl_blocking(self, input_data: AgentInput) -> Dict[str, Any]:
        import asyncio
        return asyncio.run(self._run_impl(input_data))
