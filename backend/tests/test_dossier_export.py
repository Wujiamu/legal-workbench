"""Chinese-court-compliant evidence dossier assembly (自动装卷) tests.

TEST-ONLY synthetic data; no real case files.
"""
from __future__ import annotations

import csv
import io
import json
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path

import pytest
from docx import Document as DocxDocument
from openpyxl import Workbook
from PIL import Image
from pypdf import PdfReader
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas as rl_canvas

from legal_workbench import cases, dossier_export, evidence_catalog, models
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.schemas import (
    AddEvidenceBindingInput, AddEvidenceGroupInput, AddEvidenceItemInput,
    ArchiveCaseInput, EvidenceBindingRevisionInput, EvidenceItemRevisionInput,
)

from .factories import build_material_with_storage, create_case


def _pdf_bytes(page_texts: list[str], *, landscape_pages: bool = False,
               rotate: int = 0) -> bytes:
    buf = io.BytesIO()
    sz = landscape(A4) if landscape_pages else A4
    c = rl_canvas.Canvas(buf, pagesize=sz)
    for txt in page_texts:
        c.setPageRotation(rotate)
        c.drawString(72, 720 if not landscape_pages else 480, txt)
        c.showPage()
    c.save()
    return buf.getvalue()


def _png_bytes(width: int = 400, height: int = 600, color: tuple[int, int, int] = (220, 235, 225)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def _exif_rotated_jpeg_bytes() -> bytes:
    buf = io.BytesIO()
    img = Image.new("RGB", (600, 300), (200, 210, 230))
    exif = Image.Exif()
    exif[274] = 6  # Orientation: rotate 90 CW -> 300x600 portrait after exif_transpose
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def _multi_frame_tiff_bytes() -> bytes:
    buf = io.BytesIO()
    f1 = Image.new("RGB", (300, 400), (240, 220, 220))
    f2 = Image.new("RGB", (300, 400), (220, 220, 240))
    f1.save(buf, format="TIFF", save_all=True, append_images=[f2])
    return buf.getvalue()


def _docx_bytes() -> bytes:
    buf = io.BytesIO()
    doc = DocxDocument()
    doc.add_paragraph("TEST-ONLY Complaint")
    doc.save(buf)
    return buf.getvalue()


def _xlsx_bytes() -> bytes:
    buf = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    ws.append(["Project", "Amount"])
    ws.append(["Sample", 100])
    wb.save(buf)
    return buf.getvalue()


def _build_full_dossier_case(db, store) -> uuid.UUID:
    case_id = create_case(db, title="虚构设备买卖纠纷案（一审）")
    db.commit()

    # Materials across formats
    m_contract = build_material_with_storage(
        db, store, case_id=case_id,
        content=_pdf_bytes(["CONTRACT_P1", "CONTRACT_P2", "CONTRACT_P3", "CONTRACT_P4"]),
        name="成套设备合同.pdf",
    )
    m_wide = build_material_with_storage(
        db, store, case_id=case_id,
        content=_pdf_bytes(["WIDE_CHART_P1"], landscape_pages=True, rotate=90),
        name="横向工程图.pdf",
    )
    m_png = build_material_with_storage(
        db, store, case_id=case_id, content=_png_bytes(), name="现场照片.png",
    )
    m_jpg = build_material_with_storage(
        db, store, case_id=case_id, content=_exif_rotated_jpeg_bytes(), name="EXIF照片.jpg",
    )
    m_tiff = build_material_with_storage(
        db, store, case_id=case_id, content=_multi_frame_tiff_bytes(), name="多页扫描.tiff",
    )
    m_docx = build_material_with_storage(
        db, store, case_id=case_id, content=_docx_bytes(), name="律师代理词.docx",
    )
    m_xlsx = build_material_with_storage(
        db, store, case_id=case_id, content=_xlsx_bytes(), name="对账清单.xlsx",
    )
    m_txt = build_material_with_storage(
        db, store, case_id=case_id, content="TEST-ONLY 备注文本".encode("utf-8"), name="说明.txt",
    )
    db.commit()

    # Group 1: Contract & PDF slice (2..3 -> 2 pages) + Landscape PDF (1 page)
    cat = evidence_catalog.add_group(
        db, AddEvidenceGroupInput(case_id=case_id, title="合同主体与效力"),
        actor="human", entry="local-web",
    )
    g1 = cat.groups[0].id
    cat = evidence_catalog.add_group(
        db, AddEvidenceGroupInput(case_id=case_id, title="履行与电子文档"),
        actor="human", entry="local-web",
    )
    g2 = cat.groups[1].id

    cat = evidence_catalog.add_item(
        db, AddEvidenceItemInput(
            case_id=case_id, group_id=g1, title="设备采购合同（节选）",
            purpose="证明买卖合同成立及付款条款",
        ),
        actor="human", entry="local-web",
    )
    i11 = next(i.id for i in cat.items if i.title == "设备采购合同（节选）")
    evidence_catalog.add_binding(
        db, AddEvidenceBindingInput(
            case_id=case_id, item_id=i11, material_id=m_contract["material_id"],
            page_start=2, page_end=3,
        ),
        actor="human", entry="local-web",
    )
    # Also attach a pending binding that MUST be ignored
    evidence_catalog.add_binding(
        db, AddEvidenceBindingInput(
            case_id=case_id, item_id=i11, material_id=m_wide["material_id"],
            page_start=1, page_end=1, request_key="pending-bind-1",
        ),
        actor="agent", entry="cli",
    )

    cat = evidence_catalog.add_item(
        db, AddEvidenceItemInput(
            case_id=case_id, group_id=g1, title="设备安装横向图纸",
            purpose="证明交付规格要求",
        ),
        actor="human", entry="local-web",
    )
    i12 = next(i.id for i in cat.items if i.title == "设备安装横向图纸")
    evidence_catalog.add_binding(
        db, AddEvidenceBindingInput(
            case_id=case_id, item_id=i12, material_id=m_wide["material_id"],
        ),
        actor="human", entry="local-web",
    )

    # Pending item in group 1 -> MUST be excluded from bundle
    evidence_catalog.add_item(
        db, AddEvidenceItemInput(
            case_id=case_id, group_id=g1, title="待确认草稿证据",
            purpose="不应进入卷宗", request_key="pending-item-1",
        ),
        actor="agent", entry="cli",
    )

    # Removed item in group 1 -> MUST be excluded
    cat = evidence_catalog.add_item(
        db, AddEvidenceItemInput(case_id=case_id, group_id=g1, title="已删除证据"),
        actor="human", entry="local-web",
    )
    removed_item = next(i for i in cat.items if i.title == "已删除证据")
    evidence_catalog.remove_item(
        db, EvidenceItemRevisionInput(
            case_id=case_id, item_id=removed_item.id, expected_revision=removed_item.revision,
        ),
        actor="human", entry="local-web",
    )

    # Group 2: multi-images (1 + 1 + 2 = 4 pages), DOCX+XLSX+TXT notice pages (3 pages), unbound item (1 page)
    cat = evidence_catalog.add_item(
        db, AddEvidenceItemInput(
            case_id=case_id, group_id=g2, title="现场照片与多页扫描件",
            purpose="证明交货验收情况",
        ),
        actor="human", entry="local-web",
    )
    i21 = next(i.id for i in cat.items if i.title == "现场照片与多页扫描件")
    for mid in (m_png["material_id"], m_jpg["material_id"], m_tiff["material_id"]):
        evidence_catalog.add_binding(
            db, AddEvidenceBindingInput(case_id=case_id, item_id=i21, material_id=mid),
            actor="human", entry="local-web",
        )

    cat = evidence_catalog.add_item(
        db, AddEvidenceItemInput(
            case_id=case_id, group_id=g2, title="电子辅助材料/汇总:清单?",
            purpose="提供可编辑电子文档原件",
        ),
        actor="human", entry="local-web",
    )
    i22 = next(i.id for i in cat.items if "电子辅助材料" in i.title)
    for mid in (m_docx["material_id"], m_xlsx["material_id"], m_txt["material_id"]):
        evidence_catalog.add_binding(
            db, AddEvidenceBindingInput(case_id=case_id, item_id=i22, material_id=mid),
            actor="human", entry="local-web",
        )

    evidence_catalog.add_item(
        db, AddEvidenceItemInput(
            case_id=case_id, group_id=g2, title="待补原件条目",
            purpose="测试未绑定占位页",
        ),
        actor="human", entry="local-web",
    )
    return case_id


def test_bundle_complete_layout_pages_docx_csv_and_splits(db, store):
    case_id = _build_full_dossier_case(db, store)
    result = dossier_export.export_evidence_bundle(db, store, case_id)

    assert result.filename.endswith(".zip")
    assert all(ch not in result.filename for ch in '<>:"|?*\\/')
    # 5 confirmed items: 2 + 1 + 4 + 3 + 1 = 11 body pages
    assert result.summary["group_count"] == 2
    assert result.summary["item_count"] == 5
    assert result.summary["body_page_count"] == 11

    with zipfile.ZipFile(io.BytesIO(result.data), "r") as zf:
        names = set(zf.namelist())
        assert "00_装卷说明.txt" in names
        assert "00_证据合订卷宗.pdf" in names
        assert "01_证据目录.docx" in names
        assert "01_证据目录.csv" in names

        split_names = sorted(n for n in names if n.startswith("02_分项证据材料/"))
        # 证据1-1 (.pdf), 证据1-2 (.pdf), 证据2-1 (.pdf), 证据2-2 (.docx, .xlsx, .txt) -> 6 files
        assert len(split_names) == 6
        assert any(n.startswith("02_分项证据材料/证据2-2_") and n.endswith(".docx") for n in split_names)
        for sname in split_names:
            leaf = sname.split("/", 1)[1]
            assert all(ch not in leaf for ch in '<>:"|?*\\')

        # Check sliced PDF in 02_分项证据材料/ has exact 2 pages (pages 2..3)
        pdf11_name = next(n for n in split_names if n.startswith("02_分项证据材料/证据1-1_") and n.endswith(".pdf"))
        pdf11 = PdfReader(io.BytesIO(zf.read(pdf11_name)))
        assert len(pdf11.pages) == 2
        assert pdf11.metadata is not None
        assert str(pdf11.metadata.get("/Creator", "")).startswith("PlainFolio (素章)")
        assert str(pdf11.metadata.get("/Producer", "")).startswith("PlainFolio (素章)")
        assert "证据1-1" in str(pdf11.metadata.get("/Title", ""))
        assert str(pdf11.metadata.get("/CreationDate", "")).startswith("D:")
        text11 = "\n".join(p.extract_text() or "" for p in pdf11.pages)
        assert "CONTRACT_P2" in text11
        assert "CONTRACT_P3" in text11
        assert "CONTRACT_P1" not in text11
        assert "CONTRACT_P4" not in text11

        # Check image item split PDF has 4 pages (png + exif jpeg + 2-frame tiff)
        pdf21_name = next(n for n in split_names if n.startswith("02_分项证据材料/证据2-1_") and n.endswith(".pdf"))
        assert len(PdfReader(io.BytesIO(zf.read(pdf21_name))).pages) == 4

        # Check merged PDF structure: 1 cover + 1 toc + 5 dividers + 11 body = 18 pages
        merged_pdf = PdfReader(io.BytesIO(zf.read("00_证据合订卷宗.pdf")))
        assert len(merged_pdf.pages) == result.summary["total_pdf_pages"]
        assert len(merged_pdf.pages) >= 18
        assert len(merged_pdf.outline) >= 4  # 封面, 证据目录, 第1组, 第2组
        assert merged_pdf.metadata is not None
        assert str(merged_pdf.metadata.get("/Creator", "")).startswith("PlainFolio (素章)")
        assert str(merged_pdf.metadata.get("/Producer", "")).startswith("PlainFolio (素章)")
        assert "虚构设备买卖纠纷案（一审）" in str(merged_pdf.metadata.get("/Title", ""))
        assert str(merged_pdf.metadata.get("/CreationDate", "")).startswith("D:")

        # Verify A4 dimensions on all pages (either portrait or landscape)
        for p in merged_pdf.pages:
            w, h = float(p.mediabox.width), float(p.mediabox.height)
            is_portrait = abs(w - 595.28) < 2 and abs(h - 841.89) < 2
            is_landscape = abs(w - 841.89) < 2 and abs(h - 595.28) < 2
            assert is_portrait or is_landscape

        all_pdf_text = "\n".join((p.extract_text() or "") for p in merged_pdf.pages)
        assert "待确认草稿证据" not in all_pdf_text
        assert "已删除证据" not in all_pdf_text
        assert "WIDE_CHART_P1" in all_pdf_text
        for num in range(1, 12):
            assert f"第 {num} 页" in all_pdf_text

        # Check CSV BOM + exact page ranges + exclusion of pending/removed
        raw_csv = zf.read("01_证据目录.csv")
        assert raw_csv.startswith(b"\xef\xbb\xbf")
        rows = list(csv.reader(io.StringIO(raw_csv.decode("utf-8-sig"))))
        codes = [r[2] for r in rows[1:]]
        assert codes == ["证据1-1", "证据1-2", "证据2-1", "证据2-2", "证据2-3"]
        page_ranges = [r[5] for r in rows[1:]]
        assert page_ranges == ["第 1-2 页", "第 3 页", "第 4-7 页", "第 8-10 页", "第 11 页"]

        # Check DOCX catalog matches exact table rows and signature block
        raw_docx = zf.read("01_证据目录.docx")
        docx = DocxDocument(io.BytesIO(raw_docx))
        assert docx.core_properties.author == "PlainFolio (素章)"
        assert "PlainFolio (素章)" in (docx.core_properties.comments or "")
        assert "虚构设备买卖纠纷案（一审）" in (docx.core_properties.title or "")
        assert docx.core_properties.created is not None and docx.core_properties.created.year >= 2025
        with zipfile.ZipFile(io.BytesIO(raw_docx), "r") as docx_zf:
            app_xml = docx_zf.read("docProps/app.xml").decode("utf-8")
            assert "PlainFolio (素章)" in app_xml
            assert "Microsoft Macintosh Word" not in app_xml
        assert len(docx.tables) == 1
        table_text = "\n".join(cell.text for row in docx.tables[0].rows for cell in row.cells)
        assert "证据1-1" in table_text
        assert "第 1-2 页" in table_text
        assert "第 8-10 页" in table_text
        para_text = "\n".join(p.text for p in docx.paragraphs)
        assert "提交人（签字/盖章）" in para_text

        # Original non-PDF electronic material MUST remain untouched (original bytes preserved)
        orig_docx_name = next(n for n in split_names if n.startswith("02_分项证据材料/证据2-2_") and n.endswith(".docx"))
        orig_docx = DocxDocument(io.BytesIO(zf.read(orig_docx_name)))
        assert any("TEST-ONLY Complaint" in p.text for p in orig_docx.paragraphs)
        assert orig_docx.core_properties.author != "PlainFolio (素章)"


def test_bundle_http_endpoint_active_archived_and_errors(db, client):
    # 1. Empty confirmed catalog -> 422 invalid_input with user-facing message
    empty_case = create_case(db, title="空目录测试案")
    db.commit()
    res_empty = client.get(f"/api/v1/cases/{empty_case}/evidence-catalog/bundle")
    assert res_empty.status_code == 422
    assert res_empty.json()["code"] == "invalid_input"
    assert "暂无已确认的证据条目" in res_empty.json()["message"]

    # 2. Missing case -> 404
    res_missing = client.get(f"/api/v1/cases/{uuid.uuid4()}/evidence-catalog/bundle")
    assert res_missing.status_code == 404

    # 3. Archived case -> read-only bundle succeeds with ZIP headers
    from legal_workbench.app import app
    case_id = _build_full_dossier_case(db, app.state.storage)
    cases.archive_case(db, ArchiveCaseInput(case_id=case_id, expected_revision=0))
    res_ok = client.get(f"/api/v1/cases/{case_id}/evidence-catalog/bundle")
    assert res_ok.status_code == 200
    assert res_ok.headers["content-type"].startswith("application/zip")
    assert res_ok.headers["x-content-type-options"] == "nosniff"
    assert "filename*=UTF-8''" in res_ok.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(res_ok.content), "r") as zf:
        assert "00_证据合订卷宗.pdf" in zf.namelist()


