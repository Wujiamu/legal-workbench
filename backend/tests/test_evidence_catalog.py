"""P2 evidence catalog: numbering, reordering, drafts, bindings, labels, export.

Numbering is the single source of truth (D2): only confirmed, non-removed groups,
items and bindings participate.  TEST-ONLY synthetic data.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from legal_workbench import cases, evidence_catalog, graph, models
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.schemas import (
    AddEvidenceBindingInput, AddEvidenceGroupInput, AddEvidenceItemInput, AddEntityInput,
    AddSourceInput, ArchiveCaseInput, EvidenceBindingRevisionInput, EvidenceChangeIdInput,
    EvidenceGroupChanges, EvidenceGroupRevisionInput,
    EvidenceItemChanges, EvidenceItemRevisionInput, ImportBindingRow, ImportCatalogRow,
    ImportEvidenceCatalogInput, MoveEvidenceItemInput, ReorderEvidenceGroupsInput,
    RemoveEvidenceBindingInput, SourceRefInput, UpdateEvidenceGroupInput,
    UpdateEvidenceItemInput,
)

from .graph_factories import build_graph_case


def _codes(fn):
    with pytest.raises(WorkbenchError) as exc:
        fn()
    return exc.value.code


def _group(session, case_id, title="第一组", *, actor="human", **kwargs):
    return evidence_catalog.add_group(session, AddEvidenceGroupInput(
        case_id=case_id, title=title, **kwargs), actor=actor, entry="local-web")


def _item(session, case_id, group_id, title, *, actor="human", **kwargs):
    return evidence_catalog.add_item(session, AddEvidenceItemInput(
        case_id=case_id, group_id=group_id, title=title, **kwargs),
        actor=actor, entry="local-web")


def _binding(session, case_id, item_id, material_id, *, actor="human", **kwargs):
    return evidence_catalog.add_binding(session, AddEvidenceBindingInput(
        case_id=case_id, item_id=item_id, material_id=material_id, **kwargs),
        actor=actor, entry="local-web")


def _numbers(catalog):
    return {item_id: label for item_id, label in catalog.numbers.items()}


# --- numbering --------------------------------------------------------------

def test_numbering_is_group_and_item_order(db):
    case = build_graph_case(db)
    case_id = case["case_id"]
    _group(db, case_id, "第一组：合同")
    catalog = _group(db, case_id, "第二组：履行")
    groups = catalog.groups
    first, second = groups[0], groups[1]
    _item(db, case_id, first.id, "证据名称 A")
    catalog = _item(db, case_id, first.id, "证据名称 B")
    catalog = _item(db, case_id, second.id, "证据名称 C")
    items = {item.title: item.id for item in catalog.items}
    assert catalog.numbers[str(items["证据名称 A"])] == "证据1-1"
    assert catalog.numbers[str(items["证据名称 B"])] == "证据1-2"
    assert catalog.numbers[str(items["证据名称 C"])] == "证据2-1"

    # reorder groups -> numbers follow the new group order immediately
    catalog = evidence_catalog.reorder_groups(db, ReorderEvidenceGroupsInput(
        case_id=case_id, group_ids=[second.id, first.id]), actor="human", entry="local-web")
    assert catalog.numbers[str(items["证据名称 C"])] == "证据1-1"
    assert catalog.numbers[str(items["证据名称 A"])] == "证据2-1"

    # delete the first item -> the sibling is renumbered
    item_b = next(item for item in catalog.items if item.id == items["证据名称 B"])
    catalog = evidence_catalog.remove_item(db, EvidenceItemRevisionInput(
        case_id=case_id, item_id=items["证据名称 A"],
        expected_revision=next(item.revision for item in catalog.items
                               if item.id == items["证据名称 A"])),
        actor="human", entry="local-web")
    assert catalog.numbers[str(item_b.id)] == "证据2-1"


def test_move_item_between_groups_and_before_anchor(db):
    case = build_graph_case(db)
    case_id = case["case_id"]
    catalog = _group(db, case_id, "第一组")
    first = catalog.groups[0]
    catalog = _group(db, case_id, "第二组")
    second = next(row for row in catalog.groups if row.id != first.id)
    catalog = _item(db, case_id, first.id, "A")
    catalog = _item(db, case_id, first.id, "B")
    catalog = _item(db, case_id, second.id, "C")
    ids = {item.title: item.id for item in catalog.items}
    catalog = evidence_catalog.move_item(db, MoveEvidenceItemInput(
        case_id=case_id, item_id=ids["B"], to_group_id=second.id),
        actor="human", entry="local-web")
    assert catalog.numbers[str(ids["A"])] == "证据1-1"
    assert catalog.numbers[str(ids["C"])] == "证据2-1"
    assert catalog.numbers[str(ids["B"])] == "证据2-2"
    # move before an anchor
    catalog = evidence_catalog.move_item(db, MoveEvidenceItemInput(
        case_id=case_id, item_id=ids["B"], to_group_id=second.id,
        before_item_id=ids["C"]), actor="human", entry="local-web")
    assert catalog.numbers[str(ids["B"])] == "证据2-1"
    assert catalog.numbers[str(ids["C"])] == "证据2-2"


def test_group_removal_requires_empty_and_reorders(db):
    case = build_graph_case(db)
    case_id = case["case_id"]
    catalog = _group(db, case_id, "第一组")
    first = catalog.groups[0]
    catalog = _group(db, case_id, "第二组")
    second = next(row for row in catalog.groups if row.id != first.id)
    catalog = _item(db, case_id, second.id, "C")
    item = catalog.items[0]
    assert _codes(lambda: evidence_catalog.remove_group(db, EvidenceGroupRevisionInput(
        case_id=case_id, group_id=second.id, expected_revision=second.revision),
        actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT
    catalog = evidence_catalog.remove_item(db, EvidenceItemRevisionInput(
        case_id=case_id, item_id=item.id, expected_revision=item.revision),
        actor="human", entry="local-web")
    catalog = evidence_catalog.remove_group(db, EvidenceGroupRevisionInput(
        case_id=case_id, group_id=second.id,
        expected_revision=next(row.revision for row in catalog.groups if row.id == second.id)),
        actor="human", entry="local-web")
    assert next(row for row in catalog.groups if row.id == second.id).removed_at is not None
    # a removed group does not occupy a number
    catalog = _item(db, case_id, first.id, "新条目")
    new_item = next(item for item in catalog.items if item.title == "新条目")
    assert catalog.numbers[str(new_item.id)] == "证据1-1"


def test_update_item_cas_and_validation(db):
    case = build_graph_case(db)
    case_id = case["case_id"]
    catalog = _group(db, case_id, "第一组")
    catalog = _item(db, case_id, catalog.groups[0].id, "原名")
    item = catalog.items[0]
    catalog = evidence_catalog.update_item(db, UpdateEvidenceItemInput(
        case_id=case_id, item_id=item.id, expected_revision=0,
        changes=EvidenceItemChanges(title="新名", purpose="TEST-ONLY 证明目的")),
        actor="human", entry="local-web")
    assert catalog.items[0].title == "新名"
    assert _codes(lambda: evidence_catalog.update_item(db, UpdateEvidenceItemInput(
        case_id=case_id, item_id=item.id, expected_revision=0,
        changes=EvidenceItemChanges(title="陈旧")), actor="human",
        entry="local-web")) == ErrorCode.CONFLICT
    assert _codes(lambda: evidence_catalog.update_group(db, UpdateEvidenceGroupInput(
        case_id=case_id, group_id=catalog.groups[0].id, expected_revision=0,
        changes=EvidenceGroupChanges(title="  ")), actor="human",
        entry="local-web")) == ErrorCode.INVALID_INPUT


# --- drafts from external imports (D2) --------------------------------------

def test_imported_drafts_stay_pending_until_confirmed(db):
    case = build_graph_case(db)
    case_id = case["case_id"]
    _group(db, case_id, "工作稿第一组")
    group_id = db.execute(select(models.EvidenceGroup.id).where(
        models.EvidenceGroup.case_id == case_id)).scalar_one()
    catalog = _item(db, case_id, group_id, "现有证据")
    existing_id = catalog.items[0].id
    assert catalog.numbers[str(existing_id)] == "证据1-1"

    catalog = evidence_catalog.import_catalog(db, ImportEvidenceCatalogInput(
        case_id=case_id, import_material_id=case["second_material_id"],
        rows=[ImportCatalogRow(
            group_title="导入组", item_title="导入证据一",
            bindings=[ImportBindingRow(material_id=case["material_id"], page_start=1,
                                       page_end=3)]),
              ImportCatalogRow(group_title="导入组", item_title="导入证据二")],
        request_key="import-1"), actor="human", entry="local-web")
    drafts = [item for item in catalog.items if item.source_kind == "imported"]
    assert len(drafts) == 2 and all(item.review_state == "pending" for item in drafts)
    # pending drafts never take part in numbering
    assert all(str(item.id) not in catalog.numbers for item in drafts)
    assert catalog.numbers[str(existing_id)] == "证据1-1"
    pending_bindings = [row for row in catalog.bindings if row.review_state == "pending"]
    assert len(pending_bindings) == 1

    # confirming the first draft gives it the next number in its group
    draft = next(item for item in drafts if item.title == "导入证据一")
    catalog = evidence_catalog.confirm_item(db, EvidenceItemRevisionInput(
        case_id=case_id, item_id=draft.id, expected_revision=draft.revision),
        actor="human", entry="local-web")
    assert catalog.numbers[str(draft.id)] == "证据2-1"
    # rejecting the other keeps it out
    other = next(item for item in drafts if item.title == "导入证据二")
    catalog = evidence_catalog.reject_item(db, EvidenceItemRevisionInput(
        case_id=case_id, item_id=other.id, expected_revision=other.revision),
        actor="human", entry="local-web")
    assert str(other.id) not in catalog.numbers
    # import replay is idempotent
    catalog = evidence_catalog.import_catalog(db, ImportEvidenceCatalogInput(
        case_id=case_id, import_material_id=case["second_material_id"],
        rows=[ImportCatalogRow(group_title="导入组", item_title="导入证据一")],
        request_key="import-1"), actor="agent", entry="mcp")
    assert len([item for item in catalog.items if item.source_kind == "imported"]) == 2


def test_import_draft_requires_request_key_and_case_material(db):
    case = build_graph_case(db)
    other = build_graph_case(db)
    assert _codes(lambda: evidence_catalog.import_catalog(db, ImportEvidenceCatalogInput(
        case_id=case["case_id"], import_material_id=case["second_material_id"],
        rows=[ImportCatalogRow(group_title="组", item_title="条目")]),
        actor="agent", entry="mcp")) == ErrorCode.INVALID_INPUT
    assert _codes(lambda: evidence_catalog.import_catalog(db, ImportEvidenceCatalogInput(
        case_id=case["case_id"], import_material_id=other["material_id"],
        rows=[ImportCatalogRow(group_title="组", item_title="条目")],
        request_key="x"), actor="human", entry="local-web")) == ErrorCode.CASE_MISMATCH


# --- labels resolved from the confirmed catalog -----------------------------

def test_source_labels_resolve_pages_and_multiple_hits(db):
    case = build_graph_case(db)
    case_id = case["case_id"]
    catalog = _group(db, case_id, "第一组：合同")
    group = catalog.groups[0]
    catalog = _item(db, case_id, group.id, "合同文本")
    item = catalog.items[0]
    catalog = _binding(db, case_id, item.id, case["material_id"], page_start=1, page_end=5)
    snap = graph.add_entity(db, AddEntityInput(
        case_id=case_id, kind="org", name="TEST-ONLY 甲"), actor="human", entry="local-web")
    entity = snap.entities[0]
    snap = graph.add_source(db, AddSourceInput(
        case_id=case_id, entity_id=entity.id,
        ref=SourceRefInput(segment_id=case["segment_id"])), actor="human", entry="local-web")
    source_id = snap.sources[0].id
    label = snap.labels[str(source_id)]
    # the segment is on page 2 and the binding covers pages 1-5
    assert label.labels == ["证据1-1 第2页"]
    assert label.text == "证据1-1 第2页"
    assert label.page == 2 and label.material_id == case["material_id"]

    # a second item bound to the same pages yields both labels, in catalog order
    catalog = _item(db, case_id, group.id, "合同附件")
    second = catalog.items[1]
    catalog = _binding(db, case_id, second.id, case["material_id"])
    snap = graph.get_workspace(db, case_id)
    assert snap.labels[str(source_id)].labels == ["证据1-1 第2页", "证据1-2 第2页"]

    # removing the binding removes the number but keeps the material name
    binding = next(row for row in catalog.bindings if row.item_id == second.id)
    catalog = evidence_catalog.remove_binding(db, RemoveEvidenceBindingInput(
        case_id=case_id, binding_id=binding.id), actor="human", entry="local-web")
    snap = graph.get_workspace(db, case_id)
    assert snap.labels[str(source_id)].labels == ["证据1-1 第2页"]

    # deleting the item drops its label entirely
    item_row = next(row for row in catalog.items if row.id == item.id)
    catalog = evidence_catalog.remove_item(db, EvidenceItemRevisionInput(
        case_id=case_id, item_id=item.id, expected_revision=item_row.revision),
        actor="human", entry="local-web")
    snap = graph.get_workspace(db, case_id)
    assert snap.labels[str(source_id)].text.endswith("（未编号）")


def test_unconfirmed_binding_does_not_change_numbering(db):
    case = build_graph_case(db)
    case_id = case["case_id"]
    catalog = _group(db, case_id, "第一组")
    catalog = _item(db, case_id, catalog.groups[0].id, "合同文本")
    item = catalog.items[0]
    # agent binding is pending -> no label yet
    catalog = _binding(db, case_id, item.id, case["material_id"], actor="agent",
                       request_key="agent-binding-1")
    assert all(row.review_state == "pending" for row in catalog.bindings)
    snap = graph.add_entity(db, AddEntityInput(
        case_id=case_id, kind="org", name="TEST-ONLY 甲"), actor="human", entry="local-web")
    entity = snap.entities[0]
    snap = graph.add_source(db, AddSourceInput(
        case_id=case_id, entity_id=entity.id,
        ref=SourceRefInput(material_id=case["material_id"])), actor="human", entry="local-web")
    source_id = snap.sources[0].id
    assert snap.labels[str(source_id)].labels == []
    binding = catalog.bindings[0]
    # Agent 提案的确认入口是 accept_change（与网页复核面板一致）；
    # confirm_binding 用于人工导入产生的待确认绑定。
    catalog = evidence_catalog.accept_change(db, EvidenceChangeIdInput(
        case_id=case_id, change_id=catalog.pending_changes[0].id),
        actor="human", entry="local-web")
    snap = graph.get_workspace(db, case_id)
    assert snap.labels[str(source_id)].labels == ["证据1-1"]
    # agent cannot confirm its own proposal
    assert _codes(lambda: evidence_catalog.reject_binding(db, EvidenceBindingRevisionInput(
        case_id=case_id, binding_id=binding.id, expected_revision=1),
        actor="agent", entry="mcp")) == ErrorCode.FORBIDDEN


# --- guards -----------------------------------------------------------------

def test_binding_same_case_archive_and_request_key(db):
    case = build_graph_case(db)
    other = build_graph_case(db)
    case_id = case["case_id"]
    catalog = _group(db, case_id, "第一组")
    catalog = _item(db, case_id, catalog.groups[0].id, "合同文本")
    item = catalog.items[0]
    assert _codes(lambda: _binding(db, case_id, item.id, other["material_id"])) == ErrorCode.CASE_MISMATCH
    catalog = _binding(db, case_id, item.id, case["material_id"], actor="agent",
                       request_key="bind-1")
    # same request key + same triple is an idempotent replay
    catalog = _binding(db, case_id, item.id, case["material_id"], actor="agent",
                       request_key="bind-1")
    assert len(catalog.bindings) == 1
    assert _codes(lambda: _binding(db, case_id, item.id, case["material_id"])) == ErrorCode.INVALID_INPUT

    case_row = db.get(models.Case, case_id)
    cases.archive_case(db, ArchiveCaseInput(case_id=case_id, expected_revision=case_row.revision))
    assert _codes(lambda: _item(db, case_id, catalog.groups[0].id, "归档后")) == ErrorCode.INVALID_INPUT
    assert _codes(lambda: evidence_catalog.reorder_groups(db, ReorderEvidenceGroupsInput(
        case_id=case_id, group_ids=[catalog.groups[0].id]), actor="human",
        entry="local-web")) == ErrorCode.INVALID_INPUT
    # reads still work while archived
    assert evidence_catalog.get_catalog(db, case_id).groups


def test_agent_cannot_use_human_only_evidence_commands(db):
    case = build_graph_case(db)
    case_id = case["case_id"]
    catalog = _group(db, case_id, "第一组")
    catalog = _item(db, case_id, catalog.groups[0].id, "合同文本")
    item = catalog.items[0]
    assert _codes(lambda: evidence_catalog.confirm_item(db, EvidenceItemRevisionInput(
        case_id=case_id, item_id=item.id, expected_revision=0), actor="agent",
        entry="mcp")) == ErrorCode.FORBIDDEN
    assert _codes(lambda: evidence_catalog.remove_item(db, EvidenceItemRevisionInput(
        case_id=case_id, item_id=item.id, expected_revision=0), actor="agent",
        entry="mcp")) == ErrorCode.FORBIDDEN
    assert _codes(lambda: evidence_catalog.reorder_groups(db, ReorderEvidenceGroupsInput(
        case_id=case_id, group_ids=[catalog.groups[0].id]), actor="agent",
        entry="mcp")) == ErrorCode.FORBIDDEN


def test_remove_item_removes_bindings_and_is_revertible(db):
    case = build_graph_case(db)
    case_id = case["case_id"]
    catalog = _group(db, case_id, "第一组")
    catalog = _item(db, case_id, catalog.groups[0].id, "合同文本")
    item = catalog.items[0]
    catalog = _binding(db, case_id, item.id, case["material_id"])
    binding_id = catalog.bindings[0].id
    catalog = evidence_catalog.remove_item(db, EvidenceItemRevisionInput(
        case_id=case_id, item_id=item.id, expected_revision=0),
        actor="human", entry="local-web")
    assert all(row.removed_at is not None for row in catalog.bindings if row.id == binding_id)
    change_id = catalog.last_change_id
    catalog = evidence_catalog.revert_change(db, EvidenceChangeIdInput(
        case_id=case_id, change_id=change_id), actor="human", entry="local-web")
    assert next(row for row in catalog.items if row.id == item.id).removed_at is None
    assert next(row for row in catalog.bindings if row.id == binding_id).removed_at is None


# --- export and table parsing ----------------------------------------------

def test_export_csv_contains_numbers_and_pages(db):
    case = build_graph_case(db)
    case_id = case["case_id"]
    catalog = _group(db, case_id, "第一组：合同")
    catalog = _item(db, case_id, catalog.groups[0].id, "合同文本", purpose="TEST-ONLY 目的")
    item = catalog.items[0]
    _binding(db, case_id, item.id, case["material_id"], page_start=2, page_end=4)
    text = evidence_catalog.export_catalog_csv(db, case_id)
    assert "组号,组名,序号,编号,证据名称" in text
    assert "证据1-1" in text and "合同文本" in text and "2-4" in text


def test_table_to_draft_rows_parses_headers_pages_and_groups():
    rows = [
        ["TEST-ONLY 证据目录"],
        ["组名", "证据名称", "证明目的", "对应材料", "页码"],
        ["第一组：合同", "证据1-1 股权转让协议", "证明股权变更", "虚构合同.txt", "2-4"],
        ["", "证据1-2 付款凭证", "证明已付款", "虚构流水.txt", "第5页"],
        ["第二组：往来", "证据2-1 微信记录", "证明催告", "虚构微信.txt", ""],
    ]
    drafts = evidence_catalog.table_to_draft_rows(rows)
    assert [d["item_title"] for d in drafts] == [
        "股权转让协议", "付款凭证", "微信记录"]
    assert drafts[0]["group_title"] == "第一组：合同"
    assert drafts[0]["page_start"] == 2 and drafts[0]["page_end"] == 4
    assert drafts[1]["group_title"] == "第一组：合同"  # carried forward
    assert drafts[1]["page_start"] == 5 and drafts[1]["page_end"] == 5
    assert drafts[2]["group_title"] == "第二组：往来"
    assert drafts[0]["material_title"] == "虚构合同.txt"


def test_table_to_draft_rows_rejects_missing_header():
    with pytest.raises(WorkbenchError) as exc:
        evidence_catalog.table_to_draft_rows([["甲", "乙"], ["1", "2"]])
    assert exc.value.code == ErrorCode.UNSUPPORTED_FORMAT
