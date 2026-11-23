"""The dashboard and annotation review use real PostgreSQL and fixed source versions."""
from datetime import datetime, timezone
from io import BytesIO
from uuid import UUID, uuid4

from PIL import Image
from sqlalchemy import select

from legal_workbench import cases as cases_service
from legal_workbench import models, progress, source_notes
from legal_workbench.app import app
from legal_workbench.schemas import (AddNodeInput, CreateAnnotationInput, NodeChanges,
                                     SetActiveStageInput, UpdateNodeInput)

from .factories import build_material_with_storage
from .test_api import new_case


def test_dashboard_orders_across_pages_and_only_counts_open_deadlines(client):
    first = new_case(client, "TEST-ONLY A")
    second = new_case(client, "TEST-ONLY B")
    third = new_case(client, "TEST-ONLY C")
    url = f"/api/v1/cases/{first}/schedule"
    schedule = {
        "case_id": first, "expected_revision": 0, "stage": "庭审准备", "next_action": "核对原件",
        "deadlines": [
            {"title": "已完成", "due": "2026-11-05", "kind": "external", "completed": True},
            {"title": "内部计划", "due": "2026-11-26", "kind": "internal"},
            {"title": "提交证据", "due": "2026-11-24", "kind": "external"},
        ],
    }
    saved = client.patch(url, json=schedule)
    assert saved.status_code == 200, saved.text
    assert saved.json()["stage"] == "庭审准备"
    assert saved.json()["revision"] == 1
    assert client.patch(url, json=schedule).status_code == 409
    assert client.patch(url, json={**schedule, "case_id": third}).json()["code"] == "case_mismatch"
    saved = client.patch(f"/api/v1/cases/{second}/schedule", json={
        "case_id": second, "expected_revision": 0, "deadlines": [
            {"title": "收到法院通知", "due": "2026-11-25", "kind": "external"}]})
    assert saved.status_code == 200
    assert client.get("/api/v1/dashboard?limit=1").json()["items"][0]["case_id"] == first
    assert client.get("/api/v1/dashboard?offset=1&limit=1").json()["items"][0]["case_id"] == second
    schedule["expected_revision"] = 1
    schedule["deadlines"][2]["completed"] = True
    assert client.patch(url, json=schedule).status_code == 200
    assert [item["case_id"] for item in client.get("/api/v1/dashboard").json()["items"]] == [
        second, first, third]
    assert client.get(f"/api/v1/cases/{third}").json()["deadlines"] == []


def test_overview_shows_all_active_stages_and_nested_work(client, db):
    """审查末节：首页并行程序——两个程序各有进行中阶段全部展示，嵌套未完成工作计入。"""
    case_id = new_case(client, "TEST-ONLY 并行程序")
    case_uuid = UUID(case_id)
    snap = progress.get_progress(db, case_uuid)
    pretrial = next(n for n in snap.nodes if n.title == "庭前程序")
    progress.set_active_stage(db, SetActiveStageInput(
        case_id=case_uuid, stage_id=pretrial.id, expected_revision=pretrial.revision),
        actor="human", entry="local-web")
    # 第二个并行程序「二审」＋进行中阶段
    p2 = progress.add_node(db, AddNodeInput(
        case_id=case_uuid, parent_id=None, kind="program", title="二审"),
        actor="human", entry="local-web")
    second_program = next(n for n in p2.nodes if n.title == "二审")
    s2 = progress.add_node(db, AddNodeInput(
        case_id=case_uuid, parent_id=second_program.id, kind="stage", title="二审审理"),
        actor="human", entry="local-web")
    second_stage = next(n for n in s2.nodes if n.title == "二审审理")
    progress.set_active_stage(db, SetActiveStageInput(
        case_id=case_uuid, stage_id=second_stage.id, expected_revision=second_stage.revision),
        actor="human", entry="local-web")
    # 嵌套工作：庭前程序 → 事件 → 工作；以及直接子工作
    ev = progress.add_node(db, AddNodeInput(
        case_id=case_uuid, parent_id=pretrial.id, kind="event", title="证据交换"),
        actor="human", entry="local-web")
    event = next(n for n in ev.nodes if n.title == "证据交换")
    progress.add_node(db, AddNodeInput(
        case_id=case_uuid, parent_id=event.id, kind="work", title="补充证据"),
        actor="human", entry="local-web")
    progress.add_node(db, AddNodeInput(
        case_id=case_uuid, parent_id=pretrial.id, kind="work", title="质证意见"),
        actor="human", entry="local-web")
    progress.add_node(db, AddNodeInput(
        case_id=case_uuid, parent_id=pretrial.id, kind="work", title="开庭准备"),
        actor="human", entry="local-web")
    # 已完成工作不计入未完成
    w = progress.add_node(db, AddNodeInput(
        case_id=case_uuid, parent_id=second_stage.id, kind="work", title="已办结"),
        actor="human", entry="local-web")
    done = next(n for n in w.nodes if n.title == "已办结")
    progress.update_node(db, UpdateNodeInput(
        case_id=case_uuid, node_id=done.id, expected_revision=done.revision,
        changes=NodeChanges(state="done")), actor="human", entry="local-web")
    overview = client.get(f"/api/v1/cases/{case_id}").json()
    assert set(overview["active_stages"]) == {"庭前程序", "二审审理"}
    assert overview["active_stage"] in {"庭前程序", "二审审理"}
    # 顺序：同级按 position 正序（质证意见→开庭准备），嵌套工作（证据交换下的补充证据）就地展开。
    assert overview["pending_work"] == ["补充证据", "质证意见", "开庭准备"]


