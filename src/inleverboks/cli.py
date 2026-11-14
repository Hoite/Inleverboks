"""Command line interface: ``inleverboks <command>``.

Available: version, migrate, create-admin, audit verify, retention run, cleanup run,
backup, restore and integrity check. ``demo-seed`` follows in phase 7.
"""

from __future__ import annotations

import secrets
import uuid
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy.orm import Session

from inleverboks import __version__
from inleverboks.adapters.storage import build_storage
from inleverboks.config import Settings, get_settings
from inleverboks.db import migrate as db_migrate
from inleverboks.db.models import User
from inleverboks.db.session import build_engine, build_sessionmaker
from inleverboks.services.audit import Actor
from inleverboks.services.backup import create_backup, prune_backups, restore_backup
from inleverboks.services.container import Services, build_services
from inleverboks.services.passwords import hash_password
from inleverboks.services.pv import RenderPv
from inleverboks.services.whitelist import is_valid_email, normalize_email

app = typer.Typer(
    name="inleverboks",
    help="Inleverboks command line tools.",
    no_args_is_help=True,
    add_completion=False,
)
audit_app = typer.Typer(help="Audit log tools.", no_args_is_help=True)
app.add_typer(audit_app, name="audit")
retention_app = typer.Typer(help="Retention period.", no_args_is_help=True)
app.add_typer(retention_app, name="retention")
cleanup_app = typer.Typer(help="Housekeeping.", no_args_is_help=True)
app.add_typer(cleanup_app, name="cleanup")
integrity_app = typer.Typer(help="Integrity of stored files.", no_args_is_help=True)
app.add_typer(integrity_app, name="integrity")


def _open() -> tuple[Settings, Services, Session]:
    settings = get_settings()
    services = build_services(settings, build_storage(settings))
    db = build_sessionmaker(build_engine(settings.database_url))()
    return settings, services, db


def _render_pv(settings: Settings) -> RenderPv:
    # Imported here: the web layer (templates, WeasyPrint) is only needed for these commands.
    from inleverboks.web.pdf import PdfRenderer  # noqa: PLC0415
    from inleverboks.web.routes.review import render_pv_factory  # noqa: PLC0415
    from inleverboks.web.templating import build_templates  # noqa: PLC0415

    return render_pv_factory(PdfRenderer(build_templates(settings).env))


@app.command()
def version() -> None:
    """Print the application version."""
    typer.echo(__version__)


@app.command()
def migrate(
    revision: Annotated[str, typer.Option(help="Target revision.")] = "head",
) -> None:
    """Apply database migrations."""
    settings = get_settings()
    db_migrate.upgrade(settings.database_url, revision)
    typer.echo(f"Database migrated to {revision}.")


@app.command("db-revision", hidden=True)
def db_revision(
    message: Annotated[str, typer.Argument(help="Short description of the change.")],
) -> None:
    """Autogenerate a new migration (development only)."""
    db_migrate.revision(get_settings().database_url, message)


@app.command("create-admin")
def create_admin(
    email: Annotated[str, typer.Option(help="E-mail address of the admin.")],
    name: Annotated[str, typer.Option(help="Display name.")] = "Admin",
    reset: Annotated[
        bool,
        typer.Option(
            "--reset",
            help="If the account exists: set a new password, clear TOTP and grant admin.",
        ),
    ] = False,
) -> None:
    """Create an admin account (or recover one with --reset).

    The password is generated and printed once; it must be changed at first login.
    The first admin created on an empty installation becomes the local admin.
    """
    _settings, services, db = _open()
    email = normalize_email(email)
    if not is_valid_email(email):
        typer.echo("Invalid e-mail address.", err=True)
        raise typer.Exit(code=2)
    password = secrets.token_urlsafe(18)
    with db:
        user = services.users.by_email(db, email)
        if user is not None and not reset:
            typer.echo("An account with this e-mail address exists. Use --reset.", err=True)
            raise typer.Exit(code=1)
        has_admin = db.query(User.id).filter(User.is_admin.is_(True)).first() is not None
        if user is None:
            user = User(email=email, name=name.strip() or "Admin", created_at=services.clock.now())
            db.add(user)
        user.password_hash = hash_password(password)
        user.is_admin = True
        user.is_active = True
        user.must_change_password = True
        user.totp_enabled = False
        user.totp_secret_enc = None
        user.totp_last_step = None
        for code in list(user.recovery_codes):
            db.delete(code)
        if not has_admin:
            user.is_local_admin = True
        db.flush()
        services.auth.revoke_all(db, user.id)
        services.audit.append(
            db,
            "user.admin_created_cli" if not reset else "user.admin_reset_cli",
            actor=Actor.system(),
            details={"user_id": str(user.id), "email": email, "local_admin": user.is_local_admin},
        )
        db.commit()
    typer.echo(f"Admin: {email}")
    typer.echo(f"Password (shown once, change at first login): {password}")


