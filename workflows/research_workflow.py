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
from data_service.data_quality import DataQualityReport
from data_service.market_data_service import get_market_service
from data_service.news_service import get_news_service
from database import repository as repo
from features.market_state import build_market_summary
from features.technical_indicators import (
    compute_etf_features, compute_intraday_features, compute_money_flow_features,
    compute_technical_features,
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
# 短键 → Agent 全名(agent_switch 开关按全名配置, 修复: 原实现用短键
# 查开关全部返回 False, 7个分析师被整体跳过, 决策链路只剩4个节点)
_ANALYST_FULL_NAMES = {
    "technical": "technical_analyst",
    "etf": "etf_analyst",
    "fundamental": "fundamental_analyst",
    "news": "news_analyst",
    "sentiment": "sentiment_analyst",
    "money_flow": "money_flow_analyst",
    "macro": "macro_analyst",
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

# 市场环境指数缓存(修复: 每次投研都重新拉000300日K, 回测/批量扫描时
# 数据源登录失败要等120秒, 单个标的分析被拖慢数分钟)
_index_env_cache: Dict[str, Any] = {"ts": 0.0, "bars": []}


def _market_env_bars(end: datetime, days: int = 120) -> List[dict]:
    import time as _t
    now = _t.time()
    if now - _index_env_cache.get("ts", 0.0) < 600:
        return _index_env_cache.get("bars", [])
    try:
        idx = get_market_service().get_index_bars(
            "000300", end - timedelta(days=days), end)
        _index_env_cache.update({"ts": now, "bars": idx})
        return idx
    except Exception as exc:
        logger.warning("市场环境指数获取失败: %s", exc)
        return _index_env_cache.get("bars", [])


async def node_collect_data(state: WorkflowState) -> Dict[str, Any]:
    """采集数据: 日K/实时行情/盘口/新闻/舆情/资金流(新闻公告主动拉取入库)。
    回测模式(asof_override): 使用回测日数据切片, 不采集实时行情与最新新闻,
    杜绝 look-ahead bias。"""
    symbol = state.symbol
    svc = get_market_service()
    news_svc = get_news_service()
    asset_type = state.get("asset_type", "etf")
    override = state.get("asof_override")

    if override:
        # ---- 回测模式: 使用注入的数据切片 ----
        bars = override.get("bars") or []
        quote = override.get("quote") or {}
        ob = override.get("order_book") or {}
        money_flow = override.get("money_flow") or {}
        rep = DataQualityReport(symbol, "daily_bar")
        qrep = DataQualityReport(symbol, "realtime_quote")
        obrep = DataQualityReport(symbol, "order_book")
        # 回测日没有对应的历史新闻/舆情(实时新闻属于未来数据, 不可用于回测)
        news_ctx = {"count": 0, "risk_announcements": 0,
                    "avg_sentiment": 0.0, "raw": "[回测模式] 新闻舆情不参与历史决策"}
        sentiment = {}
        risk_ann = []
        etf_info = {}
        # 修复: 原实现回测分支未定义 minute_bars, 引擎Agent模式回测
        # 每次投研都在此 UnboundLocalError 崩溃, 只能回退纯策略
        minute_bars = []
    else:
        # 并行采集各数据类别(修复: 原实现串行调用5个上游接口, 东财/baostock
        # 网络往返逐次等待, 单节点耗时 10-30s 是整条链路最慢节点)
        import asyncio
        async def _gather():
            async def _run(fn):
                try:
                    return await asyncio.to_thread(fn)
                except Exception as exc:
                    logger.warning("并行采集失败(%s): %s", symbol, exc)
                    return None
            now = datetime.now()
            bars_res = _run(lambda: svc.get_daily_bars(symbol, asset_type=asset_type))
            quote_res = _run(lambda: svc.get_realtime_quote(symbol, asset_type))
            ob_res = _run(lambda: svc.get_order_book(symbol, asset_type))
            mf_res = _run(lambda: svc.get_money_flow(symbol, asset_type))
            etf_res = _run(lambda: svc.get_etf_info(symbol))
            # 分时(当日1分钟): 修复: 原实现只喂日K特征, AI 看不到盘中走势。
            # 腾讯今日分时1次请求即得, 盘中/盘后都可取; 失败不影响主流程。
            minute_res = _run(lambda: svc.get_minute_bars(
                symbol, datetime.combine(now.date(), datetime.min.time()),
                now, "1m", asset_type))
            return await asyncio.gather(bars_res, quote_res, ob_res, mf_res,
                                        etf_res, minute_res)

        (bars, rep), (quote, qrep), (ob, obrep), mf_tuple, etf_info, minute_res = await _gather()
        bars = bars or []
        quote = quote or {}
        ob = ob or {}
        money_flow = mf_tuple or {}
        etf_info = etf_info or {}
        minute_bars = (minute_res or ([], None))[0] if isinstance(minute_res, tuple) else (minute_res or [])
        rep = rep or DataQualityReport(symbol, "daily_bar")
        qrep = qrep or DataQualityReport(symbol, "realtime_quote")
        obrep = obrep or DataQualityReport(symbol, "order_book")

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

        news_ctx = {
            "count": len(news_list),
            "risk_announcements": len(risk_ann),
            "avg_sentiment": round(sum(n.sentiment_score for n in news_list) / len(news_list), 4)
            if news_list else 0.0,
            "raw": "\n".join(f"[{n.publish_time:%m-%d %H:%M}] {n.title}" for n in news_list[:15]),
        }

    quality_reports = [rep.to_dict(), qrep.to_dict(), obrep.to_dict()]

    return {
        "bars": bars, "quote": quote, "order_book": ob,
        "money_flow_raw": money_flow, "news": news_ctx,
        "sentiment_stats": sentiment,
        "quality_reports": quality_reports,
        "etf_info": etf_info,
        "minute_bars": minute_bars,
        "fundamental": (svc.get_fundamentals(symbol)
                        if asset_type == "stock" and not override else {}),
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
    """特征计算: 技术指标/ETF特征/资金流特征 + 市场环境 + 当日分时特征。"""
    symbol = state.symbol
    bars = state.get("bars") or []
    quote = state.get("quote") or {}
    etf_info = state.get("etf_info") or {}
    money_flow = state.get("money_flow_raw") or {}
    minute_bars = state.get("minute_bars") or []

    tech = compute_technical_features(bars)
    etf = compute_etf_features(bars, quote, etf_info)
    mf = compute_money_flow_features(money_flow)
    # 分时特征(修复: 原来只喂日K, AI 看不到盘中走势/均价/尾盘强弱)
    intraday = compute_intraday_features(
        minute_bars, prev_close=quote.get("prev_close"),
        latest=quote.get("latest_price"))

    # 市场环境: 沪深300 动量(进程内缓存, 修复: 回测/批量扫描重复拉取)
    env: Dict[str, Any] = {}
    try:
        end = datetime.now().date()
        idx = _market_env_bars(end)
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
            "market_env": env, "strategy_signal": strategy_signal,
            "intraday": intraday}


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
        intraday=state.get("intraday") or {},
    )
    return {"summary": summary}


async def node_analysts(state: WorkflowState) -> Dict[str, Any]:
    """7个分析师并行分析(受 agents.enabled 开关控制, 修复: 成本控制)。
    关闭的分析师不调 LLM, 用规则化 mock 输出占位 —— 决策链路保持完整。
    必须启用: data_admin/chief_researcher(开关在 core/agent_switch.py)。"""
    from core.agent_switch import agent_enabled
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
        "intraday": state.get("intraday"),
    }
    # 修复: 用全名过滤开关(短键只用于执行)
    names = [k for k in _ANALYSTS
             if agent_enabled(_ANALYST_FULL_NAMES.get(k, k))]
    if not names:
        # 全部分析师被关: 注入中性占位, 保持 DAG 可跑
        return {"analyst_outputs": {n: {"agent": n, "view": "neutral",
                                        "score": 50.0, "confidence": 0.3,
                                        "key_points": ["分析师已按配置关闭"], "risks": []}
                                    for n in _ANALYSTS}}
    outputs = await asyncio.gather(
        *[_ANALYSTS[n].run(AgentInput(symbol=symbol, context=ctx)) for n in names],
        return_exceptions=True)
    results = {}
    for name, out in zip(names, outputs):
        if isinstance(out, Exception):
            logger.error("分析师 %s 失败: %s", name, out)
            results[name] = {"agent": name, "view": "neutral", "score": 50.0,
                             "confidence": 0.0, "key_points": [f"分析失败: {out}"],
                             "risks": []}
        else:
            results[name] = out
    # 被关闭的分析师: 用规则化 mock 占位(无 LLM 成本)
    for n in _ANALYSTS:
        if n not in results:
            try:
                results[n] = _ANALYSTS[n].mock_output(
                    AgentInput(symbol=symbol, context=ctx))
            except Exception:
                results[n] = {"agent": n, "view": "neutral", "score": 50.0,
                              "confidence": 0.2, "key_points": ["按配置关闭(规则占位)"],
                              "risks": []}
    state.record("analysts", f"分析师完成(并行): " +
                 ", ".join(f"{k}={v.get('view')}" for k, v in results.items()), results)
    return {"analyst_outputs": results}


