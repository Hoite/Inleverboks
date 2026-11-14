"""Application configuration, loaded from environment variables (see .env.example)."""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

MIN_SECRET_KEY_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_base_url: str
    secret_key: SecretStr
    database_url: str = "sqlite:////data/inleverboks.db"

    storage_backend: Literal["local", "s3"] = "local"
    storage_path: str = "/data/files"
    s3_endpoint: str | None = None
    s3_bucket: str | None = None
    s3_region: str | None = None
    s3_access_key: SecretStr | None = None
    s3_secret_key: SecretStr | None = None

    admin_email: str | None = None
    admin_password: SecretStr | None = None

    app_timezone: str = "Europe/Amsterdam"
    trusted_proxies: Annotated[list[IPNetwork], NoDecode] = Field(default_factory=list)

    max_file_mb: int = Field(default=25, gt=0)
    max_submission_mb: int = Field(default=100, gt=0)
    max_files_per_submission: int = Field(default=20, gt=0)

    scheduler_enabled: bool = True
    run_migrations_on_start: bool = True

    backup_path: str | None = None
    backup_schedule: str = "0 2 * * *"
    backup_keep_days: int = Field(default=7, ge=1)

    session_cookie_secure: bool = True
    hsts_enabled: bool = False

    log_level: Literal["debug", "info", "warning", "error"] = "info"
    log_format: Literal["json", "console"] = "json"

    demo_mode: bool = False

    @field_validator("app_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            msg = "APP_BASE_URL must start with http:// or https://"
            raise ValueError(msg)
        return value.rstrip("/")

    @field_validator("secret_key")
    @classmethod
    def _secret_key_length(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < MIN_SECRET_KEY_LENGTH:
            msg = f"SECRET_KEY must be at least {MIN_SECRET_KEY_LENGTH} characters"
            raise ValueError(msg)
        return value

    @field_validator("trusted_proxies", mode="before")
    @classmethod
    def _parse_cidrs(cls, value: object) -> object:
        if isinstance(value, str):
            return [
                ipaddress.ip_network(part.strip(), strict=False)
                for part in value.split(",")
                if part.strip()
            ]
        return value

    @field_validator("app_timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            msg = f"Unknown time zone: {value}"
            raise ValueError(msg) from exc
        return value

    @field_validator("s3_endpoint", "s3_bucket", "s3_region", "backup_path", mode="before")
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        return None if value == "" else value

    @model_validator(mode="after")
    def _check_consistency(self) -> Settings:
        if self.max_file_mb > self.max_submission_mb:
            msg = "MAX_FILE_MB may not exceed MAX_SUBMISSION_MB"
            raise ValueError(msg)
        if self.storage_backend == "s3":
            missing = [
                name
                for name in ("s3_endpoint", "s3_bucket", "s3_access_key", "s3_secret_key")
                if getattr(self, name) is None
            ]
            if missing:
                msg = f"STORAGE_BACKEND=s3 requires: {', '.join(m.upper() for m in missing)}"
                raise ValueError(msg)
        return self

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.app_timezone)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def max_request_bytes(self) -> int:
        """Hard cap on any request body: a full submission plus multipart overhead."""
        return self.max_submission_mb * 1024 * 1024 + 1024 * 1024

    def forwarded_allow_ips(self) -> str:
        """Value for uvicorn's --forwarded-allow-ips."""
        return ",".join(str(net) for net in self.trusted_proxies)


@lru_cache
def get_settings() -> Settings:
    return Settings()
