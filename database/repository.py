# -*- coding: utf-8 -*-
"""
仓库层: 所有数据库读写操作封装
===============================
供数据服务/Agent/风控/模拟盘等上层模块调用, 统一会话管理。
"""
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from core.ids import gen_id
from database.db_session import get_session
from database.models import (
    Account, AccountSnapshot, AgentOutput, AgentRun, AnnouncementRecord,
    AuditLog, BacktestResult, BacktestRun, DailyBar, EtfInfo, EtfNavRecord,
    FeatureRecord, FundamentalRecord, HumanConfirmation, MemoryRecord,
    MinuteBar, MoneyFlowRecord, NewsRecord, Order, OrderBookSnapshot,
    Position, PromptVersion, RagChunk, RagDocument, RealtimeQuote,
    ReportRecord, ResearchDecision, RiskCheck, SentimentRecord, StrategySignal,
    Symbol, SystemLog, ToolPermission, Trade, TradePlan,
)


# ================================================================
# 标的
# ================================================================

def upsert_symbols(symbols: List[Dict[str, Any]]):
    """批量写入/更新标的信息。"""
    with get_session() as s:
        for item in symbols:
            sym = s.get(Symbol, item["symbol"])
            if sym is None:
                s.add(Symbol(**item))
            else:
                for k, v in item.items():
                    setattr(sym, k, v)


def get_universe(asset_type: Optional[str] = None) -> List[Symbol]:
    with get_session() as s:
        q = s.query(Symbol).filter(Symbol.status == "active")
        if asset_type:
            q = q.filter(Symbol.asset_type == asset_type)
        return list(q.all())


# ================================================================
# 行情
# ================================================================

def upsert_daily_bars(bars: List[Dict[str, Any]]):
    """日K批量写入(去重: symbol+date+source)。"""
    if not bars:
        return
    with get_session() as s:
        for b in bars:
            exist = s.query(DailyBar).filter_by(
                symbol=b["symbol"], trade_date=b["trade_date"],
                source=b.get("source", "akshare")).first()
            if exist:
                for k, v in b.items():
                    setattr(exist, k, v)
            else:
                s.add(DailyBar(**b))


def get_daily_bars(symbol: str, start: date, end: date,
                   quality: Optional[str] = "VALID") -> List[DailyBar]:
    with get_session() as s:
        q = s.query(DailyBar).filter(
            DailyBar.symbol == symbol,
            DailyBar.trade_date >= start,
            DailyBar.trade_date <= end,
        )
        if quality:
            q = q.filter(DailyBar.quality_status == quality)
        return list(q.order_by(DailyBar.trade_date).all())


def upsert_minute_bars(bars: List[Dict[str, Any]]):
    if not bars:
        return
    with get_session() as s:
        for b in bars:
            exist = s.query(MinuteBar).filter_by(
                symbol=b["symbol"], bar_time=b["bar_time"], freq=b.get("freq", "5m"),
                source=b.get("source", "akshare")).first()
            if exist:
                for k, v in b.items():
                    setattr(exist, k, v)
            else:
                s.add(MinuteBar(**b))


def get_minute_bars(symbol: str, start: datetime, end: datetime,
                    freq: str = "5m", limit: Optional[int] = None) -> List[MinuteBar]:
    with get_session() as s:
        q = s.query(MinuteBar).filter(
            MinuteBar.symbol == symbol,
            MinuteBar.bar_time >= start,
            MinuteBar.bar_time <= end,
            MinuteBar.freq == freq,
        ).order_by(MinuteBar.bar_time)
        if limit:
            q = q.limit(limit)
        return list(q.all())


def save_realtime_quote(q: Dict[str, Any]):
    with get_session() as s:
        s.add(RealtimeQuote(**q))


def get_latest_quote(symbol: str) -> Optional[RealtimeQuote]:
    with get_session() as s:
        return s.query(RealtimeQuote).filter(
            RealtimeQuote.symbol == symbol).order_by(
            RealtimeQuote.quote_time.desc()).first()


def save_order_book(ob: Dict[str, Any]):
    with get_session() as s:
        s.add(OrderBookSnapshot(**ob))


def get_latest_order_book(symbol: str) -> Optional[OrderBookSnapshot]:
    with get_session() as s:
        return s.query(OrderBookSnapshot).filter(
            OrderBookSnapshot.symbol == symbol).order_by(
            OrderBookSnapshot.snapshot_time.desc()).first()