async def node_bull(state: WorkflowState) -> Dict[str, Any]:
    """看多研究员(受 agents.enabled 开关控制, 修复: 成本控制)。"""
    from core.agent_switch import agent_enabled
    if not agent_enabled("bull_researcher"):
        result = _BULL.mock_output(AgentInput(symbol=state.symbol, context={
            "analyst_outputs": state.get("analyst_outputs")}))
        return {"bull": result}
    result = await _BULL.run(AgentInput(symbol=state.symbol, context={
        "analyst_outputs": state.get("analyst_outputs"),
        "bear_rebuttal": state.get("bear"),
        # 修复: 原实现研究员看不到持仓/账户 —— 辩论脱离账户实际,
        # 首席据此给出"无持仓却 SELL"的结论。持仓必须参与观点形成。
        "summary": state.get("summary"),
        "position": state.get("position"),
        "account": state.get("account_snapshot"),
    }))
    return {"bull": result}


async def node_bear(state: WorkflowState) -> Dict[str, Any]:
    """看空研究员(受 agents.enabled 开关控制, 修复: 成本控制)。"""
    from core.agent_switch import agent_enabled
    if not agent_enabled("bear_researcher"):
        result = _BEAR.mock_output(AgentInput(symbol=state.symbol, context={
            "analyst_outputs": state.get("analyst_outputs"),
            "bull": state.get("bull")}))
        return {"bear": result}
    result = await _BEAR.run(AgentInput(symbol=state.symbol, context={
        "analyst_outputs": state.get("analyst_outputs"),
        "bull": state.get("bull"),      # 修复: 原实现 bear_rebuttal 恒空, 辩论退化为独立分析
        "summary": state.get("summary"),
        "position": state.get("position"),
        "account": state.get("account_snapshot"),
    }))
    return {"bear": result}


