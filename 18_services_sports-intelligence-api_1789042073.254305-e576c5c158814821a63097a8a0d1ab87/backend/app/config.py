from functools import lru_cache
import os
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(
    os.getenv("APP_ROOT", str(Path(__file__).resolve().parents[2]))
).resolve()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / ".env",
        extra="ignore",
    )

    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-2.5-flash"
    gemini_fallback_model: str = "gemini-2.5-flash-lite"
    gemini_secondary_fallback_model: str = "gemini-3.1-flash-lite"
    google_genai_use_vertexai: bool = False
    gcp_project: str = ""
    vertex_location: str = "global"
    enable_vector_search: bool = True
    data_dir: Path = Path("data")
    chroma_dir: Path = Path("data/chroma")
    model_dir: Path = Path("data/models")
    embedding_model: str = (
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )
    top_k: int = Field(default=8, ge=1, le=20)
    max_distance: float = Field(default=0.60, ge=0, le=2)
    frontend_origin: str = "http://localhost:5173"

    def absolute(self, path: Path) -> Path:
        return path if path.is_absolute() else PROJECT_ROOT / path

    @property
    def data_path(self) -> Path:
        return self.absolute(self.data_dir)

    @property
    def chroma_path(self) -> Path:
        return self.absolute(self.chroma_dir)

    @property
    def model_path(self) -> Path:
        return self.absolute(self.model_dir)

    @property
    def nsp_glob(self) -> str:
        return str(self.data_path / "raw" / "nsp" / "*.parquet")

    @property
    def sopc_glob(self) -> str:
        return str(self.data_path / "raw" / "sopc" / "*.parquet")

    @property
    def gemini_key(self) -> str | None:
        if not self.gemini_api_key:
            return None
        value = self.gemini_api_key.get_secret_value().strip()
        return value or None

    @property
    def gemini_enabled(self) -> bool:
        return bool(
            self.gemini_key
            or (self.google_genai_use_vertexai and self.gcp_project.strip())
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
