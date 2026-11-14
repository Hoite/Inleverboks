"""Proces-verbaal (official report) of an exam (spec §7.3)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from inleverboks.adapters.clock import Clock
from inleverboks.db.models import Exam, ExtensionGrant, ProcesVerbaal, Submission, User
from inleverboks.services.audit import Actor, AuditService
from inleverboks.services.exams import ExamService
from inleverboks.services.exports import IntegrityReport
from inleverboks.services.overview import RowStatus, build_overview, load_submissions
from inleverboks.services.security import human_code


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} kB".replace(".", ",")
    return f"{size / 1048576:.1f} MB".replace(".", ",")


def new_document_id(now: datetime) -> str:
    return f"PV-{now:%Y%m%d}-{human_code(6)}"


class ProcesVerbaalService:
    def __init__(self, *, clock: Clock, audit: AuditService, exams: ExamService) -> None:
        self.clock = clock
        self.audit = audit
        self.exams = exams

    def context(
        self,
        db: Session,
        exam: Exam,
        *,
        generated_by: User | None,
        report: IntegrityReport,
        document_id: str,
        final: bool,
    ) -> dict[str, Any]:
        users = {u.id: u for u in db.scalars(select(User))}
        owner = users.get(exam.owner_id)
        reviewer = users.get(exam.second_reviewer_id) if exam.second_reviewer_id else None
        overview = build_overview(db, exam)
        submissions = load_submissions(db, exam)
        grants = list(db.scalars(select(ExtensionGrant).where(ExtensionGrant.exam_id == exam.id)))
        labels = {s.id: s.label for s in exam.expected_files}
        integrity = {check.file.id: check for check in report.checks}

        def file_rows(submission: Submission) -> list[dict[str, Any]]:
            return [
                {
                    "name": f.original_name,
                    "slot": labels.get(f.expected_file_id) if f.expected_file_id else None,
                    "size": f.size,
                    "size_human": human_size(f.size),
                    "sha256": f.sha256,
                    "warnings": list(f.warnings or []),
                    "integrity_ok": integrity[f.id].ok if f.id in integrity else None,
                    "deleted": f.storage_key is None,
                }
                for f in sorted(submission.files, key=lambda f: f.position)
            ]

        students = []
        for row in overview.rows:
            attempts = sorted(
                (s for s in submissions if s.student_number == row.student_number),
                key=lambda s: s.attempt,
            )
            current = row.submission if row.status is RowStatus.ACTIVE else None
            students.append(
                {
                    "row": row,
                    "current": current,
                    "files": file_rows(current) if current else [],
                    "earlier": [
                        {
                            "submission": s,
                            "changed_by": users.get(s.status_changed_by_id)
                            if s.status_changed_by_id
                            else None,
                            "files": file_rows(s),
                        }
                        for s in attempts
                        if s.status != "active"
                    ],
                }
            )

        particulars: list[dict[str, Any]] = []
        for s in submissions:
            if s.status in {"cancelled", "invalid"}:
                particulars.append(
                    {
                        "at": s.status_changed_at,
                        "kind": s.status,
                        "student": f"{s.last_name}, {s.first_name} ({s.student_number})",
                        "detail": s.status_reason or "",
                        "by": users[s.status_changed_by_id].name
                        if s.status_changed_by_id in users
                        else "",
                        "may_resubmit": s.may_resubmit,
                    }
                )
        for grant in grants:
            particulars.append(
                {
                    "at": grant.created_at,
                    "kind": "extension",
                    "student": grant.student_number,
                    "detail": grant.ends_at,
                    "by": users[grant.created_by_id].name if grant.created_by_id in users else "",
                }
            )
        if exam.hold:
            particulars.append(
                {
                    "at": None,
                    "kind": "hold",
                    "student": "",
                    "detail": exam.hold_reason or "",
                    "by": "",
                }
            )
        particulars.sort(key=lambda p: (p["at"] is None, p["at"] or self.clock.now()))

        counts = {
            "active": overview.counters.active,
            "cancelled": overview.counters.cancelled,
            "invalid": overview.counters.invalid,
            "missing": overview.counters.missing,
            "not_on_list": overview.counters.not_on_list,
            "roster": overview.counters.roster,
        }
        return {
            "exam": exam,
            "owner": owner,
            "reviewer": reviewer,
            "effective_end": self.exams.effective_end(db, exam),
            "expected_files": exam.expected_files,
            "counts": counts,
            "has_roster": overview.has_roster,
            "students": students,
            "particulars": particulars,
            "integrity": report,
            "generated_at": self.clock.now(),
            "generated_by": generated_by.name if generated_by else "systeem",
            "document_id": document_id,
            "audit_head_hash": self.audit.head_hash(db, exam.id),
            "final": final,
        }

    def record(
        self,
        db: Session,
        exam: Exam,
        *,
        user: User | None,
        document_id: str,
        storage_key: str,
        sha256: str,
        audit_head_hash: str,
        report: IntegrityReport,
        final: bool,
        ip: str | None,
    ) -> ProcesVerbaal:
        pv = ProcesVerbaal(
            id=uuid.uuid4(),
            exam_id=exam.id,
            document_id=document_id,
            storage_key=storage_key,
            sha256=sha256,
            is_final=final,
            generated_by_id=user.id if user else None,
            generated_at=self.clock.now(),
            audit_head_hash=audit_head_hash,
        )
        db.add(pv)
        actor = Actor.for_user(user) if user else Actor.system()
        for check in report.mismatches:
            self.audit.append(
                db, "integrity.mismatch", actor=actor, chain=exam.id, ip=ip,
                details={"file_id": str(check.file.id), "name": check.file.original_name,
                         "expected": check.file.sha256, "actual": check.actual_sha256},
            )  # fmt: skip
        for check in report.checks:
            check.file.integrity_checked_at = report.checked_at
            check.file.integrity_ok = check.ok
        self.audit.append(
            db, "export.pv", actor=actor, chain=exam.id, ip=ip,
            details={"document_id": document_id, "sha256": sha256, "final": final,
                     "audit_head_hash": audit_head_hash, "checked": report.checked,
                     "mismatches": len(report.mismatches)},
        )  # fmt: skip
        db.commit()
        return pv
