# Dreamory

本项目旨在提供一个伴侣llm的基础设施. 

情绪感知 + 层级记忆的伴侣 App。两大支柱:

1. **情绪状态机** —— 以 *Emotion Bids* + *Attachment Theory* 为核心的隐藏参数集
   ("脑内剧场"),用纯代码维护、确定性地编译进 prompt,让 LLM 不再"永远满状态、
   每条回复一样长"。
2. **三级层级记忆(L1 / L2 / L3 + 正交 Tag 注册表)** —— MemGPT / Letta 风格,在
   有限上下文窗口下做近乎无限、可持续的长期记忆。

在此之上的拟真模块:

- **多消息连发**(v0.2)—— 她像真人发微信一样,可以把一次回复拆成几条短消息连发
  (兴奋时连发,心冷时只回一两个字);
- **定时器 + 时间感知**(v0.2)—— 她知道现在几点、你们多久没说话;说了"等我5分钟"
  就真的会在 5 分钟后主动来找你(后台隐藏 LLM 调用 + SSE 推送);
- **好感度系统**(v0.2)—— 碧蓝航线式 0~200 长程刻度(50=陌生,100=恋人),
  跨会话积累,渗入修复门槛、冷淡阈值、耐心预算等各处动力学;
- **工具协议 / 迭代记忆搜索**(v0.3)—— 生成端是有界 agent loop:她觉得"这事
  好像聊过"就真的去翻记忆(`search_memory` 双轴向量 / `grep_memory` 原文精确),
  一次没中换措辞再搜;`set_timer` 取代标签;检索置信度低时提示她"先搜再答";
- **生活模拟器 + 日程表 + 注意力转移**(v0.3)—— 她的线下生活由后台离线预生成,
  **生成即正史**(写入 L3,细节只生成一次,之后靠检索复述,不会越编越露馅);
  话题变淡时(纯代码信号)递一条新鲜事当种子,像随口想起一样自然转移话题;
  作息表让她"活在自己的生活里"——半夜被消息吵醒会带睡意,闹钟撞上睡眠自动顺延;
- **激素模拟**(v0.3)—— adrenaline(20min)/ oxytocin(3h)/ cortisol(20h)
  三个时间尺度的残留,全由动力学规则触发:吵完架第二天早上"还是不得劲";
- **自我迭代地基**(v0.3)—— 核心人格数据化(`chats.core_identity`)+ 配置
  append-only 版本快照/回滚,为将来开放模型自改基础设施铺路。

> 铁律:**内容只存一份(L3)**;其它层只持 id。**进向量的文本是纯内容**,
> tag / 时间 / 说话人一律作旁挂列,检索时当 `WHERE` 过滤。**热路径零 LLM**;
> LLM 只在离线 Dream 阶段"给已成形的簇命名"。派生缓存(L2、摘要)可丢可重建,
> 绝不反向成为真相。

技术栈:**FastAPI** + 原生 **HTML/CSS/JS**;**DeepSeek v4 pro/flash**;
**bge-m3** embedding;**PostgreSQL + pgvector**。

---

## 架构总览

```
浏览器 (static/)  ──HTTP + SSE──>  FastAPI (app/)
                                │
        ┌───────────────────────┼────────────────────────┐
        ▼                       ▼                        ▼
  affect/ 情绪引擎        conversation/pipeline      memory/ 三级记忆
  state/persona          每轮编排:                   l3_store  (L3 冷存储+grep)
  (好感度八档+激素三轴)   ①时间效应 ②抽取(flash)       tags     (注册表+kNN打标)
  dynamics(纯代码,       ③动力学  ④落库+打标         retrieval(双轴召回+时间过滤)
   含激素/话题热度)       ⑤组装L1(+时间感知+日程       l2_hot   (热度+批量回写)
  extractor(LLM①)          +话题种子+置信度提示)      l1_assembly(去重+弹性预算)
  injector(状态→prompt,   ⑥生成(pro, 有界agent loop:  dream    (离线维护,默认关)
   关系阶段+工具教学)       search/grep/set_timer)
                          ⑦逐条落库+打标 ⑧存状态
                          ⑨后台: auto-dream + life_sim
                                │
  conversation/timer     ← 后台调度器:到点 → handle_timer_fire(隐藏LLM调用,睡眠顺延)
  conversation/bus       ← SSE 事件总线:主动消息推给前端
  conversation/tools     ← 工具协议:search_memory / grep_memory / set_timer
  conversation/schedule  ← 日程表:作息匹配 + L1【你的生活】编译(纯代码)
  conversation/life_sim  ← 生活模拟器:离线预生成她的线下事件(生成即正史)
  conversation/config_store ← 配置版本快照/回滚(自我迭代地基)
                                │
                                ▼
                    PostgreSQL + pgvector
            memories(主表 + content_vec + emotion_vec)
            tags / tag_aliases / chats / timer_pings
            schedule_items / life_events / chat_revisions
```

