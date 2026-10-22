from functools import lru_cache
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+asyncpg://anymob:anymob@localhost:5432/anymob"
    redis_url: str = "redis://localhost:6379/0"
    secret_key: str = Field(min_length=32)
    encryption_key: str = ""
    frontend_url: str = "http://localhost:3000"
    cors_origins: list[str] = ["http://localhost:3000"]
    cookie_secure: bool = False
    demo_reset_enabled: bool = False
    access_minutes: int = 15
    refresh_days: int = 30
    s3_endpoint: str = "http://localhost:9000"
    s3_public_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "anymob"
    s3_secret_key: str = "anymob-local-storage"
    s3_bucket: str = "anymob"
    s3_region: str = "us-east-1"
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_from: str = "noreply@anymob.local"
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = False
    sms_provider: Literal["disabled", "development"] = "disabled"
    payment_provider: Literal["disabled", "sandbox"] = "disabled"
    payment_webhook_secret: str = ""
    # Header carrying the client IP when behind a proxy that sets it (e.g. "x-real-ip" on Vercel). Leave empty
    # when clients connect directly, since the header could otherwise be spoofed.
    trusted_ip_header: str = ""
    # Shared with the Next.js frontend, which proxies /api/v1 and forwards the browser's IP in
    # x-anymob-client-ip. That header is trusted only when x-anymob-proxy-key matches this value.
    proxy_secret: str = ""
    # Bearer secret for scheduled job endpoints; they are disabled while empty.
    cron_secret: str = ""
    # Deliver outbox events at the end of the request that queued them, for hosts without a Celery worker.
    outbox_inline: bool = False

    @field_validator("database_url")
    @classmethod
    def asyncpg_url(cls, value: str) -> str:
        # Hosted providers such as Neon issue libpq URLs; asyncpg needs its own scheme and `ssl` instead of
        # `sslmode`, and does not support `channel_binding`.
        parts = urlsplit(value)
        scheme = "postgresql+asyncpg" if parts.scheme in {"postgres", "postgresql"} else parts.scheme
        query = [
            ("ssl" if key == "sslmode" else key, val)
            for key, val in parse_qsl(parts.query)
            if key != "channel_binding"
        ]
        return urlunsplit(parts._replace(scheme=scheme, query=urlencode(query)))

    @model_validator(mode="after")
    def production_requirements(self):
        if self.environment == "production":
            if self.demo_reset_enabled:
                raise ValueError("Demo reset is forbidden in production")
            if not self.cookie_secure or not self.encryption_key:
                raise ValueError("Production requires secure cookies and an encryption key")
            if "replace-with" in self.secret_key or self.payment_provider == "sandbox":
                raise ValueError("Development credentials/payment adapters are forbidden in production")
            if self.sms_provider == "development":
                raise ValueError("Development SMS adapter is forbidden in production")
            if not self.frontend_url.startswith("https://") or any(
                not origin.startswith("https://") for origin in self.cors_origins
            ):
                raise ValueError("Production origins must use HTTPS")
        return self


@lru_cache
def settings() -> Settings:
    return Settings()
