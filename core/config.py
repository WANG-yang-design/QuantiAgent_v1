# -*- coding: utf-8 -*-
"""
统一配置加载器
================
加载顺序: 环境变量(.env) > config/*.yaml
所有模块通过 `get_settings()` 获取全局单例配置。
"""
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

import yaml
from dotenv import load_dotenv

# 修复: 本机系统代理不可用(ProxyError)导致 akshare(内部 requests) 数据获取
# 几乎全部失败 —— 本应用的所有数据接口均需直连, 全局禁用代理。
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

# 项目根目录 (config/ 的上一级)
ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT_DIR / "config"

# 加载 .env (存在则覆盖系统环境)
_env_file = ROOT_DIR / ".env"
if _env_file.exists():
    load_dotenv(_env_file, override=True)


class Settings:
    """全局配置对象: 聚合 config.yaml 及各分类 yaml, 并叠加 .env 中的密钥。"""

    def __init__(self):
        self.data: Dict[str, Any] = {}
        self._load_yaml("config.yaml", "config")
        # 修复(重要): config.yaml 内容被整体挂在 data["config"] 下,
        # 而全站大量使用 get_settings().get("web.xxx")/("strategies.xxx")
        # 顶层点分路径读取 —— 全部返回默认值, 用户修改 config.yaml 永不生效
        # (轮动参数/风控阈值/监控开关等都静默失效)。把 config.yaml 的各顶层
        # 节平铺到 data, 同时保留 "config" 键兼容旧代码。
        for _k, _v in (self.data.get("config") or {}).items():
            if isinstance(_v, dict) and _k not in self.data:
                self.data[_k] = _v
            elif not isinstance(_v, dict):
                self.data.setdefault(_k, _v)
        self._load_yaml("risk_limits.yaml", "risk")
        self._load_yaml("data_sources.yaml", "data_sources")
        self._load_yaml("model_routes.yaml", "model_routes")
        self._load_yaml("agent_schedule.yaml", "agent_schedule")
        self._load_yaml("trading_rules.yaml", "trading_rules")
        self._apply_env()

    # ---------------------------------------------------------------
    def _load_yaml(self, filename: str, key: str):
        path = CONFIG_DIR / filename
        if not path.exists():
            print(f"[config] 警告: 缺少配置文件 {path}")
            self.data[key] = {}
            return
        with open(path, "r", encoding="utf-8") as f:
            self.data[key] = yaml.safe_load(f) or {}

    # ---------------------------------------------------------------
    def _apply_env(self):
        """密钥类配置从 .env 注入, 覆盖 yaml 中的占位值(env 优先)。"""
        env_map = {
            "LLM_BASE_URL": ("llm", "base_url"),
            "LLM_API_KEY": ("llm", "api_key"),
            "LLM_FAST_MODEL": ("llm", "fast_model"),
            "LLM_DEEP_MODEL": ("llm", "deep_model"),
            "LLM_EMBEDDING_MODEL": ("llm", "embedding_model"),
            "DB_HOST": ("database", "host"),
            "DB_PORT": ("database", "port"),
            "DB_NAME": ("database", "name"),
            "DB_USER": ("database", "user"),
            "DB_PASSWORD": ("database", "password"),
            "EMAIL_ENABLED": ("email", "enabled"),
            "EMAIL_SMTP_HOST": ("email", "smtp_host"),
            "EMAIL_SMTP_PORT": ("email", "smtp_port"),
            "EMAIL_SENDER": ("email", "sender"),
            "EMAIL_SENDER_PASS": ("email", "sender_pass"),
            "EMAIL_RECEIVER": ("email", "receiver"),
            "TUSHARE_TOKEN": ("tushare", "token"),
        }
        for env_key, (section, field) in env_map.items():
            val = os.getenv(env_key, "")
            if val:
                self.data.setdefault(section, {})[field] = val
        extras = os.getenv("EMAIL_EXTRA_RECEIVERS", "")
        if extras:
            self.data.setdefault("email", {})["extra_receivers"] = [
                x.strip() for x in extras.split(",") if x.strip()
            ]

    # ---------------------------------------------------------------
    def section(self, name: str) -> Dict[str, Any]:
        return self.data.get(name, {})

    def get(self, path: str, default: Any = None) -> Any:
        """按点分路径取值: settings.get('llm.fast_model')"""
        node: Any = self.data
        for part in path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def database_url(self) -> str:
        """拼接 PostgreSQL 连接串 (SQLAlchemy + psycopg3)。
        修复: 原实现手工拼接不 URL 编码, 密码含 @ : / % 等特殊字符即连接失败。"""
        cfg = self.data.get("database", {})
        if cfg.get("url"):
            return cfg["url"]
        from urllib.parse import quote
        host = os.getenv("DB_HOST", "localhost")
        port = os.getenv("DB_PORT", "5432")
        name = os.getenv("DB_NAME", "quantiagent")
        user = os.getenv("DB_USER", "quantiagent")
        pwd = os.getenv("DB_PASSWORD", "quantiagent")
        return (f"postgresql+psycopg://{quote(user, safe='')}:"
                f"{quote(pwd, safe='')}@{host}:{port}/{name}")

    def llm_configured(self) -> bool:
        """是否已配置真实 LLM(有 base_url + api_key)。"""
        llm = self.data.get("llm", {})
        return bool(llm.get("base_url") and llm.get("api_key"))

    def mock_mode(self) -> bool:
        """是否强制/自动进入规则模拟模式。"""
        mode = self.data.get("llm", {}).get("mock_mode", "auto")
        if mode == "true":
            return True
        if mode == "false":
            return False
        return not self.llm_configured()  # auto


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def ensure_dirs():
    """确保运行目录存在。"""
    for d in ["logs", "reports", "data", "data/charts", "data/embeddings"]:
        p = ROOT_DIR / d
        p.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    s = get_settings()
    print("config dir:", CONFIG_DIR)
    print("database url:", s.database_url().replace(s.data.get("database", {}).get("password", "x"), "***"))
    print("llm configured:", s.llm_configured(), "mock:", s.mock_mode())
