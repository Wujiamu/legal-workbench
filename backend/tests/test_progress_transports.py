"""P3 progress transports: HTTP (human), MCP and CLI (agent) over the same service.

Verifies actor is fixed by the entry point, write commands return full snapshots,
conflicts are 409, agent changes are pending, and accept/revert are absent from
agent transports. Real PostgreSQL; TEST-ONLY data.
"""
from __future__ import annotations

import uuid

import pytest

from legal_workbench import progress
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.schemas import (
    AddNodeInput, AddRelationInput, ChangeIdInput, LinkNodeInput, LinkTarget, SetActiveStageInput,
)

from .factories import build_material, create_case
from .test_api import new_case

# 访问围栏由 test_agent_access_gate.py 覆盖；本文件显式放开以验证工具业务行为。
pytestmark = pytest.mark.usefixtures("agent_access_open")


def _add(session, case_id, *, parent_id=None, kind="stage", title="阶段", state=None):
    return progress.add_node(session, AddNodeInput(
        case_id=case_id, parent_id=parent_id, kind=kind, title=title, state=state),
        actor="human", entry="local-web")


def _node(snapshot, title):
    return next(n for n in snapshot.nodes if n.title == title)


def _codes(fn):
    with pytest.raises(WorkbenchError) as exc:
        fn()
    return exc.value.code


def _progress(client, case_id):
    return client.get(f"/api/v1/cases/{case_id}/progress")


def test_http_rules_and_snapshot(client):
    rules = client.get("/api/v1/progress/rules")
    assert rules.status_code == 200
    assert rules.json()["rules"]["default_fragment"] == "litigation-default"
    case_id = new_case(client, "TEST-ONLY HTTP")
    snap = _progress(client, case_id)
    assert snap.status_code == 200
    body = snap.json()
    assert {"诉前准备", "一审"} <= {n["title"] for n in body["nodes"]}
    seq = client.get(f"/api/v1/cases/{case_id}/changes/seq")
    assert seq.json()["seqs"]["progress"] >= 1


