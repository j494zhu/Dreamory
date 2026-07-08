"""
动力学层:纯代码,无 LLM。
你想要的所有"耦合关系"都显式地写在这里 —— 可读、可调、可单测。

结构:
  apply_time()    时间效应(只有 arousal 衰减; chat超过6小时以后重置 patience;loop 跨会话计龄)
  apply_events()  事件 → 标量更新 + loop 管理(规则表)
  transition()    模式状态机(带滞回)
"""
from __future__ import annotations

from app import clock

from .state import (
    AFFECTION_MAX,
    AFFECTION_MIN,
    AffectState,
    Grievance,
    OpenLoop,
    affection_tier,
)
from .persona import Persona

# ── 可调常数(集中放,方便做参数扫描)──────────────────────────
AROUSAL_HALFLIFE_S = 45 * 60      # 情绪激活半衰期 45 分钟
SESSION_GAP_S = 6 * 3600          # 超过 6 小时视为新会话
SEC_HIT_TURN_AWAY = 0.05          # turn_away 砸在 bid 上的安全感损失(×anxiety)
SEC_HIT_TURN_AGAINST = 0.10
SEC_GAIN_TURN_TOWARD = 0.02       # 非对称:正面增益约为负面冲击的一半以下
SEC_GAIN_REPAIR = 0.05
SEC_GAIN_LOOP_CLOSED = 0.04
REPAIR_ACCEPT_BASE = 0.35         # security 高于此值才可能接受修复(×avoidance 调高)
REPAIR_PITY_ATTEMPTS = 3          # 连续哄到第几次,即使 security 不够也保底松动(避免"哄不动"死锁)
WITHDRAW_PRESSURE = 5             # loop 总压力超过此值 → withdrawn(÷avoidance 调低)
PROBING_SECURITY = 0.40           # security 低于此值且出现模糊信号 → probing
LOOP_ESCALATE_SESSIONS = 1        # 挂起回路熬过几个会话后沉淀为旧账
MIN_TURNS_IN_NEG_MODE = 2         # 滞回:负面模式最短停留轮数

# ── 好感度(affection,0~200)常数 ────────────────────────────
# 定位:security 是"此刻安不安全",affection 是"我们走到哪一步了"。
# 涨得慢(每轮零点几到几点),跌得比涨快(×anxiety),高段位增益递减(越熟越难再刷)。
AFF_GAIN_TURN_TOWARD = 0.8        # 投标被接住
AFF_GAIN_WARM_TONE = 0.4          # 语气温暖/深情(与 turn_toward 可叠加,都是小步)
AFF_GAIN_REPAIR = 2.0             # 修复被真心接受:和好比日常升温更拉近关系
AFF_GAIN_REPAIR_PITY = 0.5        # 保底松动:气消了但没真的暖回来
AFF_GAIN_LOOP_CLOSED = 1.5        # 他记得并回应了她挂着的事
AFF_HIT_TURN_AWAY = 1.2           # 投标被忽略(×anxiety)
AFF_HIT_TURN_AGAINST = 3.0        # 攻击性回应(×anxiety)
AFF_HIT_GRIEVANCE = 2.0           # 挂起回路沉淀成旧账时,每条额外掉的好感
AFF_HIGH_GAIN_KNEE = 100.0        # 高于此值(恋人档)后,正向增益开始递减
AFF_HIGH_GAIN_FLOOR = 0.3         # 递减的下限(200 时仍保留 30% 增益)
AFF_DECAY_GRACE_DAYS = 3.0        # 离线超过此天数,好感开始缓慢回落(冷落是有代价的)
AFF_DECAY_PER_DAY = 1.0           # 超出宽限期后每天回落多少
AFF_DECAY_FLOOR = 60.0            # 回落下限:感情会淡,但共同经历不会清零(只降到"友好"档)

# ── 激素常数(多时间尺度:arousal 表达不了"吵完架第二天早上还是不得劲")────
# 定位:adrenaline 是"此刻上头"(比 arousal 更尖),oxytocin 是"亲密余温"(小时级),
# cortisol 是"压力残渣"(天级)。全部由事件规则触发,LLM 永远不直接设定数值。
ADRENALINE_HALFLIFE_S = 20 * 60
OXYTOCIN_HALFLIFE_S = 3 * 3600
CORTISOL_HALFLIFE_S = 20 * 3600

