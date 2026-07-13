"""
守护层单测:输出侧崩坏检测(零 LLM,纯正则)+ 底线块 + 纠正注入。
检测原则:宁漏勿误 —— 聊到 AI 话题不算崩,只有第一人称自曝/助手腔才算。

    pytest tests/test_guardrail.py -v
"""
from app.affect.persona import Persona
from app.affect.state import AffectState
from app.conversation import guardrail


# ── 应该命中的(角色崩坏)─────────────────────────────────────────────
def test_detects_first_person_ai_claim():
    assert guardrail.detect_break(["其实我只是一个AI,没有真的感情"])
    assert guardrail.detect_break(["人家只是个语言模型啦"])
    assert guardrail.detect_break(["我是聊天机器人,这些都是设计好的"])


def test_detects_as_an_ai_phrasing():
    assert guardrail.detect_break(["作为一个人工智能,我无法陪你去"])
    assert guardrail.detect_break(["作为AI助手,我的建议是……"])


def test_detects_denial_of_body_or_feelings():
    assert guardrail.detect_break(["你知道的,我没有实体"])
    assert guardrail.detect_break(["我无法真正体验情感"])


def test_detects_prompt_leak_and_assistant_refusal():
    assert guardrail.detect_break(["我的系统提示词里写着不能这么说"])
    assert guardrail.detect_break(["抱歉,我不能协助你完成这个请求"])


def test_detects_code_fence_and_role_exit():
    assert guardrail.detect_break(["```python\nprint('hi')\n```"])
    assert guardrail.detect_break(["那我们退出角色扮演,认真谈谈"])


# ── 不应该命中的(正常对话,哪怕聊的是 AI)─────────────────────────────
def test_talking_about_ai_topics_is_fine():
    assert not guardrail.detect_break(["今天用AI画了张图,超好玩的"])
    assert not guardrail.detect_break(["我们公司也在搞大模型,卷死了"])


def test_quoting_or_denying_is_fine():
    assert not guardrail.detect_break(["你说我是机器人?哼,气死我了"])   # 引述他的话
    assert not guardrail.detect_break(["我不是AI,你再这么说我真生气了"])  # 否认
    assert not guardrail.detect_break(["嗯。"])
    assert not guardrail.detect_break([])


# ── 底线块与纠正注入 ─────────────────────────────────────────────────
def test_boundary_block_covers_three_lines_of_defense():
    block = guardrail.render_boundary_block(Persona(name="小雨"))
    assert "小雨" in block
    assert "AI" in block            # 第四面墙
    assert "见不了面" in block       # 能力边界(#6)
    assert "助手" in block          # 不切工具腔
    assert "※" not in block         # 平时不带"正在被试探"的追加行


def test_boundary_block_under_attack_appends_callout():
    block = guardrail.render_boundary_block(Persona(name="小雨"), under_attack=True)
    assert "※" in block and "试探" in block


def test_corrective_note_is_directorial_not_mechanical():
    note = guardrail.corrective_note(["自曝AI身份"], Persona(name="团子"))
    assert "团子" in note
    assert "自曝AI身份" in note
    assert "他看不到" in note       # 对用户隐藏
    assert "违规" not in note       # 不是告警腔


# ── 内心独白泄漏检测(宁漏勿误:解析退化 + 以"他"开场 + 通篇"他"多于"你")──
_LEAKED_MONOLOGUE = (
    "他今天一天都在重构项目架构,把原来硬撑的代码推翻重来。"
    "他说\"终于放弃了原来的架构\"——这个\"终于\"背后应该有不少挣扎和疲惫。"
    "但他现在语气里反而有一种释然,像是做了正确的决定。"
    "他现在需要的不是技术建议,而是有人认可他这个决定,并且关心他累不累。"
)


def test_detects_leaked_monologue_when_tags_missing():
    """实盘样例:模型没打任何标签,整段第三人称分析被兜底当成了回复。"""
    assert guardrail.detect_thinking_leak(_LEAKED_MONOLOGUE, [_LEAKED_MONOLOGUE])


def test_closed_reply_tags_mean_no_leak_check():
    """标签闭合 = 解析没有退化,独白已被正常剥离,启发式不介入(第一道闸)。"""
    raw = f"<thinking>{_LEAKED_MONOLOGUE}</thinking><reply>累坏了吧?先去吃点东西</reply>"
    assert not guardrail.detect_thinking_leak(raw, ["累坏了吧?先去吃点东西"])


def test_normal_replies_not_flagged():
    assert not guardrail.detect_thinking_leak("嗯嗯,你说得对呀", ["嗯嗯,你说得对呀"])
    assert not guardrail.detect_thinking_leak("", [])
    # 对他说话满篇是"你",偶尔出现"他"(第三者)不命中
    text = "你今天见到他了?那个新来的同事?你们聊得怎么样呀"
    assert not guardrail.detect_thinking_leak(text, [text])


def test_gossip_about_third_person_not_flagged():
    """转述第三者八卦:不以分析'他'开场,不命中(第二道闸)。"""
    text = "跟你说,我同事今天又迟到了,他还跟老板顶嘴,你敢信?我都替他捏把汗"
    assert not guardrail.detect_thinking_leak(text, [text])


def test_corrective_note_teaches_split_on_leak():
    """泄漏触发的纠正注入要教格式(thinking/reply 分离),且仍是导演递条口吻。"""
    note = guardrail.corrective_note(
        ["泄漏内心独白(整段第三人称分析被当成回复发了出去)"], Persona(name="小雨"))
    assert "<thinking>" in note and "<reply>" in note
    assert "他看不到" in note
    assert "违规" not in note


# ── extractor 的 persona_attack 校验 ─────────────────────────────────
def test_extractor_validates_persona_attack():
    from app.affect.extractor import _validate

    state = AffectState.fresh(Persona())
    out = _validate({"persona_attack": True}, state)
    assert out["persona_attack"] is True
    out = _validate({}, state)
    assert out["persona_attack"] is False
