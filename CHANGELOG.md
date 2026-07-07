# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.3] - 2026-07-07

### Fixed

- **set_timer 合规性回归(实盘 1/3 → 修复)**:0.2.2 把定时器教学降级成了
  【主动回忆】块下的第 3 个子弹点,丢掉了 0.2.0 独立块的因果框架,实测她经常
  嘴上说"我等你哦/到时候提醒你"却不调用工具 → 永远不会主动回来。现在工具开启时
  set_timer 恢复为独立区块(专属标题 +"什么时候必须调用"清单 +"不调用他就永远
  等不到你"因果重锤),与 tag 路径同构;回归测试钉死块结构
  (`tests/test_injector_blocks.py`)。
- **工具循环烧轮次在冗余检索上**:`search_memory` 增加相关性下限
  (`SEARCH_MIN_SCORE=0.40`)——exclude_ids 排掉真命中后,后续搜索返回的低分
  "填充记忆"不再伪装成成果,而是明确回"没有更相关的了,别再翻了";
  【主动回忆】块的引导同步收紧("换个明显不同的说法再试一次,还没有就承认记不清")。
- **未闭合标签泄漏(实盘发现)**:set_timer 可靠触发后工具轮变多,模型在工具轮
  之后的强制作答里常吐半个 `<reply>` 或整段未闭合 `<thinking>`,`_parse_generation`
  只认闭合标签 → 裸标签直接泄给用户(实测约 3/9)。现在解析器容错未闭合/残缺标签,
  并加最后一道 `_STRAY_TAG_RE` 保险:无论如何都不把裸 `<reply>`/`<thinking>` 泄出,
  也绝不失声;回归测试覆盖未闭合/多段未闭合/纯独白等情形(`tests/test_pipeline_parse.py`)。

### Verified

- DeepSeek 实盘端到端(0.2.2):agent loop 一轮内 3 次真实工具调用
  (grep×2 → search),结果回喂、撞轮次上限强制作答、答案正确;exclude_ids
  去重生效;失败降级路径成立。多消息连发、好感度分档、bge-m3 写入/检索同步验证通过。
- DeepSeek 实盘复验(0.2.3):三个定时器场景(洗澡/提醒吃药/视频约定)各跑 3 轮,
  set_timer 合规率 8/9(修复前工具路径 1/3),恢复到 0.2.0 的 `<timer>` 标签水平;
  且修复后回复不再泄漏任何裸标签。88 个离线单测通过(83 + 新增 5 个解析回归)。

## [0.2.2] - 2026-07-07

### Added

- **工具协议 / 迭代记忆搜索(agent loop)**:生成端(LLM②)升级为有界工具循环
  (`TOOL_MAX_ROUNDS`,默认 3 轮,超限 `tool_choice=none` 强制作答;任何一环失败
  整体降级为无工具单次生成)。工具集:
  - `search_memory(query, axis, newer/older_than_days)`——双轴向量检索,可换措辞
    迭代再搜;时间过滤走旁挂 `ts_ms` 列(时间永不进向量,铁律 2)。
  - `grep_memory(keyword, speaker, …)`——L3 原文 ILIKE 精确检索(名字/数字/原话
    这类 embedding 会糊掉的东西),同时是"不依赖向量库的备用搜索链路"。
  - `set_timer(minutes, memo)`——取代 `<timer>` 标签(标签解析保留兜底)。
  - **检索置信度提示**:自动检索 top1 低于 `RETRIEVAL_CONFIDENCE` 时注入
    "记忆很模糊,先搜再答"的提示——工具调用有据可依,多数轮次零工具零延迟。
  - 工具翻到的记忆同样记热度(L2);已注入 L1 的 id 从工具结果中排除,不重复喂。
- **生活模拟器 + 话题种子(注意力转移)**:话题转移显得假的根源是"事件在转移
  那一刻现编"。现在事件由 `life_sim` 离线预生成(与 auto-dream 同款后台触发:
  新鲜种子 < `LIFE_SIM_FRESH_TARGET` 且距上次 ≥ `LIFE_SIM_MIN_INTERVAL_HOURS`),
  **生成即正史**:内容唯一一份写入 L3(新 `MemoryKind.life_event`,可向量/grep
  检索),`life_events` 表只持 id + 调度元数据。三权分立:**代码**决定何时转
  (`dull_streak≥2` 或 warm 低概率,`SEED_COOLDOWN_TURNS` 冷却)、**事件池**提供
  素材(|valence| 大且新鲜优先,注入即 mentioned,绝不二次当新鲜事)、**LLM** 只
  决定怎么说。定时器主动消息没有备忘时也拿种子当素材。
- **日程表**:`schedule_items` 表,routine(长期作息,按星期+时段,可跨午夜)+
  oneoff(一次性事项,生活模拟器的 plans 会写进来)。编译成 L1 独立块【你的生活】
  ("你这会儿本来在睡觉,是被消息吵醒的"),不占记忆三槽预算;撞进睡眠时段的
  闹钟自动顺延到醒来后 0~15 分钟(她不会凌晨三点蹦出来发消息)。新对话自动种
  默认作息,旧对话首条消息懒种。
- **激素模拟(affect 引擎扩展)**:三个不同半衰期的慢变量,全部由 dynamics 规则
  触发(LLM 永远不碰数字,铁律同款):`adrenaline`(~20min,被攻击/狂喜/跨里程碑
  的"上头")、`oxytocin`(~3h,和好/被接住/升档的"亲密余温")、`cortisol`(~20h,
  被攻击/求安慰被敷衍/旧账沉淀的"压力残渣"——吵完架第二天早上还是不得劲,这是
  45 分钟半衰期的 arousal 表达不了的)。耦合:oxytocin 放松修复门槛并放大好感
  增益,cortisol 抬高修复门槛并磨掉新会话耐心。注入端只翻译成体感,绝不报数值。
- **自我迭代地基(core identity 数据化 + 版本快照)**:`chats.core_identity` 列
  非空时覆盖 `identity.py` 的出厂编译;`chat_revisions` 表 append-only 快照
  (建 chat 落 rev1,PATCH 配置前自动快照,回退本身也留快照可再回退)。新 API:
  `GET /chats/{id}/revisions`、`POST /chats/{id}/revisions/{rev}/rollback`、
  `GET /chats/{id}/schedule`、`GET /chats/{id}/life-events`。
- **L1 弹性预算**:刻骨铭心没用满的份额溢给"相关回忆"槽(早期对话刻骨铭心少,
  僵化 20% 不再闲置);比例仍是软上限,优先级次序不变。
- 前端 debug 面板:激素三条 gauge、日程/话题种子/工具调用 trace。

### Changed

- `dynamics.apply_events` 新增第 8 节:话题热度计数(`dull_streak`,双方都无投标
  无温度才累计;吵架不算"淡")。新会话重置。
- 抽取器 `excited` 语气现在有动力学效应(adrenaline+)。
- 配置新增:`TOOLS_ENABLED` / `TOOL_MAX_ROUNDS` / `RETRIEVAL_CONFIDENCE` /
  `LIFE_SIM_*` / `SEED_COOLDOWN_TURNS` / `SCHEDULE_ENABLED`。

### Migration

- 旧库升级:`init_db()` 幂等补 `ALTER TYPE memory_kind ADD VALUE 'life_event'`
  与 `chats.core_identity` 列;新表(`chat_revisions`/`schedule_items`/
  `life_events`)由 `create_all` 自动建。0.2.0 的 affect JSON 缺激素字段 →
  加载时默认 0,无需迁移。测试:78 passing(45 + 33 new)。

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
