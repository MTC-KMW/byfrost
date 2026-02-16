"""Server configuration loaded from environment variables.

All settings have sensible defaults for local Docker Compose development.
Production values are set via environment variables in the deployment.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Coordination server configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Application
    app_name: str = "Byfrost Coordination Server"
    debug: bool = False
    api_version: str = "v1"

    # Database
    database_url: str = "postgresql+asyncpg://byfrost:byfrost@localhost:5432/byfrost"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # CORS
    cors_origins: list[str] = ["http://localhost:3000"]

    # Server
    host: str = "0.0.0.0"
    port: int = 8000


def get_settings() -> Settings:
    """Return settings instance."""
    return Settings()
