"""
感知/决策日志(0.6.1)— 测试期审计的地基。

affect_snapshots(timeline.py)回答"状态变成了什么",这里回答"为什么":
  - extractor 对他这条消息怎么分类(confidence 单列,误判审计的原料);
  - dynamics 触发了哪些规则(人话 trace);
  - 这一轮给生成端注入了哪些块(prompt_blocks;全文按需 TURN_LOG_FULL_PROMPT);
  - 检索命中了什么、工具调了什么、guardrail 有没有介入;
  - 用户按没按「这里不对劲」(flagged / flag_note)。

铁律 1:消息内容不二存 —— 只存 L3 memory id,审计接口读取时再 join。
纯落库/查询,零 LLM;整表可清空重来,不是任何功能的数据源。
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Memory, TurnLog

# JSONB 字段的防爆截断(日志不该比正文还大)
_MAX_TRACE_ITEMS = 40
_MAX_TOOL_ITEMS = 20
_SNIPPET = 80


def block_heads(system_prompt: str, limit: int = 40) -> list[str]:
    """把 injector 编译出的 system prompt 拆回块标题清单(每块首行截断)。
    审计时看"这轮她脑子里被塞了什么"用,不需要全文。"""
    heads = []
    for b in system_prompt.split("\n\n"):
        line = b.strip().splitlines()[0].strip() if b.strip() else ""
        if line:
            heads.append(line[:limit])
    return heads


def summarize_hits(hits) -> list[dict]:
    """检索命中 → 审计摘要(id + 双分数 + 轴 + 内容截断)。
    「L1 检索命中都是什么鬼」这类问题靠它回放。"""
    return [
        {
            "memory_id": str(h.memory.id),
            "score": round(h.score, 3),
            "raw": round(h.raw, 3),
            "axis": h.axis,
            "content": (h.memory.content or "")[:_SNIPPET],
        }
        for h in hits
    ]


def record(
    session: AsyncSession, chat_id: uuid.UUID, *, turn: int, mode: str,
    source: str = "message",
    user_mem_id: uuid.UUID | None = None,
    reply_mem_ids: list[uuid.UUID] | None = None,
    events: dict | None = None,
    trace: list[str] | None = None,
    system_prompt: str = "", store_full_prompt: bool = False,
    retrieved: list[dict] | None = None,
    tools: list[dict] | None = None,
    guard: dict | None = None,
) -> TurnLog:
    """构造一行日志并 add 进当前事务(随本轮一起提交,不额外 commit)。"""
    ev = {k: v for k, v in (events or {}).items() if not k.startswith("_")}
    log = TurnLog(
        chat_id=chat_id,
        turn=turn,
        source=source,
        mode=mode,
        user_mem_id=user_mem_id,
        reply_mem_ids=[str(m) for m in (reply_mem_ids or [])],
        events=ev,
        confidence=(ev.get("confidence") or "high")[:8],
        trace=list(trace or [])[:_MAX_TRACE_ITEMS],
        prompt_blocks=block_heads(system_prompt),
        system_prompt=system_prompt if store_full_prompt else "",
        retrieved=retrieved or [],
        tools=list(tools or [])[:_MAX_TOOL_ITEMS],
        guard=guard,
    )
    session.add(log)
    return log


async def history(
    session: AsyncSession, chat_id: uuid.UUID, *,
    limit: int = 100, flagged_only: bool = False,
    low_confidence_only: bool = False,
) -> list[TurnLog]:
    """最近 limit 条日志,时间升序(尾部窗口再反转)。"""
    stmt = (
        select(TurnLog)
        .where(TurnLog.chat_id == chat_id)
        .order_by(TurnLog.ts_ms.desc(), TurnLog.id.desc())
        .limit(max(1, min(limit, 1000)))
    )
    if flagged_only:
        stmt = stmt.where(TurnLog.flagged.is_(True))
    if low_confidence_only:
        stmt = stmt.where(TurnLog.confidence == "low")
    rows = (await session.execute(stmt)).scalars().all()
    return list(reversed(rows))


async def flag(
    session: AsyncSession, chat_id: uuid.UUID, turn_log_id: uuid.UUID,
    note: str = "",
) -> TurnLog | None:
    """「这里不对劲」:给一条日志打旗。找不到(或不属于该 chat)返回 None。"""
    log = await session.get(TurnLog, turn_log_id)
    if log is None or log.chat_id != chat_id:
        return None
    log.flagged = True
    if note:
        log.flag_note = note[:2000]
    return log


async def to_dict(session: AsyncSession, log: TurnLog, *, resolve_content: bool = True) -> dict:
    """审计视图:按 id 回 L3 取原文(内容不二存,读取时 join)。"""
    out = {
        "id": str(log.id), "ts_ms": log.ts_ms, "turn": log.turn,
        "source": log.source, "mode": log.mode,
        "events": log.events, "confidence": log.confidence,
        "trace": log.trace, "prompt_blocks": log.prompt_blocks,
        "retrieved": log.retrieved, "tools": log.tools, "guard": log.guard,
        "flagged": log.flagged, "flag_note": log.flag_note or None,
        "system_prompt": log.system_prompt or None,
    }
    if resolve_content:
        ids = [uuid.UUID(i) for i in (log.reply_mem_ids or [])]
        if log.user_mem_id:
            ids.append(log.user_mem_id)
        contents: dict[str, str] = {}
        if ids:
            rows = (await session.execute(
                select(Memory.id, Memory.content).where(Memory.id.in_(ids))
            )).all()
            contents = {str(i): c for i, c in rows}
        out["user_content"] = contents.get(str(log.user_mem_id)) if log.user_mem_id else None
        out["replies"] = [contents.get(i, "") for i in (log.reply_mem_ids or [])]
    return out
