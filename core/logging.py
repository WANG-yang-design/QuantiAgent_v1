# -*- coding: utf-8 -*-
"""
统一日志系统
============
- 控制台: 带颜色、简洁
- 文件: 按天滚动, 分模块子目录 (system/data/agent/risk/order/trade/error)
- 全链路 trace_id 贯穿: 一次 Agent 工作流共用一个 trace_id
"""
import logging
import logging.handlers
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.config import ROOT_DIR, ensure_dirs

# 模块 -> 日志子目录
_MODULE_DIR = {
    "data": "data",      # 数据采集/质量
    "agent": "agent",    # Agent 调用
    "risk": "risk",      # 风控
    "order": "order",    # 订单执行
    "audit": "audit",    # 审计(独立保留)
}

_FORMAT = "%(asctime)s [%(levelname)s] [%(trace_id)s] %(name)s: %(message)s"


import contextvars

# 修复: 原 threading.local 在 asyncio 并发任务间互踩(run_pool_scan 3-5路并发
# 共享同一线程, set_trace_id 相互覆盖, 审计日志 trace_id 张冠李戴)。
# contextvars 在每个任务/协程有独立上下文, 并发的 Agent 调用不再互相污染。
_trace_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "trace_id", default=None)


class TraceFilter(logging.Filter):
    """把当前上下文的 trace_id 注入日志记录。"""

    def filter(self, record):
        record.trace_id = (get_trace_id() or "-")[:16]
        return True


_trace = TraceFilter()


def set_trace_id(tid: str):
    """设置当前上下文的 trace_id (工作流入口调用, 子线程需自行调用)。"""
    _trace_var.set(tid)


def get_trace_id() -> Optional[str]:
    return _trace_var.get()


def _attach_trace(handler: logging.Handler):
    """给 handler 附加 trace_id 过滤器(必须加到 handler 上, 加 logger 无效)。"""
    if not any(isinstance(f, TraceFilter) for f in handler.filters):
        handler.addFilter(_trace)


def setup_logging(level: str = "INFO", log_dir: Optional[str] = None):
    """初始化根日志器: 控制台 + 文件(按天滚动)。"""
    ensure_dirs()
    log_root = Path(log_dir) if log_dir else (ROOT_DIR / "logs")
    log_root.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    # 控制台
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    _attach_trace(console)
    root.addHandler(console)

    # 主文件 (按天滚动)
    main = logging.handlers.TimedRotatingFileHandler(
        log_root / "system.log", when="midnight", backupCount=30, encoding="utf-8")
    main.setFormatter(logging.Formatter(_FORMAT))
    _attach_trace(main)
    root.addHandler(main)

    # 错误文件
    err = logging.handlers.TimedRotatingFileHandler(
        log_root / "error.log", when="midnight", backupCount=90, encoding="utf-8")
    err.setLevel(logging.ERROR)
    err.setFormatter(logging.Formatter(_FORMAT))
    _attach_trace(err)
    root.addHandler(err)


def get_logger(name: str) -> logging.Logger:
    """获取带子文件输出的 logger:
       get_logger("data.collector") → logs/data/collector.log
       get_logger("agent.technical") → logs/agent/technical.log
    """
    logger = logging.getLogger(name)
    first = name.split(".")[0]
    if first in _MODULE_DIR:
        sub = _MODULE_DIR[first]
        d = ROOT_DIR / "logs" / sub
        d.mkdir(parents=True, exist_ok=True)
        h = logging.handlers.TimedRotatingFileHandler(
            d / (name.split(".")[-1] + ".log"),
            when="midnight", backupCount=30, encoding="utf-8")
        h.setFormatter(logging.Formatter(_FORMAT))
        _attach_trace(h)
        # 避免重复添加
        if not any(isinstance(x, logging.handlers.TimedRotatingFileHandler) and
                   getattr(x, "baseFilename", "") == h.baseFilename for x in logger.handlers):
            logger.addHandler(h)
    return logger


def audit_event(event_type: str, actor: str, payload: dict):
    """审计日志(写入日志 + 由 memory.audit_log 落库)。"""
    from memory.audit_log import AuditLogger
    AuditLogger.instance().log(event_type, actor, payload)


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
