"""Fixtures for integration tests that need an exam with a template file."""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy.orm import Session

from inleverboks.adapters.clock import FrozenClock
from inleverboks.db.models import Exam, TemplateFile, User
from inleverboks.services.container import Services
from tests.conftest import START, create_user
from tests.student_helpers import EMPTY_TEMPLATE, make_exam


@pytest.fixture
def owner(db: Session, clock: FrozenClock) -> User:
    return create_user(db, clock, "docent@school.nl")


@pytest.fixture
def exam(db: Session, services: Services, owner: User) -> Exam:
    exam = make_exam(db, services, owner)
    sha = hashlib.sha256(EMPTY_TEMPLATE).hexdigest()
    slot = exam.expected_files[0]
    slot.template_file = TemplateFile(
        original_name="Examen_Linux_P3_sjabloon.docx", storage_key=f"templates/{sha}",
        size=len(EMPTY_TEMPLATE), sha256=sha, uploaded_at=START,
    )  # fmt: skip
    db.commit()
    return exam