def test_http_add_node_returns_snapshot_and_actor_human(client):
    case_id = new_case(client, "TEST-ONLY 写")
    snap = _progress(client, case_id).json()
    program = next(n for n in snap["nodes"] if n["title"] == "一审")
    resp = client.post(f"/api/v1/cases/{case_id}/progress/nodes", json={
        "case_id": case_id, "parent_id": program["id"], "kind": "work", "title": "证据整理"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    added = next(n for n in body["nodes"] if n["title"] == "证据整理")
    assert added["created_by"] == "human"
    # human writes are not pending review
    assert body["pending_changes"] == []
    assert body["changes"][-1]["entry"] == "local-web"


def test_http_conflict_is_409(client):
    case_id = new_case(client, "TEST-ONLY 冲突")
    snap = _progress(client, case_id).json()
    node = next(n for n in snap["nodes"] if n["title"] == "立案")
    url = f"/api/v1/cases/{case_id}/progress/nodes/{node['id']}"
    payload = {"case_id": case_id, "node_id": node["id"], "expected_revision": node["revision"],
               "changes": {"note": "第一次"}}
    assert client.patch(url, json=payload).status_code == 200
    # stale revision -> 409
    stale = client.patch(url, json=payload)
    assert stale.status_code == 409
    assert stale.json()["code"] == "conflict"


def test_http_case_mismatch_path_vs_payload(client):
    a = new_case(client, "TEST-ONLY A")
    b = new_case(client, "TEST-ONLY B")
    resp = client.post(f"/api/v1/cases/{a}/progress/nodes", json={
        "case_id": b, "kind": "stage", "title": "错案"})
    assert resp.status_code == 404
    assert resp.json()["code"] == "case_mismatch"


def test_http_accept_and_revert_human_only_flow(client):
    case_id = new_case(client, "TEST-ONLY 复核")
    snap = _progress(client, case_id).json()
    program = next(n for n in snap["nodes"] if n["title"] == "一审")
    hearing = next(n for n in snap["nodes"] if n["title"] == "庭审")
    # agent writes via service (MCP/CLI path) -> pending
    from legal_workbench.db import get_session_factory
    session = get_session_factory()()
    try:
        progress.set_active_stage(session, SetActiveStageInput(
            case_id=uuid.UUID(case_id), stage_id=uuid.UUID(hearing["id"]),
            expected_revision=hearing["revision"]), actor="agent", entry="mcp")
    finally:
        session.close()
    snap = _progress(client, case_id).json()
    assert len(snap["pending_changes"]) == 1
    change = snap["pending_changes"][0]
    assert change["actor"] == "agent"
    accepted = client.post(
        f"/api/v1/cases/{case_id}/progress/changes/{change['id']}/accept",
        json={"case_id": case_id, "change_id": change["id"]})
    assert accepted.status_code == 200
    assert accepted.json()["pending_changes"] == []
    reverted = client.post(
        f"/api/v1/cases/{case_id}/progress/changes/{change['id']}/revert",
        json={"case_id": case_id, "change_id": change["id"]})
    assert reverted.status_code == 200
    assert next(n for n in reverted.json()["nodes"] if n["title"] == "庭审")["state"] == "not_started"


def test_http_accept_all_empty_set_and_stale_seq(client):
    """审查末节：HTTP 入口验证显式空集拒绝、省略 IDs 无序号拒绝、陈旧序号冲突。"""
    case_id = new_case(client, "TEST-ONLY 批量确认")
    snap = _progress(client, case_id).json()
    program = next(n for n in snap["nodes"] if n["title"] == "一审")
    from legal_workbench.db import get_session_factory
    session = get_session_factory()()
    try:
        progress.add_node(session, AddNodeInput(
            case_id=uuid.UUID(case_id), parent_id=uuid.UUID(program["id"]),
            kind="stage", title="甲", request_key="a"), actor="agent", entry="mcp")
    finally:
        session.close()
    shown_ids = [c["id"] for c in _progress(client, case_id).json()["pending_changes"]]
    assert len(shown_ids) == 1
    # 显式空集合 → 422
    empty = client.post(f"/api/v1/cases/{case_id}/progress/changes/accept-all",
                        json={"case_id": case_id, "change_ids": []})
    assert empty.status_code == 422
    assert empty.json()["code"] == "invalid_input"
    # 省略 change_ids 且无 expected_seq → 422
    no_seq = client.post(f"/api/v1/cases/{case_id}/progress/changes/accept-all",
                         json={"case_id": case_id})
    assert no_seq.status_code == 422
    assert no_seq.json()["code"] == "invalid_input"
    # 陈旧序号 → 409
    stale = client.post(f"/api/v1/cases/{case_id}/progress/changes/accept-all",
                        json={"case_id": case_id, "change_ids": shown_ids, "expected_seq": 0})
    assert stale.status_code == 409
    assert stale.json()["code"] == "conflict"


def test_http_accept_all_does_not_confirm_unseen_item(client):
    """审查末节/审查 04：只确认已展示子集，其间新增的待审项仍待确认。"""
    case_id = new_case(client, "TEST-ONLY 未展示")
    snap = _progress(client, case_id).json()
    program = next(n for n in snap["nodes"] if n["title"] == "一审")
    from legal_workbench.db import get_session_factory
    session = get_session_factory()()
    try:
        progress.add_node(session, AddNodeInput(
            case_id=uuid.UUID(case_id), parent_id=uuid.UUID(program["id"]),
            kind="stage", title="甲", request_key="a"), actor="agent", entry="mcp")
    finally:
        session.close()
    shown_ids = [c["id"] for c in _progress(client, case_id).json()["pending_changes"]]
    # 其间 Agent 又写入一条新待审
    session = get_session_factory()()
    try:
        progress.add_node(session, AddNodeInput(
            case_id=uuid.UUID(case_id), parent_id=uuid.UUID(program["id"]),
            kind="stage", title="乙", request_key="b"), actor="agent", entry="mcp")
    finally:
        session.close()
    resp = client.post(f"/api/v1/cases/{case_id}/progress/changes/accept-all",
                       json={"case_id": case_id, "change_ids": shown_ids})
    assert resp.status_code == 200
    remaining = [c["id"] for c in resp.json()["pending_changes"]]
    assert len(remaining) == 1
    assert remaining[0] not in shown_ids


def test_mcp_tools_agent_actor_and_no_accept_revert():
    from legal_workbench import mcp_server
    import asyncio
    tools = asyncio.run(mcp_server.server.list_tools())
    names = {t.name for t in tools}
    assert {"get_progress", "get_progress_rules", "progress_add_node",
            "progress_set_active_stage", "progress_insert_fragment"} <= names
    # 审查：P3 入口覆盖——移除关系、解除关联、分析/标注关联都在 MCP 暴露
    assert {"progress_remove_relation", "progress_unlink",
            "progress_link_analysis", "progress_link_annotation"} <= names
    # accept/revert must NOT be exposed to agents
    assert not any("accept" in n or "revert" in n for n in names)


def test_cli_full_progress_command_coverage(db):
    """审查：P3 入口覆盖——CLI 提供普通编辑、移动、删除、关系与业务关联。"""
    from legal_workbench import cli, cases
    from legal_workbench.db import get_session_factory
    session = get_session_factory()()
    try:
        created = cases.create_case(session, title="TEST-ONLY CLI 全覆盖")
        case_id = str(created.case_id)
    finally:
        session.close()
    parser = cli.build_parser()
    # 新建两个程序节点
    r1 = cli.dispatch(parser.parse_args([
        "progress-add-node", "--case-id", case_id, "--kind", "program",
        "--title", "一审", "--request-key", "c1"]))
    a = next(n for n in r1.nodes if n.title == "一审")
    r2 = cli.dispatch(parser.parse_args([
        "progress-add-node", "--case-id", case_id, "--kind", "program",
        "--title", "二审", "--request-key", "c2"]))
    b = next(n for n in r2.nodes if n.title == "二审")
    # 普通编辑
    upd = cli.dispatch(parser.parse_args([
        "progress-update-node", "--case-id", case_id, "--node-id", str(a.id),
        "--expected-revision", str(a.revision), "--note", "CLI 备注"]))
    assert next(n for n in upd.nodes if n.id == a.id).note == "CLI 备注"
    # 建立关系
    rel = cli.dispatch(parser.parse_args([
        "progress-add-relation", "--case-id", case_id, "--from-node-id", str(a.id),
        "--to-node-id", str(b.id), "--relation-type", "causes"]))
    assert len(rel.relations) == 1
    relation_id = str(rel.relations[0].id)
    # 移除关系
    rm = cli.dispatch(parser.parse_args([
        "progress-remove-relation", "--case-id", case_id, "--relation-id", relation_id]))
    assert rm.relations[0].removed_at is not None
    # 移动（把二审移到一审下不合法，改用根级重排：移到自身根级 before 一审）
    b_fresh = next(n for n in rm.nodes if n.id == b.id)
    mv = cli.dispatch(parser.parse_args([
        "progress-move-node", "--case-id", case_id, "--node-id", str(b.id),
        "--expected-revision", str(b_fresh.revision), "--before-node-id", str(a.id)]))
    roots = sorted([n for n in mv.nodes if n.parent_id is None], key=lambda n: n.position)
    titles = [n.title for n in roots]
    assert titles.index("二审") < titles.index("一审")  # 二审移到一审之前
    # 删除（二审无子节点，可删）
    b_now = next(n for n in mv.nodes if n.id == b.id)
    rmnode = cli.dispatch(parser.parse_args([
        "progress-remove-node", "--case-id", case_id, "--node-id", str(b.id),
        "--expected-revision", str(b_now.revision)]))
    assert next(n for n in rmnode.nodes if n.id == b.id).removed_at is not None
    # CLI 写命令 actor=agent entry=cli（建案自动初始化的默认片段是 human/local-web，排除）
    cli_changes = [c for c in rmnode.changes if c.command != "insert_fragment"]
    assert cli_changes and all(c.entry == "cli" and c.actor == "agent" for c in cli_changes)


def test_cli_progress_commands_agent_actor(db):
    from legal_workbench import cli
    from legal_workbench.db import get_session_factory
    import argparse
    case_id = None
    session = get_session_factory()()
    try:
        from legal_workbench import cases
        created = cases.create_case(session, title="TEST-ONLY CLI")
        case_id = str(created.case_id)
    finally:
        session.close()
    args = cli.build_parser().parse_args([
        "progress-add-node", "--case-id", case_id, "--kind", "stage",
        "--title", "CLI阶段", "--request-key", "cli-1"])
    result = cli.dispatch(args)
    added = next(n for n in result.nodes if n.title == "CLI阶段")
    assert added.created_by == "agent"
    assert added.request_key == "cli-1"
    # pending review because actor=agent
    assert len(result.pending_changes) == 1
    assert result.pending_changes[0].entry == "cli"
    # read back
    read = cli.dispatch(cli.build_parser().parse_args(["progress", "--case-id", case_id]))
    assert "CLI阶段" in {n.title for n in read.nodes}


def test_mcp_update_node_partial_fields_and_clear_note(db):
    """审查 N1：MCP progress_update_node 只纳入实际提供字段；clear_note 显式清空备注。"""
    from legal_workbench import mcp_server, cases
    from legal_workbench.db import get_session_factory
    session = get_session_factory()()
    try:
        created = cases.create_case(session, title="TEST-ONLY MCP 部分更新")
        case_id = str(created.case_id)
    finally:
        session.close()
    snap = mcp_server.get_progress(case_id)
    node = next(n for n in snap["nodes"] if n["title"] == "立案")
    # 先设备注
    mcp_server.progress_update_node(case_id, node["id"], node["revision"], note="原始备注")
    snap = mcp_server.get_progress(case_id)
    node = next(n for n in snap["nodes"] if n["id"] == node["id"])
    assert node["note"] == "原始备注"
    # 只改标题：不应被「种类没有状态：None」拒绝，备注保留
    result = mcp_server.progress_update_node(case_id, node["id"], node["revision"], title="立案（改）")
    updated = next(n for n in result["nodes"] if n["id"] == node["id"])
    assert updated["title"] == "立案（改）"
    assert updated["note"] == "原始备注"  # 未提供 note 不动
    # 改状态不清空备注
    result = mcp_server.progress_update_node(case_id, node["id"], updated["revision"], state="in_progress")
    updated = next(n for n in result["nodes"] if n["id"] == node["id"])
    assert updated["state"] == "in_progress"
    assert updated["note"] == "原始备注"
    # clear_note 显式清空
    result = mcp_server.progress_update_node(case_id, node["id"], updated["revision"], clear_note=True)
    updated = next(n for n in result["nodes"] if n["id"] == node["id"])
    assert updated["note"] is None


def test_mcp_insert_fragment_before_node(db):
    """审查 N2：MCP progress_insert_fragment 支持 before_node_id。"""
    from legal_workbench import mcp_server, cases
    from legal_workbench.db import get_session_factory
    session = get_session_factory()()
    try:
        created = cases.create_case(session, title="TEST-ONLY MCP 片段位置")
        case_id = str(created.case_id)
    finally:
        session.close()
    snap = mcp_server.get_progress(case_id)
    pretrial = next(n for n in snap["nodes"] if n["title"] == "诉前准备")
    result = mcp_server.progress_insert_fragment(case_id, "second-instance", before_node_id=pretrial["id"])
    roots = sorted([n for n in result["nodes"] if n["parent_id"] is None], key=lambda n: n["position"])
    titles = [n["title"] for n in roots]
    assert titles.index("二审") < titles.index("诉前准备")


def test_write_response_carries_last_change_id(db):
    """审查 N3：写命令响应携带本次变更 ID，前端按此入撤销栈。"""
    from legal_workbench import progress
    from legal_workbench.schemas import AddNodeInput
    case_id = create_case(db)
    db.commit()
    snap = progress.add_node(db, AddNodeInput(case_id=case_id, kind="stage", title="甲"),
                             actor="human", entry="local-web")
    assert snap.last_change_id is not None
    own = next(c for c in snap.changes if c.id == snap.last_change_id)
    assert own.command == "add_node"
    # 读快照不带 last_change_id
    read = progress.get_progress(db, case_id)
    assert read.last_change_id is None


def test_revert_remove_restores_position_without_collision(db):
    """审查 N4/R3-1：撤回 remove_node 还原旧 position，期间同级压缩则让位，
    不产生重复位置，且让位同级记入 reverse_diff（历史可追溯、二次撤回可还原）。"""
    from legal_workbench import progress
    from legal_workbench.schemas import MoveNodeInput, NodeRevisionInput
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="甲")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="乙")
    jia = _node(snap, "甲")
    yi = _node(snap, "乙")
    # 删除甲（position 0）
    snap = progress.remove_node(db, NodeRevisionInput(
        case_id=case_id, node_id=jia.id, expected_revision=jia.revision), actor="human", entry="local-web")
    remove_change = next(c for c in snap.changes if c.command == "remove_node")
    # 乙移到 position 0（压缩）
    yi_now = _node(snap, "乙")
    progress.move_node(db, MoveNodeInput(case_id=case_id, node_id=yi.id,
                                         expected_revision=yi_now.revision, new_parent_id=program.id,
                                         before_node_id=None), actor="human", entry="local-web")
    # 撤回删除甲 → 甲还原 position 0，乙让位到 1，不重复
    snap = progress.revert_change(db, ChangeIdInput(case_id=case_id, change_id=remove_change.id),
                                  actor="human", entry="local-web")
    siblings = sorted([n for n in snap.nodes if n.parent_id == program.id and n.removed_at is None],
                      key=lambda n: n.position)
    positions = [n.position for n in siblings]
    assert len(positions) == len(set(positions))  # 无重复
    assert {n.title for n in siblings} == {"甲", "乙"}
    # R3-1：让位同级乙记入 reverse_diff
    revert_change = next(c for c in snap.changes if c.command == "revert_change")
    revert_ids = {d.id for d in revert_change.diff}
    assert yi.id in revert_ids  # 让位者入 diff
    assert jia.id in revert_ids  # 被恢复者入 diff
    # R4-3：断言让位条目 before/after/rev 数值（乙 position 0→1，revision+1）
    yi_entry = next(d for d in revert_change.diff if d.id == yi.id)
    assert yi_entry.before["position"] == 0 and yi_entry.after["position"] == 1
    assert yi_entry.after_rev == yi_entry.before_rev + 1
    # R4-3：二次撤回（撤回该撤回）还原让位者位置、甲重新软删
    snap2 = progress.revert_change(db, ChangeIdInput(case_id=case_id, change_id=revert_change.id),
                                   actor="human", entry="local-web")
    siblings2 = sorted([n for n in snap2.nodes if n.parent_id == program.id and n.removed_at is None],
                       key=lambda n: n.position)
    assert [n.title for n in siblings2] == ["乙"]
    assert siblings2[0].position == 0


