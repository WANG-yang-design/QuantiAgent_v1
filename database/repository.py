# -*- coding: utf-8 -*-
"""
仓库层: 所有数据库读写操作封装
===============================
供数据服务/Agent/风控/模拟盘等上层模块调用, 统一会话管理。
"""
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session

from core.ids import gen_id
from database.db_session import get_session
from database.models import (
    Account, AccountSnapshot, AgentOutput, AgentRun, AgentShadowObservation, AnnouncementRecord,
    AuditLog, BacktestResult, BacktestRun, DailyBar, EtfInfo, EtfNavRecord,
    EtfUniverseSnapshot, FeatureRecord, FundamentalRecord, HumanConfirmation, MemoryRecord,
    MinuteBar, MoneyFlowRecord, NewsRecord, Order, OrderBookSnapshot,
    Position, PromptVersion, RagChunk, RagDocument, RealtimeQuote,
    ReportRecord, ResearchDecision, RiskCheck, SentimentRecord, StrategySignal,
    Symbol, SystemLog, SystemState, ToolPermission, Trade, TradePlan,
)


# ================================================================
# 标的
# ================================================================

def upsert_symbols(symbols: List[Dict[str, Any]]):
    """批量写入/更新标的信息。"""
    cols = _model_columns(Symbol)
    with get_session() as s:
        for item in symbols:
            item = {k: v for k, v in item.items() if k in cols}
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


def get_symbol(symbol: str) -> Optional[Symbol]:
    """读取单个标的(手续费按资产类型计算等场景用)。"""
    with get_session() as s:
        sy = s.query(Symbol).filter_by(symbol=symbol).first()
        s.expunge(sy) if sy else None
        return sy


