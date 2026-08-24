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
import threading
from typing import Any, Dict, List, Optional

from core.config import get_settings
from core.ids import gen_trace_id
from core.logging import set_trace_id
from memory.audit_log import AuditLogger
from paper_trading.paper_broker import PaperBroker
from workflows.graph import WorkflowState
from workflows.research_workflow import build_research_graph

logger = logging.getLogger("workflow.intraday")

_broker: Optional[PaperBroker] = None
# 节点级进度回调(Web 异步任务设置, 用于前端实时显示)。
# 修复: 原为进程级全局变量 —— 调度器自动扫描与 Web 手动扫描并发时互相覆盖,
# 进度显示错乱; 且 A 扫描结束后 finally 清空回调会让 B 扫描的进度丢失。
# 改为线程本地: 每个扫描线程各自持有自己的回调, 互不影响。
_progress_local = threading.local()


def set_scan_progress_cb(cb):
    """设置当前线程的节点进度回调。"""
    _progress_local.cb = cb


def get_scan_progress_cb():
    """读取当前线程的进度回调(无则 None)。"""
    return getattr(_progress_local, "cb", None)


# 节点中文名映射(前端进度展示)
NODE_LABELS = {
    "collect_data": "数据采集",
    "data_gate": "数据质量闸门",
    "features": "特征计算",
    "summary": "市场摘要",
    "analysts": "7分析师并行分析",
    "bull": "看多研究员",
    "bear": "看空研究员(反驳)",
    "chief": "首席研究员汇总",
    "trader": "交易员计划",
    "risk_manager": "五层风控审核",
    "compliance": "合规审计",
    "execute": "订单执行",
    "supervise": "执行监督",
}


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
    from core.agent_switch import agent_system_enabled
    if not agent_system_enabled():
        reason = "Agent总开关已关闭，未执行分析且未调用LLM"
        logger.info("跳过Agent扫描 %s: %s", symbol, reason)
        return {
            "trace_id": gen_trace_id(), "symbol": symbol,
            "interrupted": reason, "reason": reason, "mode": "AGENT_DISABLED",
            "chief": None, "plan": None, "risk": None,
            "analyst_outputs": None,
            "execution": {"status": "SKIPPED", "reason": reason},
        }
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
              {"symbol": symbol, "name": name, "force": force},
              trace_id=state.trace_id)

    # 阶段1: 投研(数据闸门→特征→分析师→辩论→首席)
    # 注意: 不再手动调用 node_collect_data —— 原实现先手动采集(结果丢弃),
    # 随后 research_graph.run 又采集一次, 每轮行情/盘口/资金流双倍请求(修复)。
    research_graph = build_research_graph()
    research_graph.progress_cb = get_scan_progress_cb()
    state = await research_graph.run(state)
    audit.log("decision_data_snapshot", "market_data", {
        "symbol": symbol, "name": name,
        "data_snapshot": state.get("data_snapshot") or {},
        "quality_reports": state.get("quality_reports") or [],
    }, trace_id=state.trace_id)

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
                  {"symbol": symbol, "reason": state.interrupted,
                   "data_snapshot": state.get("data_snapshot") or {}},
                  trace_id=state.trace_id)
        result["reason"] = state.interrupted
        return result

    # Agent 影子模式：研究结论只留档，不生成交易计划、不触发确认、不提交订单。
    # 正常买卖唯一来自正式轮动策略；硬风控/合规继续在策略与 Broker 层生效。
    chief = state.get("chief") or {}
    result["mode"] = "SHADOW_ONLY"
    result["execution"] = {
        "status": "OBSERVED_ONLY",
        "reason": "Agent处于影子评估模式，不拥有交易权；正式策略独立执行买卖",
    }
    audit.log("agent_shadow_observation", "agent_research", {
        "symbol": symbol, "chief": chief.get("research_decision"),
        "confidence": chief.get("confidence"),
        "data_snapshot": state.get("data_snapshot") or {},
        "execution_policy": "OBSERVE_ONLY",
    }, trace_id=state.trace_id)

    audit.log("intraday_scan_end", "workflow",
              {"symbol": symbol, "chief": chief.get("research_decision"),
               "execution": (result.get("execution") or {}).get("status"),
               "data_snapshot": state.get("data_snapshot") or {}},
              trace_id=state.trace_id)
    logger.info("[%s] 盘中扫描完成 %s: 研究=%s 执行=%s",
                state.trace_id, symbol, chief.get("research_decision"),
                (result.get("execution") or {}).get("status"))
    return result


async def run_pool_scan(symbols: List[str], name_map: Optional[Dict[str, str]] = None,
                        max_concurrent: int = 5,
                        asset_type_map: Optional[Dict[str, str]] = None
                        ) -> List[Dict[str, Any]]:
    """标的池扫描: 并发分析多个标的(受 max_concurrent 限制)。"""
    name_map = name_map or {}
    asset_type_map = asset_type_map or {}
    sem = asyncio.Semaphore(max_concurrent)

    async def one(symbol: str) -> Dict[str, Any]:
        async with sem:
            try:
                return await run_intraday_scan(
                    symbol, name_map.get(symbol, ""),
                    asset_type_map.get(symbol, "etf"))
            except Exception as exc:
                logger.error("扫描 %s 异常: %s", symbol, exc)
                return {"symbol": symbol, "interrupted": f"异常: {exc}"}

    return await asyncio.gather(*[one(s) for s in symbols])
