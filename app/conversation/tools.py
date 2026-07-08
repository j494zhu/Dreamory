"""
工具协议 — 把"她能做的事"正规化成 OpenAI function-calling 工具。

原则:
  - 工具只在生成路径(LLM②)可用,热路径(抽取/打标签)依旧零工具零发散。
  - search_memory 走双轴向量检索(可加 tag/时间过滤),grep_memory 走
    ILIKE 纯文本精确检索(名字/数字/原话这类 embedding 会糊掉的东西),
    set_timer 取代旧的 <timer> 标签(标签解析仍保留兜底)。
  - dispatch() 返回给 tool role 的结果永远是紧凑中文文本 —— 模型读的是
    "回忆起来的内容",不是 JSON dump。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.memory import l3_store, retrieval
from app.models import Memory, MemoryKind, Speaker, TimerPing, now_ms

MAX_HITS_PER_CALL = 6
# search_memory 结果的相关性下限。没有它,exclude_ids 排掉真命中之后,
# 后续搜索会把池子里剩下的低分"填充记忆"当成果喂回去,模型误以为还有料可挖,
# 把轮数烧在冗余检索上。低于下限 → 明确告知"没有更相关的了",让她停手作答。
SEARCH_MIN_SCORE = 0.40


# ── 工具 schema ───────────────────────────────────────────────────────
def build_specs(allow_timer: bool, allow_notes: bool = False) -> list[dict]:
    specs = [
        {
            "type": "function",
            "function": {
                "name": "search_memory",
                "description": (
                    "在你的长期记忆里回想。按含义搜(axis=content),或按当时的心情搜"
                    "(axis=emotion,如'我感到被冷落的时候')。一次没想起来可以换个说法再搜。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "要回想的内容,用一句自然语言描述"},
                        "axis": {"type": "string", "enum": ["content", "emotion", "both"],
                                 "description": "content=按内容含义;emotion=按当时心情;默认 content"},
                        "newer_than_days": {"type": "integer", "description": "只翻最近 N 天的记忆(可选)"},
                        "older_than_days": {"type": "integer", "description": "只翻 N 天以前的记忆(可选)"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "grep_memory",
                "description": (
                    "在记忆原文里精确找一个词(名字、数字、地点、他说过的原话)。"
                    "按含义想不起来、但记得关键词时用这个。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keyword": {"type": "string", "description": "要找的关键词(原文包含匹配)"},
                        "speaker": {"type": "string", "enum": ["user", "agent", "any"],
                                    "description": "只找他说的(user)/你说的(agent)/都找(any,默认)"},
                        "newer_than_days": {"type": "integer", "description": "只翻最近 N 天(可选)"},
                        "older_than_days": {"type": "integer", "description": "只翻 N 天以前(可选)"},
                    },
                    "required": ["keyword"],
                },
            },
        },
    ]
    if allow_notes:
        specs.append(
            {
                "type": "function",
                "function": {
                    "name": "write_note",
                    "description": (
                        "往你的私人小本子里记一条(他的喜好、你们说好的事、你自己想做的事)。"
                        "小本子每轮都会带在身上,记下的事你不会忘。挑值得记的记。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string",
                                        "description": "要记的内容,一句话,不超过120字"},
                        },
                        "required": ["content"],
                    },
                },
            }
        )
    if allow_timer:
        specs.append(
            {
                "type": "function",
                "function": {
                    "name": "set_timer",
                    "description": (
                        "定一个真实的闹钟:X 分钟后你会收到提醒并主动给他发消息。"
                        "你说了'等我一会儿''过会儿来找你',或他让你过段时间去找他,就必须定;"
                        "不打算主动去找就别定。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "minutes": {"type": "integer", "description": "多少分钟后(1~1440)"},
                            "memo": {"type": "string", "description": "到时候要跟他说什么(你的备忘)"},
                        },
                        "required": ["minutes", "memo"],
                    },
                },
            }
        )
    return specs


# ── dispatch ─────────────────────────────────────────────────────────
@dataclass
class ToolOutcome:
    text: str                      # 喂回 tool role 的结果文本
    timer: dict | None = None      # set_timer 成功时的 {minutes, topic}(debug 用)
    hit_ids: list[uuid.UUID] = field(default_factory=list)


def _ago(ts_ms: int) -> str:
    gap = max(0, now_ms() - ts_ms) / 1000
    if gap < 3600:
        return f"{int(gap // 60)}分钟前"
    if gap < 86400:
        return f"{int(gap // 3600)}小时前"
    dt = datetime.fromtimestamp(ts_ms / 1000)
    return f"{dt.month}月{dt.day}日({int(gap // 86400)}天前)"


def _fmt_memory(m: Memory) -> str:
    if m.kind == MemoryKind.life_event:
        who = "我(生活里)"
    else:
        who = "我" if m.speaker == Speaker.agent else "他"
    return f"- {_ago(m.ts_ms)} {who}: {m.content}"


def _days_range(args: dict) -> tuple[int | None, int | None]:
    """newer_than_days/older_than_days → (ts_min_ms, ts_max_ms)。"""
    ts_min = ts_max = None
    try:
        if args.get("newer_than_days"):
            ts_min = now_ms() - int(args["newer_than_days"]) * 86_400_000
        if args.get("older_than_days"):
            ts_max = now_ms() - int(args["older_than_days"]) * 86_400_000
    except (TypeError, ValueError):
        pass
    return ts_min, ts_max


def _relevant(hits: list) -> list:
    """按相关性下限过滤(纯函数,可单测)。"""
    return [h for h in hits if h.score >= SEARCH_MIN_SCORE]


async def _do_search(session: AsyncSession, chat, args: dict,
                     exclude_ids: set[uuid.UUID]) -> ToolOutcome:
    query = (args.get("query") or "").strip()
    if not query:
        return ToolOutcome("(搜索词是空的,想一句具体的再搜)")
    axis = args.get("axis") if args.get("axis") in ("content", "emotion", "both") else "content"
    ts_min, ts_max = _days_range(args)

    hits = await retrieval.retrieve(
        session, query=query, chat_id=chat.id, top_k=MAX_HITS_PER_CALL,
        axis=axis, goal=chat.goal, exclude_ids=exclude_ids,
        ts_min_ms=ts_min, ts_max_ms=ts_max,
    )
    if not hits:
        return ToolOutcome("(什么都没想起来 —— 可以换个说法/换 axis 再试,或者承认记不清)")
    kept = _relevant(hits)
    if not kept:
        # 有结果但都不相关:多半是真命中已经喂过、池子只剩边角料 → 别让她继续挖
        return ToolOutcome("(没有更相关的记忆了 —— 这件事你知道的就是眼前这些,别再翻了,直接回复)")
    lines = [_fmt_memory(h.memory) for h in kept]
    return ToolOutcome("想起来这些:\n" + "\n".join(lines),
                       hit_ids=[h.memory.id for h in kept])


async def _do_grep(session: AsyncSession, chat, args: dict) -> ToolOutcome:
    keyword = (args.get("keyword") or "").strip()
    if not keyword:
        return ToolOutcome("(关键词是空的)")
    speaker = {"user": Speaker.user, "agent": Speaker.agent}.get(args.get("speaker") or "any")
    ts_min, ts_max = _days_range(args)

    rows = await l3_store.grep_memories(
        session, chat_id=chat.id, keyword=keyword, speaker=speaker,
        limit=MAX_HITS_PER_CALL, ts_min_ms=ts_min, ts_max_ms=ts_max,
    )
    if not rows:
        return ToolOutcome(f"(记忆原文里没有『{keyword}』—— 换个写法,或按含义 search_memory)")
    lines = [_fmt_memory(m) for m in rows]
    return ToolOutcome(f"原文里带『{keyword}』的:\n" + "\n".join(lines),
                       hit_ids=[m.id for m in rows])


async def _do_write_note(session: AsyncSession, chat, args: dict) -> ToolOutcome:
    from app.conversation import notebook

    note = await notebook.add_note(session, chat.id, args.get("content") or "")
    if note is None:
        return ToolOutcome("(小本子写满了/内容是空的 —— 挑最要紧的记,旧的会在夜里整理掉)")
    return ToolOutcome(f"(已记进小本子:『{note.content}』)")


async def _do_set_timer(session: AsyncSession, chat, args: dict,
                        pending_count: int) -> ToolOutcome:
    if pending_count >= settings.timer_max_pending:
        return ToolOutcome("(闹钟已经挂满了,先别再约新的)")
    try:
        minutes = max(1, min(settings.timer_max_minutes, int(args.get("minutes", 0))))
    except (TypeError, ValueError):
        return ToolOutcome("(minutes 不是有效数字,闹钟没定上)")
    memo = (args.get("memo") or "").strip()
    session.add(TimerPing(chat_id=chat.id, due_ms=now_ms() + minutes * 60_000, topic=memo))
    return ToolOutcome(f"(闹钟已定:{minutes}分钟后提醒你『{memo}』)",
                       timer={"minutes": minutes, "topic": memo})


async def dispatch(
    session: AsyncSession, *, chat, name: str, arguments: str,
    exclude_ids: set[uuid.UUID], pending_timer_count: int,
) -> ToolOutcome:
    """执行一次工具调用。任何失败都降级成一句给模型看的文本,绝不抛异常。"""
    try:
        args = json.loads(arguments) if arguments else {}
        if not isinstance(args, dict):
            args = {}
    except (json.JSONDecodeError, TypeError):
        return ToolOutcome("(参数没读懂,换个方式再试)")

    try:
        if name == "search_memory":
            return await _do_search(session, chat, args, exclude_ids)
        if name == "grep_memory":
            return await _do_grep(session, chat, args)
        if name == "set_timer":
            return await _do_set_timer(session, chat, args, pending_timer_count)
        if name == "write_note":
            return await _do_write_note(session, chat, args)
        return ToolOutcome(f"(没有叫 {name} 的工具)")
    except Exception as e:  # noqa: BLE001 — 工具失败不能把生成主流程带崩
        return ToolOutcome(f"(这次没翻到:{type(e).__name__})")
