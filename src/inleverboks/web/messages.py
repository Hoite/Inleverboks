"""Translated user-facing messages for domain error codes."""

from __future__ import annotations

from inleverboks.i18n import gettext as _
from inleverboks.services.errors import DomainError
from inleverboks.services.passwords import MIN_LENGTH, PasswordProblem


def error_messages() -> dict[str, str]:
    return {
        "invalid_email": _("Enter a valid e-mail address."),
        "not_whitelisted": _("This e-mail address is not on the whitelist."),
        "account_exists": _("An account with this e-mail address already exists."),
        "invite_invalid": _("This invitation link is invalid, already used or expired."),
        "reset_invalid": _("This reset link is invalid, already used or expired."),
        "invalid_name": _("Enter your name."),
        "weak_password": _("Choose a stronger password."),
        "wrong_password": _("The current password is incorrect."),
        "same_password": _("The new password must differ from the current one."),
        "password_mismatch": _("The passwords do not match."),
        "wrong_code": _("The code is incorrect. Try again."),
        "totp_already_enabled": _("Two-step verification is already enabled."),
        "totp_not_started": _("Start the setup of two-step verification first."),
        "totp_not_enabled": _("Two-step verification is not enabled."),
        "totp_required": _("Two-step verification is mandatory for your account."),
        "user_not_found": _("User not found."),
        "user_inactive": _("This account is deactivated."),
        "cannot_deactivate": _("This account cannot be deactivated."),
        "local_admin_protected": _("The admin rights of the local admin cannot be removed."),
        "last_admin": _("The last admin cannot lose admin rights."),
        "invalid_domain": _("Enter a valid domain, for example school.nl."),
        "duplicate": _("This entry already exists."),
        "not_found": _("Not found."),
        "forbidden": _("You do not have access to this page."),
        "requirements_locked": _(
            "Students have handed in. The file requirements can no longer be changed."
        ),
        "has_submissions": _("This is not possible: students have already handed in."),
        "not_concept": _("Only a concept can be deleted."),
        "exam_finalized": _("This exam is finalized and can no longer be changed."),
        "student_number_invalid": _("This student number has an invalid format."),
        "extension_not_later": _("The new end time must be after the end of the exam."),
        "owner_invalid": _("Choose an active teacher."),
        "template_too_large": _("The template file is larger than the maximum per file."),
        "class_list_not_found": _("Class list not found."),
        "team_not_found": _("Education team not found."),
        "invalid_team_name": _("Enter a name for the education team (at most 200 characters)."),
        "team_name_taken": _("An education team with this name already exists."),
        "description_too_long": _("The description is too long (at most 500 characters)."),
        "team_not_empty": _(
            "This education team still has class lists, templates or exams. Assign them to "
            "another team first."
        ),
        "already_member": _("This account is already a member of the team."),
        "contact_protected": _("Only an admin can remove a team contact from the team."),
        "unknown_crebo": _("Choose a crebo from the imported qualifications."),
        "template_not_found": _("Exam template not found."),
        "exam_not_found": _("Exam not found."),
        "not_active": _("Only an active hand-in can be undone or declared invalid."),
        "reason_required": _("Enter a reason."),
        "reason_too_long": _("The reason is too long."),
        "not_finalized": _("Only a finalized exam can be deleted permanently."),
        "no_students_selected": _("Choose at least one student for the resit."),
        "no_core_task_selected": _("Choose at least one core task."),
        "logo_missing": _("Choose a logo file."),
        "sbb_missing": _("Choose the SBB file (dossiers.xml, or a zip with that file)."),
        "sbb_invalid": _("This is not an SBB qualification file in XML format."),
        "sbb_empty": _("The file contains no adopted qualifications."),
        "logo_too_large": _("The logo is too large (at most 1 MB and 4 megapixels)."),
        "logo_format": _("The logo must be a PNG or WebP file."),
        "logo_invalid": _("This image cannot be read."),
        "datetime_invalid": _("Enter a valid date and time."),
        "datetime_nonexistent": _(
            "This time does not exist (the clock moves forward for summer time)."
        ),
    }


def password_problem_messages() -> dict[str, str]:
    return {
        PasswordProblem.TOO_SHORT: _("Use at least %(n)s characters.") % {"n": MIN_LENGTH},
        PasswordProblem.TOO_LONG: _("The password is too long."),
        PasswordProblem.TOO_COMMON: _("This password is too common."),
        PasswordProblem.CONTAINS_EMAIL: _("Do not use your e-mail address in the password."),
    }


def describe(error: DomainError) -> list[str]:
    """One or more messages for an error (password problems are listed separately)."""
    messages = [error_messages().get(error.code, error.code)]
    problems = error.params.get("problems")
    if isinstance(problems, list):
        details = password_problem_messages()
        messages += [details.get(p, str(p)) for p in problems]
    return messages
