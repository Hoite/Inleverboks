"""The full authorization matrix of spec §2, with PLAN B4 (admins may download files of
other teachers' exams and export them). The expected table is written out independently
of ``policy.EXAM_MATRIX`` so a change in one without the other fails."""

from __future__ import annotations

import uuid

import pytest

from inleverboks.db.models import User
from inleverboks.policy import (
    CLASS_LIST_MATRIX,
    EXAM_MATRIX,
    ClassListAction,
    ExamAction,
    ExamRef,
    SystemAction,
    can,
    can_class_list,
    can_exam,
    exam_roles,
)

OWNER, REVIEWER, OTHER, ADMIN = "owner", "reviewer", "other", "admin"
A = ExamAction

# action: (admin on someone else's exam, owner, second reviewer, other teacher)
EXPECTED: dict[ExamAction, tuple[bool, bool, bool, bool]] = {
    A.EDIT_SETTINGS: (False, True, False, False),
    A.VIEW_SUBMISSIONS: (True, True, True, False),
    A.DOWNLOAD_FILES: (True, True, True, False),  # B4: admin ✓
    A.EXPORT: (True, True, True, False),  # B4: admin ✓
    A.VIEW_AUDIT: (True, True, True, False),
    A.MANAGE_SUBMISSIONS: (False, True, False, False),
    A.SET_HOLD: (True, True, False, False),
    A.TRANSFER_OWNERSHIP: (True, False, False, False),
    A.DELETE_FINALIZED: (True, False, False, False),
    A.DELETE_CONCEPT: (False, True, False, False),
    A.DUPLICATE: (False, True, False, False),
}


def _user(*, admin: bool = False, active: bool = True) -> User:
    return User(id=uuid.uuid4(), email="x@school.nl", name="x", is_admin=admin, is_active=active)


@pytest.fixture
def people() -> dict[str, User]:
    return {
        OWNER: _user(),
        REVIEWER: _user(),
        OTHER: _user(),
        ADMIN: _user(admin=True),
    }


@pytest.fixture
def exam(people: dict[str, User]) -> ExamRef:
    return ExamRef(owner_id=people[OWNER].id, second_reviewer_id=people[REVIEWER].id)


def test_matrix_covers_every_action() -> None:
    assert set(EXPECTED) == set(ExamAction) == set(EXAM_MATRIX)


@pytest.mark.parametrize("action", list(ExamAction))
def test_exam_matrix(action: ExamAction, people: dict[str, User], exam: ExamRef) -> None:
    admin, owner, reviewer, other = EXPECTED[action]
    assert can_exam(people[ADMIN], action, exam) is admin
    assert can_exam(people[OWNER], action, exam) is owner
    assert can_exam(people[REVIEWER], action, exam) is reviewer
    assert can_exam(people[OTHER], action, exam) is other


@pytest.mark.parametrize("action", list(ExamAction))
def test_admin_who_owns_the_exam_has_owner_rights(action: ExamAction) -> None:
    admin = _user(admin=True)
    exam = ExamRef(owner_id=admin.id, second_reviewer_id=None)
    _, owner, _, _ = EXPECTED[action]
    admin_rights = EXPECTED[action][0]
    assert can_exam(admin, action, exam) is (owner or admin_rights)


@pytest.mark.parametrize("action", list(ExamAction))
def test_inactive_or_anonymous_users_can_do_nothing(action: ExamAction, exam: ExamRef) -> None:
    owner = _user(active=False)
    owner_exam = ExamRef(owner_id=owner.id, second_reviewer_id=None)
    assert not can_exam(owner, action, owner_exam)
    assert not can_exam(None, action, exam)


def test_roles(people: dict[str, User], exam: ExamRef) -> None:
    assert exam_roles(people[OWNER], exam) == {"owner"}
    assert exam_roles(people[REVIEWER], exam) == {"second_reviewer"}
    assert exam_roles(people[OTHER], exam) == {"other_teacher"}
    assert exam_roles(people[ADMIN], exam) == {"admin", "other_teacher"}


def test_system_actions(people: dict[str, User]) -> None:
    for name in (OWNER, REVIEWER, OTHER):
        assert can(people[name], SystemAction.CREATE_EXAM)
        for action in SystemAction:
            if action is not SystemAction.CREATE_EXAM:
                assert not can(people[name], action)
    for action in SystemAction:
        assert can(people[ADMIN], action)
    assert not can(None, SystemAction.CREATE_EXAM)
    assert not can(_user(admin=True, active=False), SystemAction.MANAGE_USERS)


# Class lists (PLAN B60) --------------------------------------------------------------

C = ClassListAction

# action: (owner, other teacher, admin on someone else's list)
CLASS_LIST_EXPECTED: dict[ClassListAction, tuple[bool, bool, bool]] = {
    C.VIEW: (True, True, True),
    C.USE: (True, True, True),
    C.COPY: (True, True, True),
    C.RENAME: (True, False, True),
    C.REPLACE: (True, False, True),
    C.DELETE: (True, False, True),
}


def test_class_list_matrix_covers_every_action() -> None:
    assert set(CLASS_LIST_EXPECTED) == set(ClassListAction) == set(CLASS_LIST_MATRIX)


@pytest.mark.parametrize("action", list(ClassListAction))
def test_class_list_matrix(action: ClassListAction, people: dict[str, User]) -> None:
    owner, other, admin = CLASS_LIST_EXPECTED[action]
    owner_id = people[OWNER].id
    assert can_class_list(people[OWNER], action, owner_id) is owner
    assert can_class_list(people[OTHER], action, owner_id) is other
    assert can_class_list(people[ADMIN], action, owner_id) is admin
    assert not can_class_list(None, action, owner_id)
    assert not can_class_list(_user(active=False), action, owner_id)
