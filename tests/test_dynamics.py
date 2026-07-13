"""
动力学单测。这是这套架构最大的红利:状态转移是纯函数,每条耦合规则都可以直接断言,
不需要跑 LLM。

    pytest tests/test_dynamics.py -v
"""
from app.affect import dynamics
from app.affect.persona import Persona
from app.affect.state import AffectState, Grievance, OpenLoop


def neutral_events(**overrides):
    ev = {
        "bid_in_her_last_msg": "none", "his_response_type": "not_applicable",
        "addresses_loop_id": None, "is_repair_attempt": False,
        "new_bid_from_him": False, "new_commitment": None,
        "tone_flags": [], "topic_relates_to_grievance_id": None,
    }
    ev.update(overrides)
    return ev


def test_ignored_bid_creates_loop_and_drains_patience():
    p, s = Persona(), AffectState.fresh(Persona())
    ev = neutral_events(bid_in_her_last_msg="venting", his_response_type="turn_away")
    dynamics.apply_events(s, ev, p, "今天加班好累", "哦")
    assert s.patience == p.base_patience - 2
    assert len(s.open_loops) == 1
    assert s.security < p.security_baseline


def test_three_turn_aways_trigger_withdrawn():
    p, s = Persona(), AffectState.fresh(Persona())
    for _ in range(3):
        ev = neutral_events(bid_in_her_last_msg="sharing", his_response_type="turn_away")
        dynamics.apply_events(s, ev, p, "跟你说个事", "嗯")
        dynamics.transition(s, ev, p)
    assert s.mode == "withdrawn"


def test_conflict_has_hysteresis():
    """冲突不会因为他换了个话题就自动结束。"""
    p, s = Persona(), AffectState.fresh(Persona())
    ev = neutral_events(his_response_type="turn_against")
    dynamics.apply_events(s, ev, p, "你昨天怎么没回我", "你能不能别烦了")
    dynamics.transition(s, ev, p)
    assert s.mode == "conflict"
    ev2 = neutral_events()
    dynamics.apply_events(s, ev2, p, "…", "今天吃了个超好吃的面")
    dynamics.transition(s, ev2, p)
    assert s.mode == "conflict"


def test_repair_gated_by_security():
    p = Persona()
    high = AffectState.fresh(p); high.mode = "conflict"; high.security = 0.7
    low = AffectState.fresh(p);  low.mode = "conflict";  low.security = 0.2
    ev = neutral_events(is_repair_attempt=True)
    dynamics.apply_events(high, dict(ev), p, None, "对不起,是我不好")
    dynamics.apply_events(low, dict(ev), p, None, "对不起,是我不好")
    assert high.security > 0.7
    assert low.security == 0.2


def test_anxious_persona_loses_security_faster():
    secure = Persona(anxiety=0.6)
    anxious = Persona(anxiety=1.8)
    s1, s2 = AffectState.fresh(secure), AffectState.fresh(anxious)
    ev = neutral_events(bid_in_her_last_msg="seeking_comfort", his_response_type="turn_away")
    dynamics.apply_events(s1, dict(ev), secure, "抱抱我", "在忙")
    dynamics.apply_events(s2, dict(ev), anxious, "抱抱我", "在忙")
    assert s2.security < s1.security


def test_loop_escalates_to_grievance_across_sessions():
    """非承诺类回路保持旧语义:熬过一个会话即沉旧账。
    (承诺类 0.6.0 起走到期时刻判定——"周六打电话"周二变旧账是 bug 不是特性,
    见 tests/test_commitment.py。)"""
    p, s = Persona(), AffectState.fresh(Persona())
    s.open_loops.append(OpenLoop.new("unanswered_bid", "她说压力大,他只回了'哦'", 1, weight=3))
    s.last_ts -= 8 * 3600
    dynamics.apply_time(s, p)
    assert len(s.open_loops) == 0
    assert len(s.grievances) == 1
    assert "压力大" in s.grievances[0].content


def test_avoidant_withdraws_instead_of_fighting():
    avoidant = Persona(avoidance=1.8, expressiveness=0.5)
    s = AffectState.fresh(avoidant)
    ev = neutral_events(his_response_type="turn_against")
    dynamics.apply_events(s, ev, avoidant, "你怎么了", "烦死了别问了")
    dynamics.transition(s, ev, avoidant)
    assert s.mode == "withdrawn"


def test_state_roundtrip_serialization():
    p, s = Persona(), AffectState.fresh(Persona())
    s.open_loops.append(OpenLoop.new("commitment", "他承诺:早点睡", 1, weight=2))
    restored = AffectState.from_dict(s.to_dict())
    assert restored.open_loops[0].content == "他承诺:早点睡"
    assert restored.security == s.security


