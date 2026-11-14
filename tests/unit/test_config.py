from __future__ import annotations

import ipaddress
from pathlib import Path

import pytest
from pydantic import ValidationError

from inleverboks.config import Settings
from tests.conftest import make_settings


def test_defaults(tmp_path: Path) -> None:
    s = make_settings(tmp_path)
    assert s.max_file_mb == 25
    assert s.max_submission_mb == 100
    assert s.max_files_per_submission == 20
    assert s.app_timezone == "Europe/Amsterdam"
    assert s.trusted_proxies == []
    assert Settings.model_fields["scheduler_enabled"].default is True  # tests turn it off
    assert s.backup_keep_days == 7
    assert s.hsts_enabled is False
    assert s.demo_mode is False


def test_trailing_slash_removed(tmp_path: Path) -> None:
    s = make_settings(tmp_path, app_base_url="https://inleverboks.hoite.nl/")
    assert s.app_base_url == "https://inleverboks.hoite.nl"


def test_base_url_requires_scheme(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="http"):
        make_settings(tmp_path, app_base_url="inleverboks.hoite.nl")


def test_secret_key_too_short(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        make_settings(tmp_path, secret_key="short")


def test_trusted_proxies_parsed_from_comma_list(tmp_path: Path) -> None:
    s = make_settings(tmp_path, trusted_proxies="10.42.0.0/16, 192.168.1.1 ,fd00::/8")
    assert s.trusted_proxies == [
        ipaddress.ip_network("10.42.0.0/16"),
        ipaddress.ip_network("192.168.1.1/32"),
        ipaddress.ip_network("fd00::/8"),
    ]
    assert s.forwarded_allow_ips() == "10.42.0.0/16,192.168.1.1/32,fd00::/8"


def test_trusted_proxies_empty_string(tmp_path: Path) -> None:
    assert make_settings(tmp_path, trusted_proxies="").trusted_proxies == []


def test_invalid_cidr_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        make_settings(tmp_path, trusted_proxies="not-an-ip")


def test_unknown_timezone_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="time zone"):
        make_settings(tmp_path, app_timezone="Mars/Olympus")


def test_file_limit_may_not_exceed_submission_limit(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="MAX_FILE_MB"):
        make_settings(tmp_path, max_file_mb=200, max_submission_mb=100)


def test_s3_requires_settings(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="S3_ENDPOINT"):
        make_settings(tmp_path, storage_backend="s3")


def test_env_variables_are_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_BASE_URL", "https://example.test")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.0/8")
    monkeypatch.setenv("MAX_FILE_MB", "10")
    s = Settings(_env_file=None)
    assert s.app_base_url == "https://example.test"
    assert s.trusted_proxies == [ipaddress.ip_network("10.0.0.0/8")]
    assert s.max_file_mb == 10
    assert s.max_request_bytes == 101 * 1024 * 1024
