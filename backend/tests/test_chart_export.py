"""External export and read-only source bundles use TEST-ONLY PostgreSQL fixtures."""
import pytest

from legal_workbench import chart_export as export, evidence_catalog, graph
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.schemas import (
    AddEvidenceBindingInput, AddEvidenceGroupInput, AddEvidenceItemInput,
    AddEntityInput, AddGraphRelationInput, RelationChanges, UpdateRelationInput,
)
from .factories import create_case
from .test_claims import _group, _item
from .test_graph_snapshot import _freeze, _setup


def _catalog(db, case):
    actor = dict(actor="human", entry="local-web")
    cid = case["case_id"]
    catalog = evidence_catalog.add_group(db, AddEvidenceGroupInput(
        case_id=cid, title="TEST-ONLY 第一组"), **actor)
    catalog = evidence_catalog.add_item(db, AddEvidenceItemInput(
        case_id=cid, group_id=catalog.groups[0].id, title="TEST-ONLY 目录合同"), **actor)
    evidence_catalog.add_binding(db, AddEvidenceBindingInput(
        case_id=cid, item_id=catalog.items[0].id, material_id=case["material_id"]), **actor)


def test_graph_projection_filters_and_uses_live_catalog_on_frozen_sources(db):
    case, graph_id, a, b, relation, _ = _setup(db)
    cid = case["case_id"]
    graph.update_relation(db, UpdateRelationInput(
        case_id=cid, relation_id=relation.id, expected_revision=relation.revision,
        changes=RelationChanges(note="不应泄漏的关系备注")), actor="human", entry="local-web")
    graph.add_relation(db, AddGraphRelationInput(
        case_id=cid, from_entity_id=b.id, to_entity_id=a.id, graph_id=graph_id,
        kind="affiliate", credibility="ruled_out", label="不应泄漏的排除关系"),
        actor="human", entry="local-web")
    snap = _freeze(db, cid, graph_id).snapshots[0]
    req = export.CitationRequest(chart_type="graph", target_id=graph_id,
                                 profile="court", snapshot_id=snap.id)
    with pytest.raises(WorkbenchError) as blocked:
        export.chart_citation_table(db, cid, req)
    assert blocked.value.code == ErrorCode.CONFLICT
    assert "uncataloged_source" in str(blocked.value.details)
    db.rollback()

    _catalog(db, case)
    table = export.chart_citation_table(db, cid, req)
    assert [(row.index, row.evidence_number, row.location) for row in table.rows] == [(1, "证据1-1", "第2页")]
    assert table.rows[0].evidence_name == "TEST-ONLY 目录合同"
    assert table.chart_meta.snapshot_version == 1
    bundle = export.build_chart_audit_bundle(db, cid, req)
    origin = next(a for a in bundle.assertions if a.object_type == "relation").sources[0]
    assert origin.segment_id == case["segment_id"] and origin.context == "TEST-ONLY 股权转让协议第一条"
    assert any("冻结时不同" in issue.message for issue in bundle.preflight.issues)
    assert "不应泄漏" not in bundle.model_dump_json()
    assert "不应泄漏" not in export.render_citation_table(table, "md")
    assert "不应泄漏" not in export.render_citation_table(table, "csv")


def test_pending_and_suspected_gate_and_same_case_checks(db):
    case, graph_id, a, b, _, _ = _setup(db)
    cid = case["case_id"]
    result = graph.add_relation(db, AddGraphRelationInput(
        case_id=cid, from_entity_id=b.id, to_entity_id=a.id, graph_id=graph_id,
        kind="affiliate", credibility="suspected"), actor="human", entry="local-web")
    suspect = next(item for item in result.relations if item.credibility == "suspected")
    snap = _freeze(db, cid, graph_id).snapshots[0]
    _catalog(db, case)
    req = export.CitationRequest(chart_type="graph", target_id=graph_id,
                                 profile="court", snapshot_id=snap.id)
    preflight = export.preflight_chart_export(db, cid, req)
    assert {issue.code for issue in preflight.issues if issue.severity == "blocker"} == {
        "missing_source", "suspected_in_court"}
    waivers = [export.ChartWaiver(code=issue.code, target_id=suspect.id, reason="TEST-ONLY 说明")
               for issue in preflight.issues if issue.severity == "blocker"]
    with pytest.raises(WorkbenchError) as error:
        export.chart_citation_table(db, cid, req.model_copy(update={"waivers": waivers}))
    assert error.value.code == ErrorCode.FORBIDDEN
    table = export.chart_citation_table(db, cid, req.model_copy(update={"waivers": waivers}), human=True)
    assert any(assertion.held_for_proof for assertion in table.assertions)
    assert "TEST-ONLY 说明" in export.render_citation_table(table, "md")
    graph.add_entity(db, AddEntityInput(
        case_id=cid, kind="person", name="绝不导出的待确认主体", graph_id=graph_id,
        request_key="TEST-ONLY pending"), actor="agent", entry="mcp")
    client = req.model_copy(update={"profile": "client", "snapshot_id": None})
    bundle = export.build_chart_audit_bundle(db, cid, client)
    assert "绝不导出" not in bundle.model_dump_json()
    assert any(issue.code == "pending_changes_present" and issue.severity == "warning"
               for issue in bundle.preflight.issues)
    assert any(issue.code == "pending_changes_present" and issue.severity == "blocker"
               for issue in export.preflight_chart_export(db, cid, req).issues)
    foreign = create_case(db)
    db.commit()
    with pytest.raises(WorkbenchError) as error:
        export.build_chart_audit_bundle(db, foreign, client)
    assert error.value.code == ErrorCode.CASE_MISMATCH