# ── 旧账和解:resolve 回路(带保底,与修复门控同构)────────────────────
def _with_grievance(security: float):
    p, s = Persona(), AffectState.fresh(Persona())
    s.security = security
    s.grievances.append(Grievance(id="g1", content="说好一起去大排档,他鸽了", weight=3))
    return p, s


def test_grievance_resolved_by_genuine_repair():
    """他带着歉意直面旧账 + security 过门槛 → 和解:好感/催产素涨,皮质醇落地。"""
    p, s = _with_grievance(security=0.7)
    s.cortisol = 0.3
    aff = s.affection
    ev = neutral_events(topic_relates_to_grievance_id="g1", is_repair_attempt=True)
    trace = dynamics.apply_events(s, ev, p, "上次的事我还记着呢", "那天真的对不起,这周六补上,我订好位置了")
    g = s.grievances[0]
    assert g.resolved
    assert s.affection > aff
    assert s.oxytocin > 0
    assert s.cortisol < 0.3
    assert any("和解" in t for t in trace)
    assert s.find_grievance("g1") is None       # 翻篇的旧账不再参与动力学


def test_grievance_not_resolved_when_security_low():
    """心结还在:security 不过门槛,旧账不翻篇,只记下他试过。"""
    p, s = _with_grievance(security=0.2)
    ev = neutral_events(topic_relates_to_grievance_id="g1", is_repair_attempt=True)
    dynamics.apply_events(s, ev, p, None, "那次是我不对嘛")
    g = s.grievances[0]
    assert not g.resolved
    assert g.touches == 1
    assert s.arousal > 0.1        # 被提起还是会心里一紧


def test_grievance_pity_resolution_after_repeated_touches():
    """哄到第 GRIEVANCE_PITY_TOUCHES 次保底翻篇,但只有打折的好感(没真的暖回来)。"""
    p, s = _with_grievance(security=0.2)
    aff = s.affection
    for _ in range(dynamics.GRIEVANCE_PITY_TOUCHES):
        ev = neutral_events(topic_relates_to_grievance_id="g1", is_repair_attempt=True)
        dynamics.apply_events(s, ev, p, None, "好啦,那件事真的对不起")
    g = s.grievances[0]
    assert g.resolved
    gain = s.affection - aff
    assert 0 < gain < dynamics.AFF_GAIN_GRIEV_RESOLVED   # 保底和解 < 真心和解
    assert s.oxytocin == 0                               # 勉强翻篇没有柔软余韵


def test_mere_topic_touch_does_not_resolve():
    """只是话题擦到旧账(没有歉意)→ 维持旧行为:arousal 上浮,旧账还在。"""
    p, s = _with_grievance(security=0.9)
    ev = neutral_events(topic_relates_to_grievance_id="g1", is_repair_attempt=False)
    dynamics.apply_events(s, ev, p, None, "说起来上次那家大排档还开着吗")
    assert not s.grievances[0].resolved
    assert s.grievances[0].touches == 0
    assert s.arousal > 0.1


def test_grievance_touches_serialization_and_legacy_load():
    p, s = _with_grievance(security=0.5)
    s.grievances[0].touches = 2
    restored = AffectState.from_dict(s.to_dict())
    assert restored.grievances[0].touches == 2
    d = s.to_dict()
    del d["grievances"][0]["touches"]           # 0.6.0 以前的旧档没有该字段
    assert AffectState.from_dict(d).grievances[0].touches == 0


# ── 轻回路自然遗忘:鸡毛蒜皮不该永远挂着 ────────────────────────────────
def test_light_loop_forgotten_after_sessions_without_penalty():
    """weight<3 的回路:第一晚还挂着,熬过 LOOP_FORGET_SESSIONS 个会话自然淡忘——
    不沉旧账、不掉好感(此前它们会永远留在 open_loops 里无限碎碎念)。"""
    p, s = Persona(), AffectState.fresh(Persona())
    s.open_loops.append(OpenLoop.new("unanswered_bid", "她随口分享,他嗯了一声", 1, weight=2))
    s.last_ts -= 8 * 3600
    dynamics.apply_time(s, p)
    assert len(s.open_loops) == 1                # 第一晚:还有点在意
    aff = s.affection
    s.last_ts -= 8 * 3600
    dynamics.apply_time(s, p)
    assert s.open_loops == []                    # 第二晚:不挂心了
    assert s.grievances == []                    # 没有沉成旧账
    assert s.affection == aff                    # 淡忘无惩罚


def test_heavy_loop_still_escalates_not_forgotten():
    """遗忘只适用于轻回路:weight>=3 的沉淀路径不受影响。"""
    p, s = Persona(), AffectState.fresh(Persona())
    s.open_loops.append(OpenLoop.new("unanswered_bid", "她说压力大,他只回了'哦'", 1, weight=3))
    s.last_ts -= 8 * 3600
    dynamics.apply_time(s, p)
    assert s.open_loops == []
    assert len(s.grievances) == 1
