"""T13: local HTTP API — local access and Host/Origin guards, uploads, previews."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from legal_workbench import models
from legal_workbench.app import app
from legal_workbench.db import get_session_factory

from . import factories

BASE = "http://127.0.0.1:8765"


def new_case(client, title="虚构案件") -> str:
    response = client.post("/api/v1/cases", json={"title": title})
    assert response.status_code == 200, response.text
    return response.json()["case_id"]


def upload(client, case_id, files, paths=None, request_key=None):
    paths = paths if paths is not None else [name for name, _ in files]
    return client.post(
        f"/api/v1/cases/{case_id}/intakes/upload",
        data={"source_label": "TEST-ONLY", "request_key": request_key or uuid.uuid4().hex,
              "relative_paths": paths},
        files=[("files", (name, content, "text/plain")) for name, content in files],
    )


def test_local_reads_and_writes_without_login(api):
    created = api.post("/api/v1/cases", json={"title": "TEST-ONLY 直接建案"},
                       headers={"Origin": BASE})
    assert created.status_code == 200
    assert "set-cookie" not in created.headers
    case_id = created.json()["case_id"]
    listed = api.get("/api/v1/cases")
    assert listed.status_code == 200
    assert any(item["case_id"] == case_id for item in listed.json()["items"])
    assert api.get(f"/api/v1/cases/{case_id}/materials").status_code == 200
    assert api.get("/api/v1/session").status_code == 404


@pytest.mark.parametrize("origin", [None, "https://foreign.example", "http://127.0.0.1:9999",
                                    "http://localhost:8765", "null"])
def test_writes_require_same_origin(api, origin):
    response = api.post("/api/v1/cases", json={"title": "TEST-ONLY 不应建案"},
                        headers={} if origin is None else {"Origin": origin})
    assert response.status_code == 403 and response.json()["code"] == "forbidden"
    assert api.get("/api/v1/cases").json()["total"] == 0


def test_foreign_host_header_is_forbidden(client):
    response = client.get("/api/v1/cases", headers={"Host": "attacker.example"})
    assert response.status_code == 403
    port = client.get("/api/v1/cases", headers={"Host": "127.0.0.1:9999"})
    assert port.status_code == 403


def test_timestamps_are_returned_as_utc(client):
    """The page renders Asia/Shanghai, so the API must hand over unambiguous UTC."""
    case_id = new_case(client)
    created = client.get("/api/v1/cases").json()["items"][0]["created_at"]
    parsed = datetime.fromisoformat(created)
    assert parsed.tzinfo is not None, created
    assert parsed.utcoffset() == timedelta(0), created
    # A case created now must not read as the past in UTC.
    assert abs((datetime.now(timezone.utc) - parsed).total_seconds()) < 300

    upload(client, case_id, [("时间.txt", "TEST-ONLY 正文".encode())])
    from legal_workbench import worker
    from legal_workbench.db import get_engine
    worker.run_worker(get_engine(), app.state.storage, once=True)
    material = client.get(f"/api/v1/cases/{case_id}/materials").json()["items"][0]
    assert datetime.fromisoformat(material["created_at"]).utcoffset() == timedelta(0)


def test_case_and_material_flow(client):
    case_id = new_case(client)
    listing = client.get("/api/v1/cases")
    assert listing.status_code == 200 and listing.json()["total"] == 1

    response = upload(client, case_id, [("合同.txt", "TEST-ONLY 合同正文 100元".encode())])
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    intake_id = response.json()["intake_id"]

    status = client.get(f"/api/v1/cases/{case_id}/jobs/{job_id}")
    assert status.status_code == 200 and status.json()["status"] == "queued"

    from legal_workbench import worker
    from legal_workbench.db import get_engine
    worker.run_worker(get_engine(), app.state.storage, once=True)

    items = client.get(f"/api/v1/cases/{case_id}/intakes/{intake_id}/items")
    assert items.status_code == 200
    assert items.json()["total"] == 1 and items.json()["items"][0]["status"] == "ready"

    materials = client.get(f"/api/v1/cases/{case_id}/materials")
    assert materials.status_code == 200
    material = materials.json()["items"][0]
    assert material["display_name"] == "合同.txt" and material["category"] == "待分类"

    patched = client.patch(
        f"/api/v1/cases/{case_id}/materials/{material['material_id']}/classification",
        json={"case_id": case_id, "material_id": material["material_id"],
              "category": "合同", "expected_revision": 0},
    )
    assert patched.status_code == 200 and patched.json()["revision"] == 1
    stale = client.patch(
        f"/api/v1/cases/{case_id}/materials/{material['material_id']}/classification",
        json={"case_id": case_id, "material_id": material["material_id"],
              "category": "x", "expected_revision": 0},
    )
    assert stale.status_code == 409 and stale.json()["code"] == "conflict"

    search = client.post(f"/api/v1/cases/{case_id}/search",
                         json={"case_id": case_id, "query": "合同", "mode": "literal"})
    assert search.status_code == 200, search.text
    assert search.json()["total"] == 1
    hit = search.json()["hits"][0]
    source = client.get(f"/api/v1/cases/{case_id}/sources/{hit['source']['source_ref']}")
    assert source.status_code == 200
    assert source.json()["text"] == "TEST-ONLY 合同正文 100元"

    original = client.get(f"/api/v1/cases/{case_id}/materials/{material['material_id']}/original")
    assert original.status_code == 200
    assert original.json()["integrity_status"] == "ok"


def test_path_and_body_ids_must_match(client):
    case_id = new_case(client)
    other = new_case(client, "另一案件")
    response = client.post(f"/api/v1/cases/{case_id}/search",
                           json={"case_id": other, "query": "x", "mode": "literal"})
    assert response.status_code == 422 and response.json()["code"] == "invalid_input"


def test_upload_relative_paths_cannot_escape(client):
    case_id = new_case(client)
    for bad in ("../escape.txt", "/etc/passwd", "a/../../b.txt"):
        response = upload(client, case_id, [("x.txt", b"TEST-ONLY")], paths=[bad])
        assert response.status_code == 422, bad
        assert response.json()["code"] == "invalid_input"


def test_upload_count_mismatch_and_duplicates_rejected(client):
    case_id = new_case(client)
    mismatch = upload(client, case_id, [("a.txt", b"TEST-ONLY")], paths=["a.txt", "b.txt"])
    assert mismatch.status_code == 422
    duplicate = upload(client, case_id, [("a.txt", b"1"), ("b.txt", b"2")],
                       paths=["a.txt", "a.txt"])
    assert duplicate.status_code == 422


def test_upload_is_idempotent_by_request_key(client):
    case_id = new_case(client)
    first = upload(client, case_id, [("a.txt", b"TEST-ONLY a")], request_key="k1")
    second = upload(client, case_id, [("a.txt", b"TEST-ONLY a")], request_key="k1")
    assert first.json()["intake_id"] == second.json()["intake_id"]
    assert first.json()["job_id"] == second.json()["job_id"]


def test_cross_case_file_version_is_rejected(client):
    first = new_case(client)
    second = new_case(client, "第二案")
    upload(client, first, [("a.txt", b"TEST-ONLY a")])
    from legal_workbench import worker
    from legal_workbench.db import get_engine
    worker.run_worker(get_engine(), app.state.storage, once=True)
    session = get_session_factory()()
    try:
        version = session.execute(select(models.FileVersion)).scalars().first()
        version_id = version.id
    finally:
        session.close()
    response = client.get(f"/api/v1/cases/{second}/file-versions/{version_id}/preview")
    assert response.status_code == 404 and response.json()["code"] == "case_mismatch"
    download = client.get(f"/api/v1/cases/{second}/file-versions/{version_id}/download")
    assert download.status_code == 404


def test_download_is_attachment_and_preview_escapes_text(client):
    case_id = new_case(client)
    payload = "<script>alert(1)</script>".encode()
    upload(client, case_id, [("note.txt", payload)])
    from legal_workbench import worker
    from legal_workbench.db import get_engine
    worker.run_worker(get_engine(), app.state.storage, once=True)
    session = get_session_factory()()
    try:
        version = session.execute(select(models.FileVersion)).scalars().first()
        version_id = str(version.id)
    finally:
        session.close()

    download = client.get(f"/api/v1/cases/{case_id}/file-versions/{version_id}/download")
    assert download.status_code == 200
    disposition = download.headers["content-disposition"].lower()
    assert disposition.startswith("attachment")
    assert "filename*=utf-8''" in disposition
    assert 'filename="note.txt"' in disposition
    assert download.headers["content-type"] == "application/octet-stream"
    assert download.content == payload

    preview = client.get(f"/api/v1/cases/{case_id}/file-versions/{version_id}/preview")
    assert preview.status_code == 200
    assert preview.headers["content-type"].startswith("text/plain")
    assert preview.headers["x-content-type-options"] == "nosniff"
    assert "<script>" in preview.text


def test_chinese_download_name_gets_ascii_fallback(client):
    case_id = new_case(client)
    upload(client, case_id, [("虚构材料.txt", b"TEST-ONLY bytes")])
    from legal_workbench import worker
    from legal_workbench.db import get_engine
    worker.run_worker(get_engine(), app.state.storage, once=True)
    session = get_session_factory()()
    try:
        version = session.execute(select(models.FileVersion)).scalars().first()
        version_id = str(version.id)
    finally:
        session.close()
    response = client.get(f"/api/v1/cases/{case_id}/file-versions/{version_id}/download")
    disposition = response.headers["content-disposition"]
    # The plain filename parameter must stay ASCII; the real name rides in filename*.
    assert 'filename="material.txt"' in disposition
    assert "filename*=UTF-8''" in disposition
    assert response.content == b"TEST-ONLY bytes"


def test_invalid_uuid_is_invalid_input_not_500(client):
    response = client.get("/api/v1/cases/not-a-uuid/materials")
    assert response.status_code == 422 and response.json()["code"] == "invalid_input"


def test_analysis_endpoints_round_trip(client):
    case_id = new_case(client)
    upload(client, case_id, [("合同.txt", "TEST-ONLY 合同条款".encode())])
    from legal_workbench import worker
    from legal_workbench.db import get_engine
    worker.run_worker(get_engine(), app.state.storage, once=True)
    session = get_session_factory()()
    try:
        segment = session.execute(select(models.Segment)).scalars().first()
        source_ref = str(segment.id)
    finally:
        session.close()

    saved = client.post(f"/api/v1/cases/{case_id}/analyses",
                        json={"case_id": case_id, "title": "虚构分析",
                              "content": "分析结论：合同有效。", "statement_kind": "agent_analysis",
                              "source_refs": [source_ref]})
    assert saved.status_code == 200, saved.text
    analysis_id, version_id = saved.json()["analysis_id"], saved.json()["version_id"]

    listed = client.get(f"/api/v1/cases/{case_id}/analyses")
    assert listed.json()["total"] == 1
    versions = client.get(f"/api/v1/cases/{case_id}/analyses/{analysis_id}/versions")
    assert versions.json()["total"] == 1
    fetched = client.get(
        f"/api/v1/cases/{case_id}/analyses/{analysis_id}/versions/{version_id}")
    assert fetched.json()["content"] == "分析结论：合同有效。"

    reviewed = client.post(
        f"/api/v1/cases/{case_id}/analyses/{analysis_id}/versions/{version_id}/review",
        json={"case_id": case_id, "analysis_id": analysis_id, "version_id": version_id,
              "expected_revision": 0, "review_status": "approved"})
    assert reviewed.status_code == 200 and reviewed.json()["review_status"] == "approved"


def test_pagination_bounds_are_enforced(client):
    case_id = new_case(client)
    assert client.get("/api/v1/cases", params={"limit": 0}).status_code == 422
    assert client.get("/api/v1/cases", params={"limit": 500}).status_code == 422
    assert client.get("/api/v1/cases", params={"offset": -1}).status_code == 422
    too_wide = client.get(f"/api/v1/cases/{case_id}/sources/{uuid.uuid4()}",
                          params={"context_chars": 5000})
    assert too_wide.status_code == 422


def test_retry_reuses_job_id(client):
    case_id = new_case(client)
    created = upload(client, case_id, [("a.txt", b"TEST-ONLY a")])
    job_id = created.json()["job_id"]
    from legal_workbench import worker
    from legal_workbench.db import get_engine
    worker.run_worker(get_engine(), app.state.storage, once=True)
    retried = client.post(f"/api/v1/cases/{case_id}/jobs/{job_id}/retry")
    assert retried.status_code == 200
    assert retried.json()["job_id"] == job_id and retried.json()["status"] == "queued"


def test_txt_original_previews_without_being_parsed(client):
    """A .txt original must preview straight from bytes, not wait for parsing.

    Regression: the preview fell through to extracted text, so an unparsed .txt
    returned 422 and the iframe showed a JSON error.
    """
    case_id = new_case(client)
    upload(client, case_id, [("预览.txt", "TEST-ONLY 预览正文 <script>x</script>".encode())])
    from legal_workbench import worker
    from legal_workbench.db import get_engine
    worker.run_worker(get_engine(), app.state.storage, once=True)
    material = client.get(f"/api/v1/cases/{case_id}/materials").json()["items"][0]
    original = client.get(
        f"/api/v1/cases/{case_id}/materials/{material['material_id']}/original").json()
    response = client.get(
        f"/api/v1/cases/{case_id}/file-versions/{original['file_version_id']}/preview")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["x-content-type-options"] == "nosniff"
    # Served as text: the markup is not interpreted, it is just characters.
    assert "<script>" in response.text


def test_preview_serves_text_even_before_any_parse_run(client, db):
    """The preview must not depend on a parse run existing."""
    built = factories.build_material_with_storage(
        db, app.state.storage, content="TEST-ONLY 未解析正文".encode(), name="未解析.txt")
    preview = client.get(
        f"/api/v1/cases/{built['case_id']}/file-versions/{built['file_version_id']}/preview")
    assert preview.status_code == 200, preview.text
    assert preview.headers["content-type"].startswith("text/plain")
    assert "未解析正文" in preview.text