ADR_TURN_AGAINST = 0.40           # 被攻击:肾上腺素飙
ADR_EXCITED = 0.20                # 狂喜/兴奋的好事也会心跳加速
ADR_MILESTONE = 0.30              # 跨过关系里程碑那一刻,心怦怦跳
OXY_REPAIR = 0.30                 # 真心和好后的柔软余韵
OXY_MILESTONE_UP = 0.35           # 升到新的关系阶段
OXY_COMFORT_MET = 0.15            # 求安慰被认真接住
COR_TURN_AGAINST = 0.25           # 被攻击的压力残留
COR_COMFORT_IGNORED = 0.15        # 求安慰被敷衍,憋出来的委屈
COR_GRIEVANCE = 0.10              # 每沉淀一条旧账,多一层慢性压力
OXY_REPAIR_EASE = 0.35            # 催产素余韵中修复门槛的最大放松比例
COR_REPAIR_HARDEN = 0.30          # 压力残留中修复门槛的最大抬高比例
COR_PATIENCE_DRAG = 2             # 新会话时,高皮质醇最多磨掉的耐心点数
OXY_AFF_AMPLIFY = 0.25            # 亲密余韵放大好感增益的最大比例

# ── 承诺兑现闭环(v0.6)────────────────────────────────────────
# 定位:承诺不再是"open_loop 换皮"。有明确时间的承诺按到期时刻判爽约
# (不再是熬过一晚就沉旧账——"周六打电话"周二变旧账是 bug 不是特性);
# 含糊承诺("下次…")熬过多个会话没下文才算食言。
COMMITMENT_GRACE_H = 2.0          # 过点这么久还没动静才算爽约(时间估算本就粗)
VAGUE_COMMITMENT_SESSIONS = 4     # 含糊承诺熬过几个会话才沉旧账
AFF_GAIN_PROMISE_KEPT = 2.5       # 说到做到:比一般回路关闭(1.5)更建立信任
AFF_GAIN_PROMISE_LATE = 1.2       # 迟到的兑现也算数,但打了折
SEC_GAIN_PROMISE_KEPT = 0.05      # "他说话算数"的安全感
OXY_PROMISE_KEPT = 0.10           # 被兑现的踏实感,带一点暖
AFF_HIT_BROKEN_PROMISE = 3.0      # 爽约比一般旧账(2.0)更伤——说话不算数是关系毒药
COR_BROKEN_PROMISE = 0.15         # 爽约的压力残留


def _gain_scale(affection: float) -> float:
    """正向增益的高段位递减:恋人档以上,同样的好事带来的提升越来越小。"""
    if affection <= AFF_HIGH_GAIN_KNEE:
        return 1.0
    frac = (affection - AFF_HIGH_GAIN_KNEE) / (AFFECTION_MAX - AFF_HIGH_GAIN_KNEE)
    return max(AFF_HIGH_GAIN_FLOOR, 1.0 - frac)


def _aff_gain(state: AffectState, amount: float) -> None:
    # 催产素余韵放大亲密增益:热恋余温里,同样的好事更拉近关系。
    boost = 1.0 + OXY_AFF_AMPLIFY * state.oxytocin
    state.affection = min(AFFECTION_MAX, state.affection + amount * boost * _gain_scale(state.affection))


def _aff_hit(state: AffectState, amount: float) -> None:
    state.affection = max(AFFECTION_MIN, state.affection - amount)


def _hormone(state: AffectState, name: str, amount: float) -> None:
    setattr(state, name, min(1.0, getattr(state, name) + amount))


def repair_threshold(state: AffectState, persona: Persona) -> float:
    """修复被接受的 security 门槛。回避型更难哄;感情越深越容易心软;
    亲密余韵(oxytocin)让人容易心软,压力残留(cortisol)让人更难被哄。
    锚点:affection=100(恋人)、激素归零时系数为 1.0,与旧行为一致。"""
    aff_factor = 1.25 - 0.5 * state.affection / AFFECTION_MAX
    hormone_factor = (1.0 - OXY_REPAIR_EASE * state.oxytocin) \
        * (1.0 + COR_REPAIR_HARDEN * state.cortisol)
    return REPAIR_ACCEPT_BASE * persona.avoidance * aff_factor * hormone_factor


