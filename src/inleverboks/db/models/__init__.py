"""All ORM models, imported here so Alembic and the app see the full metadata."""

from inleverboks.db.models.audit import AuditEvent, DeniedAttemptBucket
from inleverboks.db.models.exams import (
    ClassList,
    ClassListEntry,
    Exam,
    ExamRosterEntry,
    ExamTemplate,
    ExamTemplateSlot,
    ExpectedFile,
    ExtensionGrant,
    RetiredSlug,
    TemplateFile,
)
from inleverboks.db.models.qualifications import CoreTask, Qualification, WorkProcess
from inleverboks.db.models.submissions import (
    ProcesVerbaal,
    ReceiptToken,
    Submission,
    SubmissionFile,
)
from inleverboks.db.models.system import (
    BrandingAsset,
    IpPreset,
    JobLock,
    Setting,
    WhitelistEntry,
)
from inleverboks.db.models.users import (
    Invite,
    PasswordReset,
    RecoveryCode,
    User,
    UserSession,
)

__all__ = [
    "AuditEvent",
    "BrandingAsset",
    "ClassList",
    "ClassListEntry",
    "CoreTask",
    "DeniedAttemptBucket",
    "Exam",
    "ExamRosterEntry",
    "ExamTemplate",
    "ExamTemplateSlot",
    "ExpectedFile",
    "ExtensionGrant",
    "Invite",
    "IpPreset",
    "JobLock",
    "PasswordReset",
    "ProcesVerbaal",
    "Qualification",
    "ReceiptToken",
    "RecoveryCode",
    "RetiredSlug",
    "Setting",
    "Submission",
    "SubmissionFile",
    "TemplateFile",
    "User",
    "UserSession",
    "WhitelistEntry",
    "WorkProcess",
]
