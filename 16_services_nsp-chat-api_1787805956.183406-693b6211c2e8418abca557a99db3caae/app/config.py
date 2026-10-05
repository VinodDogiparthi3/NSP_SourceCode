from functools import lru_cache
import re

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]+$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    demo_mode: bool = True
    gcp_project: str = "mos-gcp-host-dev-prj"
    bq_dataset: str = "dw_nsp_api_views"
    bq_location: str = "me-central2"
    bq_embeddings_table: str = "nsp_rag_embeddings_clean"
    embedding_model: str = "embeddinggemma-300m"
    bq_vector_mode: str = "legacy_struct"
    vertex_location: str = "global"
    gemini_model: str = "gemini-2.5-flash"
    top_k: int = Field(default=8, ge=1, le=20)
    frontend_origin: str = "http://localhost:5173"

    @field_validator(
        "gcp_project",
        "bq_dataset",
        "bq_embeddings_table",
        "embedding_model",
        "bq_location",
    )
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if not SAFE_IDENTIFIER.fullmatch(value):
            raise ValueError(f"Unsafe configuration identifier: {value}")
        return value

    @field_validator("bq_vector_mode")
    @classmethod
    def validate_vector_mode(cls, value: str) -> str:
        if value not in {"legacy_struct", "flat"}:
            raise ValueError("BQ_VECTOR_MODE must be legacy_struct or flat")
        return value

    @property
    def table_path(self) -> str:
        return f"{self.gcp_project}.{self.bq_dataset}.{self.bq_embeddings_table}"


@lru_cache
def get_settings() -> Settings:
    return Settings()

