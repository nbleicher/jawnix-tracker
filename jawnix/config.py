from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./jawnix-dev.db"
    session_secret: str = Field(default="development-only-change-me", alias="JAWNIX_SESSION_SECRET")
    session_ttl_seconds: int = Field(default=86400, alias="JAWNIX_SESSION_TTL_SECONDS")
    cookie_secure: bool = Field(default=True, alias="JAWNIX_COOKIE_SECURE")
    billing_enabled: bool = Field(default=False, alias="JAWNIX_ENABLE_BILLING")
    public_base_url: str = Field(default="http://localhost:8080", alias="JAWNIX_PUBLIC_BASE_URL")

    supabase_url: str = Field(default="", alias="JAWNIX_SUPABASE_URL")
    supabase_anon_key: str = Field(default="", alias="JAWNIX_SUPABASE_ANON_KEY")
    supabase_service_role_key: str = Field(default="", alias="SUPABASE_SERVICE_ROLE_KEY")

    slack_bot_token: str = Field(default="", alias="SLACK_BOT_TOKEN")
    slack_signing_secret: str = Field(default="", alias="SLACK_SIGNING_SECRET")
    slack_channel_id: str = Field(default="", alias="SLACK_CHANNEL_ID")
    slack_approver_user_ids: str = Field(default="", alias="SLACK_APPROVER_USER_IDS")

    resend_api_key: str = Field(default="", alias="RESEND_API_KEY")
    resend_webhook_secret: str = Field(default="", alias="RESEND_WEBHOOK_SECRET")
    batch_from_email: str = Field(default="Jawnix Batches <batches@jawnix.com>", alias="JAWNIX_BATCH_FROM_EMAIL")
    batch_dir: Path = Field(default=Path("./batches"), alias="JAWNIX_BATCH_DIR")
    batch_retention_days: int = Field(default=30, alias="JAWNIX_BATCH_RETENTION_DAYS")
    global_cooldown_days: int = Field(default=7, alias="JAWNIX_GLOBAL_COOLDOWN_DAYS")
    worker_poll_seconds: float = Field(default=2.0, alias="JAWNIX_WORKER_POLL_SECONDS")
    worker_id: str = Field(default="worker-1", alias="JAWNIX_WORKER_ID")
    job_lock_timeout_seconds: int = Field(default=900, alias="JAWNIX_JOB_LOCK_TIMEOUT_SECONDS")

    scraper_db_path: Path = Field(default=Path("/data/health_leads/data/leads.db"), alias="JAWNIX_SCRAPER_DB_PATH")
    scraper_command: str = Field(default="", alias="JAWNIX_SCRAPER_COMMAND")
    scraper_hour_utc: int = Field(default=3, alias="JAWNIX_SCRAPER_HOUR_UTC")
    nppes_index_url: str = Field(
        default="https://download.cms.gov/nppes/NPI_Files.html",
        alias="JAWNIX_NPPES_INDEX_URL",
    )

    @property
    def slack_approvers(self) -> set[str]:
        return {value.strip() for value in self.slack_approver_user_ids.split(",") if value.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
