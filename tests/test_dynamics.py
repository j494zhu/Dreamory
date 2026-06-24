"""
动力学单测。这是这套架构最大的红利:状态转移是纯函数,每条耦合规则都可以直接断言,
不需要跑 LLM。

    pytest tests/test_dynamics.py -v
"""
from app.affect import dynamics
from app.affect.persona import Persona
from app.affect.state import AffectState, OpenLoop


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
    p, s = Persona(), AffectState.fresh(Persona())
    s.open_loops.append(OpenLoop.new("commitment", "他承诺:周末打电话", 1, weight=3))
    s.last_ts -= 8 * 3600
    dynamics.apply_time(s, p)
    assert len(s.open_loops) == 0
    assert len(s.grievances) == 1
    assert "周末打电话" in s.grievances[0].content


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
