"""
L1 弹性预算 + core identity 数据化覆盖的单测。不碰 LLM / DB。

    pytest tests/test_l1_identity.py -v
"""
import uuid

from app.affect.persona import Persona
from app.conversation.identity import build_core_identity
from app.memory import l1_assembly
from app.memory.retrieval import Hit
from app.models import Memory, Speaker


def _mem(content: str, speaker=Speaker.user, cherished=False) -> Memory:
    m = Memory(content=content, speaker=speaker, cherished=cherished, tags=[])
    m.id = uuid.uuid4()
    return m


# ── L1 弹性化:刻骨铭心没用满的预算溢给相关回忆槽 ─────────────────────
def test_unused_cherished_budget_spills_to_retrieved():
    budget = 100
    # 刻骨铭心为空 → 20% 预算全额溢出;检索项每条约 10 token
    retrieved = [Hit(_mem("检索内容" * 5), 0.9 - i * 0.01, "content") for i in range(10)]

    block, dbg = l1_assembly.build_memory_block(
        cherished=[], hot=[], retrieved=retrieved, budget=budget,
    )
    rigid_capacity = int(budget * l1_assembly.RETRIEVED_FRAC)
    kept_tokens = sum(
        l1_assembly.estimate_tokens(h.memory.content)
        for h in retrieved if str(h.memory.id) in dbg.retrieved_ids
    )
    # 弹性后保留的量超过僵化 30% 上限(说明溢出生效)
    assert kept_tokens > rigid_capacity


def test_cherished_budget_still_caps_when_used():
    """刻骨铭心占满自己的份额时,相关槽只有原始 30%。"""
    budget = 100
    # 19 个 CJK 字 ≈ 20 tokens,恰好占满 20% 份额 → 无溢出
    cherished = [_mem("刻" * 19, cherished=True)]
    retrieved = [Hit(_mem("检索内容" * 5), 0.9, "content") for _ in range(10)]

    _, dbg = l1_assembly.build_memory_block(
        cherished=cherished, hot=[], retrieved=retrieved, budget=budget,
    )
    kept_tokens = sum(
        l1_assembly.estimate_tokens(h.memory.content)
        for h in retrieved if str(h.memory.id) in dbg.retrieved_ids
    )
    assert kept_tokens <= int(budget * l1_assembly.RETRIEVED_FRAC)


# ── core identity 覆盖 ───────────────────────────────────────────────
def test_default_identity_compiled_from_persona():
    block = build_core_identity(Persona(name="小雨"), "")
    assert "你是小雨" in block
    assert block.startswith("【核心人格")


def test_override_replaces_compiled_block():
    block = build_core_identity(Persona(name="小雨"), "", override="你是阿绫,22岁,美院学生。")
    assert "阿绫" in block
    assert "小雨" not in block
    assert block.startswith("【核心人格")   # 裸文本会被补上块头


def test_override_keeps_tag_vocab():
    block = build_core_identity(Persona(), "工作 / 旅行 / 猫", override="你是阿绫。")
    assert "工作 / 旅行 / 猫" in block


def test_empty_override_falls_back():
    block = build_core_identity(Persona(name="小雨"), "", override="   ")
    assert "你是小雨" in block
