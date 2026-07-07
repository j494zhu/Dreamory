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

    # ── Dream ─────────────────────────────────────────────────────
    # Auto-dream is ON: after a turn commits, the pipeline fires should_dream()
    # and, if the pending backlog is high enough, runs a Dream cycle in the
    # background (non-blocking). Set DREAM_ENABLED=false to disable.
    dream_enabled: bool = Field(True, alias="DREAM_ENABLED")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