def test_revert_remove_restores_in_progress_without_self_cascade(db):
    """审查 R4-2：撤回删除恢复原进行中阶段，期间另一阶段已置进行中，
    恢复者保持 in_progress、另一阶段联动 completed，恰好一个进行中（不自联动抵消）。"""
    from legal_workbench import progress
    from legal_workbench.schemas import NodeRevisionInput, SetActiveStageInput
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="庭前程序", state="in_progress")
    pretrial = _node(snap, "庭前程序")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="庭审")
    hearing = _node(snap, "庭审")
    # 删除进行中的庭前程序
    snap = progress.remove_node(db, NodeRevisionInput(
        case_id=case_id, node_id=pretrial.id, expected_revision=pretrial.revision),
        actor="human", entry="local-web")
    remove_change = next(c for c in snap.changes if c.command == "remove_node")
    # 期间把庭审设为进行中（不触碰被删节点 revision）
    hearing_now = _node(snap, "庭审")
    progress.set_active_stage(db, SetActiveStageInput(
        case_id=case_id, stage_id=hearing.id, expected_revision=hearing_now.revision),
        actor="human", entry="local-web")
    # 撤回删除庭前程序 → 庭前程序恢复 in_progress，庭审联动 completed
    snap = progress.revert_change(db, ChangeIdInput(case_id=case_id, change_id=remove_change.id),
                                  actor="human", entry="local-web")
    stages = [n for n in snap.nodes if n.kind == "stage" and n.removed_at is None]
    active = [n for n in stages if n.state == "in_progress"]
    assert len(active) == 1
    assert active[0].title == "庭前程序"  # 恢复者保持进行中，不被自联动抵消
    assert next(n for n in stages if n.title == "庭审").state == "completed"


