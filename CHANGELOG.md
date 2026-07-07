# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-07-07

### Added

- **多消息连发 (Tier 0)**:生成端允许 1~4 个 `<reply>` 标签;每条独立写入 L3
  (独立内容向量,检索粒度不受连发影响;脑内剧场/情绪/刻骨铭心只挂第一条,避免
  情绪轴重复嵌入)。API 返回 `messages: list[str]`(`content` 拼接保持向后兼容),
  前端逐条播放并带"正在输入…"节奏。抽取器的"她的上一条消息"升级为完整连发段
  (`_her_last_burst`)。`working_memory`/`get_messages` 排序加 uuid7 tie-break,
  同毫秒连发不乱序。
- **定时器 / 时间感知 (Tier 0)**:
  - 每轮注入【时间感知】块(当前日期时间 + 距上次说话多久),她会自己察觉
    "说好2分钟结果半小时"。
  - 她可在回复末尾挂 `<timer minutes="X">备忘</timer>`(注入器教学 + 管线解析,
    每 chat 最多 `TIMER_MAX_PENDING` 个);落库到新表 `timer_pings`,重启不丢。
  - 后台 `TimerService`(lifespan 托管,5s 轮询)到点后发起一次对用户隐藏的
    LLM 调用(`pipeline.handle_timer_fire`:时间效应 → L1 组装 → 主动情境生成
    → 落 L3),不跑抽取/动力学;主动消息里禁止再挂 timer,防连环自触发。
  - 推送通道选 **SSE**(`GET /api/chats/{id}/events`,心跳 15s)而非
    long-polling/WebSocket:单向下行 + 原生重连 + 前端一行 `EventSource`。
    掉线丢推送无所谓——消息本体在 L3,刷新即补齐(符合"缓存可丢"铁律)。
  - 新端点 `GET /api/chats/{id}/timers`;前端头部显示"⏰ 她说过会儿来找你"。
- **好感度系统 (Tier 1)**(碧蓝航线式,0~200;50=陌生,100=恋人,200=封顶):
  - `AffectState.affection`:最慢变量,跨会话积累。八档分层
    (失望/冷淡/陌生/友好/心动/恋人/挚爱/誓约),注入器按档渲染
    【你们现在的关系】框架(只注入文字层级,不注入数字)。
  - 事件增减:接投标/暖语气小步涨,修复被接受/回应挂起回路大步涨;
    turn_away/turn_against ×anxiety 下跌(跌快涨慢);旧账沉淀额外掉分;
    恋人档以上正向增益递减(越熟越难刷)。
  - 门控耦合:修复接受阈值随好感降低(深爱易心软)、withdrawn 压力阈值随
    好感升高(能扛)、新会话 warm_streak 落到好感底座(不再一律清零,
    落实 dynamics.py 的旧 TODO)、耐心预算好感加成、离线 >3 天好感缓降
    (下限 60,"感情会淡但共同经历不清零")。
  - 跨档是里程碑事件:进 trace、进 debug,跨"恋人"线的那一轮自动刻骨铭心。
  - `Persona.affection_start`:预设起点与 profile 描述的关系阶段对齐
    (如"在一起两年"的三个预设起点在恋人档)。
  - 前端:好感度仪表(0~200)+ 关系阶段;新建对话模态补全 7 个预设。
- 31 个新单测(好感度动力学 + 多消息/timer/时间感知纯函数),共 45 个,离线 <1s。

### Changed

- `injector.render` 签名扩展:`time_context` / `proactive` / `allow_timer`。
- debug 面板载荷:`scalars.affection`、`affection_tier`、`tier_shift`、
  `timer_scheduled`;`events` 中的内部 `_` 前缀键不再外泄。
- 已有对话(无 affection 字段)反序列化时默认 50(陌生档);老对话的关系
  框架会从陌生人重新开始,属预期迁移行为。

## [0.1.1] - 2026-07-03

### Fixed

- Injector rendered `open_loop.sessions_old` (a session count) as a number of
  days — now labelled as sessions (`隔了 N 次聊天还没下文`).
- Same-message double patience penalty: a `turn_away`/`turn_against` turn that
  also carried a `perfunctory`/`dismissive` tone was charged twice. The tone
  penalty is now deduplicated against the heavier event.
- Conflict "哄不动" deadlock: with low `security`, repair attempts were gated out
  forever. Added a pity floor — after `REPAIR_PITY_ATTEMPTS` sincere attempts the
  repair is accepted (with a reduced security gain); counter resets on acceptance
  or a fresh attack.
- Syntax error in `pipeline._salience_from_events` (stray char after a line
  continuation) that prevented the conversation pipeline from importing.

### Changed

- `_salience_from_events` now takes the strongest single emotional event
  (`max`) instead of summing them, removing the `turn_against` + `repair`
  stacking, and compares arousal against a named `DAILY_AROUSAL_BASELINE`.
- Persona 口癖 (`style`) is no longer pinned into the frozen core identity every
  turn; it is injected at low frequency (`STYLE_INJECT_PROB`, warm/neutral only).

### Added

- New personas: `tsundere`, `cool`, `playful`, `clingy`, each with its own style.
- `AffectState.repair_attempts` field (tracks consecutive repair attempts).
- Auto-dream: the pipeline fires `should_dream()` after each turn and runs a
  Dream cycle in the background (non-blocking, single-flight) when the pending
  backlog is high enough. `DREAM_ENABLED` now defaults to `true`.

## [0.1.0] - 2026-06-24

### Added

- Emotion state machine (affect engine) with attachment theory dynamics
- Three-tier hierarchical memory (L1 / L2 / L3 + orthogonal tag registry)
- Conversation pipeline with time effects, event extraction, and personality injection
- FastAPI backend with native HTML/CSS/JS frontend
- PostgreSQL + pgvector storage with HNSW/GIN indexes
- Dual-axis retrieval (content + emotion vectors)
- Dream offline maintenance phase (clustering + LLM naming)
- DeepSeek v4 pro/flash integration with OpenAI-compatible API
- bge-m3 embedding via SiliconFlow API (with local and fallback backends)
- 14 unit tests covering emotion dynamics + memory pure functions

[Unreleased]: https://github.com/{user}/{repo}/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/{user}/{repo}/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/{user}/{repo}/releases/tag/v0.1.0
