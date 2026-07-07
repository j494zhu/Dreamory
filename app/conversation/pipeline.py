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
        + 时间感知(现在几点、距上次说话多久)
  6. generate                (LLM② pro, two-stage <thinking>/<reply> brain-theatre;
                              可多条 <reply> 连发,可附带 <timer> 约"过会儿来找他")
  7. persist her replies to L3 (content + reasoning + emotion) + hot-path tag them
  8. save affect state (+ schedule timer ping if she asked for one)

There is a second entrypoint, handle_timer_fire(): 定时器到点后的隐藏 LLM 调用,
没有他的新消息,不跑抽取/动力学,只做 时间效应 → L1 组装 → 生成 → 落库,
产出的主动消息经 SSE 推给前端。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.affect import dynamics, extractor, injector
from app.affect.persona import PRESETS, Persona
from app.affect.state import AffectState
from app.config import settings
from app.conversation.identity import build_core_identity
from app.llm import client
from app.llm.client import MODEL_PRO
from app.memory import dream, l1_assembly, l3_store, retrieval, tags
from app.memory.l2_hot import hot_memories
from app.models import Chat, MemoryKind, Speaker, TimerPing, now_ms

logger = logging.getLogger(__name__)

_REPLY_RE = re.compile(r"<reply>(.*?)</reply>", re.S)
_THINKING_RE = re.compile(r"<thinking>(.*?)</thinking>", re.S)
_TIMER_RE = re.compile(r'<timer\s+minutes\s*=\s*"?(\d+)"?\s*>(.*?)</timer>', re.S | re.I)

MAX_REPLIES_PER_TURN = 4   # 连发上限,和注入器里的口径一致


def _parse_generation(raw: str) -> tuple[str, list[str]]:
    """<thinking> + 一到多个 <reply> → (内心独白, [消息1, 消息2, …])。"""
    think_m = _THINKING_RE.search(raw)
    thinking = think_m.group(1).strip() if think_m else ""

    replies = [r.strip() for r in _REPLY_RE.findall(raw) if r.strip()]
    if replies:
        return thinking, replies[:MAX_REPLIES_PER_TURN]

    # 没找到 reply 标签:fallback,删掉整段 thinking 后当单条消息返回
    cleaned = _THINKING_RE.sub("", raw).strip()
    return thinking, [cleaned or raw.strip()]


def _extract_timer(raw: str) -> tuple[str, dict | None]:
    """解析并剥掉 <timer minutes="X">备忘</timer>。返回 (清洗后的raw, timer|None)。"""
    m = _TIMER_RE.search(raw)
    if not m:
        return raw, None
    minutes = max(1, min(settings.timer_max_minutes, int(m.group(1))))
    return _TIMER_RE.sub("", raw), {"minutes": minutes, "topic": m.group(2).strip()}


def _recent_context(mems, n: int = 6) -> str: # 每一条消息前面加上人称
    role = {Speaker.user: "他", Speaker.agent: "她"}
    return "\n".join(f"{role.get(m.speaker, '?')}: {m.content}" for m in mems[-n:])


def _her_last_burst(mems) -> str | None:
    """她最近一次发言的完整"连发段"(连续的 agent 消息拼在一起)。
    单条消息时代这是"她的上一条消息";多消息时代,投标可能分散在连发的
    几条里,抽取器需要看到整段才判得准。"""
    idx = next(
        (i for i in range(len(mems) - 1, -1, -1) if mems[i].speaker == Speaker.agent),
        None,
    )
    if idx is None:
        return None
    start = idx
    while start > 0 and mems[start - 1].speaker == Speaker.agent:
        start -= 1
    return "\n".join(m.content for m in mems[start: idx + 1])


# ── 时间感知(注入器【时间感知】块的原料)────────────────────────────
_WEEKDAYS = "一二三四五六日"


def _humanize_gap(seconds: float) -> str:
    if seconds < 90:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)}分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)}小时前"
    return f"{int(seconds // 86400)}天前"


