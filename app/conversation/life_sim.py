"""
生活模拟器 — 她线下的人生,离线预生成。

为什么存在:话题转移显得假,根源不是"何时转",而是模型没有屏外生活,
事件在转移那一刻现编,细节无根,下次提起必然漂移。这里把事件提前生成、
持久化、生成即写入 L3 成为正史(kind=life_event)——细节只生成一次,
之后靠检索复述,永远不会"越编越露馅"。

三权分立(与 affect 引擎同一哲学):
  代码决定何时转移话题(dynamics.dull_streak 等确定性信号,见 pipeline);
  本模块提供素材(话题种子);
  LLM 只决定怎么说。

运行方式:与 auto-dream 同款 —— 每轮对话提交后检查 should_simulate(),
新鲜事件不足且距上次生成够久时,后台补写一批(不阻塞回复)。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import clock
from app.config import settings
from app.llm import client
from app.llm.client import MODEL_PRO
from app.memory import l3_store, tags
from app.models import Chat, LifeEvent, Memory, MemoryKind, ScheduleItem, Speaker, now_ms

logger = logging.getLogger(__name__)

FRESH_WINDOW_MS = 36 * 3600 * 1000    # "新鲜"= 36 小时内发生
SEED_MAX_AGE_MS = 48 * 3600 * 1000    # 种子最长可用 48 小时
EXPIRE_AFTER_MS = 72 * 3600 * 1000    # 72 小时后落为 expired
MAX_EVENTS_PER_RUN = 3
MAX_PLANS_PER_RUN = 2


# ── 触发判定 ─────────────────────────────────────────────────────────
async def should_simulate(session: AsyncSession, chat_id: uuid.UUID) -> bool:
    if not settings.life_sim_enabled:
        return False
    fresh = (
        await session.scalar(
            select(func.count()).select_from(LifeEvent).where(
                LifeEvent.chat_id == chat_id,
                LifeEvent.status == "fresh",
                LifeEvent.occurs_ms >= now_ms() - FRESH_WINDOW_MS,
            )
        )
    ) or 0
    if fresh >= settings.life_sim_fresh_target:
        return False
    last_created = await session.scalar(
        select(func.max(LifeEvent.created_ms)).where(LifeEvent.chat_id == chat_id)
    )
    min_gap_ms = int(settings.life_sim_min_interval_hours * 3600 * 1000)
    return last_created is None or now_ms() - last_created >= min_gap_ms


# ── 生成 ─────────────────────────────────────────────────────────────
def _schedule_desc(items: list[ScheduleItem]) -> str:
    parts = []
    for i in items:
        if i.kind == "routine" and i.start_hm and i.end_hm:
            days = "每天" if i.days is None else "周" + "、".join("一二三四五六日"[d] for d in i.days)
            parts.append(f"{days} {i.start_hm}~{i.end_hm} {i.label}")
        elif i.kind == "oneoff" and i.due_ms:
            dt = datetime.fromtimestamp(i.due_ms / 1000)
            parts.append(f"{dt.month}月{dt.day}日{dt:%H:%M} {i.label}")
    return "；".join(parts) or "(未知)"


async def _recent_events_desc(session: AsyncSession, chat_id: uuid.UUID) -> str:
    """最近 7 天已生成的生活事件(给 LLM 看,避免重复/冲突)。内容住在 L3,按 memory_id 回取。"""
    rows = (
        await session.execute(
            select(LifeEvent, Memory.content)
            .join(Memory, LifeEvent.memory_id == Memory.id)
            .where(
                LifeEvent.chat_id == chat_id,
                LifeEvent.occurs_ms >= now_ms() - 7 * 86_400_000,
            )
            .order_by(LifeEvent.occurs_ms.desc())
            .limit(10)
        )
    ).all()
    if not rows:
        return "(还没有)"
    lines = []
    for ev, content in rows:
        dt = datetime.fromtimestamp(ev.occurs_ms / 1000)
        lines.append(f"- {dt.month}月{dt.day}日: {content}")
    return "\n".join(lines)


def _build_prompt(persona_desc: str, schedule_desc: str, events_desc: str,
                  convo_tail: str, now: datetime) -> list[dict]:
    system = (
        "你在为一个角色补写她线下生活的时间线。她是一个真实生活着的人,不聊天的时候"
        "也在过自己的日子。你生成的事件会成为她人生的正史(以后她会当作亲身经历谈起),"
        "所以必须遵守:\n"
        "1. 与她的人设、作息、已有事件严格一致,绝不重复、绝不矛盾;\n"
        "2. 以平凡的小事为主(工作里的小波折、吃到的东西、看到的路人、朋友的一句话),"
        "偶尔才有一件带明显情绪的事;要有具体细节(人名/地点/数字),但每条不超过80字;\n"
        "3. 第一人称『我』写,像她自己会在心里记下的那种句子;\n"
        "4. hours_ago 表示发生在几小时前(0~36 的数),要和她的作息对得上"
        "(睡觉时段不会发生白天的事);\n"
        "5. valence 是这件事的情绪色彩,-1(很糟心)~1(很开心),小事就给 ±0.3 以内;\n"
        "6. plans 是她接下来 2~48 小时内的安排(可为空数组),label 不超过 20 字。\n"
        '只输出 JSON: {"events": [{"content": "...", "hours_ago": 5, "valence": 0.2}], '
        '"plans": [{"label": "...", "in_hours": 30}]}\n'
        f"events 最多 {MAX_EVENTS_PER_RUN} 条,plans 最多 {MAX_PLANS_PER_RUN} 条。"
    )
    user = (
        f"她的人设:{persona_desc}\n\n"
        f"她的作息与已有安排:{schedule_desc}\n\n"
        f"她最近已经发生过的事(不要重复/冲突):\n{events_desc}\n\n"
        f"最近的聊天片段(供参考生活语境,不要把聊天内容当成线下事件):\n{convo_tail or '(无)'}\n\n"
        f"现在是 {now.year}年{now.month}月{now.day}日 {now:%H:%M}。补写她这段时间的生活。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def run_life_sim(session: AsyncSession, chat: Chat) -> dict:
    """生成一批生活事件:LLM 补写 → 校验 → 写 LifeEvent + L3 正史 + 打标签 + oneoff 日程。"""
    from app.affect.persona import Persona
    from app.conversation import schedule as sched

    persona = Persona.from_dict(chat.persona) if chat.persona else Persona()
    items = await sched.load_active(session, chat.id)
    events_desc = await _recent_events_desc(session, chat.id)
    working = await l3_store.working_memory(session, chat.id, 6)
    convo_tail = "\n".join(
        f"{'她' if m.speaker == Speaker.agent else '他'}: {m.content[:60]}" for m in working
    )

    now = clock.now_dt()
    data = await client.chat_json(
        _build_prompt(f"{persona.name},{persona.profile}", _schedule_desc(items),
                      events_desc, convo_tail, now),
        model=MODEL_PRO, temperature=0.9, default={"events": [], "plans": []},
    )

    written, plans_added = 0, 0
    for raw in (data.get("events") or [])[:MAX_EVENTS_PER_RUN]:
        content = str(raw.get("content") or "").strip()
        if not content or len(content) > 200:
            continue
        try:
            hours_ago = min(36.0, max(0.0, float(raw.get("hours_ago", 3))))
            valence = min(1.0, max(-1.0, float(raw.get("valence", 0.0))))
        except (TypeError, ValueError):
            hours_ago, valence = 3.0, 0.0
        occurs = now_ms() - int(hours_ago * 3600 * 1000)

        # 生成即正史:内容唯一一份落进 L3(可被向量/grep 检索),种子表只持 id
        mem = await l3_store.write_memory(
            session, chat_id=chat.id, content=content, speaker=Speaker.agent,
            kind=MemoryKind.life_event, commit=False,
        )
        mem.ts_ms = occurs           # 记忆时间 = 事件发生时间,邻近查询才对得上
        await tags.assign_tags(session, mem)
        session.add(LifeEvent(
            chat_id=chat.id, memory_id=mem.id, valence=valence, occurs_ms=occurs,
        ))
        written += 1

    for raw in (data.get("plans") or [])[:MAX_PLANS_PER_RUN]:
        label = str(raw.get("label") or "").strip()
        if not label or len(label) > 40:
            continue
        try:
            in_hours = min(48.0, max(2.0, float(raw.get("in_hours", 24))))
        except (TypeError, ValueError):
            continue
        session.add(ScheduleItem(
            chat_id=chat.id, kind="oneoff", label=label,
            due_ms=now_ms() + int(in_hours * 3600 * 1000), source="life_sim",
        ))
        plans_added += 1

    # 保鲜期维护:过老的 fresh 落为 expired
    stale = (
        await session.execute(
            select(LifeEvent).where(
                LifeEvent.chat_id == chat.id, LifeEvent.status == "fresh",
                LifeEvent.occurs_ms < now_ms() - EXPIRE_AFTER_MS,
            )
        )
    ).scalars().all()
    for ev in stale:
        ev.status = "expired"

    await session.commit()
    return {"events": written, "plans": plans_added, "expired": len(stale)}


async def recent_event_texts(session: AsyncSession, chat_id: uuid.UUID,
                             limit: int = 3) -> list[str]:
    """最近 48h 的生活事件原文(不限状态)——confabulation 的表面归因素材:
    '是白天甲方改稿闹的,跟你没关系'。真实正史做错误归因,具体又绝不穿帮。"""
    rows = (
        await session.execute(
            select(Memory.content)
            .join(LifeEvent, LifeEvent.memory_id == Memory.id)
            .where(
                LifeEvent.chat_id == chat_id,
                LifeEvent.occurs_ms >= now_ms() - SEED_MAX_AGE_MS,
            )
            .order_by(LifeEvent.occurs_ms.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


# ── 话题种子选取(热路径,零 LLM)───────────────────────────────────────
async def pick_seed(session: AsyncSession, chat_id: uuid.UUID) -> str | None:
    """挑一条最值得说的新鲜事:|valence| 大者优先,再看新近。
    注入即标记 mentioned —— 同一件事绝不第二次被当成'新鲜事'递进去
    (她的记忆里有它,后续靠检索自然复述)。"""
    row = (
        await session.execute(
            select(LifeEvent, Memory.content)
            .join(Memory, LifeEvent.memory_id == Memory.id)
            .where(
                LifeEvent.chat_id == chat_id,
                LifeEvent.status == "fresh",
                LifeEvent.occurs_ms >= now_ms() - SEED_MAX_AGE_MS,
            )
            .order_by(func.abs(LifeEvent.valence).desc(), LifeEvent.occurs_ms.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    event, content = row
    event.status = "mentioned"
    event.injected_count += 1
    return content


# ── 后台调度(与 auto-dream 同款:单飞锁 + 独立 session)────────────────
_sim_lock = asyncio.Lock()
_bg_tasks: set[asyncio.Task] = set()


async def _auto_sim(chat_id: uuid.UUID) -> None:
    if not settings.life_sim_enabled or _sim_lock.locked():
        return
    async with _sim_lock:   # 进程内第一道闸
        from app.db import SessionLocal
        from app.db_locks import LOCK_LIFE_SIM, advisory_guard, chat_key

        try:
            # 跨进程单飞(per-chat):别的 worker 正在给这只 chat 补写就让过
            async with advisory_guard(LOCK_LIFE_SIM, chat_key(chat_id)) as acquired:
                if not acquired:
                    return
                async with SessionLocal() as s:
                    if not await should_simulate(s, chat_id):
                        return
                    chat = await s.get(Chat, chat_id)
                    if chat is None:
                        return
                    report = await run_life_sim(s, chat)
                    logger.info("life-sim ran for chat %s: %s", chat_id, report)
        except Exception:  # 后台补写绝不能把主流程带崩
            logger.exception("life-sim failed (chat %s)", chat_id)


def schedule_auto_sim(chat_id: uuid.UUID) -> None:
    """Fire-and-forget;持有引用防 GC。"""
    if not settings.life_sim_enabled:
        return
    task = asyncio.create_task(_auto_sim(chat_id))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
