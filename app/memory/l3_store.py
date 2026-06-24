"""
L3 — cold storage, infinite, the single source of truth.

Everything else (L2, materialised views, the Hot Zone) is a *derived, droppable*
read cache of this table. Lose them → rebuild from L3. Never the reverse.

This module owns:
  - write_memory()      : persist a turn (content + emotion/reasoning), embed PURE
                          content on the content axis and PURE emotion/reasoning
                          on the emotion axis.
  - get_by_ids()        : the "go back to the main table for full fields" step,
                          done as a single SQL fetch (one store, one truth).
  - working_memory()    : last-k messages for a chat (the traditional FIFO tail).
  - search_content()    : VectorDB_1 semantic recall (cosine) + optional tag filter.
  - search_emotion()    : VectorDB_2 emotion/mood recall (the second axis).
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import embeddings
from app.models import Memory, MemoryKind, Speaker, now_ms


async def write_memory(
    session: AsyncSession,
    *,
    chat_id: uuid.UUID,
    content: str,
    speaker: Speaker,
    reasoning: str = "",
    emotion: str = "",
    cherished: bool = False,
    salience: float = 0.0,
    kind: MemoryKind = MemoryKind.message,
    commit: bool = True,
) -> Memory:
    """
    Insert one L3 row.

    Two vectors are produced from DISJOINT, PURE texts:
      content_vec  <- content only            (semantic axis)
      emotion_vec  <- emotion + reasoning only (mood axis)
    No tags / timestamps / speaker are ever concatenated into either.
    """
    content_vec = await embeddings.embed_one(content)

    emotion_text = "\n".join(p for p in (emotion, reasoning) if p).strip()
    emotion_vec = await embeddings.embed_one(emotion_text) if emotion_text else None

    mem = Memory(
        chat_id=chat_id,
        content=content,
        speaker=speaker,
        kind=kind,
        emotion_reasoning={"emotion": emotion, "reasoning": reasoning},
        tags=[],
        pending=True,
        cherished=cherished,
        salience=salience,
        ts_ms=now_ms(),
        last_used=now_ms(),
        use_count=0,
        heat=0.0,
        content_vec=content_vec,
        emotion_vec=emotion_vec,
    )
    session.add(mem)
    if commit:
        await session.commit()
        await session.refresh(mem)
    else:
        await session.flush()
    return mem


async def get_by_ids(
    session: AsyncSession, ids: list[uuid.UUID]
) -> dict[uuid.UUID, Memory]:
    """Full-field fetch by id (id -> Memory). Order is the caller's business."""
    if not ids:
        return {}
    rows = (
        await session.execute(select(Memory).where(Memory.id.in_(ids)))
    ).scalars().all()
    return {m.id: m for m in rows}


async def working_memory(
    session: AsyncSession, chat_id: uuid.UUID, k: int
) -> list[Memory]:
    """Most recent k messages for this chat, oldest→newest (FIFO window = L1 工作记忆)."""
    rows = (
        await session.execute(
            select(Memory)
            .where(Memory.chat_id == chat_id, Memory.kind == MemoryKind.message)
            .order_by(Memory.ts_ms.desc())
            .limit(k)
        )
    ).scalars().all()
    return list(reversed(rows))


async def cherished_memories(
    session: AsyncSession, chat_id: uuid.UUID, limit: int = 32
) -> list[Memory]:
    """刻骨铭心 — pinned high-salience memories (almost never evicted)."""
    rows = (
        await session.execute(
            select(Memory)
            .where(Memory.chat_id == chat_id, Memory.cherished.is_(True))
            .order_by(Memory.salience.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def _vector_search(
    session: AsyncSession,
    *,
    column,
    query_vec: list[float],
    chat_id: uuid.UUID,
    top_k: int,
    tags_any: list[str] | None,
    exclude_ids: set[uuid.UUID] | None,
) -> list[tuple[Memory, float]]:
    """Shared cosine ANN search over a given vector column. Returns (Memory, score)
    with score in [0,1] (1 = identical), tag overlap applied as a hard WHERE filter."""
    distance = column.cosine_distance(query_vec)
    stmt = (
        select(Memory, distance.label("dist"))
        .where(Memory.chat_id == chat_id, column.isnot(None))
    )
    if tags_any:
        # tag = hard category axis: keep only rows sharing ≥1 requested tag.
        stmt = stmt.where(Memory.tags.op("&&")(tags_any))
    if exclude_ids:
        stmt = stmt.where(Memory.id.notin_(exclude_ids))
    stmt = stmt.order_by(distance).limit(top_k)

    rows = (await session.execute(stmt)).all()
    return [(m, 1.0 - float(dist)) for m, dist in rows]


async def search_content(
    session: AsyncSession,
    *,
    query: str,
    chat_id: uuid.UUID,
    top_k: int,
    tags_any: list[str] | None = None,
    exclude_ids: set[uuid.UUID] | None = None,
) -> list[tuple[Memory, float]]:
    """VectorDB_1: semantic recall by content. (the 'what was said' axis)"""
    qvec = await embeddings.embed_one(query)
    return await _vector_search(
        session, column=Memory.content_vec, query_vec=qvec, chat_id=chat_id,
        top_k=top_k, tags_any=tags_any, exclude_ids=exclude_ids,
    )


async def search_emotion(
    session: AsyncSession,
    *,
    query: str,
    chat_id: uuid.UUID,
    top_k: int,
    tags_any: list[str] | None = None,
    exclude_ids: set[uuid.UUID] | None = None,
) -> list[tuple[Memory, float]]:
    """VectorDB_2: recall by emotion/mood. (the 'how it felt' axis — e.g. '找我感到被背叛的记忆')"""
    qvec = await embeddings.embed_one(query)
    return await _vector_search(
        session, column=Memory.emotion_vec, query_vec=qvec, chat_id=chat_id,
        top_k=top_k, tags_any=tags_any, exclude_ids=exclude_ids,
    )
