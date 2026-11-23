"""Database engine, session helpers and optimistic-concurrency primitives."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import load_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


class Base(DeclarativeBase):
    pass


def sqlalchemy_url(url: str) -> str:
    """SQLAlchemy needs the explicit psycopg3 dialect; plain psycopg.connect does not."""
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = load_settings()
        _engine = create_engine(
            sqlalchemy_url(settings.database_url),
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            future=True,
        )
    return _engine


def configure_engine(url: str) -> Engine:
    """Point the process at another database (tests, CLI tools)."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = create_engine(sqlalchemy_url(url), pool_pre_ping=True, pool_size=5, max_overflow=10, future=True)
    _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def reset_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Short transaction scope; callers must not hold it across parsing work."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def update_with_revision(
    session: Session,
    model: type[Base],
    object_id: Any,
    expected_revision: int,
    **values: Any,
) -> bool:
    """CAS update: succeeds only when the stored revision still matches.

    Returns False when zero rows were affected (stale revision or missing row).
    """
    begin_write(session)
    revision_column = getattr(model, "revision")
    statement = (
        update(model)
        .where(getattr(model, "id") == object_id, revision_column == expected_revision)
        .values(revision=revision_column + 1, **values)
    )
    return session.execute(statement).rowcount == 1


MAINTENANCE_KEY = 0x4C5742  # "LWB"


def assert_not_in_maintenance(session: Session) -> None:
    """Reject business writes while a backup holds the maintenance flag."""
    from .errors import ErrorCode, WorkbenchError

    active = session.execute(
        text("SELECT active FROM maintenance_state WHERE id = 1")
    ).scalar_one_or_none()
    if active:
        raise WorkbenchError(ErrorCode.MAINTENANCE, "系统正在维护（备份中），请稍后重试")


def begin_exclusive(session: Session) -> None:
    """Serialize failure cleanup with business writes and the backup snapshot."""
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": MAINTENANCE_KEY})


def begin_write(session: Session) -> None:
    """Enter the global shared write lock used by backup coordination.

    Ordering is fixed: maintenance flag check -> shared advisory lock ->
    short transaction.  The lock is transaction-scoped and released on
    commit/rollback.  Backup takes the exclusive session-level lock.
    """
    assert_not_in_maintenance(session)
    session.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": MAINTENANCE_KEY})
    # Re-check after acquiring the lock: a backup may have set the flag while
    # this transaction waited.
    assert_not_in_maintenance(session)