def warm_streak_floor(affection: float) -> int:
    """新会话 warm_streak 不再一律清零:感情越深,重逢时的底色越暖。
    (对应旧注释"引入一个好感度系统,折算成可变的基础 warm_streak"。)"""
    if affection >= 140:
        return 2
    if affection >= 85:
        return 1
    return 0


def _patience_bonus(affection: float) -> int:
    """好感度折算的耐心加成:深爱的人,可忍受的敷衍多一些;失望透顶时耐心更薄。"""
    if affection >= 160:
        return 2
    if affection >= 100:
        return 1
    if affection < 20:
        return -1
    return 0


def apply_time(state: AffectState, persona: Persona, now: float | None = None) -> None:
    now = now or clock.now_s()
    gap = max(0.0, now - state.last_ts)

    # 只有 arousal 与激素自然冷却。security 不自发回升,open_loops 不衰减。
    state.arousal *= 0.5 ** (gap / AROUSAL_HALFLIFE_S) # 半衰期样式的指数衰减
    state.adrenaline *= 0.5 ** (gap / ADRENALINE_HALFLIFE_S)
    state.oxytocin *= 0.5 ** (gap / OXYTOCIN_HALFLIFE_S)
    state.cortisol *= 0.5 ** (gap / CORTISOL_HALFLIFE_S)

    # 好感度的离线回落:被晾了太多天,感情会淡一点,但不会淡穿"友好"档
    # (共同的经历还在,只是热度凉了)。
    idle_days = gap / 86400.0
    if idle_days > AFF_DECAY_GRACE_DAYS and state.affection > AFF_DECAY_FLOOR:
        decay = (idle_days - AFF_DECAY_GRACE_DAYS) * AFF_DECAY_PER_DAY
        state.affection = max(AFF_DECAY_FLOOR, state.affection - decay)

    if gap > SESSION_GAP_S:
        # 新会话:耐心重置(好感度折算加成;隔夜没散的压力磨掉一截),
        # warm_streak 落回好感度决定的底座
        cortisol_drag = int(round(COR_PATIENCE_DRAG * state.cortisol))
        state.patience = max(1, persona.base_patience + _patience_bonus(state.affection) - cortisol_drag)
        state.warm_streak = warm_streak_floor(state.affection)
        state.dull_streak = 0
        # 新会话:昨天编的"我就是累了"翻篇了,解释口径重置(narrative.py 按需再生成)
        state.self_narrative = ""
        state.narrative_mode = ""
        for loop in state.open_loops:
            loop.sessions_old += 1                   # 挂起的事熬过了一晚,更重了
        _escalate_old_loops(state, now)

    state.last_ts = now


def _escalate_old_loops(state: AffectState, now_s: float) -> None:
    """挂太久的回路 → 沉淀为旧账(grievance),并磨掉一截好感。

    承诺走自己的时间语义(v0.6):有到期时刻的,过点+宽限才算爽约;
    含糊的("下次…"),熬过 VAGUE_COMMITMENT_SESSIONS 个会话没下文才算食言。
    没到期的承诺安然越冬——"周六打电话"周二早上不该变成旧账。"""
    now_ms_v = now_s * 1000.0
    remain = []
    for loop in state.open_loops:
        if loop.type == "commitment":
            overdue = (loop.due_ms is not None
                       and now_ms_v > loop.due_ms + COMMITMENT_GRACE_H * 3600_000)
            vague_stale = loop.due_ms is None and loop.sessions_old >= VAGUE_COMMITMENT_SESSIONS
            if overdue or vague_stale:
                state.grievances.append(Grievance(
                    id=loop.id, weight=loop.weight,
                    content=f"{loop.content}(说好的没做到)",
                ))
                _aff_hit(state, AFF_HIT_BROKEN_PROMISE)      # 爽约比一般旧账更伤
                _hormone(state, "cortisol", COR_BROKEN_PROMISE)
            else:
                remain.append(loop)
        elif loop.sessions_old >= LOOP_ESCALATE_SESSIONS and loop.weight >= 3:
            state.grievances.append(Grievance(
                id=loop.id, weight=loop.weight,
                content=f"{loop.content}(一直没被回应/兑现)",
            ))
            _aff_hit(state, AFF_HIT_GRIEVANCE)
            _hormone(state, "cortisol", COR_GRIEVANCE)   # 旧账是慢性压力源
        else:
            remain.append(loop)
    state.open_loops = remain