# ================================================================
# 资金流 / 新闻 / 公告 / 舆情 / 基本面 / ETF
# ================================================================

def save_money_flow(mf: Dict[str, Any]):
    with get_session() as s:
        s.add(MoneyFlowRecord(**mf))


def get_money_flow(symbol: str, start: datetime, end: datetime) -> List[MoneyFlowRecord]:
    with get_session() as s:
        return list(s.query(MoneyFlowRecord).filter(
            MoneyFlowRecord.symbol == symbol,
            MoneyFlowRecord.record_time >= start,
            MoneyFlowRecord.record_time <= end,
        ).order_by(MoneyFlowRecord.record_time).all())


def upsert_news(news_list: List[Dict[str, Any]]):
    with get_session() as s:
        for n in news_list:
            if not n.get("news_id"):
                n["news_id"] = gen_id("NEWS")
            exist = s.query(NewsRecord).filter_by(news_id=n["news_id"]).first()
            if exist is None:
                s.add(NewsRecord(**n))


def get_news(symbol: Optional[str] = None, start: Optional[datetime] = None,
             end: Optional[datetime] = None, limit: int = 50) -> List[NewsRecord]:
    with get_session() as s:
        q = s.query(NewsRecord)
        if symbol:
            q = q.filter(NewsRecord.symbol == symbol)
        if start:
            q = q.filter(NewsRecord.publish_time >= start)
        if end:
            q = q.filter(NewsRecord.publish_time <= end)
        return list(q.order_by(NewsRecord.publish_time.desc()).limit(limit).all())


def upsert_announcements(ann_list: List[Dict[str, Any]]):
    with get_session() as s:
        for a in ann_list:
            exist = s.query(AnnouncementRecord).filter_by(
                announcement_id=a["announcement_id"]).first()
            if exist is None:
                s.add(AnnouncementRecord(**a))


def get_announcements(symbol: Optional[str] = None, start: Optional[datetime] = None,
                      end: Optional[datetime] = None, limit: int = 50) -> List[AnnouncementRecord]:
    with get_session() as s:
        q = s.query(AnnouncementRecord)
        if symbol:
            q = q.filter(AnnouncementRecord.symbol == symbol)
        if start:
            q = q.filter(AnnouncementRecord.publish_time >= start)
        if end:
            q = q.filter(AnnouncementRecord.publish_time <= end)
        return list(q.order_by(AnnouncementRecord.publish_time.desc()).limit(limit).all())


def save_sentiment(rec: Dict[str, Any]):
    with get_session() as s:
        s.add(SentimentRecord(**rec))


def get_sentiment(symbol: str, start: datetime, end: datetime, limit: int = 200) -> List[SentimentRecord]:
    with get_session() as s:
        return list(s.query(SentimentRecord).filter(
            SentimentRecord.symbol == symbol,
            SentimentRecord.publish_time >= start,
            SentimentRecord.publish_time <= end,
        ).order_by(SentimentRecord.publish_time.desc()).limit(limit).all())


def upsert_fundamentals(items: List[Dict[str, Any]]):
    with get_session() as s:
        for it in items:
            exist = s.query(FundamentalRecord).filter_by(
                symbol=it["symbol"], report_date=it["report_date"]).first()
            if exist:
                for k, v in it.items():
                    setattr(exist, k, v)
            else:
                s.add(FundamentalRecord(**it))


def get_fundamentals(symbol: str) -> Optional[FundamentalRecord]:
    with get_session() as s:
        return s.query(FundamentalRecord).filter(
            FundamentalRecord.symbol == symbol).order_by(
            FundamentalRecord.report_date.desc()).first()


def upsert_etf_info(items: List[Dict[str, Any]]):
    with get_session() as s:
        for it in items:
            exist = s.get(EtfInfo, it["symbol"])
            if exist:
                for k, v in it.items():
                    setattr(exist, k, v)
            else:
                s.add(EtfInfo(**it))


def get_etf_info(symbol: str) -> Optional[EtfInfo]:
    with get_session() as s:
        return s.get(EtfInfo, symbol)


def save_etf_nav(nav: Dict[str, Any]):
    with get_session() as s:
        s.add(EtfNavRecord(**nav))


# ================================================================
# 特征 / 策略信号
# ================================================================

def save_features(features: List[Dict[str, Any]]):
    with get_session() as s:
        for f in features:
            exist = s.query(FeatureRecord).filter_by(
                symbol=f["symbol"], feature_time=f["feature_time"],
                feature_name=f["feature_name"], timeframe=f.get("timeframe", "1d")).first()
            if exist:
                exist.feature_value = f["feature_value"]
            else:
                s.add(FeatureRecord(**f))


