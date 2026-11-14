"""Admin-managed settings stored in the ``setting`` table, with typed defaults."""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from inleverboks.adapters.clock import Clock
from inleverboks.config import Settings
from inleverboks.db.models import Setting

DEFAULT_EXTENSIONS = [
    "docx", "xlsx", "pptx", "pdf", "txt", "md", "csv", "zip",
    "py", "sh", "js", "ts", "html", "css", "java", "c", "h", "cpp", "cs",
    "sql", "json", "yaml", "yml", "xml",
    "png", "jpg", "jpeg", "gif", "webp",
]  # fmt: skip

_EXTENSION_RE = re.compile(r"^[a-z0-9]{1,10}$")


@dataclass
class SystemSettings:
    retention_days: int = 60
    student_number_regex: str = r"^\d{4,12}$"
    default_max_file_mb: int = 25
    default_max_submission_mb: int = 100
    max_files_per_submission: int = 20
    default_extensions: list[str] = field(default_factory=lambda: list(DEFAULT_EXTENSIONS))
    require_totp_all: bool = False


class SettingsValidationError(ValueError):
    def __init__(self, problems: dict[str, str]) -> None:
        super().__init__(", ".join(f"{k}: {v}" for k, v in problems.items()))
        self.problems = problems


def parse_extensions(raw: str | list[str]) -> list[str]:
    """Normalize an extension list: lowercase, no dots, unique, sorted input order kept."""
    items = raw if isinstance(raw, list) else [raw]
    parts = [part for item in items for part in re.split(r"[\s,;]+", item)]
    result: list[str] = []
    for part in parts:
        ext = part.strip().lower().lstrip(".").lstrip("*.")
        if ext and ext not in result:
            result.append(ext)
    return result


class SettingsService:
    def __init__(self, clock: Clock, env: Settings) -> None:
        self.clock = clock
        self.env = env

    def load(self, db: Session) -> SystemSettings:
        stored = {row.key: row.value for row in db.scalars(select(Setting))}
        defaults = SystemSettings(
            default_max_file_mb=min(25, self.env.max_file_mb),
            default_max_submission_mb=min(100, self.env.max_submission_mb),
            max_files_per_submission=self.env.max_files_per_submission,
        )
        values = asdict(defaults)
        for f in fields(SystemSettings):
            if f.name in stored:
                values[f.name] = stored[f.name]
        return SystemSettings(**values)

    def validate(self, new: SystemSettings) -> None:
        problems: dict[str, str] = {}
        if not 1 <= new.retention_days <= 3650:
            problems["retention_days"] = "range"
        try:
            re.compile(new.student_number_regex)
        except re.error:
            problems["student_number_regex"] = "invalid_regex"
        if not 1 <= new.default_max_file_mb <= self.env.max_file_mb:
            problems["default_max_file_mb"] = "range"
        if not 1 <= new.default_max_submission_mb <= self.env.max_submission_mb:
            problems["default_max_submission_mb"] = "range"
        if new.default_max_file_mb > new.default_max_submission_mb:
            problems["default_max_file_mb"] = "exceeds_submission"
        if not 1 <= new.max_files_per_submission <= self.env.max_files_per_submission:
            problems["max_files_per_submission"] = "range"
        if not new.default_extensions or not all(
            _EXTENSION_RE.fullmatch(e) for e in new.default_extensions
        ):
            problems["default_extensions"] = "invalid"
        if problems:
            raise SettingsValidationError(problems)

    def save(
        self, db: Session, new: SystemSettings, *, by: uuid.UUID | None
    ) -> dict[str, dict[str, Any]]:
        """Persist changed values; returns {key: {"old": .., "new": ..}} for the audit log."""
        self.validate(new)
        current = self.load(db)
        changes: dict[str, dict[str, Any]] = {}
        now = self.clock.now()
        for f in fields(SystemSettings):
            old_value, new_value = getattr(current, f.name), getattr(new, f.name)
            if old_value == new_value:
                continue
            changes[f.name] = {"old": old_value, "new": new_value}
            row = db.get(Setting, f.name)
            if row is None:
                db.add(Setting(key=f.name, value=new_value, updated_at=now, updated_by_id=by))
            else:
                row.value = new_value
                row.updated_at = now
                row.updated_by_id = by
        return changes
