"""R02: a working copy must keep a parseable extension.

The browser used to send ``合同.docx（工作副本）``; ``Path.suffix`` then returned
``.docx（工作副本）``, which is non-empty, so no extension was restored and
``safe_extension`` turned the file into ``.bin``.  The copy registered fine while
unchanged (the stored object was reused), so the old test missed it.
"""
from __future__ import annotations

import io
import uuid

import pytest
from docx import Document
from openpyxl import Workbook
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from sqlalchemy import select

from legal_workbench import materials, models, parsing, worker
from legal_workbench.db import get_engine, get_session_factory
from legal_workbench.parsing import pipeline
from legal_workbench.schemas import SearchMode
from legal_workbench.storage import Storage

from . import factories


@pytest.fixture
def store(tmp_path) -> Storage:
    return Storage(root=tmp_path / "data")


def docx_bytes(body: str) -> bytes:
    document = Document()
    document.add_paragraph(body)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def xlsx_bytes(body: str) -> bytes:
    workbook = Workbook()
    workbook.active["A1"] = body
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def pdf_bytes(body: str) -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.setFont("Helvetica", 12)
    pdf.drawString(40, 700, body)
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def png_bytes(body: str, tmp_path) -> bytes:
    path = tmp_path / "img.png"
    Image.new("RGB", (60, 20), "white").save(path)
    return path.read_bytes()


@pytest.mark.parametrize(("display_name", "source_name", "factory", "expected"), [
    ("合同.docx", "合同.docx", lambda tmp: docx_bytes("TEST-ONLY 正文"), ".docx"),
    ("表.xlsx", "表.xlsx", lambda tmp: xlsx_bytes("TEST-ONLY 正文"), ".xlsx"),
    ("扫描.pdf", "扫描.pdf", lambda tmp: pdf_bytes("TEST-ONLY body"), ".pdf"),
    ("备注.txt", "备注.txt", lambda tmp: "TEST-ONLY 正文".encode(), ".txt"),
    # A renamed material: the display name lost the extension, but the stored
    # object still knows the real type.
    ("改名后", "原件.docx", lambda tmp: docx_bytes("TEST-ONLY 正文"), ".docx"),
    # A display name that already carries the bracket marker.
    ("a（工作副本）", "原件.pdf", lambda tmp: pdf_bytes("TEST-ONLY body"), ".pdf"),
    # Version dots are part of the name, not the extension.  Stripping ".docx"
    # and re-reading the suffix turned this into ".v2" and the copy could not be
    # parsed at all.
    ("合同.v2.docx", "合同.v2.docx", lambda tmp: docx_bytes("TEST-ONLY 正文"), ".docx"),
    ("证据 3.1 终稿.xlsx", "证据 3.1 终稿.xlsx",
     lambda tmp: xlsx_bytes("TEST-ONLY 正文"), ".xlsx"),
    ("报告 v1.0.0.pdf", "报告 v1.0.0.pdf", lambda tmp: pdf_bytes("TEST-ONLY body"), ".pdf"),
    ("记录.2024.01.txt", "记录.2024.01.txt",
     lambda tmp: "TEST-ONLY 正文".encode(), ".txt"),
    # A legal rename can leave a version number where an extension would be.
    # The real type still comes from the bytes, so the copy stays a DOCX.
    ("合同.v2", "合同.docx", lambda tmp: docx_bytes("TEST-ONLY 正文"), ".docx"),
    # Display name whose suffix contradicts the real format entirely.
    ("附件.txt", "附件.docx", lambda tmp: docx_bytes("TEST-ONLY 正文"), ".docx"),
    ("证据.pdf", "证据.xlsx", lambda tmp: xlsx_bytes("TEST-ONLY 正文"), ".xlsx"),
])
def test_working_copy_keeps_extension(store, tmp_path, display_name, source_name,
                                      factory, expected):
    db = get_session_factory()()
    try:
        built = factories.build_material_with_storage(
            db, store, content=factory(tmp_path), name=source_name)
        material = db.get(models.Material, built["material_id"])
        material.display_name = display_name
        db.commit()
        working_id, relative = materials.create_working_copy(
            db, store, case_id=built["case_id"],
            file_version_id=built["file_version_id"])
        assert relative.endswith(expected), relative
        assert ".bin" not in relative, relative
        row = db.get(models.WorkingCopy, working_id)
        assert row.display_name.endswith(expected), row.display_name
        assert "（工作副本）" in row.display_name
    finally:
        db.close()


