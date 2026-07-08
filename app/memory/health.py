"""
记忆健康度与漂移检测 — "数字生命寿命"的第一块仪表盘。

长期运行的伴侣会不会悄悄坏掉?坏法无非几种:打标系统跟不上(pending 积压)、
词表失去区分度(单 tag 垄断)、记忆库被近重复内容灌水、她最近的情绪轴悄悄
偏离既往的自己、她的言行漂离人格锚点、情绪模式高频震荡(状态机不稳定)。
本模块把这些全部变成可计算的指标 + 阈值旗标 + 一个 0~100 总分。

设计约束:
  - 全部只读,零写入;除 identity_drift 需要一次 embed 调用外,零 LLM 零额外嵌入
    (其余指标全用 L3 里已存的向量和 affect_snapshots 时间序列)。
  - 数据不足时指标为 None、不打旗——新对话天然满分,不制造噪音。
  - 阈值是模块常数,不进 settings(调它们的人应该看着这份代码调)。

消费方:GET /chats/{id}/health(按需体检);夜间代理(每晚体检,有旗标时
强制跑一次 Dream 做全局维护——这是 spec 里"漂移阈值触发全局维护"的落地)。
"""
from __future__ import annotations

import uuid

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.affect.persona import Persona
from app.llm import embeddings
from app.models import AffectSnapshot, Chat, Memory, MemoryKind, Speaker

# ── 阈值(超过即打旗)──────────────────────────────────────────────────
PENDING_RATIO_MAX = 0.5      # 最近记忆里 pending(没打上tag)的比例
DOMINANT_TAG_MAX = 0.5       # 单个 tag 覆盖率(spec: >50% 即无区分度)
REDUNDANCY_MAX = 0.92        # 最近内容向量的平均两两余弦(近重复灌水)
EMOTION_DRIFT_MAX = 0.35     # 近期情绪轴质心 vs 既往基线质心的余弦距离
IDENTITY_DRIFT_MAX = 0.65    # 人格锚点 vs 近期言行质心的余弦距离
VOLATILITY_MAX = 0.45        # 模式切换频率(次/轮,状态机震荡)

# ── 各旗标的扣分权重(score = 100 - Σ命中旗标)────────────────────────
PENALTIES = {
    "tagging_backlog": 15,
    "tag_monoculture": 10,
    "redundancy": 15,
    "emotion_drift": 20,
    "identity_drift": 25,
    "mode_volatility": 15,
}
LABELS = {
    "tagging_backlog": "打标积压:大量记忆没有拿到 tag,检索的类别轴在失效",
    "tag_monoculture": "词表垄断:某个 tag 覆盖过半记忆,失去区分度",
    "redundancy": "近重复灌水:最近的记忆彼此过于相似,库在膨胀而信息没有增加",
    "emotion_drift": "情绪轴漂移:她近期的心境质心明显偏离既往基线",
    "identity_drift": "人格漂移:她最近的言行偏离人格锚点",
    "mode_volatility": "模式震荡:情绪状态机高频跳变,行为会显得喜怒无常",
}

# 样本窗口
RECENT_MEMORIES = 200        # pending/tag 统计窗口
REDUNDANCY_SAMPLE = 30       # 冗余检测取最近多少条内容向量
EMOTION_RECENT = 15          # 情绪漂移:近期窗口
EMOTION_BASELINE = 40        # 情绪漂移:基线窗口(近期窗口之前的)
IDENTITY_SAMPLE = 20         # 人格漂移:她最近多少条发言
VOLATILITY_WINDOW = 60       # 模式震荡:看最近多少个快照
MIN_SAMPLES = 10             # 任何指标的最小样本量,不足则 None