def test_revert_update_node_restores_fields_without_cascade(db):
    """审查 R5-2：普通编辑（update_node）撤回还原字段，同程序其他进行中阶段不被误联动。"""
    from legal_workbench import progress
    from legal_workbench.schemas import NodeChanges, UpdateNodeInput
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="立案", state="in_progress")
    filing = _node(snap, "立案")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="庭审")
    hearing = _node(snap, "庭审")
    # Agent 编辑庭审备注（pending）
    snap = progress.update_node(db, UpdateNodeInput(
        case_id=case_id, node_id=hearing.id, expected_revision=hearing.revision,
        changes=NodeChanges(note="TEST-ONLY 待开庭")), actor="agent", entry="mcp")
    edit_change = next(c for c in snap.changes if c.command == "update_node")
    # 人工撤回 → 备注还原 null，立案保持 in_progress 不被误联动
    snap = progress.revert_change(db, ChangeIdInput(case_id=case_id, change_id=edit_change.id),
                                  actor="human", entry="local-web")
    assert _node(snap, "庭审").note is None
    assert _node(snap, "立案").state == "in_progress"
    stages = [n for n in snap.nodes if n.kind == "stage" and n.removed_at is None]
    assert len([n for n in stages if n.state == "in_progress"]) == 1


def test_revert_remove_yielding_sibling_in_progress_cascades(db):
    """审查 R4-2 边界：让位同级原本进行中，既让位又被联动，恰好一个进行中，
    该节点 reverse_diff 含两条（让位 position＋联动 state）。"""
    from legal_workbench import progress
    from legal_workbench.schemas import AddNodeInput, NodeRevisionInput, SetActiveStageInput
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="庭前程序", state="in_progress")
    pretrial = _node(snap, "庭前程序")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="庭审")
    hearing = _node(snap, "庭审")
    # 删除进行中的庭前程序（position 0）→ 庭审压缩到 0
    snap = progress.remove_node(db, NodeRevisionInput(
        case_id=case_id, node_id=pretrial.id, expected_revision=pretrial.revision),
        actor="human", entry="local-web")
    remove_change = next(c for c in snap.changes if c.command == "remove_node")
    # 期间把庭审设为进行中
    hearing_now = _node(snap, "庭审")
    progress.set_active_stage(db, SetActiveStageInput(
        case_id=case_id, stage_id=hearing.id, expected_revision=hearing_now.revision),
        actor="human", entry="local-web")
    # 新增裁判插到 position 0 → 庭审让位到 1，仍 in_progress
    snap = progress.add_node(db, AddNodeInput(
        case_id=case_id, parent_id=program.id, kind="stage", title="裁判", position=0),
        actor="human", entry="local-web")
    # 撤回删除庭前程序 → 庭前程序回 0 恢复 in_progress；庭审让位且被联动 completed
    snap = progress.revert_change(db, ChangeIdInput(case_id=case_id, change_id=remove_change.id),
                                  actor="human", entry="local-web")
    stages = [n for n in snap.nodes if n.kind == "stage" and n.removed_at is None]
    active = [n for n in stages if n.state == "in_progress"]
    assert len(active) == 1
    assert active[0].title == "庭前程序"
    assert next(n for n in stages if n.title == "庭审").state == "completed"
    revert_change = next(c for c in snap.changes if c.command == "revert_change")
    hearing_entries = [d for d in revert_change.diff if d.id == hearing.id]
    assert len(hearing_entries) >= 2  # 让位＋联动两条


