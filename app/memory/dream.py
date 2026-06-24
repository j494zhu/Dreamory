"""
Dream — offline memory-base maintenance. The ONLY place an LLM is allowed to touch
the tag vocabulary, and even there its sole job is *naming a cluster that already
formed*. The hot path never creates tags.

Flow (per spec):
  1. 聚类  cluster pending memories (+ suspicious regions) by vector
  2. 命名  a large, tight cluster → ask the LLM for a name (LLM's only job)
  3. 合并  merge near-duplicate tags (centroids very close)
  4. 拆分  split a tag whose members clearly form multiple sub-clusters
  5. 重映射 rewrite affected memories' tags; keep an old→canonical alias map
  6. 更新  refresh the controlled vocabulary + every tag's centroid

Design constraints:
  - Stickiness: strongly prefer keeping existing canonical tags; only merge/split
    on a strong signal, so tag identity doesn't flip every night (ml ↔ machine-learning).
  - Both merge AND split. Criterion = "do members form clean sub-clusters?".
  - Remapping is the bulk of the work: rewrite stored memories AND maintain old→new.

Triggers (signal-driven, NOT hard-bound to "user asleep"): tag count over threshold /
vocabulary entropy rising / idle detected / pending backlog over threshold.

DISABLED BY DEFAULT (settings.dream_enabled = False). Built and callable, but the
pipeline never auto-runs it yet — invoke run_dream(..., force=True) manually.
"""
from __future__ import annotations

import uuid

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.llm import client
from app.llm.client import MODEL_PRO
from app.memory import tags as tag_ops
from app.models import Memory, Tag, TagAlias

# ── tunables ─────────────────────────────────────────────────────────
MIN_CLUSTER_SIZE = 4          # a cluster must be at least this big to earn a name
CLUSTER_EPS = 0.25            # DBSCAN cosine distance radius (tightness)
MERGE_SIM = 0.92             # centroid cosine above which two tags merge
SPLIT_MIN_SUB = 2            # a tag splits only into ≥2 clean sub-clusters
SPLIT_MIN_MEMBERS = 8        # ...and only if it has at least this many members
PENDING_BACKLOG_TRIGGER = 12  # signal: pending memories piled up this high


