"""
日程表 + 工具协议的纯函数单测。不碰 LLM / DB。

    pytest tests/test_schedule_tools.py -v
"""
import uuid
from datetime import datetime

from app.conversation import schedule as sched
from app.conversation import tools
from app.models import Memory, MemoryKind, ScheduleItem, Speaker, now_ms


def _routine(label, days=None, start="00:30", end="08:30", created=0):
    item = ScheduleItem(kind="routine", label=label, days=days,
                        start_hm=start, end_hm=end)
    item.created_ms = created
    return item


def _at(h, m=0, weekday_shift=0):
    """2026-07-06 是周一;shift 换星期。"""
    return datetime(2026, 7, 6 + weekday_shift, h, m)


# ── routine 匹配 ─────────────────────────────────────────────────────
def test_plain_range_matches_inside_only():
    work = _routine("上班", days=[0, 1, 2, 3, 4], start="09:30", end="18:00")
    assert sched.current_routine([work], _at(10)) is work
    assert sched.current_routine([work], _at(20)) is None
    assert sched.current_routine([work], _at(10, weekday_shift=5)) is None  # 周六不上班


def test_overnight_range_crosses_midnight():
    sleep = _routine("睡觉", start="23:00", end="07:00")
    assert sched.is_sleeping([sleep], _at(23, 30))
    assert sched.is_sleeping([sleep], _at(3))
    assert not sched.is_sleeping([sleep], _at(12))


def test_overnight_days_belong_to_start_day():
    """周五 23:00~03:00 → 周六凌晨 2 点算命中,周日凌晨不算。"""
    late = _routine("熬夜赶稿", days=[4], start="23:00", end="03:00")
    assert sched._routine_active(late, _at(2, weekday_shift=5))       # 周六凌晨
    assert not sched._routine_active(late, _at(2, weekday_shift=6))   # 周日凌晨


def test_wake_ms_lands_at_end_of_sleep():
    sleep = _routine("睡觉", start="00:30", end="08:30")
    now = _at(3)
    wake = sched.wake_ms([sleep], now)
    assert wake is not None
    woke = datetime.fromtimestamp(wake / 1000)
    assert (woke.hour, woke.minute) == (8, 30)
    assert woke.date() == now.date()


def test_sleep_wins_when_overlapping():
    sleep = _routine("睡觉", start="00:30", end="08:30", created=5)
    other = _routine("晨跑", start="06:00", end="09:00", created=1)
    assert sched.current_routine([other, sleep], _at(7)).label == "睡觉"


# ── L1 块编译 ────────────────────────────────────────────────────────
def test_render_block_mentions_sleep_and_upcoming():
    sleep = _routine("睡觉", start="00:30", end="08:30")
    oneoff = ScheduleItem(kind="oneoff", label="给甲方交稿",
                          due_ms=now_ms() + 3600 * 1000)
    block = sched.render_block([sleep, oneoff], _at(3))
    assert "睡觉" in block or "吵醒" in block
    assert "给甲方交稿" in block


def test_render_block_empty_schedule_is_empty():
    assert sched.render_block([], _at(12)) == ""


def test_render_block_ignores_far_future_oneoff():
    faraway = ScheduleItem(kind="oneoff", label="下个月的展",
                           due_ms=now_ms() + 30 * 86400 * 1000)
    assert "下个月的展" not in sched.render_block([faraway], _at(12))


# ── 工具 schema / 参数解析 ───────────────────────────────────────────
def test_specs_include_timer_only_when_allowed():
    names = [s["function"]["name"] for s in tools.build_specs(allow_timer=True)]
    assert names == ["search_memory", "grep_memory", "set_timer"]
    names = [s["function"]["name"] for s in tools.build_specs(allow_timer=False)]
    assert "set_timer" not in names


def test_days_range_maps_to_ts_window():
    ts_min, ts_max = tools._days_range({"newer_than_days": 7, "older_than_days": 1})
    assert ts_min is not None and ts_max is not None
    assert ts_min < ts_max
    ts_min, ts_max = tools._days_range({})
    assert ts_min is None and ts_max is None
    ts_min, ts_max = tools._days_range({"newer_than_days": "abc"})
    assert ts_min is None


def test_relevant_filters_low_score_filler():
    """exclude_ids 排掉真命中后,剩下的低分填充项必须被滤掉,
    否则模型误以为还有料可挖,把轮数烧在冗余检索上。
    (相关性看裸分数 raw:goal 偏置是排序手段,救不了不相关的命中。)"""
    from app.memory.retrieval import Hit

    good = Hit(Memory(content="真命中", speaker=Speaker.agent, tags=[]), 0.72, "content", raw=0.72)
    filler = Hit(Memory(content="边角料", speaker=Speaker.agent, tags=[]), 0.31, "content", raw=0.31)
    biased = Hit(Memory(content="被goal抬分的边角料", speaker=Speaker.agent, tags=[]),
                 0.66, "content", raw=0.31)
    kept = tools._relevant([good, filler, biased])
    assert kept == [good]
    assert tools._relevant([filler]) == []


def test_fmt_memory_distinguishes_speakers_and_life_events():
    m_user = Memory(content="我下周出差", speaker=Speaker.user, tags=[])
    m_user.ts_ms = now_ms() - 60_000
    m_user.kind = MemoryKind.message
    assert "他:" in tools._fmt_memory(m_user)

    m_life = Memory(content="今天甲方又改需求", speaker=Speaker.agent, tags=[])
    m_life.ts_ms = now_ms() - 60_000
    m_life.kind = MemoryKind.life_event
    assert "生活" in tools._fmt_memory(m_life)


# ── dispatch 的降级路径(不需要真 session)────────────────────────────
class _FakeSession:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)


def test_dispatch_bad_json_degrades():
    import asyncio
    out = asyncio.run(tools.dispatch(
        None, chat=None, name="search_memory", arguments="{not json",
        exclude_ids=set(), pending_timer_count=0,
    ))
    assert "参数" in out.text


def test_dispatch_unknown_tool_degrades():
    import asyncio
    out = asyncio.run(tools.dispatch(
        None, chat=None, name="fly_to_moon", arguments="{}",
        exclude_ids=set(), pending_timer_count=0,
    ))
    assert "没有" in out.text


def test_set_timer_respects_quota_and_clamps():
    import asyncio
    from app.config import settings

    class _Chat:
        id = uuid.uuid4()

    fake = _FakeSession()
    # 配额已满 → 拒绝
    out = asyncio.run(tools._do_set_timer(
        fake, _Chat(), {"minutes": 10, "memo": "x"},
        pending_count=settings.timer_max_pending,
    ))
    assert out.timer is None and not fake.added

    # 正常 → 入库 + 分钟数被钳制
    out = asyncio.run(tools._do_set_timer(
        fake, _Chat(), {"minutes": 999999, "memo": "洗完澡去找他"}, pending_count=0,
    ))
    assert out.timer is not None
    assert out.timer["minutes"] == settings.timer_max_minutes
    assert len(fake.added) == 1
