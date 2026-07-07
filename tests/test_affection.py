"""
好感度(affection)单测。与情绪动力学一样是纯函数,逐条耦合规则直接断言,不跑 LLM。

    pytest tests/test_affection.py -v
"""
from app.affect import dynamics
from app.affect.persona import PRESETS, Persona
from app.affect.state import AffectState, affection_tier


def neutral_events(**overrides):
    ev = {
        "bid_in_her_last_msg": "none", "his_response_type": "not_applicable",
        "addresses_loop_id": None, "is_repair_attempt": False,
        "new_bid_from_him": False, "new_commitment": None,
        "tone_flags": [], "topic_relates_to_grievance_id": None,
    }
    ev.update(overrides)
    return ev


# ── 基线与预设 ─────────────────────────────────────────────


def test_default_start_is_stranger():
    s = AffectState.fresh(Persona())
    assert s.affection == 50.0
    assert affection_tier(s.affection) == ("stranger", "陌生")


def test_preset_couples_start_as_lovers():
    """profile 写着'在一起两年'的预设,起点必须落在恋人档,否则注入自相矛盾。"""
    for key in ("secure", "anxious", "avoidant"):
        s = AffectState.fresh(PRESETS[key])
        assert affection_tier(s.affection)[0] == "lover", key


def test_roundtrip_serialization_keeps_affection():
    s = AffectState.fresh(Persona())
    s.affection = 123.4
    assert AffectState.from_dict(s.to_dict()).affection == 123.4


# ── 事件 → 好感度增减 ──────────────────────────────────────


def test_turn_toward_raises_affection():
    p, s = Persona(), AffectState.fresh(Persona())
    ev = neutral_events(bid_in_her_last_msg="sharing", his_response_type="turn_toward")
    dynamics.apply_events(s, ev, p, "跟你说个事", "然后呢然后呢")
    assert s.affection > 50.0


def test_turn_against_hits_anxious_harder():
    secure, anxious = Persona(anxiety=0.6), Persona(anxiety=1.8)
    s1, s2 = AffectState.fresh(secure), AffectState.fresh(anxious)
    ev = neutral_events(his_response_type="turn_against")
    dynamics.apply_events(s1, dict(ev), secure, None, "你烦不烦")
    dynamics.apply_events(s2, dict(ev), anxious, None, "你烦不烦")
    assert s2.affection < s1.affection < 50.0


def test_affection_clamped_to_bounds():
    p = Persona(anxiety=2.0)
    hi, lo = AffectState.fresh(p), AffectState.fresh(p)
    hi.affection = 199.9
    ev_good = neutral_events(bid_in_her_last_msg="sharing",
                             his_response_type="turn_toward", tone_flags=["warm"])
    dynamics.apply_events(hi, ev_good, p, "看!", "太棒了吧")
    assert hi.affection <= 200.0

    lo.affection = 1.0
    ev_bad = neutral_events(his_response_type="turn_against")
    dynamics.apply_events(lo, ev_bad, p, None, "有病吧")
    assert lo.affection == 0.0


def test_gains_diminish_at_high_affection():
    """恋人档以上,同样的好事拉不动同样多的好感(高段位递减)。"""
    p = Persona()
    low, high = AffectState.fresh(p), AffectState.fresh(p)
    low.affection, high.affection = 60.0, 160.0
    ev = neutral_events(bid_in_her_last_msg="sharing", his_response_type="turn_toward")
    dynamics.apply_events(low, dict(ev), p, "分享", "接住")
    dynamics.apply_events(high, dict(ev), p, "分享", "接住")
    assert (low.affection - 60.0) > (high.affection - 160.0) > 0


# ── 好感度对既有门控的耦合 ─────────────────────────────────


def test_repair_easier_when_deeply_in_love():
    """同样的 security,誓约级好感会心软,失望级好感哄不动。"""
    p = Persona()
    deep = AffectState.fresh(p); deep.mode = "conflict"
    deep.security = 0.42; deep.affection = 200.0
    cold = AffectState.fresh(p); cold.mode = "conflict"
    cold.security = 0.42; cold.affection = 0.0
    ev = neutral_events(is_repair_attempt=True)
    dynamics.apply_events(deep, dict(ev), p, None, "对不起,是我不好")
    dynamics.apply_events(cold, dict(ev), p, None, "对不起,是我不好")
    assert deep.security > 0.42          # 接受修复,security 回升
    assert cold.security == 0.42         # 被拒绝,原地不动


def test_session_reset_uses_affection_floor_and_patience_bonus():
    """新会话 warm_streak 不再一律清零;深爱的人耐心预算更足。"""
    p = Persona()  # base_patience=5
    s = AffectState.fresh(p)
    s.affection = 150.0
    s.warm_streak = 5
    s.patience = 0
    s.last_ts -= 8 * 3600
    dynamics.apply_time(s, p)
    assert s.warm_streak == 2            # 挚爱档的重逢底色
    assert s.patience == 6               # 5 + 1(恋人以上加成)


def test_stranger_session_reset_unchanged():
    p, s = Persona(), AffectState.fresh(Persona())
    s.warm_streak = 3
    s.last_ts -= 8 * 3600
    dynamics.apply_time(s, p)
    assert s.warm_streak == 0
    assert s.patience == p.base_patience


# ── 离线衰减 ───────────────────────────────────────────────


def test_long_absence_decays_affection():
    p, s = Persona(), AffectState.fresh(Persona())
    s.affection = 100.0
    s.last_ts -= 10 * 86400              # 被晾了 10 天
    dynamics.apply_time(s, p)
    assert abs(s.affection - 93.0) < 1e-6   # (10-3天宽限) × 1/天


def test_absence_decay_never_below_friendly_floor():
    p, s = Persona(), AffectState.fresh(Persona())
    s.affection = 62.0
    s.last_ts -= 30 * 86400
    dynamics.apply_time(s, p)
    assert s.affection == 60.0           # 感情会淡,共同经历不清零


def test_short_gap_does_not_decay():
    p, s = Persona(), AffectState.fresh(Persona())
    s.affection = 100.0
    s.last_ts -= 8 * 3600
    dynamics.apply_time(s, p)
    assert s.affection == 100.0


# ── 分层跨越 ───────────────────────────────────────────────


def test_tier_shift_recorded_on_crossing():
    p, s = Persona(), AffectState.fresh(Persona())
    s.affection = 59.5
    ev = neutral_events(bid_in_her_last_msg="sharing", his_response_type="turn_toward")
    dynamics.apply_events(s, ev, p, "分享", "接住")
    assert ev["_tier_shift"] == {
        "from": "陌生", "to": "友好", "direction": "up", "milestone": False,
    }


def test_crossing_lover_line_is_milestone():
    p, s = Persona(), AffectState.fresh(Persona())
    s.affection = 99.5
    ev = neutral_events(bid_in_her_last_msg="sharing", his_response_type="turn_toward")
    dynamics.apply_events(s, ev, p, "分享", "接住")
    assert ev["_tier_shift"]["milestone"] is True
    assert ev["_tier_shift"]["to"] == "恋人"


def test_no_shift_when_staying_in_tier():
    p, s = Persona(), AffectState.fresh(Persona())
    ev = neutral_events(bid_in_her_last_msg="sharing", his_response_type="turn_toward")
    dynamics.apply_events(s, ev, p, "分享", "接住")
    assert ev["_tier_shift"] is None