def test_fixed_pdf_page_and_scoped_review(client, db):
    original = BytesIO()
    red = Image.new("RGB", (200, 240), "red")
    red.save(original, "PDF", save_all=True, append_images=[Image.new("RGB", (200, 240), "blue")])
    built = build_material_with_storage(db, app.state.storage, content=original.getvalue(), name="TEST-ONLY.pdf")
    case_id, version_id = built["case_id"], built["file_version_id"]
    other = new_case(client, "TEST-ONLY unrelated")
    path = f"/api/v1/cases/{case_id}/file-versions/{version_id}/page-preview"
    page = client.get(path, params={"page": 2})
    assert page.status_code == 200
    with Image.open(BytesIO(page.content)) as rendered:
        assert rendered.getpixel((40, 40))[2] > 240
    assert client.get(path, params={"page": 3}).status_code == 404
    assert client.get(path.replace(str(case_id), other), params={"page": 2}).status_code == 404

    base = f"/api/v1/cases/{case_id}/annotations"
    note = {"case_id": str(case_id), "file_version_id": str(version_id), "page": 2,
            "body": "TEST-ONLY exact original text", "kind": "transcription"}
    invalid = client.post(base, json={**note, "page": 3})
    assert invalid.json()["code"] == "invalid_input"
    human = client.post(base, json={**note, "kind": "interpretation"}).json()
    assert human["author"] == "human"
    agent = source_notes.create_annotation(db, CreateAnnotationInput.model_validate(note),
                                           author="agent", storage=app.state.storage)
    listed = client.get(base, params={"query": "exact original"}).json()
    assert listed["total"] == 2
    assert [item for item in listed["items"] if item["author"] == "agent"][0]["reviewed_text"] is None
    review_path = f"{base}/{agent.annotation_id}/review"
    body = {"case_id": str(case_id), "annotation_id": str(agent.annotation_id),
            "reviewed_text": "TEST-ONLY matched original page"}
    conflict = client.post(review_path.replace(str(case_id), other),
                           json={**body, "case_id": other})
    assert conflict.json()["code"] == "case_mismatch"
    reviewed = client.post(review_path, json=body)
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["page"] == 2 and reviewed.json()["revision"] == 1
    assert reviewed.json()["body"] == note["body"]
    assert client.get(base, params={"query": "matched original"}).json()["total"] == 1
    assert client.post(review_path, json={**body, "reviewed_text": "do not overwrite"}).status_code == 409
    stored = db.scalar(select(models.Annotation).where(models.Annotation.id == UUID(reviewed.json()["annotation_id"])))
    assert stored.reviewed_text == body["reviewed_text"]


def test_acquisition_is_per_material_and_readable_from_a_fixed_source(client, db):
    from legal_workbench import search
    from legal_workbench.db import get_engine
    from legal_workbench.schemas import SearchMode
    from legal_workbench.worker import run_worker
    from .test_api import upload

    case_id = new_case(client)
    assert upload(client, case_id, [("TEST-ONLY.txt", b"TEST-ONLY transfer receipt")]).status_code == 200
    run_worker(get_engine(), app.state.storage, once=True)
    materials = f"/api/v1/cases/{case_id}/materials"
    item = client.get(materials).json()["items"][0]
    assert item["acquisition"] is None
    body = {"case_id": case_id, "material_id": item["material_id"],
            "acquisition": "纸件扫描", "expected_revision": item["revision"]}
    path = f"{materials}/{item['material_id']}/acquisition"
    assert client.patch(path, json=body).status_code == 200
    assert client.patch(path, json=body).status_code == 409
    result = search.search_case(db, case_id=UUID(case_id), query="transfer", mode=SearchMode.LITERAL)
    ref = result.hits[0].source.source_ref
    source = client.get(f"/api/v1/cases/{case_id}/sources/{ref}").json()
    assert source["acquisition"] == "纸件扫描"
    assert source["source_label"] == "TEST-ONLY"


def test_preview_uses_indexed_image_frame(client, db):
    original = BytesIO()
    Image.new("RGB", (20, 20), "red").save(
        original, "TIFF", save_all=True, append_images=[Image.new("RGB", (20, 20), "blue")])
    built = build_material_with_storage(db, app.state.storage, content=original.getvalue(), name="TEST-ONLY.tiff")
    route = f"/api/v1/cases/{built['case_id']}/file-versions/{built['file_version_id']}/preview"
    response = client.get(route, params={"frame": 1})
    assert response.status_code == 200
    with Image.open(BytesIO(response.content)) as rendered:
        assert rendered.getpixel((10, 10)) == (0, 0, 255)


