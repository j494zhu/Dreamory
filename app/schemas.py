"""Pydantic request/response models for the API surface."""
from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


# ── Persona ──────────────────────────────────────────────────────────
class PersonaIn(BaseModel):
    name: str | None = None
    profile: str | None = None
    style: str | None = None
    anxiety: float | None = None
    avoidance: float | None = None
    expressiveness: float | None = None
    base_patience: int | None = None
    security_baseline: float | None = None


# ── Chat ─────────────────────────────────────────────────────────────
class ChatCreate(BaseModel):
    title: str = "新对话"
    preset: str | None = Field(None, description="secure | anxious | avoidant")
    persona: PersonaIn | None = None
    goal: str | None = None


class ChatUpdate(BaseModel):
    title: str | None = None
    goal: str | None = None
    persona: PersonaIn | None = None


class ChatOut(BaseModel):
    id: uuid.UUID
    title: str
    goal: str | None
    persona: dict
    affect: dict


class ChatSummary(BaseModel):
    id: uuid.UUID
    title: str
    goal: str | None


# ── Messages ─────────────────────────────────────────────────────────
class MessageIn(BaseModel):
    content: str


class MessageOut(BaseModel):
    role: str
    content: str                       # 向后兼容:多条消息用 \n 拼接
    messages: list[str] = []           # 连发消息,前端逐条渲染
    debug: dict | None = None


class MemoryOut(BaseModel):
    id: uuid.UUID
    speaker: str
    content: str
    tags: list[str]
    emotion_reasoning: dict
    cherished: bool
    salience: float
    use_count: int
    heat: float
    ts_ms: int


# ── Retrieval / tags / dream ─────────────────────────────────────────
class RetrieveIn(BaseModel):
    query: str
    axis: str = "content"        # content | emotion | both
    top_k: int | None = None
    tags_any: list[str] | None = None
    use_goal: bool = True


class RetrieveHit(BaseModel):
    id: uuid.UUID
    content: str
    score: float
    axis: str
    speaker: str
    tags: list[str]


class TagSeedIn(BaseModel):
    name: str
    facet: str = "topic"
    example_texts: list[str]
    description: str | None = None


class TagOut(BaseModel):
    name: str
    facet: str
    member_count: int
    description: str | None
