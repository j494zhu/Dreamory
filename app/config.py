"""
Central configuration. All tunables live here so the rest of the codebase
never reaches for os.environ directly.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ── Database ──────────────────────────────────────────────────
    database_url: str = Field(
        "postgresql+asyncpg://dreamory:dreamory@localhost:5432/dreamory",
        alias="DATABASE_URL",
    )

    # ── DeepSeek (OpenAI-compatible) ──────────────────────────────
    deepseek_api_key: str = Field("", alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field("https://api.deepseek.com", alias="DEEPSEEK_BASE_URL")
    # pro = generation + Dream naming; flash = cheap event extraction.
    deepseek_model_pro: str = Field("deepseek-v4-pro", alias="DEEPSEEK_MODEL_PRO")
    deepseek_model_flash: str = Field("deepseek-v4-flash", alias="DEEPSEEK_MODEL_FLASH")
    # 事件抽取用哪个模型:"pro" / "flash" / 任意显式模型 id。
    # 0.6.1 起默认 pro——抽取是全管线最脆弱的一环(单条消息的语用分类,反讽/玩笑
    # 极易误判),而误判会被 dynamics 不打折地执行;flash 省的钱远小于误判的代价。
    extractor_model: str = Field("pro", alias="EXTRACTOR_MODEL")

    # ── Embeddings ────────────────────────────────────────────────
    # "api"      -> bge-m3 over an OpenAI-compatible HTTP API (e.g. SiliconFlow)
    # "bge_m3"   -> local BAAI/bge-m3 via FlagEmbedding (requires torch)
    # "fallback" -> deterministic hash embedder, zero deps, for local dev/CI
    embedding_backend: str = Field("fallback", alias="EMBEDDING_BACKEND")
    embedding_model: str = Field("BAAI/bge-m3", alias="EMBEDDING_MODEL")
    embedding_dim: int = Field(1024, alias="EMBEDDING_DIM")
    # used only when embedding_backend == "api"
    embedding_api_key: str = Field("", alias="EMBEDDING_API_KEY")
    embedding_base_url: str = Field("https://api.siliconflow.cn/v1", alias="EMBEDDING_BASE_URL")

    # ── App ───────────────────────────────────────────────────────
    app_host: str = Field("127.0.0.1", alias="APP_HOST")
    app_port: int = Field(8000, alias="APP_PORT")
    debug_panel: bool = Field(True, alias="DEBUG_PANEL")
    # 感知/决策日志(0.6.1,测试期审计):每轮落一行 turn_logs。
    turn_log_enabled: bool = Field(True, alias="TURN_LOG_ENABLED")
    # 连完整 system prompt 一起存(体积大,深度排查时才开)
    turn_log_full_prompt: bool = Field(False, alias="TURN_LOG_FULL_PROMPT")
    # ── 测试期访问控制(0.6.1)────────────────────────────────────
    # 空 = 鉴权关闭(本地开发)。设置后:admin 全通;每个 chat 的 access_token
    # 只开自己那扇门(专属链接 /?chat=<id>&token=<access_token>)。
    admin_token: str = Field("", alias="ADMIN_TOKEN")
    # 每个 chat 24 小时内的用户消息上限(成本闸,0 = 不限)
    daily_message_limit: int = Field(0, alias="DAILY_MESSAGE_LIMIT")

    # ── Memory tuning ─────────────────────────────────────────────
    working_memory_k: int = Field(10, alias="WORKING_MEMORY_K")
    retrieval_top_k: int = Field(6, alias="RETRIEVAL_TOP_K")
    l2_capacity: int = Field(256, alias="L2_CAPACITY")
    l1_token_budget: int = Field(2400, alias="L1_TOKEN_BUDGET")
    heat_flush_seconds: int = Field(15, alias="HEAT_FLUSH_SECONDS")
    heat_flush_max_buffer: int = Field(64, alias="HEAT_FLUSH_MAX_BUFFER")
    heat_halflife_seconds: float = Field(86400.0, alias="HEAT_HALFLIFE_SECONDS")

    # ── Tagging ───────────────────────────────────────────────────
    tag_knn_k: int = Field(10, alias="TAG_KNN_K")
    tag_vote_threshold: float = Field(0.6, alias="TAG_VOTE_THRESHOLD")
    tag_max_per_memory: int = Field(5, alias="TAG_MAX_PER_MEMORY")

    # ── Timer(时间感知 + 主动消息)────────────────────────────────
    # 她可以在回复里用 <timer minutes="X">…</timer> 约一个"过会儿来找他";
    # 后台调度器到点触发一次隐藏的 LLM 调用,经 SSE 把主动消息推给前端。
    timer_enabled: bool = Field(True, alias="TIMER_ENABLED")
    timer_poll_seconds: int = Field(5, alias="TIMER_POLL_SECONDS")
    timer_max_pending: int = Field(3, alias="TIMER_MAX_PENDING")   # 每个 chat 同时挂着的闹钟上限
    timer_max_minutes: int = Field(1440, alias="TIMER_MAX_MINUTES")

    # ── Tools(生成端 agent loop:主动回忆 / grep / 闹钟)──────────
    # 关掉则退回 0.2.0 的单次生成 + <timer> 标签协议。
    tools_enabled: bool = Field(True, alias="TOOLS_ENABLED")
    tool_max_rounds: int = Field(3, alias="TOOL_MAX_ROUNDS")   # 工具往返上限,超过强制作答
    # 自动检索 top1 裸分数(goal 偏置前)低于此值 → 注入"记忆很模糊,可以主动翻"的提示。
    # 需要 > RETRIEVAL_MIN_SCORE,否则永远不触发(过滤后幸存者都在下限之上)。
    retrieval_confidence: float = Field(0.55, alias="RETRIEVAL_CONFIDENCE")
    # 自动检索的绝对相关性下限(裸分数)。kNN top-K 永远会凑满 K 条——没有下限,
    # 池子里没有相关内容时"最不不相关"的底噪照样进 L1、照样涨热度。
    # bge-m3 上两段无关中文闲聊约 0.35~0.5,真相关通常 0.55+。
    retrieval_min_score: float = Field(0.50, alias="RETRIEVAL_MIN_SCORE")
    # life_event(生活正史)在自动检索里要求更高的分数线:随口想起生活琐事
    # 归话题种子通道管;只有他真的聊到那件事(高相关)才该被自动"想起来"。
    retrieval_min_score_life: float = Field(0.60, alias="RETRIEVAL_MIN_SCORE_LIFE")

    # ── Life sim(生活模拟器:离线预生成她的线下生活)───────────────
    life_sim_enabled: bool = Field(True, alias="LIFE_SIM_ENABLED")
    life_sim_min_interval_hours: float = Field(6.0, alias="LIFE_SIM_MIN_INTERVAL_HOURS")
    life_sim_fresh_target: int = Field(2, alias="LIFE_SIM_FRESH_TARGET")  # 新鲜种子低于此数才补写
    # 话题种子注入的冷却轮数(距上次注入至少隔这么多轮)
    seed_cooldown_turns: int = Field(6, alias="SEED_COOLDOWN_TURNS")

    # ── Schedule(日程表)──────────────────────────────────────────
    schedule_enabled: bool = Field(True, alias="SCHEDULE_ENABLED")

    # ── Guardrail(守护层:防角色崩坏,绝不吐机械警告)───────────────
    guardrail_enabled: bool = Field(True, alias="GUARDRAIL_ENABLED")

    # ── Notebook(她的小本子:write_note 随手记 + 夜间日记)──────────
    notes_enabled: bool = Field(True, alias="NOTES_ENABLED")
    notes_max_active: int = Field(12, alias="NOTES_MAX_ACTIVE")

    # ── Night agent(夜间代理:蒸馏/日记/明日计划/Dream)─────────────
    night_agent_enabled: bool = Field(True, alias="NIGHT_AGENT_ENABLED")
    night_poll_seconds: int = Field(600, alias="NIGHT_POLL_SECONDS")
    night_idle_hours: float = Field(2.0, alias="NIGHT_IDLE_HOURS")      # 用户静默这么久才开工
    night_min_gap_hours: float = Field(20.0, alias="NIGHT_MIN_GAP_HOURS")  # 每晚最多一次
    # 允许夜间代理改她自己的长期作息(routine)。默认关:只允许排明日 oneoff。
    night_agent_edit_routine: bool = Field(False, alias="NIGHT_AGENT_EDIT_ROUTINE")

    # ── Persona evolution(好感度里程碑解锁的人格演化)──────────────
    persona_evolution_enabled: bool = Field(True, alias="PERSONA_EVOLUTION_ENABLED")

    # ── Commitment(承诺兑现闭环:到点没兑现她会主动催)─────────────
    commitment_ping_enabled: bool = Field(True, alias="COMMITMENT_PING_ENABLED")
    # 过点多少分钟后催(到期估算本就粗,别掐着秒催)
    commitment_ping_delay_min: int = Field(30, alias="COMMITMENT_PING_DELAY_MIN")

    # ── Dream ─────────────────────────────────────────────────────
    # Auto-dream is ON: after a turn commits, the pipeline fires should_dream()
    # and, if the pending backlog is high enough, runs a Dream cycle in the
    # background (non-blocking). Set DREAM_ENABLED=false to disable.
    dream_enabled: bool = Field(True, alias="DREAM_ENABLED")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