def test_dashboard_deadlines_cover_all_open_cases_beyond_the_dashboard_page(client, db):
    """首页时间轴：全部在办案件（含第 2 页），排除归档，字段与案件概览一致。"""
    # 52 个批量案件排在 /dashboard 一页（limit=50）之外，目标案件是第 53 个。
    for index in range(52):
        db.add(models.Case(
            id=uuid4(), title=f"TEST-ONLY 批量 {index:02d}",
            deadlines=[{"title": f"批量事项 {index:02d}", "due": "2026-12-01",
                        "kind": "internal", "completed": False}]))
    archived = models.Case(
        id=uuid4(), title="TEST-ONLY 已归档", archived_at=datetime.now(timezone.utc),
        deadlines=[{"title": "归档后不应出现", "due": "2026-11-20",
                    "kind": "external", "completed": False}])
    db.add(archived)
    db.commit()

    late = new_case(client, "TEST-ONLY 后页案件")
    scheduled = client.patch(f"/api/v1/cases/{late}/schedule", json={
        "case_id": late, "expected_revision": 0, "deadlines": [
            {"title": "二审开庭", "due": "2027-03-01", "kind": "external"},
        ]})
    assert scheduled.status_code == 200, scheduled.text
    same_day = new_case(client, "TEST-ONLY 同日案件")
    assert client.patch(f"/api/v1/cases/{same_day}/schedule", json={
        "case_id": same_day, "expected_revision": 0, "deadlines": [
            {"title": "提交答辩状", "due": "2026-11-01", "kind": "external"},
            {"title": "内部复核", "due": "2026-11-01", "kind": "internal"},
            {"title": "已完成的送达", "due": "2026-11-02", "kind": "external", "completed": True},
        ]}).status_code == 200

    # 前提：后页案件确实不在 /dashboard 第一页，只在第二页。
    first_page = client.get("/api/v1/dashboard", params={"limit": 50}).json()
    assert late not in {item["case_id"] for item in first_page["items"]}
    second_page = client.get("/api/v1/dashboard", params={"offset": 50, "limit": 50}).json()
    assert late in {item["case_id"] for item in second_page["items"]}

    response = client.get("/api/v1/dashboard/deadlines")
    assert response.status_code == 200, response.text
    payload = response.json()
    # 52 批量 + 后页 + 同日 = 54 个未归档案件；归档案件不计数。
    assert payload["case_total"] == 54
    items = payload["items"]
    assert len(items) == 52 + 1 + 3
    assert payload["as_of"] == cases_service.shanghai_today().isoformat()
    dues = [item["due"] for item in items]
    assert dues == sorted(dues)
    # 第 2 页案件的期限没有被 50 条分页截断。
    assert {"case_id": late, "case_title": "TEST-ONLY 后页案件", "title": "二审开庭",
            "due": "2027-03-01", "kind": "external", "completed": False} in items
    # 归档案件不出现。
    assert all(item["case_id"] != str(archived.id) for item in items)
    assert "归档后不应出现" not in {item["title"] for item in items}
    # 同日堆叠：日期相同按案件标题 → 事项标题稳定排序；完成状态照实返回。
    same_day_items = [item for item in items if item["case_id"] == same_day]
    assert [item["title"] for item in same_day_items] == ["内部复核", "提交答辩状", "已完成的送达"]
    assert [item["kind"] for item in same_day_items] == ["internal", "external", "external"]
    assert [item["completed"] for item in same_day_items] == [False, False, True]
    # 验收条件 1：时间轴条目与案件概览页的期限逐项一致（标题、日期、类别、完成）。
    # 时间轴同日按标题排序，概览保留录入顺序，因此比对内容集合而不是顺序。
    overview = client.get(f"/api/v1/cases/{same_day}").json()
    timeline_view = sorted((item["title"], item["due"], item["kind"], item["completed"])
                           for item in same_day_items)
    overview_view = sorted((entry["title"], entry["due"], entry["kind"], entry["completed"])
                           for entry in overview["deadlines"])
    assert timeline_view == overview_view


def test_dashboard_deadlines_skips_malformed_jsonb_and_empty_database(client, db):
    """存量 JSONB 的坏条目被跳过，不拖垮首页；无期限时返回空列表。"""
    assert client.get("/api/v1/dashboard/deadlines").json() == {
        "items": [], "as_of": cases_service.shanghai_today().isoformat(), "case_total": 0}
    db.add(models.Case(id=uuid4(), title="TEST-ONLY 脏数据", deadlines=[
        {"title": "缺日期", "kind": "external"},
        {"title": "类别非法", "due": "2026-12-06", "kind": "court"},
        {"title": "合法条目", "due": "2026-12-05", "kind": "external"},
    ]))
    db.commit()
    payload = client.get("/api/v1/dashboard/deadlines").json()
    assert payload["case_total"] == 1
    assert [(item["title"], item["due"]) for item in payload["items"]] == [
        ("合法条目", "2026-12-05")]
