"""Coverage reporting shared by material lists and case search.

A file version's coverage describes its *latest* parse run.  Only when the
version has never been parsed does it count as pending: an unsupported or failed
run is a real, reportable outcome and must not be disguised as "still waiting".
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from . import models
from .schemas import Coverage

_UNIT_TO_BUCKET = {
    "extracted": "complete",
    "ocr_done": "complete",
    "no_text_detected": "complete",
    "failed": "failed",
    "unsupported": "unsupported",
    "pending": "pending",
}


def _counts_from_units(session: Session, run_id: uuid.UUID) -> dict[str, int]:
    """Aggregate unit outcomes in one query.

    Loading full ParseUnit rows only to count statuses was the dominant cost when
    a case has many runs; the JSONB summary normally avoids this path entirely.
    """
    rows = session.execute(
        select(models.ParseUnit.status, func.count())
        .where(models.ParseUnit.parse_run_id == run_id)
        .group_by(models.ParseUnit.status)
    ).all()
    counts = {"complete": 0, "partial": 0, "failed": 0, "pending": 0, "unsupported": 0}
    for status, count in rows:
        counts[_UNIT_TO_BUCKET.get(status, "pending")] += count
    with_warnings = session.execute(
        select(func.count()).select_from(models.ParseUnit)
        .where(models.ParseUnit.parse_run_id == run_id,
               models.ParseUnit.warnings != sa_text("'[]'::jsonb"))
    ).scalar_one()
    counts["partial"] += with_warnings
    return counts


def latest_run(session: Session, version: models.FileVersion | None) -> models.ParseRun | None:
    """The active run, or the newest run when nothing was ever published."""
    if version is None:
        return None
    if version.active_parse_run_id is not None:
        return session.get(models.ParseRun, version.active_parse_run_id)
    return session.execute(
        select(models.ParseRun).where(models.ParseRun.file_version_id == version.id)
        .order_by(models.ParseRun.sequence.desc(), models.ParseRun.created_at.desc())
        .limit(1)
    ).scalars().first()


def run_coverage(session: Session, run: models.ParseRun | None) -> Coverage:
    if run is None:
        return Coverage(complete=0, partial=0, failed=0, pending=1, unsupported=0)
    stored = run.coverage or {}
    if stored.get("total"):
        counts = {key: int(stored.get(key, 0)) for key in
                  ("complete", "partial", "failed", "pending", "unsupported")}
    else:
        counts = _counts_from_units(session, run.id)
    if not any(counts.values()):
        # A run can end without producing units (unsupported container, reader
        # crash).  The run status is then the only truthful signal.
        bucket = {"unsupported": "unsupported", "failed": "failed",
                  "complete": "complete", "partial": "partial"}.get(run.status)
        counts[bucket or "pending"] = 1
    if run.status in ("queued", "running") and not counts["complete"]:
        # Nothing extracted yet: still waiting, not a terminal outcome.
        counts["pending"] = max(counts["pending"], 1)
    return Coverage(**counts)


def coverage_for_version(session: Session, version: models.FileVersion | None) -> Coverage:
    return run_coverage(session, latest_run(session, version))
