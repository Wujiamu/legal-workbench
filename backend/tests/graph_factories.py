"""TEST-ONLY fixtures for the graph/evidence tests (synthetic, no real case files)."""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from legal_workbench import models

from .factories import (build_material, create_file_version, create_intake,
                        create_intake_item, create_material, create_stored_object)


def build_segment(session: Session, *, case_id: uuid.UUID, material_id: uuid.UUID,
                  file_version_id: uuid.UUID, text: str = "TEST-ONLY 片段正文",
                  page: int = 1, unit_key: str | None = None) -> uuid.UUID:
    """Create a parse run/unit/segment so a source can point at a real ``source_ref``."""
    run = models.ParseRun(
        id=uuid.uuid4(), case_id=case_id, file_version_id=file_version_id,
        parser_version="test-only", sequence=0, status="complete",
    )
    session.add(run)
    session.flush()
    unit = models.ParseUnit(
        id=uuid.uuid4(), case_id=case_id, parse_run_id=run.id,
        unit_key=unit_key or f"pdf:{page}", status="extracted",
    )
    session.add(unit)
    session.flush()
    segment = models.Segment(
        id=uuid.uuid4(), case_id=case_id, parse_run_id=run.id, parse_unit_id=unit.id,
        locator={"type": "pdf", "page": page}, text=text, normalized_text=text,
    )
    session.add(segment)
    session.flush()
    return segment.id


def build_annotation(session: Session, *, case_id: uuid.UUID,
                     file_version_id: uuid.UUID, page: int = 1,
                     body: str = "TEST-ONLY 标注") -> uuid.UUID:
    row = models.Annotation(
        id=uuid.uuid4(), case_id=case_id, file_version_id=file_version_id,
        page=page, author="agent", kind="interpretation", body=body, revision=0,
    )
    session.add(row)
    session.flush()
    return row.id


def build_graph_case(session: Session) -> dict[str, Any]:
    """Case + two materials + segment + annotation, all TEST-ONLY."""
    first = build_material(session, relative_source="虚构/合同.txt")
    case_id = first["case_id"]
    # A second material with different bytes: stored objects are unique per case
    # by sha256, so reusing the default content would collide.
    intake_id = create_intake(session, case_id)
    item_id = create_intake_item(session, case_id, intake_id, "虚构/流水.txt")
    object_id = create_stored_object(session, case_id, content="TEST-ONLY 流水 bytes".encode())
    second_material_id = create_material(session, case_id, item_id, "虚构流水.txt")
    second_version_id = create_file_version(session, case_id, second_material_id, object_id)
    segment_id = build_segment(
        session, case_id=case_id, material_id=first["material_id"],
        file_version_id=first["file_version_id"], text="TEST-ONLY 股权转让协议第一条", page=2)
    annotation_id = build_annotation(
        session, case_id=case_id, file_version_id=first["file_version_id"], page=3)
    session.commit()
    return {
        **first,
        "case_id": case_id,
        "second_material_id": second_material_id,
        "second_file_version_id": second_version_id,
        "segment_id": segment_id,
        "annotation_id": annotation_id,
    }
