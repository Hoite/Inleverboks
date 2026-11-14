"""Append-only audit log with a SHA-256 hash chain.

Each event's hash covers the previous hash plus a canonical JSON encoding of all its
fields. ``verify`` recomputes every chain and reports the first broken link per chain.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from inleverboks.adapters.clock import Clock
from inleverboks.db.models import AuditEvent, User
from inleverboks.db.models.audit import GENESIS_HASH, SYSTEM_CHAIN

MAX_APPEND_RETRIES = 8


@dataclass(frozen=True)
class Actor:
    type: str  # user | student | system
    label: str
    id: str | None = None

    @classmethod
    def for_user(cls, user: User) -> Actor:
        return cls(type="user", label=user.email, id=str(user.id))

    @classmethod
    def student(cls, student_number: str) -> Actor:
        return cls(type="student", label=f"student {student_number}", id=student_number)

    @classmethod
    def anonymous(cls) -> Actor:
        return cls(type="anonymous", label="anoniem")

    @classmethod
    def system(cls) -> Actor:
        return cls(type="system", label="systeem")


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, set | frozenset):
        return json.dumps(sorted(value))
    msg = f"Not JSON serializable: {type(value).__name__}"
    raise TypeError(msg)


def normalize_details(details: dict[str, Any] | None) -> dict[str, Any]:
    """Round-trip through JSON so the stored value equals the hashed value."""
    result: dict[str, Any] = json.loads(json.dumps(details or {}, default=_json_default))
    return result


def canonical_bytes(fields: dict[str, Any]) -> bytes:
    return json.dumps(
        fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_json_default
    ).encode()


def compute_hash(event: AuditEvent) -> str:
    fields = {
        "chain": event.chain,
        "seq": event.seq,
        "ts": event.ts.isoformat(timespec="microseconds"),
        "actor_type": event.actor_type,
        "actor_id": event.actor_id,
        "actor_label": event.actor_label,
        "ip": event.ip,
        "event": event.event,
        "details": event.details,
    }
    return hashlib.sha256(event.prev_hash.encode() + canonical_bytes(fields)).hexdigest()


@dataclass(frozen=True)
class ChainIssue:
    chain: str
    seq: int
    problem: str  # hash_mismatch | broken_link | seq_gap


class AuditService:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    def append(
        self,
        db: Session,
        event: str,
        *,
        actor: Actor,
        chain: str | uuid.UUID = SYSTEM_CHAIN,
        ip: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """Add an event in the caller's transaction (the caller commits).

        Concurrent appenders to the same chain are serialized by the unique
        (chain, seq) constraint: on conflict the savepoint is rolled back and retried.
        """
        chain_key = str(chain)
        normalized = normalize_details(details)
        for _ in range(MAX_APPEND_RETRIES):
            last = db.execute(
                select(AuditEvent.seq, AuditEvent.hash)
                .where(AuditEvent.chain == chain_key)
                .order_by(AuditEvent.seq.desc())
                .limit(1)
                .with_for_update()
            ).first()
            seq, prev_hash = (last[0] + 1, last[1]) if last else (1, GENESIS_HASH)
            ts = self.clock.now()
            record = AuditEvent(
                chain=chain_key,
                seq=seq,
                ts=ts,
                actor_type=actor.type,
                actor_id=actor.id,
                actor_label=actor.label,
                ip=ip,
                event=event,
                details=normalized,
                prev_hash=prev_hash,
            )
            record.hash = compute_hash(record)
            try:
                with db.begin_nested():
                    db.add(record)
            except IntegrityError:
                continue
            return record
        msg = f"Could not append audit event to chain {chain_key}"
        raise RuntimeError(msg)

    def head_hash(self, db: Session, chain: str | uuid.UUID = SYSTEM_CHAIN) -> str:
        value = db.scalar(
            select(AuditEvent.hash)
            .where(AuditEvent.chain == str(chain))
            .order_by(AuditEvent.seq.desc())
            .limit(1)
        )
        return value or GENESIS_HASH

    def verify(self, db: Session, chain: str | None = None) -> tuple[int, list[ChainIssue]]:
        """Recompute all (or one) chains. Returns (events checked, issues)."""
        query = select(AuditEvent).order_by(AuditEvent.chain, AuditEvent.seq)
        if chain is not None:
            query = query.where(AuditEvent.chain == chain)
        issues: list[ChainIssue] = []
        checked = 0
        current_chain: str | None = None
        expected_prev = GENESIS_HASH
        expected_seq = 1
        broken: set[str] = set()
        for event in db.scalars(query).yield_per(500):
            checked += 1
            if event.chain != current_chain:
                current_chain, expected_prev, expected_seq = event.chain, GENESIS_HASH, 1
            if event.chain in broken:
                continue
            problem = None
            if event.seq != expected_seq:
                problem = "seq_gap"
            elif event.prev_hash != expected_prev:
                problem = "broken_link"
            elif compute_hash(event) != event.hash:
                problem = "hash_mismatch"
            if problem:
                issues.append(ChainIssue(chain=event.chain, seq=event.seq, problem=problem))
                broken.add(event.chain)
            expected_prev = event.hash
            expected_seq = event.seq + 1
        return checked, issues
