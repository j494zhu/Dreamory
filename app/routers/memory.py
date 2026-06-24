"""Memory introspection: retrieval, tags, and a manual Dream trigger."""
from __future__ import annotations

import uuid 

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.memory import dream as dream_mod
from app.memory import retrieval, tags
from app.models import Chat
from app.schemas import RetrieveHit, RetrieveIn, TagOut, TagSeedIn

router = APIRouter(prefix="/api", tags=["memory"])


@router.post("/chats/{chat_id}/retrieve", response_model=list[RetrieveHit])
async def retrieve(chat_id: uuid.UUID, body: RetrieveIn,
                   session: AsyncSession = Depends(get_session)):
    chat = await session.get(Chat, chat_id)
    if chat is None:
        raise HTTPException(404, "chat not found")
    hits = await retrieval.retrieve(
        session, query=body.query, chat_id=chat_id, top_k=body.top_k,
        axis=body.axis, tags_any=body.tags_any,
        goal=chat.goal if body.use_goal else None,
        record=False,   # introspection shouldn't heat memories
    )
    return [
        RetrieveHit(
            id=h.memory.id, content=h.memory.content, score=round(h.score, 4),
            axis=h.axis, speaker=h.memory.speaker.value, tags=h.memory.tags,
        )
        for h in hits
    ]


@router.get("/tags", response_model=list[TagOut])
async def list_tags(session: AsyncSession = Depends(get_session)):
    rows = await tags.get_vocabulary(session)
    return [
        TagOut(name=t.name, facet=t.facet, member_count=t.member_count, description=t.description)
        for t in rows
    ]


@router.post("/tags/seed", response_model=TagOut)
async def seed_tag(body: TagSeedIn, session: AsyncSession = Depends(get_session)):
    tag = await tags.seed_tag(
        session, name=body.name, facet=body.facet,
        example_texts=body.example_texts, description=body.description,
    )
    await session.commit()
    return TagOut(name=tag.name, facet=tag.facet, member_count=tag.member_count,
                  description=tag.description)


@router.post("/dream/run")
async def run_dream(force: bool = True, session: AsyncSession = Depends(get_session)):
    """Manually kick a Dream cycle. (Auto-run stays OFF per spec; force defaults
    to True here so the endpoint is usable for inspection/testing.)"""
    return await dream_mod.run_dream(session, force=force)
