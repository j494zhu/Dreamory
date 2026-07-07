"""
注入器区块结构的回归测试。

背景:0.2.2 曾把 set_timer 教学降级成【主动回忆】块下的一个子弹点,实盘合规率
从 ~100%(独立块 + 因果重锤)掉到 ~33%(嘴上答应却不调用工具)。这里把
"定时器必须独立成块"钉死成断言,防止再被重构掉。

    pytest tests/test_injector_blocks.py -v
"""
from app.affect.injector import render
from app.affect.persona import Persona
from app.affect.state import AffectState

CAUSAL_HAMMER = "他就永远等不到你"   # 因果重锤:两条路径共用的关键句


def _blocks(**kwargs) -> list[str]:
    out = render(AffectState.fresh(Persona()), Persona(), **kwargs)
    return out.split("\n\n")


def _find(blocks: list[str], head: str) -> str | None:
    return next((b for b in blocks if b.startswith(head)), None)


def test_tools_on_timer_gets_dedicated_block():
    blocks = _blocks(allow_tools=True, allow_timer=True)
    timer_block = _find(blocks, "【定时器")
    assert timer_block is not None, "set_timer 必须独立成块,不许塞进主动回忆"
    assert "set_timer" in timer_block
    assert CAUSAL_HAMMER in timer_block            # 因果重锤在
    assert "什么时候必须调用" in timer_block        # 触发清单在

    recall_block = _find(blocks, "【主动回忆")
    assert recall_block is not None
    assert "set_timer" not in recall_block         # 回忆块保持专注


def test_tools_on_without_timer_has_no_timer_block():
    blocks = _blocks(allow_tools=True, allow_timer=False)
    assert _find(blocks, "【定时器") is None
    assert _find(blocks, "【主动回忆") is not None


def test_tools_off_falls_back_to_tag_block():
    blocks = _blocks(allow_tools=False, allow_timer=True)
    timer_block = _find(blocks, "【定时器")
    assert timer_block is not None
    assert "<timer" in timer_block                 # 标签协议
    assert CAUSAL_HAMMER in timer_block
    assert _find(blocks, "【主动回忆") is None      # 没开工具就不教工具


def test_memory_hint_lands_in_recall_block():
    blocks = _blocks(allow_tools=True, allow_timer=True,
                     memory_hint="(提示:这一轮自动想起的内容很少)")
    recall_block = _find(blocks, "【主动回忆")
    assert "自动想起的内容很少" in recall_block