def test_revert_revert_softdelete_with_child_refused(db):
    """审查 N4：「撤回的撤回」软删现有节点时其下已有有效子节点则整笔拒绝。"""
    from legal_workbench import progress
    from legal_workbench.schemas import NodeRevisionInput
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="庭审")
    hearing = _node(snap, "庭审")
    # 人工删除庭审
    snap = progress.remove_node(db, NodeRevisionInput(
        case_id=case_id, node_id=hearing.id, expected_revision=hearing.revision), actor="human", entry="local-web")
    remove_change = next(c for c in snap.changes if c.command == "remove_node")
    # 撤回删除 → 庭审恢复
    snap = progress.revert_change(db, ChangeIdInput(case_id=case_id, change_id=remove_change.id),
                                  actor="human", entry="local-web")
    revert_change = next(c for c in snap.changes if c.command == "revert_change")
    hearing_now = _node(snap, "庭审")
    # 在恢复的庭审下新增子工作
    _add(db, case_id, parent_id=hearing_now.id, kind="work", title="出庭准备")
    # 再撤回该撤回（逆操作软删庭审）→ 其下已有子节点，整笔拒绝
    assert _codes(lambda: progress.revert_change(
        db, ChangeIdInput(case_id=case_id, change_id=revert_change.id),
        actor="human", entry="local-web")) == ErrorCode.CONFLICT
    final = progress.get_progress(db, case_id)
    assert _node(final, "庭审").removed_at is None
    assert _node(final, "出庭准备").removed_at is None


