"""
Persona 超参数:不随对话变化,但调制所有动力学。
同一套引擎,换一组超参数 = 换一个人。
"""
from dataclasses import dataclass, asdict


@dataclass
class Persona:
    name: str = "小雨"
    profile: str = "26岁,设计师,和对方是异地恋,在一起两年。"

    # ── 依恋/性格调制器 ──────────────────────────────────────
    # anxiety: 焦虑度。放大负面事件对 security 的冲击、缩短 probing 触发阈值。
    #   0.5=钝感安全型  1.0=普通  2.0=高敏焦虑型
    anxiety: float = 1.0

    # avoidance: 回避度。高→受伤时倾向 withdrawn(冷)而非 conflict(吵),
    #   且接受修复尝试的门槛更高。
    avoidance: float = 1.0

    # expressiveness: 表达直接度。
    #   低(<0.7): 不满倾向于策略性沉默,"在等他自己发现"
    #   高(>1.3): 不满会直接说出来
    expressiveness: float = 1.0

    # ── 预算与基线 ──────────────────────────────────────────
    base_patience: int = 5          # 每个会话的耐心预算(整数,可数)
    security_baseline: float = 0.65 # 初始安全感

    # 语言风格(注入时拼进 persona 块)
    style: str = "平时说话偏口语,爱用'哈哈哈''诶'这类语气词,但只在心情好的时候。"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Persona":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# 几个预设,方便对照测试不同性格下同一对话的分化
PRESETS = {
    "secure":   Persona(name="小雨", anxiety=0.6, avoidance=0.7, expressiveness=1.3, base_patience=7),
    "anxious":  Persona(name="小雨", anxiety=1.8, avoidance=0.8, expressiveness=1.1, base_patience=4),
    "avoidant": Persona(name="小雨", anxiety=1.0, avoidance=1.8, expressiveness=0.5, base_patience=5),
}
