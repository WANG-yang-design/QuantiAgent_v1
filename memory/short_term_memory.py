# -*- coding: utf-8 -*-
"""
短期记忆 / 长期记忆 (facade)
============================
实现位于 memory.audit_log(ShortTermMemory/LongTermMemory), 此处保持目录结构。
"""
from memory.audit_log import (
    LongTermMemory, ShortTermMemory, get_long_memory, get_short_memory,
)

__all__ = ["ShortTermMemory", "LongTermMemory", "get_short_memory", "get_long_memory"]
