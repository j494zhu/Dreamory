"""Chat + messaging endpoints (+ per-chat SSE stream for proactive messages)."""
from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.affect.persona import PRESETS, Persona
from app.affect.state import AffectState
from app.conversation import pipeline
from app.conversation.bus import event_bus, sse_format
from app.db import SessionLocal, get_session
from app.memory import l3_store
from app.models import Chat, Memory, MemoryKind, TimerPing
from app.schemas import (
    ChatCreate,
    ChatOut,
    ChatSummary,
    ChatUpdate,
    MemoryOut,
    MessageIn,
    MessageOut,
)

router = APIRouter(prefix="/api/chats", tags=["chat"])

SSE_KEEPALIVE_S = 15.0


def _build_persona(body: ChatCreate | ChatUpdate, base: Persona | None = None) -> Persona:
    persona = base or (PRESETS.get(getattr(body, "preset", None) or "", None) or Persona())
    if body.persona:
        merged = persona.to_dict() | {k: v for k, v in body.persona.model_dump().items() if v is not None}
        persona = Persona.from_dict(merged)
    return persona


async def _get_chat(session: AsyncSession, chat_id: uuid.UUID) -> Chat:
    chat = await session.get(Chat, chat_id)
    if chat is None:
        raise HTTPException(404, "chat not found")
    return chat


@router.post("", response_model=ChatOut)
async def create_chat(body: ChatCreate, session: AsyncSession = Depends(get_session)):
    persona = _build_persona(body)
    state = AffectState.fresh(persona)
    chat = Chat(
        title=body.title,
        persona=persona.to_dict(),
        affect=state.to_dict(),
        goal=body.goal,
    )
    session.add(chat)
    await session.commit()
    await session.refresh(chat)
    return ChatOut(id=chat.id, title=chat.title, goal=chat.goal,
                   persona=chat.persona, affect=chat.affect)


@router.get("", response_model=list[ChatSummary])
async def list_chats(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(Chat).order_by(Chat.last_active.desc()))).scalars().all()
    return [ChatSummary(id=c.id, title=c.title, goal=c.goal) for c in rows]

 
@router.get("/{chat_id}", response_model=ChatOut)
async def get_chat(chat_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    chat = await _get_chat(session, chat_id)
    return ChatOut(id=chat.id, title=chat.title, goal=chat.goal,
                   persona=chat.persona, affect=chat.affect)


@router.patch("/{chat_id}", response_model=ChatOut)
async def update_chat(chat_id: uuid.UUID, body: ChatUpdate,
                      session: AsyncSession = Depends(get_session)):
    chat = await _get_chat(session, chat_id)
    if body.title is not None:
        chat.title = body.title
    if body.goal is not None:
        chat.goal = body.goal
    if body.persona is not None:
        persona = _build_persona(body, Persona.from_dict(chat.persona))
        chat.persona = persona.to_dict()
    await session.commit()
    await session.refresh(chat)
    return ChatOut(id=chat.id, title=chat.title, goal=chat.goal,
                   persona=chat.persona, affect=chat.affect)


@router.delete("/{chat_id}")
async def delete_chat(chat_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    chat = await _get_chat(session, chat_id)
    await session.delete(chat)
    await session.commit()
    return {"deleted": str(chat_id)}


@router.get("/{chat_id}/messages", response_model=list[MemoryOut])
async def get_messages(chat_id: uuid.UUID, limit: int = 200,
                       session: AsyncSession = Depends(get_session)):
    await _get_chat(session, chat_id)
    rows = (
        await session.execute(
            select(Memory)
            .where(Memory.chat_id == chat_id, Memory.kind == MemoryKind.message)
            .order_by(Memory.ts_ms.asc(), Memory.id.asc())   # 连发同毫秒时按 uuid7 保序
            .limit(limit)
        )
    ).scalars().all()
    return [
        MemoryOut(
            id=m.id, speaker=m.speaker.value, content=m.content, tags=m.tags,
            emotion_reasoning=m.emotion_reasoning, cherished=m.cherished,
            salience=m.salience, use_count=m.use_count, heat=m.heat, ts_ms=m.ts_ms,
        )
        for m in rows
    ]


@router.post("/{chat_id}/messages", response_model=MessageOut)
async def post_message(chat_id: uuid.UUID, body: MessageIn,
                       session: AsyncSession = Depends(get_session)):
    chat = await _get_chat(session, chat_id)
    if not body.content.strip():
        raise HTTPException(400, "empty message")
    result = await pipeline.handle_message(session, chat, body.content.strip())
    return MessageOut(**result)


# ── SSE: 服务端 → 浏览器的推送通道(定时器触发的主动消息走这里)────────
@router.get("/{chat_id}/events")
async def chat_events(chat_id: uuid.UUID):
    # 存在性检查用独立短会话:SSE 是长连接,不能占着连接池里的连接不放
    async with SessionLocal() as session:
        if await session.get(Chat, chat_id) is None:
            raise HTTPException(404, "chat not found")

    async def stream():
        q = event_bus.subscribe(chat_id)
        try:
            yield ": connected\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=SSE_KEEPALIVE_S)
                    yield sse_format(event)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"   # 心跳注释行,防代理断连
        finally:
            event_bus.unsubscribe(chat_id, q)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{chat_id}/timers")
async def get_timers(chat_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    """当前挂着的闹钟(调试/前端提示用)。"""
    await _get_chat(session, chat_id)
    rows = (
        await session.execute(
            select(TimerPing)
            .where(TimerPing.chat_id == chat_id, TimerPing.status == "pending")
            .order_by(TimerPing.due_ms.asc())
        )
    ).scalars().all()
    return [
        {"id": str(t.id), "due_ms": t.due_ms, "topic": t.topic, "status": t.status}
        for t in rows
    ]
