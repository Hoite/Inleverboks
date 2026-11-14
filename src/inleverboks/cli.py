"""Command line interface: ``inleverboks <command>``.

Available: version, migrate, create-admin, audit verify. The remaining commands from
the spec (retention run, backup, restore, integrity check, demo-seed) are added in the
phase that implements their domain logic.
"""

from __future__ import annotations

import secrets
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
from inleverboks.services.container import Services, build_services
from inleverboks.services.passwords import hash_password
from inleverboks.services.whitelist import is_valid_email, normalize_email

app = typer.Typer(
    name="inleverboks",
    help="Inleverboks command line tools.",
    no_args_is_help=True,
    add_completion=False,
)
audit_app = typer.Typer(help="Audit log tools.", no_args_is_help=True)
app.add_typer(audit_app, name="audit")


def _open() -> tuple[Settings, Services, Session]:
    settings = get_settings()
    services = build_services(settings, build_storage(settings))
    db = build_sessionmaker(build_engine(settings.database_url))()
    return settings, services, db


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


if __name__ == "__main__":  # pragma: no cover
    app()
