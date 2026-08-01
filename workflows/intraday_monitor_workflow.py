# -*- coding: utf-8 -*-
"""
盘中监控工作流 (完整主流程)
===========================
实时行情/盘口/资金流/新闻/舆情更新 → 数据质量检查 → 特征计算 →
技术/ETF/新闻/情绪/资金流 Agent 并行分析 → 看多/看空辩论 → 首席汇总 →
交易员计划 → 风控 → 合规 → 模拟执行或人工确认 → 订单监控 → 日志通知

依赖: research_workflow + trading_workflow
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional

from core.config import get_settings
from core.ids import gen_trace_id
from core.logging import set_trace_id
from memory.audit_log import AuditLogger
from paper_trading.paper_broker import PaperBroker
from workflows.graph import WorkflowState
from workflows.research_workflow import build_research_graph, node_collect_data
from workflows.trading_workflow import run_trading_workflow

logger = logging.getLogger("workflow.intraday")

_broker: Optional[PaperBroker] = None


def get_broker() -> PaperBroker:
    global _broker
    if _broker is None:
        acc_id = get_settings().get("paper_account.account_id", "PA-001")
        _broker = PaperBroker(acc_id)
    return _broker


async def run_intraday_scan(symbol: str, name: str = "", asset_type: str = "etf",
                            force: bool = False) -> Dict[str, Any]:
    """
    单个标的的完整盘中分析 → 交易闭环。
    force=True 时忽略分析频率限制(紧急触发/手动触发)。
    """
    audit = AuditLogger.instance()
    broker = get_broker()
    account_snapshot = broker.get_account()
    position = broker.account.get_position(symbol)

    state = WorkflowState(symbol=symbol)
    state.set("name", name)
    state.set("asset_type", asset_type)
    state.set("position", position)
    state.set("broker", broker)
    state.set("account_snapshot", account_snapshot)
    state.set("system_paused", broker.account.get_snapshot().get("status") != "normal")

    audit.log("intraday_scan_start", "workflow",
              {"symbol": symbol, "name": name, "force": force})

    # 阶段1: 投研(数据闸门→特征→分析师→辩论→首席)
    research_graph = build_research_graph()
    # 先采集数据(复用投研图的采集节点)
    data_res = await node_collect_data(state)
    state = await research_graph.run(state)

    result: Dict[str, Any] = {
        "trace_id": state.trace_id,
        "symbol": symbol,
        "interrupted": state.interrupted,
        "chief": state.get("chief"),
        "plan": state.get("plan"),
        "risk": state.get("risk"),
        "execution": state.get("execution"),
        "analyst_outputs": state.get("analyst_outputs"),
    }

    # 数据闸门 BLOCKED → 不进入交易阶段
    if state.is_interrupted():
        audit.log("intraday_scan_blocked", "workflow",
                  {"symbol": symbol, "reason": state.interrupted})
        result["reason"] = state.interrupted
        return result

    # 阶段2: 交易(仅当首席结论不是 EXCLUDE/HOLD 时进入交易员, 减少无效调用)
    chief = state.get("chief") or {}
    if chief.get("research_decision") in ("BUY_CANDIDATE", "SELL_CANDIDATE"):
        state = await run_trading_workflow(state, account_snapshot, broker)
        result["plan"] = state.get("plan")
        result["risk"] = state.get("risk")
        result["execution"] = state.get("execution")

    audit.log("intraday_scan_end", "workflow",
              {"symbol": symbol, "chief": chief.get("research_decision"),
               "execution": (result.get("execution") or {}).get("status")})
    logger.info("[%s] 盘中扫描完成 %s: 研究=%s 执行=%s",
                state.trace_id, symbol, chief.get("research_decision"),
                (result.get("execution") or {}).get("status"))
    return result


async def run_pool_scan(symbols: List[str], name_map: Optional[Dict[str, str]] = None,
                        max_concurrent: int = 5) -> List[Dict[str, Any]]:
    """标的池扫描: 并发分析多个标的(受 max_concurrent 限制)。"""
    name_map = name_map or {}
    sem = asyncio.Semaphore(max_concurrent)

    async def one(symbol: str) -> Dict[str, Any]:
        async with sem:
            try:
                return await run_intraday_scan(symbol, name_map.get(symbol, ""))
            except Exception as exc:
                logger.error("扫描 %s 异常: %s", symbol, exc)
                return {"symbol": symbol, "interrupted": f"异常: {exc}"}

    return await asyncio.gather(*[one(s) for s in symbols])
