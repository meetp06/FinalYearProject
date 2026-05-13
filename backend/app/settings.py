from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # HF Inference Endpoint (fine-tuned Phi-4-mini)
    hf_endpoint_url: str = Field(default="")
    hf_token: str = Field(default="")

    # Groq / OpenAI-compatible LLM
    groq_api_key: str = Field(default="")
    groq_api_key_fallback: str = Field(default="")
    llm_model: str = Field(default="llama-3.3-70b-versatile")
    llm_provider: str = Field(default="groq")  # "groq" or "hf"

    llm_timeout_seconds: float = Field(default=120.0)
    agent_max_steps: int = Field(default=6)

    embedding_model: str = Field(default="sentence-transformers/all-MiniLM-L6-v2")
    embedding_dim: int = Field(default=384)

    database_url: str = Field(
        default="postgresql+psycopg://support:support@postgres:5432/support"
    )

    qdrant_url: str = Field(default="http://qdrant:6333")
    qdrant_collection: str = Field(default="amazon_products_2023")
    qdrant_policy_collection: str = Field(default="support_policies")
    rag_top_k: int = Field(default=6)
    #: Drop weak Qdrant neighbors so refund/order "related_products" cites don't show random electronics.
    rag_related_min_score: float = Field(default=0.56, ge=0.0, le=1.0)
    #: If True, retrieved title must share a meaningful token with the order-line query unless semantic score clears this bar.
    rag_related_require_keyword_overlap: bool = Field(default=True)
    rag_related_keyword_min_len: int = Field(default=3, ge=2, le=8)
    rag_related_strong_semantic_score: float = Field(default=0.72, ge=0.0, le=1.0)
    #: Softer floor for explicit customer catalog searches (search_product_knowledge).
    rag_catalog_min_score: float = Field(default=0.48, ge=0.0, le=1.0)

    smtp_host: str = Field(default="")
    smtp_port: int = Field(default=587)
    smtp_username: str = Field(default="")
    smtp_password: str = Field(default="")
    smtp_from: str = Field(default="support@example.com")
    smtp_use_tls: bool = Field(default=False)

    escalation_webhook_url: str = Field(default="")

    app_host: str = Field(default="0.0.0.0")
    app_port: int = Field(default=8000)
    app_log_level: str = Field(default="info")
    cors_origins: str = Field(default="*")

    @property
    def cors_origin_list(self) -> List[str]:
        raw = self.cors_origins.strip()
        if not raw or raw == "*":
            return ["*"]
        return [o.strip() for o in raw.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
