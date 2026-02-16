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
    database_url: str = "postgresql+asyncpg://byfrost:byfrost@localhost:5433/byfrost"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # CORS
    cors_origins: list[str] = ["http://localhost:3000"]

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    server_url: str = "http://localhost:8000"

    # GitHub OAuth
    github_client_id: str = ""
    github_client_secret: str = ""

    # Encryption (AES-256-GCM for HMAC secrets at rest)
    encryption_key: str = ""  # Base64-encoded 32-byte key

    # JWT
    jwt_secret_key: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 60
    jwt_refresh_token_expire_days: int = 30


def get_settings() -> Settings:
    """Return settings instance."""
    return Settings()