def apply_events(state: AffectState, ev: dict, persona: Persona,
                 her_last_msg: str | None, his_msg: str) -> list[str]:
    """事件 → 状态。返回 trace(人话日志,调试用)。"""
    trace = []
    state.turn += 1
    tier_before = affection_tier(state.affection)

    # ── 1. 他回应了挂起回路 → 关闭 + 安全感小幅修复 ────────────
    # 承诺兑现走分级奖励(v0.6):说到做到 > 迟到兑现 > 一般回路关闭。
    if ev["addresses_loop_id"]:
        loop = state.find_loop(ev["addresses_loop_id"])
        if loop:
            state.open_loops.remove(loop)
            if loop.type == "commitment":
                on_time = (loop.due_ms is None
                           or clock.now_ms() <= loop.due_ms + COMMITMENT_GRACE_H * 3600_000)
                state.security = min(1.0, state.security + SEC_GAIN_PROMISE_KEPT)
                _aff_gain(state, AFF_GAIN_PROMISE_KEPT if on_time else AFF_GAIN_PROMISE_LATE)
                _hormone(state, "oxytocin", OXY_PROMISE_KEPT)   # 说话算数的踏实感
                trace.append(f"承诺兑现({'准时' if on_time else '迟到但兑现了'}): {loop.content}")
            else:
                state.security = min(1.0, state.security + SEC_GAIN_LOOP_CLOSED)
                _aff_gain(state, AFF_GAIN_LOOP_CLOSED)
                trace.append(f"回路关闭: {loop.content}")

    # ── 2. 投标被如何对待(杠杆最大的一条规则)─────────────────
    bid = ev["bid_in_her_last_msg"]
    resp = ev["his_response_type"]
    patience_charged = False  # 去重:本条消息是否已被更重的事件扣过耐心

    if bid != "none" and resp == "turn_away": # 如果她上条消息里确实隐含了情感期待，而他选择了忽略/敷衍
        state.patience -= 2
        patience_charged = True
        state.security = max(0.0, state.security - SEC_HIT_TURN_AWAY * persona.anxiety)
        state.warm_streak = 0
        _aff_hit(state, AFF_HIT_TURN_AWAY * persona.anxiety)
        if bid in ("seeking_comfort", "venting"):
            _hormone(state, "cortisol", COR_COMFORT_IGNORED)  # 委屈憋着,压力残留
        weight = 3 if bid in ("seeking_comfort", "venting") else 2
        snippet = (her_last_msg or "")[:40]
        state.open_loops.append(OpenLoop.new(
            "unanswered_bid",
            f"她说'{snippet}…'({bid}),他敷衍带过/转移了话题",
            state.turn, weight,
        ))
        trace.append(f"投标被忽略({bid}): patience-2, security受损, 新增挂起回路")

    elif bid != "none" and resp == "turn_toward": # 如果她上条消息里确实隐含了情感期待，而他选择了承接
        state.security = min(1.0, state.security + SEC_GAIN_TURN_TOWARD)
        state.warm_streak += 1
        _aff_gain(state, AFF_GAIN_TURN_TOWARD)
        if bid == "seeking_comfort":
            _hormone(state, "oxytocin", OXY_COMFORT_MET)  # 被认真接住的暖
        trace.append("投标被接住: security+, warm_streak+")

    if resp == "turn_against":  # 如果被攻击了
        state.arousal = min(1.0, state.arousal + 0.35)
        state.security = max(0.0, state.security - SEC_HIT_TURN_AGAINST * persona.anxiety)
        state.patience -= 2
        patience_charged = True
        state.warm_streak = 0
        _aff_hit(state, AFF_HIT_TURN_AGAINST * persona.anxiety)
        _hormone(state, "adrenaline", ADR_TURN_AGAINST)
        _hormone(state, "cortisol", COR_TURN_AGAINST)
        trace.append("攻击性回应: arousal飙升, security重损, 应激激素上涌")

    # ── 3. 敷衍语气即使不构成 turn_away 也磨损耐心 ─────────────
    # 去重:若这条消息已因 turn_away/turn_against 扣过耐心,敷衍语气不再叠加扣分,
    # 只清零 warm_streak(敷衍就是不温暖,与是否再扣耐心无关)。
    if "perfunctory" in ev["tone_flags"] or "dismissive" in ev["tone_flags"]:
        state.warm_streak = 0
        if not patience_charged:
            state.patience -= 1
            trace.append("敷衍语气: patience-1")

    if "affectionate" in ev["tone_flags"] or "warm" in ev["tone_flags"]:
        state.warm_streak += 1
        _aff_gain(state, AFF_GAIN_WARM_TONE)

    if "excited" in ev["tone_flags"]:
        _hormone(state, "adrenaline", ADR_EXCITED)   # 好事也会心跳加速

    # ── 4. 修复尝试:接受与否由 security 门控(耦合点)──────────
    # 保底松动:security 太低时门控会永远拒绝 → "哄不动"死锁。连续哄到第
    # REPAIR_PITY_ATTEMPTS 次,即使 security 不够也勉强消气(但只小幅回升,人没真的暖回来)。
    repair_accepted = False
    if ev["is_repair_attempt"] and state.mode in ("conflict", "withdrawn"):
        state.repair_attempts += 1
        threshold = repair_threshold(state, persona)   # 回避型更难被哄好;感情深则容易心软
        if state.security > threshold:
            repair_accepted = True
            state.security = min(1.0, state.security + SEC_GAIN_REPAIR)
            state.arousal = max(0.0, state.arousal - 0.2)
            state.repair_attempts = 0
            _aff_gain(state, AFF_GAIN_REPAIR)
            _hormone(state, "oxytocin", OXY_REPAIR)   # 和好后的柔软余韵
            trace.append("修复尝试被接受")
        elif state.repair_attempts >= REPAIR_PITY_ATTEMPTS:
            repair_accepted = True
            state.security = min(1.0, state.security + SEC_GAIN_REPAIR * 0.5)
            state.arousal = max(0.0, state.arousal - 0.15)
            state.repair_attempts = 0
            _aff_gain(state, AFF_GAIN_REPAIR_PITY)
            trace.append(f"修复被勉强接受(哄到第{REPAIR_PITY_ATTEMPTS}次,气消了但还没真的好)")
        else:
            trace.append(f"修复尝试被拒绝(security太低,已哄{state.repair_attempts}次,'道歉有什么用')")
    elif resp == "turn_against":
        state.repair_attempts = 0  # 又开吵:之前哄的次数作废,重新数

    # ── 5. 他做出新承诺 → 记入 open_loops(带到期时刻),将来检查兑现 ──
    if ev["new_commitment"]:
        due_h = ev.get("commitment_due_hours")
        due_ms = int(clock.now_ms() + due_h * 3600_000) if due_h else None
        loop = OpenLoop.new(
            "commitment", f"他承诺:{ev['new_commitment']}", state.turn,
            weight=3, due_ms=due_ms,
        )
        state.open_loops.append(loop)
        ev["_commitment_loop"] = loop   # pipeline 读它来挂"到点催"的 ping
        trace.append(
            f"记录承诺: {ev['new_commitment']}"
            + (f"(约{due_h:.0f}小时后到期)" if due_h else "(没说具体时间)")
        )

    # ── 6. 旧账被话题触碰 → arousal 上浮 ───────────────────────
    if ev["topic_relates_to_grievance_id"]:
        state.arousal = min(1.0, state.arousal + 0.2)
        trace.append("话题触碰旧账: arousal+")

    # ── 7. 好感度分层跨越检测(升到"恋人"或跌出某档都是大事)────
    tier_after = affection_tier(state.affection)
    if tier_after != tier_before:
        direction = "up" if _tier_floor(tier_after[0]) > _tier_floor(tier_before[0]) else "down"
        ev["_tier_shift"] = {
            "from": tier_before[1], "to": tier_after[1], "direction": direction,
            # 跨过"恋人"线(无论上下)是里程碑级事件
            "milestone": "lover" in (tier_before[0], tier_after[0]),
        }
        arrow = "↑" if direction == "up" else "↓"
        if direction == "up":
            _hormone(state, "adrenaline", ADR_MILESTONE)  # 跨过那条线的瞬间,心怦怦跳
            _hormone(state, "oxytocin", OXY_MILESTONE_UP)
        trace.append(f"关系阶段变化: {tier_before[1]} {arrow} {tier_after[1]}")
    else:
        ev["_tier_shift"] = None

    # ── 8. 话题热度计数(注意力转移的确定性信号)─────────────────
    # "变淡" = 双方都没有情感投标、语气也没温度的一轮;有任何投标/温度即重置。
    engaged = (
        bid != "none" or ev["new_bid_from_him"] or ev["is_repair_attempt"]
        or bool(set(ev["tone_flags"]) & {"warm", "affectionate", "excited", "demanding"})
        or resp == "turn_against"          # 吵架不是"淡",是另一种投入
    )
    if engaged:
        state.dull_streak = 0
    else:
        state.dull_streak += 1
        if state.dull_streak >= 2:
            trace.append(f"话题变淡(dull_streak={state.dull_streak})")

    ev["_repair_accepted"] = repair_accepted
    return trace


