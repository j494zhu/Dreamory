"""
日程表 — 她的生活节奏。

两层结构:
  routine(长期作息): 睡觉/工作等,按星期+时段重复,可跨午夜("00:30"~"08:30")。
  oneoff(当前日程):  一次性事项(交稿/约了朋友),有确切到点时间,过点自动落为 done。

职责:
  - render_block(): 编译 L1【你的生活】块 —— "你现在按理在做什么、接下来有什么安排"。
    日程不占记忆三槽预算,是和【时间感知】同级的状态块。
  - is_sleeping()/wake_ms(): 供 TimerService 把撞进睡眠时段的闹钟顺延到醒来之后
    (她不会凌晨三点蹦出来发消息 —— 除非你把作息删了)。
  - seed_defaults(): 新对话冷启动的通用作息;生活模拟器之后可以往里加 oneoff。

纯代码,零 LLM。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ScheduleItem, now_ms

UPCOMING_HORIZON_MS = 48 * 3600 * 1000   # 只把 48 小时内的安排编进 L1
_WEEKDAYS = "一二三四五六日"

# 冷启动通用作息(生活模拟器/后续版本可按 persona 调整)
DEFAULT_ROUTINES = [
    {"label": "睡觉", "days": None, "start_hm": "00:30", "end_hm": "08:30"},
    {"label": "上班/上课", "days": [0, 1, 2, 3, 4], "start_hm": "09:30", "end_hm": "18:00"},
]


async def seed_defaults(session: AsyncSession, chat_id: uuid.UUID) -> None:
    """给新对话种上通用作息。幂等交给调用方(只在建 chat 时调一次)。"""
    for r in DEFAULT_ROUTINES:
        session.add(ScheduleItem(
            chat_id=chat_id, kind="routine", label=r["label"],
            days=r["days"], start_hm=r["start_hm"], end_hm=r["end_hm"],
            source="default",
        ))


async def load_active(session: AsyncSession, chat_id: uuid.UUID) -> list[ScheduleItem]:
    rows = (
        await session.execute(
            select(ScheduleItem).where(
                ScheduleItem.chat_id == chat_id, ScheduleItem.status == "active"
            )
        )
    ).scalars().all()
    return list(rows)


# ── routine 匹配(纯函数,可单测)──────────────────────────────────────
def _parse_hm(hm: str) -> int:
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def _routine_active(item: ScheduleItem, dt: datetime) -> bool:
    """此刻是否落在 routine 的时段内。跨午夜时段(start>end)归 *开始那天* 的星期:
    周五 23:00~03:00 覆盖周六凌晨。"""
    if not item.start_hm or not item.end_hm:
        return False
    start, end = _parse_hm(item.start_hm), _parse_hm(item.end_hm)
    minutes = dt.hour * 60 + dt.minute

    if start <= end:  # 普通时段
        return (item.days is None or dt.weekday() in item.days) and start <= minutes < end
    # 跨午夜:今天的下半段(>=start)按今天算,凌晨的上半段(<end)算前一天开始的
    if minutes >= start:
        return item.days is None or dt.weekday() in item.days
    if minutes < end:
        yesterday = (dt - timedelta(days=1)).weekday()
        return item.days is None or yesterday in item.days
    return False


def current_routine(items: list[ScheduleItem], now: datetime) -> ScheduleItem | None:
    """此刻生效的 routine(多个同时命中取最先建的;睡觉优先——它最不该被忽略)。"""
    active = [i for i in items if i.kind == "routine" and _routine_active(i, now)]
    if not active:
        return None
    # created_ms 是 insert 默认值,未入库的实例可能还是 None → 兜底 0
    active.sort(key=lambda i: (0 if i.label == "睡觉" else 1, i.created_ms or 0))
    return active[0]


def is_sleeping(items: list[ScheduleItem], now: datetime) -> bool:
    cur = current_routine(items, now)
    return cur is not None and cur.label == "睡觉"


def wake_ms(items: list[ScheduleItem], now: datetime) -> int | None:
    """当前睡眠时段的结束时刻(ms)。不在睡觉则 None。"""
    cur = current_routine(items, now)
    if cur is None or cur.label != "睡觉" or not cur.end_hm:
        return None
    end = _parse_hm(cur.end_hm)
    wake = now.replace(hour=end // 60, minute=end % 60, second=0, microsecond=0)
    if wake <= now:  # 跨午夜:醒来在"明天"
        wake += timedelta(days=1)
    return int(wake.timestamp() * 1000)


# ── L1 编译 ──────────────────────────────────────────────────────────
def _fmt_due(due_ms: int, now: datetime) -> str:
    dt = datetime.fromtimestamp(due_ms / 1000)
    if dt.date() == now.date():
        return f"今天{dt:%H:%M}"
    if dt.date() == (now + timedelta(days=1)).date():
        return f"明天{dt:%H:%M}"
    return f"周{_WEEKDAYS[dt.weekday()]}{dt:%H:%M}"


def render_block(items: list[ScheduleItem], now: datetime | None = None) -> str:
    """编译【你的生活】块。空日程返回空串(块不注入)。"""
    now = now or datetime.now()
    lines: list[str] = []

    cur = current_routine(items, now)
    if cur is not None:
        if cur.label == "睡觉":
            lines.append(
                "按你的作息,你这会儿本来在睡觉。如果他这时候来找你,你是被消息吵醒的"
                "——带着睡意回,可以嘟囔、可以困,别精神饱满得像白天。"
            )
        else:
            lines.append(
                f"你这会儿正在{cur.label}。回消息是忙里偷闲,"
                "节奏可以慢一点、短一点,也可以自然提到手头正在做的事。"
            )

    upcoming = sorted(
        (i for i in items
         if i.kind == "oneoff" and i.due_ms and 0 <= i.due_ms - now_ms() <= UPCOMING_HORIZON_MS),
        key=lambda i: i.due_ms,
    )[:3]
    if upcoming:
        plans = "；".join(f"{_fmt_due(i.due_ms, now)} {i.label}" for i in upcoming)
        lines.append(f"你接下来的安排:{plans}。这些是你自己的生活,聊到相关话题可以自然提起。")

    return "\n".join(lines)


async def expire_past_oneoffs(session: AsyncSession, chat_id: uuid.UUID) -> None:
    """过点的 oneoff 落为 done(轻量维护,随对话顺带跑)。"""
    rows = (
        await session.execute(
            select(ScheduleItem).where(
                ScheduleItem.chat_id == chat_id, ScheduleItem.kind == "oneoff",
                ScheduleItem.status == "active", ScheduleItem.due_ms < now_ms(),
            )
        )
    ).scalars().all()
    for r in rows:
        r.status = "done"
