"""P2 服务层独立整改回归（graph.py / evidence_catalog.py）。

真实 PostgreSQL（conftest 重建 legal_workbench_test），全部 TEST-ONLY 虚构数据。
覆盖四组审查重点：
1. 统一案件行锁（维护门禁 → case FOR UPDATE → 域 seq），归档后所有写拒绝，
   两连接同一争用点断言；读工作区/catalog 数据与 seq 同一状态。
2. 冻结排除所有未确认操作（连续 pending 倒序字段复原、人工改写保值、
   Agent 改名/别名/成员新增/出处排除、待确认计数、实线必有出处）。
3. 证据编号唯一真源（Agent 组/条目/绑定/import 确认前不影响编号与快照、
   通用确认联动、position 间隙稳定排序、无 revision 绑定的撤回检查、
   移动/恢复 diff 合并净变化）。
4. 已发布原文 source_ref 守卫与正常 worker（未发布段拒绝引用、
   旧已发布段可 read_source、解析重试不被阻断）。
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from legal_workbench import cases, evidence_catalog, graph, models, parsing, search
from legal_workbench.db import get_session_factory, sqlalchemy_url
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.parsing import pipeline
from legal_workbench.schemas import (
    AddAliasInput, AddConsentInput, AddEntityInput, AddEvidenceBindingInput,
    AddEvidenceGroupInput, AddEvidenceItemInput, AddGraphRelationInput, AddSourceInput,
    AddToGraphInput, ArchiveCaseInput, CreateGraphInput, CreateSnapshotInput,
    EntityChanges, EntityRevisionInput, EvidenceAcceptAllInput, EvidenceBindingRevisionInput,
    EvidenceChangeIdInput, EvidenceGroupRevisionInput, EvidenceItemChanges,
    EvidenceItemRevisionInput, GraphAcceptAllInput, GraphChangeIdInput, GraphChanges,
    ImportCatalogRow, ImportBindingRow, ImportEvidenceCatalogInput, MoveEvidenceItemInput,
    RelationChanges, RemoveEvidenceBindingInput,
    RemoveSourceInput, RestoreSnapshotInput, SourceRefInput, UpdateEntityInput,
    UpdateEvidenceItemInput, UpdateGraphInput, UpdateRelationInput,
)

from .factories import build_material_with_storage, create_case


def _codes(fn):
    with pytest.raises(WorkbenchError) as exc:
        fn()
    return exc.value.code


def _msg(fn):
    with pytest.raises(WorkbenchError) as exc:
        fn()
    return exc.value.message


def _engine(test_database):
    return create_engine(sqlalchemy_url(test_database[1]), future=True)


def _hold_case_lock(engine, case_id) -> Session:
    session = Session(bind=engine)
    session.execute(select(models.Case).where(models.Case.id == case_id)
                    .with_for_update()).scalar_one()
    return session


def _run_in_thread(fn, results: list, key: str):
    def target():
        try:
            results.append((key, fn()))
        except WorkbenchError as exc:
            results.append((key, exc.code))
        except Exception as exc:  # noqa: BLE001
            results.append((key, f"error:{type(exc).__name__}:{exc}"))
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


# --- helpers -----------------------------------------------------------------

def _case(db) -> uuid.UUID:
    case_id = create_case(db, "TEST-ONLY 关系图整改")
    db.commit()
    return case_id


def _entity(ws_or_db, case_id, name, *, actor="human", graph_id=None, request_key=None):
    return graph.add_entity(ws_or_db, AddEntityInput(
        case_id=case_id, kind="person", name=name, graph_id=graph_id,
        request_key=request_key), actor=actor, entry="local-web" if actor == "human" else "mcp")


def _graph(db, case_id, title="TEST-ONLY 图", *, actor="human", request_key=None):
    return graph.create_graph(db, CreateGraphInput(
        case_id=case_id, title=title, request_key=request_key),
        actor=actor, entry="local-web" if actor == "human" else "mcp")


def _relation(db, case_id, a, b, *, label=None, credibility="suspected",
              actor="human", graph_id=None, request_key=None):
    return graph.add_relation(db, AddGraphRelationInput(
        case_id=case_id, from_entity_id=a, to_entity_id=b, kind="contract",
        label=label, credibility=credibility, graph_id=graph_id,
        request_key=request_key), actor=actor, entry="local-web" if actor == "human" else "mcp")


def _find(ws, collection, **kw):
    for item in getattr(ws, collection):
        if all(getattr(item, key) == value for key, value in kw.items()):
            return item
    raise AssertionError(f"missing {collection} {kw}")


def _pending(ws, command, target_id=None):
    for change in ws.pending_changes:
        if change.command != command:
            continue
        if target_id is None or any(
                str(entry.id) == str(target_id)
                or str((entry.after or entry.before or {}).get("relation_id", "")) == str(target_id)
                or str((entry.after or entry.before or {}).get("entity_id", "")) == str(target_id)
                for entry in change.diff):
            return change
    raise AssertionError(f"missing pending {command}")


def _material(db, store, case_id, name="原件.txt", content=b"TEST-ONLY \xe5\x8e\x9f\xe6\x96\x87"):
    return build_material_with_storage(db, store, case_id=case_id, content=content, name=name)


def _parsed_segment(db, store, case_id, name="合同.txt"):
    """Build a material, run the real parse pipeline, return (built, segment)."""
    built = _material(db, store, case_id, name=name,
                      content="TEST-ONLY 第一段正文\n\n第二段".encode("utf-8"))
    run_id, job_id = parsing.create_parse_run(
        db, case_id=case_id, file_version_id=built["file_version_id"])
    pipeline.process_parse_job(get_session_factory(), store, job_id)
    db.expire_all()
    segment = db.execute(select(models.Segment).where(
        models.Segment.parse_run_id == run_id)).scalars().first()
    assert segment is not None
    return built, segment, run_id


# =============================================================================
# 1. 统一案件行锁 / 归档守卫 / 一致读
# =============================================================================

def test_graph_write_waits_on_case_row_lock(db, test_database):
    """graph 写入必须排在案件行锁上：另一连接持锁期间写入等待，释放后成功。"""
    case_id = _case(db)
    engine = _engine(test_database)
    blocker = _hold_case_lock(engine, case_id)
    results: list = []
    try:
        def write():
            session = Session(bind=engine)
            try:
                return graph.create_graph(session, CreateGraphInput(
                    case_id=case_id, title="TEST-ONLY 锁后写入"),
                    actor="human", entry="local-web")
            finally:
                session.close()
        thread = _run_in_thread(write, results, "write")
        thread.join(0.8)
        assert thread.is_alive(), "graph 写入没有等待案件行锁"
        blocker.commit()
        thread.join(15)
    finally:
        blocker.rollback()
        blocker.close()
        engine.dispose()
    assert len(results) == 1 and not isinstance(results[0][1], (ErrorCode, str)), results


def test_evidence_write_waits_on_case_row_lock(db, test_database):
    case_id = _case(db)
    engine = _engine(test_database)
    blocker = _hold_case_lock(engine, case_id)
    results: list = []
    try:
        def write():
            session = Session(bind=engine)
            try:
                return evidence_catalog.add_group(session, AddEvidenceGroupInput(
                    case_id=case_id, title="TEST-ONLY 组"),
                    actor="human", entry="local-web")
            finally:
                session.close()
        thread = _run_in_thread(write, results, "write")
        thread.join(0.8)
        assert thread.is_alive(), "evidence 写入没有等待案件行锁"
        blocker.commit()
        thread.join(15)
    finally:
        blocker.rollback()
        blocker.close()
        engine.dispose()
    assert len(results) == 1 and not isinstance(results[0][1], (ErrorCode, str)), results


def test_late_archive_rejects_graph_and_evidence_writes(db, test_database):
    """前置检查已通过、归档随后提交：写入必须在锁内复核被拒（两连接同一争用点）。"""
    case_id = _case(db)
    engine = _engine(test_database)
    late = Session(bind=engine)
    try:
        stale = late.get(models.Case, case_id)
        assert stale.archived_at is None  # 旧实现的先查后写在这里就已通过
        cases.archive_case(db, ArchiveCaseInput(case_id=case_id, expected_revision=0))
        assert _codes(lambda: graph.create_graph(late, CreateGraphInput(
            case_id=case_id, title="迟到图"), actor="human", entry="local-web")) \
            == ErrorCode.INVALID_INPUT
        late.rollback()
        assert _codes(lambda: evidence_catalog.add_group(late, AddEvidenceGroupInput(
            case_id=case_id, title="迟到组"), actor="human", entry="local-web")) \
            == ErrorCode.INVALID_INPUT
        late.rollback()
        assert _codes(lambda: graph.accept_all(late, GraphAcceptAllInput(
            case_id=case_id, expected_seq=0), actor="human", entry="local-web")) \
            == ErrorCode.INVALID_INPUT
        late.rollback()
        assert _codes(lambda: evidence_catalog.accept_all(late, EvidenceAcceptAllInput(
            case_id=case_id, expected_seq=0), actor="human", entry="local-web")) \
            == ErrorCode.INVALID_INPUT
    finally:
        late.rollback()
        late.close()
        engine.dispose()
    db.expire_all()
    assert not db.execute(select(models.RelationGraph).where(
        models.RelationGraph.case_id == case_id)).scalars().all()
    assert not db.execute(select(models.EvidenceGroup).where(
        models.EvidenceGroup.case_id == case_id)).scalars().all()


def test_archived_case_rejects_every_write_entry(db):
    """归档后 graph/evidence 全部写入口拒绝；查看与导出正常。"""
    case_id = _case(db)
    ws = _graph(db, case_id)
    g = _find(ws, "graphs", title="TEST-ONLY 图")
    ws = _entity(db, case_id, "甲", graph_id=g.id)
    a = _find(ws, "entities", name="甲")
    ws = _entity(db, case_id, "乙", graph_id=g.id)
    b = _find(ws, "entities", name="乙")
    ws = _relation(db, case_id, a.id, b.id, graph_id=g.id)
    rel = _find(ws, "relations", from_entity_id=a.id)
    cat = evidence_catalog.add_group(db, AddEvidenceGroupInput(
        case_id=case_id, title="组一"), actor="human", entry="local-web")
    eg = cat.groups[0]
    cat = evidence_catalog.add_item(db, AddEvidenceItemInput(
        case_id=case_id, group_id=eg.id, title="条目一"), actor="human", entry="local-web")
    item = cat.items[0]
    ws = graph.create_snapshot(db, CreateSnapshotInput(
        case_id=case_id, graph_id=g.id, stage_label="庭前"), actor="human", entry="local-web")
    snap = ws.snapshots[0]
    cases.archive_case(db, ArchiveCaseInput(case_id=case_id, expected_revision=0))

    calls = [
        lambda: graph.add_entity(db, AddEntityInput(case_id=case_id, kind="person", name="丙"),
                                 actor="human", entry="local-web"),
        lambda: graph.update_entity(db, UpdateEntityInput(
            case_id=case_id, entity_id=a.id, expected_revision=a.revision,
            changes=EntityChanges(note="x")), actor="human", entry="local-web"),
        lambda: graph.remove_entity(db, EntityRevisionInput(
            case_id=case_id, entity_id=a.id, expected_revision=a.revision),
            actor="human", entry="local-web"),
        lambda: graph.add_alias(db, AddAliasInput(case_id=case_id, entity_id=a.id, alias="别名"),
                                actor="human", entry="local-web"),
        lambda: graph.add_relation(db, AddGraphRelationInput(
            case_id=case_id, from_entity_id=b.id, to_entity_id=a.id, kind="debt",
            credibility="suspected"), actor="human", entry="local-web"),
        lambda: graph.update_relation(db, UpdateRelationInput(
            case_id=case_id, relation_id=rel.id, expected_revision=rel.revision,
            changes=RelationChanges(label="x")), actor="human", entry="local-web"),
        lambda: graph.create_graph(db, CreateGraphInput(case_id=case_id, title="新图"),
                                   actor="human", entry="local-web"),
        lambda: graph.update_graph(db, UpdateGraphInput(
            case_id=case_id, graph_id=g.id, expected_revision=g.revision,
            changes=GraphChanges(title="改名")), actor="human", entry="local-web"),
        lambda: graph.add_to_graph(db, AddToGraphInput(
            case_id=case_id, graph_id=g.id, entity_ids=[b.id]),
            actor="human", entry="local-web"),
        lambda: graph.create_snapshot(db, CreateSnapshotInput(
            case_id=case_id, graph_id=g.id, stage_label="庭后"),
            actor="human", entry="local-web"),
        lambda: graph.restore_snapshot(db, RestoreSnapshotInput(
            case_id=case_id, snapshot_id=snap.id), actor="human", entry="local-web"),
        lambda: graph.add_snapshot_consent(db, AddConsentInput(
            case_id=case_id, snapshot_id=snap.id, action="confirm", consenter="客户",
            consented_at=datetime.now(timezone.utc), channel="meeting", source_detail="会议"),
            actor="human", entry="local-web"),
        lambda: graph.accept_all(db, GraphAcceptAllInput(case_id=case_id, expected_seq=ws.seq),
                                 actor="human", entry="local-web"),
        lambda: graph.revert_change(db, GraphChangeIdInput(
            case_id=case_id, change_id=ws.changes[0].id), actor="human", entry="local-web"),
        lambda: evidence_catalog.add_group(db, AddEvidenceGroupInput(
            case_id=case_id, title="新组"), actor="human", entry="local-web"),
        lambda: evidence_catalog.add_item(db, AddEvidenceItemInput(
            case_id=case_id, group_id=eg.id, title="新条目"), actor="human", entry="local-web"),
        lambda: evidence_catalog.update_item(db, UpdateEvidenceItemInput(
            case_id=case_id, item_id=item.id, expected_revision=item.revision,
            changes=EvidenceItemChanges(title="改名")), actor="human", entry="local-web"),
        lambda: evidence_catalog.remove_group(db, EvidenceGroupRevisionInput(
            case_id=case_id, group_id=eg.id, expected_revision=eg.revision),
            actor="human", entry="local-web"),
        lambda: evidence_catalog.confirm_item(db, EvidenceItemRevisionInput(
            case_id=case_id, item_id=item.id, expected_revision=item.revision),
            actor="human", entry="local-web"),
        lambda: evidence_catalog.accept_all(db, EvidenceAcceptAllInput(
            case_id=case_id, expected_seq=cat.seq), actor="human", entry="local-web"),
        lambda: evidence_catalog.revert_change(db, EvidenceChangeIdInput(
            case_id=case_id, change_id=uuid.uuid4()), actor="human", entry="local-web"),
    ]
    for call in calls:
        assert _codes(call) == ErrorCode.INVALID_INPUT
        db.rollback()
    # 读取与导出不受影响。
    ws2 = graph.get_workspace(db, case_id)
    assert ws2.archived is True
    assert len(ws2.entities) == 2
    csv_text = evidence_catalog.export_catalog_csv(db, case_id)
    assert "条目一" in csv_text


def test_workspace_data_and_seqs_are_one_state(db, client, test_database):
    """读工作区：catalog 数据与两个域 seq 必须来自同一已提交状态。

    另一连接持有案件行锁并已完成一次 evidence 写入（未提交）时，读取等待；
    提交后读到的 numbers 与 seqs 同属该写入之后。首写无 seq 行也不漏。
    """
    case_id = _case(db)
    ws = graph.get_workspace(db, case_id)
    db.commit()  # 释放读取持有的案件共享锁，避免与后续写入 FOR UPDATE 自锁
    assert ws.seq == 0 and ws.seqs.get("evidence", 0) == 0
    assert ws.evidence.seq == 0
    engine = _engine(test_database)
    writer = Session(bind=engine)
    try:
        # 一次已提交的 evidence 写入（建立 seq 行与组数据）。
        catalog = evidence_catalog.add_group(writer, AddEvidenceGroupInput(
            case_id=case_id, title="并发组"), actor="human", entry="local-web")
        # 再开一笔持锁未提交的写入：读取方必须等待，不能读到撕裂状态。
        writer.execute(select(models.Case).where(models.Case.id == case_id)
                       .with_for_update()).scalar_one()
        writer.execute(text(
            "UPDATE case_change_seq SET seq = seq + 5 WHERE case_id = :c AND domain = 'evidence'"),
            {"c": str(case_id)})
        reader_results: list = []

        def read():
            session = Session(bind=engine)
            try:
                return graph.get_workspace(session, case_id)
            finally:
                session.close()
        thread = _run_in_thread(read, reader_results, "read")
        thread.join(0.8)
        assert thread.is_alive(), "读取没有等待案件行锁（数据与 seq 可能撕裂）"
        writer.commit()
        thread.join(15)
        assert len(reader_results) == 1
        ws2 = reader_results[0][1]
        assert not isinstance(ws2, (ErrorCode, str)), ws2
        assert ws2.evidence.seq == catalog.seq + 5
        assert ws2.seqs["evidence"] == ws2.evidence.seq
        assert any(g.title == "并发组" for g in ws2.evidence.groups)
    finally:
        writer.rollback()
        writer.close()
        engine.dispose()


def test_first_write_creates_seq_without_gap(db):
    case_id = _case(db)
    ws = _graph(db, case_id)
    assert ws.seq == 1
    assert [c.seq for c in ws.changes] == [1]
    cat = evidence_catalog.add_group(db, AddEvidenceGroupInput(
        case_id=case_id, title="组"), actor="human", entry="local-web")
    assert cat.seq == 1


def test_maintenance_gate_blocks_graph_and_evidence(db):
    case_id = _case(db)
    db.execute(text("UPDATE maintenance_state SET active=true, reason='test' WHERE id=1"))
    db.commit()
    try:
        assert _codes(lambda: graph.create_graph(db, CreateGraphInput(
            case_id=case_id, title="维护中"), actor="human", entry="local-web")) \
            == ErrorCode.MAINTENANCE
        db.rollback()
        assert _codes(lambda: evidence_catalog.add_group(db, AddEvidenceGroupInput(
            case_id=case_id, title="维护中"), actor="human", entry="local-web")) \
            == ErrorCode.MAINTENANCE
        db.rollback()
    finally:
        db.execute(text("UPDATE maintenance_state SET active=false, reason=NULL WHERE id=1"))
        db.commit()


# =============================================================================
# 2. 冻结排除所有未确认操作（确认态视图）
# =============================================================================

def _fixture_graph(db, case_id):
    """Two entities + one suspected relation in one graph."""
    ws = _graph(db, case_id, "股权图")
    g = _find(ws, "graphs", title="股权图")
    ws = _entity(db, case_id, "张三", graph_id=g.id)
    a = _find(ws, "entities", name="张三")
    ws = _entity(db, case_id, "甲公司", graph_id=g.id)
    b = _find(ws, "entities", name="甲公司")
    ws = _relation(db, case_id, a.id, b.id, label="董事", graph_id=g.id)
    rel = _find(ws, "relations", label="董事")
    return g, a, b, rel


def test_stacked_pending_edits_freeze_earliest_confirmed_value(db):
    """连续两条 Agent 改标签：快照取原始「董事」，不是「待确认A」。"""
    case_id = _case(db)
    g, a, b, rel = _fixture_graph(db, case_id)
    ws = graph.update_relation(db, UpdateRelationInput(
        case_id=case_id, relation_id=rel.id, expected_revision=rel.revision,
        changes=RelationChanges(label="待确认A")), actor="agent", entry="mcp")
    first = _pending(ws, "update_relation", rel.id)
    rel_a = _find(ws, "relations", label="待确认A")
    ws = graph.update_relation(db, UpdateRelationInput(
        case_id=case_id, relation_id=rel.id, expected_revision=rel_a.revision,
        changes=RelationChanges(label="待确认B")), actor="agent", entry="mcp")
    second = [c for c in ws.pending_changes if c.command == "update_relation"][-1]
    assert second.id != first.id

    ws = graph.create_snapshot(db, CreateSnapshotInput(
        case_id=case_id, graph_id=g.id, stage_label="庭前", acknowledge_pending=True),
        actor="human", entry="local-web")
    payload = ws.snapshots[-1].payload
    edge = next(e for e in payload["edges"] if e["relation_id"] == str(rel.id))
    assert edge["label"] == "董事"
    assert ws.snapshots[-1].excluded_pending_count >= 2

    # 超前确认第二条被拒：必须按写入顺序复核。
    assert _codes(lambda: graph.accept_change(db, GraphChangeIdInput(
        case_id=case_id, change_id=second.id), actor="human", entry="local-web")) \
        == ErrorCode.CONFLICT
    db.rollback()
    graph.accept_change(db, GraphChangeIdInput(case_id=case_id, change_id=first.id),
                        actor="human", entry="local-web")
    ws = graph.accept_change(db, GraphChangeIdInput(case_id=case_id, change_id=second.id),
                             actor="human", entry="local-web")
    assert _find(ws, "relations", id=rel.id).label == "待确认B"
    ws = graph.create_snapshot(db, CreateSnapshotInput(
        case_id=case_id, graph_id=g.id, stage_label="庭后"), actor="human", entry="local-web")
    edge = next(e for e in ws.snapshots[-1].payload["edges"]
                if e["relation_id"] == str(rel.id))
    assert edge["label"] == "待确认B"


def test_stacked_pending_revert_in_reverse_order(db):
    case_id = _case(db)
    g, a, b, rel = _fixture_graph(db, case_id)
    ws = graph.update_relation(db, UpdateRelationInput(
        case_id=case_id, relation_id=rel.id, expected_revision=rel.revision,
        changes=RelationChanges(label="待确认A")), actor="agent", entry="mcp")
    first = _pending(ws, "update_relation", rel.id)
    rel_a = _find(ws, "relations", label="待确认A")
    ws = graph.update_relation(db, UpdateRelationInput(
        case_id=case_id, relation_id=rel.id, expected_revision=rel_a.revision,
        changes=RelationChanges(label="待确认B")), actor="agent", entry="mcp")
    second = [c for c in ws.pending_changes if c.command == "update_relation"][-1]
    # 先撤第一条被拒（有更晚的同对象待确认）。
    assert _codes(lambda: graph.revert_change(db, GraphChangeIdInput(
        case_id=case_id, change_id=first.id), actor="human", entry="local-web")) \
        == ErrorCode.CONFLICT
    db.rollback()
    graph.revert_change(db, GraphChangeIdInput(case_id=case_id, change_id=second.id),
                        actor="human", entry="local-web")
    ws = graph.revert_change(db, GraphChangeIdInput(case_id=case_id, change_id=first.id),
                             actor="human", entry="local-web")
    assert _find(ws, "relations", id=rel.id).label == "董事"


def test_human_overwrite_keeps_human_value_and_stale_proposal_cannot_accept(db):
    case_id = _case(db)
    g, a, b, rel = _fixture_graph(db, case_id)
    ws = graph.update_relation(db, UpdateRelationInput(
        case_id=case_id, relation_id=rel.id, expected_revision=rel.revision,
        changes=RelationChanges(label="待确认A")), actor="agent", entry="mcp")
    stale = _pending(ws, "update_relation", rel.id)
    rel_a = _find(ws, "relations", label="待确认A")
    ws = graph.update_relation(db, UpdateRelationInput(
        case_id=case_id, relation_id=rel.id, expected_revision=rel_a.revision,
        changes=RelationChanges(label="人工定稿")), actor="human", entry="local-web")
    ws = graph.create_snapshot(db, CreateSnapshotInput(
        case_id=case_id, graph_id=g.id, stage_label="庭前", acknowledge_pending=True),
        actor="human", entry="local-web")
    edge = next(e for e in ws.snapshots[-1].payload["edges"]
                if e["relation_id"] == str(rel.id))
    assert edge["label"] == "人工定稿"
    message = _msg(lambda: graph.accept_change(db, GraphChangeIdInput(
        case_id=case_id, change_id=stale.id), actor="human", entry="local-web"))
    assert "人工" in message
    db.rollback()
    ws = graph.revert_change(db, GraphChangeIdInput(case_id=case_id, change_id=stale.id),
                             actor="human", entry="local-web")
    assert "人工" in (ws.notice or "")
    assert _find(ws, "relations", id=rel.id).label == "人工定稿"


def test_agent_rename_alias_membership_and_sources_excluded_from_freeze(db):
    """Agent 改图名、加别名、把已有主体/关系加入图、补出处：全部不进快照且计数。"""
    case_id = _case(db)
    g, a, b, rel = _fixture_graph(db, case_id)
    ws = _entity(db, case_id, "案外人丁")
    ding = _find(ws, "entities", name="案外人丁")
    ws = _relation(db, case_id, a.id, ding.id, label="运输合同")
    extra_rel = _find(ws, "relations", label="运输合同")

    ws = graph.update_graph(db, UpdateGraphInput(
        case_id=case_id, graph_id=g.id, expected_revision=g.revision,
        changes=GraphChanges(title="Agent 拟改名")), actor="agent", entry="mcp")
    ws = graph.add_alias(db, AddAliasInput(case_id=case_id, entity_id=a.id, alias="Agent 别名"),
                         actor="agent", entry="mcp")
    ws = graph.add_to_graph(db, AddToGraphInput(
        case_id=case_id, graph_id=g.id, entity_ids=[ding.id], relation_ids=[extra_rel.id]),
        actor="agent", entry="mcp")
    # 出处用材料引用（无需解析）。
    from .factories import build_material
    mat = build_material(db, case_id=case_id)
    db.commit()
    ws = graph.add_source(db, AddSourceInput(
        case_id=case_id, relation_id=rel.id,
        ref=SourceRefInput(material_id=mat["material_id"]),
        proves="Agent 补充", request_key="agent-src-1"), actor="agent", entry="mcp")

    # 冻结前提示：不给 acknowledge 必须拒绝并给出数量。
    message = _msg(lambda: graph.create_snapshot(db, CreateSnapshotInput(
        case_id=case_id, graph_id=g.id, stage_label="庭前"),
        actor="human", entry="local-web"))
    assert "待确认" in message
    db.rollback()
    ws = graph.create_snapshot(db, CreateSnapshotInput(
        case_id=case_id, graph_id=g.id, stage_label="庭前", acknowledge_pending=True),
        actor="human", entry="local-web")
    snap = ws.snapshots[-1]
    payload = snap.payload
    assert payload["graph"]["title"] == "股权图"
    node = next(n for n in payload["nodes"] if n["entity_id"] == str(a.id))
    assert "Agent 别名" not in node["aliases"]
    assert all(n["entity_id"] != str(ding.id) for n in payload["nodes"])
    assert all(e["relation_id"] != str(extra_rel.id) for e in payload["edges"])
    edge = next(e for e in payload["edges"] if e["relation_id"] == str(rel.id))
    assert edge["sources"] == []
    assert snap.excluded_pending_count >= 4
    # 工作稿黄标保留：pending 列表仍含这些变更。
    commands = {c.command for c in ws.pending_changes}
    assert {"update_graph", "add_alias", "add_to_graph", "add_source"} <= commands


def test_agent_new_graph_cannot_freeze_until_accepted(db):
    case_id = _case(db)
    ws = _graph(db, case_id, "Agent 拟建图", actor="agent", request_key="agent-g-1")
    agent_graph = _find(ws, "graphs", title="Agent 拟建图")
    message = _msg(lambda: graph.create_snapshot(db, CreateSnapshotInput(
        case_id=case_id, graph_id=agent_graph.id, stage_label="庭前"),
        actor="human", entry="local-web"))
    assert "待确认" in message
    db.rollback()
    change = _pending(ws, "create_graph")
    graph.accept_change(db, GraphChangeIdInput(case_id=case_id, change_id=change.id),
                        actor="human", entry="local-web")
    ws = graph.create_snapshot(db, CreateSnapshotInput(
        case_id=case_id, graph_id=agent_graph.id, stage_label="庭前"),
        actor="human", entry="local-web")
    assert ws.snapshots[-1].payload["graph"]["title"] == "Agent 拟建图"
    assert ws.snapshots[-1].payload["nodes"] == []


def test_pending_source_not_evidence_until_accepted(db, store):
    """失败链 1：Agent 待确认出处不算有效出处；先确认出处才能确认升级。"""
    case_id = _case(db)
    g, a, b, rel = _fixture_graph(db, case_id)
    built, segment, run_id = _parsed_segment(db, store, case_id)
    ws = graph.add_source(db, AddSourceInput(
        case_id=case_id, relation_id=rel.id, ref=SourceRefInput(segment_id=segment.id),
        proves="决议", request_key="agent-src-office"), actor="agent", entry="mcp")
    # 人工升级被拒（只有待确认出处）。
    message = _msg(lambda: graph.update_relation(db, UpdateRelationInput(
        case_id=case_id, relation_id=rel.id, expected_revision=rel.revision,
        changes=RelationChanges(credibility="evidenced")),
        actor="human", entry="local-web"))
    assert "待确认" in message
    db.rollback()
    # Agent 同批提案：先补出处再升级，可以提交。
    ws = graph.update_relation(db, UpdateRelationInput(
        case_id=case_id, relation_id=rel.id, expected_revision=rel.revision,
        changes=RelationChanges(credibility="evidenced")), actor="agent", entry="mcp")
    upgrade = _pending(ws, "update_relation", rel.id)
    source_change = _pending(ws, "add_source", rel.id)
    # 强行冻结：不能出现无出处的实线（升级回退到 suspected）。
    ws = graph.create_snapshot(db, CreateSnapshotInput(
        case_id=case_id, graph_id=g.id, stage_label="庭前", acknowledge_pending=True),
        actor="human", entry="local-web")
    edge = next(e for e in ws.snapshots[-1].payload["edges"]
                if e["relation_id"] == str(rel.id))
    assert edge["credibility"] == "suspected"
    assert edge["sources"] == []
    # 超前确认升级被拒。
    message = _msg(lambda: graph.accept_change(db, GraphChangeIdInput(
        case_id=case_id, change_id=upgrade.id), actor="human", entry="local-web"))
    assert "出处" in message
    db.rollback()
    graph.accept_change(db, GraphChangeIdInput(case_id=case_id, change_id=source_change.id),
                        actor="human", entry="local-web")
    ws = graph.accept_change(db, GraphChangeIdInput(case_id=case_id, change_id=upgrade.id),
                             actor="human", entry="local-web")
    assert _find(ws, "relations", id=rel.id).credibility == "evidenced"
    ws = graph.create_snapshot(db, CreateSnapshotInput(
        case_id=case_id, graph_id=g.id, stage_label="庭后"), actor="human", entry="local-web")
    edge = next(e for e in ws.snapshots[-1].payload["edges"]
                if e["relation_id"] == str(rel.id))
    assert edge["credibility"] == "evidenced"
    assert len(edge["sources"]) == 1


def test_revert_last_pending_source_requires_downgrade_first(db, store):
    """撤回最后出处守卫：有证据关系的唯一出处不能直接撤。"""
    case_id = _case(db)
    g, a, b, rel = _fixture_graph(db, case_id)
    built, segment, run_id = _parsed_segment(db, store, case_id)
    ws = graph.add_source(db, AddSourceInput(
        case_id=case_id, relation_id=rel.id, ref=SourceRefInput(segment_id=segment.id),
        request_key="agent-src-1"), actor="agent", entry="mcp")
    source_change = _pending(ws, "add_source", rel.id)
    graph.accept_change(db, GraphChangeIdInput(case_id=case_id, change_id=source_change.id),
                        actor="human", entry="local-web")
    ws = graph.update_relation(db, UpdateRelationInput(
        case_id=case_id, relation_id=rel.id, expected_revision=rel.revision,
        changes=RelationChanges(credibility="evidenced")), actor="human", entry="local-web")
    # 撤回已确认的出处变更：它是唯一有效出处 → 拒绝。
    message = _msg(lambda: graph.revert_change(db, GraphChangeIdInput(
        case_id=case_id, change_id=source_change.id), actor="human", entry="local-web"))
    assert "有证据" in message
    db.rollback()
    # 人工删除同样要求显式降级。
    src = _find(ws, "sources", relation_id=rel.id)
    assert _codes(lambda: graph.remove_source(db, RemoveSourceInput(
        case_id=case_id, source_id=src.id), actor="human", entry="local-web")) \
        == ErrorCode.INVALID_INPUT
    db.rollback()
    ws = graph.remove_source(db, RemoveSourceInput(
        case_id=case_id, source_id=src.id, downgrade_to="suspected"),
        actor="human", entry="local-web")
    assert _find(ws, "relations", id=rel.id).credibility == "suspected"
    # 现在可以撤回出处变更（关系已不是 evidenced）。
    graph.revert_change(db, GraphChangeIdInput(case_id=case_id, change_id=source_change.id),
                        actor="human", entry="local-web")


def test_human_write_blocked_on_pending_addition(db):
    case_id = _case(db)
    ws = _entity(db, case_id, "Agent 主体", actor="agent", request_key="agent-e-1")
    ding = _find(ws, "entities", name="Agent 主体")
    message = _msg(lambda: graph.update_entity(db, UpdateEntityInput(
        case_id=case_id, entity_id=ding.id, expected_revision=ding.revision,
        changes=EntityChanges(note="人工补注")), actor="human", entry="local-web"))
    assert "待确认" in message
    db.rollback()
    ws = _entity(db, case_id, "另一主体")
    other = _find(ws, "entities", name="另一主体")
    ws = _relation(db, case_id, ding.id, other.id, actor="agent", request_key="agent-r-1")
    rel = ws.relations[0]
    message = _msg(lambda: graph.add_source(db, AddSourceInput(
        case_id=case_id, relation_id=rel.id,
        ref=SourceRefInput(material_id=uuid.uuid4())), actor="human", entry="local-web"))
    assert "待确认" in message


def test_accept_all_skips_superseded_and_respects_scope(db):
    case_id = _case(db)
    g, a, b, rel = _fixture_graph(db, case_id)
    ws = graph.update_relation(db, UpdateRelationInput(
        case_id=case_id, relation_id=rel.id, expected_revision=rel.revision,
        changes=RelationChanges(label="提案A")), actor="agent", entry="mcp")
    stale = _pending(ws, "update_relation", rel.id)
    rel_a = _find(ws, "relations", label="提案A")
    ws = graph.update_relation(db, UpdateRelationInput(
        case_id=case_id, relation_id=rel.id, expected_revision=rel_a.revision,
        changes=RelationChanges(label="人工值")), actor="human", entry="local-web")
    ws = graph.add_alias(db, AddAliasInput(case_id=case_id, entity_id=a.id, alias="新鲜别名"),
                         actor="agent", entry="mcp")
    fresh = _pending(ws, "add_alias")
    # expected_seq 过期 → 冲突。
    assert _codes(lambda: graph.accept_all(db, GraphAcceptAllInput(
        case_id=case_id, expected_seq=0), actor="human", entry="local-web")) \
        == ErrorCode.CONFLICT
    db.rollback()
    ws = graph.accept_all(db, GraphAcceptAllInput(
        case_id=case_id, change_ids=[stale.id, fresh.id]), actor="human", entry="local-web")
    assert "人工修改覆盖" in (ws.notice or "")
    assert _find(ws, "relations", id=rel.id).label == "人工值"
    assert any(al.alias == "新鲜别名" for al in ws.aliases)
    states = {c.id: c.review_state for c in ws.changes}
    assert states[fresh.id] == "accepted"
    assert states[stale.id] == "pending"


# =============================================================================
# 3. 证据目录：编号唯一真源 / 复核联动 / 稳定排序 / 撤回
# =============================================================================

def test_agent_group_item_binding_do_not_move_numbers_until_accepted(db):
    case_id = _case(db)
    cat = evidence_catalog.add_group(db, AddEvidenceGroupInput(
        case_id=case_id, title="组一"), actor="human", entry="local-web")
    g1 = cat.groups[0]
    cat = evidence_catalog.add_item(db, AddEvidenceItemInput(
        case_id=case_id, group_id=g1.id, title="条目一"), actor="human", entry="local-web")
    item1 = cat.items[0]
    assert cat.numbers[str(item1.id)] == "证据1-1"

    # Agent 在中间插入新组：确认前不得挤动组号。
    cat = evidence_catalog.add_group(db, AddEvidenceGroupInput(
        case_id=case_id, title="Agent 组", position=0, request_key="agent-g"),
        actor="agent", entry="mcp")
    agent_group = next(g for g in cat.groups if g.title == "Agent 组")
    assert cat.numbers[str(item1.id)] == "证据1-1"
    assert str(agent_group.id) not in cat.group_numbers
    # Agent 条目错误 confirmed 的回归：必须是 pending 且不参与编号。
    cat = evidence_catalog.add_item(db, AddEvidenceItemInput(
        case_id=case_id, group_id=g1.id, title="Agent 条目", position=0,
        request_key="agent-i"), actor="agent", entry="mcp")
    agent_item = next(i for i in cat.items if i.title == "Agent 条目")
    assert agent_item.review_state == "pending"
    assert str(agent_item.id) not in cat.numbers
    assert cat.numbers[str(item1.id)] == "证据1-1"
    # Agent 绑定同样 pending。
    from .factories import build_material
    mat = build_material(db, case_id=case_id)
    db.commit()
    cat = evidence_catalog.add_binding(db, AddEvidenceBindingInput(
        case_id=case_id, item_id=item1.id, material_id=mat["material_id"],
        request_key="agent-b"), actor="agent", entry="mcp")
    binding = next(b for b in cat.bindings if b.material_id == mat["material_id"])
    assert binding.review_state == "pending"

    # 通用确认联动：逐条确认后组/条目/绑定全部生效。
    ws_changes = db.execute(select(models.EvidenceChange).where(
        models.EvidenceChange.case_id == case_id,
        models.EvidenceChange.review_state == "pending")
        .order_by(models.EvidenceChange.seq)).scalars().all()
    for change in ws_changes:
        cat = evidence_catalog.accept_change(db, EvidenceChangeIdInput(
            case_id=case_id, change_id=change.id), actor="human", entry="local-web")
    assert cat.numbers[str(item1.id)] == "证据2-2"
    assert cat.numbers[str(agent_item.id)] == "证据2-1"
    assert cat.group_numbers[str(agent_group.id)] == 1
    confirmed_binding = next(b for b in cat.bindings if b.id == binding.id)
    assert confirmed_binding.review_state == "confirmed"


def test_import_catalog_pending_until_accept_then_numbering(db, store):
    case_id = _case(db)
    mat = _material(db, store, case_id, name="材料甲.txt", content=b"TEST-ONLY material A")
    imp = _material(db, store, case_id, name="目录.docx", content=b"TEST-ONLY catalog B")
    cat = evidence_catalog.add_group(db, AddEvidenceGroupInput(
        case_id=case_id, title="既有组"), actor="human", entry="local-web")
    g1 = cat.groups[0]
    cat = evidence_catalog.add_item(db, AddEvidenceItemInput(
        case_id=case_id, group_id=g1.id, title="既有条目"), actor="human", entry="local-web")
    existing = cat.items[0]
    cat = evidence_catalog.import_catalog(db, ImportEvidenceCatalogInput(
        case_id=case_id, import_material_id=imp["material_id"], request_key="imp-1",
        rows=[ImportCatalogRow(group_title="导入组", item_title="导入条目",
                               bindings=[ImportBindingRow(material_id=mat["material_id"],
                                                          page_start=1, page_end=2)])]),
        actor="agent", entry="mcp")
    assert cat.numbers[str(existing.id)] == "证据1-1"
    imported_item = next(i for i in cat.items if i.title == "导入条目")
    assert imported_item.review_state == "pending"
    assert str(imported_item.id) not in cat.numbers
    # 幂等重放。
    seq_before = cat.seq
    cat = evidence_catalog.import_catalog(db, ImportEvidenceCatalogInput(
        case_id=case_id, import_material_id=imp["material_id"], request_key="imp-1",
        rows=[ImportCatalogRow(group_title="导入组", item_title="导入条目")]),
        actor="agent", entry="mcp")
    assert cat.seq == seq_before
    # 一键确认（按 seq）→ 编号生效。
    change = db.execute(select(models.EvidenceChange).where(
        models.EvidenceChange.case_id == case_id,
        models.EvidenceChange.command == "import_catalog")).scalar_one()
    cat = evidence_catalog.accept_all(db, EvidenceAcceptAllInput(
        case_id=case_id, change_ids=[change.id]), actor="human", entry="local-web")
    assert cat.numbers[str(imported_item.id)] == "证据2-1"
    assert cat.numbers[str(existing.id)] == "证据1-1"


@pytest.mark.parametrize("remaining_action", ["accept_change", "revert_change"])
def test_individual_import_confirmation_preserves_number_and_remaining_review(db, store, remaining_action):
    case_id = _case(db)
    material = _material(db, store, case_id, name="目录.docx", content=b"TEST-ONLY review catalog")
    catalog = evidence_catalog.import_catalog(db, ImportEvidenceCatalogInput(
        case_id=case_id, import_material_id=material["material_id"], request_key="partial-review",
        rows=[
            ImportCatalogRow(group_title="导入组", item_title="逐条确认"),
            ImportCatalogRow(group_title="导入组", item_title="剩余条目",
                             bindings=[ImportBindingRow(material_id=material["material_id"])]),
        ]), actor="agent", entry="mcp")
    first, remaining = sorted(catalog.items, key=lambda item: item.position)
    change, = catalog.pending_changes
    assert catalog.group_numbers == {}
    assert catalog.numbers == {}

    catalog = evidence_catalog.confirm_item(db, EvidenceItemRevisionInput(
        case_id=case_id, item_id=first.id, expected_revision=first.revision),
        actor="human", entry="local-web")
    assert catalog.numbers == {str(first.id): "证据1-1"}
    confirmed = next(item for item in catalog.items if item.id == first.id)
    catalog = evidence_catalog.update_item(db, UpdateEvidenceItemInput(
        case_id=case_id, item_id=confirmed.id, expected_revision=confirmed.revision,
        changes=EvidenceItemChanges(title="已复核后人工改名")),
        actor="human", entry="local-web")
    assert [entry.id for entry in catalog.pending_changes] == [change.id]
    payload = EvidenceChangeIdInput(case_id=case_id, change_id=change.id)
    action = getattr(evidence_catalog, remaining_action)
    catalog = action(db, payload, actor="human", entry="local-web")
    assert catalog.pending_changes == []
    expected_state = "confirmed" if remaining_action == "accept_change" else "rejected"
    items = {item.id: item for item in catalog.items}
    assert items[first.id].review_state == "confirmed"
    assert items[first.id].title == "已复核后人工改名"
    assert items[remaining.id].review_state == expected_state
    assert catalog.bindings[0].review_state == expected_state
    assert catalog.numbers[str(first.id)] == "证据1-1"
    assert (str(remaining.id) in catalog.numbers) == (expected_state == "confirmed")


def test_agent_edit_keeps_preexisting_evidence_number(db):
    case_id = _case(db)
    cat = evidence_catalog.add_group(db, AddEvidenceGroupInput(
        case_id=case_id, title="组"), actor="human", entry="local-web")
    cat = evidence_catalog.add_item(db, AddEvidenceItemInput(
        case_id=case_id, group_id=cat.groups[0].id, title="原名"), actor="human", entry="local-web")
    item = cat.items[0]
    cat = evidence_catalog.update_item(db, UpdateEvidenceItemInput(
        case_id=case_id, item_id=item.id, expected_revision=item.revision,
        changes=EvidenceItemChanges(title="Agent 建议名")), actor="agent", entry="mcp")
    proposal, = cat.pending_changes
    assert cat.numbers[str(item.id)] == "证据1-1"
    cat = evidence_catalog.accept_change(db, EvidenceChangeIdInput(
        case_id=case_id, change_id=proposal.id), actor="human", entry="local-web")
    assert cat.items[0].title == "Agent 建议名"
    assert cat.numbers[str(item.id)] == "证据1-1"
    assert cat.pending_changes == []


def test_confirm_binding_requires_confirmed_item(db):
    case_id = _case(db)
    from .factories import build_material
    mat = build_material(db, case_id=case_id)
    db.commit()
    cat = evidence_catalog.add_group(db, AddEvidenceGroupInput(
        case_id=case_id, title="组", request_key="g"), actor="agent", entry="mcp")
    g = cat.groups[0]
    cat = evidence_catalog.add_item(db, AddEvidenceItemInput(
        case_id=case_id, group_id=g.id, title="草稿条目", request_key="i"),
        actor="agent", entry="mcp")
    item = cat.items[0]
    cat = evidence_catalog.add_binding(db, AddEvidenceBindingInput(
        case_id=case_id, item_id=item.id, material_id=mat["material_id"],
        request_key="b"), actor="agent", entry="mcp")
    binding = cat.bindings[0]
    # 条目未确认时绑定不能单独确认。
    assert _codes(lambda: evidence_catalog.confirm_binding(db, EvidenceBindingRevisionInput(
        case_id=case_id, binding_id=binding.id, expected_revision=0),
        actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT
    db.rollback()
    # 人工也不能直接在待确认新增条目上加绑定。
    assert _codes(lambda: evidence_catalog.add_binding(db, AddEvidenceBindingInput(
        case_id=case_id, item_id=item.id, material_id=mat["material_id"], page_start=5),
        actor="human", entry="local-web")) == ErrorCode.CONFLICT
    db.rollback()
    evidence_catalog.accept_all(db, EvidenceAcceptAllInput(
        case_id=case_id, expected_seq=cat.seq), actor="human", entry="local-web")
    decision = EvidenceBindingRevisionInput(case_id=case_id, binding_id=binding.id,
                                            expected_revision=0)
    evidence_catalog.confirm_binding(db, decision, actor="human", entry="local-web")
    assert _codes(lambda: evidence_catalog.reject_binding(
        db, decision, actor="human", entry="local-web")) == ErrorCode.CONFLICT



def test_position_gap_insert_is_stable(db):
    """删中间组/条目留下 position 间隙后，len() 插入不再出 tie。"""
    case_id = _case(db)
    cat = evidence_catalog.add_group(db, AddEvidenceGroupInput(
        case_id=case_id, title="G0"), actor="human", entry="local-web")
    cat = evidence_catalog.add_group(db, AddEvidenceGroupInput(
        case_id=case_id, title="G1"), actor="human", entry="local-web")
    cat = evidence_catalog.add_group(db, AddEvidenceGroupInput(
        case_id=case_id, title="G2"), actor="human", entry="local-web")
    g0, g1, g2 = sorted(cat.groups, key=lambda g: g.position)
    cat = evidence_catalog.remove_group(db, EvidenceGroupRevisionInput(
        case_id=case_id, group_id=g1.id, expected_revision=g1.revision),
        actor="human", entry="local-web")
    # 剩余 position 0, 2（间隙）。追加新组不得与 G2 出 tie。
    cat = evidence_catalog.add_group(db, AddEvidenceGroupInput(
        case_id=case_id, title="G3"), actor="human", entry="local-web")
    active = sorted((g for g in cat.groups if g.removed_at is None),
                    key=lambda g: (g.position, str(g.id)))
    assert [g.title for g in active] == ["G0", "G2", "G3"]
    assert len({g.position for g in active}) == 3
    assert [cat.group_numbers[str(g.id)] for g in active] == [1, 2, 3]

    # 条目级同样验证。
    g0_id = g0.id
    for title in ("I0", "I1", "I2"):
        cat = evidence_catalog.add_item(db, AddEvidenceItemInput(
            case_id=case_id, group_id=g0_id, title=title), actor="human", entry="local-web")
    items = sorted((i for i in cat.items if i.group_id == g0_id), key=lambda i: i.position)
    cat = evidence_catalog.remove_item(db, EvidenceItemRevisionInput(
        case_id=case_id, item_id=items[1].id, expected_revision=items[1].revision),
        actor="human", entry="local-web")
    cat = evidence_catalog.add_item(db, AddEvidenceItemInput(
        case_id=case_id, group_id=g0_id, title="I3"), actor="human", entry="local-web")
    active_items = sorted((i for i in cat.items
                           if i.group_id == g0_id and i.removed_at is None),
                          key=lambda i: (i.position, str(i.id)))
    assert [i.title for i in active_items] == ["I0", "I2", "I3"]
    assert [cat.numbers[str(i.id)] for i in active_items] == ["证据1-1", "证据1-2", "证据1-3"]


def test_move_item_normalizes_and_revert_restores(db):
    """移动条目整体归一化；撤回移动恢复原顺序（diff 合并净变化）。"""
    case_id = _case(db)
    cat = evidence_catalog.add_group(db, AddEvidenceGroupInput(
        case_id=case_id, title="GA"), actor="human", entry="local-web")
    cat = evidence_catalog.add_group(db, AddEvidenceGroupInput(
        case_id=case_id, title="GB"), actor="human", entry="local-web")
    ga, gb = sorted(cat.groups, key=lambda g: g.position)
    for title in ("A0", "A1", "A2"):
        cat = evidence_catalog.add_item(db, AddEvidenceItemInput(
            case_id=case_id, group_id=ga.id, title=title), actor="human", entry="local-web")
    cat = evidence_catalog.add_item(db, AddEvidenceItemInput(
        case_id=case_id, group_id=gb.id, title="B0"), actor="human", entry="local-web")
    a1 = next(i for i in cat.items if i.title == "A1")
    b0 = next(i for i in cat.items if i.title == "B0")
    before_numbers = dict(cat.numbers)
    cat = evidence_catalog.move_item(db, MoveEvidenceItemInput(
        case_id=case_id, item_id=a1.id, to_group_id=gb.id, before_item_id=b0.id),
        actor="human", entry="local-web")
    assert cat.numbers[str(a1.id)] == "证据2-1"
    assert cat.numbers[str(b0.id)] == "证据2-2"
    a0 = next(i for i in cat.items if i.title == "A0")
    a2 = next(i for i in cat.items if i.title == "A2")
    assert cat.numbers[str(a0.id)] == "证据1-1"
    assert cat.numbers[str(a2.id)] == "证据1-2"
    move_change = db.execute(select(models.EvidenceChange).where(
        models.EvidenceChange.case_id == case_id,
        models.EvidenceChange.command == "move_item")).scalar_one()
    # 每个受影响对象在 diff 中只出现一次（净变化）。
    ids = [str(entry["id"]) for entry in move_change.diff]
    assert len(ids) == len(set(ids))
    cat = evidence_catalog.revert_change(db, EvidenceChangeIdInput(
        case_id=case_id, change_id=move_change.id), actor="human", entry="local-web")
    assert cat.numbers == before_numbers


def test_revert_binding_without_revision_checks_fields(db):
    """绑定无 revision：撤回 remove_binding 用字段级 after 检查。"""
    case_id = _case(db)
    from .factories import build_material
    mat = build_material(db, case_id=case_id)
    db.commit()
    cat = evidence_catalog.add_group(db, AddEvidenceGroupInput(
        case_id=case_id, title="组"), actor="human", entry="local-web")
    g = cat.groups[0]
    cat = evidence_catalog.add_item(db, AddEvidenceItemInput(
        case_id=case_id, group_id=g.id, title="条目"), actor="human", entry="local-web")
    item = cat.items[0]
    cat = evidence_catalog.add_binding(db, AddEvidenceBindingInput(
        case_id=case_id, item_id=item.id, material_id=mat["material_id"]),
        actor="human", entry="local-web")
    binding = cat.bindings[0]
    cat = evidence_catalog.remove_binding(db, RemoveEvidenceBindingInput(
        case_id=case_id, binding_id=binding.id), actor="human", entry="local-web")
    remove_change = db.execute(select(models.EvidenceChange).where(
        models.EvidenceChange.case_id == case_id,
        models.EvidenceChange.command == "remove_binding")).scalar_one()
    # 撤回解除 → 绑定恢复。
    cat = evidence_catalog.revert_change(db, EvidenceChangeIdInput(
        case_id=case_id, change_id=remove_change.id), actor="human", entry="local-web")
    restored = next(b for b in cat.bindings if b.id == binding.id)
    assert restored.removed_at is None
    # 已撤回过的变更不能再次撤回（review_state=reverted）。
    assert _codes(lambda: evidence_catalog.revert_change(db, EvidenceChangeIdInput(
        case_id=case_id, change_id=remove_change.id), actor="human", entry="local-web")) \
        == ErrorCode.INVALID_INPUT
    # 字段级守卫：先解除（after=confirmed+removed），再人工恢复并驳回该绑定，
    # 此时 after 的 removed_at/review_state 均不成立 → 撤回被拒。
    cat = evidence_catalog.remove_binding(db, RemoveEvidenceBindingInput(
        case_id=case_id, binding_id=binding.id), actor="human", entry="local-web")
    second_remove = db.execute(select(models.EvidenceChange).where(
        models.EvidenceChange.case_id == case_id,
        models.EvidenceChange.command == "remove_binding",
        models.EvidenceChange.review_state == "none")
        .order_by(models.EvidenceChange.seq.desc())).scalars().first()
    db.execute(text("UPDATE evidence_item_bindings SET removed_at=NULL, review_state='pending' WHERE id=:i"),
               {"i": str(binding.id)})
    db.commit()
    cat = evidence_catalog.reject_binding(db, EvidenceBindingRevisionInput(
        case_id=case_id, binding_id=binding.id, expected_revision=0),
        actor="human", entry="local-web")
    assert _codes(lambda: evidence_catalog.revert_change(db, EvidenceChangeIdInput(
        case_id=case_id, change_id=second_remove.id), actor="human", entry="local-web")) \
        == ErrorCode.CONFLICT


def test_revert_agent_add_group_soft_delete_keeps_row(db):
    """硬删分组的撤回需重建或保留软删除语义：这里保留软删除，行仍可查。"""
    case_id = _case(db)
    cat = evidence_catalog.add_group(db, AddEvidenceGroupInput(
        case_id=case_id, title="Agent 组", request_key="ag"), actor="agent", entry="mcp")
    change = db.execute(select(models.EvidenceChange).where(
        models.EvidenceChange.case_id == case_id)).scalar_one()
    group_id = uuid.UUID(change.diff[0]["id"])
    cat = evidence_catalog.revert_change(db, EvidenceChangeIdInput(
        case_id=case_id, change_id=change.id), actor="human", entry="local-web")
    row = db.get(models.EvidenceGroup, group_id)
    assert row is not None and row.removed_at is not None
    assert str(group_id) not in cat.group_numbers
    # 撤回本身可再撤回（软删除保留 id）：恢复该组。
    revert_change = db.execute(select(models.EvidenceChange).where(
        models.EvidenceChange.case_id == case_id,
        models.EvidenceChange.command == "revert_change")).scalar_one()
    cat = evidence_catalog.revert_change(db, EvidenceChangeIdInput(
        case_id=case_id, change_id=revert_change.id), actor="human", entry="local-web")
    assert cat.group_numbers[str(group_id)] == 1


def test_reverting_insert_preserves_unrelated_human_title_edit(db):
    case_id = _case(db)
    human = {"actor": "human", "entry": "local-web"}
    cat = evidence_catalog.add_group(db, AddEvidenceGroupInput(case_id=case_id, title="组"), **human)
    group = cat.groups[0]
    cat = evidence_catalog.add_item(db, AddEvidenceItemInput(
        case_id=case_id, group_id=group.id, title="原名"), **human)
    item_id = cat.items[0].id
    cat = evidence_catalog.add_item(db, AddEvidenceItemInput(
        case_id=case_id, group_id=group.id, title="插入草稿", position=0, request_key="revert-slot"),
        actor="agent", entry="mcp")
    proposal, = cat.pending_changes
    item = next(row for row in cat.items if row.id == item_id)
    evidence_catalog.update_item(db, UpdateEvidenceItemInput(
        case_id=case_id, item_id=item_id, expected_revision=item.revision,
        changes=EvidenceItemChanges(title="人工更名")), **human)
    cat = evidence_catalog.revert_change(db, EvidenceChangeIdInput(
        case_id=case_id, change_id=proposal.id), **human)
    restored = next(row for row in cat.items if row.id == item_id)
    assert restored.title == "人工更名"
    assert restored.position == 0
    assert cat.numbers == {str(item_id): "证据1-1"}


def test_revert_agent_item_preserves_separately_added_binding(db, store):
    case_id = _case(db)
    material = _material(db, store, case_id, name="证据.txt", content=b"TEST-ONLY binding")
    human = {"actor": "human", "entry": "local-web"}
    catalog = evidence_catalog.add_group(db, AddEvidenceGroupInput(case_id=case_id, title="组"),
                                         **human)
    catalog = evidence_catalog.add_item(db, AddEvidenceItemInput(
        case_id=case_id, group_id=catalog.groups[0].id, title="条目", request_key="bound-item"),
        actor="agent", entry="mcp")
    item = catalog.items[0]
    payload = EvidenceChangeIdInput(case_id=case_id, change_id=catalog.pending_changes[0].id)
    evidence_catalog.accept_change(db, payload, **human)
    catalog = evidence_catalog.add_binding(db, AddEvidenceBindingInput(
        case_id=case_id, item_id=item.id, material_id=material["material_id"]), **human)
    assert _codes(lambda: evidence_catalog.revert_change(db, payload, **human)) == ErrorCode.CONFLICT
    db.rollback()
    after = evidence_catalog.get_catalog(db, case_id)
    assert after.numbers == catalog.numbers
    assert after.bindings[0].removed_at is None


def test_revert_confirmed_item_blocked(db):
    case_id = _case(db)
    cat = evidence_catalog.add_group(db, AddEvidenceGroupInput(
        case_id=case_id, title="组", request_key="g"), actor="agent", entry="mcp")
    g = cat.groups[0]
    cat = evidence_catalog.add_item(db, AddEvidenceItemInput(
        case_id=case_id, group_id=g.id, title="条目", request_key="i"),
        actor="agent", entry="mcp")
    item = cat.items[0]
    add_change = db.execute(select(models.EvidenceChange).where(
        models.EvidenceChange.case_id == case_id,
        models.EvidenceChange.command == "add_item")).scalar_one()
    assert _codes(lambda: evidence_catalog.confirm_item(db, EvidenceItemRevisionInput(
        case_id=case_id, item_id=item.id, expected_revision=item.revision),
        actor="human", entry="local-web")) == ErrorCode.CONFLICT
    db.rollback()
    # 先确认组变更，再人工确认条目。
    group_change = db.execute(select(models.EvidenceChange).where(
        models.EvidenceChange.case_id == case_id,
        models.EvidenceChange.command == "add_group")).scalar_one()
    evidence_catalog.accept_change(db, EvidenceChangeIdInput(
        case_id=case_id, change_id=group_change.id), actor="human", entry="local-web")
    cat = evidence_catalog.confirm_item(db, EvidenceItemRevisionInput(
        case_id=case_id, item_id=item.id, expected_revision=item.revision),
        actor="human", entry="local-web")
    assert cat.numbers[str(item.id)] == "证据1-1"
    assert cat.pending_changes == []
    message = _msg(lambda: evidence_catalog.revert_change(db, EvidenceChangeIdInput(
        case_id=case_id, change_id=add_change.id), actor="human", entry="local-web"))
    assert "人工确认" in message


def test_snapshot_labels_ignore_pending_catalog(db, store):
    """快照编号只来自已确认目录；Agent 目录草稿确认前不影响快照。"""
    case_id = _case(db)
    g, a, b, rel = _fixture_graph(db, case_id)
    built, segment, run_id = _parsed_segment(db, store, case_id)
    cat = evidence_catalog.add_group(db, AddEvidenceGroupInput(
        case_id=case_id, title="组一"), actor="human", entry="local-web")
    eg = cat.groups[0]
    cat = evidence_catalog.add_item(db, AddEvidenceItemInput(
        case_id=case_id, group_id=eg.id, title="合同"), actor="human", entry="local-web")
    item = cat.items[0]
    cat = evidence_catalog.add_binding(db, AddEvidenceBindingInput(
        case_id=case_id, item_id=item.id, material_id=built["material_id"]),
        actor="human", entry="local-web")
    ws = graph.add_source(db, AddSourceInput(
        case_id=case_id, relation_id=rel.id, ref=SourceRefInput(segment_id=segment.id)),
        actor="human", entry="local-web")
    ws = graph.update_relation(db, UpdateRelationInput(
        case_id=case_id, relation_id=rel.id, expected_revision=rel.revision,
        changes=RelationChanges(credibility="evidenced")), actor="human", entry="local-web")
    # Agent 插入新组 + 绑定建议（pending）。
    evidence_catalog.add_group(db, AddEvidenceGroupInput(
        case_id=case_id, title="Agent 新组", position=0, request_key="ag"),
        actor="agent", entry="mcp")
    ws = graph.create_snapshot(db, CreateSnapshotInput(
        case_id=case_id, graph_id=g.id, stage_label="庭前", acknowledge_pending=True),
        actor="human", entry="local-web")
    edge = next(e for e in ws.snapshots[-1].payload["edges"]
                if e["relation_id"] == str(rel.id))
    assert edge["sources"][0]["label"].startswith("证据1-1")
    # 冻结后目录变化不影响快照正文（不可变）。
    evidence_catalog.add_group(db, AddEvidenceGroupInput(
        case_id=case_id, title="又一组", position=0), actor="human", entry="local-web")
    ws = graph.get_workspace(db, case_id)
    frozen = next(s for s in ws.snapshots if s.stage_label == "庭前")
    edge = next(e for e in frozen.payload["edges"] if e["relation_id"] == str(rel.id))
    assert edge["sources"][0]["label"].startswith("证据1-1")
    # 现行编号漂移在 diff 中体现。
    diff = graph.diff_snapshots(db, case_id, frozen.id)
    assert any(d.frozen.startswith("证据1-1") and d.current.startswith("证据2-1")
               for d in diff.label_drift)


def test_restore_snapshot_merges_diff_and_revert_restores(db, store):
    """恢复同一对象多次触碰 → diff 合并净变化；撤回恢复不逆写中间态。"""
    case_id = _case(db)
    g, a, b, rel = _fixture_graph(db, case_id)
    built, segment, run_id = _parsed_segment(db, store, case_id)
    ws = graph.add_source(db, AddSourceInput(
        case_id=case_id, relation_id=rel.id, ref=SourceRefInput(segment_id=segment.id)),
        actor="human", entry="local-web")
    ws = graph.update_relation(db, UpdateRelationInput(
        case_id=case_id, relation_id=rel.id, expected_revision=rel.revision,
        changes=RelationChanges(credibility="evidenced", label="董事")),
        actor="human", entry="local-web")
    ws = graph.create_snapshot(db, CreateSnapshotInput(
        case_id=case_id, graph_id=g.id, stage_label="庭前"), actor="human", entry="local-web")
    snap = ws.snapshots[-1]
    # 冻结后：改标签、删出处（降级）、再加一条新出处。
    rel_now = _find(ws, "relations", id=rel.id)
    ws = graph.update_relation(db, UpdateRelationInput(
        case_id=case_id, relation_id=rel.id, expected_revision=rel_now.revision,
        changes=RelationChanges(label="后来改名")), actor="human", entry="local-web")
    src = _find(ws, "sources", relation_id=rel.id)
    ws = graph.remove_source(db, RemoveSourceInput(
        case_id=case_id, source_id=src.id, downgrade_to="disputed"),
        actor="human", entry="local-web")
    ws = graph.add_source(db, AddSourceInput(
        case_id=case_id, relation_id=rel.id,
        ref=SourceRefInput(material_id=built["material_id"]), proves="冻结后新增"),
        actor="human", entry="local-web")
    ws = graph.restore_snapshot(db, RestoreSnapshotInput(
        case_id=case_id, snapshot_id=snap.id), actor="human", entry="local-web")
    rel_after = _find(ws, "relations", id=rel.id)
    assert rel_after.label == "董事"
    assert rel_after.credibility == "evidenced"
    active_sources = [s for s in ws.sources
                      if s.relation_id == rel.id and s.removed_at is None]
    assert len(active_sources) == 1  # 冻结后新增的出处被覆盖移除
    restore_change = next(c for c in ws.changes if c.command == "restore_snapshot")
    ids = [(e.target, str(e.id)) for e in restore_change.diff]
    assert len(ids) == len(set(ids)), "同一对象出现多条 diff，撤回会逆写中间态"
    # 撤回恢复 → 回到恢复前（后来改名 / disputed / 新出处）。
    ws = graph.revert_change(db, GraphChangeIdInput(
        case_id=case_id, change_id=restore_change.id), actor="human", entry="local-web")
    rel_reverted = _find(ws, "relations", id=rel.id)
    assert rel_reverted.label == "后来改名"
    assert rel_reverted.credibility == "disputed"
    active_sources = [s for s in ws.sources
                      if s.relation_id == rel.id and s.removed_at is None]
    assert len(active_sources) == 1
    assert active_sources[0].proves == "冻结后新增"


def test_cross_case_and_cross_domain_rejected(db):
    case_a = _case(db)
    case_b = _case(db)
    ws = _graph(db, case_a, "A 图")
    ga = _find(ws, "graphs", title="A 图")
    ws = _entity(db, case_a, "A 主体", graph_id=ga.id)
    ea = _find(ws, "entities", name="A 主体")
    ws_b = _graph(db, case_b, "B 图")
    gb = _find(ws_b, "graphs", title="B 图")
    # 跨案：把 A 案主体加入 B 案图。
    assert _codes(lambda: graph.add_to_graph(db, AddToGraphInput(
        case_id=case_b, graph_id=gb.id, entity_ids=[ea.id]),
        actor="human", entry="local-web")) == ErrorCode.CASE_MISMATCH
    db.rollback()
    # 跨案快照恢复 / 同意。
    ws = graph.create_snapshot(db, CreateSnapshotInput(
        case_id=case_a, graph_id=ga.id, stage_label="庭前"), actor="human", entry="local-web")
    snap_a = ws.snapshots[0]
    assert _codes(lambda: graph.restore_snapshot(db, RestoreSnapshotInput(
        case_id=case_b, snapshot_id=snap_a.id), actor="human", entry="local-web")) \
        == ErrorCode.CASE_MISMATCH
    db.rollback()
    assert _codes(lambda: graph.add_snapshot_consent(db, AddConsentInput(
        case_id=case_b, snapshot_id=snap_a.id, action="confirm", consenter="客户",
        consented_at=datetime.now(timezone.utc), channel="meeting", source_detail="x"),
        actor="human", entry="local-web")) == ErrorCode.CASE_MISMATCH
    db.rollback()
    # 跨域：graph 的 accept 不能确认证据变更，反之亦然。
    cat = evidence_catalog.add_group(db, AddEvidenceGroupInput(
        case_id=case_a, title="组", request_key="x"), actor="agent", entry="mcp")
    ev_change = db.execute(select(models.EvidenceChange).where(
        models.EvidenceChange.case_id == case_a)).scalar_one()
    assert _codes(lambda: graph.accept_change(db, GraphChangeIdInput(
        case_id=case_a, change_id=ev_change.id), actor="human", entry="local-web")) \
        == ErrorCode.NOT_FOUND
    db.rollback()
    ws = graph.add_alias(db, AddAliasInput(case_id=case_a, entity_id=ea.id, alias="别名"),
                         actor="agent", entry="mcp")
    g_change = _pending(ws, "add_alias")
    assert _codes(lambda: evidence_catalog.accept_change(db, EvidenceChangeIdInput(
        case_id=case_a, change_id=g_change.id), actor="human", entry="local-web")) \
        == ErrorCode.NOT_FOUND


def test_agent_cannot_review_freeze_consent(db):
    case_id = _case(db)
    ws = _graph(db, case_id)
    g = _find(ws, "graphs", title="TEST-ONLY 图")
    for call in (
        lambda: graph.create_snapshot(db, CreateSnapshotInput(
            case_id=case_id, graph_id=g.id, stage_label="庭前"), actor="agent", entry="mcp"),
        lambda: graph.accept_change(db, GraphChangeIdInput(
            case_id=case_id, change_id=uuid.uuid4()), actor="agent", entry="mcp"),
        lambda: graph.accept_all(db, GraphAcceptAllInput(
            case_id=case_id, expected_seq=0), actor="agent", entry="mcp"),
        lambda: graph.revert_change(db, GraphChangeIdInput(
            case_id=case_id, change_id=uuid.uuid4()), actor="agent", entry="mcp"),
        lambda: evidence_catalog.accept_change(db, EvidenceChangeIdInput(
            case_id=case_id, change_id=uuid.uuid4()), actor="agent", entry="mcp"),
        lambda: evidence_catalog.accept_all(db, EvidenceAcceptAllInput(
            case_id=case_id, expected_seq=0), actor="agent", entry="mcp"),
        lambda: evidence_catalog.revert_change(db, EvidenceChangeIdInput(
            case_id=case_id, change_id=uuid.uuid4()), actor="agent", entry="mcp"),
    ):
        assert _codes(call) == ErrorCode.FORBIDDEN
        db.rollback()


def test_request_key_idempotent_replay(db):
    case_id = _case(db)
    ws = _entity(db, case_id, "幂等主体", actor="agent", request_key="dup-1")
    seq = ws.seq
    ws = _entity(db, case_id, "幂等主体", actor="agent", request_key="dup-1")
    assert ws.seq == seq
    assert len([e for e in ws.entities if e.name == "幂等主体"]) == 1


# =============================================================================
# 4. 已发布原文 source_ref 与正常 worker
# =============================================================================

def test_unpublished_segment_cannot_be_referenced(db, store):
    """进行中的 parse_run 段不可引用；引用已发布段后解析重试不被阻断。"""
    case_id = _case(db)
    g, a, b, rel = _fixture_graph(db, case_id)
    built = _material(db, store, case_id, name="重试.txt",
                      content="TEST-ONLY 正文".encode("utf-8"))
    run_id, job_id = parsing.create_parse_run(
        db, case_id=case_id, file_version_id=built["file_version_id"])
    # 模拟解析中途崩溃：run running，部分 unit 非终态。
    with pytest.raises(RuntimeError):
        pipeline.process_parse_job(get_session_factory(), store, job_id,
                                   fault_inject=lambda stage: (_ for _ in ()).throw(
                                       RuntimeError("kill")))
    db.expire_all()
    run = db.get(models.ParseRun, run_id)
    assert run.status == "running"
    # 手工造一个属于该未完成 run 的段（正常流程不会发布它）。
    unit = models.ParseUnit(id=uuid.uuid4(), case_id=case_id, parse_run_id=run_id,
                            unit_key="text:99", status="pending")
    db.add(unit)
    db.flush()
    seg = models.Segment(id=uuid.uuid4(), case_id=case_id, parse_run_id=run_id,
                         parse_unit_id=unit.id, locator={}, text="未发布", normalized_text="未发布")
    db.add(seg)
    db.commit()
    assert _codes(lambda: graph.add_source(db, AddSourceInput(
        case_id=case_id, relation_id=rel.id, ref=SourceRefInput(segment_id=seg.id)),
        actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT
    db.rollback()
    # 正常 worker 恢复重试成功（没有被非法引用阻断）。
    pipeline.process_parse_job(get_session_factory(), store, job_id)
    db.expire_all()
    assert db.get(models.ParseRun, run_id).status in ("complete", "partial")
    version = db.get(models.FileVersion, built["file_version_id"])
    assert version.active_parse_run_id == run_id
    published = db.execute(select(models.Segment).where(
        models.Segment.parse_run_id == run_id)).scalars().first()
    assert published is not None
    ws = graph.add_source(db, AddSourceInput(
        case_id=case_id, relation_id=rel.id,
        ref=SourceRefInput(segment_id=published.id)), actor="human", entry="local-web")
    assert any(s.segment_id == published.id for s in ws.sources)


def test_old_published_segment_still_readable_after_reparse(db, store):
    """材料重新解析后，旧已发布段是不可变 source_ref：仍可引用、read_source。"""
    case_id = _case(db)
    g, a, b, rel = _fixture_graph(db, case_id)
    built, old_seg, old_run = _parsed_segment(db, store, case_id, name="重解析.txt")
    ws = graph.add_source(db, AddSourceInput(
        case_id=case_id, relation_id=rel.id, ref=SourceRefInput(segment_id=old_seg.id)),
        actor="human", entry="local-web")
    # 重新解析（新 run 成为活动版本）。
    new_run, new_job = parsing.create_parse_run(
        db, case_id=case_id, file_version_id=built["file_version_id"])
    pipeline.process_parse_job(get_session_factory(), store, new_job)
    db.expire_all()
    version = db.get(models.FileVersion, built["file_version_id"])
    assert version.active_parse_run_id == new_run
    # 旧段仍在且可读。
    out = search.read_source(db, case_id=case_id, source_ref=old_seg.id)
    assert "TEST-ONLY" in out.text
    # 旧已发布段仍可新增引用（不要求是活动 run）。
    ws = graph.add_source(db, AddSourceInput(
        case_id=case_id, entity_id=a.id, ref=SourceRefInput(segment_id=old_seg.id)),
        actor="human", entry="local-web")
    assert any(s.segment_id == old_seg.id and s.entity_id == a.id for s in ws.sources)
    # 新 run 的段也可引用。
    new_seg = db.execute(select(models.Segment).where(
        models.Segment.parse_run_id == new_run)).scalars().first()
    graph.add_source(db, AddSourceInput(
        case_id=case_id, entity_id=b.id, ref=SourceRefInput(segment_id=new_seg.id)),
        actor="human", entry="local-web")


def test_cross_case_segment_rejected(db, store):
    case_a = _case(db)
    case_b = _case(db)
    ws = _graph(db, case_b)
    gb = _find(ws, "graphs", title="TEST-ONLY 图")
    ws = _entity(db, case_b, "B 主体", graph_id=gb.id)
    eb = _find(ws, "entities", name="B 主体")
    built, seg, run = _parsed_segment(db, store, case_a)
    assert _codes(lambda: graph.add_source(db, AddSourceInput(
        case_id=case_b, entity_id=eb.id, ref=SourceRefInput(segment_id=seg.id)),
        actor="human", entry="local-web")) == ErrorCode.CASE_MISMATCH