def test_claims_digest_covers_frozen_references_and_no_calc_notes(db):
    case, *_ = _setup(db)
    cid = case["case_id"]
    _catalog(db, case)
    group = _group(db, cid).groups[0]
    _item(db, cid, group.id, 500_00, segment_id=case["segment_id"])
    req = export.CitationRequest(chart_type="claims", target_id=group.id, profile="court")
    result = export.build_chart_audit_bundle(db, cid, req)
    table = export.chart_citation_table(db, cid, req)
    assert result.chart_meta.digest == table.chart_meta.digest and len(result.chart_meta.digest) == 64
    assert result.chart_meta.frozen_at is not None
    assert result.assertions[0].amount_cents == 500_00
    assert result.assertions[0].sources[0].context.startswith("TEST-ONLY")
    assert "calc_note" not in result.model_dump_json()
    assert "relative_source" not in result.model_dump_json()


def test_claims_pending_isolation_and_csv_formula_escape(db):
    from legal_workbench import claims
    from legal_workbench.schemas import (
        AddClaimGroupInput, AddClaimItemInput, ClaimItemChanges, UpdateClaimItemInput,
    )

    case, graph_id, *_ = _setup(db)
    cid = case["case_id"]
    _catalog(db, case)
    group = _group(db, cid).groups[0]
    snap = _item(db, cid, group.id, 100_00, title="=CMD|已确认本金", segment_id=case["segment_id"])
    confirmed_item = snap.items[0]

    # Test CSV formula escaping cleanly on confirmed group before staging pending changes
    suspect_snap = _item(db, cid, group.id, 250_00, title="@待核定费用")
    suspect_item = next(i for i in suspect_snap.items if i.title == "@待核定费用")
    waived_claims = export.CitationRequest(
        chart_type="claims", target_id=group.id, profile="court",
        waivers=[export.ChartWaiver(
            code="missing_source", target_id=suspect_item.id, reason="=HYPERLINK(\"http://evil\",\"click\")",
        )],
    )
    table = export.chart_citation_table(db, cid, waived_claims, human=True)
    csv_out = export.render_citation_table(table, "csv")
    assert "'=CMD|已确认本金" in csv_out
    waived_leading = table.model_copy(update={
        "waivers": [export.ChartWaiver(
            code="=EVIL_CODE", target_id=suspect_item.id, reason="=1+1",
        )],
    })
    csv_leading = export.render_citation_table(waived_leading, "csv")
    assert "'=EVIL_CODE" in csv_leading

    # Agent pending proposals (new group, new item, update item) are staged only
    # and must never leak into external exports or audit bundles.
    claims.add_group(db, AddClaimGroupInput(
        case_id=cid, title="绝不外泄的待确认分组", request_key="agent-grp-1",
    ), actor="agent", entry="mcp")
    claims.add_item(db, AddClaimItemInput(
        case_id=cid, group_id=group.id, title="绝不外泄的待确认款项",
        category="interest", amount_cents=999_00, request_key="agent-item-1",
    ), actor="agent", entry="mcp")
    claims.update_item(db, UpdateClaimItemInput(
        case_id=cid, item_id=confirmed_item.id, expected_revision=confirmed_item.revision,
        changes=ClaimItemChanges(title="绝不外泄的待确认修改", amount_cents=888_00),
        request_key="agent-upd-1",
    ), actor="agent", entry="mcp")

    req_client = export.CitationRequest(chart_type="claims", target_id=group.id, profile="client")
    bundle_client = export.build_chart_audit_bundle(db, cid, req_client)
    dumped = bundle_client.model_dump_json()
    assert "绝不外泄" not in dumped
    assert "999.00" not in dumped and "888.00" not in dumped
    assert any(i.code == "pending_changes_present" and i.severity == "warning"
               for i in bundle_client.preflight.issues)

    # Confirm missing_snapshot and pending_changes_present blockers in court mode
    graph_court = export.CitationRequest(chart_type="graph", target_id=graph_id, profile="court")
    pref_missing = export.preflight_chart_export(db, cid, graph_court)
    assert any(i.code == "missing_snapshot" and i.severity == "blocker" for i in pref_missing.issues)
    claims_court = export.preflight_chart_export(db, cid, req_client.model_copy(update={"profile": "court"}))
    assert any(i.code == "pending_changes_present" and i.severity == "blocker" for i in claims_court.issues)
