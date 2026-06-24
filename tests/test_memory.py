"""
Pure-function memory tests — no DB / no LLM required (fallback embedder + L1 logic).

    pytest tests/test_memory.py -v
"""
import uuid

from app.llm import embeddings
from app.memory import l1_assembly
from app.memory.l1_assembly import estimate_tokens
from app.memory.retrieval import Hit
from app.models import Memory, Speaker


def _mem(content: str, speaker=Speaker.user, mid=None, cherished=False) -> Memory:
    m = Memory(content=content, speaker=speaker, cherished=cherished, tags=[])
    m.id = mid or uuid.uuid4()
    return m


# ── Fallback embedder (hermetic: tests the local hash path, never the network) ─
def test_fallback_embedder_dim_and_norm():
    v = embeddings._hash_embed("今天工作好累")
    assert len(v) == embeddings.DIM
    norm = sum(x * x for x in v) ** 0.5
    assert abs(norm - 1.0) < 1e-5 or norm == 0.0


def test_fallback_embedder_semantic_order():
    """Shared tokens → higher cosine. Deterministic & stable."""
    import numpy as np
    a = np.array(embeddings._hash_embed("我今天加班到很晚好累"))
    b = np.array(embeddings._hash_embed("今天加班好累啊"))
    c = np.array(embeddings._hash_embed("周末我们去看海吧天气很好"))
    assert float(a @ b) > float(a @ c)


# ── Token estimation ────────────────────────────────────────────────
def test_estimate_tokens_cjk_heavier():
    assert estimate_tokens("中文中文中文") > estimate_tokens("abcdef")


# ── L1 assembly: dedup + priority + budget ──────────────────────────
def test_l1_global_dedup_priority():
    shared = uuid.uuid4()
    cher = [_mem("刻骨铭心的一句", mid=shared, cherished=True)]
    hot = [_mem("热点记忆", mid=shared)]          # same id as cherished
    retrieved = [Hit(_mem("检索到的", mid=uuid.uuid4()), 0.9, "content")]

    block, dbg = l1_assembly.build_memory_block(
        cherished=cher, hot=hot, retrieved=retrieved,
    )
    # shared id claimed by cherished (higher priority), not duplicated into hot
    assert str(shared) in dbg.cherished_ids
    assert str(shared) not in dbg.hot_ids


def test_l1_excludes_working_window():
    wid = uuid.uuid4()
    retrieved = [Hit(_mem("已经在对话里了", mid=wid), 0.95, "content")]
    block, dbg = l1_assembly.build_memory_block(
        cherished=[], hot=[], retrieved=retrieved, exclude_ids={wid},
    )
    assert str(wid) not in dbg.retrieved_ids
    assert "已经在对话里了" not in block


def test_l1_budget_drops_overflow():
    cher = [_mem("超长记忆" * 500, mid=uuid.uuid4(), cherished=True) for _ in range(5)]
    block, dbg = l1_assembly.build_memory_block(
        cherished=cher, hot=[], retrieved=[], budget=200,
    )
    assert dbg.dropped_ids  # not everything fits → something dropped
