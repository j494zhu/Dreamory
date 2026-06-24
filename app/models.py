"""
ORM models == the physical layout of L3 (the cold, infinite, single source of truth)
plus the orthogonal Tag registry.

Iron laws encoded here:
  1. Content lives exactly once (memories.content). Other layers hold only ids.
  2. Vectors encode pure content / pure emotion-reasoning — never tags/time/speaker.
     Those metadata are side-car columns used as WHERE filters at retrieval time.
  3. UUIDv7 primary key (identity + coarse time order); a dedicated ts_ms column
     carries an independent B-tree index for range/nearest-time queries.
"""
from __future__ import annotations

import enum
import time
import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid_extensions import uuid7  # provided by the `uuid7` package

from app.config import settings
from app.db import Base

DIM = settings.embedding_dim


def now_ms() -> int:
    return int(time.time() * 1000)


class Speaker(str, enum.Enum):
    user = "user"
    agent = "agent"


class MemoryKind(str, enum.Enum):
    message = "message"   # raw conversational stream (tagged only by vector)
    passage = "passage"   # distilled fact/passage produced by Dream (gets tags)


# ──────────────────────────────────────────────────────────────────
#  Chat session: holds persona + serialized affect state (derived-ish,
#  but the relationship narrative lives here as the working anchor). 
# ──────────────────────────────────────────────────────────────────
class Chat(Base):
    __tablename__ = "chats"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    title: Mapped[str] = mapped_column(String(200), default="新对话")
    persona: Mapped[dict] = mapped_column(JSONB, default=dict)   # Persona.to_dict()
    affect: Mapped[dict] = mapped_column(JSONB, default=dict)    # AffectState.to_dict()
    goal: Mapped[str | None] = mapped_column(Text, nullable=True)  # L1 "当前目标"
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_active: Mapped[object] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    memories: Mapped[list["Memory"]] = relationship(
        back_populates="chat", cascade="all, delete-orphan"
    )


# ──────────────────────────────────────────────────────────────────
#  L3 main table — the only place full content is stored.
# ──────────────────────────────────────────────────────────────────
class Memory(Base):
    __tablename__ = "memories"

    # identity + coarse time order (uuid7 lexicographic ≈ chronological)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    chat_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chats.id", ondelete="CASCADE"), index=True
    )

    # precise time — independent B-tree column; never parse the UUID for this.
    ts_ms: Mapped[int] = mapped_column(BigInteger, default=now_ms, index=True)

    content: Mapped[str] = mapped_column(Text)          # the message body, unique copy
    speaker: Mapped[Speaker] = mapped_column(Enum(Speaker, name="speaker"))
    kind: Mapped[MemoryKind] = mapped_column(
        Enum(MemoryKind, name="memory_kind"), default=MemoryKind.message
    )

    # emotion + reasoning ("脑内剧场") raw text, stored as JSON {reasoning, emotion}
    emotion_reasoning: Mapped[dict] = mapped_column(JSONB, default=dict)

    # side-car metadata: filter axis, never embedded
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    pending: Mapped[bool] = mapped_column(Boolean, default=True)  # no tag met threshold yet

    # 刻骨铭心 — cherished/scarring memories that pin into L1
    cherished: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    salience: Mapped[float] = mapped_column(Float, default=0.0)   # |emotional impact|

    # heat fields — batch-written by the background flusher
    last_used: Mapped[int] = mapped_column(BigInteger, default=now_ms)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    heat: Mapped[float] = mapped_column(Float, default=0.0, index=True)  # time-decayed score

    # VectorDB_1 (content axis) and VectorDB_2 (emotion/reasoning axis)
    content_vec: Mapped[list[float] | None] = mapped_column(Vector(DIM), nullable=True)
    emotion_vec: Mapped[list[float] | None] = mapped_column(Vector(DIM), nullable=True)

    chat: Mapped["Chat"] = relationship(back_populates="memories")

    __table_args__ = (
        # GIN over the tags array — hard category axis (tag filtering)
        Index("ix_memories_tags_gin", "tags", postgresql_using="gin"),
        # HNSW ANN indexes for sub-second cosine retrieval on both axes
        Index(
            "ix_memories_content_vec_hnsw",
            "content_vec",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"content_vec": "vector_cosine_ops"},
        ),
        Index(
            "ix_memories_emotion_vec_hnsw",
            "emotion_vec",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"emotion_vec": "vector_cosine_ops"},
        ),
    )


# ──────────────────────────────────────────────────────────────────
#  Tag registry — orthogonal index spanning L3. NOT a capacity layer.
#  controlled vocabulary + per-tag centroid prototype vector.
# ──────────────────────────────────────────────────────────────────
class Tag(Base):
    __tablename__ = "tags"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    # facet/dimension this tag belongs to: domain | entity | type | project | time
    facet: Mapped[str] = mapped_column(String(32), default="topic", index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # centroid = mean of member memories' content vectors (the prototype)
    centroid: Mapped[list[float] | None] = mapped_column(Vector(DIM), nullable=True)
    member_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TagAlias(Base):
    """Old-tag -> canonical-tag remap table maintained by Dream, so legacy
    queries/filters keep resolving after merges/splits."""
    __tablename__ = "tag_aliases"

    alias: Mapped[str] = mapped_column(String(128), primary_key=True)
    canonical: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