def test_bundle_rejects_tampered_storage_integrity(db, store):
    case_id = create_case(db, title="完整性校验测试案")
    mat = build_material_with_storage(
        db, store, case_id=case_id, content=_pdf_bytes(["SECRET"]), name="原始文件.pdf",
    )
    cat = evidence_catalog.add_group(
        db, AddEvidenceGroupInput(case_id=case_id, title="第一组"),
        actor="human", entry="local-web",
    )
    cat = evidence_catalog.add_item(
        db, AddEvidenceItemInput(case_id=case_id, group_id=cat.groups[0].id, title="证据A"),
        actor="human", entry="local-web",
    )
    evidence_catalog.add_binding(
        db, AddEvidenceBindingInput(case_id=case_id, item_id=cat.items[0].id, material_id=mat["material_id"]),
        actor="human", entry="local-web",
    )
    # Tamper stored bytes on disk
    stored = db.get(models.StoredObject, mat["stored_object_id"])
    assert stored is not None
    store.managed_path(stored.relative_path).write_bytes(b"TAMPERED")
    db.commit()

    with pytest.raises(WorkbenchError) as exc:
        dossier_export.export_evidence_bundle(db, store, case_id)
    assert exc.value.code == ErrorCode.INTEGRITY_ERROR


def test_bundle_cli_command(db, store, tmp_path, app_database_url, monkeypatch):
    case_id = _build_full_dossier_case(db, store)
    out_zip = tmp_path / "cli_out" / "dossier.zip"
    env = {
        "PATH": "/usr/bin:/bin",
        "LWB_DATABASE_URL": app_database_url,
        "LWB_DATA_ROOT": str(store.root),
    }
    res = subprocess.run(
        [sys.executable, "-m", "legal_workbench.cli", "evidence-export-bundle",
         "--case-id", str(case_id), "--output", str(out_zip)],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout)
    assert payload["item_count"] == 5
    assert payload["body_page_count"] == 11
    assert out_zip.is_file()
    with zipfile.ZipFile(out_zip, "r") as zf:
        assert "01_证据目录.docx" in zf.namelist()