def test_revert_remove_relation_endpoint_removed_refused(db):
    """审查 N5：撤回移除关系时端点已软删则拒绝恢复，避免悬空关系。"""
    from legal_workbench import progress
    from legal_workbench.schemas import NodeRevisionInput, RemoveRelationInput
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    a = _node(snap, "一审")
    snap = _add(db, case_id, kind="program", title="二审")
    b = _node(snap, "二审")
    snap = progress.add_relation(db, AddRelationInput(
        case_id=case_id, from_node_id=a.id, to_node_id=b.id, relation_type="causes"),
        actor="human", entry="local-web")
    rel = snap.relations[0]
    snap = progress.remove_relation(db, RemoveRelationInput(case_id=case_id, relation_id=rel.id),
                                    actor="human", entry="local-web")
    remove_change = next(c for c in snap.changes if c.command == "remove_relation")
    # 软删端点二审
    b_now = _node(snap, "二审")
    progress.remove_node(db, NodeRevisionInput(case_id=case_id, node_id=b.id, expected_revision=b_now.revision),
                         actor="human", entry="local-web")
    # 撤回移除关系 → 端点已删，拒绝
    assert _codes(lambda: progress.revert_change(
        db, ChangeIdInput(case_id=case_id, change_id=remove_change.id),
        actor="human", entry="local-web")) == ErrorCode.CONFLICT


