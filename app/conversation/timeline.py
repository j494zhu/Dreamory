"""
情绪时间序列 — 可观测性的地基。

每轮对话(handle_message)/每次主动消息(handle_timer_fire)后把 AffectState
的全部物理量落一行 affect_snapshots。这是"这个改动有没有让角色更真实"从
肉眼感觉变成数据判断的前提:前端画曲线、健康度模块算模式震荡、将来做
参数扫描/加速老化测试都读这张表。

纯落库/查询,零 LLM。行很小(全定长列),不做保留期清理。
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.affect.state import AffectState
from app.models import AffectSnapshot


def record(session: AsyncSession, chat_id: uuid.UUID, state: AffectState, *,
           source: str = "message", events: dict | None = None) -> AffectSnapshot:
    """从 state 构造一行快照并 add 进当前事务(随本轮一起提交,不额外 commit)。"""
    ev = events or {}
    snap = AffectSnapshot(
        chat_id=chat_id,
        turn=state.turn,
        source=source,
        mode=state.mode,
        arousal=state.arousal,
        security=state.security,
        affection=state.affection,
        adrenaline=state.adrenaline,
        oxytocin=state.oxytocin,
        cortisol=state.cortisol,
        patience=state.patience,
        warm_streak=state.warm_streak,
        dull_streak=state.dull_streak,
        loop_pressure=state.loop_pressure(),
        grievances=sum(1 for g in state.grievances if not g.resolved),
        event=(ev.get("his_response_type") or "")[:24],
        bid=(ev.get("bid_in_her_last_msg") or "")[:24],
    )
    session.add(snap)
    return snap


async def history(
    session: AsyncSession, chat_id: uuid.UUID, *,
    limit: int = 500, since_ms: int | None = None,
) -> list[AffectSnapshot]:
    """最近 limit 条快照,时间升序(取尾部窗口再反转,老对话不用全表扫)。"""
    stmt = (
        select(AffectSnapshot)
        .where(AffectSnapshot.chat_id == chat_id)
        .order_by(AffectSnapshot.ts_ms.desc(), AffectSnapshot.id.desc())
        .limit(max(1, min(limit, 2000)))
    )
    if since_ms is not None:
        stmt = stmt.where(AffectSnapshot.ts_ms >= since_ms)
    rows = (await session.execute(stmt)).scalars().all()
    return list(reversed(rows))


def to_dict(s: AffectSnapshot) -> dict:
    return {
        "ts_ms": s.ts_ms, "turn": s.turn, "source": s.source, "mode": s.mode,
        "arousal": round(s.arousal, 3), "security": round(s.security, 3),
        "affection": round(s.affection, 2),
        "adrenaline": round(s.adrenaline, 3), "oxytocin": round(s.oxytocin, 3),
        "cortisol": round(s.cortisol, 3),
        "patience": s.patience, "warm_streak": s.warm_streak,
        "dull_streak": s.dull_streak, "loop_pressure": s.loop_pressure,
        "grievances": s.grievances, "event": s.event, "bid": s.bid,
    }
