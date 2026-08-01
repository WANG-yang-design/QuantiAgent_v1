# -*- coding: utf-8 -*-
"""
Prompt / Skill 管理
====================
- Prompt 从 config/prompts/*.yaml 加载, 支持版本化登记到 prompt_versions 表
- 提供 get_prompt(agent_name) 供各 Agent 使用
- Skill 权限: 从 tool_permissions 表读取, 基类 Agent 执行工具调用前校验
"""
from typing import Dict

import yaml

from core.config import CONFIG_DIR, get_settings


class PromptManager:
    """提示词管理器: 加载 config/prompts 目录下所有 yaml。"""

    def __init__(self):
        self._prompts: Dict[str, str] = {}
        self._load_all()

    def _load_all(self):
        prompt_dir = CONFIG_DIR / "prompts"
        if not prompt_dir.exists():
            return
        for f in sorted(prompt_dir.glob("*.yaml")):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
                for agent_name, content in data.items():
                    if isinstance(content, str) and content.strip():
                        self._prompts[agent_name] = content.strip()
            except Exception as e:
                print(f"[prompt] 加载失败 {f}: {e}")

    def get(self, agent_name: str) -> str:
        return self._prompts.get(agent_name, "")

    def all_prompts(self) -> Dict[str, str]:
        return dict(self._prompts)


_pm: PromptManager | None = None


def get_prompt_manager() -> PromptManager:
    global _pm
    if _pm is None:
        _pm = PromptManager()
    return _pm