def test_edited_working_copy_registers_and_parses(store):
    """The whole point: an edited copy must register a version that parses."""
    db = get_session_factory()()
    try:
        built = factories.build_material_with_storage(
            db, store, content=docx_bytes("TEST-ONLY 原始正文"), name="合同.docx")
    finally:
        db.close()

    db = get_session_factory()()
    try:
        working_id, relative = materials.create_working_copy(
            db, store, case_id=built["case_id"],
            file_version_id=built["file_version_id"])
    finally:
        db.close()

    # Simulate the user editing the copy through their own editor.
    path = store.managed_path(relative)
    path.write_bytes(docx_bytes("TEST-ONLY 修改后正文 独有关键词"))

    db = get_session_factory()()
    try:
        version_id, job_id = materials.register_file_version(
            db, store, case_id=built["case_id"], working_copy_id=working_id,
            expected_revision=0)
    finally:
        db.close()

    status = pipeline.process_parse_job(get_session_factory(), store, job_id)
    assert status == "complete", status

    db = get_session_factory()()
    try:
        version = db.get(models.FileVersion, version_id)
        assert version.kind == "working"
        assert version.stored_object_id != db.get(
            models.FileVersion, built["file_version_id"]).stored_object_id
        stored = db.get(models.StoredObject, version.stored_object_id)
        assert stored.relative_path.endswith(".docx"), stored.relative_path
        units = db.execute(select(models.ParseUnit)
                           .where(models.ParseUnit.parse_run_id == version.active_parse_run_id)
                           ).scalars().all()
        assert units and all(u.status == "extracted" for u in units)
    finally:
        db.close()


def test_original_bytes_unchanged_after_copy(store):
    db = get_session_factory()()
    try:
        built = factories.build_material_with_storage(
            db, store, content=docx_bytes("TEST-ONLY 原始正文"), name="合同.docx")
        original = db.get(models.FileVersion, built["file_version_id"])
        stored = db.get(models.StoredObject, original.stored_object_id)
        before = (stored.sha256, stored.size)
        working_id, relative = materials.create_working_copy(
            db, store, case_id=built["case_id"],
            file_version_id=built["file_version_id"])
    finally:
        db.close()

    path = store.managed_path(relative)
    path.write_bytes(docx_bytes("TEST-ONLY 修改后正文"))

    db = get_session_factory()()
    try:
        version_id, _job = materials.register_file_version(
            db, store, case_id=built["case_id"], working_copy_id=working_id,
            expected_revision=0)
        original = db.get(models.FileVersion, built["file_version_id"])
        stored = db.get(models.StoredObject, original.stored_object_id)
        assert (stored.sha256, stored.size) == before
        assert store.verify(stored.relative_path, stored.sha256, stored.size)
        assert db.get(models.FileVersion, version_id).kind == "working"
    finally:
        db.close()


def test_unchanged_copy_still_deduplicates(store):
    """Registering without editing must reuse the stored object, not duplicate it."""
    db = get_session_factory()()
    try:
        built = factories.build_material_with_storage(
            db, store, content=docx_bytes("TEST-ONLY 原始正文"), name="合同.docx")
        working_id, _relative = materials.create_working_copy(
            db, store, case_id=built["case_id"],
            file_version_id=built["file_version_id"])
        version_id, _job = materials.register_file_version(
            db, store, case_id=built["case_id"], working_copy_id=working_id,
            expected_revision=0)
        original = db.get(models.FileVersion, built["file_version_id"])
        copy = db.get(models.FileVersion, version_id)
        assert copy.stored_object_id == original.stored_object_id
    finally:
        db.close()


