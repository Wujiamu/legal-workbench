"""审查：进度一致性检查（主计划第十节）。

用合成数据制造悬空父节点、重复位置、多进行中阶段、悬空关系端点，
验证 check-consistency 能报出具体案件与节点；正常进度树不误报。
真实 PostgreSQL；TEST-ONLY 数据。
"""
from __future__ import annotations

import uuid

from sqlalchemy import text

from legal_workbench import consistency, progress
from legal_workbench.schemas import AddNodeInput, AddRelationInput, SetActiveStageInput
from legal_workbench.storage import Storage

from .factories import create_case


def _add(db, case_id, *, parent_id=None, kind="stage", title="阶段", state=None):
    return progress.add_node(db, AddNodeInput(
        case_id=case_id, parent_id=parent_id, kind=kind, title=title, state=state),
        actor="human", entry="local-web")


def _node(snap, title):
    return next(n for n in snap.nodes if n.title == title)


def test_clean_progress_tree_reports_no_progress_issues(db, tmp_path):
    store = Storage(root=tmp_path / "data")
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    _add(db, case_id, parent_id=program.id, kind="stage", title="立案")
    _add(db, case_id, parent_id=program.id, kind="stage", title="庭审")
    report = consistency.check_consistency(db, store, case_id=case_id)
    assert report.checked_progress_nodes >= 3
    assert not any(i.kind.startswith("progress_") for i in report.issues), report.to_dict()


def test_dangling_parent_is_reported(db, tmp_path):
    store = Storage(root=tmp_path / "data")
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    snap = _add(db, case_id, parent_id=program.id, kind="work", title="证据整理")
    work = _node(snap, "证据整理")
    # 直接软删除父节点（绕过服务的子节点检查），制造悬空子节点
    db.execute(text("UPDATE progress_nodes SET removed_at=now() WHERE id=:id"),
               {"id": str(program.id)})
    db.commit()
    report = consistency.check_consistency(db, store, case_id=case_id)
    issues = [i for i in report.issues if i.kind == "progress_dangling_parent"]
    assert len(issues) == 1
    assert issues[0].object_id == str(work.id)
    assert issues[0].case_id == str(case_id)


def test_duplicate_position_is_reported(db, tmp_path):
    store = Storage(root=tmp_path / "data")
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    _add(db, case_id, parent_id=program.id, kind="stage", title="甲")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="乙")
    yi = _node(snap, "乙")
    # 制造重复位置
    db.execute(text("UPDATE progress_nodes SET position=0 WHERE id=:id"), {"id": str(yi.id)})
    db.commit()
    report = consistency.check_consistency(db, store, case_id=case_id)
    assert any(i.kind == "progress_duplicate_position" for i in report.issues)


def test_multiple_active_stages_is_reported(db, tmp_path):
    store = Storage(root=tmp_path / "data")
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="庭前程序", state="in_progress")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="庭审")
    hearing = _node(snap, "庭审")
    # 绕过服务联动，直接把第二个阶段也设为进行中
    db.execute(text("UPDATE progress_nodes SET state='in_progress' WHERE id=:id"),
               {"id": str(hearing.id)})
    db.commit()
    report = consistency.check_consistency(db, store, case_id=case_id)
    issues = [i for i in report.issues if i.kind == "progress_multiple_active_stages"]
    assert len(issues) == 1
    assert "庭前程序" in issues[0].detail and "庭审" in issues[0].detail


def test_relation_dangling_endpoint_is_reported(db, tmp_path):
    store = Storage(root=tmp_path / "data")
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    a = _node(snap, "一审")
    snap = _add(db, case_id, kind="program", title="二审")
    b = _node(snap, "二审")
    progress.add_relation(db, AddRelationInput(
        case_id=case_id, from_node_id=a.id, to_node_id=b.id, relation_type="causes"),
        actor="human", entry="local-web")
    # 软删除一个端点，关系仍有效 → 悬空端点
    db.execute(text("UPDATE progress_nodes SET removed_at=now() WHERE id=:id"), {"id": str(b.id)})
    db.commit()
    report = consistency.check_consistency(db, store, case_id=case_id)
    assert any(i.kind == "progress_relation_dangling_endpoint" for i in report.issues)


def test_whole_database_check_keeps_cases_separate(db, tmp_path):
    """整库检查不得把不同案件的根节点当成同级：各自一个进行中阶段是合法的。"""
    store = Storage(root=tmp_path / "data")
    cases = [create_case(db), create_case(db)]
    db.commit()
    for case_id, program_title in zip(cases, ("一审甲", "一审乙")):
        snap = _add(db, case_id, kind="program", title=program_title)
        program = _node(snap, program_title)
        snap = _add(db, case_id, parent_id=program.id, kind="stage", title="庭前程序")
        stage = _node(snap, "庭前程序")
        progress.set_active_stage(db, SetActiveStageInput(
            case_id=case_id, stage_id=stage.id, expected_revision=stage.revision),
            actor="human", entry="local-web")
    report = consistency.check_consistency(db, store)  # 整库，不限定案件
    assert report.checked_progress_nodes >= 4
    assert not any(i.kind.startswith("progress_") for i in report.issues), report.to_dict()