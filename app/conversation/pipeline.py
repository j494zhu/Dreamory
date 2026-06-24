"""
Conversation pipeline — orchestrates affect + the three-tier memory for one turn.

Order matters: event extraction & dynamics run BEFORE generation, so her reply
reflects how *his current message* moved her state.

  0. load chat (persona, affect state, goal)
  1. time effects (arousal cooldown / session boundary / loop escalation)
  2. event extraction        (LLM① flash, classify only — no numbers)
  3. dynamics + transition   (pure code: the coupling rules decide the numbers)
  4. persist his message to L3 + hot-path tag it (zero LLM)
  5. assemble L1:
        刻骨铭心 (cherished) + L2 hot + L3 retrieval, deduped & budgeted
        + working-memory FIFO turns
        + core identity / goal / affect directives via the injector
  6. generate                (LLM② pro, two-stage <thinking>/<reply> brain-theatre)
  7. persist her reply to L3 (content + reasoning + emotion) + hot-path tag it
  8. save affect state
"""
from __future__ import annotations

import re
import time
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.affect import dynamics, extractor, injector
from app.affect.persona import PRESETS, Persona
from app.affect.state import AffectState
from app.config import settings
from app.conversation.identity import build_core_identity
from app.llm import client
from app.llm.client import MODEL_PRO
from app.memory import l1_assembly, l3_store, retrieval, tags
from app.memory.l2_hot import hot_memories
from app.models import Chat, MemoryKind, Speaker

_REPLY_RE = re.compile(r"<reply>(.*?)</reply>", re.S)
_THINKING_RE = re.compile(r"<thinking>(.*?)</thinking>", re.S)


def _parse_generation(raw: str) -> tuple[str, str]:
    reply_m = _REPLY_RE.search(raw)
    think_m = _THINKING_RE.search(raw)
    thinking = think_m.group(1).strip() if think_m else ""
    if reply_m:
        return thinking, reply_m.group(1).strip()
    cleaned = _THINKING_RE.sub("", raw).strip()
    return thinking, cleaned or raw.strip()


def _recent_context(mems, n: int = 6) -> str:
    role = {Speaker.user: "他", Speaker.agent: "她"}
    return "\n".join(f"{role.get(m.speaker, '?')}: {m.content}" for m in mems[-n:])


def _salience_from_events(ev: dict, state: AffectState) -> tuple[bool, float]:
    """Heuristic 刻骨铭心 flag: strong emotional impact → cherish this turn."""
    score = 0.0
    if ev["his_response_type"] == "turn_against":
        score += 0.6
    if ev["bid_in_her_last_msg"] in ("seeking_comfort", "venting") and \
            ev["his_response_type"] == "turn_away":
        score += 0.5
    if ev["is_repair_attempt"] and ev.get("_repair_accepted"):
        score += 0.3
    score += max(0.0, state.arousal - 0.6)
    return score >= 0.6, round(score, 2)


async def handle_message(
    session: AsyncSession, chat: Chat, user_content: str
) -> dict:
    persona = Persona.from_dict(chat.persona) if chat.persona else PRESETS["anxious"]
    state = AffectState.from_dict(chat.affect) if chat.affect else AffectState.fresh(persona)

    # 1. time effects --------------------------------------------------------
    dynamics.apply_time(state, persona, now=time.time())

    # context for extraction (state before his new message lands) ------------
    recent = await l3_store.working_memory(session, chat.id, settings.working_memory_k)
    her_last = next((m.content for m in reversed(recent) if m.speaker == Speaker.agent), None)
    ctx = _recent_context(recent)

    # 2. event extraction (LLM①) --------------------------------------------
    events = await extractor.extract(her_last, user_content, ctx, state)

    # 3. dynamics (pure code) -----------------------------------------------
    trace = dynamics.apply_events(state, events, persona, her_last, user_content)
    trace += dynamics.transition(state, events, persona)

    # 4. persist his message to L3 + hot-path tagging ------------------------
    user_mem = await l3_store.write_memory(
        session, chat_id=chat.id, content=user_content, speaker=Speaker.user, commit=False,
    )
    await tags.assign_tags(session, user_mem)
    await session.flush()

    # 5. assemble L1 ---------------------------------------------------------
    working = await l3_store.working_memory(session, chat.id, settings.working_memory_k)
    working_ids = {m.id for m in working}

    hits = await retrieval.retrieve(
        session, query=user_content, chat_id=chat.id,
        top_k=settings.retrieval_top_k, axis="content",
        goal=chat.goal, exclude_ids=working_ids,
    )
    cherished = await l3_store.cherished_memories(session, chat.id)
    hot = await hot_memories(session, chat.id, limit=settings.retrieval_top_k)

    memory_block, l1_dbg = l1_assembly.build_memory_block(
        cherished=cherished, hot=hot, retrieved=hits, exclude_ids=working_ids,
    )
    working_turns, working_summary = await l1_assembly.build_working_turns(working)

    tag_vocab = await tags.vocabulary_summary(session)
    core_identity = build_core_identity(persona, tag_vocab)
    if working_summary:
        memory_block = f"{memory_block}\n\n【更早对话摘要】\n{working_summary}".strip()

    system_prompt = injector.render(
        state, persona, core_identity=core_identity,
        memory_block=memory_block, goal=chat.goal,
    )

    # 6. generate (LLM②) -----------------------------------------------------
    # thinking=False: the "脑内剧场" inner monologue is an in-band roleplay <thinking>
    # block, NOT the model's native reasoning — keep native reasoning off so it
    # doesn't suppress the in-band block (and to save tokens/latency).
    messages = [{"role": "system", "content": system_prompt}] + working_turns
    raw = await client.chat(messages, model=MODEL_PRO, temperature=0.85, thinking=False)
    thinking, reply = _parse_generation(raw)

    # 7. persist her reply to L3 (content + reasoning + emotion) + tag -------
    cherish, salience = _salience_from_events(events, state)
    reply_mem = await l3_store.write_memory(
        session, chat_id=chat.id, content=reply, speaker=Speaker.agent,
        reasoning=thinking, emotion=state.mode, cherished=cherish, salience=salience,
        commit=False,
    )
    await tags.assign_tags(session, reply_mem)

    # 8. save affect ---------------------------------------------------------
    chat.affect = state.to_dict()
    await session.commit()

    result = {"role": "assistant", "content": reply}
    if settings.debug_panel:
        result["debug"] = {
            "thinking": thinking,
            "mode": state.mode,
            "events": events,
            "trace": trace,
            "open_loops": [l.content for l in state.open_loops],
            "grievances": [g.content for g in state.grievances if not g.resolved],
            "scalars": {
                "arousal": round(state.arousal, 2),
                "security": round(state.security, 2),
                "patience": state.patience,
                "warm_streak": state.warm_streak,
            },
            "l1": {
                "cherished": l1_dbg.cherished_ids,
                "hot": l1_dbg.hot_ids,
                "retrieved": [
                    {"content": h.memory.content[:60], "score": round(h.score, 3), "axis": h.axis}
                    for h in hits
                ],
                "dropped": l1_dbg.dropped_ids,
                "tokens": l1_dbg.tokens,
            },
            "tags_assigned": reply_mem.tags,
        }
    return result
