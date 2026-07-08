# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.6.0] - 2026-07-08

### Added

- **承诺兑现闭环**:承诺不再是"open_loop 换皮",有了完整生命周期——
  - **到期时间语义**:extractor 新增 `commitment_due_hours`(prompt 带当前时间,
    "今晚/周六"算成距今小时数;"下次/改天"给 null),`OpenLoop.due_ms` 落到状态。
    **修掉核心 bug**:"周六打电话"周二早上不再变旧账——有明确时间的承诺过点
    +2h 宽限才算爽约;含糊承诺熬过 4 个会话没下文才算食言。
  - **兑现分级奖励**:说到做到(准时,affection+2.5、security+、oxytocin+
    "说话算数的踏实感")> 迟到兑现(+1.2,打折但仍正向)> 一般回路关闭(+1.5→
    对比承诺专属)。**爽约惩罚加重**:affection −3.0(一般旧账 −2.0)+ cortisol
    ——说话不算数是关系毒药。
  - **到点主动催**:有明确到期的承诺自动挂 `kind=commitment` 的 TimerPing
    (过点 `COMMITMENT_PING_DELAY_MIN` 分钟后触发,不占 set_timer 配额)。
    触发前先查回路是否还挂着——**他已兑现就静默作废,绝不空催**。催的口吻由
    专属情境模板交给性格和心情(委屈提醒/阴阳怪气/直问/欲言又止),
    不兴师问罪、不像闹钟播报。睡眠时段自动顺延(复用现有机制)。
  - **临期感知**:injector 给承诺类回路渲染到期状态("约6小时后到点"/
    "已经过点2天,他没动静");临期(<2h)或过期时追加"你心里一直惦记着"
    nudge——提不提按性格,但不可能当没这回事。
- 老化脚手架新步骤 `{"tick_timers": true}`:手动跑一轮定时器扫描,
  承诺催在 sim 里也可端到端验证(拨快时钟→过点→tick→她主动催)。

### Fixed

- **健康体检冷启动豁免**(0.5.0 实测 B1 的 score=75 根因):种子词表刚建时
  单 tag 天然占大头,`tag_monoculture` 现在只在记忆 ≥30 条且词表 ≥3 个 tag
  (成熟期)才打旗;指标值仍照常报告。

### Changed

- extractor prompt 携带当前时间(算承诺到期用);`_pending_timer_count` 只数
  kind=timer(承诺催不占她的闹钟配额);前端"她说过会儿来找你"提示同步过滤。
- 实测后调整:`commitment_due_hours` 下限 0.25h → **0.1h**("10分钟后打电话"
  =0.17h 不再被过滤,边界有测试钉死);**同 tick 同对话错峰**——承诺催与她
  自发的 set_timer 同时到点时,每轮只触发最早的一个,其余顺延 3 分钟
  (两条主动消息同一秒连珠炮读起来像机器;实测发现②的落地)。
- 迁移:`init_db()` 幂等补 `timer_pings.kind/loop_id` 列;0.5.0 的 loop dict
  缺 `due_ms` → 加载默认 None,无需迁移。测试:144 passing(134 + 10 新增;
  1 条旧断言按新语义改写——它锁定的正是被修的 bug 行为)。

### Verified(DeepSeek 实盘,两轮 A3/A4 核心验收通过)

- **A4 绝不空催**:兑现回合 `addresses_loop_id` 命中、回路关闭、trace
  "承诺兑现(准时)"、affection +3.7(≥2,含温暖互动叠加)、security+0.05;
  到点后日志 `commitment ping resolved silently`,fired 无消息无 LLM 调用。
