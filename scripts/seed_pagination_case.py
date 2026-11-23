"""Browser-acceptance data: a case with 120 materials across three categories.

R05 requires reaching material 51 and 101 in the real UI, and a category that
only appears on the last page.  All content is synthetic (TEST-ONLY / 虚构).
"""
from __future__ import annotations

import sys
import uuid

sys.path.insert(0, "backend/src")

from sqlalchemy import select

from legal_workbench import cases, models
from legal_workbench.config import load_settings
from legal_workbench.db import get_session_factory
from legal_workbench.storage import Storage

TOTAL = 120


def category_for(index: int) -> str:
    if index < 50:
        return "合同"
    if index < 100:
        return "证据"
    return "仅在末页"


def main() -> int:
    session = get_session_factory()()
    try:
        existing = session.execute(
            select(models.Case).where(models.Case.title == "TEST-ONLY 分页验收案件")
        ).scalars().first()
        if existing is not None:
            print(f"CASE_ID={existing.id}")
            return 0

        created = cases.create_case(session, title="TEST-ONLY 分页验收案件",
                                    description="仅用于分页与分类导航验收的虚构数据")
        case_id = created.case_id
        intake = models.Intake(
            id=uuid.uuid4(), case_id=case_id, source_label="虚构批量导入",
            source_root="/虚构/分页验收", request_key="r05-acceptance",
        )
        session.add(intake)
        session.flush()
        store = Storage(root=load_settings().data_root)
        for index in range(TOTAL):
            item = models.IntakeItem(
                id=uuid.uuid4(), case_id=case_id, intake_id=intake.id,
                relative_source=f"虚构/材料{index + 1}.txt",
                status="ready", error_code=None, error_message=None,
            )
            session.add(item)
            session.flush()
            material = models.Material(
                id=uuid.uuid4(), case_id=case_id, intake_item_id=item.id,
                display_name=f"虚构材料{index + 1}.txt",
                category=category_for(index),
            )
            session.add(material)
            session.flush()
            item.material_id = material.id

            # Real bytes, so the browser acceptance can actually open an
            # original for material 51 / 101 rather than only renaming it.
            body = f"TEST-ONLY 虚构材料{index + 1}\n分类：{category_for(index)}\n".encode()
            staged = store.stage_bytes(body, f"材料{index + 1}.txt")
            published = store.publish_original(case_id, staged)
            obj = models.StoredObject(
                id=uuid.uuid4(), case_id=case_id, sha256=published.sha256,
                size=published.size, relative_path=published.relative_path,
            )
            session.add(obj)
            session.flush()
            version = models.FileVersion(
                id=uuid.uuid4(), case_id=case_id, material_id=material.id,
                stored_object_id=obj.id, kind="original",
            )
            session.add(version)
            session.flush()
            material.original_file_version_id = version.id
            item.stored_object_id = obj.id
        session.commit()
        print(f"CASE_ID={case_id}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
