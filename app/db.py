"""
Async SQLAlchemy engine + session factory, plus one-time schema bootstrap.

Single source of truth = PostgreSQL (relational table + pgvector columns),
so "go back to the main table" is a real SQL JOIN, not a cross-store id round-trip.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    pass 


engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncSession:
    """FastAPI dependency: one session per request."""
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    """
    Enable pgvector and create all tables + indexes if missing.
    Idempotent — safe to call on every startup.
    """
    # Import models so they register on Base.metadata before create_all.
    from app import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        # 0.2.2 升级:旧库的 memory_kind 枚举补 life_event 值(幂等;
        # 新库 create_all 建类型时已带全值,这里是 no-op)。PG12+ 允许事务内 ADD VALUE。
        await conn.execute(
            text("ALTER TYPE memory_kind ADD VALUE IF NOT EXISTS 'life_event'")
        )
        # 旧库补 chats.core_identity 列(create_all 不改已有表)
        await conn.execute(
            text("ALTER TABLE chats ADD COLUMN IF NOT EXISTS core_identity TEXT")
        )
        # 0.3.0 升级:夜间代理的"每晚一次"闸
        await conn.execute(
            text("ALTER TABLE chats ADD COLUMN IF NOT EXISTS "
                 "last_night_run_ms BIGINT NOT NULL DEFAULT 0")
        )
        # 0.6.0 升级:承诺兑现闭环的"到点催" ping
        await conn.execute(
            text("ALTER TABLE timer_pings ADD COLUMN IF NOT EXISTS "
                 "kind VARCHAR(16) NOT NULL DEFAULT 'timer'")
        )
        await conn.execute(
            text("ALTER TABLE timer_pings ADD COLUMN IF NOT EXISTS "
                 "loop_id VARCHAR(16)")
        )
