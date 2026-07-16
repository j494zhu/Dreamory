"""
感知/决策日志(0.6.1)纯函数单测:块标题拆解、检索摘要、record 构造。
不碰 LLM / DB。

    pytest tests/test_turnlog.py -v
"""
import uuid
from types import SimpleNamespace

from app.conversation import turnlog


class _FakeSession:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)


# ── block_heads:system prompt → 注入块标题清单 ───────────────────────
def test_block_heads_takes_first_line_of_each_block():
    prompt = (
        "你是小雨。自由插画师。\n\n"
        "【你们现在的关系】\n你们是恋人。\n\n"
        "【此刻的状态】\n你有点累。\n\n"
        "【输出格式】\n先在 <thinking>…"
    )
    heads = turnlog.block_heads(prompt)
    assert heads[0].startswith("你是小雨")
    assert "【你们现在的关系】" in heads
    assert "【此刻的状态】" in heads
    assert heads[-1] == "【输出格式】"


def test_block_heads_truncates_and_skips_blanks():
    prompt = "A" * 100 + "\n\n\n\n" + "B块"
    heads = turnlog.block_heads(prompt, limit=10)
    assert heads == ["A" * 10, "B块"]


# ── summarize_hits:检索命中 → 审计摘要 ───────────────────────────────
def _hit(content: str, score: float = 0.8, raw: float = 0.7, axis: str = "content"):
    mem = SimpleNamespace(id=uuid.uuid4(), content=content)
    return SimpleNamespace(memory=mem, score=score, raw=raw, axis=axis)


def test_summarize_hits_snips_and_keeps_scores():
    hits = [_hit("很长的内容" * 50, score=0.91234, raw=0.8), _hit("短的")]
    out = turnlog.summarize_hits(hits)
    assert len(out) == 2
    assert out[0]["score"] == 0.912 and out[0]["raw"] == 0.8
    assert len(out[0]["content"]) <= 80
    assert out[1]["content"] == "短的"
    assert all("memory_id" in h and "axis" in h for h in out)


# ── record:构造日志行 ────────────────────────────────────────────────
def test_record_strips_internal_keys_and_stores_ids_only():
    fake = _FakeSession()
    chat_id, user_mid = uuid.uuid4(), uuid.uuid4()
    reply_ids = [uuid.uuid4(), uuid.uuid4()]
    events = {"his_response_type": "turn_toward", "confidence": "low",
              "_commitment_loop": object(), "_tier_shift": None}
    log = turnlog.record(
        fake, chat_id, turn=3, mode="warm", source="message",
        user_mem_id=user_mid, reply_mem_ids=reply_ids,
        events=events, trace=["投标被接住"],
        system_prompt="你是小雨。\n\n【输出格式】\n…",
        store_full_prompt=False,
    )
    assert fake.added == [log]
    assert log.events == {"his_response_type": "turn_toward", "confidence": "low"}
    assert log.confidence == "low"
    assert log.reply_mem_ids == [str(i) for i in reply_ids]   # 只存 id,不存内容
    assert log.prompt_blocks == ["你是小雨。", "【输出格式】"]
    assert log.system_prompt == ""                            # 未开全文存储


def test_record_full_prompt_gated_by_flag():
    log = turnlog.record(
        _FakeSession(), uuid.uuid4(), turn=1, mode="neutral",
        system_prompt="完整prompt", store_full_prompt=True,
    )
    assert log.system_prompt == "完整prompt"
    assert log.confidence == "high"       # events 缺省按 high


def test_record_truncates_runaway_traces():
    log = turnlog.record(
        _FakeSession(), uuid.uuid4(), turn=1, mode="neutral",
        trace=[f"规则{i}" for i in range(100)],
        tools=[{"tool": "search_memory"}] * 50,
    )
    assert len(log.trace) == 40 and len(log.tools) == 20
