"""
L2 — Hot Zone.

It is a *derived, droppable* read cache over L3, holding only ids (full content
lives once, in L3). Membership = the fixed-capacity top slice of L3 by
time-decayed heat.

Heat model (per spec):
  - heat is a TIME-DECAYED score, not raw frequency. A hit adds weight; the score
    decays exponentially with a half-life, so "ancient chart-toppers" fall out
    instead of squatting in the buffer forever.
  - retrieval hits do NOT touch the DB: they bump in-memory counters
    (hit_buffer[id] += 1, last_used_buffer[id] = now).
  - a background task flushes in batches every N seconds OR once M hits pile up
    (heat is a statistic, not a ledger — a few seconds of lag is fine).

Storage trick: we persist accumulated heat *as of last_used*, and order by the
continuously-decayed value at query time:
      effective_heat = heat * 0.5 ** ((now - last_used) / halflife)
so a never-re-hit row keeps decaying without rewriting every row each tick.
"""
from __future__ import annotations

import asyncio
import time
import uuid

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Memory, now_ms

HALFLIFE_MS = settings.heat_halflife_seconds * 1000.0
HIT_WEIGHT = 1.0


def _decayed_heat_expr(now_ms_val: int):
    """SQL expression: heat decayed from last_used to `now_ms_val`."""
    return Memory.heat * func.power(
        0.5, (now_ms_val - Memory.last_used) / HALFLIFE_MS
    )


class HeatTracker:
    """In-memory hit counters + background batch write-back to L3."""

    def __init__(self) -> None:
        self._hits: dict[uuid.UUID, int] = {}
        self._last_used: dict[uuid.UUID, int] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # ── hot path: record, don't persist ──────────────────────────────
    def record_hits(self, ids: list[uuid.UUID]) -> None:
        now = now_ms()
        for mid in ids:
            self._hits[mid] = self._hits.get(mid, 0) + 1
            self._last_used[mid] = now
        # opportunistic flush if the buffer got big (don't await; fire & forget)
        if len(self._hits) >= settings.heat_flush_max_buffer and self._task:
            asyncio.create_task(self.flush())

    async def flush(self) -> int:
        """Batch-apply buffered hits to L3. Returns number of rows updated."""
        async with self._lock:
            if not self._hits:
                return 0
            hits = self._hits
            last_used = self._last_used
            self._hits = {}
            self._last_used = {}

        from app.db import SessionLocal

        now = now_ms()
        # halflife is a constant per row (keeps executemany params homogeneous)
        params = [
            {
                "mid": mid,
                "inc": cnt,
                "lu": last_used.get(mid, now),
                "w": cnt * HIT_WEIGHT,
                "halflife": HALFLIFE_MS,
            }
            for mid, cnt in hits.items()
        ]
        # heat <- decayed-to-now old heat + this batch's weight
        stmt = text(
            """
            UPDATE memories
               SET use_count = use_count + :inc,
                   heat = heat * power(0.5, (:lu - last_used) / :halflife) + :w,
                   last_used = :lu
             WHERE id = :mid
            """
        )
        async with SessionLocal() as session:  # type: AsyncSession
            await session.execute(stmt, params)
            await session.commit()
        return len(params)

    # ── background loop, owned by the app lifespan ───────────────────
    async def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=settings.heat_flush_seconds)
                except asyncio.TimeoutError:
                    pass
                await self.flush()
        except asyncio.CancelledError:
            pass

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.flush()  # final drain


# module-level singleton
heat_tracker = HeatTracker()


async def hot_ids(
    session: AsyncSession, chat_id: uuid.UUID, capacity: int | None = None
) -> list[uuid.UUID]:
    """The Hot Zone: ids of the top-`capacity` memories by *current* decayed heat."""
    cap = capacity or settings.l2_capacity
    now = now_ms()
    rows = (
        await session.execute(
            select(Memory.id)
            .where(Memory.chat_id == chat_id, Memory.heat > 0)
            .order_by(_decayed_heat_expr(now).desc())
            .limit(cap)
        )
    ).scalars().all()
    return list(rows)


async def hot_memories(
    session: AsyncSession, chat_id: uuid.UUID, limit: int
) -> list[Memory]:
    """Hottest `limit` memories (full rows), current decayed heat order."""
    now = now_ms()
    rows = (
        await session.execute(
            select(Memory)
            .where(Memory.chat_id == chat_id, Memory.heat > 0)
            .order_by(_decayed_heat_expr(now).desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)
