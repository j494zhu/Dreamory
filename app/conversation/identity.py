"""
L1【核心人格】— the固有认知 (fixed cognition): the most stubborn part of the
self. Meta-cognition, name/gender/birthday, long-term habits. It is loaded into
L1 on every turn and, for this stage, treated as FROZEN (per spec: 暂且将其固化).

Some fields are meant to need special machinery to change (e.g. age), some need a
very strong reason (e.g. hobbies); none of that is wired up yet — we just pin it.
"""
from __future__ import annotations

from app.affect.persona import Persona


def build_core_identity(persona: Persona, tag_vocab: str = "") -> str:
    """Compile the frozen self-cognition block injected at the top of L1."""
    lines = [
        f"你是{persona.name}。这是你最核心、最不可动摇的自我认知:",
        f"- 关于你自己:{persona.profile}",
        f"- 说话风格:{persona.style}",
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
