"""
config.py – Centralised settings loaded from environment variables / .env file.

All external credentials and tunable thresholds live here.  Import `settings`
anywhere in the codebase; never read os.environ directly.

Usage
─────
    from rfp_responder.config import settings
    client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)

The `@lru_cache` on `get_settings()` ensures the .env file is parsed exactly
once per process.  Tests override individual fields by patching `settings`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Application ───────────────────────────────────────────────────────────
    app_env: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"

    # ── OpenAI ────────────────────────────────────────────────────────────────
    openai_api_key: str = Field(..., description="sk-… key from platform.openai.com")
    synthesis_model: str = "gpt-4o-2024-08-06"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536   # text-embedding-3-small native dim

    # ── Anthropic (fallback synthesizer) ──────────────────────────────────────
    anthropic_api_key: str = Field(..., description="Anthropic API key")
    anthropic_model: str = "claude-3-5-sonnet-20241022"

    # ── Qdrant ────────────────────────────────────────────────────────────────
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None          # None for local / no-auth clusters
    qdrant_collection_name: str = "rfp_answers"

    # ── Neo4j ─────────────────────────────────────────────────────────────────
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = Field(..., description="Neo4j auth password")
    neo4j_database: str = "neo4j"
    # Max concurrent Cypher sessions per driver instance (pool ceiling).
    neo4j_max_connection_pool_size: int = 50

    # ── PostgreSQL  (LangGraph durable checkpointer) ──────────────────────────
    # Format: postgresql+psycopg://user:pass@host:5432/db
    postgres_dsn: str = Field(
        ...,
        description="PostgreSQL DSN for AsyncPostgresSaver. "
                    "Must use the psycopg v3 scheme (postgresql+psycopg://).",
    )

    # ── LangSmith ─────────────────────────────────────────────────────────────
    langchain_api_key: str = Field(..., description="LangSmith API key (LANGCHAIN_API_KEY)")
    langchain_project: str = "rfp-responder"
    langchain_tracing_v2: bool = True    # Enables automatic LangSmith tracing

    # ── Redis / arq job queue ─────────────────────────────────────────────────
    redis_dsn: str = "redis://localhost:6379/0"

    # ── Routing thresholds ────────────────────────────────────────────────────
    # Must match AUTO_APPROVE_VECTOR_THRESHOLD in graph.py – kept here as the
    # single source of truth; graph.py imports it at module load time.
    auto_approve_vector_threshold: float = Field(
        default=0.92, ge=0.0, le=1.0,
        description="Cosine similarity minimum for automatic approval.",
    )

    # ── Retry / resilience ────────────────────────────────────────────────────
    max_retries: int = Field(default=3, ge=1, le=10)
    retry_initial_wait_seconds: float = 1.0   # first backoff interval
    retry_max_wait_seconds: float = 10.0      # backoff ceiling (with jitter)

    # ── Excel export ──────────────────────────────────────────────────────────
    export_dir: str = "/tmp/rfp_exports"      # override with cloud path in prod

    @field_validator("postgres_dsn")
    @classmethod
    def _validate_postgres_scheme(cls, v: str) -> str:
        if not v.startswith(("postgresql+psycopg://", "postgresql://", "postgres://")):
            raise ValueError(
                "postgres_dsn must start with postgresql+psycopg:// "
                "(psycopg v3 required by langgraph-checkpoint-postgres)"
            )
        return v

    @field_validator("embedding_dimensions")
    @classmethod
    def _validate_embedding_dims(cls, v: int) -> int:
        valid = {256, 512, 1536}   # text-embedding-3-small supported dims
        if v not in valid:
            raise ValueError(f"embedding_dimensions must be one of {valid}")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide singleton Settings instance."""
    return Settings()


# Module-level alias – most modules just `from rfp_responder.config import settings`
settings: Settings = get_settings()