@audit_app.command("verify")
def audit_verify(
    chain: Annotated[
        str | None, typer.Option(help="Only this chain (exam id or 'system').")
    ] = None,
) -> None:
    """Recompute the hash chains. Exit code 1 when any chain is broken."""
    _settings, services, db = _open()
    with db:
        checked, issues = services.audit.verify(db, chain)
    if issues:
        for issue in issues:
            typer.echo(f"BROKEN chain={issue.chain} seq={issue.seq} problem={issue.problem}")
        typer.echo(f"{checked} events checked, {len(issues)} broken chain(s).")
        raise typer.Exit(code=1)
    typer.echo(f"OK: {checked} events checked, all chains intact.")


@retention_app.command("run")
def retention_run(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Only list due exams.")] = False,
) -> None:
    """Finalize exams whose retention period has passed (final PV, then delete files)."""
    settings, services, db = _open()
    with db:
        due = services.lifecycle.due_exams(db)
        for exam in due:
            typer.echo(f"due: {exam.slug} ({exam.id})")
        if dry_run:
            typer.echo(f"{len(due)} exam(s) due.")
            return
        result = services.lifecycle.run(db, _render_pv(settings))
    typer.echo(f"Finalized {len(result.finalized)} exam(s), {len(result.failed)} failed.")
    if result.failed:
        raise typer.Exit(code=1)


@cleanup_app.command("run")
def cleanup_run() -> None:
    """Delete orphaned objects and expired tokens."""
    _settings, services, db = _open()
    with db:
        result = services.maintenance.cleanup(db)
    typer.echo(
        f"Deleted {len(result.deleted_objects)} orphaned object(s) and {result.deleted_tokens} "
        f"expired token(s)."
    )


@app.command()
def backup(
    to: Annotated[Path, typer.Option("--to", help="Directory for the backup archive.")],
    keep_days: Annotated[int | None, typer.Option(help="Delete older backups.")] = None,
) -> None:
    """Back up the database (SQLite) and all stored files into a tar.gz."""
    settings, services, _db = _open()
    now = services.clock.now()
    result = create_backup(settings.database_url, services.storage, to, now)
    typer.echo(f"Backup written: {result.path} ({result.objects} files)")
    if not result.database_included:
        typer.echo("PostgreSQL: back up the database separately with pg_dump (see README).")
    removed = prune_backups(to, now, keep_days or settings.backup_keep_days)
    if removed:
        typer.echo(f"Deleted {len(removed)} old backup(s).")


@app.command()
def restore(
    archive: Annotated[Path, typer.Argument(help="Backup archive (.tar.gz).")],
    yes: Annotated[bool, typer.Option("--yes", help="Confirm: the app is stopped.")] = False,
) -> None:
    """Restore a backup. Stop the application first."""
    if not yes:
        typer.echo("Stop the application, then run again with --yes.", err=True)
        raise typer.Exit(code=2)
    settings, services, _db = _open()
    result = restore_backup(archive, settings.database_url, services.storage)
    typer.echo(f"Restored {result.objects} files; database restored: {result.database_restored}.")
    if not result.database_restored:
        typer.echo("PostgreSQL: restore the database with pg_restore (see README).")


@integrity_app.command("check")
def integrity_check(
    exam: Annotated[str | None, typer.Option(help="Only this exam (id).")] = None,
) -> None:
    """Re-hash all stored files and compare with the stored SHA-256."""
    _settings, services, db = _open()
    with db:
        report = services.maintenance.integrity_check(db, uuid.UUID(exam) if exam else None)
    for check in report.mismatches:
        typer.echo(f"MISMATCH {check.file.id} {check.file.original_name}")
    typer.echo(f"{report.checked} files checked, {len(report.mismatches)} mismatch(es).")
    if report.mismatches:
        raise typer.Exit(code=1)


if __name__ == "__main__":  # pragma: no cover
    app()
