"""Central authorization: the role/action matrix from spec §2, adjusted by PLAN B4.

Every route asks this module; no route decides on its own. The matrix is data, so the
tests can compare it cell by cell with the spec.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum

from inleverboks.db.models import User


class ExamRole(StrEnum):
    ADMIN = "admin"  # admin looking at someone else's exam
    OWNER = "owner"
    SECOND_REVIEWER = "second_reviewer"
    OTHER_TEACHER = "other_teacher"


class ExamAction(StrEnum):
    EDIT_SETTINGS = "edit_settings"  # settings, file requirements, times, code, IP list
    VIEW_SUBMISSIONS = "view_submissions"  # overview and metadata
    DOWNLOAD_FILES = "download_files"  # per file, per student, zip
    EXPORT = "export"  # proces-verbaal and CSV
    VIEW_AUDIT = "view_audit"
    MANAGE_SUBMISSIONS = "manage_submissions"  # undo, invalidate, extension
    SET_HOLD = "set_hold"
    TRANSFER_OWNERSHIP = "transfer_ownership"
    DELETE_FINALIZED = "delete_finalized"
    DELETE_CONCEPT = "delete_concept"
    DUPLICATE = "duplicate"  # duplicate and save as exam template


class SystemAction(StrEnum):
    CREATE_EXAM = "create_exam"
    MANAGE_USERS = "manage_users"
    MANAGE_WHITELIST = "manage_whitelist"
    MANAGE_SETTINGS = "manage_settings"
    MANAGE_IP_PRESETS = "manage_ip_presets"
    MANAGE_BRANDING = "manage_branding"
    VIEW_SYSTEM_AUDIT = "view_system_audit"
    VERIFY_RECEIPT = "verify_receipt"  # admin only, for any exam


_A, _O, _S, _T = (
    ExamRole.ADMIN,
    ExamRole.OWNER,
    ExamRole.SECOND_REVIEWER,
    ExamRole.OTHER_TEACHER,
)

EXAM_MATRIX: dict[ExamAction, frozenset[ExamRole]] = {
    ExamAction.EDIT_SETTINGS: frozenset({_O}),
    ExamAction.VIEW_SUBMISSIONS: frozenset({_A, _O, _S}),
    # PLAN B4: admins may download files of other teachers' exams.
    ExamAction.DOWNLOAD_FILES: frozenset({_A, _O, _S}),
    ExamAction.EXPORT: frozenset({_A, _O, _S}),
    ExamAction.VIEW_AUDIT: frozenset({_A, _O, _S}),
    ExamAction.MANAGE_SUBMISSIONS: frozenset({_O}),
    ExamAction.SET_HOLD: frozenset({_A, _O}),
    ExamAction.TRANSFER_OWNERSHIP: frozenset({_A}),
    ExamAction.DELETE_FINALIZED: frozenset({_A}),
    ExamAction.DELETE_CONCEPT: frozenset({_O}),
    ExamAction.DUPLICATE: frozenset({_O}),
}


@dataclass(frozen=True)
class ExamRef:
    """The minimum the policy needs to know about an exam."""

    owner_id: uuid.UUID
    second_reviewer_id: uuid.UUID | None


def exam_roles(user: User, exam: ExamRef) -> frozenset[ExamRole]:
    """All roles a user holds on an exam. An admin who owns the exam is both."""
    roles: set[ExamRole] = set()
    if exam.owner_id == user.id:
        roles.add(ExamRole.OWNER)
    elif exam.second_reviewer_id == user.id:
        roles.add(ExamRole.SECOND_REVIEWER)
    else:
        roles.add(ExamRole.OTHER_TEACHER)
    if user.is_admin:
        roles.add(ExamRole.ADMIN)
    return frozenset(roles)


def can_exam(user: User | None, action: ExamAction, exam: ExamRef) -> bool:
    if user is None or not user.is_active:
        return False
    return bool(exam_roles(user, exam) & EXAM_MATRIX[action])


# Class lists (PLAN B60) ------------------------------------------------------------


class ClassListRole(StrEnum):
    ADMIN = "admin"  # admin looking at someone else's list
    OWNER = "owner"
    OTHER_TEACHER = "other_teacher"


class ClassListAction(StrEnum):
    VIEW = "view"  # library, detail page (names and student numbers)
    USE = "use"  # snapshot into an exam
    COPY = "copy"  # copy to one's own lists
    RENAME = "rename"
    REPLACE = "replace"  # import a new CSV over the entries
    DELETE = "delete"


_CA, _CO, _CT = ClassListRole.ADMIN, ClassListRole.OWNER, ClassListRole.OTHER_TEACHER

CLASS_LIST_MATRIX: dict[ClassListAction, frozenset[ClassListRole]] = {
    # PLAN B60: every teacher sees and uses every class list; only the owner and admins
    # change or delete one.
    ClassListAction.VIEW: frozenset({_CA, _CO, _CT}),
    ClassListAction.USE: frozenset({_CA, _CO, _CT}),
    ClassListAction.COPY: frozenset({_CA, _CO, _CT}),
    ClassListAction.RENAME: frozenset({_CA, _CO}),
    ClassListAction.REPLACE: frozenset({_CA, _CO}),
    ClassListAction.DELETE: frozenset({_CA, _CO}),
}


def class_list_roles(user: User, owner_id: uuid.UUID) -> frozenset[ClassListRole]:
    roles = {ClassListRole.OWNER if owner_id == user.id else ClassListRole.OTHER_TEACHER}
    if user.is_admin:
        roles.add(ClassListRole.ADMIN)
    return frozenset(roles)


def can_class_list(user: User | None, action: ClassListAction, owner_id: uuid.UUID) -> bool:
    if user is None or not user.is_active:
        return False
    return bool(class_list_roles(user, owner_id) & CLASS_LIST_MATRIX[action])


def can(user: User | None, action: SystemAction) -> bool:
    if user is None or not user.is_active:
        return False
    if action is SystemAction.CREATE_EXAM:
        return True
    return user.is_admin
