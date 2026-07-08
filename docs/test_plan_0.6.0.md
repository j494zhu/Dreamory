# 0.6.0 端到端测试计划(承诺兑现闭环)

本轮验收一条完整生命周期:**承诺产生(带到期)→ 临期惦记 → 到点没兑现主动催 /
兑现分级奖励 → 爽约沉旧账**。外加健康体检冷启动豁免的复验。

## 0. 前置

```bash
python -m scripts.init_db     # 幂等补 timer_pings.kind/loop_id 列
python -m pytest -q           # 必须 144 passed
python -c "from app import __version__; print(__version__)"   # 0.6.0
```

## A 组:交互式(uvicorn,真实时间)

### A1. 承诺捕获与到期估算
1. 对她说:"**我 10 分钟后打电话给你**"(用短时限,真实时间可等)。
   (注:due_hours 下限为 0.1h——0.6.0 实测后从 0.25 调低,10 分钟承诺可过。)
2. **预期**:debug `events.new_commitment` 非空、`commitment_due_hours ≈ 0.17`;
   debug 新字段 `commitment_ping` 非空(loop_id + due_ms);
   `GET /timers` 出现 `kind=commitment` 的 ping;
   **前端"⏰ 她说过会儿来找你"提示不出现**(承诺催不占她的闹钟语义)。
3. 对照:说"**下次带你去吃那家**" → `commitment_due_hours=null`,不挂 ping,
   回路照记(含糊承诺)。

### A2. 临期感知(注入渲染)
1. A1 的承诺挂上后立刻再聊一轮闲话。
2. **预期**:她的【关系记忆】里该承诺带"(马上就到他说的时间了)"——
   可从回复里她是否隐约提起/惦记观察;不强制她开口(nudge 交给性格)。

### A3. 到点主动催(核心)⭐
1. A1 之后**不兑现**,等 10 分钟 + `COMMITMENT_PING_DELAY_MIN`(测试时可先在
   .env 把它调成 1)。
2. **预期**:SSE 到达她的主动消息;**口吻验收**:不机械复述承诺原文、不像闹钟
   播报、不兴师问罪——委屈提醒/装作随口一问/直问/欲言又止皆可(按 preset:
   tsundere 应嘴硬阴阳,clingy 应委屈直白;建议两个 preset 各跑一次对比)。
3. ping 转 fired;debug/timers 清空。

### A4. 兑现路径:绝不空催 ⭐
1. 重复 A1,但在 10 分钟内**兑现**:"电话来啦!(假装在打)"或明确回应承诺。
2. **预期**:debug `addresses_loop_id` 命中、回路关闭、trace 出现
   "承诺兑现(准时)";affection 涨幅 ≥2(说到做到 > 一般回应);
3. 到点后:**她不来催**——后台日志出现 `commitment ping resolved silently`,
   ping 转 fired 但无消息、无 LLM 调用。这是"绝不空催"的验收。

### A5. 迟到兑现打折
过点 >2h 后才兑现(可配合 B 组时钟做,或口头验证 trace):trace 应为
"承诺兑现(迟到但兑现了)",affection 涨幅明显小于 A4。

## B 组:老化脚手架(拨快时钟,全链路)

写一个临时场景(或手改 neglect_week)验证完整周期,关键步骤:

```json
{"steps": [
  {"say": "宝,今晚8点我打电话给你,说好了"},
  {"advance_hours": 9},
  {"tick_timers": true},
  {"advance_hours": 1},
  {"say": "在吗"},
  {"night": true},
  {"say": "早"}
]}
```

- **tick_timers 后**:终端应出现她的催促消息生成(timer fired 日志);
  若想测静默作废,在 tick 前加一步兑现的 say。
- **跨夜后**(承诺已过点+宽限):affect CSV 中 grievances +1、affection 掉
  ~3(爽约比一般旧账 2.0 更伤)、cortisol 抬升;她的【关系记忆】里旧账文案
  含"说好的没做到"。
- **未到期承诺越冬**:另跑一段"说好周六(72h后)"+ 隔夜 → 回路保留、
  grievances 不变(这是 0.6.0 修的核心 bug,必须验)。

## C 组:回归 + 冷启动豁免

1. `pytest -q` 144 passed;
2. 她自己的 set_timer("洗澡5分钟")与承诺催并存时:两种 ping 互不挤占配额
   (set_timer 仍可挂满 3 个);
3. **健康冷启动豁免复验**:新建对话聊 5 轮 → `GET /health` 的
   `tag_monoculture` 不再打旗(0.5.0 实测 score=75 的根因),score 应 ≥90;
   指标值仍在 metrics 里正常报告。

## 判定与已知限界

- 必附数据:A4 与一般回路关闭的 affection 涨幅对比;B 组爽约前后的
  affection/cortisol/grievances 变化;A3 催促消息原文(评口吻自然度)。
- 已知限界:`commitment_due_hours` 是 flash 的估算,"今晚"给 3~6h 都算对
  (宽限 2h 吸收误差);催只发一次(不轰炸);uvicorn 与 sim 进程时钟互不影响。