### 三级记忆与里程碑映射

| 层 | 角色 | 实现 | 里程碑 |
|----|------|------|--------|
| **L1** 上下文窗口 | 快、小、当前在用。槽位:核心人格 / 刻骨铭心 / 工作记忆 / L3检索 / 当前目标 / 当前情绪 / 挂起回路 | [l1_assembly.py](app/memory/l1_assembly.py) + [injector.py](app/affect/injector.py) + [identity.py](app/conversation/identity.py) | M5 |
| **L2** Hot Zone | 中、只读、常用。**只存 id**,按时间衰减热度进出,内存计数 + 后台批量回写 | [l2_hot.py](app/memory/l2_hot.py) | M4 |
| **L3** 冷存储 | 慢、无限、唯一真相源。主表 + VectorDB_1(内容轴)+ VectorDB_2(情绪轴) | [l3_store.py](app/memory/l3_store.py) + [models.py](app/models.py) | M1 / M3 |
| **Tag 注册表** | 横跨 L3 的正交索引:受控词表 + 每个 tag 的 centroid;热路径 kNN 打标(零 LLM) | [tags.py](app/memory/tags.py) | M2 |
| **Dream** | 离线维护:聚类→命名(LLM)→合并/拆分→重映射→刷新 centroid。**默认关闭** | [dream.py](app/memory/dream.py) | M6 |
| 检索/偏置增强 | 双轴检索、多步检索、当前目标条件偏置 | [retrieval.py](app/memory/retrieval.py) | M3 / M7 |

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

> Embedding 有三种后端(`EMBEDDING_BACKEND`):
> - **`api`(推荐,已配好)** —— 真实 bge-m3 走 SiliconFlow 的 OpenAI 兼容 HTTP 接口,
>   **无需 torch/GPU**。需要 `EMBEDDING_API_KEY` + `EMBEDDING_BASE_URL`。
> - `bge_m3` —— 本地 BAAI/bge-m3,需 `pip install FlagEmbedding torch`。
> - `fallback` —— 零依赖确定性哈希向量,供离线开发/CI。

### 2. 起数据库(Postgres + pgvector)

```bash
docker compose up -d          # 拉起带 pgvector 的 Postgres 16
```

### 3. 配置

```bash
cp .env.example .env          # 填入 DEEPSEEK_API_KEY;其它有合理默认值
```

### 4. 建表 + 冷启动种子标签

```bash
python -m scripts.init_db     # 启用 pgvector + 建表 + HNSW/GIN 索引
python -m scripts.seed_tags   # 手动种子词表(按 facet 组织)
```

### 5. 启动

```bash
uvicorn app.main:app --reload --port 8000
```

打开 <http://127.0.0.1:8000> —— 左侧新建对话(可选性格预设/目标),中间聊天,
右侧"脑内剧场"实时显示:内心独白、情绪标量、事件分类、动力学轨迹、挂起回路/旧账、
L1 检索命中、本轮打的 tag。

---

