"""
承诺兑现闭环(v0.6)单测:到期时间语义、兑现分级奖励、爽约沉淀、注入渲染。
纯函数,不碰 LLM / DB。

    pytest tests/test_commitment.py -v
"""
from app import clock
from app.affect import dynamics
from app.affect.injector import _due_phrase, render
from app.affect.persona import Persona
from app.affect.state import AffectState, OpenLoop

H = 3600_000  # ms per hour


def neutral_events(**overrides):
    ev = {
        "bid_in_her_last_msg": "none", "his_response_type": "not_applicable",
        "addresses_loop_id": None, "is_repair_attempt": False,
        "new_bid_from_him": False, "new_commitment": None,
        "commitment_due_hours": None, "tone_flags": [],
        "topic_relates_to_grievance_id": None, "persona_attack": False,
    }
    ev.update(overrides)
    return ev


def _commit_loop(state, content="他承诺:周六晚上打电话", due_in_h=None, sessions_old=0):
    due = int(clock.now_ms() + due_in_h * H) if due_in_h is not None else None
    loop = OpenLoop.new("commitment", content, state.turn, weight=3, due_ms=due)
    loop.sessions_old = sessions_old
    state.open_loops.append(loop)
    return loop


# ── 沉淀语义:到期才算爽约 ────────────────────────────────────────────
def test_future_commitment_survives_session_boundary():
    """"周六打电话"周二早上不该变成旧账——这是 0.6.0 修的核心 bug。"""
    p, s = Persona(), AffectState.fresh(Persona())
    _commit_loop(s, due_in_h=72)          # 三天后到期
    s.last_ts -= 8 * 3600                 # 跨一个会话
    dynamics.apply_time(s, p)
    assert len(s.open_loops) == 1         # 安然越冬
    assert len(s.grievances) == 0


def test_overdue_commitment_sediments_with_extra_pain():
    p, s = Persona(), AffectState.fresh(Persona())
    aff0, cor0 = s.affection, s.cortisol
    _commit_loop(s, due_in_h=-6)          # 六小时前就该兑现(超过2h宽限)
    s.last_ts -= 8 * 3600
    dynamics.apply_time(s, p)
    assert len(s.open_loops) == 0
    assert len(s.grievances) == 1
    assert "说好的没做到" in s.grievances[0].content
    assert aff0 - s.affection >= dynamics.AFF_HIT_BROKEN_PROMISE - 0.01  # 比一般旧账更伤
    assert s.cortisol >= cor0             # 爽约留下压力残留(注意衰减,只验方向)


def test_overdue_within_grace_survives():
    """刚过点半小时(宽限2h内)还不算爽约。"""
    p, s = Persona(), AffectState.fresh(Persona())
    _commit_loop(s, due_in_h=-0.5)
    s.last_ts -= 8 * 3600
    dynamics.apply_time(s, p)
    assert len(s.open_loops) == 1


def test_vague_commitment_needs_many_sessions():
    """含糊承诺("下次带你去")熬过 4 个会话才算食言,1 个不够。"""
    p = Persona()
    fresh = AffectState.fresh(p)
    _commit_loop(fresh, content="他承诺:下次带你去吃那家", due_in_h=None, sessions_old=0)
    fresh.last_ts -= 8 * 3600
    dynamics.apply_time(fresh, p)         # sessions_old 0→1
    assert len(fresh.open_loops) == 1     # 还没到食言的份上

    stale = AffectState.fresh(p)
    _commit_loop(stale, content="他承诺:下次带你去吃那家", due_in_h=None,
                 sessions_old=dynamics.VAGUE_COMMITMENT_SESSIONS)
    stale.last_ts -= 8 * 3600
    dynamics.apply_time(stale, p)
    assert len(stale.open_loops) == 0
    assert len(stale.grievances) == 1