def save_strategy_signal(sig: Dict[str, Any]):
    with get_session() as s:
        if not sig.get("signal_id"):
            sig["signal_id"] = gen_id("SIG")
        s.add(StrategySignal(**sig))


def get_strategy_signals(strategy_id: str, start: datetime, end: datetime) -> List[StrategySignal]:
    with get_session() as s:
        return list(s.query(StrategySignal).filter(
            StrategySignal.strategy_id == strategy_id,
            StrategySignal.signal_time >= start,
            StrategySignal.signal_time <= end,
        ).order_by(StrategySignal.signal_time).all())


# ================================================================
# Agent 运行记录 / 输出
# ================================================================

def start_agent_run(agent_name: str, symbol: str, trace_id: str,
                    model_name: str = "") -> AgentRun:
    run = AgentRun(run_id=gen_id("RUN"), agent_name=agent_name,
                   symbol=symbol, trace_id=trace_id, model_name=model_name)
    with get_session() as s:
        s.add(run)
        s.flush()
        s.expunge(run)
    return run


def finish_agent_run(run_id: str, status: str, error: str = ""):
    with get_session() as s:
        run = s.query(AgentRun).filter_by(run_id=run_id).first()
        if run:
            run.end_time = datetime.now()
            run.status = status
            run.error = error


def save_agent_output(run_id: str, agent_name: str, view: str, score: float,
                      confidence: float, output_json: dict):
    with get_session() as s:
        s.add(AgentOutput(
            output_id=gen_id("OUT"), run_id=run_id, agent_name=agent_name,
            view=view, score=score, confidence=confidence,
            output_json=output_json))


# ================================================================
# 投研 / 计划 / 风控
# ================================================================

def save_research_decision(d: Dict[str, Any]):
    with get_session() as s:
        if not d.get("decision_id"):
            d["decision_id"] = gen_id("DEC")
        s.add(ResearchDecision(**d))


def save_trade_plan(p: Dict[str, Any]):
    with get_session() as s:
        if not p.get("plan_id"):
            p["plan_id"] = gen_id("PLAN")
        s.add(TradePlan(**p))


def update_plan_status(plan_id: str, status: str):
    with get_session() as s:
        plan = s.query(TradePlan).filter_by(plan_id=plan_id).first()
        if plan:
            plan.status = status


def save_risk_check(r: Dict[str, Any]):
    with get_session() as s:
        if not r.get("risk_check_id"):
            r["risk_check_id"] = gen_id("RISK")
        s.add(RiskCheck(**r))


def save_human_confirmation(c: Dict[str, Any]):
    with get_session() as s:
        if not c.get("confirm_id"):
            c["confirm_id"] = gen_id("CFM")
        s.add(HumanConfirmation(**c))


def list_pending_confirmations() -> List[HumanConfirmation]:
    with get_session() as s:
        return list(s.query(HumanConfirmation).filter_by(status="PENDING")
                    .order_by(HumanConfirmation.created_at).all())


def decide_confirmation(confirm_id: str, approved: bool, by: str = "web"):
    with get_session() as s:
        c = s.query(HumanConfirmation).filter_by(confirm_id=confirm_id).first()
        if c:
            c.status = "APPROVED" if approved else "REJECTED"
            c.decided_at = datetime.now()
            c.decided_by = by


# ================================================================
# 账户 / 持仓 / 订单 / 成交
# ================================================================

def get_account(account_id: str = "PA-001") -> Optional[Account]:
    with get_session() as s:
        acc = s.get(Account, account_id)
        s.expunge(acc) if acc else None
        return acc


def save_account(acc: Account):
    with get_session() as s:
        merged = s.merge(acc)
        s.commit()
        s.expunge(merged)
    return acc


def get_positions(account_id: str = "PA-001") -> List[Position]:
    with get_session() as s:
        rows = list(s.query(Position).filter_by(account_id=account_id).all())
        for r in rows:
            s.expunge(r)
        return rows


def get_position(account_id: str, symbol: str) -> Optional[Position]:
    with get_session() as s:
        p = s.query(Position).filter_by(account_id=account_id, symbol=symbol).first()
        if p:
            s.expunge(p)
        return p


def save_position(p: Position):
    with get_session() as s:
        s.merge(p)
        s.commit()


