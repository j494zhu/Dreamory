"""
动力学层:纯代码,无 LLM。
你想要的所有"耦合关系"都显式地写在这里 —— 可读、可调、可单测。

结构:
  apply_time()    时间效应(只有 arousal 衰减; chat超过6小时以后重置 patience;loop 跨会话计龄)
  apply_events()  事件 → 标量更新 + loop 管理(规则表)
  transition()    模式状态机(带滞回)
"""
from __future__ import annotations
import time
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


def _gain_scale(affection: float) -> float:
    """正向增益的高段位递减:恋人档以上,同样的好事带来的提升越来越小。"""
    if affection <= AFF_HIGH_GAIN_KNEE:
        return 1.0
    frac = (affection - AFF_HIGH_GAIN_KNEE) / (AFFECTION_MAX - AFF_HIGH_GAIN_KNEE)
    return max(AFF_HIGH_GAIN_FLOOR, 1.0 - frac)


def _aff_gain(state: AffectState, amount: float) -> None:
    state.affection = min(AFFECTION_MAX, state.affection + amount * _gain_scale(state.affection))


def _aff_hit(state: AffectState, amount: float) -> None:
    state.affection = max(AFFECTION_MIN, state.affection - amount)


def repair_threshold(state: AffectState, persona: Persona) -> float:
    """修复被接受的 security 门槛。回避型更难哄;感情越深越容易心软。
    锚点:affection=100(恋人)时系数为 1.0,与旧行为一致。"""
    aff_factor = 1.25 - 0.5 * state.affection / AFFECTION_MAX
    return REPAIR_ACCEPT_BASE * persona.avoidance * aff_factor


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
    now = now or time.time()
    gap = max(0.0, now - state.last_ts)

    # 只有 arousal 自然冷却。security 不自发回升,open_loops 不衰减。
    state.arousal *= 0.5 ** (gap / AROUSAL_HALFLIFE_S) # 半衰期样式的指数衰减

    # 好感度的离线回落:被晾了太多天,感情会淡一点,但不会淡穿"友好"档
    # (共同的经历还在,只是热度凉了)。
    idle_days = gap / 86400.0
    if idle_days > AFF_DECAY_GRACE_DAYS and state.affection > AFF_DECAY_FLOOR:
        decay = (idle_days - AFF_DECAY_GRACE_DAYS) * AFF_DECAY_PER_DAY
        state.affection = max(AFF_DECAY_FLOOR, state.affection - decay)

    if gap > SESSION_GAP_S:
        # 新会话:耐心重置(好感度折算加成),warm_streak 落回好感度决定的底座
        state.patience = max(1, persona.base_patience + _patience_bonus(state.affection))
        state.warm_streak = warm_streak_floor(state.affection)
        for loop in state.open_loops:
            loop.sessions_old += 1                   # 挂起的事熬过了一晚,更重了
        _escalate_old_loops(state)

    state.last_ts = now


def _escalate_old_loops(state: AffectState) -> None:
    """挂太久的重要回路 → 沉淀为旧账(grievance),并磨掉一截好感。"""
    remain = []
    for loop in state.open_loops:
        if loop.sessions_old >= LOOP_ESCALATE_SESSIONS and loop.weight >= 3:
            state.grievances.append(Grievance(
                id=loop.id, weight=loop.weight,
                content=f"{loop.content}(一直没被回应/兑现)",
            ))
            _aff_hit(state, AFF_HIT_GRIEVANCE)
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
    if ev["addresses_loop_id"]:
        loop = state.find_loop(ev["addresses_loop_id"])
        if loop:
            state.open_loops.remove(loop)
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
        trace.append("投标被接住: security+, warm_streak+")

    if resp == "turn_against":  # 如果被攻击了
        state.arousal = min(1.0, state.arousal + 0.35)
        state.security = max(0.0, state.security - SEC_HIT_TURN_AGAINST * persona.anxiety)
        state.patience -= 2
        patience_charged = True
        state.warm_streak = 0
        _aff_hit(state, AFF_HIT_TURN_AGAINST * persona.anxiety)
        trace.append("攻击性回应: arousal飙升, security重损")

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

    # ── 5. 他做出新承诺 → 记入 open_loops,将来检查兑现 ─────────
    if ev["new_commitment"]:
        state.open_loops.append(OpenLoop.new(
            "commitment", f"他承诺:{ev['new_commitment']}", state.turn, weight=3))
        trace.append(f"记录承诺: {ev['new_commitment']}")

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
        trace.append(f"关系阶段变化: {tier_before[1]} {arrow} {tier_after[1]}")
    else:
        ev["_tier_shift"] = None

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
