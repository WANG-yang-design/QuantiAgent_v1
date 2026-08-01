# -*- coding: utf-8 -*-
"""
投研工作流 (多Agent分析 DAG)
=============================
数据闸门 → 特征计算 → 分析师并行(7个) → 看多/看空辩论 → 首席汇总

Agent 调用关系(关键):
  data_admin(闸门, 失败则中断)
    → technical/etf/fundamental/news/sentiment/money_flow/macro 并行
    → bull_researcher(看多) / bear_researcher(看空, 先看多后反驳)
    → chief_researcher(汇总)
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from agents.analyst_agents import (
    EtfAnalystAgent, FundamentalAnalystAgent, MacroAnalystAgent,
    MoneyFlowAnalystAgent, NewsAnalystAgent, SentimentAnalystAgent,
    TechnicalAnalystAgent,
)
from agents.base_agent import AgentInput
from agents.data_admin_agent import DataAdminAgent
from agents.researcher_agents import (
    BearResearcherAgent, BullResearcherAgent, ChiefResearcherAgent,
)
from core.config import get_settings
from core.ids import gen_decision_id
from data_service.market_data_service import get_market_service
from data_service.news_service import get_news_service
from database import repository as repo
from features.market_state import build_market_summary
from features.technical_indicators import (
    compute_etf_features, compute_money_flow_features, compute_technical_features,
)
from strategies.etf_momentum_rotation import EtfMomentumRotationStrategy
from workflows.graph import WorkflowGraph, WorkflowState

logger = logging.getLogger("workflow.research")

# 并行分析组(7个分析师)
_ANALYSTS = {
    "technical": TechnicalAnalystAgent(),
    "etf": EtfAnalystAgent(),
    "fundamental": FundamentalAnalystAgent(),
    "news": NewsAnalystAgent(),
    "sentiment": SentimentAnalystAgent(),
    "money_flow": MoneyFlowAnalystAgent(),
    "macro": MacroAnalystAgent(),
}
_ADMIN = DataAdminAgent()
_BULL = BullResearcherAgent()
_BEAR = BearResearcherAgent()
_CHIEF = ChiefResearcherAgent()


# ================================================================
# 节点实现
# ================================================================

# 新闻/公告采集缓存(symbol -> 最近采集时间, 30分钟TTL防限流)
_news_fetch_cache: Dict[str, datetime] = {}


async def node_collect_data(state: WorkflowState) -> Dict[str, Any]:
    """采集数据: 日K/实时行情/盘口/新闻/舆情/资金流(新闻公告主动拉取入库)。"""
    symbol = state.symbol
    svc = get_market_service()
    news_svc = get_news_service()
    asset_type = state.get("asset_type", "etf")

    bars, rep = svc.get_daily_bars(symbol, asset_type=asset_type)
    quote, qrep = svc.get_realtime_quote(symbol, asset_type)
    ob, obrep = svc.get_order_book(symbol, asset_type)
    money_flow = svc.get_money_flow(symbol, asset_type) or {}

    # 主动采集新闻/公告(带30分钟缓存, 避免每轮重复拉取触发限流)
    last_fetch = _news_fetch_cache.get(symbol, datetime.min)
    if (datetime.now() - last_fetch).total_seconds() > 1800:
        try:
            news_svc.fetch_and_store_news([symbol])
            news_svc.fetch_and_store_announcements([symbol])
            _news_fetch_cache[symbol] = datetime.now()
        except Exception as exc:
            logger.warning("新闻公告采集失败 %s: %s", symbol, exc)

    # 新闻/舆情(读库取最近)
    news_list = news_svc.get_recent_news(symbol, hours=48)
    ann_list = news_svc.get_recent_announcements(symbol, days=7)
    risk_ann = [a for a in ann_list if a.risk_level in ("high", "medium")]
    sentiment = news_svc.get_sentiment_stats(symbol, hours=24)

    quality_reports = [rep.to_dict(), qrep.to_dict(), obrep.to_dict()]
    news_ctx = {
        "count": len(news_list),
        "risk_announcements": len(risk_ann),
        "avg_sentiment": round(sum(n.sentiment_score for n in news_list) / len(news_list), 4)
        if news_list else 0.0,
        "raw": "\n".join(f"[{n.publish_time:%m-%d %H:%M}] {n.title}" for n in news_list[:15]),
    }

    return {
        "bars": bars, "quote": quote, "order_book": ob,
        "money_flow_raw": money_flow, "news": news_ctx,
        "sentiment_stats": sentiment,
        "quality_reports": quality_reports,
        "etf_info": svc.get_etf_info(symbol),
        "fundamental": svc.get_fundamentals(symbol) if asset_type == "stock" else {},
    }


async def node_data_gate(state: WorkflowState) -> Dict[str, Any]:
    """数据管理员闸门: BLOCKED 则中断工作流。"""
    result = await _ADMIN.run(AgentInput(
        symbol=state.symbol,
        context={
            "quality_reports": state.get("quality_reports"),
            "system_paused": state.get("system_paused", False),
        }))
    state.record("data_admin", f"数据状态: {result.get('data_status')}", result)
    if result.get("data_status") == "BLOCKED":
        state.interrupt(f"数据闸门BLOCKED: {result.get('blocked_reason')}")
    return {"data_admin": result}


async def node_features(state: WorkflowState) -> Dict[str, Any]:
    """特征计算: 技术指标/ETF特征/资金流特征 + 市场环境。"""
    symbol = state.symbol
    bars = state.get("bars") or []
    quote = state.get("quote") or {}
    etf_info = state.get("etf_info") or {}
    money_flow = state.get("money_flow_raw") or {}

    tech = compute_technical_features(bars)
    etf = compute_etf_features(bars, quote, etf_info)
    mf = compute_money_flow_features(money_flow)

    # 市场环境: 沪深300 动量
    env: Dict[str, Any] = {}
    try:
        end = datetime.now().date()
        idx = get_market_service().get_index_bars("000300", end - timedelta(days=120), end)
        if len(idx) >= 21:
            env = {
                "index_momentum_20d": idx[-1]["close"] / idx[-21]["close"] - 1,
                "index_close": idx[-1]["close"],
                "state_desc": "risk_on" if idx[-1]["close"] > idx[-21]["close"] else "risk_off",
            }
    except Exception as exc:
        logger.warning("市场环境计算失败: %s", exc)

    # 策略参考信号(动量轮动)
    strategy_signal = None
    try:
        strat = EtfMomentumRotationStrategy()
        signals = strat.generate_signals([{
            "symbol": symbol, "name": state.get("name", ""), "features": tech}])
        if signals:
            strategy_signal = signals[0]
    except Exception as exc:
        logger.warning("策略信号失败: %s", exc)

    return {"technical": tech, "etf": etf, "money_flow": mf,
            "market_env": env, "strategy_signal": strategy_signal}


async def node_build_summary(state: WorkflowState) -> Dict[str, Any]:
    """构建市场状态摘要(喂给 Agent 的紧凑文本)。"""
    summary = build_market_summary(
        symbol=state.symbol,
        name=state.get("name", ""),
        tech=state.get("technical") or {},
        etf=state.get("etf") or {},
        quote=state.get("quote") or {},
        money_flow=state.get("money_flow") or {},
        news_summary=state.get("news") or {},
        sentiment=state.get("sentiment_stats") or {},
        market_env=state.get("market_env") or {},
        position=state.get("position"),
        strategy_signal=state.get("strategy_signal"),
        order_book=state.get("order_book") or {},
    )
    return {"summary": summary}


async def node_analysts(state: WorkflowState) -> Dict[str, Any]:
    """7个分析师并行分析。"""
    symbol = state.symbol
    ctx = {
        "summary": state.get("summary"),
        "technical": state.get("technical"),
        "etf": state.get("etf"),
        "fundamental": state.get("fundamental"),
        "news": state.get("news"),
        "sentiment": state.get("sentiment_stats"),
        "money_flow": state.get("money_flow"),
        "market_env": state.get("market_env"),
        "order_book": state.get("order_book"),
    }
    tasks = []
    for name, agent in _ANALYSTS.items():
        tasks.append((name, agent.run(AgentInput(symbol=symbol, context=ctx))))
    results = {}
    for name, task in tasks:
        try:
            results[name] = await task
        except Exception as exc:
            logger.error("分析师 %s 失败: %s", name, exc)
            results[name] = {"agent": name, "view": "neutral", "score": 50.0,
                             "confidence": 0.0, "key_points": [f"分析失败: {exc}"], "risks": []}
    state.record("analysts", f"7个分析师完成: " +
                 ", ".join(f"{k}={v.get('view')}" for k, v in results.items()), results)
    return {"analyst_outputs": results}


async def node_bull(state: WorkflowState) -> Dict[str, Any]:
    """看多研究员。"""
    result = await _BULL.run(AgentInput(symbol=state.symbol, context={
        "analyst_outputs": state.get("analyst_outputs"),
        "bear_rebuttal": state.get("bear"),
    }))
    return {"bull": result}


async def node_bear(state: WorkflowState) -> Dict[str, Any]:
    """看空研究员(先看到多理由, 再反驳)。"""
    result = await _BEAR.run(AgentInput(symbol=state.symbol, context={
        "analyst_outputs": state.get("analyst_outputs"),
    }))
    return {"bear": result}


async def node_chief(state: WorkflowState) -> Dict[str, Any]:
    """首席研究员汇总(生成 decision_id 并落库, 供审计追溯)。"""
    result = await _CHIEF.run(AgentInput(symbol=state.symbol, context={
        "bull": state.get("bull"), "bear": state.get("bear"),
    }))
    result["decision_id"] = gen_decision_id()
    # 落库投研结论(审计: 谁给的观点/结论是什么)
    repo.save_research_decision({
        "decision_id": result["decision_id"],
        "symbol": state.symbol,
        "decision": result.get("research_decision", "HOLD"),
        "confidence": result.get("confidence", 0),
        "bull_summary": result.get("bull_summary", ""),
        "bear_summary": result.get("bear_summary", ""),
        "reasoning": {
            "bull": state.get("bull"),
            "bear": state.get("bear"),
            "analyst_outputs": {k: {kk: vv for kk, vv in v.items()
                                    if kk != "raw"} if isinstance(v, dict) else v
                                for k, v in (state.get("analyst_outputs") or {}).items()},
        },
    })
    state.record("chief_researcher", f"研究结论: {result.get('research_decision')}", result)
    return {"chief": result}


# ================================================================
# 图构建
# ================================================================
def build_research_graph() -> WorkflowGraph:
    g = WorkflowGraph("research")
    g.add_node("collect_data", node_collect_data)
    g.add_node("data_gate", node_data_gate)
    g.add_node("features", node_features)
    g.add_node("summary", node_build_summary)
    g.add_node("analysts", node_analysts)
    g.add_node("bull", node_bull)
    g.add_node("bear", node_bear)
    g.add_node("chief", node_chief)
    # 边: 固定 DAG
    g.add_edge("collect_data", "data_gate")
    g.add_edge("data_gate", "features")
    g.add_edge("features", "summary")
    g.add_edge("summary", "analysts")
    g.add_edge("analysts", "bull")
    g.add_edge("analysts", "bear")
    g.add_edge("bull", "chief")
    g.add_edge("bear", "chief")
    return g


async def run_research(symbol: str, name: str = "", asset_type: str = "etf",
                       position: Optional[Dict[str, Any]] = None,
                       system_paused: bool = False) -> WorkflowState:
    """运行投研工作流(盘中/回测复用)。"""
    state = WorkflowState(symbol=symbol)
    state.set("name", name)
    state.set("asset_type", asset_type)
    state.set("position", position)
    state.set("system_paused", system_paused)
    graph = build_research_graph()
    return await graph.run(state)