def _time_context(prev_ts: float, *, now: float | None = None, first_turn: bool = False) -> str:
    now = now or time.time()
    dt = datetime.fromtimestamp(now)
    line = f"现在是{dt.year}年{dt.month}月{dt.day}日 周{_WEEKDAYS[dt.weekday()]} {dt:%H:%M}。"
    gap = max(0.0, now - prev_ts)
    if not first_turn and gap >= 90:
        line += f"你们上一次说话是{_humanize_gap(gap)}。"
    return line


async def _persist_agent_replies(
    session: AsyncSession, chat_id, replies: list[str], thinking: str,
    state: AffectState, *, cherish: bool = False, salience: float = 0.0,
) -> list:
    """把一次生成的多条消息逐条写入 L3(每条独立成一条记忆、独立内容向量,
    检索粒度不受连发影响)。脑内剧场/情绪/刻骨铭心 只挂在第一条上——
    一次生成只有一份 reasoning,重复嵌入会污染情绪轴。"""
    mems = []
    for i, content in enumerate(replies):
        mem = await l3_store.write_memory(
            session, chat_id=chat_id, content=content, speaker=Speaker.agent,
            reasoning=thinking if i == 0 else "",
            emotion=state.mode if i == 0 else "",
            cherished=cherish if i == 0 else False,
            salience=salience if i == 0 else 0.0,
            commit=False,
        )
        await tags.assign_tags(session, mem)
        mems.append(mem)
    return mems


# 日常对话的情绪基准线:只有明显高出日常水位的波峰才值得"刻骨铭心",
# 否则每次小磕碰都被永久珍藏,记忆区会被稀释。
DAILY_AROUSAL_BASELINE = 0.6
CHERISH_THRESHOLD = 0.6


def _salience_from_events(ev: dict, state: AffectState) -> tuple[bool, float]:
    """Heuristic 刻骨铭心 flag: strong emotional impact → cherish this turn.

    一轮对话通常只承载一种主导情绪(要么被伤到、要么被安抚),所以三类事件
    取最强的那一个而非相加 —— 消除 turn_against 与 repair 在同一轮里的叠加。
    """
    event_score = 0.0
    if ev["his_response_type"] == "turn_against": # 用户攻击了llm (他暴击她)
        event_score = max(event_score, 0.6)
    if ev["bid_in_her_last_msg"] in ("seeking_comfort", "venting") and \
            ev["his_response_type"] == "turn_away": # llm寻求安慰, 用户不理睬, 忽视, 敷衍
        event_score = max(event_score, 0.5)
    if ev["is_repair_attempt"] and ev.get("_repair_accepted"):
        event_score = max(event_score, 0.3)

    # 好感度层级跨越:关系升到/跌出某一档是里程碑级记忆
    # (跨过"恋人"线的那一轮,足以单独刻骨铭心)。
    shift = ev.get("_tier_shift")
    if shift:
        event_score = max(event_score, 0.65 if shift.get("milestone") else 0.45)

    # 日常基准比较:超出日常情绪水位的部分才计入。
    arousal_excess = max(0.0, state.arousal - DAILY_AROUSAL_BASELINE)
    score = event_score + arousal_excess
    return score >= CHERISH_THRESHOLD, round(score, 2)


# ── auto-dream (Dream / 后台离线维护)──────────────────────────────────
# 触发点:每轮对话提交后检查 should_dream();积压够多就在后台跑一次 Dream。
# 后台运行(不阻塞回复)+ 独立 session + 单飞锁(同一时刻只跑一个 Dream)。
_dream_lock = asyncio.Lock()
_bg_tasks: set[asyncio.Task] = set()


async def _auto_dream() -> None:
    if not settings.dream_enabled or _dream_lock.locked():
        return
    async with _dream_lock:
        from app.db import SessionLocal

        try:
            async with SessionLocal() as s:
                if await dream.should_dream(s):
                    report = await dream.run_dream(s)
                    logger.info("auto-dream ran: %s", report)
        except Exception:  # 后台维护绝不能把主流程带崩
            logger.exception("auto-dream failed")


def _schedule_auto_dream() -> None:
    """Fire-and-forget the Dream check; keep a ref so the task isn't GC'd."""
    if not settings.dream_enabled:
        return
    task = asyncio.create_task(_auto_dream())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _pending_timer_count(session: AsyncSession, chat_id) -> int:
    return (
        await session.scalar(
            select(func.count()).select_from(TimerPing).where(
                TimerPing.chat_id == chat_id, TimerPing.status == "pending"
            )
        )
    ) or 0