async def node_chief(state: WorkflowState) -> Dict[str, Any]:
    """首席研究员汇总(生成 decision_id 并落库, 供审计追溯)。
    修复: 原实现只喂 bull/bear 两个观点 —— 首席完全不知道账户持仓,
    会对没有持仓的标的下 SELL_CANDIDATE。现在把持仓/账户/市场摘要
    一并交给首席, 结论必须与持仓一致。"""
    result = await _CHIEF.run(AgentInput(symbol=state.symbol, context={
        "bull": state.get("bull"), "bear": state.get("bear"),
        "summary": state.get("summary"),
        "position": state.get("position"),
        "account": state.get("account_snapshot"),
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
    g.add_edge("bull", "bear")       # 看空先看完多理由再反驳(修复: 原 bull/bear 并行, 无辩论)
    g.add_edge("bear", "chief")
    return g


async def run_research(symbol: str, name: str = "", asset_type: str = "etf",
                       position: Optional[Dict[str, Any]] = None,
                       system_paused: bool = False,
                       asof_override: Optional[Dict[str, Any]] = None) -> WorkflowState:
    """运行投研工作流(盘中/回测复用)。
    asof_override: 回测模式注入的历史数据切片 {bars, quote, order_book, money_flow},
    此时不采集实时行情/新闻(避免未来数据)。"""
    state = WorkflowState(symbol=symbol)
    state.set("name", name)
    state.set("asset_type", asset_type)
    state.set("position", position)
    state.set("system_paused", system_paused)
    if asof_override:
        state.set("asof_override", asof_override)
    graph = build_research_graph()
    return await graph.run(state)
