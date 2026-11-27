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
    TEAM_CONTACT = "team_contact"  # team contact of the exam's team (PLAN B68)
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
    MANAGE_TEAMS = "manage_teams"  # create teams, see all of them (PLAN B63)


_A, _O, _S, _C, _T = (
    ExamRole.ADMIN,
    ExamRole.OWNER,
    ExamRole.SECOND_REVIEWER,
    ExamRole.TEAM_CONTACT,
    ExamRole.OTHER_TEACHER,
)

EXAM_MATRIX: dict[ExamAction, frozenset[ExamRole]] = {
    ExamAction.EDIT_SETTINGS: frozenset({_O}),
    # PLAN B68: a team contact has the rights of an admin (B4) on the exams of his teams,
    # plus transferring them within the team (the service checks the new owner).
    ExamAction.VIEW_SUBMISSIONS: frozenset({_A, _O, _S, _C}),
    # PLAN B4: admins may download files of other teachers' exams.
    ExamAction.DOWNLOAD_FILES: frozenset({_A, _O, _S, _C}),
    ExamAction.EXPORT: frozenset({_A, _O, _S, _C}),
    ExamAction.VIEW_AUDIT: frozenset({_A, _O, _S, _C}),
    ExamAction.MANAGE_SUBMISSIONS: frozenset({_O}),
    ExamAction.SET_HOLD: frozenset({_A, _O, _C}),
    ExamAction.TRANSFER_OWNERSHIP: frozenset({_A, _C}),
    ExamAction.DELETE_FINALIZED: frozenset({_A}),
    ExamAction.DELETE_CONCEPT: frozenset({_O}),
    ExamAction.DUPLICATE: frozenset({_O}),
}


@dataclass(frozen=True)
class ExamRef:
    """The minimum the policy needs to know about an exam."""

    owner_id: uuid.UUID
    second_reviewer_id: uuid.UUID | None
    team_id: uuid.UUID | None = None


def exam_roles(user: User, exam: ExamRef, teams: TeamContext | None = None) -> frozenset[ExamRole]:
    """All roles a user holds on an exam. An admin who owns the exam is both; a team
    contact who is second reviewer too."""
    roles: set[ExamRole] = set()
    if exam.owner_id == user.id:
        roles.add(ExamRole.OWNER)
    elif exam.second_reviewer_id == user.id:
        roles.add(ExamRole.SECOND_REVIEWER)
    else:
        roles.add(ExamRole.OTHER_TEACHER)
    if teams is not None and exam.team_id is not None and exam.team_id in teams.contact_of:
        roles.add(ExamRole.TEAM_CONTACT)
    if user.is_admin:
        roles.add(ExamRole.ADMIN)
    return frozenset(roles)


def can_exam(
    user: User | None, action: ExamAction, exam: ExamRef, teams: TeamContext | None = None
) -> bool:
    """``teams`` is the user's team context; without it, team roles do not count."""
    if user is None or not user.is_active:
        return False
    return bool(exam_roles(user, exam, teams) & EXAM_MATRIX[action])


# Team context (PLAN B63) ------------------------------------------------------------


@dataclass(frozen=True)
class TeamContext:
    """The teams a user is a member of, and those where he is a team contact (TCP)."""

    member_of: frozenset[uuid.UUID] = frozenset()
    contact_of: frozenset[uuid.UUID] = frozenset()


# Class lists (PLAN B65, B69) ---------------------------------------------------------


class ClassListRole(StrEnum):
    ADMIN = "admin"  # admin, for any list
    CONTACT = "contact"  # team contact of the list's team
    MEMBER = "member"  # member of the list's team
    OUTSIDER = "outsider"  # a team list of a team the user is not in
    # A list without team (from before B65, not yet assigned): B60 still applies.
    UNASSIGNED_OWNER = "unassigned_owner"
    UNASSIGNED_OTHER = "unassigned_other"


class ClassListAction(StrEnum):
    VIEW = "view"  # library, detail page (names and student numbers)
    USE = "use"  # snapshot into an exam
    RENAME = "rename"
    REPLACE = "replace"  # import a new CSV over the entries
    DELETE = "delete"


