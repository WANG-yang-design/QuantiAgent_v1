# -*- coding: utf-8 -*-
"""
数据库初始化
============
1. 建表 (全部表结构)
2. 初始化默认数据 (模拟账户、Prompt版本、工具权限)
3. pgvector HNSW 索引(向量检索)
用法: python -m database.init_db
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from core.config import get_settings
from core.logging import get_logger, setup_logging
from database.db_session import get_engine, get_session
from database.models import (
    Account, Base, PromptVersion, RagChunk, Symbol, ToolPermission,
)
from core.ids import gen_id

setup_logging()
logger = get_logger("database.init")


def _ensure_columns():
    """轻量迁移: 为已存在的表补充新增列(Postgres ADD COLUMN IF NOT EXISTS)。
    create_all 不会改动已有表, 新功能依赖的新列在此补齐, 幂等可重复执行。"""
    statements = [
        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS name VARCHAR(64) DEFAULT ''",
        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS pnl FLOAT",
        # 修复: 订单幂等键超长(INTENT-DEC...+重报后缀超过32字符)导致下单失败
        "ALTER TABLE orders ALTER COLUMN order_intent_id TYPE VARCHAR(64)",
        # LLM 真实用量统计(输出token是成本大头)
        "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS prompt_tokens INTEGER DEFAULT 0",
        "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS completion_tokens INTEGER DEFAULT 0",
    ]
    with get_engine().connect() as conn:
        for stmt in statements:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception as e:
                logger.warning("列补齐失败(%s): %s", stmt[:60], e)


def init_db(seed: bool = True):
    """建表 + 可选种子数据。"""
    engine = get_engine()
    # 1. 确保 vector 扩展可用 (已由 DBA 预建, 这里兜底)
    #    修复: 托管 PG(RDS等)普通用户无 CREATE EXTENSION 权限,
    #    原实现直接抛异常中断初始化 —— 改为告警降级(向量功能不可用)
    try:
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
    except Exception as e:
        logger.warning("pgvector 扩展创建失败(向量检索将不可用): %s", e)

    # 2. 建表
    Base.metadata.create_all(engine)
    logger.info("数据库表结构创建完成")

    # 2.5 轻量迁移(新增列)
    _ensure_columns()

    # 3. HNSW 向量索引 (pgvector 余弦距离)
    try:
        with engine.connect() as conn:
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_rag_chunks_embedding "
                "ON rag_chunks USING hnsw (embedding vector_cosine_ops)"
            ))
            conn.commit()
        logger.info("pgvector HNSW 索引已创建")
    except Exception as e:
        logger.warning("HNSW 索引创建失败(不影响使用): %s", e)

    if seed:
        seed_data()


def seed_data():
    """初始化: 模拟账户 / 默认 Prompt / 工具权限 / 沪深ETF池占位。"""
    with get_session() as s:
        # 模拟账户
        cfg = get_settings().section("paper_account")
        acc_id = cfg.get("account_id", "PA-001")
        cash = float(cfg.get("initial_cash", 100000))
        if s.get(Account, acc_id) is None:
            s.add(Account(
                account_id=acc_id,
                account_type="paper",
                cash=cash,
                frozen_cash=0,
                market_value=0,
                total_asset=cash,
                init_cash=cash,
            ))
            logger.info("模拟账户 %s 初始化: %.0f 元", acc_id, cash)

        # 默认工具权限(硬规则: 分析师不能下单, 交易员/风控才有对应工具)
        perms = [
            # (agent, tool, allow)
            ("data_admin", "get_data_quality", "allow"),
            ("data_admin", "block_workflow", "allow"),
            ("technical_analyst", "get_technical_indicators", "allow"),
            ("technical_analyst", "get_kline", "allow"),
            ("etf_analyst", "get_etf_features", "allow"),
            ("fundamental_analyst", "get_fundamentals", "allow"),
            ("news_analyst", "get_news", "allow"),
            ("news_analyst", "get_announcements", "allow"),
            ("news_analyst", "search_rag", "allow"),
            ("sentiment_analyst", "get_sentiment", "allow"),
            ("money_flow_analyst", "get_money_flow", "allow"),
            ("macro_analyst", "get_market_state", "allow"),
            ("bull_researcher", "get_all_analyses", "allow"),
            ("bear_researcher", "get_all_analyses", "allow"),
            ("chief_researcher", "get_all_analyses", "allow"),
            ("trader", "get_account", "allow"),
            ("trader", "get_positions", "allow"),
            ("trader", "generate_order_plan", "allow"),
            ("risk_manager", "get_risk_limits", "allow"),
            ("risk_manager", "check_order_risk", "allow"),
            ("risk_manager", "check_portfolio_risk", "allow"),
            ("compliance", "check_compliance", "allow"),
            ("execution_supervisor", "query_order", "allow"),
            ("execution_supervisor", "cancel_order", "allow"),
            ("review", "get_trade_history", "allow"),
            # 敏感工具: 一律 deny (防止分析师越权下单)
            ("technical_analyst", "place_order", "deny"),
            ("news_analyst", "place_order", "deny"),
            ("sentiment_analyst", "place_order", "deny"),
            ("fundamental_analyst", "place_order", "deny"),
            ("etf_analyst", "place_order", "deny"),
            ("money_flow_analyst", "place_order", "deny"),
            ("macro_analyst", "place_order", "deny"),
        ]
        existing = {p.agent_name + ":" + p.tool_name
                    for p in s.query(ToolPermission).all()}
        for agent, tool, perm in perms:
            if agent + ":" + tool not in existing:
                s.add(ToolPermission(agent_name=agent, tool_name=tool,
                                     permission=perm, note="V1默认权限"))

        # 默认 Prompt 占位(内容由 config/prompts/*.yaml 提供, 此处登记版本)
        from core.prompt_manager import PromptManager
        pm = PromptManager()
        registered = {p.agent_name for p in s.query(PromptVersion).all()}
        for agent_name, content in pm.all_prompts().items():
            if agent_name not in registered:
                s.add(PromptVersion(
                    prompt_id=gen_id("PRM"),
                    agent_name=agent_name,
                    version=1,
                    content=content,
                    status="active",
                    description="V1初始Prompt",
                    test_result="",
                ))
        logger.info("种子数据初始化完成")
    logger.info("数据库初始化完毕")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