def _norm(mat: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(mat, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return mat / n


def _dbscan(vectors: list[list[float]], eps: float = CLUSTER_EPS) -> np.ndarray:
    from sklearn.cluster import DBSCAN

    X = _norm(np.asarray(vectors, dtype=np.float32))
    labels = DBSCAN(eps=eps, min_samples=2, metric="cosine").fit_predict(X)
    return labels


async def should_dream(session: AsyncSession) -> bool:
    """Signal check — is there enough backlog/entropy to bother dreaming?"""
    pending = (
        await session.execute(
            select(func.count()).select_from(Memory).where(Memory.pending.is_(True))
        )
    ).scalar_one()
    return pending >= PENDING_BACKLOG_TRIGGER


async def _name_cluster(texts: list[str]) -> dict:
    """LLM's ONLY job: give an already-formed cluster a short canonical name + facet."""
    sample = "\n".join(f"- {t[:120]}" for t in texts[:12])
    data = await client.chat_json(
        [
            {
                "role": "system",
                "content": (
                    "你在给一簇语义相近的记忆起一个规范标签(tag),用于将来检索过滤。\n"
                    "要求:标签是 1~3 个词的短名词短语(中文或英文小写),不要句子。\n"
                    "facet 从这些维度里选一个: domain(领域) / entity(实体) / type(类型) / "
                    "project(项目) / time(时间)。\n"
                    '只输出 JSON: {"name": "...", "facet": "..."}'
                ),
            },
            {"role": "user", "content": f"这簇记忆:\n{sample}\n\n起名。"},
        ],
        model=MODEL_PRO,
        default={"name": "", "facet": "topic"},
    )
    return data


async def _remap(session: AsyncSession, old: str, new: str) -> None:
    """Rewrite every memory carrying `old` to carry `new`, and record the alias."""
    rows = (
        await session.execute(select(Memory).where(Memory.tags.op("&&")([old])))
    ).scalars().all()
    for m in rows:
        m.tags = sorted({(new if t == old else t) for t in m.tags})
    session.add(TagAlias(alias=old, canonical=new))
    await session.flush()


async def _cluster_and_name(session: AsyncSession, force: bool) -> list[str]:
    """Steps 1+2: cluster pending memories, name the big/tight ones, assign tags."""
    pend = (
        await session.execute(
            select(Memory).where(Memory.pending.is_(True), Memory.content_vec.isnot(None))
        )
    ).scalars().all()
    if len(pend) < MIN_CLUSTER_SIZE:
        return []

    labels = _dbscan([list(m.content_vec) for m in pend])
    created: list[str] = []
    for label in sorted(set(labels)):
        if label == -1:
            continue  # noise stays pending
        members = [pend[i] for i in range(len(pend)) if labels[i] == label]
        if len(members) < MIN_CLUSTER_SIZE:
            continue
        named = await _name_cluster([m.content for m in members])
        name = (named.get("name") or "").strip().lower()
        if not name:
            continue
        await tag_ops.seed_tag(
            session, name=name, facet=named.get("facet", "topic"),
            example_texts=[m.content for m in members[:8]],
        )
        for m in members:
            m.tags = sorted(set(m.tags) | {name})
            m.pending = False
        created.append(name)
    await session.flush()
    return created


async def _merge_tags(session: AsyncSession) -> list[tuple[str, str]]:
    """Step 3: merge tags whose centroids are nearly identical (stickiness:
    keep the larger one as canonical)."""
    tags = [t for t in await tag_ops.get_vocabulary(session) if t.centroid is not None]
    merged: list[tuple[str, str]] = []
    used: set[str] = set()
    for i in range(len(tags)):
        if tags[i].name in used:
            continue
        a = np.asarray(tags[i].centroid, dtype=np.float32)
        for j in range(i + 1, len(tags)):
            if tags[j].name in used:
                continue
            b = np.asarray(tags[j].centroid, dtype=np.float32)
            sim = float(np.dot(a, b) / ((np.linalg.norm(a) * np.linalg.norm(b)) or 1))
            if sim >= MERGE_SIM:
                keep, drop = sorted((tags[i], tags[j]), key=lambda t: -t.member_count)
                await _remap(session, drop.name, keep.name)
                await session.delete(drop)
                used.add(drop.name)
                merged.append((drop.name, keep.name))
    await session.flush()
    return merged


async def _split_tags(session: AsyncSession) -> list[str]:
    """Step 4: split a tag whose members form ≥2 clean sub-clusters."""
    new_tags: list[str] = []
    for tag in await tag_ops.get_vocabulary(session):
        rows = (
            await session.execute(
                select(Memory).where(
                    Memory.tags.op("&&")([tag.name]), Memory.content_vec.isnot(None)
                )
            )
        ).scalars().all()
        if len(rows) < SPLIT_MIN_MEMBERS:
            continue
        labels = _dbscan([list(m.content_vec) for m in rows], eps=CLUSTER_EPS * 0.7)
        clusters = [c for c in set(labels) if c != -1]
        if len(clusters) < SPLIT_MIN_SUB:
            continue
        for ci, c in enumerate(clusters, start=1):
            members = [rows[i] for i in range(len(rows)) if labels[i] == c]
            named = await _name_cluster([m.content for m in members])
            sub = (named.get("name") or f"{tag.name}-{ci}").strip().lower()
            if sub == tag.name:
                continue
            await tag_ops.seed_tag(
                session, name=sub, facet=tag.facet,
                example_texts=[m.content for m in members[:8]],
            )
            for m in members:
                m.tags = sorted((set(m.tags) - {tag.name}) | {sub})
            session.add(TagAlias(alias=tag.name, canonical=sub))
            new_tags.append(sub)
    await session.flush()
    return new_tags


async def run_dream(
    session: AsyncSession, *, force: bool = False
) -> dict:
    """
    Full Dream cycle. No-op unless settings.dream_enabled or force=True.
    Returns a report dict (what changed).
    """
    if not (settings.dream_enabled or force):
        return {"ran": False, "reason": "dream disabled"}

    report: dict = {"ran": True}
    report["named"] = await _cluster_and_name(session, force)
    report["merged"] = await _merge_tags(session)
    report["split"] = await _split_tags(session)

    # step 6: refresh all centroids + member counts
    for tag in await tag_ops.get_vocabulary(session):
        await tag_ops.recompute_centroid(session, tag.name)

    await session.commit()
    return report
