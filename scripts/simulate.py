"""
加速老化测试脚手架 — 几分钟跑完一周的相处,曲线代替肉眼。

原理:全项目时间已收口到 app/clock(可注入偏移),本脚本拨快"现在",
让时间效应(激素衰减/好感缓降/会话边界/作息/夜间代理)在真实管线上真实发生。
对话经 pipeline.handle_message 全链路执行(抽取/动力学/工具/守护层/落库),
不是 mock —— 跑出来的就是产品行为。

用法:
    python -m scripts.simulate --scenario scripts/scenarios/neglect_week.json --preset anxious
    python -m scripts.simulate --scenario scripts/scenarios/warm_week.json --preset tsundere --no-bg

场景文件是步骤 DSL(JSON):
    {"say": "早安"}                     他发一条消息(走完整管线)
    {"advance_hours": 4}                时间快进
    {"night": true}                     快进到凌晨3点跑夜间代理,再快进到早9点
    {"auto": 2, "style": "温柔追问"}    LLM 扮演他,按风格自动聊 N 轮
    {"repeat": 7, "steps": [...]}       重复一段(嵌套展开)

产出:
    - 终端逐轮日志(mode/好感度) + 汇总报告(好感轨迹/mode分布/健康体检)
    - {out}/{场景}_{preset}.csv         全量快照时间序列(可拖进任何绘图工具)
    - {out}/{场景}_{preset}_health.json 健康报告
    - 对话真实存在于 DB:打开前端选中 [sim] 对话,直接看情绪曲线卡片

需要:Postgres(docker compose up -d)+ 真实 DEEPSEEK_API_KEY(生成是真的)。
成本控制:--no-bg 关掉生活模拟器/Dream 的后台 LLM 调用;场景步数自己定。
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from datetime import timedelta
from pathlib import Path

from app import clock
from app.config import settings


def _flatten(steps: list[dict]):
    for st in steps:
        if "repeat" in st:
            for _ in range(int(st["repeat"])):
                yield from _flatten(st.get("steps", []))
        else:
            yield st


def advance_to_hour(hour: int, minute: int = 0) -> None:
    """快进到下一个 HH:MM(必然向前,绝不回拨)。"""
    now = clock.now_dt()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    clock.advance((target - now).total_seconds())


def _clean_user_msg(raw: str, cap: int = 72) -> str:
    """auto 用户消息清洗:压平换行、剥引号;超长时在句读处截断——
    绝不产出半截话(实测教训:'晚上请你吃…'式的吊句会被她读成敷衍,污染曲线)。"""
    text = " ".join((raw or "").split()).strip("\"'「」『』“”‘’")
    if not text:
        return "在吗"
    if len(text) <= cap:
        return text
    cut = text[:cap]
    # 取上限内最靠后的句读;宁可短,不可半截(半截话会被她读成吊人/敷衍)
    for i in range(len(cut) - 1, 3, -1):
        if cut[i] in "。！？!?~；;":
            return cut[: i + 1]
    return cut


async def _llm_user_message(transcript: list[str], style: str) -> str:
    """LLM 扮演男方用户:按风格指令接着聊(flash,便宜)。"""
    from app.llm import client
    from app.llm.client import MODEL_FLASH

    tail = "\n".join(transcript[-8:]) or "(对话刚开始)"
    msg = await client.chat(
        [
            {"role": "system", "content": (
                "你在扮演一段情侣线上聊天里的男方。按照风格指令生成他的下一条微信消息。"
                "只输出消息本体:一句完整的话(最多两短句),不要引号、不要旁白、不要解释;"
                "把话一次说完——不要用省略号吊半截话,不要换行,不要留'待会儿再说'式的钩子。"
            )},
            {"role": "user", "content": f"风格指令:{style}\n\n最近对话:\n{tail}\n\n他的下一条消息:"},
        ],
        model=MODEL_FLASH, temperature=0.9, max_tokens=80, thinking=False,
    )
    return _clean_user_msg(msg)


async def run(args: argparse.Namespace) -> None:
    if args.no_bg:
        # 省钱模式:关掉对话后自动触发的后台 LLM(夜跑仍由 night 步骤显式执行)
        settings.life_sim_enabled = False
        settings.dream_enabled = False

    from app.affect.persona import PRESETS
    from app.affect.state import AffectState
    from app.conversation import night_agent, pipeline, timeline
    from app.conversation import schedule as sched
    from app.db import SessionLocal, init_db
    from app.memory import health as health_mod
    from app.models import Chat

    scenario = json.loads(Path(args.scenario).read_text(encoding="utf-8"))
    await init_db()

    persona = PRESETS[args.preset]
    async with SessionLocal() as s:
        chat = Chat(
            title=f"[sim] {scenario.get('name', Path(args.scenario).stem)} · {args.preset}",
            persona=persona.to_dict(),
            affect=AffectState.fresh(persona).to_dict(),
        )
        s.add(chat)
        await s.flush()
        await sched.seed_defaults(s, chat.id)
        await s.commit()
        chat_id = chat.id
    print(f"▶ 模拟开始 preset={args.preset} scenario={scenario.get('name')} chat={chat_id}")

    turns = 0
    transcript: list[str] = []

    async def say(text: str) -> None:
        nonlocal turns
        async with SessionLocal() as s:
            c = await s.get(Chat, chat_id)
            res = await pipeline.handle_message(s, c, text)
        turns += 1
        dbg = res.get("debug") or {}
        scal = dbg.get("scalars") or {}
        transcript.append(f"他: {text}")
        transcript.append(f"她: {res['content']}")
        del transcript[:-12]
        print(f"  [{turns:>3}] {clock.now_dt():%m-%d %H:%M} 他: {text[:26]}")
        print(f"        她({dbg.get('mode', '?')} 好感{scal.get('affection', '?')}): "
              f"{' / '.join(res['messages'])[:64]}")

    for st in _flatten(scenario["steps"]):
        if "advance_hours" in st:
            clock.advance(float(st["advance_hours"]) * 3600)
        elif st.get("night"):
            advance_to_hour(3)
            async with SessionLocal() as s:
                c = await s.get(Chat, chat_id)
                report = await night_agent.run_night(s, c, force=bool(st.get("force")))
            brief = {k: report[k] for k in ("facts", "diary", "plans", "health") if k in report}
            print(f"  🌙 {clock.now_dt():%m-%d} 夜跑: {brief}")
            advance_to_hour(9)
        elif "auto" in st:
            for _ in range(int(st["auto"])):
                msg = await _llm_user_message(transcript, st.get("style", "自然日常"))
                await say(msg)
                clock.advance(float(st.get("gap_minutes", 20)) * 60)
        elif "say" in st:
            await say(st["say"])
        else:
            print(f"  ? 跳过未知步骤: {st}")

    await asyncio.sleep(1.0)   # 给 fire-and-forget 的后台任务一点收尾时间

    # ── 报告 ────────────────────────────────────────────────────────
    async with SessionLocal() as s:
        c = await s.get(Chat, chat_id)
        rows = await timeline.history(s, chat_id, limit=2000)
        hp = await health_mod.compute_health(s, c)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{Path(args.scenario).stem}_{args.preset}"
    csv_path = out / f"{stem}.csv"
    cols = ["turn", "ts_ms", "source", "mode", "affection", "security", "arousal",
            "adrenaline", "oxytocin", "cortisol", "patience", "warm_streak",
            "dull_streak", "loop_pressure", "grievances", "event", "bid"]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            d = timeline.to_dict(r)
            w.writerow({k: d[k] for k in cols})
    (out / f"{stem}_health.json").write_text(
        json.dumps(hp, ensure_ascii=False, indent=2), encoding="utf-8")

    modes: dict[str, int] = {}
    for r in rows:
        modes[r.mode] = modes.get(r.mode, 0) + 1
    print("\n══ 模拟报告 ══")
    print(f"轮次: {turns} · 快照: {len(rows)} · 虚拟时长: {clock.offset_ms() / 3600_000:.1f} 小时")
    if rows:
        print(f"好感度: {rows[0].affection:.1f} → {rows[-1].affection:.1f} · 终态 mode={rows[-1].mode}")
    print(f"mode 分布: {modes}")
    print(f"健康: score={hp['score']} flags={[fl['key'] for fl in hp['flags']]}")
    print(f"CSV: {csv_path}")
    print(f"前端可视化: 打开 UI 选中该 [sim] 对话看情绪曲线 (chat={chat_id})")


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Dreamory 加速老化测试")
    ap.add_argument("--scenario", required=True, help="场景 JSON(步骤 DSL)")
    ap.add_argument("--preset", default="anxious", help="persona 预设名")
    ap.add_argument("--out", default="sim_out", help="报告输出目录")
    ap.add_argument("--no-bg", action="store_true",
                    help="关掉生活模拟器/Dream 的后台 LLM 调用(省钱;夜跑仍显式执行)")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