async def handle_message(
    session: AsyncSession, chat: Chat, user_content: str
) -> dict:
    persona = Persona.from_dict(chat.persona) if chat.persona else PRESETS["anxious"]
    state = AffectState.from_dict(chat.affect) if chat.affect else AffectState.fresh(persona)

    # 1. time effects --------------------------------------------------------
    prev_ts = state.last_ts    # apply_time 会覆盖 last_ts,时间感知要用旧值
    first_turn = state.turn == 0
    dynamics.apply_time(state, persona, now=time.time())

    # context for extraction (state before his new message lands) ------------
    recent = await l3_store.working_memory(session, chat.id, settings.working_memory_k)
    her_last = _her_last_burst(recent)  # 她最近一次发言的完整连发段(多消息安全)

    ctx = _recent_context(recent) # context, 给消息加上 他/她 人称

    # 2. event extraction (LLM①) --------------------------------------------
    events = await extractor.extract(her_last, user_content, ctx, state)

    # 3. dynamics (pure code) -----------------------------------------------
    trace = dynamics.apply_events(state, events, persona, her_last, user_content)
    trace += dynamics.transition(state, events, persona)

    # 4. persist his message to L3 + hot-path tagging ------------------------
    user_mem = await l3_store.write_memory(
        session, chat_id=chat.id, content=user_content, speaker=Speaker.user, commit=False,
    ) # 将用户刚刚发的消息模拟写进数据库, 不提交; 然后返回一个Memory对象
    await tags.assign_tags(session, user_mem)
    await session.flush()

    # 5. assemble L1 ---------------------------------------------------------
    working = await l3_store.working_memory(session, chat.id, settings.working_memory_k)
    working_ids = {m.id for m in working} # 每一条消息都对应一个uuid7

    hits = await retrieval.retrieve( # 找出与当前语义有关, 但是不存在工作记忆里的内容
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

    pending_timers = await _pending_timer_count(session, chat.id)
    allow_timer = settings.timer_enabled and pending_timers < settings.timer_max_pending

    system_prompt = injector.render(
        state, persona, core_identity=core_identity,
        memory_block=memory_block, goal=chat.goal,
        time_context=_time_context(prev_ts, first_turn=first_turn),
        allow_timer=allow_timer,
    )

    # 6. generate (LLM②) -----------------------------------------------------
    # thinking=False: the "脑内剧场" inner monologue is an in-band roleplay <thinking>
    # block, NOT the model's native reasoning — keep native reasoning off so it
    # doesn't suppress the in-band block (and to save tokens/latency).
    messages = [{"role": "system", "content": system_prompt}] + working_turns
    raw = await client.chat(messages, model=MODEL_PRO, temperature=0.85, thinking=False)
    raw, timer_req = _extract_timer(raw)          # 她约的"过会儿来找他"
    thinking, replies = _parse_generation(raw)    # 一到多条连发消息

    # 7. persist her replies to L3 (content + reasoning + emotion) + tag -----
    cherish, salience = _salience_from_events(events, state)
    reply_mems = await _persist_agent_replies(
        session, chat.id, replies, thinking, state, cherish=cherish, salience=salience,
    )

    # 7.5 schedule her timer ping (if she asked for one and quota allows) ----
    timer_scheduled = None
    if timer_req and allow_timer:
        session.add(TimerPing(
            chat_id=chat.id,
            due_ms=now_ms() + timer_req["minutes"] * 60_000,
            topic=timer_req["topic"],
        ))
        timer_scheduled = timer_req

    # 8. save affect ---------------------------------------------------------
    chat.affect = state.to_dict()
    await session.commit()

    # 后台离线维护:回复已提交,趁机检查是否该做梦(不阻塞本次响应)。
    _schedule_auto_dream()

    tier_key, tier_label = state.affection_tier()
    result = {
        "role": "assistant",
        "content": "\n".join(replies),   # 向后兼容:单字段拼接
        "messages": replies,             # 多消息:前端逐条展示
    }
    if settings.debug_panel:
        result["debug"] = {
            "thinking": thinking,
            "mode": state.mode,
            "events": {k: v for k, v in events.items() if not k.startswith("_")},
            "trace": trace,
            "open_loops": [l.content for l in state.open_loops],
            "grievances": [g.content for g in state.grievances if not g.resolved],
            "scalars": {
                "arousal": round(state.arousal, 2),
                "security": round(state.security, 2),
                "patience": state.patience,
                "warm_streak": state.warm_streak,
                "affection": round(state.affection, 1),
            },
            "affection_tier": {"key": tier_key, "label": tier_label},
            "tier_shift": events.get("_tier_shift"),
            "timer_scheduled": timer_scheduled,
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
            "tags_assigned": reply_mems[0].tags if reply_mems else [],
        }
    return result


# ── 定时器到点:对用户隐藏的 LLM 调用,生成主动消息 ────────────────────
_PROACTIVE_TMPL = (
    "之前聊天时你说过等会儿要来找他,你当时的备忘是:『{topic}』。\n"
    "现在时间到了。这条消息是【你主动发起】的——他并没有发新消息,"
    "不要假装在回复他。自然地衔接你说过要做的事,别用'我回来了''报告'这类机械开场;"
    "如果那件事现在看已经聊过了或不合适,就顺着你此刻的心情说点别的,"
    "但要让他感觉到:你记得你答应过的事。"
)


async def handle_timer_fire(
    session: AsyncSession, chat: Chat, topic: str, due_ms: int
) -> dict:
    """定时器到点的隐藏调用。没有他的新消息 → 不跑抽取/事件动力学,
    只做:时间效应 → L1 组装 → 生成(带主动情境)→ 落库 → 存状态。
    不提交(调用方把 ping 状态和消息放进同一个事务),返回 SSE 载荷。"""
    persona = Persona.from_dict(chat.persona) if chat.persona else PRESETS["anxious"]
    state = AffectState.from_dict(chat.affect) if chat.affect else AffectState.fresh(persona)

    prev_ts = state.last_ts
    dynamics.apply_time(state, persona, now=time.time())

    working = await l3_store.working_memory(session, chat.id, settings.working_memory_k)
    working_ids = {m.id for m in working}

    query = (topic or "").strip() or next(
        (m.content for m in reversed(working) if m.speaker == Speaker.user), ""
    )
    hits = []
    if query:
        hits = await retrieval.retrieve(
            session, query=query, chat_id=chat.id,
            top_k=settings.retrieval_top_k, axis="content",
            goal=chat.goal, exclude_ids=working_ids,
        )
    cherished = await l3_store.cherished_memories(session, chat.id)
    hot = await hot_memories(session, chat.id, limit=settings.retrieval_top_k)

    memory_block, _l1_dbg = l1_assembly.build_memory_block(
        cherished=cherished, hot=hot, retrieved=hits, exclude_ids=working_ids,
    )
    working_turns, working_summary = await l1_assembly.build_working_turns(working)
    if working_summary:
        memory_block = f"{memory_block}\n\n【更早对话摘要】\n{working_summary}".strip()

    tag_vocab = await tags.vocabulary_summary(session)
    core_identity = build_core_identity(persona, tag_vocab)

    system_prompt = injector.render(
        state, persona, core_identity=core_identity,
        memory_block=memory_block, goal=chat.goal,
        time_context=_time_context(prev_ts),
        proactive=_PROACTIVE_TMPL.format(topic=topic or "(她只说了等会儿来找他)"),
        allow_timer=False,   # 主动消息里不许再约闹钟,防止连环自触发
    )

    messages = [{"role": "system", "content": system_prompt}] + working_turns
    raw = await client.chat(messages, model=MODEL_PRO, temperature=0.85, thinking=False)
    raw, _ = _extract_timer(raw)   # 就算模型不听话吐了 timer 标签,也剥掉不调度
    thinking, replies = _parse_generation(raw)

    await _persist_agent_replies(session, chat.id, replies, thinking, state)
    chat.affect = state.to_dict()
    await session.flush()

    return {
        "type": "proactive",
        "chat_id": str(chat.id),
        "messages": replies,
        "mode": state.mode,
        "thinking": thinking if settings.debug_panel else "",
        "topic": topic,
    }
