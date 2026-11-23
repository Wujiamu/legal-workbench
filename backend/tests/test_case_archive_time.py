"""Case archive and persisted time entries (0006). Real PostgreSQL, TEST-ONLY data."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from legal_workbench import materials, models, progress, time_entries
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.schemas import AddNodeInput, StopTimerInput

from .factories import build_material
from .test_api import new_case


def _codes(fn):
    with pytest.raises(WorkbenchError) as exc:
        fn()
    return exc.value.code


def _overview(client, case_id: str) -> dict:
    response = client.get(f"/api/v1/cases/{case_id}")
    assert response.status_code == 200, response.text
    return response.json()


def test_archive_blocks_progress_material_and_schedule_writes(client, db):
    """归档后进度/材料/办理信息写入被拒；列表过滤、首页隐藏；取消归档恢复。"""
    case_id = new_case(client, "TEST-ONLY 归档")
    case_uuid = uuid.UUID(case_id)
    built = build_material(db, case_id=case_uuid)
    db.commit()
    before = _overview(client, case_id)
    archived = client.post(f"/api/v1/cases/{case_id}/archive",
                           json={"case_id": case_id, "expected_revision": before["revision"]})
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived_at"] is not None
    # 进度写入
    assert _codes(lambda: progress.add_node(db, AddNodeInput(
        case_id=case_uuid, parent_id=None, kind="stage", title="归档后新增"),
        actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT
    # 材料改名
    assert _codes(lambda: materials.update_display_name(
        db, case_id=case_uuid, material_id=built["material_id"],
        display_name="归档后改名", expected_revision=0)) == ErrorCode.INVALID_INPUT
    # 办理信息
    denied = client.patch(f"/api/v1/cases/{case_id}/schedule", json={
        "case_id": case_id, "expected_revision": archived.json()["revision"],
        "stage": "立案", "deadlines": []})
    assert denied.status_code == 422
    assert denied.json()["code"] == "invalid_input"
    # 列表过滤与首页隐藏（全量分页，避免被其他用例创建的案件挤出首页）
    archived_ids = [item["case_id"] for item in
                    client.get("/api/v1/cases?archived=true&limit=200").json()["items"]]
    active_ids = [item["case_id"] for item in
                  client.get("/api/v1/cases?archived=false&limit=200").json()["items"]]
    assert case_id in archived_ids and case_id not in active_ids
    dashboard_ids = [item["case_id"] for item in
                     client.get("/api/v1/dashboard?limit=200").json()["items"]]
    assert case_id not in dashboard_ids
    # 取消归档恢复写入
    current = _overview(client, case_id)
    restored = client.post(f"/api/v1/cases/{case_id}/unarchive",
                           json={"case_id": case_id, "expected_revision": current["revision"]})
    assert restored.status_code == 200, restored.text
    assert restored.json()["archived_at"] is None
    assert progress.add_node(db, AddNodeInput(
        case_id=case_uuid, parent_id=None, kind="stage", title="恢复后新增"),
        actor="human", entry="local-web").nodes


def test_archive_stale_revision_conflicts(client, db):
    case_id = new_case(client, "TEST-ONLY 归档 CAS")
    stale = _overview(client, case_id)["revision"]
    moved = client.patch(f"/api/v1/cases/{case_id}/schedule", json={
        "case_id": case_id, "expected_revision": stale, "stage": "立案", "deadlines": []})
    assert moved.status_code == 200, moved.text
    resp = client.post(f"/api/v1/cases/{case_id}/archive",
                       json={"case_id": case_id, "expected_revision": stale})
    assert resp.status_code == 409
    assert resp.json()["code"] == "conflict"


def test_archive_requires_no_running_timer_and_blocks_time_writes(client, db):
    case_id = new_case(client, "TEST-ONLY 归档计时")
    started = client.post(f"/api/v1/cases/{case_id}/time/start",
                          json={"case_id": case_id, "node_title": "起草起诉状"})
    assert started.status_code == 200, started.text
    entry = started.json()
    overview = _overview(client, case_id)
    blocked = client.post(f"/api/v1/cases/{case_id}/archive",
                          json={"case_id": case_id, "expected_revision": overview["revision"]})
    assert blocked.status_code == 422
    assert "计时" in blocked.json()["message"]
    stopped = client.post(f"/api/v1/cases/{case_id}/time/entries/{entry['id']}/stop",
                          json={"case_id": case_id, "entry_id": entry["id"]})
    assert stopped.status_code == 200, stopped.text
    overview = _overview(client, case_id)
    assert client.post(f"/api/v1/cases/{case_id}/archive",
                       json={"case_id": case_id,
                             "expected_revision": overview["revision"]}).status_code == 200
    denied = client.post(f"/api/v1/cases/{case_id}/time/entries", json={
        "case_id": case_id, "node_title": "补录", "hours": 0.25,
        "work_date": date.today().isoformat()})
    assert denied.status_code == 422
    assert denied.json()["code"] == "invalid_input"


def test_timer_rounds_up_and_rejects_second_running_timer(client, db):
    case_id = new_case(client, "TEST-ONLY 计时")
    case_uuid = uuid.UUID(case_id)
    started = client.post(f"/api/v1/cases/{case_id}/time/start",
                          json={"case_id": case_id, "node_title": "起草起诉状"})
    assert started.status_code == 200, started.text
    entry = started.json()
    assert entry["source"] == "timer" and entry["stopped_at"] is None and entry["hours"] == 0
    again = client.post(f"/api/v1/cases/{case_id}/time/start",
                        json={"case_id": case_id, "node_title": "另一项"})
    assert again.status_code == 422
    # 把开始时间提前 16 分钟：960s 向上取整到 2 个 15 分钟 = 0.5h
    row = db.get(models.TimeEntry, uuid.UUID(entry["id"]))
    row.started_at = datetime.now(timezone.utc) - timedelta(minutes=16)
    db.commit()
    stopped = client.post(f"/api/v1/cases/{case_id}/time/entries/{entry['id']}/stop",
                          json={"case_id": case_id, "entry_id": entry["id"]})
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["hours"] == 0.5
    # 已停止的计时不能再停
    twice = client.post(f"/api/v1/cases/{case_id}/time/entries/{entry['id']}/stop",
                        json={"case_id": case_id, "entry_id": entry["id"]})
    assert twice.status_code == 422


def test_manual_entry_validation_day_view_and_summary(client, db):
    case_id = new_case(client, "TEST-ONLY 补录")
    today = date.today().isoformat()
    ok = client.post(f"/api/v1/cases/{case_id}/time/entries", json={
        "case_id": case_id, "node_title": "阅卷", "hours": 1.5, "work_date": today})
    assert ok.status_code == 200, ok.text
    bad = client.post(f"/api/v1/cases/{case_id}/time/entries", json={
        "case_id": case_id, "node_title": "阅卷", "hours": 0.3, "work_date": today})
    assert bad.status_code == 422
    assert bad.json()["code"] == "invalid_input"
    day = client.get(f"/api/v1/cases/{case_id}/time?work_date={today}").json()
    assert day["total_hours"] == 1.5
    assert day["summary"] == [{"title": "阅卷", "hours": 1.5, "entries": 1}]
    # 软删除后不计入
    entry_id = ok.json()["id"]
    removed = client.post(f"/api/v1/cases/{case_id}/time/entries/{entry_id}/remove",
                          json={"case_id": case_id, "entry_id": entry_id})
    assert removed.status_code == 200, removed.text
    day = client.get(f"/api/v1/cases/{case_id}/time?work_date={today}").json()
    assert day["total_hours"] == 0
    assert day["entries"] == []
    assert day["summary"] == []


def test_day_view_includes_running_timer_but_summary_excludes_it(client, db):
    """日视图含进行中的计时（界面据此显示「计时中」），标题汇总只计已完成/补录。"""
    case_id = new_case(client, "TEST-ONLY 计时汇总")
    today = date.today().isoformat()
    added = client.post(f"/api/v1/cases/{case_id}/time/entries", json={
        "case_id": case_id, "node_title": "阅卷", "hours": 0.5, "work_date": today})
    assert added.status_code == 200, added.text
    started = client.post(f"/api/v1/cases/{case_id}/time/start",
                          json={"case_id": case_id, "node_title": "起草"})
    assert started.status_code == 200, started.text
    day = client.get(f"/api/v1/cases/{case_id}/time?work_date={today}").json()
    assert [entry["node_title"] for entry in day["entries"]] == ["阅卷", "起草"]
    assert day["total_hours"] == 0.5
    assert day["summary"] == [{"title": "阅卷", "hours": 0.5, "entries": 1}]


def test_manual_entry_does_not_block_timer_or_archive(client, db):
    """补录条目没有 stopped_at，不能被当成「进行中的计时」。"""
    case_id = new_case(client, "TEST-ONLY 补录不挡计时")
    today = date.today().isoformat()
    added = client.post(f"/api/v1/cases/{case_id}/time/entries", json={
        "case_id": case_id, "node_title": "阅卷", "hours": 0.25, "work_date": today})
    assert added.status_code == 200, added.text
    started = client.post(f"/api/v1/cases/{case_id}/time/start",
                          json={"case_id": case_id, "node_title": "起草"})
    assert started.status_code == 200, started.text
    entry_id = started.json()["id"]
    stopped = client.post(f"/api/v1/cases/{case_id}/time/entries/{entry_id}/stop",
                          json={"case_id": case_id, "entry_id": entry_id})
    assert stopped.status_code == 200, stopped.text
    # 有补录条目时归档不应被拦
    overview = _overview(client, case_id)
    archived = client.post(f"/api/v1/cases/{case_id}/archive",
                           json={"case_id": case_id, "expected_revision": overview["revision"]})
    assert archived.status_code == 200, archived.text


def test_stop_manual_entry_is_rejected(client, db):
    """补录工时不能被「停止」改写时长。"""
    case_id = new_case(client, "TEST-ONLY 停止补录")
    today = date.today().isoformat()
    added = client.post(f"/api/v1/cases/{case_id}/time/entries", json={
        "case_id": case_id, "node_title": "阅卷", "hours": 0.25, "work_date": today})
    entry_id = added.json()["id"]
    stopped = client.post(f"/api/v1/cases/{case_id}/time/entries/{entry_id}/stop",
                          json={"case_id": case_id, "entry_id": entry_id})
    assert stopped.status_code == 422
    assert "补录" in stopped.json()["message"]
    day = client.get(f"/api/v1/cases/{case_id}/time?work_date={today}").json()
    assert day["entries"][0]["hours"] == 0.25
    assert day["entries"][0]["stopped_at"] is None


def test_archive_and_time_audit_rows(client, db):
    case_id = new_case(client, "TEST-ONLY 审计")
    case_uuid = uuid.UUID(case_id)
    started = client.post(f"/api/v1/cases/{case_id}/time/start",
                          json={"case_id": case_id, "node_title": "起草"})
    entry_id = started.json()["id"]
    client.post(f"/api/v1/cases/{case_id}/time/entries/{entry_id}/stop",
                json={"case_id": case_id, "entry_id": entry_id})
    # 补录与删除（time.add / time.remove）
    today = date.today().isoformat()
    added = client.post(f"/api/v1/cases/{case_id}/time/entries", json={
        "case_id": case_id, "node_title": "阅卷", "hours": 0.25, "work_date": today})
    manual_id = added.json()["id"]
    client.post(f"/api/v1/cases/{case_id}/time/entries/{manual_id}/remove",
                json={"case_id": case_id, "entry_id": manual_id})
    overview = _overview(client, case_id)
    client.post(f"/api/v1/cases/{case_id}/archive",
                json={"case_id": case_id, "expected_revision": overview["revision"]})
    overview = _overview(client, case_id)
    client.post(f"/api/v1/cases/{case_id}/unarchive",
                json={"case_id": case_id, "expected_revision": overview["revision"]})
    rows = db.execute(select(models.AuditEvent).where(
        models.AuditEvent.case_id == case_uuid)).scalars().all()
    ops = {row.operation for row in rows}
    # 0006 新增 6 类审计操作全部落库（独立复核建议补齐）
    assert {"time.start", "time.stop", "time.add", "time.remove",
            "case.archive", "case.unarchive"} <= ops


def test_archived_case_rejects_upload_and_review_commands(client, db):
    """独立复核整改：网页上传与确认/撤回同样受归档守卫约束。"""
    from .test_api import upload
    case_id = new_case(client, "TEST-ONLY 归档上传")
    case_uuid = uuid.UUID(case_id)
    # 先留一条 Agent 待确认变更（CLI 口径：actor=agent）
    snap = progress.add_node(db, AddNodeInput(
        case_id=case_uuid, parent_id=None, kind="stage", title="归档前节点",
        request_key="arch-review-1"), actor="agent", entry="cli")
    change_id = str(snap.last_change_id)
    overview = _overview(client, case_id)
    archived = client.post(f"/api/v1/cases/{case_id}/archive",
                           json={"case_id": case_id, "expected_revision": overview["revision"]})
    assert archived.status_code == 200, archived.text
    # 网页上传被拒，且无批次/材料/任务残留
    resp = upload(client, case_id, [("归档后上传.txt", b"TEST-ONLY archived upload")])
    assert resp.status_code == 422
    assert "归档" in resp.json()["message"]
    assert db.execute(select(models.IntakeItem).where(
        models.IntakeItem.case_id == case_uuid)).scalars().all() == []
    assert db.execute(select(models.Job).where(
        models.Job.case_id == case_uuid)).scalars().all() == []
    # 确认/一键确认/撤回被拒
    r1 = client.post(f"/api/v1/cases/{case_id}/progress/changes/{change_id}/accept",
                     json={"case_id": case_id, "change_id": change_id})
    assert r1.status_code == 422 and "归档" in r1.json()["message"]
    r2 = client.post(f"/api/v1/cases/{case_id}/progress/changes/accept-all",
                     json={"case_id": case_id, "change_ids": [str(change_id)]})
    assert r2.status_code == 422 and "归档" in r2.json()["message"]
    r3 = client.post(f"/api/v1/cases/{case_id}/progress/changes/{change_id}/revert",
                     json={"case_id": case_id, "change_id": change_id})
    assert r3.status_code == 422 and "归档" in r3.json()["message"]
    # 变更仍为 pending，节点未被改动
    after = progress.get_progress(db, case_uuid)
    assert [c.review_state for c in after.changes if str(c.id) == str(change_id)] == ["pending"]
    # get_progress 为自洽快照持有序号行 FOR SHARE；同一事务里后续再经 HTTP
    # （另一条连接）确认会等这把锁。生产请求各自结束事务，测试里显式释放读事务。
    db.rollback()
    # 取消归档后上传与确认恢复
    overview = _overview(client, case_id)
    client.post(f"/api/v1/cases/{case_id}/unarchive",
                json={"case_id": case_id, "expected_revision": overview["revision"]})
    ok = upload(client, case_id, [("取消归档后上传.txt", b"TEST-ONLY unarchived upload")])
    assert ok.status_code == 200, ok.text
    r4 = client.post(f"/api/v1/cases/{case_id}/progress/changes/{change_id}/accept",
                     json={"case_id": case_id, "change_id": change_id})
    assert r4.status_code == 200, r4.text


def test_time_service_rejects_cross_case_entry(client, db):
    first = new_case(client, "TEST-ONLY 工时甲")
    second = new_case(client, "TEST-ONLY 工时乙")
    started = client.post(f"/api/v1/cases/{first}/time/start",
                          json={"case_id": first, "node_title": "起草"})
    entry_id = started.json()["id"]
    # 用另一案的路径停止本案计时：路径与 body 一致但 entry 不属于该案
    resp = client.post(f"/api/v1/cases/{second}/time/entries/{entry_id}/stop",
                       json={"case_id": second, "entry_id": entry_id})
    assert resp.status_code == 404
    # 服务层同样拒绝
    assert _codes(lambda: time_entries.stop_timer(
        db, StopTimerInput(case_id=uuid.UUID(second), entry_id=uuid.UUID(entry_id)))
        ) == ErrorCode.NOT_FOUND
