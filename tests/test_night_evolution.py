"""
夜间代理载荷校验 + persona 演化门控的纯函数单测。不碰 LLM / DB。

    pytest tests/test_night_evolution.py -v
"""
from datetime import datetime

from app.affect.persona import Persona
from app.conversation import evolution, night_agent


# ── 夜间代理:LLM 载荷校验(输出不可信,逐项清洗)────────────────────────
def test_validate_payload_cleans_facts():
    data = {"facts": ["他不吃香菜", 123, "", "  x  " * 100, "他下周三考试"]}
    out = night_agent.validate_payload(data)
    assert "他不吃香菜" in out["facts"]
    assert "他下周三考试" in out["facts"]
    assert all(isinstance(f, str) and len(f) <= night_agent.FACT_MAX_LEN
               for f in out["facts"])
    assert 123 not in out["facts"]


def test_validate_payload_caps_counts():
    data = {"facts": [f"事实{i}" for i in range(20)],
            "plans": [{"label": f"p{i}", "at_hm": "10:00"} for i in range(9)]}
    out = night_agent.validate_payload(data)
    assert len(out["facts"]) == night_agent.MAX_FACTS
    assert len(out["plans"]) == night_agent.MAX_PLANS


def test_validate_payload_rejects_bad_plans_and_routines():
    data = {
        "plans": [{"label": "交稿", "at_hm": "25:99"},          # 非法时间
                  {"label": "", "at_hm": "10:00"},              # 空标签
                  {"label": "健身", "at_hm": "19:30"}],
        "routine": [{"label": "睡觉", "start_hm": "夜里", "end_hm": "08:30"},   # 非法
                    {"label": "睡觉", "start_hm": "01:00", "end_hm": "09:00",
                     "days": [0, 8]},                            # 星期越界
                    {"label": "睡觉", "start_hm": "01:00", "end_hm": "09:00",
                     "days": None}],
    }
    out = night_agent.validate_payload(data)
    assert out["plans"] == [{"label": "健身", "at_hm": "19:30"}]
    assert len(out["routines"]) == 1
    assert out["routines"][0]["start_hm"] == "01:00"


def test_validate_payload_diary_none_when_empty():
    assert night_agent.validate_payload({"diary": "   "})["diary"] is None
    assert night_agent.validate_payload({"diary": None})["diary"] is None
    long = "今天" * 200
    out = night_agent.validate_payload({"diary": long})
    assert len(out["diary"]) == night_agent.DIARY_MAX_LEN


def test_plan_due_ms_lands_tomorrow():
    now = datetime(2026, 7, 7, 23, 50)
    due = datetime.fromtimestamp(night_agent.plan_due_ms("09:30", now) / 1000)
    assert (due.year, due.month, due.day, due.hour, due.minute) == (2026, 7, 8, 9, 30)


# ── persona 演化:append-only 门控 ────────────────────────────────────
def test_proposal_validation_caps_and_cleans():
    out = evolution.validate_proposal({
        "style_append": "  开始叫他'老公'  ",
        "profile_append": "x" * 200,        # 超长 → 丢
        "identity_append": "他是我\n最重要的人",  # 换行 → 空格
        "junk": "ignored",
    })
    assert out["style_append"] == "开始叫他'老公'"
    assert "profile_append" not in out
    assert "\n" not in out["identity_append"]


def test_proposal_validation_empty_means_no_change():
    assert evolution.validate_proposal({}) == {}
    assert evolution.validate_proposal({"style_append": "", "profile_append": None}) == {}


def test_apply_to_persona_is_append_only():
    p = Persona(name="小雨", profile="26岁,设计师。", style="爱用语气词。")
    evolution.apply_to_persona(p, {"style_append": "开始叫他'笨蛋'。",
                                   "profile_append": "和他在一起了。"})
    assert p.name == "小雨"                       # 名字永不动
    assert p.style.startswith("爱用语气词。")      # 原文保留
    assert "笨蛋" in p.style
    assert p.profile.endswith("和他在一起了。")


def test_tier_unlocks_exclude_low_tiers():
    """低档不演化:还没熟到'这个人因你而变'。"""
    for low in ("disappointed", "cold", "stranger", "friendly"):
        assert low not in evolution.TIER_UNLOCKS
    for high in ("crush", "lover", "devoted", "oath"):
        assert high in evolution.TIER_UNLOCKS
