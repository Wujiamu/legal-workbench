"""0009_case_metadata migration, case metadata CAS, dashboard filter/sort, and dossier autofill tests.

TEST-ONLY synthetic data; no real case files.
"""
from __future__ import annotations

import io
import subprocess
import sys
import uuid
import zipfile
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from docx import Document as DocxDocument
from pypdf import PdfReader
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas as rl_canvas
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from legal_workbench import cases, dossier_export, evidence_catalog
from legal_workbench.config import load_settings
from legal_workbench.db import sqlalchemy_url
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.schemas import (
    AddEvidenceBindingInput, AddEvidenceGroupInput, AddEvidenceItemInput,
    ArchiveCaseInput, CaseMetadataInput, CaseScheduleInput, Deadline,
)

from .factories import build_material_with_storage

BACKEND_DIR = Path(__file__).resolve().parents[1]
DRILL_DB = "legal_workbench_migration_drill_0009"


def _urls() -> tuple[str, str]:
    settings = load_settings()
    migrator = make_url(settings.migration_url).set(database=DRILL_DB)
    app = make_url(settings.database_url).set(database=DRILL_DB)
    return (
        migrator.render_as_string(hide_password=False),
        app.render_as_string(hide_password=False),
    )


def _alembic(action: str, target: str, migrator_url: str, app_url: str) -> None:
    import os
    env = dict(os.environ, LWB_MIGRATION_URL=migrator_url, LWB_DATABASE_URL=app_url)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", action, target],
        cwd=BACKEND_DIR, env=env, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic {action} {target} failed: {result.stderr}")


def test_0009_migration_upgrade_and_downgrade():
    migrator_url, app_url = _urls()
    admin = make_url(migrator_url).set(database="postgres").render_as_string(hide_password=False)
    config = Config()
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    rev = ScriptDirectory.from_config(config).get_revision("0009_case_metadata")
    assert rev is not None and rev.down_revision == "0008_claim_amounts"

    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{DRILL_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{DRILL_DB}"')

    try:
        _alembic("upgrade", "0008_claim_amounts", migrator_url, app_url)
        legacy_id = uuid.uuid4()
        engine = create_engine(sqlalchemy_url(migrator_url), future=True)
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO cases (id, title, revision) VALUES (:id, 'TEST-ONLY 存量旧案', 0)"),
                {"id": str(legacy_id)},
            )
        engine.dispose()

        _alembic("upgrade", "0009_case_metadata", migrator_url, app_url)

        # App role (DML only) reads back legacy default and updates new columns
        with psycopg.connect(app_url) as conn:
            row = conn.execute(
                "SELECT case_number, plaintiff, defendant, work_status FROM cases WHERE id = %s",
                (str(legacy_id),),
            ).fetchone()
            assert row == (None, None, None, "normal")
            conn.execute(
                "UPDATE cases SET case_number = %s, plaintiff = %s, defendant = %s, "
                "work_status = 'need_materials' WHERE id = %s",
                ("(2026)沪0115民初1号", "甲", "乙", str(legacy_id)),
            )
            conn.commit()
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute("UPDATE cases SET work_status = 'bad_status' WHERE id = %s", (str(legacy_id),))
            conn.rollback()

        _alembic("downgrade", "0008_claim_amounts", migrator_url, app_url)
        engine = create_engine(sqlalchemy_url(migrator_url), future=True)
        with engine.begin() as conn:
            cols = {
                r[0] for r in conn.execute(text(
                    "SELECT column_name FROM information_schema.columns WHERE table_name='cases'"
                )).all()
            }
            assert not {"case_number", "plaintiff", "defendant", "work_status"} & cols
            assert conn.execute(text("SELECT count(*) FROM cases WHERE id = :id"), {"id": str(legacy_id)}).scalar_one() == 1
        engine.dispose()
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{DRILL_DB}" WITH (FORCE)')


def test_case_metadata_create_update_cas_and_archived_guard(db, client):
    created = client.post("/api/v1/cases", json={
        "title": "TEST-ONLY 买卖纠纷",
        "case_number": " (2026)沪0115民初100号 ",
        "plaintiff": "原告甲",
        "defendant": "被告乙",
        "work_status": "need_materials",
    })
    assert created.status_code == 200
    case_id = created.json()["case_id"]

    overview = client.get(f"/api/v1/cases/{case_id}").json()
    assert overview["case_number"] == "(2026)沪0115民初100号"
    assert overview["plaintiff"] == "原告甲"
    assert overview["defendant"] == "被告乙"
    assert overview["work_status"] == "need_materials"

    # Update via PATCH /metadata increments revision
    updated = client.patch(f"/api/v1/cases/{case_id}/metadata", json={
        "case_id": case_id,
        "expected_revision": overview["revision"],
        "case_number": "(2026)沪0115民初100号",
        "plaintiff": "原告甲公司",
        "defendant": "被告乙厂",
        "work_status": "need_sync",
    })
    assert updated.status_code == 200
    assert updated.json()["work_status"] == "need_sync"
    assert updated.json()["plaintiff"] == "原告甲公司"
    assert updated.json()["revision"] == overview["revision"] + 1

    # Stale revision -> 409 conflict
    stale = client.patch(f"/api/v1/cases/{case_id}/metadata", json={
        "case_id": case_id,
        "expected_revision": overview["revision"],
        "case_number": None,
        "plaintiff": None,
        "defendant": None,
        "work_status": "normal",
    })
    assert stale.status_code == 409

    # Archived case -> rejects metadata write
    cases.archive_case(db, ArchiveCaseInput(case_id=uuid.UUID(case_id), expected_revision=updated.json()["revision"]))
    archived_resp = client.patch(f"/api/v1/cases/{case_id}/metadata", json={
        "case_id": case_id,
        "expected_revision": updated.json()["revision"] + 1,
        "case_number": None,
        "plaintiff": None,
        "defendant": None,
        "work_status": "paused",
    })
    assert archived_resp.status_code == 422