## API 速览

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/chats` | 新建对话(`preset` / `persona` / `goal`) |
| GET  | `/api/chats` | 列出对话 |
| GET/PATCH/DELETE | `/api/chats/{id}` | 取/改/删对话 |
| GET  | `/api/chats/{id}/messages` | 历史消息 |
| POST | `/api/chats/{id}/messages` | 发消息 → 跑完整管线,返回 `messages`(连发列表)+ debug |
| GET  | `/api/chats/{id}/events` | **SSE** 推送流:定时器触发的主动消息从这里到达 |
| GET  | `/api/chats/{id}/timers` | 当前挂着的"过会儿来找他"闹钟 |
| GET  | `/api/chats/{id}/schedule` | 她的日程表(作息 + 一次性安排) |
| GET  | `/api/chats/{id}/life-events` | 生活模拟器生成的线下事件(含种子状态) |
| GET  | `/api/chats/{id}/revisions` | 配置版本历史(persona/core_identity/goal 快照) |
| POST | `/api/chats/{id}/revisions/{rev}/rollback` | 回退到某个历史配置(回退本身也留快照) |
| POST | `/api/chats/{id}/retrieve` | 双轴检索内省(`axis=content\|emotion\|both`) |
| GET  | `/api/tags` | 受控词表 |
| POST | `/api/tags/seed` | 种子/更新一个 tag(含 centroid) |
| POST | `/api/dream/run?force=true` | 手动跑一次 Dream |
| GET  | `/healthz` | 健康检查(后端/embedding/dream 状态) |

---

## 测试

```bash
pytest -q
```

- `tests/test_dynamics.py` —— 情绪动力学是纯函数,逐条耦合规则直接断言(不跑 LLM):
  投标被忽略 → 挂起回路 + 耐心下降;冲突有滞回;道歉被 security 门控;
  焦虑型掉 security 更快;挂起回路跨会话沉淀为旧账;回避型受攻击转冷而非吵架。
- `tests/test_affection.py` —— 好感度动力学:预设起点与关系阶段一致;涨慢跌快
  (×anxiety);上下限钳制;恋人档以上增益递减;深爱易被哄好 / 失望哄不动;
  新会话 warm_streak 好感底座 + 耐心加成;离线 >3 天缓降(下限 60);
  跨档记录 `_tier_shift`,跨"恋人"线是里程碑。
- `tests/test_pipeline_parse.py` —— 多消息解析(多 `<reply>` 保序 / 封顶 / 空条过滤 /
  无标签 fallback);`<timer>` 解析 + 剥除 + 分钟钳制;她的"上一条消息"=完整连发段;
  时间感知渲染(gap 人话化)。
- `tests/test_memory.py` —— fallback embedder 的维度/归一化/语义序;token 估算;
  L1 全局去重(刻骨铭心 > L2热 > 检索)、排除工作记忆窗口、预算溢出丢弃。
- `tests/test_hormones.py` —— 激素三轴半衰期彼此可分(3h 后 adrenaline≈0、
  oxytocin 恰好半衰、cortisol 剩大半);"隔夜 arousal 凉了 cortisol 还在";
  事件触发(被攻击/和好/被接住/跨里程碑);耦合(oxytocin 松修复门槛、cortisol
  紧门槛 + 磨隔夜耐心);dull_streak 计数与重置(吵架不算"淡");0.2.0 旧 affect
  JSON 无激素字段可直接加载。
- `tests/test_schedule_tools.py` —— routine 匹配(普通/跨午夜/按星期归属开始日);
  睡觉优先与 wake_ms;L1 块编译;工具 schema 按 allow_timer 裁剪;时间参数映射;
  dispatch 坏参数/未知工具的降级;set_timer 配额与分钟钳制。
- `tests/test_l1_identity.py` —— L1 弹性预算(刻骨铭心空闲份额溢给相关槽,
  用满时相关槽仍守 30%);core_identity 覆盖(替换出厂编译/保留 tag 词表/
  空覆盖回退)。

### 当前验证状态

- ✅ 78 个单测通过(45 + 0.2.2 新增 33),离线 < 1s
- ✅ **0.2.0 完整端到端已验证**(Postgres via docker + 真实 DeepSeek/bge-m3):
  多消息连发(clingy 报喜连发 3 条);时间感知(她会抱怨"说好2分钟结果半小时");
  定时器自然闭环(她自发挂 `<timer minutes="1">` → 调度器到点 → 隐藏 LLM 调用
  → 主动消息落 L3 + SSE 推送,内容精准衔接她自己写的备忘);
  好感度起点/分档正确注入(恋人档自然喊"老公")。
- ⚠️ 0.2.2 的工具循环 / 生活模拟器已通过离线单测与导入校验,**尚未跑真实
  DeepSeek 端到端**(工具调用格式依赖 DeepSeek 的 OpenAI 兼容 function calling;
  任何一环失败会整体降级为 0.2.0 的单次生成路径,不会失声)。

> 注意:`deepseek-v4-pro` 默认开启原生推理。本项目的"脑内剧场"是 prompt 内的
> 角色扮演 `<thinking>` 块(她的内心独白),与模型原生推理不同,因此生成/抽取/摘要
> 三处都显式 `thinking=False`(`{"thinking":{"type":"disabled"}}`),避免原生推理
> 抢占预算或抑制 in-band 独白。要开启原生推理可传 `thinking=True, reasoning_effort="high"`。

---

## 设计自检清单(每个模块都对一遍)

- 内容只存一份(L3 `memories.content`)?其它结构只持 id?→ ✅
- 进向量的文本是纯内容,没拼 tag/时间/情绪?→ ✅(见 `l3_store.write_memory` 双向量分别取纯内容 / 纯情绪+reasoning)
- 主键 UUIDv7、时间查询走独立 `ts_ms` 索引列?→ ✅
- 热路径调 LLM 了吗?→ ❌(打标是确定性 kNN/centroid;LLM 只在 Dream)
- 每个新结构能说出它回答的精确查询?→ 见上表"角色"列
