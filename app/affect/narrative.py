"""
自我解释生成器 — "解释与真实动因分离"(confabulation)。

心理学底子:人对自己状态的解释是"解释器"事后编的,接触不到真实的动因机制
(选择盲视实验里,人们会给自己从没做过的选择编出自信的理由)。真人说"没事,
就是累了"的时候往往真的相信这句话——真实原因(被敷衍磨掉的耐心、压着的
挂起回路、没散的皮质醇)只体现在行为里,不体现在自我报告里。

本项目的天然优势:真实动因层已经存在,就是 dynamics 维护的 AffectState。
本模块做的是给她配一个"解释器":
  真实动因 → 只驱动行为(injector 的行为指令,她自己"意识不到");
  自我解释 → 由这里的错误归因规则表生成,她被问"怎么了"时真诚地相信并说出。

错误归因的方向性(照着人类的系统性偏差抄):
  高估:近期表面事件(生活里的糟心小事)、身体状态(累/没睡好)、"没事"最小化;
  低估:依恋焦虑、累积的小委屈、激素残留。
  persona.insight(内省力)决定说到点子上的概率——回避型/傲娇几乎从不。

关键性质:
  - 口径黏性:编的理由存进 state,会话内不变(每轮换借口才是真的假),
    模式切换/跨会话/过老才刷新;
  - 回报机制免费:他顺着编的理由哄 → 不命中 addresses_loop_id,security 几乎
    不涨("哄不到点子上");他猜中她说不出的真实原因 → 回路关闭 + 大额修复。
    dynamics 现有机制天然区分这两种情况,confabulation 只是把真相从"她直接
    告诉你"变成"需要你去发现"。

纯代码,零 LLM;rng 可注入,可确定性单测。
"""
from __future__ import annotations

import random

from .persona import Persona
from .state import AffectState

NARRATIVE_STALE_TURNS = 12    # 同一口径最多撑多少轮(太久了人也会换说法)
TRUTH_BASE_P = 0.10           # insight=0 时说到点子上的概率
TRUTH_INSIGHT_GAIN = 0.75     # insight=1 时约 0.85

# ── 错误归因素材池(她真诚相信的"原因";生成端会用她自己的口吻重述)────
CONFAB_PHYSICAL = (
    "就是有点累,没什么",
    "昨晚没睡好,今天一天都提不起劲",
    "可能快到生理期了,人有点烦",
    "这两天头有点昏沉,别管我",
)
CONFAB_MINIMIZE = (
    "没事啊,我挺好的,你别多想",
    "没什么,真的,过会儿就好了",
)


def needs_narrative(state: AffectState) -> bool:
    """有没有需要解释的状态。心平气和时没人问'你怎么了',也就不需要口径。"""
    return (
        state.mode in ("probing", "withdrawn", "conflict", "repair_pending")
        or state.patience <= 2
        or state.security < 0.45
        or state.cortisol > 0.45
        or state.loop_pressure() >= 3
    )


def _truthful(state: AffectState) -> str:
    """高内省:把真实动因里权重最高的说到点子上(仍是人话,不是数据)。"""
    if state.open_loops:
        top = max(state.open_loops, key=lambda l: l.weight)
        return f"其实还是那件事压着:{top.content[:36]}"
    if state.security < 0.45:
        return "说到底是没安全感,总觉得你最近离我有点远"
    if state.patience <= 2:
        return "是被一次一次敷衍磨的,不是这一句两句的事"
    if state.cortisol > 0.45:
        return "是前两天的事一直没缓过来,心里一直紧绷着"
    return "心里堵着,说不清具体是哪件事,但知道跟你有关"


def _confabulated(surface_candidates: tuple[str, ...] | list[str],
                  rng: random.Random) -> str:
    """低内省:真诚地编一个错误归因。优先甩锅给生活里真实发生过的糟心事
    (来自 life_sim 正史——具体、可复述、绝不穿帮),其次身体归因,最后最小化。"""
    pools: list[str] = []
    for ev in surface_candidates:
        ev = (ev or "").strip()
        if ev:
            pools.append(f"是{ev[:32]}这事闹的,跟你没关系")
    pools.extend(CONFAB_PHYSICAL)
    if not pools:
        pools.extend(CONFAB_MINIMIZE)
    # 三成概率直接最小化("没事") —— 最常见的人类口径
    if rng.random() < 0.3:
        return rng.choice(CONFAB_MINIMIZE)
    return rng.choice(pools)


def refresh(state: AffectState, persona: Persona,
            surface_candidates: tuple[str, ...] | list[str] = (),
            rng: random.Random | None = None) -> bool:
    """按需刷新自我解释。返回口径是否发生了变化。

    刷新时机(黏性优先,能不换就不换):
      - 状态回到不需要解释 → 清空("翻篇了");
      - 没有口径但需要 / 模式变了(生气的理由和试探的理由不是一套)/
        口径撑太久 → 重新生成。
    """
    rng = rng or random

    if not needs_narrative(state):
        if state.self_narrative:
            state.self_narrative = ""
            state.narrative_mode = ""
            return True
        return False

    stale = state.turn - state.narrative_turn > NARRATIVE_STALE_TURNS
    if state.self_narrative and state.narrative_mode == state.mode and not stale:
        return False   # 口径还新鲜,坚持同一套说法

    p_truth = TRUTH_BASE_P + TRUTH_INSIGHT_GAIN * max(0.0, min(1.0, persona.insight))
    if rng.random() < p_truth:
        text = _truthful(state)
    else:
        text = _confabulated(surface_candidates, rng)

    state.self_narrative = text
    state.narrative_mode = state.mode
    state.narrative_turn = state.turn
    return True
