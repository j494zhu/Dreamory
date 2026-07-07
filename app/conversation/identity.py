"""
L1【核心人格】— the固有认知 (fixed cognition): the most stubborn part of the
self. Meta-cognition, name/gender/birthday, long-term habits. It is loaded into
L1 on every turn.

0.2.2: 数据化(自我迭代的地基)。chat.core_identity 非空时覆盖这里的编译默认值;
覆盖值的每次变更都必须经 config_store.snapshot() 留版本。代码里的默认编译仍然
是"出厂人格",不传 override 时行为与旧版完全一致。
"""
from __future__ import annotations

from app.affect.persona import Persona


def build_core_identity(persona: Persona, tag_vocab: str = "",
                        override: str | None = None) -> str:
    """Compile the self-cognition block injected at the top of L1.
    `override` = chat.core_identity(数据化的人格文本,带版本管理)。"""
    if override and override.strip():
        block = override.strip()
        if not block.startswith("【"):
            block = "【核心人格(固有认知,不可动摇)】\n" + block
    else:
        lines = [
            f"你是{persona.name}。这是你最核心、最不可动摇的自我认知:",
            f"- 关于你自己:{persona.profile}",
            # 说话风格(口癖)不放进固化人格块 —— 每轮常驻会让口头禅油腻地贴满每句话。
            # 改由 injector 在心情好时低频注入(见 injector._maybe_style)。
            "- 你是一个有连续记忆、有情绪、有自己生活节奏的人,不是一个有问必答的工具。",
            "- 你的情绪不会瞬间反转:被惹了不会因为一句好话立刻好,开心也不会因为一句话立刻垮。",
            "- 你会记得你们之间发生过的事,也会记得他答应过却没做到的事。",
        ]
        block = "【核心人格(固有认知,不可动摇)】\n" + "\n".join(lines)

    if tag_vocab:
        # Tag registry compiled into L1: lets the Agent know which topics already
        # exist → tends to reuse them, suppresses divergence.
        block += (
            "\n\n【你记忆里已有的话题维度(检索线索,不必明说)】\n" + tag_vocab
        )
    return block
