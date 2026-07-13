"""
Retrieval pipeline.

  query
   → VectorDB_1 (by content) and/or VectorDB_2 (by emotion) semantic recall → ids
   → back to the main table for full fields (single store: the search already
     returns full rows via JOIN, no cross-store id round-trip)
   → tags as a WHERE filter (vector = fuzzy meaning axis, tag = hard category axis)
   → record hits in the in-memory heat counter (see l2_hot)
   → dedupe, optionally bias by the current goal, return

Two axes are independent: "search by content" vs "search by emotion"
(e.g. 找我感到被背叛的记忆 → emotion axis), so they never pollute each other.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import numpy as np

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.llm import embeddings
from app.memory import l3_store
from app.memory.l2_hot import heat_tracker
from app.models import Memory, MemoryKind


@dataclass
class Hit:
    memory: Memory
    score: float   # 排序用分数(可能含 goal 偏置)
    axis: str      # "content" | "emotion" | "both"
    raw: float = 0.0   # 裸相似度(goal 偏置前)——下限过滤与置信度门都用它


def apply_score_floor(hits: list[Hit], *, min_score: float,
                      life_min_score: float) -> list[Hit]:
    """绝对相关性下限(纯函数,可单测)。用裸分数比较,life_event 用更高的线
    (生活琐事有话题种子这条专用通道,只有真聊到那件事才该被自动想起)。
    min_score <= 0 时整体关闭 —— 工具路径的主动搜索自己决定怎么过滤。"""
    if min_score <= 0:
        return hits
    kept = []
    for h in hits:
        bar = max(min_score, life_min_score) \
            if h.memory.kind == MemoryKind.life_event else min_score
        if h.raw >= bar:
            kept.append(h)
    return kept


def _cos(a, b) -> float:
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return float(np.dot(a, b) / (na * nb)) if na and nb else 0.0


async def retrieve(
    session: AsyncSession,
    *,
    query: str,
    chat_id: uuid.UUID,
    top_k: int | None = None,
    axis: str = "content",            # "content" | "emotion" | "both"
    tags_any: list[str] | None = None,
    goal: str | None = None,          # L1 current goal → conditional bias
    goal_weight: float = 0.25,
    exclude_ids: set[uuid.UUID] | None = None,
    ts_min_ms: int | None = None,     # side-car time filter (tool-driven recall)
    ts_max_ms: int | None = None,
    record: bool = True,
    min_score: float | None = None,   # None → settings 下限;0 → 关闭(工具路径)
) -> list[Hit]:
    k = top_k or settings.retrieval_top_k
    pool = max(k * 2, k + 4)          # over-fetch, then re-rank with goal bias

    merged: dict[uuid.UUID, Hit] = {}

    if axis in ("content", "both"):
        for mem, score in await l3_store.search_content(
            session, query=query, chat_id=chat_id, top_k=pool,
            tags_any=tags_any, exclude_ids=exclude_ids,
            ts_min_ms=ts_min_ms, ts_max_ms=ts_max_ms,
        ):
            merged[mem.id] = Hit(mem, score, "content", raw=score)

    if axis in ("emotion", "both"):
        for mem, score in await l3_store.search_emotion(
            session, query=query, chat_id=chat_id, top_k=pool,
            tags_any=tags_any, exclude_ids=exclude_ids,
            ts_min_ms=ts_min_ms, ts_max_ms=ts_max_ms,
        ):
            cur = merged.get(mem.id)
            if cur is None:
                merged[mem.id] = Hit(mem, score, "emotion", raw=score)
            else:
                cur.score = max(cur.score, score)
                cur.raw = max(cur.raw, score)
                cur.axis = "both"

    hits = list(merged.values())

    # 绝对下限(goal 偏置前的裸分数):没有相关内容时,kNN 返回的是
    # "最不不相关"的底噪——被过滤的命中不进 L1,也不记热度(否则垃圾命中
    # 每轮涨热度,迟早爬进 L2 被钉死在 L1 里)。
    floor = settings.retrieval_min_score if min_score is None else min_score
    hits = apply_score_floor(
        hits, min_score=floor, life_min_score=settings.retrieval_min_score_life,
    )

    # conditional bias: nudge ranking toward the current goal -----------------
    if goal and hits:
        gvec = await embeddings.embed_one(goal)
        for h in hits:
            if h.memory.content_vec is not None:
                h.score += goal_weight * _cos(h.memory.content_vec, gvec)

    hits.sort(key=lambda h: h.score, reverse=True)
    hits = hits[:k]

    # record hits in the heat counter (hot path: memory only, never the DB) ----
    if record and hits:
        heat_tracker.record_hits([h.memory.id for h in hits])

    return hits


async def multi_step_retrieve(
    session: AsyncSession,
    *,
    queries: list[str],
    chat_id: uuid.UUID,
    top_k: int | None = None,
    axis: str = "content",
    goal: str | None = None,
) -> list[Hit]:
    """MemGPT-style function chaining: try several phrasings, union & dedupe.
    (Optional enhancement — the Agent can 'search again with different words'.)"""
    seen: dict[uuid.UUID, Hit] = {}
    for q in queries:
        for h in await retrieve(
            session, query=q, chat_id=chat_id, top_k=top_k, axis=axis,
            goal=goal, exclude_ids=set(seen), record=False,
        ):
            prev = seen.get(h.memory.id)
            if prev is None or h.score > prev.score:
                seen[h.memory.id] = h
    hits = sorted(seen.values(), key=lambda h: h.score, reverse=True)
    if hits:
        heat_tracker.record_hits([h.memory.id for h in hits])
    return hits