_CA, _CC, _CM = ClassListRole.ADMIN, ClassListRole.CONTACT, ClassListRole.MEMBER
_CUO, _CUT = ClassListRole.UNASSIGNED_OWNER, ClassListRole.UNASSIGNED_OTHER

CLASS_LIST_MATRIX: dict[ClassListAction, frozenset[ClassListRole]] = {
    # PLAN B65: the members of the team see and use its lists; its team contacts and
    # admins change them. B69: lists without a team keep B60 until they are assigned.
    ClassListAction.VIEW: frozenset({_CA, _CC, _CM, _CUO, _CUT}),
    ClassListAction.USE: frozenset({_CA, _CC, _CM, _CUO, _CUT}),
    ClassListAction.RENAME: frozenset({_CA, _CC, _CUO}),
    ClassListAction.REPLACE: frozenset({_CA, _CC, _CUO}),
    ClassListAction.DELETE: frozenset({_CA, _CC, _CUO}),
}


@dataclass(frozen=True)
class ClassListRef:
    """The minimum the policy needs to know about a class list."""

    owner_id: uuid.UUID
    team_id: uuid.UUID | None


def class_list_roles(
    user: User, teams: TeamContext, class_list: ClassListRef
) -> frozenset[ClassListRole]:
    roles: set[ClassListRole] = set()
    if class_list.team_id is None:
        owner = class_list.owner_id == user.id
        roles.add(ClassListRole.UNASSIGNED_OWNER if owner else ClassListRole.UNASSIGNED_OTHER)
    elif class_list.team_id in teams.contact_of:
        roles.add(ClassListRole.CONTACT)
    elif class_list.team_id in teams.member_of:
        roles.add(ClassListRole.MEMBER)
    else:
        roles.add(ClassListRole.OUTSIDER)
    if user.is_admin:
        roles.add(ClassListRole.ADMIN)
    return frozenset(roles)


def can_class_list(
    user: User | None, teams: TeamContext, action: ClassListAction, class_list: ClassListRef
) -> bool:
    if user is None or not user.is_active:
        return False
    return bool(class_list_roles(user, teams, class_list) & CLASS_LIST_MATRIX[action])


# Exam templates (PLAN B53, B61, B66, B69) ------------------------------------------


class TemplateRole(StrEnum):
    OWNER = "owner"  # own personal template
    PERSONAL_ADMIN = "personal_admin"  # admin, someone else's personal template (B61)
    TEAM_CONTACT = "team_contact"  # team contact of the template's team
    TEAM_MEMBER = "team_member"
    TEAM_ADMIN = "team_admin"  # admin, a team template
    SHARED_ADMIN = "shared_admin"  # admin, a shared template not yet assigned (B53)
    SHARED_USER = "shared_user"  # any teacher, a shared template not yet assigned
    OUTSIDER = "outsider"


class TemplateAction(StrEnum):
    VIEW = "view"
    USE = "use"  # create an exam from it
    COPY = "copy"  # copy to one's own personal templates
    EDIT = "edit"  # settings, expected files, template files
    DELETE = "delete"


_PO, _PA = TemplateRole.OWNER, TemplateRole.PERSONAL_ADMIN
_PC, _PM, _PT = TemplateRole.TEAM_CONTACT, TemplateRole.TEAM_MEMBER, TemplateRole.TEAM_ADMIN
_PSA, _PSU = TemplateRole.SHARED_ADMIN, TemplateRole.SHARED_USER
_SEE = frozenset({_PO, _PA, _PC, _PM, _PT, _PSA, _PSU})
_MANAGE = frozenset({_PO, _PC, _PT, _PSA})

TEMPLATE_MATRIX: dict[TemplateAction, frozenset[TemplateRole]] = {
    TemplateAction.VIEW: _SEE,
    TemplateAction.USE: _SEE,
    TemplateAction.COPY: _SEE,
    # B61: an admin does not change someone else's personal template. B66: a team
    # template is managed by the team contacts of its team and by admins.
    TemplateAction.EDIT: _MANAGE,
    TemplateAction.DELETE: _MANAGE,
}


