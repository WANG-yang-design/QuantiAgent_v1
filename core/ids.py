# -*- coding: utf-8 -*-
"""
ID 生成器: 全链路订单/决策/风控/回测的幂等 ID
=============================================
交易系统的防重复下单依赖唯一 ID:
  decision_id → risk_check_id → order_intent_id → order_id → trade_id
同一 order_intent_id 只能提交一次(幂等), 即使 API 超时也不能重复下单。
"""
import uuid
from datetime import datetime


def _prefix(kind: str) -> str:
    return f"{kind}{datetime.now().strftime('%y%m%d%H%M%S')}"


def gen_id(kind: str = "id") -> str:
    """生成唯一 ID: 前缀 + 时间戳 + uuid 短尾。
    修复: 尾部由 8 hex 位(32bit)提升到 12 hex 位(48bit), 降低高频并发下单的碰撞率。"""
    return f"{_prefix(kind)}_{uuid.uuid4().hex[:12].upper()}"


def gen_trace_id() -> str:
    """一次工作流共享的追踪 ID。"""
    return gen_id("TR")

def gen_run_id() -> str:
    """Agent 运行记录 ID。"""
    return gen_id("RUN")

def gen_decision_id() -> str:
    """投研决策 ID。"""
    return gen_id("DEC")

def gen_plan_id() -> str:
    """交易计划 ID。"""
    return gen_id("PLAN")

def gen_risk_check_id() -> str:
    """风控审核 ID。"""
    return gen_id("RISK")

def gen_order_intent_id() -> str:
    """订单意图 ID (幂等键, 同一意图只能提交一次)。"""
    return gen_id("INTENT")

def gen_order_id() -> str:
    """订单 ID。"""
    return gen_id("ORD")

def gen_trade_id() -> str:
    """成交 ID。"""
    return gen_id("TRADE")

def gen_backtest_id() -> str:
    """回测运行 ID。"""
    return gen_id("BT")

def gen_report_id() -> str:
    """报告 ID。"""
    return gen_id("RPT")