def test_revert_unlink_node_removed_refused(db):
    """审查 N5：撤回解除关联时节点已软删则拒绝恢复，避免悬空关联。"""
    from legal_workbench import progress
    from legal_workbench.schemas import NodeRevisionInput, UnlinkInput
    case_id = create_case(db)
    db.commit()
    built = build_material(db, case_id=case_id)
    db.commit()
    snap = _add(db, case_id, kind="stage", title="证据")
    node = _node(snap, "证据")
    snap = progress.link_node(db, LinkNodeInput(
        case_id=case_id, node_id=node.id, target=LinkTarget(material_id=built["material_id"])),
        actor="human", entry="local-web")
    link = snap.links[0]
    snap = progress.unlink_node(db, UnlinkInput(case_id=case_id, link_id=link.id),
                                actor="human", entry="local-web")
    unlink_change = next(c for c in snap.changes if c.command == "unlink")
    # 软删节点
    node_now = _node(snap, "证据")
    progress.remove_node(db, NodeRevisionInput(case_id=case_id, node_id=node.id, expected_revision=node_now.revision),
                         actor="human", entry="local-web")
    assert _codes(lambda: progress.revert_change(
        db, ChangeIdInput(case_id=case_id, change_id=unlink_change.id),
        actor="human", entry="local-web")) == ErrorCode.CONFLICT


def test_add_node_shifted_siblings_recorded_and_reverted(db):
    """审查 N6：add_node 让位同级记入 diff，撤回新建时整体还原位置。"""
    from legal_workbench import progress
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="stage", title="甲")
    snap = _add(db, case_id, kind="stage", title="乙")
    jia = _node(snap, "甲")
    # 在 position 0 插入新节点，甲乙让位
    snap = progress.add_node(db, AddNodeInput(case_id=case_id, kind="stage", title="丙", position=0),
                             actor="human", entry="local-web")
    add_change = next(c for c in snap.changes if c.command == "add_node" and c.diff[0].after["title"] == "丙")
    # 让位同级入 diff
    shifted_ids = {d.id for d in add_change.diff if d.before is not None}
    assert jia.id in shifted_ids
    roots = sorted([n for n in snap.nodes if n.parent_id is None and n.removed_at is None], key=lambda n: n.position)
    assert [n.title for n in roots] == ["丙", "甲", "乙"]
    # 撤回新建丙 → 甲乙位置还原
    snap = progress.revert_change(db, ChangeIdInput(case_id=case_id, change_id=add_change.id),
                                  actor="human", entry="local-web")
    roots = sorted([n for n in snap.nodes if n.parent_id is None and n.removed_at is None], key=lambda n: n.position)
    assert [n.title for n in roots] == ["甲", "乙"]
    assert [n.position for n in roots] == [0, 1]