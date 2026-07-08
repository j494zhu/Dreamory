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
    # 自动检索 top1 分数低于此值 → 注入"记忆很模糊,可以主动翻"的提示
    retrieval_confidence: float = Field(0.45, alias="RETRIEVAL_CONFIDENCE")

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

    # ── Dream ─────────────────────────────────────────────────────
    # Auto-dream is ON: after a turn commits, the pipeline fires should_dream()
    # and, if the pending backlog is high enough, runs a Dream cycle in the
    # background (non-blocking). Set DREAM_ENABLED=false to disable.
    dream_enabled: bool = Field(True, alias="DREAM_ENABLED")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