def test_bundle_handles_empty_password_pdf_and_corrupt_image(db, store):
    from pypdf import PdfWriter
    case_id = create_case(db, title="容错/特殊文件测试案")

    # Owner-password-only PDF (empty user password -> decrypt("") succeeds)
    w = PdfWriter()
    r_plain = PdfReader(io.BytesIO(_pdf_bytes(["OWNER_ENC_TEXT"])))
    w.add_page(r_plain.pages[0])
    w.encrypt(user_password="", owner_password="secret-owner-pw")
    enc_buf = io.BytesIO()
    w.write(enc_buf)

    m_enc = build_material_with_storage(db, store, case_id=case_id, content=enc_buf.getvalue(), name="限制打印.pdf")
    m_bad_img = build_material_with_storage(db, store, case_id=case_id, content=b"NOT_A_REAL_PNG", name="损坏图片.png")
    db.commit()

    cat = evidence_catalog.add_group(db, AddEvidenceGroupInput(case_id=case_id, title="容错测试"), actor="human", entry="local-web")
    cat = evidence_catalog.add_item(db, AddEvidenceItemInput(case_id=case_id, group_id=cat.groups[0].id, title="加密PDF"), actor="human", entry="local-web")
    i1 = cat.items[0].id
    cat = evidence_catalog.add_item(db, AddEvidenceItemInput(case_id=case_id, group_id=cat.groups[0].id, title="坏图"), actor="human", entry="local-web")
    i2 = next(i.id for i in cat.items if i.title == "坏图")
    evidence_catalog.add_binding(db, AddEvidenceBindingInput(case_id=case_id, item_id=i1, material_id=m_enc["material_id"]), actor="human", entry="local-web")
    evidence_catalog.add_binding(db, AddEvidenceBindingInput(case_id=case_id, item_id=i2, material_id=m_bad_img["material_id"]), actor="human", entry="local-web")

    result = dossier_export.export_evidence_bundle(db, store, case_id)
    assert "/" not in result.filename
    assert any("损坏图片.png" in wmsg for wmsg in result.summary["warnings"])
    with zipfile.ZipFile(io.BytesIO(result.data), "r") as zf:
        split_pdf = zf.read("02_分项证据材料/证据1-1_加密PDF.pdf")
        assert "OWNER_ENC_TEXT" in (PdfReader(io.BytesIO(split_pdf)).pages[0].extract_text() or "")
        assert "02_分项证据材料/证据1-2_坏图.png" in zf.namelist()