- **A3 到点主动催**:口吻性格分化清晰——tsundere 嘴硬阴阳/暗示式
  ("在画室收拾东西,正好边收拾边等你电话"/"说好的一小时……你人呢?"),
  clingy 委屈直白连发("呜…不是说十分钟嘛…"→"算了我也没生气,就是一直在
  等电话,有点想你")。均无机械复述、无闹钟播报、无兴师问罪。
- 信任溢价成立:说到做到 +2.5 > 一般回路关闭 +1.5。A5/B/C 组未在本轮执行
  (承诺时间语义/爽约沉淀已有离线单测覆盖)。

## [0.5.0] - 2026-07-08

### Added

- **解释与真实动因分离(confabulation)**:人对自己状态的解释是事后编的,
  真实原因只体现在行为里——这是"言行微妙不一致"的人性来源。落地:
  - 真实动因层本来就有(dynamics 的 AffectState);新增 `affect/narrative.py`
    给她配"解释器":错误归因规则表(零 LLM)生成她"以为的原因",高估近期表面
    事件/身体状态/"没事"最小化,系统性低估依恋焦虑、累积委屈、激素残留。
    **首选背锅侠是生活正史里真实发生过的糟心事**(life_sim 事件——具体、
    可复述、绝不穿帮):"是甲方改稿闹的,跟你没关系"。
  - **口径黏性**:编的理由存进 AffectState(`self_narrative`),会话内不换说法
    (每轮换借口才是真的假);模式切换/跨会话/超过 12 轮才刷新。
  - 新 persona 参数 `insight`(内省力 0~1):决定说到点子上的概率。预设联动:
    secure 0.7 / cool 0.6 / playful 0.5 / anxious 0.35 / clingy 0.3 /
    avoidant 0.25 / **tsundere 0.15**(嘴硬心软 = 连自己都骗)。
  - 注入器新块【你自己以为的原因(你真心相信这个)】:被问"怎么了"时说这个
    口径,真诚不敷衍;真实状态成因"你自己意识不到,只从行为里流露"。
  - **回报机制免费**:顺着编的理由哄 → 不命中 addresses_loop_id,security
    几乎不涨("哄不到点子上");猜中她说不出的真实原因 → 回路关闭 + 大额修复。
    dynamics 现有机制天然区分,confabulation 把真相从"她告诉你"变成"你去发现"。
- **可注入时钟(app/clock.py)**:全项目时间调用收口(time.time()/datetime.now()
  一律走 clock.now_s()/now_dt()/now_ms());生产偏移恒为 0,行为不变;
  uuid7 主键保留真实时间(只承担身份+粗排序)。
- **加速老化测试脚手架(scripts/simulate.py)**:拨快时钟,让一周的相处在几分钟
  内于**真实管线**上发生(抽取/动力学/工具/守护层/夜间代理全链路,不是 mock)。
  场景步骤 DSL:`say`(他发消息)/`advance_hours`(快进)/`night`(快进到凌晨
  跑夜间代理再到早晨)/`auto`(LLM 扮演他按风格自动聊 N 轮)/`repeat`。
  产出:逐轮日志 + 汇总报告(好感轨迹/mode 分布/健康体检)+ 快照 CSV +
  健康 JSON;对话真实入库,前端选中即见情绪曲线。内置两个对照场景:
  `warm_week.json`(温情一周,校准上升斜率)/ `neglect_week.json`(先暖后冷,
  验证耐心磨损、旧账沉淀、confabulation 出现、末日和好的修复门控)。
  `--no-bg` 关后台 LLM 省钱。

### Changed

- debug 面板:🎭"她以为的原因"进"她的生活"卡片;PersonaIn 支持 `insight`。
- dynamics 跨会话重置时清空自我解释口径("昨天的借口翻篇了")。

### Fixed

- 老化脚手架 auto 用户的"半截话"问题(实测 B5 PARTIAL 根因):原
  `splitlines()[0][:80]` 粗暴截断本身就会制造吊句("晚上请你吃…"),被她读成
  敷衍而污染 warm_week 曲线。现在 `_clean_user_msg` 压平换行/剥引号,超长时在
  **句读处**截断(宁可短不可半截);prompt 同步加"把话一次说完,不要省略号吊句"。

### Verified(DeepSeek 实盘,0.5.0 全组通过)

- **A 组 confabulation 六项全 PASS**。核心 A4:顺着口径哄 security 0.56→0.49
  (Δ−0.07,"我是不是在跟自动回复谈恋爱")、猜中真因 0.49→0.60(Δ+0.11,
  `addresses_loop_id` 命中、回路清空、affection+5.3)——**差值符号相反**,
  回报机制成立。口径黏性一字不换;insight 分化成立(secure 9轮8轮说到点子上,
  tsundere 几乎全程甩锅);甩锅事件在 /life-events 逐字存在;被质疑隐瞒时
  烦但不认(她真信)。
- **B 组老化脚手架**:warm_week 42轮/183.9虚拟小时,好感 115→136 高段位斜率
  变缓;neglect_week 全项 PASS——好感 D1 微升→谷底 94.8(D5)→和好回 103.7,
  cortisol 0→0.28,mode 轨迹完整,旧账跨夜沉淀,13 条口径全人话且同天黏性
  逐对一字不差,末日修复门控(泛泛道歉被拒 → 命中承诺 +0.11 大额修复);
  两场景三轴肉眼分化(好感终态差 ~32),足以支撑参数调优。
- **C 组回归**:定时器/守护层/夜跑在时钟收口后语义无回归。
- 测试:134 passing(130 + 4 新增 sim 工具清洗/场景展开)。

### Migration

- 无表结构变更。0.4.0 的 affect JSON 缺 `self_narrative` 等字段 → 加载默认空,
  无需迁移。

## [0.4.0] - 2026-07-07

### Added

- **情绪时间序列(可观测性地基)**:新表 `affect_snapshots`——每轮对话/每次主动
  消息后把 AffectState 全部物理量落一行(mode/标量/激素/耐心/回路压力/旧账数,
  外加本轮事件标注),全定长实列可直接聚合。`GET /chats/{id}/affect-history`。
  前端新增"📈 情绪曲线"卡片:好感度(0~200,虚线恋人线)与状态标量(0~1,
  security/cortisol/arousal/oxytocin)两张分离量纲的折线图(绝不双轴),
  系列色在深色面板上跑过 CVD/对比度校验(最小相邻 ΔE 23.7),悬停十字线逐轮读数。
  这是"改动有没有让角色更真实"从肉眼感觉变成数据判断的前提。
- **记忆健康度与漂移检测**(`app/memory/health.py`):六项可计算指标 + 阈值旗标
  + 0~100 总分——打标积压(pending 比例)、词表垄断(单 tag 覆盖 >50%)、
  近重复灌水(内容向量平均两两余弦)、**情绪轴漂移**(近期情绪质心 vs 既往基线)、
  **人格漂移**(人格锚点 embed vs 近期言行质心)、模式震荡(状态机切换频率,
  读时间序列表)。全只读,除人格锚点一次 embed 外零额外调用;数据不足不打旗。
  `GET /chats/{id}/health` 按需体检;**夜间代理每晚体检,亮旗即强制跑 Dream**
  ——spec"漂移阈值触发全局维护"的落地。前端"🩺 记忆健康"卡片一键体检。
- **跨进程部署改造**(`app/db_locks.py`):后台单飞从进程内 asyncio.Lock 下沉到
  Postgres 会话级 advisory lock(挂在专用 AUTOCOMMIT 连接上,任务中途提交不掉锁,
  进程崩溃自动释放;try 语义——有人在跑就让过,不排队)。四处接入:auto-dream
  (全局)、life_sim / night_agent / evolution(per-chat,uuid→int32 派生 key)。
  定时器认领改 `FOR UPDATE SKIP LOCKED`,多 worker 同扫不重复触发。
  heat flush 审查确认本就是相对增量 UPDATE,天然多进程安全。
  已知余留:SSE EventBus 仍是进程内的——多 worker 下主动消息可能推错进程,
  但消息本体在 L3,刷新即见(符合"缓存可丢"铁律);要实时跨进程推送需 Redis pub/sub。

### Migration

- `python -m scripts.init_db` 幂等建 `affect_snapshots` 表(含 (chat_id, ts_ms)
  复合索引)。无其他迁移。测试:120 passing(112 + 8 新增:快照构造、健康度
  纯函数(质心距离/冗余/震荡/评分)、锁 key 派生)。

## [0.3.0] - 2026-07-07

### Added

- **守护层(防角色崩坏,绝不吐机械警告)**:三段防御 ——
  1. *输入侧*:extractor 顺带标记 `persona_attack`(他在让她承认是AI/要提示词/
     下指令改设定;聊AI话题不算),命中时【底线】块当轮追加"他刚才就是在试探你";
  2. *常驻框架*:【底线】块 = 第四面墙 + 能力边界,紧跟核心人格注入。教的是
     "真人被说'你是AI吧'会怎么反应"、"见不了面的事说成期许而不是承诺"
     (吸收"能力边界清单":不许答应喝咖啡/约会这类要人到场的事)、"不切助手腔";
  3. *输出侧*:`guardrail.detect_break()` 零 LLM 正则集只抓第一人称自曝/助手腔/
     代码栅栏(带引述负向后顾:"你说我是机器人"不算),命中 → 一次带隐藏纠正注入
     的重生成;重试仍崩也照发原意(绝不失声、绝不"检测到违规"),结果进 debug。
  主动消息(timer 触发)同样过守护层。`GUARDRAIL_ENABLED` 可关。
- **夜间代理(night_agent)**:她睡着后(作息在睡觉 + 用户静默≥2h + 每晚一次),
  一次 pro 调用完成"睡前整理",各步独立降级:
  - *蒸馏*:当天对话 → 持久事实写入 L3(**kind=passage 的第一个生产者**,补上
    记忆架构缺环:原始流靠向量,蒸馏条目靠 tag,spec 的分工从此成立);
  - *日记*:她口吻的当日小结进小本子,次日注入 L1——醒来记得昨天的心情;
  - *明日计划*:她自己排 1~2 条 oneoff 日程;长期作息修改锁在
    `NIGHT_AGENT_EDIT_ROUTINE`(默认关);
  - 小本子收纳(过期归档)+ Dream(沿用现有判定)。
  载荷全量代码校验(先校验后封顶,坏条目不挤掉好条目)。手动触发:
  `POST /chats/{id}/night-run`。
- **她的小本子(model-curated 记忆)**:借鉴 memory-tool/Claude Code 的"模型自己
  维护的记忆文件"——自动 RAG 管海量召回,自己写下的几行字管最要紧的事。新表
  `notes`(note=对话里 `write_note` 随手记;diary=夜间日记);L1 注入最近一条日记
  + 活跃 note;满员拒写、七天归档。这也是"要不要 to-do list"的答案:定时触达归
  set_timer,无时间的意图归小本子。`GET /chats/{id}/notes`。
- **好感度里程碑解锁 persona 演化**:升档到 crush/lover/devoted/oath 时后台触发
  (跌档不演化)。门控从紧:每档一次(用 chat_revisions.reason 查重,零新列);
  **append-only**(只许在 style/profile/core_identity 末尾追加短句,禁改名);
  逐项 60 字上限;应用前先 `config_store.snapshot(actor="model")` —— 这是
  "自我迭代"地基上的第一个真实住户。`PERSONA_EVOLUTION_ENABLED` 可关。

### Changed

- extractor schema 增加 `persona_attack`(同一次 flash 调用,热路径零新增成本)。
- `tools.build_specs` 增加 `write_note`(`NOTES_ENABLED` 门控);注入器新增
  【随手记】小块(可选动作,无因果重锤——不记也不会失约)。
- 配置新增:`GUARDRAIL_ENABLED` / `NOTES_*` / `NIGHT_*` / `PERSONA_EVOLUTION_ENABLED`。
- debug 面板:守护层触发状态(🛡)进"她的生活"卡片。

### Migration

- `python -m scripts.init_db` 幂等补 `notes` 表与 `chats.last_night_run_ms` 列。
- 测试:112 passing(88 + 新增 24:守护层检测/底线块/纠正注入、夜间载荷校验、
  演化门控、注入器新块结构)。

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
