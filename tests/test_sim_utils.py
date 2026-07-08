"""
老化脚手架工具函数单测(auto 用户消息清洗)。不碰 LLM / DB。

    pytest tests/test_sim_utils.py -v
"""
from scripts.simulate import _clean_user_msg, _flatten


def test_clean_flattens_lines_and_strips_quotes():
    assert _clean_user_msg('"晚上请你吃火锅\n早点睡"') == "晚上请你吃火锅 早点睡"
    assert _clean_user_msg("「在吗」") == "在吗"
    assert _clean_user_msg("") == "在吗"


def test_clean_truncates_at_sentence_boundary():
    """超长消息在句读处截断,绝不产出半截话(实测教训:吊句污染曲线)。"""
    long = "今天好累啊!" + "然后我们聊了很多关于未来的计划还有旅行的安排" * 3
    out = _clean_user_msg(long, cap=30)
    assert out == "今天好累啊!"          # 回退到最近的完整句
    assert not out.endswith("…")


def test_clean_short_passthrough():
    assert _clean_user_msg("嗯,今天太忙了,先睡了") == "嗯,今天太忙了,先睡了"


def test_flatten_expands_repeat():
    steps = [{"repeat": 2, "steps": [{"say": "a"}, {"advance_hours": 1}]}, {"night": True}]
    flat = list(_flatten(steps))
    assert [s.get("say") for s in flat] == ["a", None, "a", None, None]
    assert flat[-1] == {"night": True}
