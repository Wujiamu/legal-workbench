"""Single-worker execution: advisory worker lock, recovery and dispatch.

Lock order is fixed: maintenance shared lock (per write transaction) ->
worker session lock -> short job/object transactions.  Nothing acquires the
maintenance lock while holding the worker lock in reverse order.
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Callable

from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from . import imports, jobs, models
from .db import begin_write
from .errors import ErrorCode, WorkbenchError
from .storage import Storage

WORKER_LOCK_KEY = 0x4C57425F  # "LWB_"


def _session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def try_acquire_worker_lock(engine: Engine):
    """Return a connection holding the worker lock, or None when another worker owns it."""
    connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    acquired = connection.execute(text("SELECT pg_try_advisory_lock(:key)"),
                                  {"key": WORKER_LOCK_KEY}).scalar()
    if not acquired:
        connection.close()
        return None
    return connection


def release_worker_lock(connection) -> None:
    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": WORKER_LOCK_KEY})
    connection.close()


def recover_interrupted_jobs(session: Session) -> int:
    """Running jobs are requeued after a crash; completed units remain committed."""
    rows = session.execute(
        select(models.Job).where(models.Job.status == "running")
    ).scalars().all()
    for job in rows:
        job.status = "queued"
    session.commit()
    return len(rows)


def claim_next_job(session: Session) -> models.Job | None:
    job = session.execute(
        select(models.Job).where(models.Job.status == "queued")
        .order_by(models.Job.created_at, models.Job.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    ).scalars().first()
    if job is None:
        return None
    begin_write(session)
    job.status = "running"
    job.attempt += 1
    job.error = None
    session.commit()
    return job


def process_job(session_factory: sessionmaker[Session], storage: Storage, job: models.Job,
                fault_inject: Callable[[str], None] | None = None) -> str:
    if job.kind == "import":
        return imports.process_import_job(session_factory, storage, job.id,
                                          fault_inject=fault_inject).status
    if job.kind == "parse":
        from . import parsing
        return parsing.process_parse_job(session_factory, storage, job.id,
                                         fault_inject=fault_inject)
    session = session_factory()
    try:
        begin_write(session)
        job = session.get(models.Job, job.id)
        job.status = "failed"
        job.error = f"未知任务类型: {job.kind}"
        session.commit()
    finally:
        session.close()
    return "failed"


def run_worker(engine: Engine, storage: Storage, *, once: bool = True,
               poll_seconds: float = 0.5,
               fault_inject: Callable[[str], None] | None = None) -> int:
    """Process queued jobs while holding the exclusive worker lock.

    Returns the number of jobs processed.  A second worker returns 0 at once.
    """
    lock = try_acquire_worker_lock(engine)
    if lock is None:
        return 0
    factory = _session_factory(engine)
    processed = 0
    try:
        session = factory()
        try:
            recover_interrupted_jobs(session)
            # Terminal runs that never became the active version cannot be
            # reached by claiming a job: their job is already succeeded.
            from . import parsing
            parsing.repair_unpublished_runs(session)
        finally:
            session.close()
        while True:
            session = factory()
            try:
                job = claim_next_job(session)
            finally:
                session.close()
            if job is None:
                if once:
                    break
                time.sleep(poll_seconds)
                continue
            try:
                process_job(factory, storage, job, fault_inject=fault_inject)
            except WorkbenchError as exc:
                if exc.code == ErrorCode.MAINTENANCE:
                    break
                raise
            processed += 1
            if once:
                # Keep draining queued work in a single invocation.
                continue
    finally:
        release_worker_lock(lock)
    return processed


def main() -> int:
    import argparse
    from .config import load_settings
    from .db import get_engine

    parser = argparse.ArgumentParser(description="Legal Workbench single worker")
    parser.add_argument("--once", action="store_true", help="exit when the queue is empty")
    args = parser.parse_args()
    settings = load_settings()
    engine = get_engine()
    count = run_worker(engine, Storage(settings.data_root), once=args.once)
    print(f"processed_jobs={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
