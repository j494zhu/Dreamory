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
    # 委托给可注入时钟:生产恒等于真实时间,模拟脚手架可拨快(见 app/clock.py)
    from app import clock
    return clock.now_ms()


class Speaker(str, enum.Enum):
    user = "user"
    agent = "agent"


class MemoryKind(str, enum.Enum):
    message = "message"       # raw conversational stream (tagged only by vector)
    passage = "passage"       # distilled fact/passage produced by Dream (gets tags)
    life_event = "life_event" # 她线下生活里发生的事(生活模拟器生成,即刻成为正史)
    # NOTE 旧库升级需手动: ALTER TYPE memory_kind ADD VALUE IF NOT EXISTS 'life_event';


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
    # 核心人格数据化(为"自我迭代"打地基):非空时覆盖 identity.py 编译的默认块。
    # 修改必须走 config_store.snapshot() 留版本,不允许静默改写。
    core_identity: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 夜间代理上次运行时刻(每晚最多一次的闸)
    last_night_run_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    # 0.6.1 测试期访问控制:每个 chat 一把钥匙(专属链接),不做用户系统。
    # ADMIN_TOKEN 未设置时不参与任何判断。
    access_token: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
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


# ──────────────────────────────────────────────────────────────────
#  Timer pings — 她自己约下的"过会儿来找他"。
#  生成端用 <timer minutes="X">topic</timer> 声明;后台调度器到点后发起一次
#  对用户隐藏的 LLM 调用,生成主动消息并经 SSE 推给前端。
#  持久化在表里(而非纯内存)是为了服务重启后闹钟不丢。
# ──────────────────────────────────────────────────────────────────
class TimerPing(Base):
    __tablename__ = "timer_pings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    chat_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chats.id", ondelete="CASCADE"), index=True
    )
    due_ms: Mapped[int] = mapped_column(BigInteger, index=True)   # 到点时间
    topic: Mapped[str] = mapped_column(Text, default="")          # 她的备忘:到时候要说什么
    # pending -> firing -> fired | failed
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    created_ms: Mapped[int] = mapped_column(BigInteger, default=now_ms)
    # v0.6 承诺兑现闭环:kind=commitment 的 ping 是"到点催"——触发前先查
    # 对应 open_loop 是否还挂着(loop_id),已兑现就静默完成,绝不空催。
    kind: Mapped[str] = mapped_column(String(16), default="timer")   # timer | commitment
    loop_id: Mapped[str | None] = mapped_column(String(16), nullable=True)


# ──────────────────────────────────────────────────────────────────
#  Chat config revisions — 自我迭代的地基:persona / core_identity / goal
#  的 append-only 快照史。任何一次配置变更前先落一条快照,崩了可回退。
# ──────────────────────────────────────────────────────────────────
class ChatRevision(Base):
    __tablename__ = "chat_revisions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    chat_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chats.id", ondelete="CASCADE"), index=True
    )
    rev: Mapped[int] = mapped_column(Integer)                     # 每 chat 递增
    data: Mapped[dict] = mapped_column(JSONB, default=dict)       # {persona, core_identity, goal}
    reason: Mapped[str] = mapped_column(Text, default="")         # 为什么改(user_edit / rollback / …)
    actor: Mapped[str] = mapped_column(String(16), default="user")  # user | model | system
    created_ms: Mapped[int] = mapped_column(BigInteger, default=now_ms)


# ──────────────────────────────────────────────────────────────────
#  Schedule — 她的日程表。routine = 长期作息(睡觉/上班,按星期+时段重复),
#  oneoff = 一次性事项(交稿/约了朋友,有确切到点时间)。
#  日程不进记忆三槽:活动窗口被编译成 L1 的独立【你的生活】块。
# ──────────────────────────────────────────────────────────────────
class ScheduleItem(Base):
    __tablename__ = "schedule_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    chat_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chats.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16), default="routine")  # routine | oneoff
    label: Mapped[str] = mapped_column(String(200))                   # "睡觉" / "给甲方交稿"
    # routine: 周几生效(0=周一…6=周日,NULL=每天)+ "HH:MM" 起止(可跨午夜)
    days: Mapped[list[int] | None] = mapped_column(ARRAY(Integer), nullable=True)
    start_hm: Mapped[str | None] = mapped_column(String(5), nullable=True)
    end_hm: Mapped[str | None] = mapped_column(String(5), nullable=True)
    # oneoff: 到点时间
    due_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)  # active|done|cancelled
    source: Mapped[str] = mapped_column(String(16), default="default")  # default | life_sim | model
    created_ms: Mapped[int] = mapped_column(BigInteger, default=now_ms)


