"""
跨进程单飞 — Postgres advisory lock 封装。

背景:后台任务(auto-dream / life_sim / night_agent / evolution)原本用进程内
asyncio.Lock 单飞,单进程没问题;多 worker / 多进程部署时锁互相看不见,
任务会并发重复跑(重复烧 LLM 调用、重复演化)。本模块把单飞语义下沉到
Postgres:同一把 (space, key) 锁全库唯一,谁先拿到谁跑,拿不到就让过。

用法:
    async with advisory_guard(LOCK_NIGHT, chat_key(chat_id)) as acquired:
        if not acquired:
            return          # 别的进程正在跑,让过(不等待)
        ...                 # 干活,期间可自由开/提交任意事务

实现要点:
  - 用 *会话级* pg_try_advisory_lock(非事务级):任务中途会 commit 多次,
    事务级锁在第一次 commit 就没了。
  - 锁挂在一条 **专用 AUTOCOMMIT 连接** 上,与任务自己的 session 完全分离:
    不占事务、不受任务提交影响;进程崩溃连接断开时 Postgres 自动释放,不会死锁。
  - try(不阻塞):后台任务的正确语义是"有人在跑就让过",不是排队。

进程内的 asyncio.Lock 仍保留在各调用点,作为零成本的第一道闸
(省掉同进程内明显重复的连接开销)。
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from sqlalchemy import text

# 锁空间(int32):每类后台任务一个,chat 级任务再配一个 chat 派生 key
LOCK_DREAM = 1        # 全局(Dream 跨 chat 维护词表)
LOCK_LIFE_SIM = 2     # per-chat
LOCK_NIGHT = 3        # per-chat
LOCK_EVOLUTION = 4    # per-chat


def chat_key(chat_id: uuid.UUID) -> int:
    """uuid → 有符号 int32(pg advisory lock 的第二个 key)。
    截断哈希,极小概率两只 chat 撞 key —— 后果只是偶尔互相串行,无正确性问题。"""
    return int.from_bytes(chat_id.bytes[:4], "big", signed=True)


@asynccontextmanager
async def advisory_guard(space: int, key: int = 0):
    """尝试拿 (space, key) 会话级咨询锁。yield 是否拿到;退出时释放。
    数据库不可用等异常向上抛(调用方的后台任务外层本就有 try/except)。"""
    from app.db import engine

    async with engine.connect() as raw:
        conn = await raw.execution_options(isolation_level="AUTOCOMMIT")
        got = bool(
            (await conn.execute(
                text("SELECT pg_try_advisory_lock(:s, :k)"),
                {"s": space, "k": key},
            )).scalar()
        )
        try:
            yield got
        finally:
            if got:
                await conn.execute(
                    text("SELECT pg_advisory_unlock(:s, :k)"),
                    {"s": space, "k": key},
                )
