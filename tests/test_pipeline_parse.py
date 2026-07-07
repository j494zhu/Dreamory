"""
管线纯函数单测:多消息解析、timer 标签解析、连发段拼接、时间感知渲染。
全部离线,不碰 LLM / DB。

    pytest tests/test_pipeline_parse.py -v
"""
import time
from types import SimpleNamespace

from app.config import settings
from app.conversation import pipeline
from app.models import Speaker


# ── 多消息:<reply> × N ────────────────────────────────────


def test_parse_single_reply():
    thinking, replies = pipeline._parse_generation(
        "<thinking>心里一紧</thinking><reply>嗯。</reply>"
    )
    assert thinking == "心里一紧"
    assert replies == ["嗯。"]


def test_parse_multiple_replies_keeps_order():
    raw = ("<thinking>好想跟他讲!</thinking>"
           "<reply>你猜怎么着!!</reply><reply>我过稿了!!!</reply><reply>就是上次说的那个</reply>")
    thinking, replies = pipeline._parse_generation(raw)
    assert replies == ["你猜怎么着!!", "我过稿了!!!", "就是上次说的那个"]


def test_parse_caps_reply_count():
    raw = "".join(f"<reply>第{i}条</reply>" for i in range(8))
    _, replies = pipeline._parse_generation(raw)
    assert len(replies) == pipeline.MAX_REPLIES_PER_TURN


def test_parse_fallback_without_tags():
    thinking, replies = pipeline._parse_generation("<thinking>唉</thinking>就一句话")
    assert thinking == "唉"
    assert replies == ["就一句话"]


def test_parse_drops_empty_replies():
    _, replies = pipeline._parse_generation("<reply>  </reply><reply>在的</reply>")
    assert replies == ["在的"]


# ── 定时器:<timer minutes="X"> ────────────────────────────


def test_extract_timer_parses_and_strips():
    raw = '<reply>等我10分钟,洗完澡来找你</reply><timer minutes="10">继续聊白天面试的事</timer>'
    cleaned, timer = pipeline._extract_timer(raw)
    assert timer == {"minutes": 10, "topic": "继续聊白天面试的事"}
    assert "<timer" not in cleaned
    assert "<reply>" in cleaned          # reply 原样保留


def test_extract_timer_absent():
    cleaned, timer = pipeline._extract_timer("<reply>晚安</reply>")
    assert timer is None
    assert cleaned == "<reply>晚安</reply>"


def test_extract_timer_clamps_minutes():
    _, hi = pipeline._extract_timer('<timer minutes="99999">x</timer>')
    _, lo = pipeline._extract_timer('<timer minutes="0">x</timer>')
    assert hi["minutes"] == settings.timer_max_minutes
    assert lo["minutes"] == 1


def test_extract_timer_tolerates_unquoted_minutes():
    _, timer = pipeline._extract_timer("<timer minutes=5>回来汇报</timer>")
    assert timer == {"minutes": 5, "topic": "回来汇报"}


# ── 她的"上一条消息" = 完整连发段 ──────────────────────────


def _msg(speaker, content):
    return SimpleNamespace(speaker=speaker, content=content)


def test_her_last_burst_joins_consecutive_agent_messages():
    mems = [
        _msg(Speaker.user, "在吗"),
        _msg(Speaker.agent, "跟你说个事"),
        _msg(Speaker.agent, "我今天面试过了!"),
        _msg(Speaker.agent, "你快夸我"),
    ]
    assert pipeline._her_last_burst(mems) == "跟你说个事\n我今天面试过了!\n你快夸我"


def test_her_last_burst_skips_trailing_user_messages():
    """他连发了两条,她的'上一段发言'仍是更早的那段。"""
    mems = [
        _msg(Speaker.agent, "今天好累"),
        _msg(Speaker.user, "哦"),
        _msg(Speaker.user, "对了帮我看个东西"),
    ]
    assert pipeline._her_last_burst(mems) == "今天好累"


def test_her_last_burst_none_when_she_never_spoke():
    assert pipeline._her_last_burst([_msg(Speaker.user, "你好")]) is None
    assert pipeline._her_last_burst([]) is None


# ── 时间感知 ───────────────────────────────────────────────


def test_time_context_mentions_gap():
    now = time.time()
    ctx = pipeline._time_context(now - 3 * 3600, now=now)
    assert "现在是" in ctx
    assert "3小时前" in ctx


def test_time_context_first_turn_has_no_gap():
    now = time.time()
    ctx = pipeline._time_context(now - 999999, now=now, first_turn=True)
    assert "上一次说话" not in ctx


def test_humanize_gap_buckets():
    assert pipeline._humanize_gap(30) == "刚刚"
    assert pipeline._humanize_gap(10 * 60) == "10分钟前"
    assert pipeline._humanize_gap(5 * 3600) == "5小时前"
    assert pipeline._humanize_gap(3 * 86400) == "3天前"
