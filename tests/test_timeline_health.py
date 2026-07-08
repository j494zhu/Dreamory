"""
时间序列快照 + 记忆健康度 + 跨进程锁 key 的纯函数单测。不碰 LLM / DB。

    pytest tests/test_timeline_health.py -v
"""
import uuid

import numpy as np

from app.affect.persona import Persona
from app.affect.state import AffectState, Grievance, OpenLoop
from app.conversation import timeline
from app.db_locks import chat_key
from app.memory import health


class _FakeSession:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)


# ── timeline.record:state → 快照行 ───────────────────────────────────
def test_snapshot_captures_full_state():
    s = AffectState.fresh(Persona())
    s.turn, s.mode, s.affection = 7, "warm", 123.4
    s.adrenaline, s.oxytocin, s.cortisol = 0.1, 0.2, 0.3
    s.open_loops = [OpenLoop.new("commitment", "他承诺周末打电话", 5, weight=3)]
    s.grievances = [Grievance(id="g1", content="旧账", weight=2),
                    Grievance(id="g2", content="已解决", weight=1, resolved=True)]

    fake = _FakeSession()
    snap = timeline.record(fake, uuid.uuid4(), s, source="message",
                           events={"his_response_type": "turn_toward",
                                   "bid_in_her_last_msg": "sharing"})
    assert fake.added == [snap]
    assert snap.turn == 7 and snap.mode == "warm"
    assert snap.affection == 123.4 and snap.cortisol == 0.3
    assert snap.loop_pressure == 3          # 挂起回路权重和
    assert snap.grievances == 1             # 只数未解决的
    assert snap.event == "turn_toward" and snap.bid == "sharing"
    assert snap.source == "message"


def test_snapshot_without_events_is_blank_annotated():
    snap = timeline.record(_FakeSession(), uuid.uuid4(),
                           AffectState.fresh(Persona()), source="timer")
    assert snap.source == "timer"
    assert snap.event == "" and snap.bid == ""


# ── health 纯函数 ────────────────────────────────────────────────────
def _unit(v):
    v = np.asarray(v, dtype=np.float32)
    return (v / np.linalg.norm(v)).tolist()


def test_centroid_distance_zero_for_same_and_one_for_orthogonal():
    a = [_unit([1, 0, 0])] * 5
    assert health.centroid_cos_distance(a, a) < 1e-6
    b = [_unit([0, 1, 0])] * 5
    assert abs(health.centroid_cos_distance(a, b) - 1.0) < 1e-6
    assert health.centroid_cos_distance([], a) is None


def test_mean_pairwise_cos_detects_redundancy():
    dup = [_unit([1, 2, 3])] * 10
    assert health.mean_pairwise_cos(dup) > 0.999          # 全重复 → 冗余拉满
    varied = [_unit([1, 0, 0]), _unit([0, 1, 0]), _unit([0, 0, 1])]
    assert health.mean_pairwise_cos(varied) < 0.01
    assert health.mean_pairwise_cos([_unit([1, 1, 1])]) is None


def test_mode_volatility_counts_transitions():
    stable = ["warm"] * 20
    assert health.mode_volatility(stable) == 0.0
    flapping = ["warm", "conflict"] * 10
    assert health.mode_volatility(flapping) == 1.0
    assert health.mode_volatility(["warm"] * 3) is None   # 样本不足


def test_score_and_labels_consistent():
    assert health.score_from_flags([]) == 100
    assert health.score_from_flags(["identity_drift"]) == 75
    all_flags = list(health.PENALTIES)
    assert health.score_from_flags(all_flags) == 0
    assert set(health.LABELS) == set(health.PENALTIES)    # 每个旗标都有人话标签


# ── 跨进程锁 key ─────────────────────────────────────────────────────
def test_chat_key_deterministic_int32():
    cid = uuid.uuid4()
    k1, k2 = chat_key(cid), chat_key(cid)
    assert k1 == k2                                       # 确定性
    assert -(2**31) <= k1 < 2**31                         # pg int4 范围
    other = chat_key(uuid.uuid4())
    assert isinstance(other, int)


def test_lock_spaces_distinct():
    from app import db_locks

    spaces = [db_locks.LOCK_DREAM, db_locks.LOCK_LIFE_SIM,
              db_locks.LOCK_NIGHT, db_locks.LOCK_EVOLUTION]
    assert len(set(spaces)) == len(spaces)
