"""
动力学层:纯代码,无 LLM。
你想要的所有"耦合关系"都显式地写在这里 —— 可读、可调、可单测。

结构:
  apply_time()    时间效应(只有 arousal 衰减;新会话重置 patience;loop 跨会话计龄)
  apply_events()  事件 → 标量更新 + loop 管理(规则表)
  transition()    模式状态机(带滞回)
"""
from __future__ import annotations
import time
from .state import AffectState, OpenLoop, Grievance
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
WITHDRAW_PRESSURE = 5             # loop 总压力超过此值 → withdrawn(÷avoidance 调低)
PROBING_SECURITY = 0.40           # security 低于此值且出现模糊信号 → probing
LOOP_ESCALATE_SESSIONS = 1        # 挂起回路熬过几个会话后沉淀为旧账
MIN_TURNS_IN_NEG_MODE = 2         # 滞回:负面模式最短停留轮数


def apply_time(state: AffectState, persona: Persona, now: float | None = None) -> None:
    now = now or time.time()
    gap = max(0.0, now - state.last_ts)

    # 只有 arousal 自然冷却。security 不自发回升,open_loops 不衰减。
    state.arousal *= 0.5 ** (gap / AROUSAL_HALFLIFE_S)

    if gap > SESSION_GAP_S:
        state.patience = persona.base_patience       # 新会话,耐心重置
        state.warm_streak = 0
        for loop in state.open_loops:
            loop.sessions_old += 1                   # 挂起的事熬过了一晚,更重了
        _escalate_old_loops(state)

    state.last_ts = now


def _escalate_old_loops(state: AffectState) -> None:
    """挂太久的重要回路 → 沉淀为旧账(grievance)。"""
    remain = []
    for loop in state.open_loops:
        if loop.sessions_old >= LOOP_ESCALATE_SESSIONS and loop.weight >= 3:
            state.grievances.append(Grievance(
                id=loop.id, weight=loop.weight,
                content=f"{loop.content}(一直没被回应/兑现)",
            ))
        else:
            remain.append(loop)
    state.open_loops = remain


def apply_events(state: AffectState, ev: dict, persona: Persona,
                 her_last_msg: str | None, his_msg: str) -> list[str]:
    """事件 → 状态。返回 trace(人话日志,调试用)。"""
    trace = []
    state.turn += 1

    # ── 1. 他回应了挂起回路 → 关闭 + 安全感小幅修复 ────────────
    if ev["addresses_loop_id"]:
        loop = state.find_loop(ev["addresses_loop_id"])
        if loop:
            state.open_loops.remove(loop)
            state.security = min(1.0, state.security + SEC_GAIN_LOOP_CLOSED)
            trace.append(f"回路关闭: {loop.content}")

    # ── 2. 投标被如何对待(杠杆最大的一条规则)─────────────────
    bid = ev["bid_in_her_last_msg"]
    resp = ev["his_response_type"]

    if bid != "none" and resp == "turn_away":
        state.patience -= 2
        state.security = max(0.0, state.security - SEC_HIT_TURN_AWAY * persona.anxiety)
        state.warm_streak = 0
        weight = 3 if bid in ("seeking_comfort", "venting") else 2
        snippet = (her_last_msg or "")[:40]
        state.open_loops.append(OpenLoop.new(
            "unanswered_bid",
            f"她说'{snippet}…'({bid}),他敷衍带过/转移了话题",
            state.turn, weight,
        ))
        trace.append(f"投标被忽略({bid}): patience-2, security受损, 新增挂起回路")

    elif bid != "none" and resp == "turn_toward":
        state.security = min(1.0, state.security + SEC_GAIN_TURN_TOWARD)
        state.warm_streak += 1
        trace.append("投标被接住: security+, warm_streak+")

    if resp == "turn_against":
        state.arousal = min(1.0, state.arousal + 0.35)
        state.security = max(0.0, state.security - SEC_HIT_TURN_AGAINST * persona.anxiety)
        state.patience -= 2
        state.warm_streak = 0
        trace.append("攻击性回应: arousal飙升, security重损")

    # ── 3. 敷衍语气即使不构成 turn_away 也磨损耐心 ─────────────
    if "perfunctory" in ev["tone_flags"] or "dismissive" in ev["tone_flags"]:
        state.patience -= 1
        state.warm_streak = 0
        trace.append("敷衍语气: patience-1")

    if "affectionate" in ev["tone_flags"] or "warm" in ev["tone_flags"]:
        state.warm_streak += 1

    # ── 4. 修复尝试:接受与否由 security 门控(耦合点)──────────
    repair_accepted = False
    if ev["is_repair_attempt"] and state.mode in ("conflict", "withdrawn"):
        threshold = REPAIR_ACCEPT_BASE * persona.avoidance   # 回避型更难被哄好
        if state.security > threshold:
            repair_accepted = True
            state.security = min(1.0, state.security + SEC_GAIN_REPAIR)
            state.arousal = max(0.0, state.arousal - 0.2)
            trace.append("修复尝试被接受")
        else:
            trace.append("修复尝试被拒绝(security太低,'道歉有什么用')")

    # ── 5. 他做出新承诺 → 记入 open_loops,将来检查兑现 ─────────
    if ev["new_commitment"]:
        state.open_loops.append(OpenLoop.new(
            "commitment", f"他承诺:{ev['new_commitment']}", state.turn, weight=3))
        trace.append(f"记录承诺: {ev['new_commitment']}")

    # ── 6. 旧账被话题触碰 → arousal 上浮 ───────────────────────
    if ev["topic_relates_to_grievance_id"]:
        state.arousal = min(1.0, state.arousal + 0.2)
        trace.append("话题触碰旧账: arousal+")

    ev["_repair_accepted"] = repair_accepted
    return trace


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
    withdraw_threshold = WITHDRAW_PRESSURE / max(persona.avoidance, 0.3)

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
