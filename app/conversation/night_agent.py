"""
夜间代理 — 她睡着以后,后台替她"过完这一天"。

触发(全部代码门控):night_agent_enabled + 她的作息在睡觉 + 用户静默
≥ NIGHT_IDLE_HOURS + 距上次夜跑 ≥ NIGHT_MIN_GAP_HOURS(每晚最多一次)。

一次夜跑 = 一次 pro JSON 调用产出三样东西 + 两项纯代码维护:
  1. 蒸馏(distill):把当天对话流蒸馏成持久事实,写入 L3(kind=passage,
     打 tag)—— 这是 memory_kind.passage 的第一个生产者,补上记忆架构的缺环:
     原始流靠向量召回,蒸馏条目靠 tag 过滤,spec 里"给蒸馏后的事实打 tag,
     不给原始流逐条打"的分工从此成立。
  2. 日记(diary):以她的口吻写当日小结,进小本子(kind=diary),次日注入 L1
     —— 她醒来"记得昨天的心情"。
  3. 明日计划(plans):排 1~2 条 oneoff 日程(她自己定的);长期作息(routine)
     的修改锁在 NIGHT_AGENT_EDIT_ROUTINE 开关后,默认关。
  + 小本子收纳(notebook.housekeeping) + Dream(tag 维护,沿用现有触发判定)。

每步独立 try —— 一步失败绝不拖垮整晚。
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.affect.persona import PRESETS, Persona
from app.affect.state import AffectState
from app.config import settings
from app.conversation import notebook
from app.conversation import schedule as sched
from app.llm import client
from app.llm.client import MODEL_PRO
from app.memory import dream, l3_store, tags
from app.models import Chat, Memory, MemoryKind, ScheduleItem, Speaker, now_ms

logger = logging.getLogger(__name__)

MAX_FACTS = 6
FACT_MAX_LEN = 100
DIARY_MAX_LEN = 160
MAX_PLANS = 2
MAX_ROUTINE_EDITS = 2
MIN_MESSAGES_FOR_REFLECTION = 4       # 一天没聊几句就没什么可蒸馏的
TRANSCRIPT_LIMIT = 80                 # 夜跑最多回看的消息条数

_HM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


# ── 门控 ─────────────────────────────────────────────────────────────
async def should_run(session: AsyncSession, chat: Chat,
                     now_dt: datetime | None = None) -> bool:
    if not settings.night_agent_enabled:
        return False
    now_dt = now_dt or datetime.now()

    if now_ms() - (chat.last_night_run_ms or 0) < settings.night_min_gap_hours * 3600_000:
        return False

    items = await sched.load_active(session, chat.id)
    if not sched.is_sleeping(items, now_dt):
        return False

    last_msg = await session.scalar(
        select(func.max(Memory.ts_ms)).where(
            Memory.chat_id == chat.id, Memory.kind == MemoryKind.message
        )
    )
    if last_msg is None:   # 从没聊过,没什么可反思的
        return False
    return now_ms() - last_msg >= settings.night_idle_hours * 3600_000


# ── 校验(纯函数,可单测)───────────────────────────────────────────────
def _valid_hm(hm) -> bool:
    return isinstance(hm, str) and bool(_HM_RE.match(hm.strip()))


def validate_payload(data: dict) -> dict:
    """LLM 输出不可信:逐项清洗,坏的丢掉,绝不 raise。
    先校验后封顶 —— 上限裁剪若发生在校验前,靠前的坏条目会把靠后的好条目挤掉。"""
    facts = [
        s.strip()[:FACT_MAX_LEN]
        for s in (data.get("facts") or [])
        if isinstance(s, str) and s.strip()
    ][:MAX_FACTS]
    diary = data.get("diary")
    diary = diary.strip()[:DIARY_MAX_LEN] if isinstance(diary, str) and diary.strip() else None

    plans = []
    for p in (data.get("plans") or []):
        if len(plans) >= MAX_PLANS:
            break
        if not isinstance(p, dict):
            continue
        label = str(p.get("label") or "").strip()[:40]
        if label and _valid_hm(p.get("at_hm")):
            plans.append({"label": label, "at_hm": p["at_hm"].strip()})

    routines = []
    for r in (data.get("routine") or []):
        if len(routines) >= MAX_ROUTINE_EDITS:
            break
        if not isinstance(r, dict):
            continue
        label = str(r.get("label") or "").strip()[:40]
        days = r.get("days")
        days_ok = days is None or (
            isinstance(days, list) and days and all(isinstance(d, int) and 0 <= d <= 6 for d in days)
        )
        if label and _valid_hm(r.get("start_hm")) and _valid_hm(r.get("end_hm")) and days_ok:
            routines.append({"label": label, "start_hm": r["start_hm"].strip(),
                             "end_hm": r["end_hm"].strip(), "days": days})
    return {"facts": facts, "diary": diary, "plans": plans, "routines": routines}


def plan_due_ms(at_hm: str, now_dt: datetime) -> int:
    """明天 HH:MM 的毫秒时刻。"""
    h, m = at_hm.split(":")
    due = (now_dt + timedelta(days=1)).replace(hour=int(h), minute=int(m),
                                               second=0, microsecond=0)
    return int(due.timestamp() * 1000)


# ── 夜跑主体 ─────────────────────────────────────────────────────────
def _build_prompt(persona: Persona, state: AffectState, transcript: str,
                  schedule_desc: str, allow_routine: bool, now_dt: datetime) -> list[dict]:
    _, tier_label = state.affection_tier()
    routine_doc = (
        '"routine": [{"label": "睡觉", "start_hm": "00:30", "end_hm": "08:30", "days": null}]'
        "(只有确实需要调整长期作息时才给,一般为空数组)"
        if allow_routine else '"routine": []'
    )
    system = (
        "你在替一个角色做她的'睡前整理'。她今天和男朋友聊了下面这些;"
        "现在她睡了,你以她本人的视角把这一天收个尾。只输出 JSON:\n"
        "{\n"
        '  "facts": ["值得长期记住的事实,每条一句话(他的喜好/他生活里的事/你们说定的事),'
        f'最多{MAX_FACTS}条,没有就空数组"],\n'
        f'  "diary": "她口吻的当日日记,一小段({DIARY_MAX_LEN}字内):'
        '今天发生了什么、她的心情;没什么可写就 null",\n'
        '  "plans": [{"label": "明天要做的事(她自己的生活)", "at_hm": "HH:MM"}],\n'
        f"  {routine_doc}\n"
        "}\n"
        "要求:facts 写具体(带人名/数字),是聊天里真实出现的,不要编;"
        "diary 是私人日记,写心情不写流水账;plans 是她自己的生活安排(交稿/健身/见朋友),"
        f"最多{MAX_PLANS}条,和她的作息不冲突,不确定就给空数组。"
    )
    user = (
        f"她:{persona.name},{persona.profile}\n"
        f"他们的关系:{tier_label}\n"
        f"她的作息与已有安排:{schedule_desc}\n"
        f"现在:{now_dt.year}年{now_dt.month}月{now_dt.day}日 {now_dt:%H:%M}\n\n"
        f"今天的聊天记录:\n{transcript or '(今天没怎么聊)'}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def run_night(session: AsyncSession, chat: Chat, *, force: bool = False) -> dict:
    """一次完整夜跑。返回 report(干了什么)。调用方保证并发互斥。"""
    report: dict = {"ran": True}
    now_dt = datetime.now()
    persona = Persona.from_dict(chat.persona) if chat.persona else PRESETS["anxious"]
    state = AffectState.from_dict(chat.affect) if chat.affect else AffectState.fresh(persona)

    since = max(chat.last_night_run_ms or 0, now_ms() - 24 * 3600_000)
    msgs = (
        await session.execute(
            select(Memory)
            .where(Memory.chat_id == chat.id, Memory.kind == MemoryKind.message,
                   Memory.ts_ms >= since)
            .order_by(Memory.ts_ms.asc(), Memory.id.asc())
            .limit(TRANSCRIPT_LIMIT)
        )
    ).scalars().all()

    # 1+2+3. 蒸馏 / 日记 / 明日计划 —— 一次 pro JSON 调用
    if len(msgs) >= MIN_MESSAGES_FOR_REFLECTION or force:
        transcript = "\n".join(
            f"{'她' if m.speaker == Speaker.agent else '他'}: {m.content[:80]}" for m in msgs
        )
        items = await sched.load_active(session, chat.id)
        schedule_desc = "；".join(
            f"{i.label} {i.start_hm or ''}~{i.end_hm or ''}" if i.kind == "routine"
            else f"{i.label}(oneoff)" for i in items
        ) or "(未知)"
        try:
            raw = await client.chat_json(
                _build_prompt(persona, state, transcript, schedule_desc,
                              settings.night_agent_edit_routine, now_dt),
                model=MODEL_PRO, temperature=0.5,
                default={"facts": [], "diary": None, "plans": [], "routine": []},
            )
            payload = validate_payload(raw)

            for fact in payload["facts"]:
                mem = await l3_store.write_memory(
                    session, chat_id=chat.id, content=fact, speaker=Speaker.agent,
                    kind=MemoryKind.passage, commit=False,
                )
                await tags.assign_tags(session, mem)
            report["facts"] = len(payload["facts"])

            if payload["diary"]:
                await notebook.add_note(session, chat.id, payload["diary"], kind="diary")
            report["diary"] = bool(payload["diary"])

            for p in payload["plans"]:
                session.add(ScheduleItem(
                    chat_id=chat.id, kind="oneoff", label=p["label"],
                    due_ms=plan_due_ms(p["at_hm"], now_dt), source="night_agent",
                ))
            report["plans"] = len(payload["plans"])

            if settings.night_agent_edit_routine and payload["routines"]:
                for r in payload["routines"]:
                    # 同名 routine 替换(她调整自己的作息);不删别的
                    for old in items:
                        if old.kind == "routine" and old.label == r["label"]:
                            old.status = "cancelled"
                    session.add(ScheduleItem(
                        chat_id=chat.id, kind="routine", label=r["label"],
                        days=r["days"], start_hm=r["start_hm"], end_hm=r["end_hm"],
                        source="night_agent",
                    ))
                report["routine_edits"] = len(payload["routines"])
        except Exception:   # 反思失败,收纳和 Dream 照常
            logger.exception("night reflection failed (chat %s)", chat.id)
            report["reflection_error"] = True
    else:
        report["skipped_reflection"] = f"只有{len(msgs)}条消息,今天太安静"

    # 4. 小本子收纳(纯代码)
    try:
        report["notebook"] = await notebook.housekeeping(session, chat.id)
    except Exception:
        logger.exception("notebook housekeeping failed (chat %s)", chat.id)

    chat.last_night_run_ms = now_ms()
    await session.commit()

    # 5. 记忆健康体检(只读):有旗标 → 触发全局维护(强制跑一次 Dream)
    unhealthy = False
    try:
        from app.memory import health as health_mod

        hp = await health_mod.compute_health(session, chat)
        report["health"] = {"score": hp["score"],
                            "flags": [f["key"] for f in hp["flags"]]}
        unhealthy = bool(hp["flags"])
    except Exception:
        logger.exception("night health check failed (chat %s)", chat.id)

    # 6. Dream(tag 维护,自带提交;放最后,失败无所谓)。
    #    健康度亮旗时不等积压阈值,直接强制维护 —— spec"漂移阈值触发全局维护"。
    try:
        if settings.dream_enabled and (unhealthy or await dream.should_dream(session)):
            report["dream"] = await dream.run_dream(session, force=unhealthy)
    except Exception:
        logger.exception("night dream failed (chat %s)", chat.id)

    return report


# ── 后台服务(与 TimerService 同款生命周期)────────────────────────────
class NightService:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()   # 单飞:同一时刻只跑一个夜跑

    async def _tick(self) -> None:
        from app.db import SessionLocal

        async with SessionLocal() as session:
            cutoff = datetime.now() - timedelta(days=14)
            chats = (
                await session.execute(
                    select(Chat).where(Chat.last_active >= cutoff)
                )
            ).scalars().all()
            due = [c for c in chats if await should_run(session, c)]
            due_ids = [c.id for c in due]

        for chat_id in due_ids:
            if self._stop.is_set():
                return
            await self._run_one(chat_id)

    async def _run_one(self, chat_id: uuid.UUID) -> None:
        from app.db import SessionLocal
        from app.db_locks import LOCK_NIGHT, advisory_guard, chat_key

        async with self._lock:   # 进程内第一道闸
            try:
                # 跨进程单飞(per-chat):锁内重查 should_run,双保险
                async with advisory_guard(LOCK_NIGHT, chat_key(chat_id)) as acquired:
                    if not acquired:
                        return
                    async with SessionLocal() as session:
                        chat = await session.get(Chat, chat_id)
                        if chat is None or not await should_run(session, chat):
                            return   # 醒了/刚被别的进程跑过,让过
                        report = await run_night(session, chat)
                        logger.info("night agent ran for chat %s: %s", chat_id, report)
            except Exception:
                logger.exception("night agent failed (chat %s)", chat_id)

    async def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=settings.night_poll_seconds
                    )
                except asyncio.TimeoutError:
                    pass
                if self._stop.is_set():
                    return
                try:
                    await self._tick()
                except Exception:
                    logger.exception("night tick failed")
        except asyncio.CancelledError:
            pass

    def start(self) -> None:
        if self._task is None and settings.night_agent_enabled:
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


night_service = NightService()
