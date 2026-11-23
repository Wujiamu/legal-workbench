"""R01: result-level warnings must reach coverage, not just unit-level ones.

A DOCX with an unextracted page header, or an XLSX with a chart, is reported as
``partial`` by its parser.  The pipeline used to forward only ``result.units``,
so the file-level warning was dropped and the material displayed as fully
covered.
"""
from __future__ import annotations

import io

import pytest
from docx import Document
from openpyxl import Workbook
from sqlalchemy import select

from legal_workbench import models, parsing
from legal_workbench.db import get_session_factory
from legal_workbench.parsing import pipeline
from legal_workbench.storage import Storage

from . import factories


@pytest.fixture
def store(tmp_path) -> Storage:
    return Storage(root=tmp_path / "data")


def build(db, store, content: bytes, name: str) -> dict:
    return factories.build_material_with_storage(db, store, content=content, name=name)


def run_parse(store, built):
    db = get_session_factory()()
    try:
        run_id, job_id = parsing.create_parse_run(
            db, case_id=built["case_id"], file_version_id=built["file_version_id"])
        status = pipeline.process_parse_job(get_session_factory(), store, job_id)
        run = db.get(models.ParseRun, run_id)
        return run_id, job_id, status, run
    finally:
        db.close()


def docx_with_header(body: str) -> bytes:
    document = Document()
    document.add_paragraph(body)
    section = document.sections[0]
    section.header.paragraphs[0].text = "页眉：虚构案件编号 TEST-ONLY"
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def docx_plain(body: str) -> bytes:
    document = Document()
    document.add_paragraph(body)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def xlsx_with_image(tmp_path) -> bytes:
    """A workbook carrying an embedded image, which the parser cannot read."""
    from PIL import Image
    from openpyxl.drawing.image import Image as XlImage

    png = tmp_path / "img.png"
    Image.new("RGB", (20, 20), "white").save(png)
    workbook = Workbook()
    sheet = workbook.active
    sheet["A1"] = "TEST-ONLY 表格正文"
    # openpyxl reads the image back from a path; a bare PIL object has no .fp.
    sheet.add_image(XlImage(str(png)), "C3")
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_docx_header_warning_reaches_run_coverage(store):
    """The parser says partial; the run must not claim complete."""
    built = build(store.root and get_session_factory()(), store,
                  docx_with_header("正文第一段"), "有页眉.docx")
    run_id, _job_id, status, run = run_parse(store, built)
    assert status == "partial", f"status={status} coverage={run.coverage}"
    assert run.coverage["partial"] >= 1
    assert run.coverage["warnings"], "结果级警告必须持久化到 ParseRun.coverage"


def test_plain_docx_still_complete(store):
    """A document with nothing unextracted must stay complete."""
    db = get_session_factory()()
    try:
        built = build(db, store, docx_plain("只有正文，没有页眉页脚"), "普通.docx")
    finally:
        db.close()
    run_id, _job_id, status, run = run_parse(store, built)
    assert status == "complete", f"coverage={run.coverage}"
    assert run.coverage["partial"] == 0


def test_docx_embedded_image_warning_reaches_run_coverage(store, tmp_path):
    """R01 验收第 2 条要求「含嵌入图片的 DOCX」也有覆盖提示，不只是页眉。"""
    from PIL import Image

    png = tmp_path / "pic.png"
    Image.new("RGB", (20, 20), "white").save(png)
    document = Document()
    document.add_paragraph("TEST-ONLY 正文")
    document.add_picture(str(png))
    buffer = io.BytesIO()
    document.save(buffer)

    db = get_session_factory()()
    try:
        built = build(db, store, buffer.getvalue(), "含图.docx")
    finally:
        db.close()
    run_id, _job_id, status, run = run_parse(store, built)
    assert status == "partial", f"status={status} coverage={run.coverage}"
    assert any("图片" in warning for warning in run.coverage["warnings"]), run.coverage["warnings"]


def test_xlsx_image_warning_reaches_run_coverage(store, tmp_path):
    db = get_session_factory()()
    try:
        built = build(db, store, xlsx_with_image(tmp_path), "含图.xlsx")
    finally:
        db.close()
    run_id, _job_id, status, run = run_parse(store, built)
    assert status == "partial", f"status={status} coverage={run.coverage}"
    assert run.coverage["warnings"]


def test_every_read_path_reports_the_same_coverage(store):
    """R01 验收第 4 条：get_index_status、检索 coverage、材料列表必须一致。

    Checking only ``coverage_for_version`` would leave the two surfaces the user
    actually looks at untested.
    """
    from legal_workbench import coverage as coverage_module
    from legal_workbench import materials, search
    from legal_workbench.schemas import SearchMode, SearchScope

    db = get_session_factory()()
    try:
        built = build(db, store, docx_with_header("正文"), "一致性.docx")
    finally:
        db.close()
    run_id, _job_id, status, run = run_parse(store, built)
    assert status == "partial"

    db = get_session_factory()()
    try:
        version = db.get(models.FileVersion, built["file_version_id"])
        via_version = coverage_module.coverage_for_version(db, version)
        via_index = search.get_index_status(db, case_id=built["case_id"])
        via_search = search.search_case(
            db, case_id=built["case_id"], query="正文", mode=SearchMode.LITERAL,
            scope=SearchScope.ORIGINALS).coverage
        items, _total, _case_total, _cats = materials.list_materials(
            db, case_id=built["case_id"])
        via_materials = items[0].coverage

        assert via_version.partial >= 1
        for name, value in (("index_status", via_index), ("search", via_search),
                            ("materials", via_materials)):
            assert value.partial == via_version.partial, (name, value, via_version)
            assert value.complete == via_version.complete, (name, value, via_version)
            assert value.failed == via_version.failed, (name, value, via_version)
    finally:
        db.close()