def _tier_floor(tier_key: str) -> float:
    from .state import AFFECTION_TIERS
    return next(f for f, k, _ in AFFECTION_TIERS if k == tier_key)


def transition(state: AffectState, ev: dict, persona: Persona) -> list[str]:
    """模式状态机。滞回:进负面模式容易,出来难。"""
    trace = []
    old = state.mode
    in_mode_turns = state.turn - state.mode_entered_turn

    def goto(m: str, why: str):
        nonlocal old
        if m != state.mode:
            state.mode = m
            state.mode_entered_turn = state.turn
            trace.append(f"模式 {old} → {m}({why})")

    resp = ev["his_response_type"]
    pressure = state.loop_pressure()
    # 感情越深,越能扛住挂起压力才关闭自己(锚点:affection=100 时系数 1.0)
    aff_tolerance = 0.75 + 0.5 * state.affection / AFFECTION_MAX
    withdraw_threshold = WITHDRAW_PRESSURE / max(persona.avoidance, 0.3) * aff_tolerance

    # 优先级从高到低:
    if resp == "turn_against" and state.mode != "conflict":
        # 回避型受攻击倾向于冷掉而不是吵起来
        if persona.avoidance > 1.4 and persona.expressiveness < 0.8:
            goto("withdrawn", "被攻击但选择关闭")
        else:
            goto("conflict", "被攻击性回应")

    elif state.mode == "conflict":
        if ev.get("_repair_accepted"):
            goto("repair_pending", "修复尝试被接受")
        # 否则停留(滞回:conflict 不会因为他换了话题就自动结束)

    elif state.mode == "repair_pending":
        if resp == "turn_toward" or "affectionate" in ev["tone_flags"]:
            goto("neutral", "修复后他持续示好,回到正常")
        elif resp in ("turn_away",) or "dismissive" in ev["tone_flags"]:
            goto("conflict", "刚道完歉又敷衍,二次伤害")

    elif state.mode == "withdrawn":
        if ev.get("_repair_accepted") or ev["addresses_loop_id"]:
            goto("neutral", "他发现/回应了她在意的事")
        elif in_mode_turns >= MIN_TURNS_IN_NEG_MODE and ev["bid_in_her_last_msg"] != "none" \
                and resp == "turn_away" and persona.expressiveness > 1.2:
            goto("conflict", "冷淡期再次被无视,直接爆发(高表达型)")
        # 低表达型:继续沉默,模式不变

    else:  # warm / neutral / probing
        if state.patience <= 0 or pressure >= withdraw_threshold:
            goto("withdrawn", f"耐心耗尽(patience={state.patience})或挂起压力过大(pressure={pressure})")
        elif state.security < PROBING_SECURITY and (
                "perfunctory" in ev["tone_flags"] or resp == "turn_away"):
            goto("probing", "安全感低 + 模糊信号 → 进入试探")
        elif state.warm_streak >= 2:
            goto("warm", "连续被好好对待")
        elif state.mode == "warm" and ("perfunctory" in ev["tone_flags"] or resp == "turn_away"):
            goto("neutral", "热度被敷衍打断")
        elif state.mode == "probing" and resp == "turn_toward":
            goto("neutral", "试探得到正面结果")

    return trace