def test_dashboard_and_archived_filter_and_sort(db, client):
    today = cases.shanghai_today()
    c1 = cases.create_case(
        db, title="Alpha 设备案", case_number="(2026)B-002",
        plaintiff="华辰公司", defendant="启航100%建工", work_status="need_materials",
    )
    c2 = cases.create_case(
        db, title="Beta 股权案", case_number="(2026)A-001",
        plaintiff="张三", defendant="李四", work_status="paused",
    )
    c3 = cases.create_case(
        db, title="Gamma 借款案", case_number=None,
        plaintiff="王五", defendant="赵六", work_status="normal",
    )
    # Give c1 an overdue deadline, c2 a 3-day upcoming deadline, c3 no deadline
    cases.update_schedule(db, CaseScheduleInput(
        case_id=c1.case_id, expected_revision=c1.revision,
        deadlines=[Deadline(title="补交发票", due=today - timedelta(days=2), kind="external")],
    ))
    cases.update_schedule(db, CaseScheduleInput(
        case_id=c2.case_id, expected_revision=c2.revision,
        deadlines=[Deadline(title="开庭", due=today + timedelta(days=3), kind="external")],
    ))

    # 1. Keyword search escapes '%' literally and matches defendant / case_number
    res_pct = client.get("/api/v1/dashboard", params={"q": "100%"})
    assert res_pct.status_code == 200
    assert [r["title"] for r in res_pct.json()["items"]] == ["Alpha 设备案"]

    res_num = client.get("/api/v1/dashboard", params={"q": "A-001"})
    assert [r["title"] for r in res_num.json()["items"]] == ["Beta 股权案"]

    # 2. Work status filter
    res_status = client.get("/api/v1/dashboard", params={"work_status": "paused"})
    assert [r["title"] for r in res_status.json()["items"]] == ["Beta 股权案"]

    # 3. Deadline filter: overdue vs due_soon
    assert [r["title"] for r in client.get("/api/v1/dashboard", params={"deadline_filter": "overdue"}).json()["items"]] == [
        "Alpha 设备案",
    ]
    assert [r["title"] for r in client.get("/api/v1/dashboard", params={"deadline_filter": "due_soon"}).json()["items"]] == [
        "Alpha 设备案", "Beta 股权案",
    ]

    # 4. Sort by case_number asc (nulls last) and work_status asc (need_materials < normal < paused)
    by_num = client.get("/api/v1/dashboard", params={"sort_by": "case_number", "sort_dir": "asc"}).json()["items"]
    assert [r["title"] for r in by_num] == ["Beta 股权案", "Alpha 设备案", "Gamma 借款案"]

    by_status = client.get("/api/v1/dashboard", params={"sort_by": "work_status", "sort_dir": "asc"}).json()["items"]
    assert [r["title"] for r in by_status] == ["Alpha 设备案", "Gamma 借款案", "Beta 股权案"]

    # 5. Archived list also supports filter/sort
    c3_latest = cases.get_case(db, c3.case_id)
    cases.archive_case(db, ArchiveCaseInput(case_id=c3.case_id, expected_revision=c3_latest.revision))
    arch = client.get("/api/v1/cases", params={"archived": "true", "q": "王五"}).json()["items"]
    assert len(arch) == 1
    assert arch[0]["title"] == "Gamma 借款案"
    assert arch[0]["plaintiff"] == "王五"


def test_dossier_export_prefills_case_number_and_parties(db, store):
    created = cases.create_case(
        db,
        title="TEST-ONLY 自动装卷案号测试",
        case_number="(2026)沪0115民初999号",
        plaintiff="上海甲公司",
        defendant="苏州乙公司",
        work_status="normal",
    )
    case_id = created.case_id
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    c.drawString(72, 720, "DOC_PAGE")
    c.showPage()
    c.save()
    mat = build_material_with_storage(db, store, case_id=case_id, content=buf.getvalue(), name="合同.pdf")
    db.commit()

    cat = evidence_catalog.add_group(
        db, AddEvidenceGroupInput(case_id=case_id, title="第一组"), actor="human", entry="local-web",
    )
    cat = evidence_catalog.add_item(
        db, AddEvidenceItemInput(case_id=case_id, group_id=cat.groups[0].id, title="采购合同"),
        actor="human", entry="local-web",
    )
    evidence_catalog.add_binding(
        db, AddEvidenceBindingInput(case_id=case_id, item_id=cat.items[0].id, material_id=mat["material_id"]),
        actor="human", entry="local-web",
    )

    bundle = dossier_export.export_evidence_bundle(db, store, case_id)
    with zipfile.ZipFile(io.BytesIO(bundle.data), "r") as zf:
        cover_text = PdfReader(io.BytesIO(zf.read("00_证据合订卷宗.pdf"))).pages[0].extract_text() or ""
        assert "(2026)沪0115民初999号" in cover_text
        assert "上海甲公司" in cover_text
        assert "苏州乙公司" in cover_text

        docx = DocxDocument(io.BytesIO(zf.read("01_证据目录.docx")))
        meta_text = "\n".join(p.text for p in docx.paragraphs)
        assert "(2026)沪0115民初999号" in meta_text
        assert "上海甲公司" in meta_text
        assert "苏州乙公司" in meta_text
