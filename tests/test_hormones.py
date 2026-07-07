"""
激素模拟单测:多时间尺度衰减、事件触发、与修复/耐心/好感的耦合。
纯函数,不碰 LLM / DB。

    pytest tests/test_hormones.py -v
"""
import time

from app.affect import dynamics
from app.affect.persona import Persona
from app.affect.state import AffectState


def neutral_events(**overrides):
    ev = {
        "bid_in_her_last_msg": "none", "his_response_type": "not_applicable",
        "addresses_loop_id": None, "is_repair_attempt": False,
        "new_bid_from_him": False, "new_commitment": None,
        "tone_flags": [], "topic_relates_to_grievance_id": None,
    }
    ev.update(overrides)
    return ev


# ── 衰减:三种激素有不同的半衰期 ─────────────────────────────────────
def test_hormone_halflives_are_distinct():
    p = Persona()
    s = AffectState.fresh(p)
    s.adrenaline = s.oxytocin = s.cortisol = 1.0
    s.last_ts = time.time() - 3 * 3600   # 过了 3 小时

    dynamics.apply_time(s, p)
    # 肾上腺素(20min 半衰期)3 小时后几乎清零;催产素(3h)恰好半衰;皮质醇(20h)还剩大半
    assert s.adrenaline < 0.01
    assert 0.4 < s.oxytocin < 0.6
    assert 0.85 < s.cortisol < 0.95


def test_cortisol_survives_overnight_arousal_does_not():
    """吵完架第二天早上:arousal 早凉了,cortisol 还在 —— 这是激素层存在的意义。"""
    p = Persona()
    s = AffectState.fresh(p)
    s.arousal = 1.0
    s.cortisol = 0.8
    s.last_ts = time.time() - 10 * 3600   # 睡了一夜

    dynamics.apply_time(s, p)
    assert s.arousal < 0.01
    assert s.cortisol > 0.5


# ── 触发:事件 → 激素 ────────────────────────────────────────────────
def test_turn_against_spikes_adrenaline_and_cortisol():
    p, s = Persona(), AffectState.fresh(Persona())
    ev = neutral_events(his_response_type="turn_against")
    dynamics.apply_events(s, ev, p, "你怎么又这样", "烦不烦")
    assert s.adrenaline >= dynamics.ADR_TURN_AGAINST
    assert s.cortisol >= dynamics.COR_TURN_AGAINST


def test_accepted_repair_releases_oxytocin():
    p = Persona()
    s = AffectState.fresh(p)
    s.mode = "conflict"
    s.security = 0.9
    ev = neutral_events(is_repair_attempt=True)
    dynamics.apply_events(s, ev, p, None, "对不起,是我不好")
    assert ev["_repair_accepted"]
    assert s.oxytocin >= dynamics.OXY_REPAIR


def test_comfort_met_vs_ignored():
    p = Persona()
    met = AffectState.fresh(p)
    dynamics.apply_events(
        met, neutral_events(bid_in_her_last_msg="seeking_comfort",
                            his_response_type="turn_toward"), p, "我好难受", "抱抱你")
    assert met.oxytocin >= dynamics.OXY_COMFORT_MET

    ignored = AffectState.fresh(p)
    dynamics.apply_events(
        ignored, neutral_events(bid_in_her_last_msg="seeking_comfort",
                                his_response_type="turn_away"), p, "我好难受", "哦")
    assert ignored.cortisol >= dynamics.COR_COMFORT_IGNORED


def test_milestone_tier_up_releases_both():
    """跨过恋人线那一刻:心跳(adrenaline)+ 亲密余韵(oxytocin)。"""
    p = Persona()
    s = AffectState.fresh(p)
    s.affection = 99.5   # 差一点到恋人档
    ev = neutral_events(bid_in_her_last_msg="sharing", his_response_type="turn_toward",
                        tone_flags=["affectionate"])
    dynamics.apply_events(s, ev, p, "我跟你说", "我也一直想跟你说这个")
    assert ev["_tier_shift"] and ev["_tier_shift"]["direction"] == "up"
    assert s.adrenaline >= dynamics.ADR_MILESTONE
    assert s.oxytocin >= dynamics.OXY_MILESTONE_UP


# ── 耦合:激素改变系统的其他动力学 ──────────────────────────────────
def test_oxytocin_eases_repair_cortisol_hardens_it():
    p = Persona()
    base = AffectState.fresh(p)
    soft = AffectState.fresh(p); soft.oxytocin = 1.0
    hard = AffectState.fresh(p); hard.cortisol = 1.0
    t_base = dynamics.repair_threshold(base, p)
    assert dynamics.repair_threshold(soft, p) < t_base   # 余温里容易心软
    assert dynamics.repair_threshold(hard, p) > t_base   # 压力下更难被哄


def test_cortisol_drags_next_session_patience():
    """隔夜没散的压力,第二天耐心更薄。"""
    p = Persona()
    calm = AffectState.fresh(p)
    calm.last_ts = time.time() - 8 * 3600
    dynamics.apply_time(calm, p)

    stressed = AffectState.fresh(p)
    stressed.cortisol = 1.0
    stressed.last_ts = time.time() - 8 * 3600
    dynamics.apply_time(stressed, p)
    assert stressed.patience < calm.patience


def test_oxytocin_amplifies_affection_gain():
    p = Persona()
    plain = AffectState.fresh(p)
    afterglow = AffectState.fresh(p); afterglow.oxytocin = 1.0
    ev = neutral_events(bid_in_her_last_msg="sharing", his_response_type="turn_toward")
    dynamics.apply_events(plain, dict(ev), p, "跟你说个事", "你说")
    dynamics.apply_events(afterglow, dict(ev), p, "跟你说个事", "你说")
    assert afterglow.affection > plain.affection


# ── 注意力信号:dull_streak ──────────────────────────────────────────
def test_dull_streak_counts_flat_turns_and_resets_on_engagement():
    p, s = Persona(), AffectState.fresh(Persona())
    flat = neutral_events()
    dynamics.apply_events(s, dict(flat), p, "嗯", "哦")
    dynamics.apply_events(s, dict(flat), p, "嗯", "哦")
    assert s.dull_streak == 2

    engaged = neutral_events(bid_in_her_last_msg="sharing", his_response_type="turn_toward")
    dynamics.apply_events(s, engaged, p, "跟你说个事!", "你说你说")
    assert s.dull_streak == 0


def test_quarrel_is_not_dull():
    """吵架是另一种投入,不算话题变淡。"""
    p, s = Persona(), AffectState.fresh(Persona())
    dynamics.apply_events(s, neutral_events(his_response_type="turn_against"),
                          p, "你怎么又忘了", "有完没完")
    assert s.dull_streak == 0


def test_hormones_survive_serialization_roundtrip():
    s = AffectState.fresh(Persona())
    s.adrenaline, s.oxytocin, s.cortisol, s.dull_streak = 0.3, 0.4, 0.5, 2
    restored = AffectState.from_dict(s.to_dict())
    assert restored.adrenaline == 0.3
    assert restored.oxytocin == 0.4
    assert restored.cortisol == 0.5
    assert restored.dull_streak == 2


def test_old_affect_dict_without_hormones_still_loads():
    """0.2.0 存的 affect JSON 没有激素字段 → 默认 0,不炸。"""
    old = AffectState.fresh(Persona()).to_dict()
    for k in ("adrenaline", "oxytocin", "cortisol", "dull_streak", "last_shift_turn"):
        old.pop(k, None)
    restored = AffectState.from_dict(old)
    assert restored.adrenaline == 0.0
    assert restored.dull_streak == 0