# ── 纯函数(可单测)────────────────────────────────────────────────────
def _norm_rows(mat: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(mat, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return mat / n


def centroid_cos_distance(group_a: list, group_b: list) -> float | None:
    """两组向量的质心余弦距离(1-cos)。任一组为空 → None。"""
    if not group_a or not group_b:
        return None
    ca = _norm_rows(np.asarray(group_a, dtype=np.float32)).mean(axis=0)
    cb = _norm_rows(np.asarray(group_b, dtype=np.float32)).mean(axis=0)
    na, nb = np.linalg.norm(ca), np.linalg.norm(cb)
    if not na or not nb:
        return None
    return float(1.0 - np.dot(ca, cb) / (na * nb))


def mean_pairwise_cos(vectors: list) -> float | None:
    """一组向量的平均两两余弦(冗余度)。样本 <2 → None。"""
    if len(vectors) < 2:
        return None
    X = _norm_rows(np.asarray(vectors, dtype=np.float32))
    sims = X @ X.T
    n = len(vectors)
    iu = np.triu_indices(n, k=1)
    return float(sims[iu].mean())


def mode_volatility(modes: list[str]) -> float | None:
    """模式切换频率:切换次数 / (样本数-1)。样本不足 → None。"""
    if len(modes) < MIN_SAMPLES:
        return None
    changes = sum(1 for a, b in zip(modes, modes[1:]) if a != b)
    return changes / (len(modes) - 1)


def score_from_flags(flag_keys: list[str]) -> int:
    return max(0, 100 - sum(PENALTIES.get(k, 0) for k in flag_keys))


# ── 主入口 ───────────────────────────────────────────────────────────
async def compute_health(session: AsyncSession, chat: Chat) -> dict:
    metrics: dict = {}
    flags: list[dict] = []

    def check(key: str, value: float | None, threshold: float) -> None:
        metrics[key] = None if value is None else round(value, 3)
        if value is not None and value > threshold:
            flags.append({"key": key, "label": LABELS[key],
                          "value": round(value, 3), "threshold": threshold})

    # 1+2. 打标积压 / 词表垄断(最近 RECENT_MEMORIES 条的 pending 与 tag 分布)
    rows = (
        await session.execute(
            select(Memory.pending, Memory.tags)
            .where(Memory.chat_id == chat.id)
            .order_by(Memory.ts_ms.desc())
            .limit(RECENT_MEMORIES)
        )
    ).all()
    metrics["sample_memories"] = len(rows)
    if len(rows) >= MIN_SAMPLES:
        check("tagging_backlog",
              sum(1 for p, _ in rows if p) / len(rows), PENDING_RATIO_MAX)
        tag_counts: dict[str, int] = {}
        for _, tags in rows:
            for t in (tags or []):
                tag_counts[t] = tag_counts.get(t, 0) + 1
        dominant = max(tag_counts.values()) / len(rows) if tag_counts else None
        metrics["tag_monoculture"] = None if dominant is None else round(dominant, 3)
        # 冷启动豁免(0.5.0 实测教训):种子词表刚建、tag 还没长开时,单 tag 天然
        # 占大头——记忆够多且词表成型(≥3个tag)后,垄断才是真问题。
        mature = len(rows) >= 30 and len(tag_counts) >= 3
        if dominant is not None and mature and dominant > DOMINANT_TAG_MAX:
            flags.append({"key": "tag_monoculture", "label": LABELS["tag_monoculture"],
                          "value": round(dominant, 3), "threshold": DOMINANT_TAG_MAX})
    else:
        metrics["tagging_backlog"] = metrics["tag_monoculture"] = None

    # 3. 近重复灌水(最近 REDUNDANCY_SAMPLE 条对话内容向量的平均两两余弦)
    vecs = (
        await session.execute(
            select(Memory.content_vec)
            .where(Memory.chat_id == chat.id,
                   Memory.kind == MemoryKind.message,
                   Memory.content_vec.isnot(None))
            .order_by(Memory.ts_ms.desc())
            .limit(REDUNDANCY_SAMPLE)
        )
    ).scalars().all()
    check("redundancy",
          mean_pairwise_cos(list(vecs)) if len(vecs) >= MIN_SAMPLES else None,
          REDUNDANCY_MAX)

    # 4. 情绪轴漂移(她的情绪向量:近期质心 vs 之前的基线质心)
    evecs = (
        await session.execute(
            select(Memory.emotion_vec)
            .where(Memory.chat_id == chat.id,
                   Memory.speaker == Speaker.agent,
                   Memory.emotion_vec.isnot(None))
            .order_by(Memory.ts_ms.desc())
            .limit(EMOTION_RECENT + EMOTION_BASELINE)
        )
    ).scalars().all()
    recent, baseline = list(evecs[:EMOTION_RECENT]), list(evecs[EMOTION_RECENT:])
    check("emotion_drift",
          centroid_cos_distance(recent, baseline)
          if len(recent) >= MIN_SAMPLES and len(baseline) >= MIN_SAMPLES else None,
          EMOTION_DRIFT_MAX)

    # 5. 人格漂移(人格锚点 embed vs 她最近发言的内容质心;唯一一次嵌入调用)
    utter = (
        await session.execute(
            select(Memory.content_vec)
            .where(Memory.chat_id == chat.id,
                   Memory.speaker == Speaker.agent,
                   Memory.kind == MemoryKind.message,
                   Memory.content_vec.isnot(None))
            .order_by(Memory.ts_ms.desc())
            .limit(IDENTITY_SAMPLE)
        )
    ).scalars().all()
    identity_drift = None
    if len(utter) >= MIN_SAMPLES:
        persona = Persona.from_dict(chat.persona) if chat.persona else Persona()
        anchor_text = chat.core_identity or f"{persona.name}。{persona.profile} {persona.style}"
        anchor = await embeddings.embed_one(anchor_text)
        identity_drift = centroid_cos_distance([anchor], list(utter))
    check("identity_drift", identity_drift, IDENTITY_DRIFT_MAX)

    # 6. 模式震荡(affect_snapshots 时间序列)
    modes = (
        await session.execute(
            select(AffectSnapshot.mode)
            .where(AffectSnapshot.chat_id == chat.id)
            .order_by(AffectSnapshot.ts_ms.desc())
            .limit(VOLATILITY_WINDOW)
        )
    ).scalars().all()
    check("mode_volatility", mode_volatility(list(reversed(modes))), VOLATILITY_MAX)

    return {
        "score": score_from_flags([f["key"] for f in flags]),
        "flags": flags,
        "metrics": metrics,
    }
