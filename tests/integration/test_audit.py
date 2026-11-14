from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from inleverboks.adapters.clock import FrozenClock
from inleverboks.db.models import AuditEvent
from inleverboks.db.models.audit import GENESIS_HASH
from inleverboks.services.audit import Actor
from inleverboks.services.container import Services


def _append_many(db: Session, services: Services, clock: FrozenClock, chain: str, n: int) -> None:
    for i in range(n):
        services.audit.append(
            db, "test.event", actor=Actor.system(), chain=chain, ip="10.0.0.1",
            details={"i": i, "naam": "Jan de Vries", "when": clock.now()},
        )  # fmt: skip
        clock.advance(seconds=1)
    db.commit()


def test_chain_links_and_verifies(db: Session, services: Services, clock: FrozenClock) -> None:
    exam_chain = str(uuid.uuid4())
    _append_many(db, services, clock, "system", 5)
    _append_many(db, services, clock, exam_chain, 3)
    events = list(db.query(AuditEvent).filter_by(chain=exam_chain).order_by(AuditEvent.seq))
    assert [e.seq for e in events] == [1, 2, 3]
    assert events[0].prev_hash == GENESIS_HASH
    assert events[1].prev_hash == events[0].hash
    assert services.audit.head_hash(db, exam_chain) == events[-1].hash
    checked, issues = services.audit.verify(db)
    assert checked == 8
    assert issues == []


def test_timestamps_are_utc_aware_after_reload(
    db: Session, services: Services, clock: FrozenClock
) -> None:
    clock.advance(timedelta(microseconds=123456))
    _append_many(db, services, clock, "system", 1)
    db.expire_all()
    event = db.query(AuditEvent).one()
    assert event.ts == clock.now() - timedelta(seconds=1)
    assert event.ts.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "statement", ["UPDATE audit_event SET event = 'x'", "DELETE FROM audit_event"]
)
def test_database_rejects_update_and_delete(
    db: Session, services: Services, clock: FrozenClock, statement: str
) -> None:
    _append_many(db, services, clock, "system", 1)
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(text(statement))
        db.commit()
    db.rollback()


def _tamper(db: Session, sql: str) -> None:
    """Bypass the append-only triggers like an attacker with database access would."""
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        db.execute(text("ALTER TABLE audit_event DISABLE TRIGGER USER"))
        db.execute(text(sql))
        db.execute(text("ALTER TABLE audit_event ENABLE TRIGGER USER"))
    else:
        db.execute(text("DROP TRIGGER audit_event_no_update"))
        db.execute(text("DROP TRIGGER audit_event_no_delete"))
        db.execute(text(sql))
    db.commit()


@pytest.mark.parametrize(
    ("sql", "problem"),
    [
        ("UPDATE audit_event SET details = '{\"i\": 99}' WHERE seq = 3", "hash_mismatch"),
        ("UPDATE audit_event SET actor_label = 'iemand' WHERE seq = 2", "hash_mismatch"),
        ("DELETE FROM audit_event WHERE seq = 2", "seq_gap"),
        (
            "UPDATE audit_event SET prev_hash = '" + "a" * 64 + "', hash = 'x' WHERE seq = 4",
            "broken_link",
        ),
    ],
)
def test_verify_detects_tampering(
    db: Session, services: Services, clock: FrozenClock, sql: str, problem: str
) -> None:
    _append_many(db, services, clock, "system", 5)
    _tamper(db, sql)
    db.expire_all()
    _checked, issues = services.audit.verify(db)
    assert len(issues) == 1
    assert issues[0].chain == "system"
    assert issues[0].problem == problem


def test_append_retries_on_sequence_conflict(
    db: Session, services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent writer commits seq=1 between our read of the (empty) chain head and
    our insert. The unique (chain, seq) constraint rejects our row; we retry as seq=2.

    On a non-empty chain PostgreSQL's FOR UPDATE makes the second writer wait instead,
    so the race is simulated on an empty chain, where there is no row to lock.
    """
    other = Session(bind=db.get_bind())
    original = services.audit.clock.now
    fired = {"done": False}

    def racing_now():  # type: ignore[no-untyped-def]
        if not fired["done"]:
            fired["done"] = True
            services.audit.append(other, "racer", actor=Actor.system())
            other.commit()
        return original()

    monkeypatch.setattr(services.audit.clock, "now", racing_now)
    event = services.audit.append(db, "mine", actor=Actor.system())
    db.commit()
    other.close()
    assert event.seq == 2
    checked, issues = services.audit.verify(db)
    assert checked == 2
    assert issues == []