def test_bundle_sanitizes_xml_control_chars_and_csv_formula_injection(db, store):
    assert dossier_export._sanitize_xml_text("a\x00b\x0bc\x0cd\x07e") == "ab c de"
    case_id = create_case(db, title="控制字符\x0b测试\x07案")
    db.commit()

    m_pdf = build_material_with_storage(
        db, store, case_id=case_id, content=_pdf_bytes(["SAFE_PAGE"]), name="原件.pdf",
    )
    cat = evidence_catalog.add_group(db, AddEvidenceGroupInput(case_id=case_id, title="=SUM(A1)\x0c组"), actor="human", entry="local-web")
    cat = evidence_catalog.add_item(
        db,
        AddEvidenceItemInput(
            case_id=case_id, group_id=cat.groups[0].id,
            title="-100元扣款\x0b凭证", purpose="@HYPERLINK\x08说明",
        ),
        actor="human", entry="local-web",
    )
    item_id = cat.items[0].id
    evidence_catalog.add_binding(db, AddEvidenceBindingInput(case_id=case_id, item_id=item_id, material_id=m_pdf["material_id"]), actor="human", entry="local-web")

    result = dossier_export.export_evidence_bundle(db, store, case_id)
    with zipfile.ZipFile(io.BytesIO(result.data), "r") as zf:
        docx_doc = DocxDocument(io.BytesIO(zf.read("01_证据目录.docx")))
        assert any("控制字符" in p.text for p in docx_doc.paragraphs)
        csv_text = zf.read("01_证据目录.csv").decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(csv_text)))
        assert rows[1][1].startswith("'=SUM(A1)")
        assert rows[1][3].startswith("'-100元扣款")
        assert rows[1][6].startswith("'@HYPERLINK")


