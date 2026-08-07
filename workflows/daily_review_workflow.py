# -*- coding: utf-8 -*-
"""
日终复盘工作流
==============
收盘行情更新 → 账户/持仓市值 → 当日盈亏 → 订单成交汇总 → 计划对比执行 →
Agent复盘 → 日报生成 → 邮件发送
"""
import asyncio
import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from agents.base_agent import AgentInput
from agents.execution_agents import ReviewAgent
from core.logging import get_logger
from database import repository as repo
from memory.audit_log import AuditLogger
from notification.notification_service import get_notification_service
from paper_trading.paper_broker import PaperBroker
from reports.report_generator import get_report_generator
from workflows.graph import WorkflowState

logger = get_logger("workflow.review")

_REVIEW = ReviewAgent()


async def run_daily_review(broker: Optional[PaperBroker] = None,
                           date_: Optional[date] = None,
                           send_email: bool = True) -> Dict[str, Any]:
    """日终复盘: 返回复盘结果(含日报文件路径)。"""
    from workflows.intraday_monitor_workflow import get_broker
    broker = broker or get_broker()
    date_ = date_ or date.today()
    audit = AuditLogger.instance()
    audit.log("daily_review_start", "workflow", {"date": str(date_)})

    # 1. 刷新持仓市值(收盘价由数据服务最新行情注入)
    account = broker.get_account()

    # 2. 统计当日订单与成交(只统计本账户, 排除测试/其他账户的成交)
    orders_today = repo.get_orders_today(date_)
    trades = repo.get_trades(start=datetime.combine(date_, datetime.min.time()),
                             end=datetime.combine(date_, datetime.max.time()),
                             account_id=broker.account_id)
    fee_total = sum((t.fee or 0) for t in trades)   # 修复: 历史 NULL fee 导致 TypeError 复盘崩溃
    day_pnl = float(account.get("day_pnl", 0) or 0)

    stats = {
        "date": str(date_),
        "day_pnl": day_pnl,
        "total_asset": account.get("total_asset"),
        "trade_count": len(trades),
        "order_count": len(orders_today),
        "fee_total": fee_total,
        "positions": account.get("positions"),
    }

    # 3. 复盘 Agent
    review = await _REVIEW.run(AgentInput(symbol="", context={"day_stats": stats}))

    # 4. 生成日报
    report_path = get_report_generator().generate_daily_report(stats, review, date_)

    # 5. 邮件发送
    if send_email:
        try:
            get_notification_service().send_daily_report_email(stats, review, report_path)
        except Exception as exc:
            logger.warning("日报邮件发送失败: %s", exc)

    # 6. 账户快照
    broker.snapshot()

    audit.log("daily_review_end", "workflow", {"date": str(date_), "report": report_path})
    logger.info("日终复盘完成 %s, 报告: %s", date_, report_path)
    return {"stats": stats, "review": review, "report_path": report_path}
