"""
Chat 配置的版本化 — "自我迭代"的地基。

设计约束(为将来开放模型自改铺路,当前只有 user/system 两个 actor):
  - append-only:每次配置变更 *之前* 先快照当前值,历史永不改写;
  - 可回退:rollback 本身也是一次带快照的变更(回退错了还能再回退);
  - 数据化:core_identity 从代码编译改为可覆盖的数据字段(chat.core_identity),
    这是模型将来能"改自己"的前提 —— 只有先是数据,才谈得上被修改。
将来接入模型自改时,在这里加门控(幅度限额 / 场合判定 / staging),而不是散在各处。
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chat, ChatRevision


def _config_of(chat: Chat) -> dict:
    return {
        "persona": chat.persona,
        "core_identity": chat.core_identity,
        "goal": chat.goal,
    }


async def snapshot(session: AsyncSession, chat: Chat, *,
                   reason: str, actor: str = "user") -> ChatRevision:
    """把 chat 当前配置存为下一号 revision。调用方随后再改 chat 字段。"""
    last = await session.scalar(
        select(func.max(ChatRevision.rev)).where(ChatRevision.chat_id == chat.id)
    )
    rev = ChatRevision(
        chat_id=chat.id, rev=(last or 0) + 1,
        data=_config_of(chat), reason=reason, actor=actor,
    )
    session.add(rev)
    await session.flush()
    return rev


async def list_revisions(session: AsyncSession, chat_id: uuid.UUID) -> list[ChatRevision]:
    rows = (
        await session.execute(
            select(ChatRevision)
            .where(ChatRevision.chat_id == chat_id)
            .order_by(ChatRevision.rev.desc())
        )
    ).scalars().all()
    return list(rows)


async def rollback(session: AsyncSession, chat: Chat, rev: int) -> ChatRevision:
    """回退到某个历史版本。先快照现状(回退也可被回退),再套用历史配置。"""
    target = (
        await session.execute(
            select(ChatRevision).where(
                ChatRevision.chat_id == chat.id, ChatRevision.rev == rev
            )
        )
    ).scalar_one_or_none()
    if target is None:
        raise ValueError(f"revision {rev} not found")

    await snapshot(session, chat, reason=f"rollback to rev {rev}", actor="system")
    data = target.data or {}
    chat.persona = data.get("persona") or {}
    chat.core_identity = data.get("core_identity")
    chat.goal = data.get("goal")
    await session.flush()
    return target
