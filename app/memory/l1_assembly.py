"""
L1 assembly — fill the context window's memory region.

L1 slots (this module owns the three memory slots; persona/goal/affect are
rendered by affect.injector):
    记忆-刻骨铭心   cherished/scarring  (≈20% budget, almost never evicted)
    记忆-工作记忆   recent FIFO window  (≈50% budget, oldest summarised on overflow)
    记忆-L3检索     retrieved relevant  (≈30% budget, lowest score dropped first)

Two decisions from the spec:
  A) Global dedup by id. The same memory can be both hot (L2) and freshly retrieved
     (L3); it must occupy L1 once. Priority 刻骨铭心 > L2热 > 当次检索 — keep the
     highest-priority occurrence, drop the rest.
  B) Per-slot token budget. On overflow: first compress 工作记忆 to a summary, then
     drop low-score L3-retrieved items, and only touch 刻骨铭心 as a last resort.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.config import settings
from app.llm import client
from app.llm.client import MODEL_PRO
from app.memory.retrieval import Hit
from app.models import Memory, Speaker

CHERISHED_FRAC = 0.20
WORKING_FRAC = 0.50
RETRIEVED_FRAC = 0.30


def estimate_tokens(text: str) -> int:
    """Cheap token estimate: CJK chars ≈ 1 token, latin ≈ 0.3 token."""
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    return int(cjk + (len(text) - cjk) * 0.3) + 1


@dataclass
class L1Debug:
    cherished_ids: list[str] = field(default_factory=list)
    hot_ids: list[str] = field(default_factory=list)
    retrieved_ids: list[str] = field(default_factory=list)
    dropped_ids: list[str] = field(default_factory=list)
    tokens: int = 0
    working_summarised: bool = False


def _line(m: Memory) -> str:
    who = "我" if m.speaker == Speaker.agent else "他"
    return f"  - [{who}] {m.content}"


def _fit(memories: list[Memory], budget: int) -> tuple[list[Memory], list[Memory]]:
    """Greedily keep memories (already priority-ordered) under a token budget."""
    kept, dropped, used = [], [], 0
    for m in memories:
        cost = estimate_tokens(m.content)
        if used + cost <= budget:
            kept.append(m)
            used += cost
        else:
            dropped.append(m)
    return kept, dropped


def build_memory_block(
    *,
    cherished: list[Memory],
    hot: list[Memory],
    retrieved: list[Hit],
    exclude_ids: set[uuid.UUID] | None = None,
    budget: int | None = None,
) -> tuple[str, L1Debug]:
    """
    Merge the three memory slots into one injected block.
    `exclude_ids` = ids already visible as live conversation turns (working memory),
    so we don't repeat them here.
    """
    total = budget or settings.l1_token_budget
    exclude = set(exclude_ids or set())
    dbg = L1Debug()

    # ── decision A: global dedup with priority 刻骨铭心 > L2热 > 当次检索 ──────
    claimed: set[uuid.UUID] = set(exclude)

    cher: list[Memory] = []
    for m in cherished:
        if m.id not in claimed:
            cher.append(m)
            claimed.add(m.id)

    hot_unique: list[Memory] = []
    for m in hot:
        if m.id not in claimed:
            hot_unique.append(m)
            claimed.add(m.id)

    retr_unique: list[Hit] = []
    for h in retrieved:
        if h.memory.id not in claimed:
            retr_unique.append(h)
            claimed.add(h.memory.id)

    # ── decision B: per-slot budgets (0.2.2: 弹性化) ────────────────────────
    # 刻骨铭心没用满的预算溢给"相关回忆"槽 —— 早期对话刻骨铭心还很少,
    # 僵化的 20% 白白闲置;比例仍是软上限,优先级次序不变。
    cher_budget = int(total * CHERISHED_FRAC)
    cher_kept, cher_drop = _fit(cher, cher_budget)
    cher_used = sum(estimate_tokens(m.content) for m in cher_kept)
    spill = max(0, cher_budget - cher_used)

    # hot + retrieved share the "relevant" budget; retrieved sorted by score,
    # hot prepended (higher priority). Lowest-score retrieved dropped first.
    relevant_budget = int(total * RETRIEVED_FRAC) + spill
    relevant = hot_unique + [h.memory for h in sorted(retr_unique, key=lambda h: h.score, reverse=True)]
    rel_kept, rel_drop = _fit(relevant, relevant_budget)

    dbg.cherished_ids = [str(m.id) for m in cher_kept]
    dbg.hot_ids = [str(m.id) for m in hot_unique if m in rel_kept]
    dbg.retrieved_ids = [str(h.memory.id) for h in retr_unique if h.memory in rel_kept]
    dbg.dropped_ids = [str(m.id) for m in (cher_drop + rel_drop)]

    blocks: list[str] = []
    if cher_kept:
        blocks.append("【记忆 · 刻骨铭心】\n" + "\n".join(_line(m) for m in cher_kept))
    if rel_kept:
        blocks.append("【记忆 · 相关回忆(检索/高频)】\n" + "\n".join(_line(m) for m in rel_kept))

    text = "\n\n".join(blocks)
    dbg.tokens = estimate_tokens(text)
    return text, dbg


async def build_working_turns(
    messages: list[Memory],
    *,
    budget: int | None = None,
    summarise: bool = False,
) -> tuple[list[dict], str | None]:
    """
    Turn the recent FIFO window into chat turns under ≈50% budget.
    If it overflows and `summarise` is on, the oldest overflow is compressed into
    one summary note (returned separately) and only the newest turns stay verbatim.
    """
    total = budget or settings.l1_token_budget
    working_budget = int(total * WORKING_FRAC)

    turns = [
        {"role": "assistant" if m.speaker == Speaker.agent else "user", "content": m.content}
        for m in messages
    ]

    # keep newest turns within budget
    kept_rev, used = [], 0
    for t in reversed(turns):
        cost = estimate_tokens(t["content"])
        if used + cost <= working_budget:
            kept_rev.append(t)
            used += cost
        else:
            break
    kept = list(reversed(kept_rev))
    overflow = turns[: len(turns) - len(kept)]

    summary = None
    if overflow:
        if summarise:
            convo = "\n".join(f'{t["role"]}: {t["content"]}' for t in overflow)
            summary = await client.chat(
                [
                    {"role": "system", "content": "用三两句中文概括这段较早的对话要点,只输出概括。"},
                    {"role": "user", "content": convo},
                ],
                model=MODEL_PRO, temperature=0.3, max_tokens=300, thinking=False,
            )
        else:
            summary = "(更早的对话已折叠)"

    return kept, (summary or None)
