# -*- coding: utf-8 -*-
"""
数据库会话管理
==============
SQLAlchemy 引擎 + 会话工厂 + 依赖注入工具。
所有仓库层通过 get_session() 获取会话。
"""
from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from core.config import get_settings


@lru_cache(maxsize=1)
def get_engine():
    settings = get_settings()
    cfg = settings.section("database")
    return create_engine(
        settings.database_url(),
        echo=bool(cfg.get("echo", False)),
        pool_size=int(cfg.get("pool_size", 10)),
        max_overflow=int(cfg.get("max_overflow", 20)),
        pool_pre_ping=True,          # 连接失效自动重连(数据库重启/网络抖动)
    )


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


@contextmanager
def get_session() -> Iterator[Session]:
    """上下文会话: with get_session() as s: ..."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def db_health() -> bool:
    """数据库健康检查(风控熔断依赖此服务)。"""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
