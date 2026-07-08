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
        + 时间感知(现在几点、距上次说话多久)+ 日程【你的生活】+ 话题种子
  6. generate                (LLM② pro, two-stage <thinking>/<reply> brain-theatre;
                              可多条 <reply> 连发。0.2.2: 有界 agent loop ——
                              她可以先 search_memory / grep_memory 翻记忆、
                              set_timer 定闹钟,再作答;轮次用尽强制作答)
  7. persist her replies to L3 (content + reasoning + emotion) + hot-path tag them
  8. save affect state (+ schedule timer ping if she asked for one)
  9. 后台维护(不阻塞): auto-dream + 生活模拟器补写新鲜事

There is a second entrypoint, handle_timer_fire(): 定时器到点后的隐藏 LLM 调用,
没有他的新消息,不跑抽取/动力学,只做 时间效应 → L1 组装 → 生成 → 落库,
产出的主动消息经 SSE 推给前端。
"""
from __future__ import annotations

import asyncio
import logging
import random
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
from app.conversation import evolution, guardrail, life_sim, notebook, timeline, tools
from app.conversation import schedule as sched
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
# 容错:标签常未闭合(尤其工具轮之后模型偷懒,只吐半个 <reply> 或整段 <thinking>)。
# 实盘教训(0.2.3):set_timer 可靠触发后工具轮变多,未闭合标签会把裸 <reply>/<thinking>
# 泄给用户。以下正则处理未闭合情形,_STRAY_TAG_RE 是最后一道"绝不泄裸标签"的保险。
_UNCLOSED_THINKING_RE = re.compile(r"<thinking>(.*?)(?=<reply>|$)", re.S | re.I)
_REPLY_OPEN_RE = re.compile(r"<reply>(.*?)(?=<reply>|<thinking>|</reply>|$)", re.S | re.I)
_STRAY_TAG_RE = re.compile(r"</?(?:reply|thinking)\s*>", re.I)

MAX_REPLIES_PER_TURN = 4   # 连发上限,和注入器里的口径一致


def _parse_generation(raw: str) -> tuple[str, list[str]]:
    """<thinking> + 一到多个 <reply> → (内心独白, [消息1, 消息2, …])。
    对未闭合/残缺标签容错:无论如何都不把裸 <reply>/<thinking> 标签泄给用户。"""
    # 1. thinking:优先闭合块,退而求其次未闭合的 <thinking>…(到第一个 <reply> 或结尾)
    tm = _THINKING_RE.search(raw)
    if tm:
        thinking = tm.group(1).strip()
        body = _THINKING_RE.sub("", raw)
    else:
        um = _UNCLOSED_THINKING_RE.search(raw)
        if um:
            thinking = um.group(1).strip()
            body = raw[:um.start()] + raw[um.end():]
        else:
            thinking, body = "", raw

    # 2. replies:先取闭合 <reply>…</reply>,没有再退回未闭合 <reply>…
    replies = [r.strip() for r in _REPLY_RE.findall(body) if r.strip()]
    if not replies and "<reply>" in body.lower():
        replies = [seg.strip() for seg in _REPLY_OPEN_RE.findall(body) if seg.strip()]

    # 3. 兜底清掉任何残留裸标签,绝不泄给用户
    replies = [t for t in (_STRAY_TAG_RE.sub("", r).strip() for r in replies) if t]
    if replies:
        return thinking, replies[:MAX_REPLIES_PER_TURN]

    # 4. 完全没有 reply:整段清标签后当单条消息(模型只吐了独白时,独白即回复,总比失声好)
    cleaned = _STRAY_TAG_RE.sub("", body).strip()
    return thinking, [cleaned or _STRAY_TAG_RE.sub("", raw).strip() or "…"]


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
    async with _dream_lock:   # 进程内第一道闸(零成本)
        from app.db import SessionLocal
        from app.db_locks import LOCK_DREAM, advisory_guard

        try:
            # 跨进程单飞:别的 worker 正在做梦就让过
            async with advisory_guard(LOCK_DREAM) as acquired:
                if not acquired:
                    return
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


# ── 生成(0.2.2: 有界 agent loop)──────────────────────────────────────
WARM_SHIFT_PROB = 0.2   # warm 模式下即使话题不淡,也偶尔想分享点自己的事


async def _apply_guardrail(
    persona, messages: list[dict], raw_clean: str,
    thinking: str, replies: list[str], timer_req: dict | None,
) -> tuple[str, list[str], dict | None, dict | None]:
    """输出侧守护:回复里检出角色崩坏 → 一次带隐藏纠正注入的重生成。
    重试仍崩也照发(绝不失声、绝不吐机械警告),结果记进 debug。
    返回 (thinking, replies, timer_req, guard_info|None)。"""
    if not settings.guardrail_enabled:
        return thinking, replies, timer_req, None
    reasons = guardrail.detect_break(replies)
    if not reasons:
        return thinking, replies, timer_req, None
    try:
        retry_msgs = messages + [
            {"role": "assistant", "content": raw_clean},
            {"role": "user", "content": guardrail.corrective_note(reasons, persona)},
        ]
        raw2 = await client.chat(retry_msgs, model=MODEL_PRO, temperature=0.7, thinking=False)
        raw2, timer2 = _extract_timer(raw2)
        thinking2, replies2 = _parse_generation(raw2)
        if replies2:
            still = guardrail.detect_break(replies2)
            info = {"triggered": reasons, "clean_after_retry": not still}
            return thinking2, replies2, (timer_req or timer2), info
    except Exception:  # noqa: BLE001 — 守护自身失败就发原文,绝不失声
        logger.exception("guardrail retry failed; keeping original reply")
    return thinking, replies, timer_req, {"triggered": reasons, "clean_after_retry": False}


async def _generate_with_tools(
    session: AsyncSession, chat: Chat, messages: list[dict], *,
    allow_timer: bool, exclude_ids: set[uuid.UUID], pending_timers: int,
    allow_notes: bool = False,
) -> tuple[str, list[dict], dict | None]:
    """有界工具循环:最多 tool_max_rounds 次往返,之后 tool_choice=none 强制作答。
    任何一环失败都整体降级为无工具的单次生成 —— 工具是增强,不是依赖。
    返回 (raw文本, 工具调用trace, 工具定下的timer|None)。"""
    specs = tools.build_specs(allow_timer, allow_notes)
    trace: list[dict] = []
    timer_set: dict | None = None
    pending = pending_timers
    convo = list(messages)
    excl = set(exclude_ids)

    try:
        for _ in range(max(1, settings.tool_max_rounds)):
            msg = await client.chat_tools(
                convo, tools=specs, temperature=0.85, thinking=False,
            )
            if not msg.tool_calls:
                return (msg.content or ""), trace, timer_set
            convo.append(client.tool_message_to_dict(msg))
            for tc in msg.tool_calls:
                outcome = await tools.dispatch(
                    session, chat=chat, name=tc.function.name,
                    arguments=tc.function.arguments or "{}",
                    exclude_ids=excl, pending_timer_count=pending,
                )
                if outcome.timer:
                    timer_set = outcome.timer
                    pending += 1
                excl |= set(outcome.hit_ids)   # 已喂过的记忆不再重复返回
                trace.append({
                    "tool": tc.function.name,
                    "args": tc.function.arguments,
                    "result": outcome.text[:160],
                })
                convo.append({"role": "tool", "tool_call_id": tc.id, "content": outcome.text})
        # 轮次用尽:禁用工具,强制吐最终回复
        msg = await client.chat_tools(
            convo, tools=specs, tool_choice="none", temperature=0.85, thinking=False,
        )
        return (msg.content or ""), trace, timer_set
    except Exception:  # noqa: BLE001 — 工具链路故障不能让她失声
        logger.exception("tool loop failed; falling back to plain generation")
        raw = await client.chat(messages, model=MODEL_PRO, temperature=0.85, thinking=False)
        return raw, trace, timer_set


async def _maybe_topic_seed(
    session: AsyncSession, chat: Chat, state: AffectState,
) -> str:
    """注意力转移的确定性门:话题淡了(dull_streak)或 warm 模式低概率,
    且距上次转移过了冷却轮数 → 从生活事件池取一条新鲜事当种子。
    素材是预生成的正史,这里零 LLM。"""
    if not settings.life_sim_enabled or state.mode not in ("warm", "neutral"):
        return ""
    if state.turn - state.last_shift_turn < settings.seed_cooldown_turns:
        return ""
    want = state.dull_streak >= 2 or (
        state.mode == "warm" and random.random() < WARM_SHIFT_PROB
    )
    if not want:
        return ""
    seed = await life_sim.pick_seed(session, chat.id)
    if seed:
        state.last_shift_turn = state.turn
        state.dull_streak = 0
    return seed or ""


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
    core_identity = build_core_identity(persona, tag_vocab, override=chat.core_identity)
    if working_summary:
        memory_block = f"{memory_block}\n\n【更早对话摘要】\n{working_summary}".strip()

    pending_timers = await _pending_timer_count(session, chat.id)
    allow_timer = settings.timer_enabled and pending_timers < settings.timer_max_pending

    # 日程【你的生活】:她此刻按理在做什么 + 接下来的安排(不占记忆三槽预算)
    schedule_block = ""
    if settings.schedule_enabled:
        await sched.expire_past_oneoffs(session, chat.id)
        items = await sched.load_active(session, chat.id)
        if not items:   # 0.2.0 的旧对话没有作息:懒种默认值
            await sched.seed_defaults(session, chat.id)
            items = await sched.load_active(session, chat.id)
        schedule_block = sched.render_block(items)

    # 话题种子:代码决定何时转,事件池提供素材,LLM 只决定怎么说
    topic_seed = await _maybe_topic_seed(session, chat, state)

    # 检索置信度:自动想起的东西很模糊 → 提醒她可以主动去翻(工具开启时才有意义)
    memory_hint = ""
    if settings.tools_enabled and (not hits or hits[0].score < settings.retrieval_confidence):
        memory_hint = (
            "(提示:这一轮自动想起的内容很少或很模糊——如果他说的事你没印象,"
            "先搜一下再回,别不懂装懂,也别凭空编。)"
        )

    # 守护层【底线】:第四面墙 + 能力边界;本轮被试探(persona_attack)时追加点破
    boundary_block = ""
    if settings.guardrail_enabled:
        boundary_block = guardrail.render_boundary_block(
            persona, under_attack=bool(events.get("persona_attack")),
        )

    # 她的小本子:日记 + 随手记(model-curated,夜间代理维护)
    notebook_block = ""
    if settings.notes_enabled:
        notebook_block = await notebook.render_block(session, chat.id)
    allow_notes = settings.notes_enabled and settings.tools_enabled

    system_prompt = injector.render(
        state, persona, core_identity=core_identity,
        memory_block=memory_block, goal=chat.goal,
        time_context=_time_context(prev_ts, first_turn=first_turn),
        allow_timer=allow_timer,
        schedule_block=schedule_block, topic_seed=topic_seed,
        allow_tools=settings.tools_enabled, memory_hint=memory_hint,
        boundary_block=boundary_block, notebook_block=notebook_block,
        allow_notes=allow_notes,
    )

    # 6. generate (LLM②) -----------------------------------------------------
    # thinking=False: the "脑内剧场" inner monologue is an in-band roleplay <thinking>
    # block, NOT the model's native reasoning — keep native reasoning off so it
    # doesn't suppress the in-band block (and to save tokens/latency).
    messages = [{"role": "system", "content": system_prompt}] + working_turns
    tool_trace: list[dict] = []
    tool_timer: dict | None = None
    if settings.tools_enabled:
        l1_ids = {m.id for m in cherished} | {m.id for m in hot} | {h.memory.id for h in hits}
        raw, tool_trace, tool_timer = await _generate_with_tools(
            session, chat, messages, allow_timer=allow_timer,
            exclude_ids=working_ids | l1_ids, pending_timers=pending_timers,
            allow_notes=allow_notes,
        )
    else:
        raw = await client.chat(messages, model=MODEL_PRO, temperature=0.85, thinking=False)
    raw, timer_req = _extract_timer(raw)          # <timer> 标签兜底(工具未开/模型偷懒)
    thinking, replies = _parse_generation(raw)    # 一到多条连发消息

    # 6.5 输出侧守护:检出角色崩坏 → 一次纠正重生成(绝不吐机械警告)
    thinking, replies, timer_req, guard_info = await _apply_guardrail(
        persona, messages, raw, thinking, replies, timer_req,
    )

    # 7. persist her replies to L3 (content + reasoning + emotion) + tag -----
    cherish, salience = _salience_from_events(events, state)
    reply_mems = await _persist_agent_replies(
        session, chat.id, replies, thinking, state, cherish=cherish, salience=salience,
    )

    # 7.5 schedule her timer ping (tool 已直接入库;<timer> 标签是兜底路径) ----
    timer_scheduled = tool_timer
    quota_left = settings.timer_max_pending - pending_timers - (1 if tool_timer else 0)
    if timer_req and settings.timer_enabled and quota_left > 0:
        session.add(TimerPing(
            chat_id=chat.id,
            due_ms=now_ms() + timer_req["minutes"] * 60_000,
            topic=timer_req["topic"],
        ))
        timer_scheduled = timer_req

    # 8. save affect + timeline snapshot --------------------------------------
    chat.affect = state.to_dict()
    timeline.record(session, chat.id, state, source="message", events=events)
    await session.commit()

    # 后台离线维护:回复已提交,趁机检查是否该做梦/补写生活(不阻塞本次响应)。
    _schedule_auto_dream()
    life_sim.schedule_auto_sim(chat.id)

    tier_key, tier_label = state.affection_tier()

    # 好感度里程碑 → persona 演化(后台,append-only + 快照,每档一次)
    shift = events.get("_tier_shift")
    if shift and shift.get("direction") == "up":
        evolution.schedule_evolution(chat.id, tier_key)
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
            "hormones": {
                "adrenaline": round(state.adrenaline, 2),
                "oxytocin": round(state.oxytocin, 2),
                "cortisol": round(state.cortisol, 2),
            },
            "affection_tier": {"key": tier_key, "label": tier_label},
            "tier_shift": events.get("_tier_shift"),
            "timer_scheduled": timer_scheduled,
            "dull_streak": state.dull_streak,
            "topic_seed": topic_seed or None,
            "schedule": schedule_block or None,
            "tools": tool_trace,
            "guardrail": guard_info,
            "notebook": notebook_block or None,
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
    core_identity = build_core_identity(persona, tag_vocab, override=chat.core_identity)

    # 主动消息也活在她的日程里;没有明确备忘时,拿一条新鲜事当素材
    schedule_block = ""
    if settings.schedule_enabled:
        schedule_block = sched.render_block(await sched.load_active(session, chat.id))
    topic_seed = ""
    if not (topic or "").strip() and settings.life_sim_enabled:
        topic_seed = await life_sim.pick_seed(session, chat.id) or ""

    boundary_block = ""
    if settings.guardrail_enabled:
        boundary_block = guardrail.render_boundary_block(persona)
    notebook_block = ""
    if settings.notes_enabled:
        notebook_block = await notebook.render_block(session, chat.id)

    system_prompt = injector.render(
        state, persona, core_identity=core_identity,
        memory_block=memory_block, goal=chat.goal,
        time_context=_time_context(prev_ts),
        proactive=_PROACTIVE_TMPL.format(topic=topic or "(她只说了等会儿来找他)"),
        allow_timer=False,   # 主动消息里不许再约闹钟,防止连环自触发
        schedule_block=schedule_block, topic_seed=topic_seed,
        allow_tools=settings.tools_enabled,
        boundary_block=boundary_block, notebook_block=notebook_block,
    )

    messages = [{"role": "system", "content": system_prompt}] + working_turns
    if settings.tools_enabled:
        l1_ids = {m.id for m in cherished} | {m.id for m in hot} | {h.memory.id for h in hits}
        raw, _trace, _t = await _generate_with_tools(
            session, chat, messages, allow_timer=False,   # specs 里不含 set_timer
            exclude_ids=working_ids | l1_ids,
            pending_timers=settings.timer_max_pending,    # 双保险:quota 已满
        )
    else:
        raw = await client.chat(messages, model=MODEL_PRO, temperature=0.85, thinking=False)
    raw, _ = _extract_timer(raw)   # 就算模型不听话吐了 timer 标签,也剥掉不调度
    thinking, replies = _parse_generation(raw)

    # 主动消息同样过守护层(它也是用户可见的)
    thinking, replies, _, _guard = await _apply_guardrail(
        persona, messages, raw, thinking, replies, None,
    )

    await _persist_agent_replies(session, chat.id, replies, thinking, state)
    chat.affect = state.to_dict()
    timeline.record(session, chat.id, state, source="timer")
    await session.flush()

    return {
        "type": "proactive",
        "chat_id": str(chat.id),
        "messages": replies,
        "mode": state.mode,
        "thinking": thinking if settings.debug_panel else "",
        "topic": topic,
    }
