"""Graph assist reads active TEST-ONLY fragments and real exact values."""
from uuid import uuid4

import pytest

from legal_workbench import graph, graph_assist, models
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.schemas import AddAliasInput, AddEntityInput
from .graph_factories import build_graph_case


def _entity(db, case_id, name, alias):
    workspace = graph.add_entity(db, AddEntityInput(
        case_id=case_id, kind="org", name=name), actor="human", entry="local-web")
    entry = next(item for item in workspace.entities if item.name == name)
    for value in (alias, "甲方", "甲"):
        graph.add_alias(db, AddAliasInput(case_id=case_id, entity_id=entry.id, alias=value),
                        actor="human", entry="local-web")
    return entry.id


def _add_fragment(db, case_id, run_id, locator, text):
    unit = models.ParseUnit(id=uuid4(), case_id=case_id, parse_run_id=run_id,
                            unit_key=f"TEST-ONLY:{uuid4()}", status="extracted", method="native")
    db.add(unit)
    db.flush()
    fragment = models.Segment(id=uuid4(), case_id=case_id, parse_run_id=run_id,
                              parse_unit_id=unit.id, locator=locator, text=text, normalized_text=text)
    db.add(fragment)
    db.flush()
    return fragment


def test_active_segments_aliases_and_exact_values(db):
    case = build_graph_case(db)
    case_id = case["case_id"]
    first = _entity(db, case_id, "仿真开源有限公司", "仿真开源")
    second = _entity(db, case_id, "仿真石湖有限公司", "仿真石湖")
    inactive = db.get(models.Segment, case["segment_id"])
    inactive.text = inactive.normalized_text = "TEST-ONLY 仿真开源 仿真石湖 过期片段"
    version = db.get(models.FileVersion, case["file_version_id"])
    run = models.ParseRun(id=uuid4(), case_id=case_id, file_version_id=version.id,
                          parser_version="TEST-ONLY", sequence=1, status="complete")
    db.add(run)
    db.flush()
    version.active_parse_run_id = run.id
    exact = _add_fragment(db, case_id, run.id, {"page": 3},
                          "TEST-ONLY 仿真开源 仿真石湖 2026年1月2日 100元 9号")
    page_a = _add_fragment(db, case_id, run.id, {"page": 4}, "仿真开源")
    page_b = _add_fragment(db, case_id, run.id, {"page": 4}, "仿真石湖")
    _add_fragment(db, case_id, run.id, {"sheet": "TEST-ONLY 清单"}, "仿真开源")
    _add_fragment(db, case_id, run.id, {"sheet": "TEST-ONLY 清单"}, "仿真石湖")
    _add_fragment(db, case_id, run.id, {}, "甲方")
    db.add_all([
        models.ExactValue(case_id=case_id, segment_id=exact.id, kind="date",
                          raw_text="2026年1月2日", canonical_value={"iso": "2026-01-02"}),
        models.ExactValue(case_id=case_id, segment_id=exact.id, kind="amount",
                          raw_text="100元", amount_value=100, currency="CNY"),
        models.ExactValue(case_id=case_id, segment_id=exact.id, kind="identifier", raw_text="9号"),
    ])
    db.commit()

    result = graph_assist.find_entity_cooccurrences(db, case_id, first, second)
    assert "仿真开源" in result.entity_a_terms
    assert "仿真石湖" in result.entity_b_terms
    assert "甲" not in result.entity_a_terms and "甲方" not in result.entity_a_terms
    assert result.hits[0].kind == "segment"
    assert {hit.kind for hit in result.hits[1:]} == {"page", "sheet"}
    assert result.hits[0].fragments[0].location.source_ref == exact.id
    assert {source.location.source_ref for source in next(hit for hit in result.hits if hit.kind == "page").fragments} == {page_a.id, page_b.id}
    assert {value.raw_text for value in result.hits[0].fragments[0].exact_values} == {"100元", "2026年1月2日"}
    assert len(graph_assist.find_entity_cooccurrences(db, case_id, first, second, limit=1).hits) == 1
    hints = graph_assist.match_entities_in_segment(db, case_id, exact.id)
    assert {entry.entity_id for entry in hints.entities} == {first, second}
    with pytest.raises(WorkbenchError) as error:
        graph_assist.match_entities_in_segment(db, case_id, inactive.id)
    assert error.value.code == ErrorCode.NOT_FOUND


def test_nested_alias_does_not_count_as_a_second_party():
    first, second = uuid4(), uuid4()
    terms = {first: ["星湖"], second: ["星湖设备有限公司"]}
    only_second = graph_assist._entity_matches("TEST-ONLY 星湖设备有限公司签署", terms)
    assert only_second[first] == []
    assert only_second[second] == ["星湖设备有限公司"]
    both = graph_assist._entity_matches("TEST-ONLY 星湖与星湖设备有限公司签署", terms)
    assert both[first] == ["星湖"]
    assert both[second] == ["星湖设备有限公司"]
    partial = {first: ["星湖"], second: ["湖畔发展有限公司"]}
    overlapping = graph_assist._entity_matches("TEST-ONLY 星湖畔发展有限公司签署", partial)
    assert overlapping[first] == []
    assert overlapping[second] == ["湖畔发展有限公司"]
    separate = graph_assist._entity_matches("TEST-ONLY 星湖与湖畔发展有限公司签署", partial)
    assert separate[first] == ["星湖"]
    assert separate[second] == ["湖畔发展有限公司"]


def test_cross_case_identifiers_are_rejected(db):
    first = build_graph_case(db)
    second = build_graph_case(db)
    own = _entity(db, first["case_id"], "仿真甲岚有限公司", "甲岚")
    foreign = _entity(db, second["case_id"], "仿真乙屿有限公司", "乙屿")
    with pytest.raises(WorkbenchError) as entity_error:
        graph_assist.find_entity_cooccurrences(db, first["case_id"], own, foreign)
    assert entity_error.value.code == ErrorCode.CASE_MISMATCH
    with pytest.raises(WorkbenchError) as segment_error:
        graph_assist.match_entities_in_segment(db, first["case_id"], second["segment_id"])
    assert segment_error.value.code == ErrorCode.CASE_MISMATCH
