# -*- coding: utf-8 -*-
"""
Agent 启用开关 (成本控制)
==========================
总开关关闭时，自动/手动 Agent 扫描均应在工作流入口直接跳过；逐 Agent
开关只在总开关开启时生效。运行时覆盖写入 data/agent_switches.json，
未覆盖时使用 config.yaml 的 agents.master_enabled / agents.enabled。
必须启用(禁关): data_admin / chief_researcher / trader / risk_manager / compliance
"""
import json
import os
import threading
import time
from typing import Optional

from core.config import ROOT_DIR, get_settings

# 必须启用的 Agent(数据闸门/结论/执行链, 关闭会导致链路不可用)
REQUIRED_AGENTS = {"data_admin", "chief_researcher"}

# 默认启用(未配置时)
DEFAULT_ENABLED = {
    "technical_analyst", "etf_analyst", "fundamental_analyst", "news_analyst",
    "sentiment_analyst", "money_flow_analyst", "macro_analyst",
    "bull_researcher", "bear_researcher",
}

AGENT_LABELS = {
    "technical_analyst": "技术分析师",
    "etf_analyst": "ETF专项分析师",
    "fundamental_analyst": "基本面分析师",
    "news_analyst": "新闻公告分析师",
    "sentiment_analyst": "情绪分析师",
    "money_flow_analyst": "资金流分析师",
    "macro_analyst": "宏观分析师",
    "bull_researcher": "看多研究员",
    "bear_researcher": "看空研究员",
    "data_admin": "数据闸门",
    "chief_researcher": "首席研究员",
    "trader": "交易员",
    "risk_manager": "风控",
    "compliance": "合规",
    "execution_supervisor": "执行监督",
    "review": "复盘总结",
}

_SWITCH_FILE = ROOT_DIR / "data" / "agent_switches.json"
_MASTER_KEY = "_master_enabled"
_override_cache: dict = {}
_override_ts: float = 0.0
_switch_lock = threading.RLock()


def _overrides() -> dict:
    """读取 Web 设置页写入的开关覆盖(带5秒缓存)。"""
    global _override_cache, _override_ts
    now = time.time()
    if now - _override_ts < 5.0:
        return _override_cache
    _override_ts = now
    try:
        if _SWITCH_FILE.exists():
            _override_cache = json.loads(
                _SWITCH_FILE.read_text(encoding="utf-8")) or {}
        else:
            _override_cache = {}
    except Exception:
        _override_cache = {}
    return _override_cache


def agent_enabled(agent_name: str) -> bool:
    """该 Agent 是否启用 LLM。"""
    if not agent_system_enabled():
        return False
    if agent_name in REQUIRED_AGENTS:
        return True
    over = _overrides()
    if agent_name in over:
        return bool(over.get(agent_name))
    cfg = (get_settings().get("agents") or {}).get("enabled") or {}
    return bool(cfg.get(agent_name, agent_name in DEFAULT_ENABLED))


def agent_system_enabled() -> bool:
    """Agent 系统总开关；关闭后任何 Agent 都不应调用 LLM。"""
    over = _overrides()
    if _MASTER_KEY in over:
        return bool(over[_MASTER_KEY])
    return bool(get_settings().get("agents.master_enabled", True))


def enabled_agent_names(names: Optional[list] = None) -> list:
    """过滤出启用的 Agent 列表。"""
    names = names or list(DEFAULT_ENABLED)
    return [n for n in names if agent_enabled(n)]


def set_agent_enabled(agent_name: str, enabled: bool) -> bool:
    """写入开关覆盖(Web设置页调用, 运行时生效, 无需重启)。"""
    if agent_name in REQUIRED_AGENTS:
        return False
    global _override_cache, _override_ts
    with _switch_lock:
        # 每次写前重读，避免多进程各自持有 5 秒旧缓存后互相覆盖。
        try:
            over = json.loads(_SWITCH_FILE.read_text(encoding="utf-8")) \
                if _SWITCH_FILE.exists() else {}
        except Exception:
            over = {}
        over[agent_name] = bool(enabled)
        _SWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SWITCH_FILE.with_suffix(
            _SWITCH_FILE.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(over, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, _SWITCH_FILE)
        _override_cache = over
        _override_ts = time.time()
    return True


def set_agent_system_enabled(enabled: bool) -> bool:
    """设置 Agent 总开关，保留原有逐 Agent 配置以便重新开启时恢复。"""
    global _override_cache, _override_ts
    with _switch_lock:
        try:
            over = json.loads(_SWITCH_FILE.read_text(encoding="utf-8")) \
                if _SWITCH_FILE.exists() else {}
        except Exception:
            over = {}
        over[_MASTER_KEY] = bool(enabled)
        _SWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SWITCH_FILE.with_suffix(
            _SWITCH_FILE.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(over, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, _SWITCH_FILE)
        _override_cache = over
        _override_ts = time.time()
    return True


def all_agent_states() -> list:
    """全部 Agent 开关状态(供设置页展示, 含必须启用的)。"""
    over = _overrides()
    cfg = (get_settings().get("agents") or {}).get("enabled") or {}
    master_enabled = agent_system_enabled()
    names = set(DEFAULT_ENABLED) | set(cfg.keys()) | set(REQUIRED_AGENTS)
    names |= {name for name in over if name != _MASTER_KEY}
    out = []
    for name in sorted(names, key=lambda n: (n in REQUIRED_AGENTS, n)):
        if name in REQUIRED_AGENTS:
            enabled = True
        elif name in over:
            enabled = bool(over[name])
        else:
            enabled = bool(cfg.get(name, name in DEFAULT_ENABLED))
        out.append({
            "agent": name,
            "label": AGENT_LABELS.get(name, name),
            "enabled": enabled,
            "effective_enabled": master_enabled and enabled,
            "required": name in REQUIRED_AGENTS,
        })
    return out
