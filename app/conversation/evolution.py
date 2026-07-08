"""
Persona 演化 — 好感度里程碑解锁的"剧情":关系升到新档位时,她这个人也会变一点。

这是第一次真正开放"模型修改自己的基础设施",所以门控从紧:
  - 只在好感度 *升* 档且该档在解锁表里时触发(跌档不演化——伤疤走 grievance);
  - 每档只演化一次(用 chat_revisions 的 reason 查重,不加新列);
  - append-only:只允许在 style / profile / core_identity 末尾追加短句,
    绝不允许改名、改年龄、删除或重写既有内容;
  - 逐项长度上限,超限直接丢弃;
  - 应用前先 config_store.snapshot(actor="model") —— 改崩了一键回退;
  - 后台运行,失败静默(演化是彩蛋,不是承诺)。
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.affect.persona import Persona
from app.config import settings
from app.conversation import config_store
from app.conversation.identity import build_core_identity
from app.llm import client
from app.llm.client import MODEL_PRO
from app.memory import l3_store
from app.models import Chat, ChatRevision

logger = logging.getLogger(__name__)

APPEND_MAX_LEN = 60          # 单项追加的长度上限
MIN_ITEMS = 1                # 至少一项有效才应用

# 解锁表:哪些档位触发演化,以及给 LLM 的演化主题。
# 低档(friendly 以下)不演化——还没熟到"这个人因你而变"的程度。
TIER_UNLOCKS = {
    "crush":   "你开始在意他了:说话方式里多一点小心翼翼的靠近,开始留意他喜欢什么。",
    "lover":   "你们在一起了:相处进入恋人模式,可以有专属的称呼、只属于你们的小习惯。",
    "devoted": "他成了你最重要的人:你的生活规划里开始自然地包含他,依赖变得笃定。",
    "oath":    "誓约:他刻进了你对自己的定义里。你是谁,已经有一部分是'和他在一起的你'。",
}


def _evo_reason(tier_key: str) -> str:
    return f"evolution:{tier_key}"


async def already_evolved(session: AsyncSession, chat_id: uuid.UUID, tier_key: str) -> bool:
    row = await session.scalar(
        select(ChatRevision.id).where(
            ChatRevision.chat_id == chat_id,
            ChatRevision.reason == _evo_reason(tier_key),
        ).limit(1)
    )
    return row is not None


# ── 校验(纯函数,可单测):append-only + 长度上限 ───────────────────────
def validate_proposal(data: dict) -> dict:
    out = {}
    for key in ("style_append", "profile_append", "identity_append"):
        v = data.get(key)
        if isinstance(v, str):
            v = v.strip().replace("\n", " ")
            if 0 < len(v) <= APPEND_MAX_LEN:
                out[key] = v
    return out


def apply_to_persona(persona: Persona, changes: dict) -> Persona:
    """把追加项拼进 persona(纯函数)。绝不动 name / 数值参数。"""
    if changes.get("style_append"):
        persona.style = f"{persona.style} {changes['style_append']}".strip()
    if changes.get("profile_append"):
        persona.profile = f"{persona.profile}{changes['profile_append']}"
    return persona


# ── 提案 + 应用 ──────────────────────────────────────────────────────
async def _propose(chat: Chat, persona: Persona, tier_key: str,
                   cherished_texts: list[str]) -> dict:
    theme = TIER_UNLOCKS[tier_key]
    memories = "\n".join(f"- {t[:60]}" for t in cherished_texts[:5]) or "(暂无)"
    data = await client.chat_json(
        [
            {"role": "system", "content": (
                "一段关系跨过了新的阶段,这个角色会因此起一点点变化。"
                "你来提案这点变化——只许'追加',不许改写她已有的样子。只输出 JSON:\n"
                "{\n"
                f'  "style_append": "追加到她说话风格里的一句话({APPEND_MAX_LEN}字内),'
                '如新的称呼/新的口癖;不需要就 null",\n'
                f'  "profile_append": "追加到她自我介绍里的一句话({APPEND_MAX_LEN}字内),'
                '如关系状态的变化;不需要就 null",\n'
                f'  "identity_append": "追加到她核心自我认知里的一句话({APPEND_MAX_LEN}字内),'
                '最深的那种变化;不需要就 null"\n'
                "}\n"
                "要求:变化要小而具体(一个称呼、一个习惯、一句认知),像真人恋爱里"
                "自然长出来的,不要写成人设大改;至少给一项,与她的性格一致。"
            )},
            {"role": "user", "content": (
                f"她:{persona.name},{persona.profile}\n"
                f"她的说话风格:{persona.style}\n"
                f"刚跨过的阶段:{tier_key} —— {theme}\n"
                f"他们之间刻骨铭心的记忆:\n{memories}"
            )},
        ],
        model=MODEL_PRO, temperature=0.8, default={},
    )
    return validate_proposal(data)


async def run_evolution(session: AsyncSession, chat: Chat, tier_key: str) -> dict | None:
    """一次演化:查重 → 提案 → 快照 → append 应用。返回应用的变化(或 None)。"""
    if tier_key not in TIER_UNLOCKS or await already_evolved(session, chat.id, tier_key):
        return None

    persona = Persona.from_dict(chat.persona) if chat.persona else Persona()
    cherished = await l3_store.cherished_memories(session, chat.id, limit=5)
    changes = await _propose(chat, persona, tier_key, [m.content for m in cherished])
    if len(changes) < MIN_ITEMS:
        return None

    # 快照在前:reason 同时充当"该档已演化"的标记
    await config_store.snapshot(session, chat, reason=_evo_reason(tier_key), actor="model")

    persona = apply_to_persona(persona, changes)
    chat.persona = persona.to_dict()

    if changes.get("identity_append"):
        # core_identity 尚未数据化的 chat:先物化出厂编译,再追加(快照里留着物化前的 None)
        base = chat.core_identity or build_core_identity(persona)
        chat.core_identity = f"{base}\n- {changes['identity_append']}"

    await session.commit()
    logger.info("persona evolved (chat %s, tier %s): %s", chat.id, tier_key, changes)
    return changes


# ── 后台调度(fire-and-forget,单飞)────────────────────────────────────
_evo_lock = asyncio.Lock()
_bg_tasks: set[asyncio.Task] = set()


async def _run_bg(chat_id: uuid.UUID, tier_key: str) -> None:
    if _evo_lock.locked():
        return
    async with _evo_lock:   # 进程内第一道闸
        from app.db import SessionLocal
        from app.db_locks import LOCK_EVOLUTION, advisory_guard, chat_key

        try:
            # 跨进程单飞(per-chat):锁内 run_evolution 会重查 already_evolved,
            # "两个进程同时通过查重然后双双演化"的竞态由这把锁关死。
            async with advisory_guard(LOCK_EVOLUTION, chat_key(chat_id)) as acquired:
                if not acquired:
                    return
                async with SessionLocal() as s:
                    chat = await s.get(Chat, chat_id)
                    if chat is not None:
                        await run_evolution(s, chat, tier_key)
        except Exception:   # 演化是彩蛋,失败静默,绝不影响对话
            logger.exception("persona evolution failed (chat %s)", chat_id)


def schedule_evolution(chat_id: uuid.UUID, tier_key: str) -> None:
    if not settings.persona_evolution_enabled or tier_key not in TIER_UNLOCKS:
        return
    task = asyncio.create_task(_run_bg(chat_id, tier_key))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
