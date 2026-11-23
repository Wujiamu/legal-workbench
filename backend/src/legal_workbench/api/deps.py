"""Request guard, database/storage dependencies and the shared error envelope."""
from __future__ import annotations

import uuid
from collections.abc import Iterator

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_session_factory
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


DbSession = Depends(get_db)
Store = Depends(get_storage)
Guarded = Depends(guard)