def get_symbol_metadata(symbols: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
    """Read active and delisted symbol identities without survivorship filtering."""
    with get_session() as s:
        q = s.query(Symbol)
        if symbols:
            q = q.filter(Symbol.symbol.in_(symbols))
        return {
            r.symbol: {
                "name": r.name or "", "asset_type": r.asset_type,
                "status": r.status, "listed_date": r.listed_date,
                "exchange": r.exchange or "",
            }
            for r in q.all()
        }


def set_symbol_listed_date(symbol: str, listed_date: date) -> bool:
    """补记上市日；仅填充空值，避免用推断值覆盖已有权威基础资料。"""
    with get_session() as s:
        sy = s.get(Symbol, symbol)
        if sy is None or sy.listed_date is not None:
            return False
        sy.listed_date = listed_date
        sy.updated_at = datetime.now()
        return True


# ================================================================
# 行情
# ================================================================

def upsert_daily_bars(bars: List[Dict[str, Any]]):
    """日K批量写入(去重: symbol+date+source)。"""
    if not bars:
        return
    cols = _model_columns(DailyBar)
    payload = [{k: v for k, v in b.items() if k in cols} for b in bars]
    from sqlalchemy.dialects.postgresql import insert
    stmt = insert(DailyBar).values(payload)
    update_cols = {c: getattr(stmt.excluded, c) for c in cols
                   if c not in {"id", "symbol", "trade_date", "source"}}
    stmt = stmt.on_conflict_do_update(
        constraint="uq_daily_symbol_date_source", set_=update_cols)
    for attempt in range(3):
        try:
            with get_session() as s:
                s.execute(stmt)
            return
        except (OperationalError, DBAPIError) as exc:
            if attempt == 2 or not (
                isinstance(exc, OperationalError) or
                getattr(exc, "connection_invalidated", False)
            ):
                raise
            import time
            time.sleep(0.2 * (2 ** attempt))


def get_daily_bars(symbol: str, start: date, end: date,
                   quality: Optional[str] = None) -> List[DailyBar]:
    """读取日K。多源去重: 同一日期只保留主源行 —— 防止前复权(qfq)与不复权
    数据混写导致指标/回测被污染。quality=None 时不过滤(ESTIMATED 也返回)。"""
    from core.config import get_settings
    rows: List[DailyBar] = []
    for attempt in range(3):
        try:
            with get_session() as s:
                q = s.query(DailyBar).filter(
                    DailyBar.symbol == symbol,
                    DailyBar.trade_date >= start,
                    DailyBar.trade_date <= end,
                )
                if quality:
                    q = q.filter(DailyBar.quality_status == quality)
                rows = list(q.order_by(DailyBar.trade_date).all())
            break
        except (OperationalError, DBAPIError) as exc:
            if attempt == 2 or not (
                isinstance(exc, OperationalError) or
                getattr(exc, "connection_invalidated", False)
            ):
                raise
            import time
            time.sleep(0.2 * (2 ** attempt))
    # 数据源优先级(主源优先)
    try:
        cfg = get_settings().section("data_sources")
        spec = cfg.get("sources", {}).get("daily_bar", {})
        chain = [spec.get("primary", "")] + spec.get("backups", [])
        priority = {name: i for i, name in enumerate(chain) if name}
    except Exception:
        priority = {}
    seen: Dict[date, DailyBar] = {}
    try:
        from core.timeutil import today as business_today
        current_day = business_today()
    except Exception:
        current_day = date.today()
    for r in rows:
        cur = seen.get(r.trade_date)
        if cur is None:
            seen[r.trade_date] = r
        else:
            # 完整行情优先于残缺行情。实时源补出的当日K线通常含成交量/额，
            # 某些历史源的临时当日行 amount=0；不能仅因固定来源排名更高就覆盖
            # 完整OHLCV。完整度相同时再按配置的来源优先级选择。
            cur_complete = bool((cur.volume or 0) > 0 and (cur.amount or 0) > 0)
            new_complete = bool((r.volume or 0) > 0 and (r.amount or 0) > 0)
            current_day_tencent = (
                r.trade_date == current_day and r.source == "tencent" and new_complete
                and cur.source != "tencent")
            if (current_day_tencent or (new_complete and not cur_complete) or
                    (new_complete == cur_complete and
                     priority.get(r.source, 99) < priority.get(cur.source, 99))):
                seen[r.trade_date] = r
    return [seen[d] for d in sorted(seen)]


def upsert_minute_bars(bars: List[Dict[str, Any]]):
    if not bars:
        return
    cols = _model_columns(MinuteBar)
    payload = [{k: v for k, v in b.items() if k in cols} for b in bars]
    from sqlalchemy.dialects.postgresql import insert
    stmt = insert(MinuteBar).values(payload)
    update_cols = {c: getattr(stmt.excluded, c) for c in cols
                   if c not in {"id", "symbol", "bar_time", "freq", "source"}}
    stmt = stmt.on_conflict_do_update(
        constraint="uq_minute_symbol_time_freq", set_=update_cols)
    with get_session() as s:
        s.execute(stmt)


def get_minute_bars(symbol: str, start: datetime, end: datetime,
                    freq: str = "5m", limit: Optional[int] = None) -> List[MinuteBar]:
    """读取分钟K。修复: 与日K一致做多源去重 —— 原实现盘中 failover 切源后
    同一 bar_time 存在两个源的行, 指标/回测数据被翻倍。"""
    with get_session() as s:
        q = s.query(MinuteBar).filter(
            MinuteBar.symbol == symbol,
            MinuteBar.bar_time >= start,
            MinuteBar.bar_time <= end,
            MinuteBar.freq == freq,
        ).order_by(MinuteBar.bar_time)
        if limit:
            q = q.limit(limit)
        rows = list(q.all())
    try:
        cfg = get_settings().section("data_sources")
        spec = cfg.get("sources", {}).get("minute_bar", {})
        chain = [spec.get("primary", "")] + spec.get("backups", [])
        priority = {name: i for i, name in enumerate(chain) if name}
    except Exception:
        priority = {}
    seen: Dict[datetime, MinuteBar] = {}
    for r in rows:
        cur = seen.get(r.bar_time)
        if cur is None:
            seen[r.bar_time] = r
        elif priority.get(r.source, 99) < priority.get(cur.source, 99):
            seen[r.bar_time] = r
    return [seen[t] for t in sorted(seen)]


def _model_columns(model) -> set:
    """模型全部列名(入库白名单用, 防止数据源多余键导致 TypeError)。"""
    return set(model.__table__.columns.keys())


def save_realtime_quote(q: Dict[str, Any]):
    """实时行情入库。修复: 按模型列白名单过滤 —— 原实现 akshare/eastmoney
    返回的 iopv、sina 返回的 name 等多余键导致 RealtimeQuote(**q) 抛 TypeError,
    且上层静默吞异常, 实时行情从未写入数据库。"""
    with get_session() as s:
        cols = _model_columns(RealtimeQuote)
        s.add(RealtimeQuote(**{k: v for k, v in q.items() if k in cols}))


def get_latest_quote(symbol: str) -> Optional[RealtimeQuote]:
    with get_session() as s:
        return s.query(RealtimeQuote).filter(
            RealtimeQuote.symbol == symbol).order_by(
            RealtimeQuote.quote_time.desc()).first()


def save_order_book(ob: Dict[str, Any]):
    with get_session() as s:
        cols = _model_columns(OrderBookSnapshot)
        s.add(OrderBookSnapshot(**{k: v for k, v in ob.items() if k in cols}))


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


def upsert_news(news_list: List[Dict[str, Any]]) -> int:
    """新闻批量入库(去重), 返回实际新增条数。"""
    added = 0
    with get_session() as s:
        for n in news_list:
            if not n.get("news_id"):
                n["news_id"] = gen_id("NEWS")
            exist = s.query(NewsRecord).filter_by(news_id=n["news_id"]).first()
            if exist is None:
                s.add(NewsRecord(**n))
                added += 1
    return added


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


def upsert_announcements(ann_list: List[Dict[str, Any]]) -> int:
    """公告批量入库(去重), 返回实际新增条数。"""
    added = 0
    with get_session() as s:
        for a in ann_list:
            exist = s.query(AnnouncementRecord).filter_by(
                announcement_id=a["announcement_id"]).first()
            if exist is None:
                s.add(AnnouncementRecord(**a))
                added += 1
    return added


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
        cols = _model_columns(SentimentRecord)
        s.add(SentimentRecord(**{k: v for k, v in rec.items() if k in cols}))


def get_sentiment(symbol: str, start: datetime, end: datetime, limit: int = 200) -> List[SentimentRecord]:
    with get_session() as s:
        return list(s.query(SentimentRecord).filter(
            SentimentRecord.symbol == symbol,
            SentimentRecord.publish_time >= start,
            SentimentRecord.publish_time <= end,
        ).order_by(SentimentRecord.publish_time.desc()).limit(limit).all())


def upsert_fundamentals(items: List[Dict[str, Any]]):
    if not items:
        return
    cols = _model_columns(FundamentalRecord)
    payload = [{k: v for k, v in it.items() if k in cols} for it in items]
    from sqlalchemy.dialects.postgresql import insert
    stmt = insert(FundamentalRecord).values(payload)
    update_cols = {c: getattr(stmt.excluded, c) for c in cols
                   if c not in {"id", "symbol", "report_date"}}
    stmt = stmt.on_conflict_do_update(
        constraint="uq_fund_symbol_date", set_=update_cols)
    with get_session() as s:
        s.execute(stmt)


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


def get_etf_metadata(symbols: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
    """Return ETF identity metadata in one query for theme de-duplication."""
    with get_session() as s:
        q = s.query(EtfInfo)
        if symbols:
            q = q.filter(EtfInfo.symbol.in_(symbols))
        return {
            r.symbol: {
                "name": r.name or "", "tracking_index": r.tracking_index or "",
                "listed_date": r.listed_date, "scale": float(r.scale or 0),
                "is_qdii": bool(r.is_qdii),
            }
            for r in q.all()
        }


def get_etf_history_symbols(end: date, min_bars: int = 20) -> List[str]:
    """ETF master available locally by an historical date, including delisted rows."""
    with get_session() as s:
        rows = s.query(DailyBar.symbol).join(
            Symbol, Symbol.symbol == DailyBar.symbol).filter(
                DailyBar.trade_date <= end,
                Symbol.asset_type == "etf",
            ).group_by(DailyBar.symbol).having(
                func.count(func.distinct(DailyBar.trade_date)) >= max(int(min_bars), 1)
            ).order_by(DailyBar.symbol).all()
        return [str(r[0]) for r in rows]


def get_etf_bar_coverage(end: date) -> Dict[str, Dict[str, Any]]:
    """Bulk daily-bar coverage used by gradual ETF history backfill."""
    with get_session() as s:
        rows = s.query(
            DailyBar.symbol, func.count(func.distinct(DailyBar.trade_date)),
            func.min(DailyBar.trade_date), func.max(DailyBar.trade_date),
        ).join(Symbol, Symbol.symbol == DailyBar.symbol).filter(
            Symbol.asset_type == "etf", DailyBar.trade_date <= end,
        ).group_by(DailyBar.symbol).all()
        return {
            str(symbol): {"count": int(count or 0), "start": start, "end": last}
            for symbol, count, start, last in rows
        }


def save_etf_universe_snapshot(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Persist an immutable universe snapshot; identical ids are idempotent."""
    with get_session() as s:
        row = s.query(EtfUniverseSnapshot).filter_by(
            snapshot_id=payload["snapshot_id"]).first()
        if row is None:
            row = EtfUniverseSnapshot(**{
                k: v for k, v in payload.items()
                if k in _model_columns(EtfUniverseSnapshot)
            })
            s.add(row)
            s.flush()
        return etf_universe_snapshot_dict(row)


def etf_universe_snapshot_dict(row: EtfUniverseSnapshot) -> Dict[str, Any]:
    return {
        "snapshot_id": row.snapshot_id,
        "selector_version": row.selector_version,
        "source_mode": row.source_mode,
        "asof_date": str(row.asof_date),
        "effective_date": str(row.effective_date),
        "candidate_count": int(row.candidate_count or 0),
        "members": list(row.members_json or []),
        "config": dict(row.config_json or {}),
        "snapshot_hash": row.snapshot_hash or "",
        "created_at": str(row.created_at)[:19],
    }


def get_latest_etf_universe_snapshot(
        effective_on: date, source_mode: str = "paper"
) -> Optional[Dict[str, Any]]:
    with get_session() as s:
        row = s.query(EtfUniverseSnapshot).filter(
            EtfUniverseSnapshot.source_mode == source_mode,
            EtfUniverseSnapshot.effective_date <= effective_on,
        ).order_by(
            EtfUniverseSnapshot.effective_date.desc(),
            EtfUniverseSnapshot.created_at.desc(),
        ).first()
        return etf_universe_snapshot_dict(row) if row else None


def list_etf_universe_snapshots(
        limit: int = 20, source_mode: Optional[str] = None
) -> List[Dict[str, Any]]:
    with get_session() as s:
        q = s.query(EtfUniverseSnapshot)
        if source_mode:
            q = q.filter(EtfUniverseSnapshot.source_mode == source_mode)
        rows = q.order_by(
            EtfUniverseSnapshot.effective_date.desc(),
            EtfUniverseSnapshot.created_at.desc(),
        ).limit(max(1, min(int(limit), 200))).all()
        return [etf_universe_snapshot_dict(r) for r in rows]


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


def upsert_agent_shadow_observation(item: Dict[str, Any]) -> AgentShadowObservation:
    """保存策略/Agent 配对样本；同一策略同日同标的同方向重复运行只更新执行结果。"""
    cols = _model_columns(AgentShadowObservation)
    payload = {k: v for k, v in item.items() if k in cols}
    payload.setdefault("shadow_id", gen_id("SHADOW"))
    payload["updated_at"] = datetime.now()
    with get_session() as s:
        row = s.query(AgentShadowObservation).filter_by(
            strategy_id=payload["strategy_id"], symbol=payload["symbol"],
            signal_date=payload["signal_date"],
            strategy_action=payload["strategy_action"],
        ).first()
        if row is None:
            row = AgentShadowObservation(**payload)
            s.add(row)
        else:
            # 观点和行情必须保持为信号发生当时的事实；重复调度仅可补订单结果。
            immutable = {
                "id", "shadow_id", "created_at", "signal_time",
                "strategy_id", "strategy_name", "universe_snapshot_id",
                "symbol", "name", "signal_date", "strategy_action",
                "strategy_reason", "signal_price", "agent_decision",
                "agent_confidence", "agent_trace_id", "agent_decision_time",
                "agent_age_minutes", "agreement",
            }
            for key, value in payload.items():
                if key not in immutable:
                    setattr(row, key, value)
        s.flush()
        s.expunge(row)
        return row


def list_agent_shadow_observations(start: Optional[datetime] = None,
                                   limit: int = 1000) -> List[AgentShadowObservation]:
    with get_session() as s:
        q = s.query(AgentShadowObservation)
        if start:
            q = q.filter(AgentShadowObservation.signal_time >= start)
        return list(q.order_by(AgentShadowObservation.signal_time.desc()).limit(limit).all())


def update_agent_shadow_evaluation(shadow_id: str, forward_returns: Dict[str, float],
                                   evaluated_horizons: List[int], status: str) -> None:
    with get_session() as s:
        row = s.query(AgentShadowObservation).filter_by(shadow_id=shadow_id).first()
        if row:
            row.forward_returns = dict(forward_returns)
            row.evaluated_horizons = list(evaluated_horizons)
            row.evaluation_status = status
            row.updated_at = datetime.now()


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


def update_agent_usage(run_id: str, prompt_tokens: int, completion_tokens: int):
    """记录单次 LLM 调用的真实 token 用量(成本审计, 修复: 之前完全无记录)。"""
    with get_session() as s:
        run = s.query(AgentRun).filter_by(run_id=run_id).first()
        if run:
            run.prompt_tokens = int(prompt_tokens or 0)
            run.completion_tokens = int(completion_tokens or 0)


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
    """保存人工确认单。修复: 原实现不返回值, 调用方拿到 None,
    人工确认闭环(按 confirm_id 直连确认)断裂。"""
    with get_session() as s:
        if not c.get("confirm_id"):
            c["confirm_id"] = gen_id("CFM")
        s.add(HumanConfirmation(**c))
        return c["confirm_id"]


def list_pending_confirmations() -> List[HumanConfirmation]:
    with get_session() as s:
        return list(s.query(HumanConfirmation).filter_by(status="PENDING")
                    .order_by(HumanConfirmation.created_at).all())


def list_confirmations(limit: int = 100) -> List[HumanConfirmation]:
    """全部确认记录（含终态），用于操作审计页面。"""
    with get_session() as s:
        return list(s.query(HumanConfirmation)
                    .order_by(HumanConfirmation.created_at.desc())
                    .limit(max(1, min(int(limit), 500))).all())


def decide_confirmation(confirm_id: str, approved: bool, by: str = "web",
                        note: str = ""):
    """兼容入口；仅允许 PENDING 状态被决定，返回是否成功。"""
    target = "PROCESSING" if approved else "REJECTED"
    with get_session() as s:
        changed = s.query(HumanConfirmation).filter_by(
            confirm_id=confirm_id, status="PENDING").update({
                "status": target, "decided_at": datetime.now(), "decided_by": by,
                "decision_note": note or "",
            }, synchronize_session=False)
        return changed == 1


def set_confirmation_status(confirm_id: str, status: str, by: str = "web",
                            expected: Optional[str] = None,
                            note: Optional[str] = None) -> bool:
    """条件式更新确认单状态，避免并发确认产生两笔订单。"""
    with get_session() as s:
        q = s.query(HumanConfirmation).filter_by(confirm_id=confirm_id)
        if expected:
            q = q.filter(HumanConfirmation.status == expected)
        values = {"status": status, "decided_at": datetime.now(), "decided_by": by}
        if note is not None:
            values["decision_note"] = note
        changed = q.update(values, synchronize_session=False)
        return changed == 1


def get_confirmation(confirm_id: str) -> Optional[HumanConfirmation]:
    """读取确认单(人工确认闭环用)。"""
    with get_session() as s:
        c = s.query(HumanConfirmation).filter_by(confirm_id=confirm_id).first()
        s.expunge(c) if c else None
        return c


def get_trade_plan(plan_id: str) -> Optional[TradePlan]:
    """读取交易计划(人工确认批准后恢复执行用)。"""
    with get_session() as s:
        p = s.query(TradePlan).filter_by(plan_id=plan_id).first()
        s.expunge(p) if p else None
        return p


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


def refresh_account_prices(account_id: str, prices: Dict[str, float]):
    """只更新行情派生字段，避免把 PaperAccount 的过期账户快照写回数据库。"""
    clean = {str(k): float(v) for k, v in prices.items() if float(v or 0) > 0}
    with get_session() as s:
        acc = s.scalar(select(Account).where(
            Account.account_id == account_id).with_for_update())
        if acc is None:
            raise ValueError(f"账户不存在: {account_id}")
        positions = list(s.scalars(select(Position).where(
            Position.account_id == account_id).with_for_update()))
        total_mv = 0.0
        for p in positions:
            if p.symbol in clean:
                p.latest_price = clean[p.symbol]
                p.peak_price = max(float(p.peak_price or 0), p.latest_price)
            p.market_value = int(p.total_qty or 0) * float(p.latest_price or 0)
            p.pnl = p.market_value - int(p.total_qty or 0) * float(p.cost_price or 0)
            p.pnl_pct = (float(p.latest_price or 0) / float(p.cost_price or 0) - 1) \
                if p.cost_price else 0
            p.updated_at = datetime.now()
            total_mv += p.market_value
        acc.market_value = total_mv
        acc.total_asset = float(acc.cash or 0) + float(acc.frozen_cash or 0) + total_mv
        acc.total_pnl = acc.total_asset - float(acc.init_cash or 0)
        acc.update_time = datetime.now()


def unlock_t1_positions(account_id: str, today: date) -> bool:
    """原子解锁隔夜买入持仓，返回是否有更新。"""
    with get_session() as s:
        # 账户锁使解锁与下单/成交按账户串行，避免重复解锁。
        s.scalar(select(Account).where(
            Account.account_id == account_id).with_for_update())
        rows = list(s.scalars(select(Position).where(
            Position.account_id == account_id,
            Position.today_buy_qty > 0,
        ).with_for_update()))
        changed = False
        for p in rows:
            if p.buy_date is None or p.buy_date < today:
                p.available_qty = int(p.available_qty or 0) + int(p.today_buy_qty or 0)
                p.today_buy_qty = 0
                p.updated_at = datetime.now()
                changed = True
        return changed


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
    """
    按 position_id 更新或插入持仓(不能用 session.merge:
    Position 主键是自增 id, merge 会误判为新增导致唯一冲突)。
    """
    with get_session() as s:
        exist = s.query(Position).filter_by(position_id=p.position_id).first()
        if exist:
            for col in p.__table__.columns.keys():
                if col != "id":
                    setattr(exist, col, getattr(p, col))
        else:
            s.add(p)
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


def reserve_order(o: Dict[str, Any]) -> Order:
    """在同一数据库事务中创建订单并冻结资金/持仓。

    账户行同时充当同一账户所有资金和持仓变更的串行化锁，因此 Web、调度器等
    多进程实例不会再各自依据过期快照重复使用同一笔资金或持仓。
    """
    side = str(o.get("side", "")).upper()
    qty = int(o.get("qty", 0) or 0)
    if side not in {"BUY", "SELL"} or qty <= 0:
        raise ValueError("无效的订单方向或数量")
    with get_session() as s:
        intent = str(o.get("order_intent_id") or gen_id("INTENT"))
        existing = s.scalar(select(Order).where(Order.order_intent_id == intent))
        if existing is not None:
            s.expunge(existing)
            return existing

        account_id = str(o.get("account_id") or "PA-001")
        acc = s.scalar(select(Account).where(
            Account.account_id == account_id).with_for_update())
        if acc is None:
            raise ValueError(f"账户不存在: {account_id}")

        frozen_amount = float(o.get("frozen_amount", 0) or 0)
        if side == "BUY":
            if frozen_amount <= 0:
                raise ValueError("买单冻结金额必须大于 0")
            if float(acc.cash or 0) + 1e-6 < frozen_amount:
                raise ValueError(
                    f"资金不足: 需{frozen_amount:.2f}, 可用{float(acc.cash or 0):.2f}")
            acc.cash = float(acc.cash or 0) - frozen_amount
            acc.frozen_cash = float(acc.frozen_cash or 0) + frozen_amount
        else:
            pos = s.scalar(select(Position).where(
                Position.account_id == account_id,
                Position.symbol == o["symbol"],
            ).with_for_update())
            available = int(pos.available_qty or 0) if pos else 0
            if pos is None or available < qty:
                raise ValueError(f"可用持仓不足: {o['symbol']} 可用{available}, 需{qty}")
            pos.available_qty = available - qty
            pos.frozen_qty = int(pos.frozen_qty or 0) + qty
            pos.updated_at = datetime.now()
            frozen_amount = 0.0

        payload = {k: v for k, v in o.items() if k in _model_columns(Order)}
        payload.update({
            "order_id": payload.get("order_id") or gen_id("ORD"),
            "order_intent_id": intent,
            "frozen_amount": frozen_amount,
        })
        order = Order(**payload)
        s.add(order)
        s.flush()
        s.expunge(order)
        return order


def fill_order(order_id: str, price: float, qty: int, fee: float,
               trade_time: datetime, is_t0: bool = False) -> Optional[Dict[str, Any]]:
    """锁定订单并原子完成订单、账户、持仓和成交四类更新。"""
    active = {"SUBMITTED", "ACCEPTED", "PARTIALLY_FILLED"}
    price, fee, qty = float(price), float(fee), int(qty)
    if price <= 0 or qty <= 0 or fee < 0:
        raise ValueError("无效的成交价格、数量或费用")
    with get_session() as s:
        order = s.scalar(select(Order).where(
            Order.order_id == order_id).with_for_update())
        if order is None or order.status not in active:
            return None
        remaining_before = int(order.remaining_qty or 0)
        if qty > remaining_before:
            raise ValueError(f"成交数量超过未成交数量: {qty}>{remaining_before}")

        acc = s.scalar(select(Account).where(
            Account.account_id == order.account_id).with_for_update())
        if acc is None:
            raise ValueError(f"账户不存在: {order.account_id}")
        pos = s.scalar(select(Position).where(
            Position.account_id == order.account_id,
            Position.symbol == order.symbol,
        ).with_for_update())

        pnl = None
        if order.side == "BUY":
            frozen = float(order.frozen_amount or 0)
            release = frozen if qty == remaining_before else frozen * qty / remaining_before
            cost = price * qty + fee
            if float(acc.cash or 0) + release + 1e-6 < cost:
                raise ValueError(
                    f"滑点后资金不足: 需{cost:.2f}, 可用{float(acc.cash or 0) + release:.2f}")
            acc.frozen_cash = max(0.0, float(acc.frozen_cash or 0) - release)
            acc.cash = float(acc.cash or 0) + release - cost
            order.frozen_amount = max(0.0, frozen - release)
            if pos is None:
                pos = Position(
                    position_id=f"POS-{order.account_id}-{order.symbol}",
                    account_id=order.account_id, symbol=order.symbol,
                    name=order.name or "", total_qty=0, available_qty=0,
                    frozen_qty=0, today_buy_qty=0, cost_price=price,
                    latest_price=price, peak_price=price, market_value=0,
                    pnl=0, pnl_pct=0,
                )
                s.add(pos)
            old_qty = int(pos.total_qty or 0)
            old_cost = old_qty * float(pos.cost_price or 0)
            pos.total_qty = old_qty + qty
            if is_t0:
                pos.available_qty = int(pos.available_qty or 0) + qty
            else:
                pos.today_buy_qty = int(pos.today_buy_qty or 0) + qty
            pos.cost_price = (old_cost + price * qty) / pos.total_qty
            pos.latest_price = price
            pos.peak_price = max(float(pos.peak_price or 0), price)
            pos.buy_date = trade_time.date()
        else:
            if pos is None:
                raise ValueError(f"无持仓可卖: {order.symbol}")
            if int(pos.frozen_qty or 0) < qty or int(pos.total_qty or 0) < qty:
                raise ValueError(f"冻结持仓不足: {order.symbol}")
            cost_price = float(pos.cost_price or 0)
            pnl = round((price - cost_price) * qty - fee, 2) if cost_price else None
            acc.cash = float(acc.cash or 0) + price * qty - fee
            pos.frozen_qty = int(pos.frozen_qty or 0) - qty
            pos.total_qty = int(pos.total_qty or 0) - qty
            if is_t0:
                pos.today_buy_qty = max(0, int(pos.today_buy_qty or 0) - qty)
            pos.latest_price = price
            if pos.total_qty <= 0:
                s.delete(pos)

        old_filled = int(order.filled_qty or 0)
        order.filled_qty = old_filled + qty
        order.remaining_qty = remaining_before - qty
        order.avg_fill_price = (
            float(order.avg_fill_price or 0) * old_filled + price * qty
        ) / order.filled_qty
        order.fee = float(order.fee or 0) + fee
        order.status = "FILLED" if order.remaining_qty == 0 else "PARTIALLY_FILLED"
        order.filled_time = trade_time
        acc.total_fee = float(acc.total_fee or 0) + fee

        trade = Trade(
            trade_id=gen_id("TRADE"), order_id=order.order_id,
            symbol=order.symbol, name=order.name or "", side=order.side,
            price=price, qty=qty, fee=fee, pnl=pnl, trade_time=trade_time,
        )
        s.add(trade)
        s.flush()

        positions = list(s.scalars(select(Position).where(
            Position.account_id == order.account_id)))
        total_mv = 0.0
        for p in positions:
            p.market_value = int(p.total_qty or 0) * float(p.latest_price or 0)
            p.pnl = p.market_value - int(p.total_qty or 0) * float(p.cost_price or 0)
            p.pnl_pct = (float(p.latest_price or 0) / float(p.cost_price or 0) - 1) \
                if p.cost_price else 0
            p.updated_at = trade_time
            total_mv += p.market_value
        acc.market_value = total_mv
        acc.total_asset = float(acc.cash or 0) + float(acc.frozen_cash or 0) + total_mv
        acc.total_pnl = acc.total_asset - float(acc.init_cash or 0)
        acc.update_time = trade_time
        return {
            "trade_id": trade.trade_id, "order_id": order.order_id,
            "symbol": order.symbol, "name": order.name or "", "side": order.side,
            "price": price, "qty": qty, "fee": fee, "pnl": pnl,
            "trade_time": trade_time, "status": order.status,
            "filled_qty": order.filled_qty, "remaining_qty": order.remaining_qty,
            "avg_fill_price": order.avg_fill_price,
            "frozen_amount": float(order.frozen_amount or 0),
        }


def cancel_order_atomic(order_id: str, reason: str = "manual") -> Order:
    """原子撤单并仅释放订单当前实际剩余的冻结资源。"""
    active = {"CREATED", "RISK_CHECKED", "SUBMITTED", "ACCEPTED", "PARTIALLY_FILLED"}
    with get_session() as s:
        order = s.scalar(select(Order).where(
            Order.order_id == order_id).with_for_update())
        if order is None:
            raise ValueError(f"订单不存在: {order_id}")
        if order.status not in active:
            raise ValueError(f"订单状态 {order.status} 不可撤单")
        acc = s.scalar(select(Account).where(
            Account.account_id == order.account_id).with_for_update())
        if order.side == "BUY":
            release = min(float(order.frozen_amount or 0), float(acc.frozen_cash or 0))
            acc.frozen_cash = float(acc.frozen_cash or 0) - release
            acc.cash = float(acc.cash or 0) + release
            order.frozen_amount = 0.0
        elif int(order.remaining_qty or 0) > 0:
            pos = s.scalar(select(Position).where(
                Position.account_id == order.account_id,
                Position.symbol == order.symbol,
            ).with_for_update())
            if pos is not None:
                release_qty = min(int(order.remaining_qty or 0), int(pos.frozen_qty or 0))
                pos.frozen_qty = int(pos.frozen_qty or 0) - release_qty
                pos.available_qty = int(pos.available_qty or 0) + release_qty
                pos.updated_at = datetime.now()
        order.status = "CANCELLED"
        order.cancel_time = datetime.now()
        order.reject_reason = reason
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
               end: Optional[datetime] = None, limit: int = 500,
               account_id: Optional[str] = None) -> List[Trade]:
    """最近成交。
    修复: 原实现无账户过滤 —— 测试套件(PA-TEST-*)、回测等其他账户的成交
    全部混入 trades 表(表本身无 account_id 列), 模拟盘页面会看到与真实
    持仓无关的"幽灵成交"(29条成交但持仓没动)。现按订单表 join 过滤。"""
    with get_session() as s:
        q = s.query(Trade)
        if account_id:
            from database.models import Order
            q = q.join(Order, Trade.order_id == Order.order_id).filter(
                Order.account_id == account_id)
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
    """回测任务记录(幂等: run_id 已存在则更新而非重复插入)。"""
    from database.models import BacktestRun
    with get_session() as s:
        if not r.get("run_id"):
            r["run_id"] = gen_id("BT")
        exist = s.query(BacktestRun).filter_by(run_id=r["run_id"]).first()
        if exist:
            for k, v in r.items():
                if k != "run_id":
                    setattr(exist, k, v)
        else:
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


def list_reports(report_type: Optional[str] = None, limit: int = 30) -> List[ReportRecord]:
    """报告列表(按生成时间倒序)。"""
    with get_session() as s:
        q = s.query(ReportRecord)
        if report_type:
            q = q.filter(ReportRecord.report_type == report_type)
        return list(q.order_by(ReportRecord.created_at.desc()).limit(limit).all())


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


def get_system_state(key: str) -> Dict[str, Any]:
    with get_session() as s:
        row = s.get(SystemState, key)
        return dict(row.value_json or {}) if row else {}


def update_system_state(key: str, updater) -> Dict[str, Any]:
    """锁行后读改写共享状态；updater 接收可变 dict。"""
    with get_session() as s:
        row = s.scalar(select(SystemState).where(
            SystemState.key == key).with_for_update())
        if row is None:
            row = SystemState(key=key, value_json={})
            s.add(row)
            s.flush()
        value = dict(row.value_json or {})
        updater(value)
        row.value_json = value
        row.updated_at = datetime.now()
        return value


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


def set_watch_category_flag(symbol: str, category: str, enabled: bool) -> None:
    """Atomically add/remove one category without losing concurrent flags."""
    from database.models import WatchItem
    with get_session() as s:
        item = s.query(WatchItem).filter_by(symbol=symbol).with_for_update().first()
        if item:
            cats = [x for x in (item.categories or "").split(",") if x]
            cats = [x for x in cats if x != category]
            if enabled:
                cats.append(category)
            item.categories = ",".join(dict.fromkeys(cats))
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


# ================================================================
# 市场诊断
# ================================================================

def save_market_diagnostic(d: Dict[str, Any]):
    from database.models import MarketDiagnostic
    with get_session() as s:
        s.add(MarketDiagnostic(**d))
        s.commit()


def get_latest_market_diagnostic(max_age_minutes: int = 60):
    """最近一次市场诊断(超过 max_age 分钟返回 None, 需重新计算)。"""
    from database.models import MarketDiagnostic
    with get_session() as s:
        row = s.query(MarketDiagnostic).order_by(
            MarketDiagnostic.created_at.desc()).first()
        if row is None:
            return None
        age = (datetime.now() - row.created_at).total_seconds() / 60
        if age > max_age_minutes:
            return None
        return {"state": row.state, "label": row.label, "advice": row.advice,
                "score": row.score, "detail": row.detail,
                "time": str(row.created_at)[:16]}
