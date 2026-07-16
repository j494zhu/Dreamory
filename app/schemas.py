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
    insight: float | None = None   # 内省力 0~1:自我解释有多接近真实动因


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
    # 数据化的核心人格文本(非空覆盖 identity.py 的出厂编译;变更自动留版本快照)
    core_identity: str | None = None


class ChatOut(BaseModel):
    id: uuid.UUID
    title: str
    goal: str | None
    persona: dict
    affect: dict
    core_identity: str | None = None
    # 该 chat 的钥匙(测试期专属链接用;能读到本对象说明请求方本就持有钥匙或是 admin)
    access_token: str | None = None


class RevisionOut(BaseModel):
    rev: int
    reason: str
    actor: str
    created_ms: int
    data: dict


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
    turn_id: str | None = None         # 本轮感知/决策日志 id(「不对劲」反馈回指)
    debug: dict | None = None


class TurnFlagIn(BaseModel):
    note: str = ""                     # 可选的一句话说明(哪里不对劲)


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