@pytest.mark.parametrize("display_name", ["改名后", "a（工作副本）"])
def test_renamed_material_full_flow(store, display_name):
    """R02 验收第 3 条：重命名过的材料要能走完整条流程，不只是扩展名对。

    Create copy -> edit bytes -> register -> parse -> working search finds the
    new text and originals does not.
    """
    from legal_workbench import search
    from legal_workbench.schemas import SearchMode, SearchScope

    db = get_session_factory()()
    try:
        built = factories.build_material_with_storage(
            db, store, content=docx_bytes("TEST-ONLY 原始正文"), name="原件.docx")
        db.get(models.Material, built["material_id"]).display_name = display_name
        db.commit()
        working_id, relative = materials.create_working_copy(
            db, store, case_id=built["case_id"],
            file_version_id=built["file_version_id"])
        # Index the original first: otherwise "originals still searchable" would
        # pass or fail for reasons unrelated to the working copy.
        _orig_run, orig_job = parsing.create_parse_run(
            db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    finally:
        db.close()

    pipeline.process_parse_job(get_session_factory(), store, orig_job)
    assert relative.endswith(".docx"), relative
    store.managed_path(relative).write_bytes(docx_bytes("TEST-ONLY 改名后修改 关键词戊戊戊"))

    db = get_session_factory()()
    try:
        version_id, job_id = materials.register_file_version(
            db, store, case_id=built["case_id"], working_copy_id=working_id,
            expected_revision=0)
    finally:
        db.close()

    status = pipeline.process_parse_job(get_session_factory(), store, job_id)
    assert status == "complete", status

    db = get_session_factory()()
    try:
        new_hits = search.search_case(
            db, case_id=built["case_id"], query="戊戊戊", mode=SearchMode.LITERAL,
            scope=SearchScope.WORKING)
        old_in_originals = search.search_case(
            db, case_id=built["case_id"], query="戊戊戊", mode=SearchMode.LITERAL,
            scope=SearchScope.ORIGINALS)
        original_hits = search.search_case(
            db, case_id=built["case_id"], query="原始正文", mode=SearchMode.LITERAL,
            scope=SearchScope.ORIGINALS)
        assert new_hits.total == 1, new_hits.total
        assert old_in_originals.total == 0, "原件不应包含副本新增内容"
        assert original_hits.total == 1, "原件内容必须仍可检索"
    finally:
        db.close()


@pytest.mark.parametrize("display_name", ["合同.v2.docx", "证据 3.1 终稿.xlsx"])
def test_multi_dot_name_full_flow(store, display_name):
    """R02 (round 2): a dotted name must survive copy, edit, register and parse.

    The unit test above checks the name; this checks the consequence the review
    found — the registered version parsed as ``unsupported_format``.
    """
    from legal_workbench import search
    from legal_workbench.schemas import SearchScope

    factory = docx_bytes if display_name.endswith(".docx") else xlsx_bytes
    db = get_session_factory()()
    try:
        built = factories.build_material_with_storage(
            db, store, content=factory("TEST-ONLY 原始正文"), name=display_name)
        working_id, relative = materials.create_working_copy(
            db, store, case_id=built["case_id"], file_version_id=built["file_version_id"])
        _orig_run, orig_job = parsing.create_parse_run(
            db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    finally:
        db.close()

    assert relative.endswith(display_name[-5:]) or relative.endswith(".xlsx") or \
        relative.endswith(".docx"), relative
    assert "（工作副本）" in relative
    # The version dot must still be there, and the real extension must follow it.
    assert relative.rsplit("（工作副本）", 1)[1].startswith("."), relative
    assert relative.rsplit("（工作副本）", 1)[1] in (".docx", ".xlsx"), relative

    pipeline.process_parse_job(get_session_factory(), store, orig_job)
    store.managed_path(relative).write_bytes(factory("TEST-ONLY 修改后 关键词子子子"))

    db = get_session_factory()()
    try:
        version_id, job_id = materials.register_file_version(
            db, store, case_id=built["case_id"], working_copy_id=working_id,
            expected_revision=0)
    finally:
        db.close()

    status = pipeline.process_parse_job(get_session_factory(), store, job_id)
    assert status == "complete", status

    db = get_session_factory()()
    try:
        new_hits = search.search_case(db, case_id=built["case_id"], query="子子子",
                                      mode=SearchMode.LITERAL, scope=SearchScope.WORKING)
        leaked = search.search_case(db, case_id=built["case_id"], query="子子子",
                                    mode=SearchMode.LITERAL, scope=SearchScope.ORIGINALS)
        original = search.search_case(db, case_id=built["case_id"], query="原始正文",
                                      mode=SearchMode.LITERAL, scope=SearchScope.ORIGINALS)
        assert new_hits.total == 1, new_hits.total
        assert leaked.total == 0, "原件不应包含副本新增内容"
        assert original.total == 1, "原件内容必须仍可检索"
        version = db.get(models.FileVersion, version_id)
        assert version.kind == "working"
    finally:
        db.close()


@pytest.mark.parametrize(("display_name", "source_name", "factory"), [
    ("合同.v2", "合同.docx", lambda: docx_bytes("TEST-ONLY 原始正文")),
    ("附件.txt", "附件.docx", lambda: docx_bytes("TEST-ONLY 原始正文")),
])
def test_name_without_real_extension_full_flow(store, display_name, source_name, factory):
    """A name ending in a version number (or a wrong extension) must still work.

    Review repro: rename ``合同.docx`` to ``合同.v2``, create a copy, edit it,
    register — the copy was ``合同.v2（工作副本）.v2`` and parsing failed with
    ``unsupported_format``.
    """
    from legal_workbench import search
    from legal_workbench.schemas import SearchScope

    db = get_session_factory()()
    try:
        built = factories.build_material_with_storage(
            db, store, content=factory(), name=source_name)
        db.get(models.Material, built["material_id"]).display_name = display_name
        db.commit()
        working_id, relative = materials.create_working_copy(
            db, store, case_id=built["case_id"],
            file_version_id=built["file_version_id"])
        _orig_run, orig_job = parsing.create_parse_run(
            db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    finally:
        db.close()

    pipeline.process_parse_job(get_session_factory(), store, orig_job)
    # The copy must carry the real extension, and the version number must survive.
    assert relative.endswith(".docx"), relative
    assert display_name in relative, (display_name, relative)

    store.managed_path(relative).write_bytes(docx_bytes("TEST-ONLY 修改后 关键词卯卯卯"))
    db = get_session_factory()()
    try:
        _version_id, job_id = materials.register_file_version(
            db, store, case_id=built["case_id"], working_copy_id=working_id,
            expected_revision=0)
    finally:
        db.close()

    status = pipeline.process_parse_job(get_session_factory(), store, job_id)
    assert status == "complete", status

    db = get_session_factory()()
    try:
        hits = search.search_case(db, case_id=built["case_id"], query="卯卯卯",
                                  mode=SearchMode.LITERAL, scope=SearchScope.WORKING)
        assert hits.total == 1, hits.total
    finally:
        db.close()
