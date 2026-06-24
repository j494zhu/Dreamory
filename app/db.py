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
