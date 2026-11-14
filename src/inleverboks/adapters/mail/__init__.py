"""Mail interface. There is no mail in v1: ``NullMailer`` only logs that it was asked."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import structlog

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Message:
    to: str
    subject: str
    text: str
    html: str | None = None


class Mailer(Protocol):
    def send(self, message: Message) -> None: ...


class NullMailer:
    def send(self, message: Message) -> None:
        log.info("mail.skipped", subject=message.subject)