@dataclass(frozen=True)
class TemplateRef:
    owner_id: uuid.UUID
    team_id: uuid.UUID | None
    shared: bool


def template_roles(
    user: User, teams: TeamContext, template: TemplateRef
) -> frozenset[TemplateRole]:
    if template.team_id is not None:
        if template.team_id in teams.contact_of:
            role = TemplateRole.TEAM_CONTACT
        elif template.team_id in teams.member_of:
            role = TemplateRole.TEAM_MEMBER
        else:
            role = TemplateRole.TEAM_ADMIN if user.is_admin else TemplateRole.OUTSIDER
    elif template.shared:
        role = TemplateRole.SHARED_ADMIN if user.is_admin else TemplateRole.SHARED_USER
    elif template.owner_id == user.id:
        role = TemplateRole.OWNER
    else:
        role = TemplateRole.PERSONAL_ADMIN if user.is_admin else TemplateRole.OUTSIDER
    roles = {role}
    if user.is_admin and template.team_id is not None:
        roles.add(TemplateRole.TEAM_ADMIN)
    return frozenset(roles)


def can_template(
    user: User | None, teams: TeamContext, action: TemplateAction, template: TemplateRef
) -> bool:
    if user is None or not user.is_active:
        return False
    return bool(template_roles(user, teams, template) & TEMPLATE_MATRIX[action])


# Education teams (PLAN B63) ---------------------------------------------------------


class TeamRole(StrEnum):
    ADMIN = "admin"
    CONTACT = "contact"  # team contact ("TCP") of this team
    MEMBER = "member"
    OTHER_TEACHER = "other_teacher"


class TeamAction(StrEnum):
    VIEW = "view"  # team page: description, crebos, contacts and members
    ADD_MEMBER = "add_member"  # an existing account, never as contact
    REMOVE_MEMBER = "remove_member"  # not a contact: the service enforces that
    SET_CONTACT = "set_contact"
    EDIT = "edit"  # name and description
    MANAGE_CREBOS = "manage_crebos"
    DELETE = "delete"  # only when empty: the service enforces that
    MANAGE_CLASS_LISTS = "manage_class_lists"  # create lists for the team (B65)
    MANAGE_TEMPLATES = "manage_templates"  # create or copy templates into the team (B66)


_TA, _TC, _TM, _TT = TeamRole.ADMIN, TeamRole.CONTACT, TeamRole.MEMBER, TeamRole.OTHER_TEACHER

TEAM_MATRIX: dict[TeamAction, frozenset[TeamRole]] = {
    TeamAction.VIEW: frozenset({_TA, _TC, _TM}),
    TeamAction.ADD_MEMBER: frozenset({_TA, _TC}),
    TeamAction.REMOVE_MEMBER: frozenset({_TA, _TC}),
    TeamAction.SET_CONTACT: frozenset({_TA}),
    TeamAction.EDIT: frozenset({_TA}),
    TeamAction.MANAGE_CREBOS: frozenset({_TA}),
    TeamAction.DELETE: frozenset({_TA}),
    TeamAction.MANAGE_CLASS_LISTS: frozenset({_TA, _TC}),
    TeamAction.MANAGE_TEMPLATES: frozenset({_TA, _TC}),
}


def team_roles(user: User, teams: TeamContext, team_id: uuid.UUID) -> frozenset[TeamRole]:
    roles: set[TeamRole] = set()
    if team_id in teams.contact_of:
        roles.add(TeamRole.CONTACT)
    if team_id in teams.member_of:
        roles.add(TeamRole.MEMBER)
    if not roles:
        roles.add(TeamRole.OTHER_TEACHER)
    if user.is_admin:
        roles.add(TeamRole.ADMIN)
    return frozenset(roles)


def can_team(user: User | None, teams: TeamContext, action: TeamAction, team_id: uuid.UUID) -> bool:
    if user is None or not user.is_active:
        return False
    return bool(team_roles(user, teams, team_id) & TEAM_MATRIX[action])


def can(user: User | None, action: SystemAction) -> bool:
    if user is None or not user.is_active:
        return False
    if action is SystemAction.CREATE_EXAM:
        return True
    return user.is_admin
