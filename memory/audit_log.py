# -*- coding: utf-8 -*-
"""
审计日志 / 短期记忆 / 长期记忆
==============================
- 审计: 全链路事件落库(audit_logs), 每笔交易可追溯
- 短期记忆: 当前工作流上下文(内存)
- 长期记忆: 历史教训/标的风险事件/Agent准确率(数据库)
"""
import logging
from datetime import date, datetime
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.ids import gen_id
from core.logging import get_trace_id
from database import repository as repo

logger = logging.getLogger("memory.audit")


class AuditLogger:
    """审计日志器(单例)。"""

    _inst: Optional["AuditLogger"] = None

    @classmethod
    def instance(cls) -> "AuditLogger":
        if cls._inst is None:
            cls._inst = AuditLogger()
        return cls._inst

    def log(self, event_type: str, actor: str, payload: Optional[Dict[str, Any]] = None,
            trace_id: Optional[str] = None):
        """记录审计事件。payload 自动做 JSON 安全转换。"""
        try:
            repo.save_audit_log(
                trace_id=trace_id or get_trace_id() or "",
                event_type=event_type,
                actor=actor,
                payload=self._json_safe(payload or {}),
            )
        except Exception as exc:
            logger.error("审计落库失败 %s: %s", event_type, exc)

    @staticmethod
    def _json_safe(obj: Any) -> Any:
        """递归把 datetime/date 等转为字符串(JSON 可序列化)。"""
        if isinstance(obj, dict):
            return {k: AuditLogger._json_safe(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [AuditLogger._json_safe(v) for v in obj]
        if isinstance(obj, (datetime, date)):
            return str(obj)
        return obj

    def query(self, trace_id: Optional[str] = None, event_type: Optional[str] = None,
              limit: int = 500) -> List[Dict[str, Any]]:
        rows = repo.get_audit_logs(trace_id, event_type, limit)
        return [
            {"log_id": r.log_id, "trace_id": r.trace_id, "event_type": r.event_type,
             "actor": r.actor, "payload": r.payload_json,
             "created_at": str(r.created_at)}
            for r in rows
        ]


class ShortTermMemory:
    """短期记忆: 一次工作流内的共享上下文(线程安全)。"""

    def __init__(self):
        self._data: Dict[str, Any] = {}

    def set(self, key: str, value: Any):
        self._data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def snapshot(self) -> Dict[str, Any]:
        return dict(self._data)

    def clear(self):
        self._data.clear()


class LongTermMemory:
    """长期记忆: 持久化到 memory_records 表。"""

    def remember(self, agent_name: str, content: str, category: str = "lesson",
                 symbol: str = "", extra: Optional[Dict[str, Any]] = None):
        repo.save_memory({
            "agent_name": agent_name, "symbol": symbol, "category": category,
            "content": content, "extra": extra or {},
        })

    def recall(self, agent_name: Optional[str] = None, symbol: Optional[str] = None,
               category: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        rows = repo.get_memories(agent_name, symbol, category, limit)
        return [
            {"memory_id": r.memory_id, "agent_name": r.agent_name, "symbol": r.symbol,
             "category": r.category, "content": r.content, "extra": r.extra,
             "created_at": str(r.created_at)}
            for r in rows
        ]


_short = ShortTermMemory()
_long = LongTermMemory()


def get_short_memory() -> ShortTermMemory:
    return _short


def get_long_memory() -> LongTermMemory:
    return _long