# ──────────────────────────────────────────────────────────────────
#  Life events — 生活模拟器离线预生成的"她线下经历的事"。
#  生成即正史:内容同时写入 L3(kind=life_event,memory_id 回链),细节只生成
#  一次,之后靠检索复述 —— 这是话题转移不"越编越露馅"的关键。
#  本表只承载种子调度元数据(状态/情绪色彩/发生时间),内容不二存。
# ──────────────────────────────────────────────────────────────────
# ──────────────────────────────────────────────────────────────────
#  Affect snapshots — 情绪状态的时间序列(可观测性)。
#  每轮对话/每次主动消息后落一行,前端画好感度/激素/安全感曲线,
#  健康度模块用它算模式震荡。全是定长物理量 → 存实列不存 JSONB,可直接聚合。
# ──────────────────────────────────────────────────────────────────
class AffectSnapshot(Base):
    __tablename__ = "affect_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    chat_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chats.id", ondelete="CASCADE"), index=True
    )
    ts_ms: Mapped[int] = mapped_column(BigInteger, default=now_ms)
    turn: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(8), default="message")  # message | timer

    mode: Mapped[str] = mapped_column(String(16), default="neutral")
    arousal: Mapped[float] = mapped_column(Float, default=0.0)
    security: Mapped[float] = mapped_column(Float, default=0.0)
    affection: Mapped[float] = mapped_column(Float, default=0.0)
    adrenaline: Mapped[float] = mapped_column(Float, default=0.0)
    oxytocin: Mapped[float] = mapped_column(Float, default=0.0)
    cortisol: Mapped[float] = mapped_column(Float, default=0.0)
    patience: Mapped[int] = mapped_column(Integer, default=0)
    warm_streak: Mapped[int] = mapped_column(Integer, default=0)
    dull_streak: Mapped[int] = mapped_column(Integer, default=0)
    loop_pressure: Mapped[int] = mapped_column(Integer, default=0)   # 挂起回路权重和
    grievances: Mapped[int] = mapped_column(Integer, default=0)      # 未解决旧账数

    # 本轮发生了什么(标注曲线上的事件点)
    event: Mapped[str] = mapped_column(String(24), default="")   # his_response_type
    bid: Mapped[str] = mapped_column(String(24), default="")     # bid_in_her_last_msg

    __table_args__ = (
        Index("ix_affect_snapshots_chat_ts", "chat_id", "ts_ms"),
    )


# ──────────────────────────────────────────────────────────────────
#  Turn logs — 感知/决策日志(0.6.1,测试期审计)。
#  affect_snapshots 记"状态变成了什么",这张表记"为什么":extractor 怎么分类
#  (误判审计的原料)、dynamics 触发了哪些规则、注入了哪些块、guardrail 是否
#  介入、工具调了什么。纯观测,可整表清空重来,不是任何功能的数据源。
#  铁律 1:消息内容不二存,只存 L3 memory id,审计接口时再 join。
# ──────────────────────────────────────────────────────────────────
class TurnLog(Base):
    __tablename__ = "turn_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    chat_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chats.id", ondelete="CASCADE"), index=True
    )
    ts_ms: Mapped[int] = mapped_column(BigInteger, default=now_ms)
    turn: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(8), default="message")  # message | timer

    # 内容指针(不二存):他这条消息 / 她这轮发出的各条回复,都在 L3
    user_mem_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memories.id", ondelete="SET NULL"), nullable=True
    )
    reply_mem_ids: Mapped[list] = mapped_column(JSONB, default=list)   # [str(uuid), …]

    # 感知:extractor 校验后的输出(去 _ 前缀内部键),confidence 单列便于过滤
    events: Mapped[dict] = mapped_column(JSONB, default=dict)
    confidence: Mapped[str] = mapped_column(String(8), default="high", index=True)

    # 决策:dynamics 触发的规则(人话 trace)、进入的模式、注入块标题清单
    trace: Mapped[list] = mapped_column(JSONB, default=list)
    mode: Mapped[str] = mapped_column(String(16), default="neutral")
    prompt_blocks: Mapped[list] = mapped_column(JSONB, default=list)   # 各块首行
    system_prompt: Mapped[str] = mapped_column(Text, default="")       # 全文,TURN_LOG_FULL_PROMPT 开启时

    # 生成侧:检索命中摘要(id+分数+截断)、工具调用、guardrail 介入记录
    retrieved: Mapped[list] = mapped_column(JSONB, default=list)
    tools: Mapped[list] = mapped_column(JSONB, default=list)
    guard: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # 用户反馈:「这里不对劲」一键旗标(测试期最便宜的高信号反馈)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    flag_note: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (
        Index("ix_turn_logs_chat_ts", "chat_id", "ts_ms"),
    )


# ──────────────────────────────────────────────────────────────────
#  Notes — 她的小本子(model-curated 记忆,借鉴 memory-tool 的文件式思路)。
#  日记由夜间代理写;note 由她在对话里 write_note 随手记。
#  这是独立的文档类存储(她写的笔记本),不是 memories 的副本 —— 铁律 1 无涉。
# ──────────────────────────────────────────────────────────────────
class Note(Base):
    __tablename__ = "notes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    chat_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chats.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16), default="note", index=True)  # note | diary
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)  # active | archived
    created_ms: Mapped[int] = mapped_column(BigInteger, default=now_ms)


class LifeEvent(Base):
    __tablename__ = "life_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    chat_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chats.id", ondelete="CASCADE"), index=True
    )
    memory_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memories.id", ondelete="SET NULL"), nullable=True
    )
    valence: Mapped[float] = mapped_column(Float, default=0.0)    # 情绪色彩 -1(糟心)~+1(开心)
    occurs_ms: Mapped[int] = mapped_column(BigInteger, index=True)  # 事件发生时间
    # fresh(可当话题种子) -> mentioned(已注入过,不再当新鲜事) | expired(过了保鲜期)
    status: Mapped[str] = mapped_column(String(16), default="fresh", index=True)
    injected_count: Mapped[int] = mapped_column(Integer, default=0)
    created_ms: Mapped[int] = mapped_column(BigInteger, default=now_ms)
