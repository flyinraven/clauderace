"""Environment-level configuration.

Anything that must be known *before* the database is reachable lives here and is
read from environment variables / `.env`. Everything else that an administrator
should be able to change at runtime (AI provider, model choices, image search
keys, SMTP) lives in the `settings` table instead - see `app.services.settings`.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Core -------------------------------------------------------------
    app_name: str = "RANZCO RACE Exam Simulator"
    environment: str = "development"
    debug: bool = True

    # SQLite for local development; Render sets DATABASE_URL to the SiteGround
    # Postgres connection string in production.
    database_url: str = "sqlite:///./race.db"

    # --- Security ---------------------------------------------------------
    # Signs JWTs. MUST be overridden in production.
    secret_key: str = "dev-only-insecure-secret-change-me"
    access_token_ttl_minutes: int = 60 * 12

    # Fernet key used to encrypt API keys held in the `settings` table.
    # Generated on first run if absent (dev only); set explicitly in production
    # or stored secrets become unreadable after a redeploy.
    settings_encryption_key: str = ""

    # --- Bootstrap admin --------------------------------------------------
    # Applied on startup: creates the account if missing, and promotes it to
    # admin if it exists. Password is only used at creation time.
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = ""

    # --- HTTP -------------------------------------------------------------
    cors_origins: str = "http://localhost:5173,https://exam.txglobal.com.au"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
