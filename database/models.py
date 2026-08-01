# -*- coding: utf-8 -*-
"""
SQLAlchemy 模型层
=================
按《多Agent量化交易系统模块与数据库表结构V1》实现全部数据表:
行情/ETF/新闻公告/舆情/特征/信号/Agent运行/投研/计划/风控/账户/持仓/订单/成交/
回测/审计/报告 + RAG向量表(pgvector)。

命名规范: 表名 snake_case, 字段与文档一致。时间字段统一 UTC 毫秒无时区。
"""
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Index,
    Integer, JSON, Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from pgvector.sqlalchemy import Vector


class Base(DeclarativeBase):
    pass


# ================================================================
# 一、标的与行情
# ================================================================

class Symbol(Base):
    """股票和ETF基础标的信息表"""
    __tablename__ = "symbols"

    symbol: Mapped[str] = mapped_column(String(12), primary_key=True)     # 6位代码
    name: Mapped[str] = mapped_column(String(64))                          # 名称
    asset_type: Mapped[str] = mapped_column(String(8))                     # stock / etf
    exchange: Mapped[str] = mapped_column(String(8), default="")           # SH / SZ
    status: Mapped[str] = mapped_column(String(8), default="active")       # active / delisted / suspended
    listed_date: Mapped[Date | None] = mapped_column(Date, nullable=True)
    is_st: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class DailyBar(Base):
    """日K行情表 (质量标签见 quality_status: VALID/MISSING/DELAYED/SUSPICIOUS/CONFLICT/ESTIMATED)"""
    __tablename__ = "market_daily_bars"
    __table_args__ = (
        UniqueConstraint("symbol", "trade_date", "source", name="uq_daily_symbol_date_source"),
        Index("ix_daily_symbol_date", "symbol", "trade_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(12))
    trade_date: Mapped[Date] = mapped_column(Date)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0)      # 成交量(股/份)
    amount: Mapped[float] = mapped_column(Float, default=0)      # 成交额(元)
    source: Mapped[str] = mapped_column(String(16), default="akshare")
    quality_status: Mapped[str] = mapped_column(String(12), default="VALID")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class MinuteBar(Base):
    """分钟K行情表 (freq: 1m/5m/15m/30m/60m)"""
    __tablename__ = "market_minute_bars"
    __table_args__ = (
        UniqueConstraint("symbol", "bar_time", "freq", "source", name="uq_minute_symbol_time_freq"),
        Index("ix_minute_symbol_time", "symbol", "bar_time"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(12))
    bar_time: Mapped[datetime] = mapped_column(DateTime)
    freq: Mapped[str] = mapped_column(String(4), default="5m")
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0)
    amount: Mapped[float] = mapped_column(Float, default=0)
    source: Mapped[str] = mapped_column(String(16), default="akshare")
    quality_status: Mapped[str] = mapped_column(String(12), default="VALID")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class RealtimeQuote(Base):
    """实时行情快照表"""
    __tablename__ = "realtime_quotes"
    __table_args__ = (Index("ix_quote_symbol_time", "symbol", "quote_time"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(12))
    quote_time: Mapped[datetime] = mapped_column(DateTime)
    latest_price: Mapped[float] = mapped_column(Float)
    change_pct: Mapped[float] = mapped_column(Float, default=0)   # 涨跌幅(%)
    volume: Mapped[float] = mapped_column(Float, default=0)
    amount: Mapped[float] = mapped_column(Float, default=0)
    high: Mapped[float] = mapped_column(Float, default=0)
    low: Mapped[float] = mapped_column(Float, default=0)
    open: Mapped[float] = mapped_column(Float, default=0)
    prev_close: Mapped[float] = mapped_column(Float, default=0)
    source: Mapped[str] = mapped_column(String(16), default="akshare")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class OrderBookSnapshot(Base):
    """盘口快照表 (五档/十档)"""
    __tablename__ = "order_book_snapshots"
    __table_args__ = (Index("ix_ob_symbol_time", "symbol", "snapshot_time"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(12))
    snapshot_time: Mapped[datetime] = mapped_column(DateTime)
    bid1: Mapped[float] = mapped_column(Float, default=0)
    ask1: Mapped[float] = mapped_column(Float, default=0)
    bid_vol1: Mapped[float] = mapped_column(Float, default=0)
    ask_vol1: Mapped[float] = mapped_column(Float, default=0)
    spread: Mapped[float] = mapped_column(Float, default=0)        # (ask1-bid1)/ask1
    order_book_json: Mapped[dict] = mapped_column(JSON, default=dict)   # 完整五档
    source: Mapped[str] = mapped_column(String(16), default="eastmoney")


class MoneyFlowRecord(Base):
    """资金流记录表 (主力/超大/大单/中单/小单净流入, 单位:元)"""
    __tablename__ = "money_flow_records"
    __table_args__ = (Index("ix_mf_symbol_time", "symbol", "record_time"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(12))
    record_time: Mapped[datetime] = mapped_column(DateTime)
    main_inflow: Mapped[float] = mapped_column(Float, default=0)
    net_inflow: Mapped[float] = mapped_column(Float, default=0)
    super_inflow: Mapped[float] = mapped_column(Float, default=0)
    large_inflow: Mapped[float] = mapped_column(Float, default=0)
    medium_inflow: Mapped[float] = mapped_column(Float, default=0)
    small_inflow: Mapped[float] = mapped_column(Float, default=0)
    main_inflow_ratio: Mapped[float] = mapped_column(Float, default=0)
    flow_rank: Mapped[int] = mapped_column(Integer, default=0)     # 当日板块/ETF内资金流排名
    source: Mapped[str] = mapped_column(String(16), default="eastmoney")


# ================================================================
# 二、新闻/公告/舆情
# ================================================================

class NewsRecord(Base):
    """新闻表 (个股/行业/宏观新闻)"""
    __tablename__ = "news_records"
    __table_args__ = (Index("ix_news_symbol_time", "symbol", "publish_time"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    news_id: Mapped[str] = mapped_column(String(32), unique=True)  # 来源新闻ID(去重)
    symbol: Mapped[str] = mapped_column(String(12), default="")    # 空=大盘新闻
    title: Mapped[str] = mapped_column(Text)
    content: Mapped[Text] = mapped_column(Text, default="")
    publish_time: Mapped[datetime] = mapped_column(DateTime)
    source: Mapped[str] = mapped_column(String(32), default="eastmoney")
    sentiment_score: Mapped[float] = mapped_column(Float, default=0.0)  # -1~1
    url: Mapped[str] = mapped_column(Text, default="")


class AnnouncementRecord(Base):
    """公告表 (巨潮资讯主源)"""
    __tablename__ = "announcement_records"
    __table_args__ = (Index("ix_ann_symbol_time", "symbol", "publish_time"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    announcement_id: Mapped[str] = mapped_column(String(48), unique=True)
    symbol: Mapped[str] = mapped_column(String(12), default="")
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text, default="")
    publish_time: Mapped[datetime] = mapped_column(DateTime)
    event_type: Mapped[str] = mapped_column(String(32), default="")   # 分红/减持/重组/业绩...
    risk_level: Mapped[str] = mapped_column(String(8), default="none")  # none/low/medium/high


class SentimentRecord(Base):
    """舆情情绪表 (股吧/雪球帖子情绪)"""
    __tablename__ = "sentiment_records"
    __table_args__ = (Index("ix_sent_symbol_time", "symbol", "publish_time"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    record_id: Mapped[str] = mapped_column(String(32), unique=True)
    symbol: Mapped[str] = mapped_column(String(12), default="")
    platform: Mapped[str] = mapped_column(String(16), default="guba")  # guba/xueqiu/news
    content: Mapped[Text] = mapped_column(Text, default="")
    score: Mapped[float] = mapped_column(Float, default=0.0)          # -1~1 情绪分
    heat: Mapped[float] = mapped_column(Float, default=0.0)           # 热度(阅读量等)
    publish_time: Mapped[datetime] = mapped_column(DateTime)


# ================================================================
# 三、基本面 / ETF
# ================================================================

class FundamentalRecord(Base):
    """基本面数据表 (股票)"""
    __tablename__ = "fundamental_records"
    __table_args__ = (Index("ix_fund_symbol_date", "symbol", "report_date"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(12))
    report_date: Mapped[Date] = mapped_column(Date)
    pe: Mapped[float] = mapped_column(Float, default=0)
    pb: Mapped[float] = mapped_column(Float, default=0)
    roe: Mapped[float] = mapped_column(Float, default=0)          # %
    revenue_growth: Mapped[float] = mapped_column(Float, default=0)
    profit_growth: Mapped[float] = mapped_column(Float, default=0)
    gross_margin: Mapped[float] = mapped_column(Float, default=0)
    debt_ratio: Mapped[float] = mapped_column(Float, default=0)
    source: Mapped[str] = mapped_column(String(16), default="akshare")


class EtfInfo(Base):
    """ETF基础信息表"""
    __tablename__ = "etf_info"

    symbol: Mapped[str] = mapped_column(String(12), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    tracking_index: Mapped[str] = mapped_column(String(64), default="")  # 跟踪指数
    scale: Mapped[float] = mapped_column(Float, default=0)          # 规模(元)
    fee_rate: Mapped[float] = mapped_column(Float, default=0)       # 管理费率
    fund_company: Mapped[str] = mapped_column(String(64), default="")
    is_qdii: Mapped[bool] = mapped_column(Boolean, default=False)   # 跨境ETF溢价风险高
    listed_date: Mapped[Date | None] = mapped_column(Date, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class EtfNavRecord(Base):
    """ETF净值和折溢价表"""
    __tablename__ = "etf_nav_records"
    __table_args__ = (Index("ix_nav_symbol_time", "symbol", "record_time"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(12))
    record_time: Mapped[datetime] = mapped_column(DateTime)
    nav: Mapped[float] = mapped_column(Float, default=0)           # 净值
    iopv: Mapped[float] = mapped_column(Float, default=0)          # 实时估值
    premium_rate: Mapped[float] = mapped_column(Float, default=0)  # 溢价率(%)
    source: Mapped[str] = mapped_column(String(16), default="eastmoney")


# ================================================================
# 四、特征/信号/Agent
# ================================================================

class FeatureRecord(Base):
    """特征指标表 (时间序列特征)"""
    __tablename__ = "features"
    __table_args__ = (
        UniqueConstraint("symbol", "feature_time", "feature_name", "timeframe",
                         name="uq_feature_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(12))
    feature_time: Mapped[datetime] = mapped_column(DateTime)
    feature_name: Mapped[str] = mapped_column(String(64))
    feature_value: Mapped[float] = mapped_column(Float, default=0)
    timeframe: Mapped[str] = mapped_column(String(8), default="1d")  # 1d/5m/1m


class StrategySignal(Base):
    """策略参考信号表 (策略信号作为Agent决策参考, 非最终指令)"""
    __tablename__ = "strategy_signals"
    __table_args__ = (Index("ix_sig_strategy_time", "strategy_id", "signal_time"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    signal_id: Mapped[str] = mapped_column(String(32), unique=True)
    strategy_id: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(12))
    signal_time: Mapped[datetime] = mapped_column(DateTime)
    signal: Mapped[str] = mapped_column(String(8))       # BUY/SELL/HOLD/RANK/EXCLUDE
    score: Mapped[float] = mapped_column(Float, default=0)
    reason: Mapped[str] = mapped_column(Text, default="")


class AgentRun(Base):
    """Agent运行记录表"""
    __tablename__ = "agent_runs"
    __table_args__ = (Index("ix_run_time", "start_time"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(32), unique=True)
    agent_name: Mapped[str] = mapped_column(String(48))
    symbol: Mapped[str] = mapped_column(String(12), default="")
    trace_id: Mapped[str] = mapped_column(String(32), default="")
    start_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    end_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="RUNNING")  # RUNNING/OK/FAILED
    model_name: Mapped[str] = mapped_column(String(48), default="")
    error: Mapped[str] = mapped_column(Text, default="")


class AgentOutput(Base):
    """Agent输出表 (结构化输出全量留痕, 审计用)"""
    __tablename__ = "agent_outputs"
    __table_args__ = (Index("ix_out_run", "run_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    output_id: Mapped[str] = mapped_column(String(32), unique=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    agent_name: Mapped[str] = mapped_column(String(48))
    view: Mapped[str] = mapped_column(String(16), default="")
    score: Mapped[float] = mapped_column(Float, default=0)
    confidence: Mapped[float] = mapped_column(Float, default=0)
    output_json: Mapped[dict] = mapped_column(JSON, default=dict)


class ResearchDecision(Base):
    """投研结论表 (首席研究员输出)"""
    __tablename__ = "research_decisions"
    __table_args__ = (Index("ix_research_symbol", "symbol"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(String(32), unique=True)
    symbol: Mapped[str] = mapped_column(String(12))
    decision: Mapped[str] = mapped_column(String(16))     # BUY_CANDIDATE/SELL_CANDIDATE/HOLD/EXCLUDE
    confidence: Mapped[float] = mapped_column(Float, default=0)
    bull_summary: Mapped[str] = mapped_column(Text, default="")
    bear_summary: Mapped[str] = mapped_column(Text, default="")
    reasoning: Mapped[dict] = mapped_column(JSON, default=dict)   # 各Agent观点汇总
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


# ================================================================
# 五、交易计划/风控
# ================================================================

class TradePlan(Base):
    """交易计划表"""
    __tablename__ = "trade_plans"
    __table_args__ = (Index("ix_plan_symbol", "symbol"), Index("ix_plan_status", "status"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plan_id: Mapped[str] = mapped_column(String(32), unique=True)
    decision_id: Mapped[str] = mapped_column(String(32), default="")
    trace_id: Mapped[str] = mapped_column(String(32), default="")
    symbol: Mapped[str] = mapped_column(String(12))
    name: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(8))        # BUY/SELL/HOLD/CANCEL
    target_weight: Mapped[float] = mapped_column(Float, default=0)
    order_amount: Mapped[float] = mapped_column(Float, default=0)
    estimated_quantity: Mapped[int] = mapped_column(Integer, default=0)
    order_type: Mapped[str] = mapped_column(String(8), default="LIMIT")
    limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0)
    reasons: Mapped[dict] = mapped_column(JSON, default=list)
    risks: Mapped[dict] = mapped_column(JSON, default=list)
    fallback: Mapped[str] = mapped_column(Text, default="")
    human_confirm_required: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="CREATED")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class RiskCheck(Base):
    """风控审核表"""
    __tablename__ = "risk_checks"
    __table_args__ = (Index("ix_risk_plan", "plan_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    risk_check_id: Mapped[str] = mapped_column(String(32), unique=True)
    decision_id: Mapped[str] = mapped_column(String(32), default="")
    plan_id: Mapped[str] = mapped_column(String(32), default="")
    result: Mapped[str] = mapped_column(String(16))     # APPROVE/REJECT/REDUCE/CONFIRM_REQUIRED
    risk_level: Mapped[str] = mapped_column(String(8), default="LOW")  # LOW/MEDIUM/HIGH
    approved_amount: Mapped[float] = mapped_column(Float, default=0)
    approved_quantity: Mapped[int] = mapped_column(Integer, default=0)
    warnings: Mapped[dict] = mapped_column(JSON, default=list)
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    layer_results: Mapped[dict] = mapped_column(JSON, default=dict)   # 每层风控明细(审计)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class HumanConfirmation(Base):
    """人工确认表 (分级确认: 自动/邮件界面确认/禁止)"""
    __tablename__ = "human_confirmations"
    __table_args__ = (Index("ix_confirm_status", "status"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    confirm_id: Mapped[str] = mapped_column(String(32), unique=True)
    plan_id: Mapped[str] = mapped_column(String(32), default="")
    symbol: Mapped[str] = mapped_column(String(12), default="")
    action: Mapped[str] = mapped_column(String(8), default="")
    amount: Mapped[float] = mapped_column(Float, default=0)
    risk_level: Mapped[str] = mapped_column(String(8), default="MEDIUM")
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(12), default="PENDING")  # PENDING/APPROVED/REJECTED/EXPIRED
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by: Mapped[str] = mapped_column(String(32), default="")


# ================================================================
# 六、账户/持仓/订单/成交
# ================================================================

class Account(Base):
    """账户表 (模拟盘/实盘预留)"""
    __tablename__ = "accounts"

    account_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    account_type: Mapped[str] = mapped_column(String(8), default="paper")  # paper/live
    cash: Mapped[float] = mapped_column(Float, default=0)         # 可用资金
    frozen_cash: Mapped[float] = mapped_column(Float, default=0)  # 冻结资金
    market_value: Mapped[float] = mapped_column(Float, default=0) # 证券市值
    total_asset: Mapped[float] = mapped_column(Float, default=0)
    total_pnl: Mapped[float] = mapped_column(Float, default=0)    # 总盈亏
    day_pnl: Mapped[float] = mapped_column(Float, default=0)      # 当日盈亏
    total_fee: Mapped[float] = mapped_column(Float, default=0)    # 累计手续费
    init_cash: Mapped[float] = mapped_column(Float, default=0)
    update_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    status: Mapped[str] = mapped_column(String(12), default="normal")  # normal/paused/readonly


class Position(Base):
    """持仓表 (支持A股T+1)"""
    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("account_id", "symbol", name="uq_pos_account_symbol"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    position_id: Mapped[str] = mapped_column(String(32), unique=True)
    account_id: Mapped[str] = mapped_column(String(16), index=True)
    symbol: Mapped[str] = mapped_column(String(12))
    name: Mapped[str] = mapped_column(String(64), default="")
    total_qty: Mapped[int] = mapped_column(Integer, default=0)
    available_qty: Mapped[int] = mapped_column(Integer, default=0)  # T+1 可卖数量
    frozen_qty: Mapped[int] = mapped_column(Integer, default=0)     # 冻结(挂卖单)
    today_buy_qty: Mapped[int] = mapped_column(Integer, default=0)  # 今日买入(不可卖)
    cost_price: Mapped[float] = mapped_column(Float, default=0)     # 摊薄成本
    latest_price: Mapped[float] = mapped_column(Float, default=0)
    market_value: Mapped[float] = mapped_column(Float, default=0)
    pnl: Mapped[float] = mapped_column(Float, default=0)            # 浮动盈亏
    pnl_pct: Mapped[float] = mapped_column(Float, default=0)
    buy_date: Mapped[Date | None] = mapped_column(Date, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Order(Base):
    """订单表 (订单状态机: CREATED→RISK_CHECKED→SUBMITTED→ACCEPTED→
       PARTIALLY_FILLED→FILLED / CANCEL_PENDING→CANCELLED / REJECTED / FAILED / UNKNOWN)"""
    __tablename__ = "orders"
    __table_args__ = (Index("ix_order_account", "account_id"), Index("ix_order_status", "status"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(32), unique=True)
    order_intent_id: Mapped[str] = mapped_column(String(32), unique=True)  # 幂等键
    plan_id: Mapped[str] = mapped_column(String(32), default="")
    account_id: Mapped[str] = mapped_column(String(32), default="PA-001")
    symbol: Mapped[str] = mapped_column(String(12))
    name: Mapped[str] = mapped_column(String(64), default="")
    side: Mapped[str] = mapped_column(String(8))          # BUY/SELL
    order_type: Mapped[str] = mapped_column(String(8), default="LIMIT")  # LIMIT/MARKET
    price: Mapped[float] = mapped_column(Float, default=0)
    qty: Mapped[int] = mapped_column(Integer, default=0)
    filled_qty: Mapped[int] = mapped_column(Integer, default=0)
    remaining_qty: Mapped[int] = mapped_column(Integer, default=0)
    avg_fill_price: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(16), default="CREATED")
    submit_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    filled_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancel_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reject_reason: Mapped[str] = mapped_column(Text, default="")
    fee: Mapped[float] = mapped_column(Float, default=0)
    source: Mapped[str] = mapped_column(String(16), default="agent")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Trade(Base):
    """成交表"""
    __tablename__ = "trades"
    __table_args__ = (Index("ix_trade_order", "order_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trade_id: Mapped[str] = mapped_column(String(32), unique=True)
    order_id: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str] = mapped_column(String(12))
    side: Mapped[str] = mapped_column(String(8))
    price: Mapped[float] = mapped_column(Float, default=0)
    qty: Mapped[int] = mapped_column(Integer, default=0)
    fee: Mapped[float] = mapped_column(Float, default=0)
    trade_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AccountSnapshot(Base):
    """账户快照表 (净值曲线/回撤分析)"""
    __tablename__ = "account_snapshots"
    __table_args__ = (Index("ix_snap_account_time", "account_id", "snapshot_time"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[str] = mapped_column(String(32), unique=True)
    account_id: Mapped[str] = mapped_column(String(16))
    snapshot_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    cash: Mapped[float] = mapped_column(Float, default=0)
    market_value: Mapped[float] = mapped_column(Float, default=0)
    total_asset: Mapped[float] = mapped_column(Float, default=0)
    pnl: Mapped[float] = mapped_column(Float, default=0)
    source: Mapped[str] = mapped_column(String(16), default="paper")


# ================================================================
# 七、回测
# ================================================================

class BacktestRun(Base):
    """回测任务表"""
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    start_date: Mapped[Date] = mapped_column(Date)
    end_date: Mapped[Date] = mapped_column(Date)
    mode: Mapped[str] = mapped_column(String(8), default="daily")   # daily/minute
    status: Mapped[str] = mapped_column(String(12), default="RUNNING")  # RUNNING/DONE/FAILED
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BacktestResult(Base):
    """回测结果表 (指标JSON全量存储)"""
    __tablename__ = "backtest_results"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    result_id: Mapped[str] = mapped_column(String(32), unique=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    total_return: Mapped[float] = mapped_column(Float, default=0)
    annual_return: Mapped[float] = mapped_column(Float, default=0)
    max_drawdown: Mapped[float] = mapped_column(Float, default=0)
    sharpe: Mapped[float] = mapped_column(Float, default=0)
    calmar: Mapped[float] = mapped_column(Float, default=0)
    win_rate: Mapped[float] = mapped_column(Float, default=0)
    metrics_json: Mapped[dict] = mapped_column(JSON, default=dict)   # 全部19+指标
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


# ================================================================
# 八、Prompt/Skill/审计/报告
# ================================================================

class PromptVersion(Base):
    """Prompt版本表"""
    __tablename__ = "prompt_versions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    prompt_id: Mapped[str] = mapped_column(String(32), unique=True)
    agent_name: Mapped[str] = mapped_column(String(48), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[Text] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12), default="active")  # active/draft/archived
    description: Mapped[str] = mapped_column(Text, default="")
    test_result: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(32), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ToolPermission(Base):
    """Agent工具权限表 (Skill/Tool 权限控制)"""
    __tablename__ = "tool_permissions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent_name: Mapped[str] = mapped_column(String(48), index=True)
    tool_name: Mapped[str] = mapped_column(String(48))
    permission: Mapped[str] = mapped_column(String(8), default="allow")  # allow/deny
    status: Mapped[str] = mapped_column(String(8), default="active")
    note: Mapped[str] = mapped_column(Text, default="")


class AuditLog(Base):
    """审计日志表 (全链路留痕)"""
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_trace", "trace_id"), Index("ix_audit_time", "created_at"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    log_id: Mapped[str] = mapped_column(String(32), unique=True)
    trace_id: Mapped[str] = mapped_column(String(32), default="")
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    actor: Mapped[str] = mapped_column(String(32), default="system")
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class SystemLog(Base):
    """系统日志表"""
    __tablename__ = "system_logs"
    __table_args__ = (Index("ix_syslog_time", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    log_id: Mapped[str] = mapped_column(String(32), unique=True)
    level: Mapped[str] = mapped_column(String(8), default="INFO")
    module: Mapped[str] = mapped_column(String(32), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ReportRecord(Base):
    """报告表"""
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    report_id: Mapped[str] = mapped_column(String(32), unique=True)
    report_type: Mapped[str] = mapped_column(String(16), index=True)  # daily/weekly/monthly/backtest/plan
    title: Mapped[str] = mapped_column(String(128), default="")
    file_path: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


# ================================================================
# 九、RAG 向量表 (pgvector)
# ================================================================

class RagDocument(Base):
    """RAG 文档表 (新闻/公告/历史报告/策略文档)"""
    __tablename__ = "rag_documents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(String(32), unique=True)
    doc_type: Mapped[str] = mapped_column(String(16), default="news")   # news/announcement/report/strategy
    source: Mapped[str] = mapped_column(String(32), default="")
    title: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[Text] = mapped_column(Text, default="")
    symbol: Mapped[str] = mapped_column(String(12), default="")
    publish_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class RagChunk(Base):
    """RAG 分块表 (向量列 embedding, 由 pgvector 索引)"""
    __tablename__ = "rag_chunks"
    __table_args__ = (Index("ix_chunk_doc", "document_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chunk_id: Mapped[str] = mapped_column(String(32), unique=True)
    document_id: Mapped[str] = mapped_column(String(32), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[Text] = mapped_column(Text, default="")
    embedding: Mapped[list] = mapped_column(Vector(1024))     # pgvector 向量列
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


# ================================================================
# 十、长期记忆
# ================================================================

class MemoryRecord(Base):
    """Agent 长期记忆表 (历史交易教训/标的风险事件/Agent准确率)"""
    __tablename__ = "memory_records"
    __table_args__ = (Index("ix_mem_agent", "agent_name"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    memory_id: Mapped[str] = mapped_column(String(32), unique=True)
    agent_name: Mapped[str] = mapped_column(String(48), default="")
    symbol: Mapped[str] = mapped_column(String(12), default="")
    category: Mapped[str] = mapped_column(String(16), default="lesson")  # lesson/risk/accuracy/rule
    content: Mapped[Text] = mapped_column(Text, default="")
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class NotificationRecord(Base):
    """邮件通知记录表 (去重/审计)"""
    __tablename__ = "notification_records"
    __table_args__ = (Index("ix_notif_dedup", "dedup_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dedup_key: Mapped[str] = mapped_column(String(64), default="")
    subject: Mapped[str] = mapped_column(String(256), default="")
    status: Mapped[str] = mapped_column(String(8), default="SENT")  # SENT/FAILED/SKIPPED
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