def delete_position(account_id: str, symbol: str):
    """清仓后删除持仓记录。"""
    with get_session() as s:
        s.query(Position).filter_by(account_id=account_id, symbol=symbol).delete()
        s.commit()


def save_order(o: Dict[str, Any]) -> Order:
    with get_session() as s:
        if not o.get("order_id"):
            o["order_id"] = gen_id("ORD")
        if not o.get("order_intent_id"):
            o["order_intent_id"] = gen_id("INTENT")
        order = Order(**o)
        s.add(order)
        s.flush()
        s.expunge(order)
    return order


def get_order(order_id: str) -> Optional[Order]:
    with get_session() as s:
        o = s.query(Order).filter_by(order_id=order_id).first()
        if o:
            s.expunge(o)
        return o


def get_order_by_intent(intent_id: str) -> Optional[Order]:
    """幂等查询: 同一 order_intent_id 只能有一个订单。"""
    with get_session() as s:
        o = s.query(Order).filter_by(order_intent_id=intent_id).first()
        if o:
            s.expunge(o)
        return o


def update_order(order: Order):
    with get_session() as s:
        s.merge(order)
        s.commit()


def get_open_orders(account_id: str = "PA-001") -> List[Order]:
    with get_session() as s:
        return list(s.query(Order).filter(
            Order.account_id == account_id,
            Order.status.in_(["SUBMITTED", "ACCEPTED", "PARTIALLY_FILLED"])).all())


def get_orders_today(today: date, account_id: str = "PA-001") -> List[Order]:
    """当日全部订单(合规审计用: 每日次数/金额限额)。"""
    start = datetime.combine(today, datetime.min.time())
    with get_session() as s:
        return list(s.query(Order).filter(
            Order.account_id == account_id,
            Order.submit_time >= start).all())


def get_orders_recent(limit: int = 50, account_id: str = "PA-001") -> List[Order]:
    """最近订单(按提交时间倒序)。"""
    with get_session() as s:
        return list(s.query(Order).filter(Order.account_id == account_id)
                    .order_by(Order.submit_time.desc()).limit(limit).all())


def save_trade(t: Dict[str, Any]):
    with get_session() as s:
        if not t.get("trade_id"):
            t["trade_id"] = gen_id("TRADE")
        s.add(Trade(**t))


def get_trades(symbol: Optional[str] = None, start: Optional[datetime] = None,
               end: Optional[datetime] = None, limit: int = 500) -> List[Trade]:
    with get_session() as s:
        q = s.query(Trade)
        if symbol:
            q = q.filter(Trade.symbol == symbol)
        if start:
            q = q.filter(Trade.trade_time >= start)
        if end:
            q = q.filter(Trade.trade_time <= end)
        return list(q.order_by(Trade.trade_time.desc()).limit(limit).all())


def save_account_snapshot(snap: Dict[str, Any]):
    with get_session() as s:
        if not snap.get("snapshot_id"):
            snap["snapshot_id"] = gen_id("SNAP")
        s.add(AccountSnapshot(**snap))


def get_account_snapshots(account_id: str = "PA-001", limit: int = 1000) -> List[AccountSnapshot]:
    with get_session() as s:
        return list(s.query(AccountSnapshot).filter_by(account_id=account_id)
                    .order_by(AccountSnapshot.snapshot_time).limit(limit).all())


# ================================================================
# 回测 / 报告 / 记忆 / 日志
# ================================================================

def save_backtest_run(r: Dict[str, Any]):
    with get_session() as s:
        if not r.get("run_id"):
            r["run_id"] = gen_id("BT")
        s.add(BacktestRun(**r))


def update_backtest_run(run_id: str, status: str, finished_at=None):
    with get_session() as s:
        run = s.query(BacktestRun).filter_by(run_id=run_id).first()
        if run:
            run.status = status
            run.finished_at = finished_at or datetime.now()


def save_backtest_result(r: Dict[str, Any]):
    with get_session() as s:
        if not r.get("result_id"):
            r["result_id"] = gen_id("BTR")
        s.add(BacktestResult(**r))


def get_backtest_result(run_id: str) -> Optional[BacktestResult]:
    with get_session() as s:
        r = s.query(BacktestResult).filter_by(run_id=run_id).first()
        if r:
            s.expunge(r)
        return r


def save_report(r: Dict[str, Any]):
    with get_session() as s:
        if not r.get("report_id"):
            r["report_id"] = gen_id("RPT")
        s.add(ReportRecord(**r))


