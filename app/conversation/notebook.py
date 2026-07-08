"""
她的小本子 — model-curated 记忆(借鉴 Anthropic memory-tool / Claude Code 的
"模型自己维护的记忆文件"思路:自动 RAG 负责海量召回,自己写下的几行字负责
最要紧的那几件事,两者互补)。

  note  — 她在对话里 write_note 随手记的("他不吃香菜"、"下次问他考试结果")
  diary — 夜间代理替她写的当日小结(一天一条)

L1 注入:最近一条日记 + 活跃 note(有限几条,新者优先)。夜间代理负责收纳:
过期的 note 归档,日记只保留最近的在册。纯代码,零 LLM。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Note, now_ms

L1_MAX_NOTES = 5                      # 注入 L1 的活跃 note 上限
NOTE_MAX_LEN = 120                    # 单条长度上限(写不下就不是"随手记"了)
NOTE_STALE_MS = 7 * 86_400_000        # note 七天没动静 → 夜间归档
DIARY_KEEP = 3                        # 日记在册保留条数(更早的归档,正文永不删)


async def add_note(session: AsyncSession, chat_id: uuid.UUID, content: str,
                   kind: str = "note") -> Note | None:
    """写一条。超长截断;活跃 note 满员时拒绝(返回 None,让工具层给她人话)。"""
    content = (content or "").strip()[:NOTE_MAX_LEN]
    if not content:
        return None
    if kind == "note":
        active = await count_active_notes(session, chat_id)
        if active >= settings.notes_max_active:
            return None
    note = Note(chat_id=chat_id, kind=kind, content=content)
    session.add(note)
    await session.flush()
    return note


async def count_active_notes(session: AsyncSession, chat_id: uuid.UUID) -> int:
    from sqlalchemy import func
    return (
        await session.scalar(
            select(func.count()).select_from(Note).where(
                Note.chat_id == chat_id, Note.kind == "note", Note.status == "active"
            )
        )
    ) or 0


async def list_active(session: AsyncSession, chat_id: uuid.UUID,
                      kind: str | None = None, limit: int = 50) -> list[Note]:
    stmt = (
        select(Note)
        .where(Note.chat_id == chat_id, Note.status == "active")
        .order_by(Note.created_ms.desc())
        .limit(limit)
    )
    if kind:
        stmt = stmt.where(Note.kind == kind)
    return list((await session.execute(stmt)).scalars().all())


async def render_block(session: AsyncSession, chat_id: uuid.UUID) -> str:
    """编译 L1【你的小本子】块。空本子返回空串(块不注入)。"""
    diaries = await list_active(session, chat_id, kind="diary", limit=1)
    notes = await list_active(session, chat_id, kind="note", limit=L1_MAX_NOTES)

    lines: list[str] = []
    if diaries:
        d = diaries[0]
        dt = datetime.fromtimestamp(d.created_ms / 1000)
        lines.append(f"你{dt.month}月{dt.day}日的日记:{d.content}")
    if notes:
        items = "\n".join(f"  - {n.content}" for n in notes)
        lines.append(f"你记下的事:\n{items}")
    return "\n".join(lines)


async def housekeeping(session: AsyncSession, chat_id: uuid.UUID) -> dict:
    """夜间收纳:过期 note 归档;日记只留最近几条在册。"""
    archived = 0
    for n in await list_active(session, chat_id, kind="note"):
        if now_ms() - n.created_ms > NOTE_STALE_MS:
            n.status = "archived"
            archived += 1
    diaries = await list_active(session, chat_id, kind="diary")
    for d in diaries[DIARY_KEEP:]:
        d.status = "archived"
        archived += 1
    await session.flush()
    return {"archived": archived}
