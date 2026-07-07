"""
对话状态。四层结构:
  1. mode        —— 离散行为模式(主开关,带滞回)
  2. open_loops / grievances —— 结构化关系记忆(一等公民,不衰减,只能被解决或沉淀)
  3. 标量        —— 只保留真正像物理量的三个:arousal / security / patience
  4. affection   —— 好感度(0~200,跨会话的慢积累,类似碧蓝航线的好感系统):
                   security 是"这段关系此刻安不安全"的快照,affection 是
                   "我们走到哪一步了"的长程刻度,升得慢、掉得也有底。
"""
from __future__ import annotations
import time
import uuid
from dataclasses import dataclass, field, asdict

MODES = ("warm", "neutral", "probing", "withdrawn", "conflict", "repair_pending")

# ── 好感度分层(下限, key, 中文标签)。50=陌生人默认,100=恋人,200=封顶。──
AFFECTION_TIERS = (
    (0,   "disappointed", "失望"),
    (20,  "cold",         "冷淡"),
    (40,  "stranger",     "陌生"),
    (60,  "friendly",     "友好"),
    (85,  "crush",        "心动"),
    (100, "lover",        "恋人"),
    (140, "devoted",      "挚爱"),
    (180, "oath",         "誓约"),
)

AFFECTION_MIN, AFFECTION_MAX = 0.0, 200.0


def affection_tier(affection: float) -> tuple[str, str]:
    """好感度数值 → (tier_key, 中文标签)。注入时只用文字层级,绝不注入数字。"""
    key, label = AFFECTION_TIERS[0][1], AFFECTION_TIERS[0][2]
    for floor, k, l in AFFECTION_TIERS:
        if affection >= floor:
            key, label = k, l
    return key, label


@dataclass
class OpenLoop:
    """挂起的回路:没被回应的投标、没兑现的承诺。不随时间消失。"""
    id: str
    type: str          # "unanswered_bid" | "commitment" | "unanswered_question"
    content: str       # 自然语言描述,如 "她说工作压力大,他只回了'哦'"
    created_turn: int
    weight: int = 1    # 重要程度 1~5,决定沉淀为旧账时的杀伤力
    sessions_old: int = 0

    @staticmethod
    def new(type: str, content: str, turn: int, weight: int = 1) -> "OpenLoop":
        return OpenLoop(id=uuid.uuid4().hex[:8], type=type, content=content,
                        created_turn=turn, weight=weight)


@dataclass
class Grievance:
    """已沉淀的旧账。平时不发作,在 conflict / 相关话题被触发时注入。"""
    id: str
    content: str
    weight: int
    resolved: bool = False


@dataclass
class AffectState:
    mode: str = "neutral"
    mode_entered_turn: int = 0          # 滞回用:负面模式有最短停留时间

    open_loops: list = field(default_factory=list)    # list[OpenLoop]
    grievances: list = field(default_factory=list)    # list[Grievance]

    arousal: float = 0.1      # 情绪激活度 0~1,快变量,指数冷却
    security: float = 0.65    # 关系安全感 0~1,慢变量,非对称更新,几乎不自发回升
    patience: int = 5         # 本会话耐心预算,整数;新会话重置
    affection: float = 50.0   # 好感度 0~200,最慢变量,跨会话积累(50=陌生,100=恋人)

    repair_attempts: int = 0  # 当前冷战/冲突里他连续尝试哄的次数(保底松动用,消气或重新开吵时清零)
    warm_streak: int = 0      # 连续正面回应计数(进入 warm 的条件)
    turn: int = 0
    last_ts: float = field(default_factory=time.time)

    # ── 序列化(存库用)─────────────────────────────────────
    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "AffectState":
        s = cls(**{k: v for k, v in d.items()
                   if k in cls.__dataclass_fields__ and k not in ("open_loops", "grievances")})
        s.open_loops = [OpenLoop(**l) for l in d.get("open_loops", [])]
        s.grievances = [Grievance(**g) for g in d.get("grievances", [])]
        return s

    @classmethod
    def fresh(cls, persona) -> "AffectState":
        return cls(security=persona.security_baseline, patience=persona.base_patience,
                   affection=getattr(persona, "affection_start", 50.0))

    # ── 便捷查询 ───────────────────────────────────────────
    def loop_pressure(self) -> int:
        """挂起回路的总压力(weight 求和),用于触发 withdrawn。"""
        return sum(l.weight for l in self.open_loops)

    def affection_tier(self) -> tuple[str, str]:
        """当前好感度层级 (key, 中文标签)。"""
        return affection_tier(self.affection)

    def find_loop(self, loop_id: str) -> OpenLoop | None:
        return next((l for l in self.open_loops if l.id == loop_id), None)