def save_memory(m: Dict[str, Any]):
    with get_session() as s:
        if not m.get("memory_id"):
            m["memory_id"] = gen_id("MEM")
        s.add(MemoryRecord(**m))


def get_memories(agent_name: Optional[str] = None, symbol: Optional[str] = None,
                 category: Optional[str] = None, limit: int = 100) -> List[MemoryRecord]:
    with get_session() as s:
        q = s.query(MemoryRecord)
        if agent_name:
            q = q.filter(MemoryRecord.agent_name == agent_name)
        if symbol:
            q = q.filter(MemoryRecord.symbol == symbol)
        if category:
            q = q.filter(MemoryRecord.category == category)
        return list(q.order_by(MemoryRecord.created_at.desc()).limit(limit).all())


def save_system_log(level: str, module: str, message: str):
    with get_session() as s:
        s.add(SystemLog(log_id=gen_id("LOG"), level=level, module=module, message=message))


def save_audit_log(trace_id: str, event_type: str, actor: str, payload: dict):
    with get_session() as s:
        s.add(AuditLog(log_id=gen_id("AUD"), trace_id=trace_id,
                       event_type=event_type, actor=actor,
                       payload_json=payload))


def get_audit_logs(trace_id: Optional[str] = None, event_type: Optional[str] = None,
                   limit: int = 500) -> List[AuditLog]:
    with get_session() as s:
        q = s.query(AuditLog)
        if trace_id:
            q = q.filter(AuditLog.trace_id == trace_id)
        if event_type:
            q = q.filter(AuditLog.event_type == event_type)
        return list(q.order_by(AuditLog.created_at.desc()).limit(limit).all())


def get_tool_permission(agent_name: str) -> Dict[str, str]:
    """Agent 工具权限: {tool_name: allow/deny}"""
    with get_session() as s:
        rows = s.query(ToolPermission).filter_by(agent_name=agent_name).all()
        return {r.tool_name: r.permission for r in rows}


# ================================================================
# 监控标的 (watchlist)
# ================================================================

def upsert_watch_item(symbol: str, name: str, asset_type: str = "etf",
                      categories: Optional[List[str]] = None,
                      enabled: bool = True, priority: int = 0):
    """
    新增/更新监控标的(幂等)。categories 合并而非覆盖。
    重要: 已存在的标的不会修改 enabled —— 用户停用的标的不被自动任务重新启用。
    """
    from database.models import WatchItem
    cats = categories or ["watched"]
    with get_session() as s:
        item = s.query(WatchItem).filter_by(symbol=symbol).first()
        if item is None:
            s.add(WatchItem(symbol=symbol, name=name, asset_type=asset_type,
                            categories=",".join(dict.fromkeys(cats)),
                            enabled=enabled, priority=priority))
        else:
            cur = set(item.categories.split(",")) if item.categories else set()
            cur.update(cats)
            item.categories = ",".join(dict.fromkeys(cur))
            item.name = name or item.name
            item.asset_type = asset_type
            item.priority = max(item.priority, priority)
            # enabled 保持用户设置(不覆盖停用状态)
            item.updated_at = datetime.now()


def set_watch_enabled(symbol: str, enabled: bool):
    from database.models import WatchItem
    with get_session() as s:
        item = s.query(WatchItem).filter_by(symbol=symbol).first()
        if item:
            item.enabled = enabled
            item.updated_at = datetime.now()


def set_watch_categories(symbol: str, categories: List[str]):
    from database.models import WatchItem
    with get_session() as s:
        item = s.query(WatchItem).filter_by(symbol=symbol).first()
        if item:
            item.categories = ",".join(dict.fromkeys(categories))
            item.updated_at = datetime.now()


def remove_watch_item(symbol: str):
    from database.models import WatchItem
    with get_session() as s:
        s.query(WatchItem).filter_by(symbol=symbol).delete()
        s.commit()


def get_watchlist(enabled_only: bool = False) -> List[Dict[str, Any]]:
    """监控列表(按优先级+加入时间排序)。"""
    from database.models import WatchItem
    with get_session() as s:
        q = s.query(WatchItem)
        if enabled_only:
            q = q.filter(WatchItem.enabled == True)  # noqa: E712
        rows = q.order_by(WatchItem.priority.desc(), WatchItem.added_at).all()
        return [
            {"symbol": r.symbol, "name": r.name, "asset_type": r.asset_type,
             "categories": r.categories.split(",") if r.categories else [],
             "enabled": r.enabled, "priority": r.priority,
             "added_at": str(r.added_at)[:16]}
            for r in rows
        ]
