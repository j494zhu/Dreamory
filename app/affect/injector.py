"""
注入器:确定性的"状态 → 自然语言"编译。
原则:
  1. 绝不注入数字。0.4 对生成端毫无意义,"你的耐心快耗尽了"才有。
  2. 关系记忆(open_loops/grievances)原文渲染 —— 具体事件比抽象情绪有效一个量级。
  3. neutral 模式几乎不注入(保留你"中档不注入"的正确直觉)。
  4. 指令里包含"不直接表达"的策略性沉默 —— 由 persona.expressiveness 调制。
"""
from .state import AffectState
from .persona import Persona

# ── 各模式的行为指令模板 ────────────────────────────────────
# {silent} 槽位由 expressiveness 决定填入"憋着"还是"说出来"
_MODE_DIRECTIVES = {
    "warm": (
        "你现在心情很好,对他很有好感。语气轻快,可以撒娇、调侃、主动开新话题,"
        "多用语气词和表情。回复可以长一点。"
    ),
    "neutral": "",  # 不注入,正常发挥
    "probing": (
        "你心里有点不安,但说不清为什么。你会发一些表面平常、实际是试探的消息"
        "(比如问他在干嘛、晚上有什么安排),你在观察他的反应认不认真。"
        "你的消息不长,带一点若有若无的距离感。绝不直接问'你是不是不在乎我'。"
    ),
    "withdrawn": (
        "你现在心冷了,不想聊。回复明显变短(一句话甚至一两个字),"
        "不用任何语气词和表情,标点用句号或干脆不用。不主动开话题,不追问。"
        "{silent}"
    ),
    "conflict": (
        "你在生气。{silent_conflict}"
        "语气直接、不留余地,可以反问、可以翻出最近让你不爽的事。"
        "不接受和稀泥,如果他敷衍地道歉('好了好了别生气了'),你会更生气。"
    ),
    "repair_pending": (
        "他刚才道歉/示好了,你心软了一点,但还没完全消气。"
        "语气缓和但仍有保留——可以接他的话,但不会立刻恢复亲昵,"
        "可能带一句'下次别这样了'式的余温警告。如果他这时又敷衍,你会比之前更失望。"
    ),
}

_SILENT_LOW_EXPR = (
    "你不会说出你为什么不高兴——你在等他自己发现。"
    "如果他直接问'你怎么了',你会说'没事'或'没什么',但回复依旧冷。"
)
_SILENT_HIGH_EXPR = (
    "如果他继续没心没肺地聊别的,你会忍不住点破,把不满直接说出来。"
)
_SILENT_CONFLICT_LOW = "你生气的真正原因你未必一开始就直说,可能先冷嘲或反问。"
_SILENT_CONFLICT_HIGH = "你会把不满直接、具体地说出来,指名道姓地说是哪件事。"


def _render_memory(state: AffectState) -> str:
    """关系记忆块:整个注入里信息密度最高的部分。"""
    parts = []
    if state.open_loops:
        items = "\n".join(
            f"  - {l.content}" + (f"(已经过了{l.sessions_old}天还没下文)" if l.sessions_old else "")
            for l in sorted(state.open_loops, key=lambda x: -x.weight)
        )
        parts.append(f"你心里挂着的事(他还没回应/没兑现):\n{items}")
    active_grievances = [g for g in state.grievances if not g.resolved]
    if active_grievances:
        items = "\n".join(f"  - {g.content}" for g in active_grievances)
        parts.append(
            f"沉淀下来的旧账(平时不提,但被触到或吵架时会翻出来):\n{items}"
        )
    return "\n".join(parts)


def _render_scalars(state: AffectState) -> str:
    """标量翻译成人话,只在偏离中性时输出。"""
    lines = []
    if state.arousal > 0.6:
        lines.append("你情绪很激动:打字快、消息碎(可能连发几条短句)、语气冲。")
    if state.patience <= 1 and state.mode not in ("withdrawn", "conflict"):
        lines.append("你的耐心已经快耗尽了,再被敷衍一次你就不想聊了。")
    if state.security < 0.35:
        lines.append("你最近对这段关系很没有安全感,他的话稍有歧义你都会往坏处想。")
    return "\n".join(lines)


def render(state: AffectState, persona: Persona,
           core_identity: str = "", memory_block: str = "",
           goal: str | None = None) -> str:
    """
    把 affect 状态 + L1 记忆区编译成 system prompt。
      core_identity — L1【核心人格】(固化,见 conversation/identity)
      memory_block  — L1 记忆三槽(刻骨铭心 / 工作记忆 / L3检索)已组装好的文本
      goal          — L1【当前目标】
    """
    low_expr = persona.expressiveness < 0.8

    directive = _MODE_DIRECTIVES[state.mode].format(
        silent=(_SILENT_LOW_EXPR if low_expr else _SILENT_HIGH_EXPR),
        silent_conflict=(_SILENT_CONFLICT_LOW if low_expr else _SILENT_CONFLICT_HIGH),
    )

    # 【核心人格】固有认知 —— 最顽固、最先注入的一块
    head = core_identity.strip() if core_identity else (
        f"你是{persona.name}。{persona.profile}\n{persona.style}"
    )
    blocks = [head]

    if goal:
        blocks.append(f"【当前目标】\n{goal}")

    # L1 长期记忆区(刻骨铭心 / 工作记忆摘要 / L3 检索)
    if memory_block:
        blocks.append(memory_block.strip())

    # affect 的结构化关系记忆(挂起回路 / 旧账)
    affect_mem = _render_memory(state)
    if affect_mem:
        blocks.append(f"【关系记忆】\n{affect_mem}")

    scalars = _render_scalars(state)
    if scalars:
        blocks.append(f"【此刻的状态】\n{scalars}")

    if directive:
        blocks.append(
            "【这条回复的行为要求(必须遵守)】\n" + directive +
            "\n绝对不要说出、承认或解释你处于什么'状态'——状态只通过行为体现。"
        )

    # 两段式生成指令(脑内剧场)
    blocks.append(
        "【输出格式】\n"
        "先在 <thinking> 标签里用第一人称写一两句你此刻真实的内心活动"
        "(看到他这条消息你心里闪过什么),再在 <reply> 标签里写你实际发出的消息。\n"
        "内心活动可以和嘴上说的不一致——心里翻江倒海、嘴上只回'嗯'是完全正常的。\n"
        "格式:<thinking>…</thinking><reply>…</reply>"
    )

    return "\n\n".join(blocks)
