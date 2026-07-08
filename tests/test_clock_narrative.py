"""
可注入时钟 + confabulation(解释与真实动因分离)的单测。不碰 LLM / DB。

    pytest tests/test_clock_narrative.py -v
"""
import time

from app import clock
from app.affect import dynamics, narrative
from app.affect.injector import render
from app.affect.persona import Persona
from app.affect.state import AffectState, OpenLoop


class FixedRng:
    """确定性 rng:random() 依次吐预设值,choice() 恒取第一个。"""
    def __init__(self, *vals):
        self.vals = list(vals)

    def random(self):
        return self.vals.pop(0) if self.vals else 0.5

    def choice(self, seq):
        return seq[0]


def _upset_state() -> AffectState:
    s = AffectState.fresh(Persona())
    s.mode = "withdrawn"
    s.turn = 10
    s.open_loops = [OpenLoop.new("commitment", "他说周六打电话结果没打", 5, weight=4)]
    return s


# ── 时钟 ─────────────────────────────────────────────────────────────
def test_clock_offset_and_reset():
    clock.reset()
    try:
        real = time.time()
        assert abs(clock.now_s() - real) < 1.0        # 偏移为零时 = 真实时间
        clock.advance(3600)
        assert abs(clock.now_s() - real - 3600) < 1.0
        from app.models import now_ms                  # models 委托 clock
        assert now_ms() - int(real * 1000) > 3_500_000
    finally:
        clock.reset()


def test_time_warp_drives_session_boundary():
    """拨快 8 小时 → dynamics 判定新会话(耐心重置/口径清空),这是老化脚手架的根基。"""
    clock.reset()
    try:
        p = Persona()
        s = AffectState.fresh(p)
        s.patience = 1
        s.self_narrative = "就是有点累"
        clock.advance(8 * 3600)
        dynamics.apply_time(s, p)                      # now 缺省走 clock
        assert s.patience >= p.base_patience - 2       # 新会话重置(可能有激素拖累)
        assert s.self_narrative == ""                  # 昨天的口径翻篇
    finally:
        clock.reset()


# ── needs_narrative:心平气和不需要口径 ───────────────────────────────
def test_calm_state_needs_no_narrative():
    s = AffectState.fresh(Persona())
    assert not narrative.needs_narrative(s)
    assert narrative.refresh(s, Persona()) is False
    assert s.self_narrative == ""


def test_upset_state_generates_and_clears():
    s = _upset_state()
    assert narrative.needs_narrative(s)
    assert narrative.refresh(s, Persona(), rng=FixedRng(0.99, 0.99))
    assert s.self_narrative

    s.mode = "neutral"                                # 回到平静(其他指标也正常)
    s.open_loops = []
    assert narrative.refresh(s, Persona())            # 清空也算变化
    assert s.self_narrative == ""


# ── 口径黏性:会话内不换说法 ──────────────────────────────────────────
def test_narrative_is_sticky_within_mode():
    s = _upset_state()
    narrative.refresh(s, Persona(), rng=FixedRng(0.99, 0.99))
    first = s.self_narrative
    for _ in range(5):
        s.turn += 1
        changed = narrative.refresh(s, Persona(), rng=FixedRng(0.0))
        assert not changed and s.self_narrative == first   # 坚持同一套说法

    s.mode = "conflict"                                # 模式变了 → 换口径
    assert narrative.refresh(s, Persona(), rng=FixedRng(0.0))


# ── insight 决定真话概率;真话说到点子上 ──────────────────────────────
def test_high_insight_truth_references_real_cause():
    s = _upset_state()
    narrative.refresh(s, Persona(insight=1.0), rng=FixedRng(0.0))
    assert "周六打电话" in s.self_narrative            # 说到真实的挂起回路

def test_low_insight_confabulates_from_surface_events():
    s = _upset_state()
    narrative.refresh(
        s, Persona(insight=0.0),
        surface_candidates=["今天甲方第三次改需求,重画到晚上八点"],
        rng=FixedRng(0.99, 0.99),                      # 跳过真话,跳过最小化
    )
    assert "甲方" in s.self_narrative                  # 甩锅给真实发生过的生活事件
    assert "跟你没关系" in s.self_narrative
    assert "周六打电话" not in s.self_narrative        # 真实动因绝不出现


def test_minimize_pool_used_at_low_roll():
    s = _upset_state()
    narrative.refresh(s, Persona(insight=0.0), rng=FixedRng(0.99, 0.1))
    assert s.self_narrative in narrative.CONFAB_MINIMIZE


# ── 注入器:口径进独立块,真实动因不进解释 ─────────────────────────────
def test_injector_renders_narrative_block():
    s = _upset_state()
    s.self_narrative = "就是有点累,没什么"
    out = render(s, Persona())
    blocks = out.split("\n\n")
    nb = next((b for b in blocks if b.startswith("【你自己以为的原因")), None)
    assert nb is not None
    assert "就是有点累" in nb
    assert "意识不到" in nb                            # 明示真实成因不进嘴

    s.self_narrative = ""
    out2 = render(s, Persona())
    assert "【你自己以为的原因" not in out2


def test_narrative_survives_serialization():
    s = _upset_state()
    s.self_narrative, s.narrative_mode, s.narrative_turn = "口径", "withdrawn", 9
    r = AffectState.from_dict(s.to_dict())
    assert (r.self_narrative, r.narrative_mode, r.narrative_turn) == ("口径", "withdrawn", 9)
