"""TEST-ONLY tests for Wave 1C audit helpers and Wave 2A candidate extraction."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from legal_workbench import evidence_catalog, graph, graph_assist, models
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.schemas import (
    AddAliasInput,
    AddEntityInput,
    AddEvidenceBindingInput,
    AddEvidenceGroupInput,
    AddEvidenceItemInput,
    AddGraphRelationInput,
    AddSourceInput,
    AddToGraphInput,
    CreateGraphInput,
    EvidenceBindingRevisionInput,
    SourceRefInput,
)
from legal_workbench.textnorm import normalize_with_map

from .factories import (
    build_material,
    create_file_version,
    create_intake,
    create_intake_item,
    create_material,
    create_stored_object,
)
from .graph_factories import build_annotation


def _make_uscc(prefix17: str) -> str:
    """Compute a valid GB 32100 18-char Unified Social Credit Code."""
    charset = "0123456789ABCDEFGHJKLMNPQRTUWXY"
    weights = (1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28)
    total = sum(charset.index(ch) * w for ch, w in zip(prefix17, weights))
    return prefix17 + charset[(31 - (total % 31)) % 31]


def _add_material_with_run(
    db,
    case_id,
    *,
    display_name: str,
    category: str = "合同",
    kind: str = "original",
    run_status: str = "complete",
    unit_status: str = "extracted",
    coverage: dict | None = None,
    segments: list[tuple[dict, str]] | None = None,
    activate: bool = True,
):
    intake_id = create_intake(db, case_id)
    item_id = create_intake_item(db, case_id, intake_id, f"虚构/{uuid4().hex}_{display_name}")
    obj_id = create_stored_object(db, case_id, content=f"TEST-ONLY {uuid4().hex}".encode())
    mat = models.Material(
        id=uuid4(),
        case_id=case_id,
        intake_item_id=item_id,
        display_name=display_name,
        category=category,
    )
    db.add(mat)
    db.flush()
    fv_id = create_file_version(db, case_id, mat.id, obj_id, kind=kind)
    fv = db.get(models.FileVersion, fv_id)
    if kind == "original":
        mat.original_file_version_id = fv_id

    run = models.ParseRun(
        id=uuid4(),
        case_id=case_id,
        file_version_id=fv_id,
        parser_version="TEST-ONLY",
        sequence=0,
        status=run_status,
        coverage=coverage or {},
    )
    db.add(run)
    db.flush()
    if activate:
        fv.active_parse_run_id = run.id

    seg_ids = []
    for idx, (locator, text) in enumerate(segments or []):
        unit = models.ParseUnit(
            id=uuid4(),
            case_id=case_id,
            parse_run_id=run.id,
            unit_key=f"TEST-ONLY:{idx}:{uuid4().hex}",
            status=unit_status,
            method="native",
        )
        db.add(unit)
        db.flush()
        norm_text, offset_map = normalize_with_map(text)
        seg = models.Segment(
            id=uuid4(),
            case_id=case_id,
            parse_run_id=run.id,
            parse_unit_id=unit.id,
            locator=locator,
            text=text,
            normalized_text=norm_text,
            offset_map=offset_map,
        )
        db.add(seg)
        db.flush()
        seg_ids.append(seg.id)
    if not segments and unit_status in ("failed", "unsupported", "pending", "extracted"):
        unit = models.ParseUnit(
            id=uuid4(),
            case_id=case_id,
            parse_run_id=run.id,
            unit_key=f"TEST-ONLY:0:{uuid4().hex}",
            status=unit_status,
            method="native",
        )
        db.add(unit)
        db.flush()
    db.flush()
    return {"material_id": mat.id, "file_version_id": fv_id, "run_id": run.id, "segment_ids": seg_ids}


def test_corpus_coverage_matrix_all_buckets_and_graph_scoping(db):
    base = build_material(db, relative_source="虚构/初始化.txt")
    case_id = base["case_id"]
    # Keep the initial material unparsed (pending_or_unsupported).
    db.get(models.Material, base["material_id"]).category = "待处理"
    db.get(models.Material, base["material_id"]).display_name = "00_未解析材料.txt"

    # 1. Referenced via GraphSource (entity in Graph 1 only)
    m_graph = _add_material_with_run(
        db, case_id, display_name="01_股权协议.pdf", category="合同",
        coverage={"complete": 1, "partial": 0, "failed": 0, "pending": 0, "unsupported": 0, "total": 1},
        segments=[({"page": 1}, "TEST-ONLY 协议正文")],
    )
    # 2. Referenced via EvidenceItemBinding (confirmed) + Annotation
    m_ev_ann = _add_material_with_run(
        db, case_id, display_name="02_银行回单.pdf", category="流水",
        coverage={"complete": 2, "partial": 0, "failed": 0, "pending": 0, "unsupported": 0, "total": 2},
        segments=[({"page": 1}, "TEST-ONLY 流水页")],
    )
    build_annotation(db, case_id=case_id, file_version_id=m_ev_ann["file_version_id"], page=1)

    # 3. Referenced via ClaimItem only
    m_claim = _add_material_with_run(
        db, case_id, display_name="03_违约清单.xlsx", category="流水",
        coverage={"complete": 1, "partial": 0, "failed": 0, "pending": 0, "unsupported": 0, "total": 1},
        segments=[({"sheet": "Sheet1"}, "TEST-ONLY 违约金 5000 元")],
    )

    # 4. Parsed unused (complete, only has a rejected EvidenceItemBinding & removed ClaimItem)
    m_unused = _add_material_with_run(
        db, case_id, display_name="04_闲置函件.pdf", category="函件",
        coverage={"complete": 1, "partial": 0, "failed": 0, "pending": 0, "unsupported": 0, "total": 1},
        segments=[({"page": 1}, "TEST-ONLY 未引用函件")],
    )

    # 5. Partial or failed (even though also referenced by GraphSource, priority puts it in partial_or_failed)
    m_failed = _add_material_with_run(
        db, case_id, display_name="05_损坏扫描件.pdf", category="函件",
        run_status="partial",
        coverage={"complete": 1, "partial": 0, "failed": 1, "pending": 0, "unsupported": 0, "total": 2},
        segments=[({"page": 1}, "TEST-ONLY 部分失败页")],
    )

    # 6. Pending or unsupported (unsupported run)
    m_unsup = _add_material_with_run(
        db, case_id, display_name="06_压缩包.zip", category="待处理",
        run_status="unsupported", unit_status="unsupported",
        coverage={"complete": 0, "partial": 0, "failed": 0, "pending": 0, "unsupported": 1, "total": 1},
    )

    # 7. Material with original_file_version_id=None falling back to latest working FileVersion + GraphSource(annotation_id)
    m_fallback = _add_material_with_run(
        db, case_id, display_name="07_工作副本补录.pdf", category="合同", kind="working",
        coverage={"complete": 1, "partial": 0, "failed": 0, "pending": 0, "unsupported": 0, "total": 1},
        segments=[({"page": 1}, "TEST-ONLY 补录版本")],
    )
    fallback_ann_id = build_annotation(db, case_id=case_id, file_version_id=m_fallback["file_version_id"], page=1)

    db.commit()

    # Build Graph 1 and Graph 2
    ws = graph.create_graph(db, CreateGraphInput(case_id=case_id, title="主图"), actor="human", entry="local-web")
    g1_id = ws.graphs[0].id
    ws = graph.create_graph(db, CreateGraphInput(case_id=case_id, title="子图"), actor="human", entry="local-web")
    g2_id = next(g.id for g in ws.graphs if g.title == "子图")

    ws = graph.add_entity(db, AddEntityInput(case_id=case_id, kind="org", name="甲公司", graph_id=g1_id),
                          actor="human", entry="local-web")
    e1_id = next(e.id for e in ws.entities if e.name == "甲公司")
    ws = graph.add_entity(db, AddEntityInput(case_id=case_id, kind="org", name="乙公司", graph_id=g2_id),
                          actor="human", entry="local-web")
    e2_id = next(e.id for e in ws.entities if e.name == "乙公司")

    graph.add_source(
        db,
        AddSourceInput(case_id=case_id, entity_id=e1_id, ref=SourceRefInput(segment_id=m_graph["segment_ids"][0])),
        actor="human", entry="local-web",
    )
    graph.add_source(
        db,
        AddSourceInput(case_id=case_id, entity_id=e1_id, ref=SourceRefInput(material_id=m_failed["material_id"])),
        actor="human", entry="local-web",
    )
    graph.add_source(
        db,
        AddSourceInput(case_id=case_id, entity_id=e2_id, ref=SourceRefInput(annotation_id=fallback_ann_id)),
        actor="human", entry="local-web",
    )
    # Add relation source in G2 referencing nothing from m_graph
    ws = graph.add_relation(
        db,
        AddGraphRelationInput(
            case_id=case_id, from_entity_id=e1_id, to_entity_id=e2_id,
            kind="contract", credibility="suspected", graph_id=g2_id,
        ),
        actor="human", entry="local-web",
    )

    # Evidence bindings: confirmed on m_ev_ann, rejected on m_unused
    cat = evidence_catalog.add_group(db, AddEvidenceGroupInput(case_id=case_id, title="第一组"),
                                     actor="human", entry="local-web")
    grp_id = cat.groups[0].id
    cat = evidence_catalog.add_item(db, AddEvidenceItemInput(case_id=case_id, group_id=grp_id, title="回单"),
                                    actor="human", entry="local-web")
    item_id = cat.items[0].id
    evidence_catalog.add_binding(
        db, AddEvidenceBindingInput(case_id=case_id, item_id=item_id, material_id=m_ev_ann["material_id"]),
        actor="human", entry="local-web",
    )
    # Pending agent binding that gets rejected -> should NOT count as referenced for m_unused
    cat = evidence_catalog.add_binding(
        db,
        AddEvidenceBindingInput(
            case_id=case_id, item_id=item_id, material_id=m_unused["material_id"],
            request_key="rej-bind",
        ),
        actor="agent", entry="agent-mcp",
    )
    rej_binding_id = next(b.id for b in cat.bindings if b.material_id == m_unused["material_id"])
    evidence_catalog.reject_binding(
        db,
        EvidenceBindingRevisionInput(case_id=case_id, binding_id=rej_binding_id, expected_revision=0),
        actor="human", entry="local-web",
    )

    # ClaimItem rows: active on m_claim, soft-removed on m_unused
    cgrp = models.ClaimGroup(
        id=uuid4(), case_id=case_id, title="诉请组", currency="CNY",
        include_in_total=True, position=0, created_by="human", updated_by="human",
    )
    db.add(cgrp)
    db.flush()
    db.add(models.ClaimItem(
        id=uuid4(), case_id=case_id, group_id=cgrp.id, title="违约金",
        category="liquidated_damages", amount_cents=500000,
        segment_id=m_claim["segment_ids"][0], position=0,
        created_by="human", updated_by="human",
    ))
    db.add(models.ClaimItem(
        id=uuid4(), case_id=case_id, group_id=cgrp.id, title="已删金额",
        category="other", amount_cents=100,
        material_id=m_unused["material_id"], position=1,
        created_by="human", updated_by="human",
        removed_at=datetime.now(timezone.utc),
    ))
    db.commit()

    matrix = graph_assist.get_corpus_coverage_matrix(db, case_id)
    assert matrix.totals == {
        "referenced": 4,
        "parsed_unused": 1,
        "partial_or_failed": 1,
        "pending_or_unsupported": 2,
        "total": 8,
    }
    by_id = {m.material_id: m for m in matrix.materials}
    assert by_id[m_graph["material_id"]].status == "referenced"
    assert by_id[m_graph["material_id"]].reference_kinds == ["graph"]
    assert by_id[m_fallback["material_id"]].status == "referenced"
    assert by_id[m_fallback["material_id"]].file_version_id == m_fallback["file_version_id"]
    assert by_id[m_fallback["material_id"]].reference_kinds == ["graph", "annotation"]
    assert by_id[m_ev_ann["material_id"]].status == "referenced"
    assert by_id[m_ev_ann["material_id"]].reference_kinds == ["evidence", "annotation"]
    assert by_id[m_claim["material_id"]].status == "referenced"
    assert by_id[m_claim["material_id"]].reference_kinds == ["claim"]
    assert by_id[m_unused["material_id"]].status == "parsed_unused"
    assert by_id[m_unused["material_id"]].reference_kinds == []
    assert by_id[m_failed["material_id"]].status == "partial_or_failed"
    assert by_id[m_failed["material_id"]].reference_kinds == ["graph"]
    assert by_id[base["material_id"]].status == "pending_or_unsupported"
    assert by_id[m_unsup["material_id"]].status == "pending_or_unsupported"
    assert sum(c.count for c in matrix.cells) == matrix.totals["total"]

    # Graph scoping: G2 does not include E1's node (Wait: add_relation auto-added E1 to G2!)
    # Let's create a third graph G3 with only E2 so E1 is NOT in G3.
    ws = graph.create_graph(db, CreateGraphInput(case_id=case_id, title="独立子图"), actor="human", entry="local-web")
    g3_id = next(g.id for g in ws.graphs if g.title == "独立子图")
    graph.add_to_graph(db, AddToGraphInput(case_id=case_id, graph_id=g3_id, entity_ids=[e2_id]),
                       actor="human", entry="local-web")

    matrix_g3 = graph_assist.get_corpus_coverage_matrix(db, case_id, graph_id=g3_id)
    by_id_g3 = {m.material_id: m for m in matrix_g3.materials}
    assert by_id_g3[m_graph["material_id"]].status == "parsed_unused"
    assert by_id_g3[m_graph["material_id"]].reference_kinds == []
    # Evidence / annotation / claim remain case-wide references.
    assert by_id_g3[m_ev_ann["material_id"]].status == "referenced"
    assert by_id_g3[m_claim["material_id"]].status == "referenced"

    # Cross-case graph_id -> CASE_MISMATCH
    other_case = build_material(db, relative_source="虚构/外案.txt")
    with pytest.raises(WorkbenchError) as err:
        graph_assist.get_corpus_coverage_matrix(db, other_case["case_id"], graph_id=g1_id)
    assert err.value.code == ErrorCode.CASE_MISMATCH


def test_find_orphan_orgs_excludes_known_entities_and_never_matches_persons(db):
    base = build_material(db, relative_source="虚构/起诉状.txt")
    case_id = base["case_id"]

    # Existing entity + alias (with fullwidth parens to verify normalization match)
    ws = graph.add_entity(
        db, AddEntityInput(case_id=case_id, kind="org", name="上海甲岚（集团）有限公司"),
        actor="human", entry="local-web",
    )
    e_id = next(e.id for e in ws.entities if e.name == "上海甲岚（集团）有限公司")
    graph.add_alias(
        db, AddAliasInput(case_id=case_id, entity_id=e_id, alias="甲岚贸易有限责任公司"),
        actor="human", entry="local-web",
    )

    m1 = _add_material_with_run(
        db, case_id, display_name="起诉状.pdf", category="诉讼文书",
        segments=[
            (
                {"page": 1},
                "原告：张三，男，1985年出生。\n"
                "被告：李四，住北京市。\n"
                "第三人：王五。\n"
                "法定代表人：赵六。\n"
                "原告：上海甲岚(集团)有限公司与杭州星湖科技有限公司、苏州远山律师事务所纠纷案。",
            ),
            (
                {"page": 2},
                "2025年1月1日杭州星湖科技有限公司向杭州星湖科技有限公司关联方支付款项，"
                "由中国工商银行股份有限公司上海分行汇出，后经甲岚贸易有限责任公司确认。"
                "华为技术有限公司与自由贸易有限公司亦多次配合。",
            ),
        ],
    )
    m2 = _add_material_with_run(
        db, case_id, display_name="仲裁书.pdf", category="诉讼文书",
        segments=[
            (
                {"page": 1},
                "经北京仲裁委员会审理，上海市浦东新区人民法院执行。"
                "被申请人：杭州星湖科技有限公司，向中国工商银行股份有限公司上海分行查询流水，"
                "并委托苏州远山律师事务所出具意见，相关款项转给华为技术有限公司。"
                "另有立信会计师事务所、天健资产评估事务所、北京安达普通合伙、招商银行北京朝阳支行。",
            ),
            ({"page": 2}, "杭州星湖科技有限公司确认收函。"),
            ({"page": 3}, "杭州星湖科技有限公司提交证据。"),
            ({"page": 4}, "杭州星湖科技有限公司补正材料。"),
            ({"page": 5}, "杭州星湖科技有限公司签署纪要。"),
        ],
    )
    # Inactive run and working copy must be ignored
    _add_material_with_run(
        db, case_id, display_name="工作副本.pdf", category="诉讼文书", kind="working",
        segments=[({"page": 1}, "工作副本虚拟科技有限公司 工作副本虚拟科技有限公司")],
    )
    _add_material_with_run(
        db, case_id, display_name="未激活版本.pdf", category="诉讼文书", activate=False,
        segments=[({"page": 1}, "未激活虚拟科技有限公司 未激活虚拟科技有限公司")],
    )
    db.commit()

    orphans = graph_assist.find_orphan_orgs(db, case_id, min_mentions=2)
    names = [o.name for o in orphans]

    assert "杭州星湖科技有限公司" in names
    assert "中国工商银行股份有限公司上海分行" in names
    assert "苏州远山律师事务所" in names
    assert "华为技术有限公司" in names

    # Existing entity name and alias must be excluded
    assert "上海甲岚(集团)有限公司" not in names
    assert "甲岚贸易有限责任公司" not in names
    # Natural persons must NEVER be matched
    for person in ("张三", "李四", "王五", "赵六"):
        assert all(person not in n for n in names)
    # Working copies and inactive runs must NEVER be scanned
    assert "工作副本虚拟科技有限公司" not in names
    assert "未激活虚拟科技有限公司" not in names
    # Single-mention strict orgs excluded when min_mentions=2
    assert "北京仲裁委员会" not in names
    assert "上海市浦东新区人民法院" not in names
    assert "自由贸易有限公司" not in names

    star = next(o for o in orphans if o.name == "杭州星湖科技有限公司")
    assert star.mention_count == 8
    assert star.material_count == 2
    assert set(star.material_ids) == {m1["material_id"], m2["material_id"]}
    assert len(star.sample_segments) == 5

    # min_mentions=1 surfaces the court/arbitration/firms/支行/自由贸易有限公司
    all_orphans = graph_assist.find_orphan_orgs(db, case_id, min_mentions=1)
    all_names = {o.name for o in all_orphans}
    assert {
        "北京仲裁委员会",
        "上海市浦东新区人民法院",
        "自由贸易有限公司",
        "立信会计师事务所",
        "天健资产评估事务所",
        "北京安达普通合伙",
        "招商银行北京朝阳支行",
    } <= all_names

    with pytest.raises(WorkbenchError) as err:
        graph_assist.find_orphan_orgs(db, case_id, min_mentions=0)
    assert err.value.code == ErrorCode.INVALID_INPUT


def test_find_identifier_bridges_and_negative_filters(db):
    base = build_material(db, relative_source="虚构/主合同.txt")
    case_id = base["case_id"]

    ws = graph.add_entity(db, AddEntityInput(case_id=case_id, kind="org", name="星湖科技"),
                          actor="human", entry="local-web")
    e_star = next(e.id for e in ws.entities if e.name == "星湖科技")
    ws = graph.add_entity(db, AddEntityInput(case_id=case_id, kind="org", name="远山贸易"),
                          actor="human", entry="local-web")
    e_far = next(e.id for e in ws.entities if e.name == "远山贸易")

    valid_uscc = _make_uscc("91310000MA1FL0012")
    # Corrupt check digit for negative test
    wrong_check = "0" if valid_uscc[-1] != "0" else "1"
    invalid_uscc = valid_uscc[:-1] + wrong_check

    m1 = _add_material_with_run(
        db, case_id, display_name="合同A.pdf", category="合同",
        segments=[
            (
                {"page": 1},
                f"甲方星湖科技，统一社会信用代码：{valid_uscc.lower()}，"
                f"联系人手机13812345678，邮箱：Admin.Contact@Example.COM，"
                f"收款账号：6222021001001234567（开户行：中国银行）。",
            ),
            (
                {"page": 2},
                # Single-material noise & invalid values that MUST NOT match or bridge:
                f"第1页，日期2025-01-01，订单号：2025010112345678，"
                f"发票号码：123456789012345670，时间戳：1700000000123456，"
                f"错误代码：{invalid_uscc}，独享手机13900001111，独享邮箱solo@onlyone.cn。",
            ),
            (
                {"page": 3},
                # Same-material cross-entity mobile bridge (links 星湖科技 and 远山贸易 in 1 material)
                "星湖科技与远山贸易共用联系电话13766668888。",
            ),
        ],
    )
    m2 = _add_material_with_run(
        db, case_id, display_name="对账单B.pdf", category="流水",
        segments=[
            (
                {"page": 4},
                f"对账单位：远山贸易（信用代码{valid_uscc}），"
                f"联系电话13812345678，联系邮箱admin.contact@example.com，"
                f"开户银行账号 6222021001001234567。"
                # Same negative numbers repeated across materials — STILL must NOT match!
                f"订单号：2025010112345678，发票号码：123456789012345670，"
                f"时间戳：1700000000123456，第123456789012345页，错误代码：{invalid_uscc}。"
                # Bank keyword > 30 chars away -> must NOT match as bank_account!
                f"银行" + ("说明" * 20) + "9876543210987654",
            ),
        ],
    )
    # Also ensure ExactValue(kind='identifier') with noise like '9号' or '2025010112345678' never triggers bridges.
    db.add(models.ExactValue(
        case_id=case_id, segment_id=m1["segment_ids"][1], kind="identifier",
        raw_text="2025010112345678", canonical_value={"value": "2025010112345678"},
    ))
    db.add(models.ExactValue(
        case_id=case_id, segment_id=m2["segment_ids"][0], kind="identifier",
        raw_text="2025010112345678", canonical_value={"value": "2025010112345678"},
    ))
    db.commit()

    bridges = graph_assist.find_identifier_bridges(db, case_id)
    by_key = {(b.kind, b.value): b for b in bridges}

    assert ("cn_mobile", "13812345678") in by_key
    assert ("cn_mobile", "13766668888") in by_key  # 1 material, 2 entities!
    assert ("uscc", valid_uscc) in by_key
    assert ("email", "admin.contact@example.com") in by_key
    assert ("bank_account", "6222021001001234567") in by_key

    # Negative assertions: none of the noise/invalid/single-material values may appear
    for b in bridges:
        assert b.value not in {
            "2025010112345678",
            "123456789012345670",
            "1700000000123456",
            "123456789012345",
            "9876543210987654",
            invalid_uscc,
            "13900001111",
            "solo@onlyone.cn",
        }

    uscc_bridge = by_key[("uscc", valid_uscc)]
    assert uscc_bridge.material_count == 2
    assert set(uscc_bridge.entity_ids) == {e_star, e_far}

    cross_ent_mobile = by_key[("cn_mobile", "13766668888")]
    assert cross_ent_mobile.material_count == 1
    assert set(cross_ent_mobile.entity_ids) == {e_star, e_far}

    # Test audit_graph_gaps aggregator + cross-case check
    audit = graph_assist.audit_graph_gaps(db, case_id, min_mentions=2)
    assert audit.case_id == case_id
    assert len(audit.identifier_bridges) == 5

    other = build_material(db, relative_source="虚构/他案.txt")
    ws_other = graph.create_graph(
        db, CreateGraphInput(case_id=other["case_id"], title="他案图"),
        actor="human", entry="local-web",
    )
    with pytest.raises(WorkbenchError) as err:
        graph_assist.audit_graph_gaps(db, case_id, graph_id=ws_other.graphs[0].id)
    assert err.value.code == ErrorCode.CASE_MISMATCH


def test_extract_entity_alias_candidates_roles_aliases_clustering_and_linking(db):
    base = build_material(db, relative_source="虚构/起诉.txt")
    case_id = base["case_id"]

    # Pre-existing entity with one registered alias
    ws = graph.add_entity(
        db, AddEntityInput(case_id=case_id, kind="org", name="上海明远科技有限公司"),
        actor="human", entry="local-web",
    )
    e_mingyuan = next(e.id for e in ws.entities if e.name == "上海明远科技有限公司")
    graph.add_alias(
        db, AddAliasInput(case_id=case_id, entity_id=e_mingyuan, alias="明远"),
        actor="human", entry="local-web",
    )

    m_suit = _add_material_with_run(
        db, case_id, display_name="民事起诉状.pdf", category="诉讼文书",
        segments=[
            (
                {"page": 1},
                "原告：上海明远科技有限公司（以下简称“明远”），住所地上海市。\n"
                "法定代表人：张伟，总经理。\n"
                "委托诉讼代理人：李娜，北京华泰律师事务所律师。\n"
                "被告：杭州星湖智能科技有限公司，住所地杭州市。\n"
                "第三人：陈涛，男。",
            ),
        ],
    )
    m_contract = _add_material_with_run(
        db, case_id, display_name="采购及担保合同.pdf", category="合同",
        segments=[
            (
                {"page": 1},
                "甲方：上海明远科技有限公司（以下简称“明远公司”）\n"
                "乙方：杭州星湖智能科技有限公司（以下简称“星湖智能”）\n"
                "保证人：深圳创新投资合伙企业（有限合伙）（以下简称“创新投资”）",
            ),
            (
                {"page": 2},
                "2025年3月，明远科技向星湖智能公司交付货物，明远科技公司确认收款，"
                "明远有限公司亦盖章。北京市朝阳区人民法院已立案。"
                "出借人：北京海天小额贷款有限公司（以下简称“海天小贷”），借款人：周杰（以下简称“周某”）。",
            ),
        ],
    )
    # Ambiguous stem test material (not included when filtering to [m_suit, m_contract])
    m_ambig = _add_material_with_run(
        db, case_id, display_name="关联协议.pdf", category="合同",
        segments=[
            (
                {"page": 1},
                "北京星河科技有限公司与上海星河科技有限公司共同签署协议，后星河科技发函确认。",
            ),
        ],
    )
    db.commit()

    # Run scoped to [m_suit, m_contract]
    scoped = graph_assist.extract_entity_alias_candidates(
        db, case_id, material_ids=[m_suit["material_id"], m_contract["material_id"]],
    )
    by_name = {c.name: c for c in scoped.candidates}

    # Existing entity linking: filters registered "明远", keeps new "明远公司", "明远科技", "明远科技公司"
    my = by_name["上海明远科技有限公司"]
    assert my.entity_kind == "org"
    assert my.existing_entity_id == e_mingyuan
    assert my.existing_entity_name == "上海明远科技有限公司"
    assert "plaintiff" in my.suggested_roles
    assert "明远" not in my.candidate_aliases
    assert "明远公司" in my.candidate_aliases
    assert "明远科技" in my.candidate_aliases
    assert "明远科技公司" in my.candidate_aliases
    assert "明远有限公司" in my.candidate_aliases
    assert my.mention_count >= 5

    # Court kind & contract lender/borrower roles
    assert by_name["北京市朝阳区人民法院"].entity_kind == "court"
    assert by_name["北京海天小额贷款有限公司"].suggested_roles == ["creditor"]
    assert "海天小贷" in by_name["北京海天小额贷款有限公司"].candidate_aliases
    assert by_name["周杰"].entity_kind == "person"
    assert by_name["周杰"].suggested_roles == ["debtor"]
    assert "周某" in by_name["周杰"].candidate_aliases

    # Lawsuit header roles + person entity_kind + contract 以下简称 + org clustering ("星湖智能公司")
    xh = by_name["杭州星湖智能科技有限公司"]
    assert xh.entity_kind == "org"
    assert xh.existing_entity_id is None
    assert "defendant" in xh.suggested_roles
    assert "星湖智能" in xh.candidate_aliases
    assert "星湖智能公司" in xh.candidate_aliases

    # Guarantor contract role + 有限合伙 suffix
    cx = by_name["深圳创新投资合伙企业(有限合伙)"]
    assert cx.entity_kind == "org"
    assert "guarantor" in cx.suggested_roles
    assert "创新投资" in cx.candidate_aliases

    # Natural persons from header lines ONLY
    assert by_name["张伟"].entity_kind == "person"
    assert by_name["张伟"].suggested_roles == ["legal_rep"]
    assert by_name["李娜"].entity_kind == "person"
    assert by_name["李娜"].suggested_roles == ["legal_rep"]
    assert by_name["陈涛"].entity_kind == "person"
    assert by_name["陈涛"].suggested_roles == ["third_party"]

    # Ambiguous material NOT scanned yet
    assert "北京星河科技有限公司" not in by_name

    # Unscoped scan includes m_ambig: "星河科技" is ambiguous between 北京星河 and 上海星河 -> NOT attached to either!
    full = graph_assist.extract_entity_alias_candidates(db, case_id)
    full_by_name = {c.name: c for c in full.candidates}
    assert "星河科技" not in full_by_name["北京星河科技有限公司"].candidate_aliases
    assert "星河科技" not in full_by_name["上海星河科技有限公司"].candidate_aliases

    # Cross-case material_id -> CASE_MISMATCH
    foreign = build_material(db, relative_source="虚构/外案材料.txt")
    with pytest.raises(WorkbenchError) as err:
        graph_assist.extract_entity_alias_candidates(db, case_id, material_ids=[foreign["material_id"]])
    assert err.value.code == ErrorCode.CASE_MISMATCH


def test_ac12_excludes_citizen_id_and_preserves_long_branch_orgs(db):
    # Pick an 18-digit citizen ID that passes BOTH ISO 7064 Mod 11-2 AND GB 32100 Mod-31
    from legal_workbench.graph_assist import _valid_uscc

    def _overlap_cn_id() -> str:
        weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
        chk = "10X98765432"
        for seq in range(100, 1000):
            body = f"11010519900101{seq:03d}"
            c = chk[sum(int(d) * w for d, w in zip(body, weights)) % 11]
            cand = body + c
            if c.isdigit() and _valid_uscc(cand):
                return cand
        raise RuntimeError("no overlapping test id found")

    cn_id = _overlap_cn_id()
    case = build_material(db, relative_source="虚构/主案.txt")
    case_id = case["case_id"]
    _add_material_with_run(
        db, case_id, display_name="身份核验1.pdf", category="主体",
        segments=[({"page": 1}, f"自然人张三号码 {cn_id}，以及中国农业银行股份有限公司上海市浦东新区支行。")],
    )
    _add_material_with_run(
        db, case_id, display_name="身份核验2.pdf", category="主体",
        segments=[({"page": 2}, f"经办人信息 {cn_id}，由甲科技有限公司上海分公司签署回执。")],
    )
    db.commit()

    audit = graph_assist.audit_graph_gaps(db, case_id, min_mentions=1)
    assert all(b.value != cn_id for b in audit.identifier_bridges)
    orphan_names = {o.name for o in audit.orphan_orgs}
    assert "中国农业银行股份有限公司上海市浦东新区支行" in orphan_names
    assert "甲科技有限公司上海分公司" in orphan_names
