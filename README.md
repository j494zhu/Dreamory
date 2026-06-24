# Dreamory

情绪感知 + 层级记忆的伴侣 App。两大支柱:

1. **情绪状态机** —— 以 *Emotion Bids* + *Attachment Theory* 为核心的隐藏参数集
   ("脑内剧场"),用纯代码维护、确定性地编译进 prompt,让 LLM 不再"永远满状态、
   每条回复一样长"。
2. **三级层级记忆(L1 / L2 / L3 + 正交 Tag 注册表)** —— MemGPT / Letta 风格,在
   有限上下文窗口下做近乎无限、可持续的长期记忆。

> 铁律:**内容只存一份(L3)**;其它层只持 id。**进向量的文本是纯内容**,
> tag / 时间 / 说话人一律作旁挂列,检索时当 `WHERE` 过滤。**热路径零 LLM**;
> LLM 只在离线 Dream 阶段"给已成形的簇命名"。派生缓存(L2、摘要)可丢可重建,
> 绝不反向成为真相。

技术栈:**FastAPI** + 原生 **HTML/CSS/JS**;**DeepSeek v4 pro/flash**;
**bge-m3** embedding;**PostgreSQL + pgvector**。

---

## 架构总览

```
浏览器 (static/)  ──HTTP──>  FastAPI (app/)
                                │
        ┌───────────────────────┼────────────────────────┐
        ▼                       ▼                        ▼
  affect/ 情绪引擎        conversation/pipeline      memory/ 三级记忆
  state/persona          每轮编排:                   l3_store  (L3 冷存储)
  dynamics(纯代码)        ①时间效应 ②抽取(flash)       tags     (注册表+kNN打标)
  extractor(LLM①)        ③动力学  ④落库+打标         retrieval(双轴召回)
  injector(状态→prompt)   ⑤组装L1  ⑥生成(pro)         l2_hot   (热度+批量回写)
                          ⑦落库+打标 ⑧存状态          l1_assembly(去重+预算)
                                                      dream    (离线维护,默认关)
                                │
                                ▼
                    PostgreSQL + pgvector
            memories(主表 + content_vec + emotion_vec)
            tags / tag_aliases / chats
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
| POST | `/api/chats/{id}/messages` | 发消息 → 跑完整管线,返回回复 + debug |
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
- `tests/test_memory.py` —— fallback embedder 的维度/归一化/语义序;token 估算;
  L1 全局去重(刻骨铭心 > L2热 > 检索)、排除工作记忆窗口、预算溢出丢弃。

### 当前验证状态

- ✅ 全模块导入、FastAPI 路由注册
- ✅ 14 个单测通过(情绪动力学 + 记忆纯函数 + fallback embedder),离线 < 1s
- ✅ Schema DDL 针对 Postgres 方言编译通过(VECTOR 列、HNSW/GIN/B-tree 索引、enum)
- ✅ **真实 API 连通已验证**:bge-m3 经 SiliconFlow 返回 1024 维向量;
  DeepSeek `deepseek-v4-pro` / `deepseek-v4-flash`(pro/flash)均正常返回。
- ⏳ 仅剩完整端到端需 **Postgres(docker daemon 当前未运行)**;`.env` 已填好真实 key。

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
