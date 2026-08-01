# -*- coding: utf-8 -*-
"""
长期记忆 (facade)
=================
实现位于 memory.audit_log.LongTermMemory(落库 memory_records 表)。
"""
from memory.audit_log import LongTermMemory, get_long_memory

__all__ = ["LongTermMemory", "get_long_memory"]
