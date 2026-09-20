from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    DATABASE_URL: str
    ANTHROPIC_API_KEY: str | None = None
    FINRECUR_DEMO: bool = True
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:3100"


# DATABASE_URL has no default so a missing .env fails fast; pydantic-settings supplies
# it from the environment/`.env` at runtime, which pyright can't see statically.
settings = Settings()  # type: ignore[call-arg]