# ── 兑现分级奖励 ─────────────────────────────────────────────────────
def test_kept_promise_beats_generic_loop_close():
    p = Persona()
    kept = AffectState.fresh(p)
    loop = _commit_loop(kept, due_in_h=5)              # 还没到点就兑现:准时
    ev = neutral_events(addresses_loop_id=loop.id, his_response_type="turn_toward")
    dynamics.apply_events(kept, ev, p, "等你电话", "来啦,现在就打给你")

    generic = AffectState.fresh(p)
    gloop = OpenLoop.new("unanswered_bid", "她说压力大没被接住", 1, weight=3)
    generic.open_loops.append(gloop)
    ev2 = neutral_events(addresses_loop_id=gloop.id, his_response_type="turn_toward")
    dynamics.apply_events(generic, ev2, p, "唉", "上次你说压力大,现在怎么样了?")

    assert not kept.open_loops and not generic.open_loops
    assert kept.affection > generic.affection          # 说到做到 > 一般回路关闭
    assert kept.oxytocin >= dynamics.OXY_PROMISE_KEPT  # 说话算数的踏实感


def test_late_fulfillment_discounted():
    p = Persona()
    on_time = AffectState.fresh(p)
    l1 = _commit_loop(on_time, due_in_h=5)
    dynamics.apply_events(on_time, neutral_events(addresses_loop_id=l1.id), p, "嗯", "打给你")

    late = AffectState.fresh(p)
    l2 = _commit_loop(late, due_in_h=-30)              # 过点一天多才兑现
    dynamics.apply_events(late, neutral_events(addresses_loop_id=l2.id), p, "嗯", "补上电话")

    assert on_time.affection > late.affection          # 迟到的兑现打折,但仍是正向


# ── 注入渲染:到期状态进人话 ──────────────────────────────────────────
def test_due_phrase_humanizes_all_stages():
    now = clock.now_ms()
    assert "马上" in _due_phrase(now + int(0.5 * H))
    assert "小时后到点" in _due_phrase(now + 6 * H)
    assert "天后到点" in _due_phrase(now + 80 * H)
    assert "过点" in _due_phrase(now - 5 * H)
    assert "天" in _due_phrase(now - 50 * H)
    assert _due_phrase(None) == ""


def test_injector_marks_pressing_commitment():
    s = AffectState.fresh(Persona())
    _commit_loop(s, due_in_h=-3)                       # 已过点
    out = render(s, Persona())
    assert "过点" in out
    assert "你心里一直惦记着" in out                    # 临期/过期 nudge

    calm = AffectState.fresh(Persona())
    _commit_loop(calm, due_in_h=72)                    # 三天后才到期
    out2 = render(calm, Persona())
    assert "惦记着" not in out2                         # 不临期不唠叨


# ── extractor 校验 + 序列化 ──────────────────────────────────────────
def test_extractor_validates_due_hours():
    from app.affect.extractor import _validate

    s = AffectState.fresh(Persona())
    ok = _validate({"new_commitment": "周六打电话", "commitment_due_hours": 72}, s)
    assert ok["commitment_due_hours"] == 72.0
    # 下限 0.1h(0.6.0 实测调整:"10分钟后打电话"=0.17h 必须能通过)
    assert _validate({"new_commitment": "10分钟后打给你", "commitment_due_hours": 0.17},
                     s)["commitment_due_hours"] == 0.17
    assert _validate({"new_commitment": "x", "commitment_due_hours": 0.05},
                     s)["commitment_due_hours"] is None
    # 越界/无承诺/非数值 → None
    assert _validate({"new_commitment": "x", "commitment_due_hours": 999}, s)["commitment_due_hours"] is None
    assert _validate({"commitment_due_hours": 5}, s)["commitment_due_hours"] is None
    assert _validate({"new_commitment": "x", "commitment_due_hours": "周六"}, s)["commitment_due_hours"] is None


def test_due_ms_survives_serialization():
    s = AffectState.fresh(Persona())
    loop = _commit_loop(s, due_in_h=10)
    restored = AffectState.from_dict(s.to_dict())
    assert restored.open_loops[0].due_ms == loop.due_ms
    # 旧版(0.5.0)存的 loop dict 没有 due_ms → 默认 None,不炸
    old = s.to_dict()
    for l in old["open_loops"]:
        l.pop("due_ms", None)
    assert AffectState.from_dict(old).open_loops[0].due_ms is None