def test_bundle_keeps_original_pdf_when_page_render_fails_and_skips_partial_tiff(db, store, monkeypatch):
    case_id = create_case(db, title="双失败与多帧容错案")
    m_pdf = build_material_with_storage(
        db, store, case_id=case_id, content=_pdf_bytes(["P1", "P2"]), name="双失败.pdf",
    )

    # Create a corrupt 2-frame TIFF where frame 0 is valid and frame 1 fails on seek/copy
    buf = io.BytesIO()
    Image.new("RGB", (200, 300), (200, 200, 200)).save(buf, format="PNG")
    m_img = build_material_with_storage(
        db, store, case_id=case_id, content=buf.getvalue(), name="坏第二帧.tiff",
    )
    db.commit()

    cat = evidence_catalog.add_group(db, AddEvidenceGroupInput(case_id=case_id, title="容错"), actor="human", entry="local-web")
    cat = evidence_catalog.add_item(db, AddEvidenceItemInput(case_id=case_id, group_id=cat.groups[0].id, title="问题PDF"), actor="human", entry="local-web")
    i1 = cat.items[0].id
    cat = evidence_catalog.add_item(db, AddEvidenceItemInput(case_id=case_id, group_id=cat.groups[0].id, title="问题TIFF"), actor="human", entry="local-web")
    i2 = next(i.id for i in cat.items if i.title == "问题TIFF")
    evidence_catalog.add_binding(db, AddEvidenceBindingInput(case_id=case_id, item_id=i1, material_id=m_pdf["material_id"]), actor="human", entry="local-web")
    evidence_catalog.add_binding(db, AddEvidenceBindingInput(case_id=case_id, item_id=i2, material_id=m_img["material_id"]), actor="human", entry="local-web")

    def boom_place(_page):
        raise RuntimeError("vector fail")

    def boom_raster(_raw, _idx):
        raise RuntimeError("raster fail")

    monkeypatch.setattr(dossier_export, "_place_pdf_page_onto_a4", boom_place)
    monkeypatch.setattr(dossier_export, "_rasterize_pdf_page_to_a4", boom_raster)

    orig_open = Image.open

    class _FakeMultiFrame:
        n_frames = 2

        def __init__(self, inner):
            self._inner = inner

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self._inner.close()

        def seek(self, idx):
            if idx == 1:
                raise OSError("corrupt frame 2")

        def copy(self):
            return self._inner.copy()

    monkeypatch.setattr(Image, "open", lambda p: _FakeMultiFrame(orig_open(p)) if str(p).endswith(".tiff") else orig_open(p))

    result = dossier_export.export_evidence_bundle(db, store, case_id)
    assert any("保留原 PDF" in wmsg for wmsg in result.summary["warnings"])
    with zipfile.ZipFile(io.BytesIO(result.data), "r") as zf:
        names = set(zf.namelist())
        assert "02_分项证据材料/证据1-1_问题PDF.pdf" in names
        assert "02_分项证据材料/证据1-2_问题TIFF.tiff" in names
        assert "02_分项证据材料/证据1-2_问题TIFF.pdf" not in names
