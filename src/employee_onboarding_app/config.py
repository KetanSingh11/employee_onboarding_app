from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Database connections
    DATABASE_URL: str = (
        "postgresql+asyncpg://onboarding_user:onboarding_pass@localhost:5432/staffplanner"
    )
    TEST_DATABASE_URL: str = (
        "postgresql+asyncpg://onboarding_user:onboarding_pass@localhost:5432/staffplanner_test"
    )

    # Database connection pool tuning
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_TIMEOUT: int = 30
    DB_POOL_PRE_PING: bool = True

    # Security & API Keys
    API_KEY_ADMIN: str = "admin-secret-key"
    API_KEY_HR: str = "hr-secret-key"
    API_KEY_VIEWER: str = "viewer-secret-key"

    # Observability & Logging
    LOG_LEVEL: str = "INFO"

    # Idempotency cache duration
    IDEMPOTENCY_TTL_SECONDS: int = 86400  # 24 hours

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
