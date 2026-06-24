"""
Tag registry + hot-path tagging.

Design rules baked in:
  - Tag for RETRIEVAL, not for description. The only job of a tag is to shrink the
    candidate set at query time; content + vector already "describe" the memory.
  - The hot path NEVER calls an LLM and NEVER invents tags. It only *assigns*
    existing tags by deterministic vector matching. New-tag creation + vocabulary
    maintenance happen exclusively offline in Dream.
  - Synonyms / non-literal tags are embedding's job, not the tag system's
    ("android" ≈ "replicant"; a betrayal with no literal word still lands near it).

Assignment algorithm (kNN label propagation + weighted vote, with centroid backstop):
  1. take the memory's content vector
  2. find top-k content-axis neighbours; each neighbour votes its tags weighted by
     cosine similarity (normalised to [0,1])
  3. independently, score every registry tag by cosine(memory, tag.centroid)
  4. a tag is assigned iff max(vote, centroid_sim) ≥ threshold
  5. if nothing clears the bar (sparse/unclaimed region) → leave pending, no tag
  6. cap at TAG_MAX_PER_MEMORY (2–5 is the sweet spot)
"""
from __future__ import annotations

import uuid

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.llm import embeddings
from app.models import Memory, Tag


def _vec(v) -> np.ndarray | None:
    if v is None:
        return None
    a = np.asarray(v, dtype=np.float32)
    return a if a.size else None


def _cos(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


async def get_vocabulary(session: AsyncSession) -> list[Tag]:
    """All canonical tags — compiled into L1 so the Agent knows what tags exist
    (encouraging reuse, suppressing divergence)."""
    return list((await session.execute(select(Tag).order_by(Tag.facet, Tag.name))).scalars().all())


async def vocabulary_summary(session: AsyncSession, max_tags: int = 40) -> str:
    """Compact, facet-grouped tag list for the L1 prompt block."""
    tags = await get_vocabulary(session)
    if not tags:
        return ""
    by_facet: dict[str, list[str]] = {}
    for t in tags[:max_tags]:
        by_facet.setdefault(t.facet, []).append(t.name)
    return "\n".join(f"  {facet}: {', '.join(names)}" for facet, names in by_facet.items())


async def assign_tags(session: AsyncSession, memory: Memory) -> list[str]:
    """Hot-path tagging for one memory. Mutates memory.tags / memory.pending.
    Returns the assigned tags. Zero LLM calls."""
    qvec = _vec(memory.content_vec)
    if qvec is None:
        return []

    k = settings.tag_knn_k
    threshold = settings.tag_vote_threshold

    # 1+2. kNN neighbours (same chat), weighted tag vote ---------------------
    dist = Memory.content_vec.cosine_distance(memory.content_vec)
    neighbours = (
        await session.execute(
            select(Memory, dist.label("d"))
            .where(
                Memory.chat_id == memory.chat_id,
                Memory.id != memory.id,
                Memory.content_vec.isnot(None),
            )
            .order_by(dist)
            .limit(k)
        )
    ).all()

    vote: dict[str, float] = {}
    sim_total = 0.0
    for nbr, d in neighbours:
        sim = max(0.0, 1.0 - float(d))
        sim_total += sim
        for t in (nbr.tags or []):
            vote[t] = vote.get(t, 0.0) + sim
    if sim_total > 0:
        vote = {t: s / sim_total for t, s in vote.items()}

    # 3. centroid backstop against the registry ------------------------------
    centroid_sim: dict[str, float] = {}
    for tag in await get_vocabulary(session):
        cvec = _vec(tag.centroid)
        if cvec is not None:
            centroid_sim[tag.name] = _cos(qvec, cvec)

    # 4. combine: a tag's score is the stronger of the two signals -----------
    scores: dict[str, float] = {}
    for t in set(vote) | set(centroid_sim):
        scores[t] = max(vote.get(t, 0.0), centroid_sim.get(t, 0.0))

    chosen = sorted(
        (t for t, s in scores.items() if s >= threshold),
        key=lambda t: scores[t],
        reverse=True,
    )[: settings.tag_max_per_memory]

    memory.tags = chosen
    memory.pending = len(chosen) == 0   # 5. nothing cleared the bar → pending
    return chosen


# ── Cold-start seeding + centroid maintenance (used by scripts & Dream) ──────
async def seed_tag(
    session: AsyncSession,
    *,
    name: str,
    facet: str,
    example_texts: list[str],
    description: str | None = None,
) -> Tag:
    """Create/update a controlled-vocabulary tag with a centroid built from
    example texts (manual seed word list for cold start, per M2)."""
    vecs = await embeddings.embed(example_texts) if example_texts else []
    centroid = np.mean(np.asarray(vecs, dtype=np.float32), axis=0).tolist() if vecs else None

    tag = await session.get(Tag, name)
    if tag is None:
        tag = Tag(name=name, facet=facet, description=description, centroid=centroid)
        session.add(tag)
    else:
        tag.facet = facet
        tag.description = description or tag.description
        if centroid is not None:
            tag.centroid = centroid
    await session.flush()
    return tag


async def recompute_centroid(session: AsyncSession, name: str) -> None:
    """Refresh a tag's centroid = mean of all member memories' content vectors."""
    rows = (
        await session.execute(
            select(Memory.content_vec).where(
                Memory.tags.op("&&")([name]), Memory.content_vec.isnot(None)
            )
        )
    ).scalars().all()
    tag = await session.get(Tag, name)
    if tag is None:
        return
    vecs = [_vec(v) for v in rows if _vec(v) is not None]
    tag.member_count = len(vecs)
    if vecs:
        tag.centroid = np.mean(np.stack(vecs), axis=0).tolist()
    await session.flush()
