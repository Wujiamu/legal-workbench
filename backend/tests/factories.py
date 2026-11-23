"""TEST-ONLY row factories for synthetic data. Never used with real case files."""
from __future__ import annotations

import hashlib
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from legal_workbench import models


def sha256_of(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def create_case(session: Session, title: str = "虚构测试案件", description: str | None = None) -> uuid.UUID:
    row = models.Case(id=uuid.uuid4(), title=title, description=description)
    session.add(row)
    session.flush()
    return row.id


def create_intake(session: Session, case_id: uuid.UUID, request_key: str | None = None,
                  source_label: str = "TEST-ONLY") -> uuid.UUID:
    row = models.Intake(id=uuid.uuid4(), case_id=case_id, request_key=request_key or uuid.uuid4().hex,
                        source_label=source_label, source_root="/tmp/test-only")
    session.add(row)
    session.flush()
    return row.id


def create_intake_item(session: Session, case_id: uuid.UUID, intake_id: uuid.UUID,
                       relative_source: str = "虚构/材料.txt") -> uuid.UUID:
    row = models.IntakeItem(id=uuid.uuid4(), case_id=case_id, intake_id=intake_id,
                            relative_source=relative_source, status="pending")
    session.add(row)
    session.flush()
    return row.id


def create_stored_object(session: Session, case_id: uuid.UUID, content: bytes = b"TEST-ONLY bytes",
                         relative_path: str | None = None) -> uuid.UUID:
    digest = sha256_of(content)
    row = models.StoredObject(
        id=uuid.uuid4(), case_id=case_id, sha256=digest, size=len(content),
        relative_path=relative_path or f"cases/{case_id}/originals/{digest}/source.txt",
    )
    session.add(row)
    session.flush()
    return row.id


def create_material(session: Session, case_id: uuid.UUID, intake_item_id: uuid.UUID,
                    display_name: str = "虚构材料.txt") -> uuid.UUID:
    row = models.Material(id=uuid.uuid4(), case_id=case_id, intake_item_id=intake_item_id,
                          display_name=display_name, category="待分类")
    session.add(row)
    session.flush()
    return row.id


def create_file_version(session: Session, case_id: uuid.UUID, material_id: uuid.UUID,
                        stored_object_id: uuid.UUID, kind: str = "original",
                        parent_version_id: uuid.UUID | None = None) -> uuid.UUID:
    row = models.FileVersion(
        id=uuid.uuid4(), case_id=case_id, material_id=material_id,
        stored_object_id=stored_object_id, kind=kind, parent_version_id=parent_version_id,
    )
    session.add(row)
    session.flush()
    if kind == "original":
        session.execute(
            text("UPDATE materials SET original_file_version_id = :fv WHERE id = :mid"),
            {"fv": row.id, "mid": material_id},
        )
    return row.id


def build_material(session: Session, *, case_id: uuid.UUID | None = None,
                   relative_source: str = "虚构/材料.txt") -> dict[str, Any]:
    """Create case -> intake -> item -> object -> material -> original version."""
    case_id = case_id or create_case(session)
    intake_id = create_intake(session, case_id)
    item_id = create_intake_item(session, case_id, intake_id, relative_source)
    object_id = create_stored_object(session, case_id)
    material_id = create_material(session, case_id, item_id)
    version_id = create_file_version(session, case_id, material_id, object_id)
    return {
        "case_id": case_id, "intake_id": intake_id, "intake_item_id": item_id,
        "stored_object_id": object_id, "material_id": material_id,
        "file_version_id": version_id,
    }


def build_material_with_storage(session: Session, storage, *, case_id: uuid.UUID | None = None,
                                content: bytes = b"TEST-ONLY content",
                                name: str = "原件.txt") -> dict[str, Any]:
    """Like build_material but publishes real bytes through managed storage."""
    case_id = case_id or create_case(session)
    intake_id = create_intake(session, case_id)
    item_id = create_intake_item(session, case_id, intake_id, f"虚构/{name}")
    published = storage.publish_original(case_id, storage.stage_bytes(content, name))
    obj = models.StoredObject(
        id=uuid.uuid4(), case_id=case_id, sha256=published.sha256, size=published.size,
        relative_path=published.relative_path,
    )
    session.add(obj)
    session.flush()
    material = models.Material(
        id=uuid.uuid4(), case_id=case_id, intake_item_id=item_id,
        display_name=name, category="待分类",
    )
    session.add(material)
    session.flush()
    version = models.FileVersion(
        id=uuid.uuid4(), case_id=case_id, material_id=material.id,
        stored_object_id=obj.id, kind="original",
    )
    session.add(version)
    session.flush()
    material.original_file_version_id = version.id
    session.execute(
        text("UPDATE intake_items SET status='ready', material_id=:m, stored_object_id=:o WHERE id=:i"),
        {"m": material.id, "o": obj.id, "i": item_id},
    )
    session.commit()
    return {
        "case_id": case_id, "intake_id": intake_id, "intake_item_id": item_id,
        "stored_object_id": obj.id, "material_id": material.id,
        "file_version_id": version.id, "relative_path": published.relative_path,
    }
