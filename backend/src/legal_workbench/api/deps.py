"""Request guard, database/storage dependencies and the shared error envelope."""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Callable

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from .. import audit
from ..config import load_settings
from ..db import get_session_factory
from ..errors import ErrorCode, WorkbenchError
from ..storage import Storage
from .security import check_local_request


def request_id(request: Request) -> uuid.UUID:
    existing = getattr(request.state, "request_id", None)
    return existing or uuid.uuid4()


async def guard(request: Request) -> None:
    check_local_request(request, request.app.state.allowed_hosts)
    audit.set_context(actor="local-web", request_id=request_id(request))


def get_db() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def get_storage(request: Request) -> Storage:
    storage = getattr(request.app.state, "storage", None)
    if storage is None:
        storage = Storage()
        request.app.state.storage = storage
    return storage


def require_module(*names: str) -> Callable[[], None]:
    """404 a route whose module is switched off in this installation.

    ``names`` are alternatives, not a conjunction: chart export needs a graph
    *or* a claim snapshot, so ``require_module("graph", "claims")`` opens the
    route when either is served.  Settings are read per request (``.env`` and
    the environment are the source of truth), which keeps a restart the only way
    to change what the server exposes.  Reporting ``not_found`` rather than
    ``forbidden`` is deliberate: a trimmed build has no such surface at all.
    """
    def dependency() -> None:
        enabled = load_settings().enabled_modules
        if any(name in enabled for name in names):
            return
        raise WorkbenchError(ErrorCode.NOT_FOUND,
                             f"模块未启用：{' / '.join(names)}",
                             {"modules": list(names)})

    return dependency


DbSession = Depends(get_db)
Store = Depends(get_storage)
Guarded = Depends(guard)
