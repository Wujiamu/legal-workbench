"""诉请金额 P3 入口：HTTP（actor=human）、MCP 与 CLI（actor=agent）共用同一服务。

覆盖：快照与候选读取、分组/金额项增改删排、人工复核、路径与请求体标识一致、
错误 envelope、Origin 守卫、跨案拒绝、独立序号，以及 Agent 入口不暴露确认/撤回。
真实 PostgreSQL（conftest 重建 legal_workbench_test），全部 TEST-ONLY 合成数据。
"""
from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest

from legal_workbench import cases, claims, progress
from legal_workbench.db import get_session_factory
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.schemas import AddClaimGroupInput, AddClaimItemInput, ChangeIdInput

from .factories import create_case
from .test_api import new_case

# 这些用例验证 MCP 工具的业务行为；访问围栏本身由 test_agent_access_gate.py 覆盖，
# 因此这里显式放开（allow_all），不依赖任何宽松默认值。
pytestmark = pytest.mark.usefixtures("agent_access_open")


def cents(amount: str) -> int:
    return int(Decimal(amount.replace(",", "")) * 100)


def _group_body(case_id: str, title="对被告一主诉请", **extra) -> dict:
    body = {"case_id": case_id, "title": title}
    body.update(extra)
    return body


def _item_body(case_id: str, group_id: str, amount_cents: int, *, title="金额项",
               category="principal", **extra) -> dict:
    body = {"case_id": case_id, "group_id": group_id, "title": title,
            "category": category, "amount_cents": amount_cents}
    body.update(extra)
    return body


def _claims(client, case_id):
    return client.get(f"/api/v1/cases/{case_id}/claims")


def _codes(fn):
    with pytest.raises(WorkbenchError) as exc:
        fn()
    return exc.value.code


# --- HTTP reads -------------------------------------------------------------

def test_http_snapshot_and_candidates_shape(client):
    case_id = new_case(client, "TEST-ONLY 诉请快照")
    response = _claims(client, case_id)
    assert response.status_code == 200
    body = response.json()
    # 空案：结构完整、序号为 0、无 pending 提示。
    assert body["groups"] == [] and body["items"] == []
    assert body["group_subtotals"] == {} and body["totals_by_currency"] == []
    assert body["pending_changes"] == [] and body["pending_count"] == 0
    assert body["seq"] == 0
    candidates = client.get(f"/api/v1/cases/{case_id}/claims/amount-candidates")
    assert candidates.status_code == 200
    assert candidates.json() == {"case_id": case_id, "candidates": []}
    filtered = client.get(f"/api/v1/cases/{case_id}/claims/amount-candidates",
                          params={"query": "合同"})
    assert filtered.status_code == 200
    # 未知案件是结构化 404，不是 500。
    missing = _claims(client, str(uuid.uuid4()))
    assert missing.status_code == 404 and missing.json()["code"] == "not_found"


# --- HTTP writes (human) ----------------------------------------------------

def test_http_group_and_item_writes_are_human_and_exact(client):
    case_id = new_case(client, "TEST-ONLY 诉请写")
    created = client.post(f"/api/v1/cases/{case_id}/claims/groups",
                          json=_group_body(case_id, note="一审主诉请"))
    assert created.status_code == 200, created.text
    body = created.json()
    group = body["groups"][0]
    assert group["created_by"] == "human" and group["currency"] == "CNY"
    assert body["pending_changes"] == []
    assert body["changes"][-1]["entry"] == "local-web"
    assert body["last_change_id"] == body["changes"][-1]["id"]

    for amount, category, title in [
            (cents("1,280,000.00"), "principal", "合同价款（本金）"),
            (cents("86,400.00"), "interest", "逾期利息"),
            (cents("300,000.00"), "deduction", "已付款抵扣")]:
        response = client.post(f"/api/v1/cases/{case_id}/claims/items",
                               json=_item_body(case_id, group["id"], amount, category=category,
                                               title=title))
        assert response.status_code == 200, response.text
    body = response.json()
    # 整数分：金额是 int，抵扣按减法进入小计。
    assert all(isinstance(item["amount_cents"], int) for item in body["items"])
    assert body["group_subtotals"][group["id"]] == cents("1,066,400.00")
    assert [entry["total_cents"] for entry in body["totals_by_currency"]] == [cents("1,066,400.00")]
    assert body["included_group_count"] == 1 and body["excluded_group_count"] == 0

    # 第二个分组按币种分开汇总，不混算。
    usd = client.post(f"/api/v1/cases/{case_id}/claims/groups",
                      json=_group_body(case_id, "美元费用", currency="usd")).json()
    usd_group = next(item for item in usd["groups"] if item["title"] == "美元费用")
    assert usd_group["currency"] == "USD"
    usd = client.post(f"/api/v1/cases/{case_id}/claims/items",
                      json=_item_body(case_id, usd_group["id"], 500, category="expense",
                                      title="翻译费")).json()
    assert [entry["currency"] for entry in usd["totals_by_currency"]] == ["CNY", "USD"]


def test_http_update_conflict_is_409_envelope(client):
    case_id = new_case(client, "TEST-ONLY 诉请冲突")
    group = client.post(f"/api/v1/cases/{case_id}/claims/groups",
                        json=_group_body(case_id)).json()["groups"][0]
    item = client.post(f"/api/v1/cases/{case_id}/claims/items",
                       json=_item_body(case_id, group["id"], 1000)).json()["items"][0]
    url = f"/api/v1/cases/{case_id}/claims/items/{item['id']}"
    payload = {"case_id": case_id, "item_id": item["id"],
               "expected_revision": item["revision"], "changes": {"amount_cents": 2000}}
    assert client.patch(url, json=payload).status_code == 200
    stale = client.patch(url, json=payload)
    assert stale.status_code == 409
    body = stale.json()
    assert body["code"] == "conflict"
    # 统一错误 envelope：code/message/details/request_id。
    assert set(body) == {"code", "message", "details", "request_id"}
    assert body["details"]["actual_revision"] == 1
    assert _claims(client, case_id).json()["items"][0]["amount_cents"] == 2000


def test_http_reorder_remove_restore_round_trip(client):
    case_id = new_case(client, "TEST-ONLY 诉请排序")
    group = client.post(f"/api/v1/cases/{case_id}/claims/groups",
                        json=_group_body(case_id)).json()["groups"][0]
    for title, amount in (("甲", 100), ("乙", 200)):
        client.post(f"/api/v1/cases/{case_id}/claims/items",
                    json=_item_body(case_id, group["id"], amount, title=title))
    body = _claims(client, case_id).json()
    yi = next(item for item in body["items"] if item["title"] == "乙")
    moved = client.post(f"/api/v1/cases/{case_id}/claims/items/{yi['id']}/move",
                        json={"case_id": case_id, "item_id": yi["id"],
                              "expected_revision": yi["revision"], "direction": "up"})
    assert moved.status_code == 200, moved.text
    assert [item["title"] for item in moved.json()["items"] if item["removed_at"] is None] == ["乙", "甲"]

    yi = next(item for item in moved.json()["items"] if item["title"] == "乙")
    removed = client.post(f"/api/v1/cases/{case_id}/claims/items/{yi['id']}/remove",
                          json={"case_id": case_id, "item_id": yi["id"],
                                "expected_revision": yi["revision"]})
    assert removed.status_code == 200
    assert removed.json()["group_subtotals"][group["id"]] == 100
    yi = next(item for item in removed.json()["items"] if item["title"] == "乙")
    restored = client.post(f"/api/v1/cases/{case_id}/claims/items/{yi['id']}/restore",
                           json={"case_id": case_id, "item_id": yi["id"],
                                 "expected_revision": yi["revision"]})
    assert restored.status_code == 200
    assert restored.json()["group_subtotals"][group["id"]] == 300

    # 分组排序与删除（级联软删金额项）。
    second = client.post(f"/api/v1/cases/{case_id}/claims/groups",
                         json=_group_body(case_id, "二审变更诉请")).json()["groups"][1]
    up = client.post(f"/api/v1/cases/{case_id}/claims/groups/{second['id']}/move",
                     json={"case_id": case_id, "group_id": second["id"],
                           "expected_revision": second["revision"], "direction": "up"})
    assert [entry["title"] for entry in up.json()["groups"]] == ["二审变更诉请", "对被告一主诉请"]
    first = next(entry for entry in up.json()["groups"] if entry["title"] == "对被告一主诉请")
    gone = client.post(f"/api/v1/cases/{case_id}/claims/groups/{first['id']}/remove",
                       json={"case_id": case_id, "group_id": first["id"],
                             "expected_revision": first["revision"]})
    assert gone.status_code == 200
    assert all(item["removed_at"] is not None for item in gone.json()["items"])
    # 被删分组不再计入任何币种总计。
    assert first["id"] not in [group_id for entry in gone.json()["totals_by_currency"]
                              for group_id in entry["included_group_ids"]]


# --- path/body agreement, cross-case, Origin --------------------------------

def test_http_path_and_body_identifiers_must_agree(client):
    case_a = new_case(client, "TEST-ONLY 路径 A")
    case_b = new_case(client, "TEST-ONLY 路径 B")
    other_group = client.post(f"/api/v1/cases/{case_b}/claims/groups",
                              json=_group_body(case_b)).json()["groups"][0]

    mismatch_case = client.post(f"/api/v1/cases/{case_a}/claims/groups",
                                json=_group_body(case_b, "错案分组"))
    assert mismatch_case.status_code == 404
    assert mismatch_case.json()["code"] == "case_mismatch"

    mismatch_target = client.patch(
        f"/api/v1/cases/{case_a}/claims/groups/{uuid.uuid4()}",
        json={"case_id": case_a, "group_id": other_group["id"],
              "expected_revision": other_group["revision"], "changes": {"title": "越权"}})
    assert mismatch_target.status_code == 404
    assert mismatch_target.json()["code"] == "case_mismatch"
    # 未被上面的请求写入。
    assert [group["title"] for group in _claims(client, case_b).json()["groups"]] == ["对被告一主诉请"]

    # 路径与请求体一致但目标属于他案：服务层归属守卫仍然拒绝。
    resp = client.patch(f"/api/v1/cases/{case_a}/claims/groups/{other_group['id']}",
                        json={"case_id": case_a, "group_id": other_group["id"],
                              "expected_revision": other_group["revision"],
                              "changes": {"title": "越权"}})
    assert resp.status_code == 404
    assert resp.json()["code"] == "case_mismatch"

    # 非法标识符 → 422 invalid_input，而不是 500。
    bad = _claims(client, "not-a-uuid")
    assert bad.status_code == 422 and bad.json()["code"] == "invalid_input"


def test_http_write_requires_workbench_origin(api):
    api.headers["Origin"] = "http://127.0.0.1:8765"
    case_id = new_case(api, "TEST-ONLY Origin")
    # 同一客户端去掉 Origin 后写请求被拒（读不受影响）。
    api.headers.pop("Origin", None)
    blocked = api.post(f"/api/v1/cases/{case_id}/claims/groups", json=_group_body(case_id))
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "forbidden"
    assert _claims(api, case_id).json()["groups"] == []


# --- human review over HTTP, agent writes never bypass it -------------------

def test_http_review_flow_for_agent_candidate(client):
    case_id = new_case(client, "TEST-ONLY 诉请复核")
    group = client.post(f"/api/v1/cases/{case_id}/claims/groups",
                        json=_group_body(case_id)).json()["groups"][0]
    # Agent 侧（MCP 路径）提交候选：暂存，不进明细与小计。
    from legal_workbench import mcp_server
    pending_snapshot = mcp_server.claim_add_item(
        case_id, group["id"], "逾期利息（补充）", "interest", cents("3,287.67"), "agent-1")
    assert pending_snapshot["items"] == []
    assert pending_snapshot["group_subtotals"][group["id"]] == 0
    assert pending_snapshot["pending_count"] == 1
    change = pending_snapshot["pending_changes"][0]
    assert change["actor"] == "agent" and change["entry"] == "mcp"
    assert change["review_state"] == "pending"
    assert change["diff"][0]["after"]["amount_cents"] == cents("3,287.67")

    # 有待确认候选时，人工修改同一金额项被拒绝（服务层守卫未被入口绕过）。
    accepted = client.post(f"/api/v1/cases/{case_id}/claims/changes/{change['id']}/accept",
                           json={"case_id": case_id, "change_id": change["id"]})
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["pending_changes"] == []
    assert body["items"][0]["amount_cents"] == cents("3,287.67")
    assert body["items"][0]["created_by"] == "agent"
    assert body["items"][0]["updated_by"] == "human"
    assert body["group_subtotals"][group["id"]] == cents("3,287.67")

    # 再提交一条候选后撤回：明细不变，候选标为 reverted。
    pending_snapshot = mcp_server.claim_add_item(
        case_id, group["id"], "不应入账", "expense", 100, "agent-2")
    change = pending_snapshot["pending_changes"][0]
    reverted = client.post(f"/api/v1/cases/{case_id}/claims/changes/{change['id']}/revert",
                           json={"case_id": case_id, "change_id": change["id"]})
    assert reverted.status_code == 200
    body = reverted.json()
    assert [item["title"] for item in body["items"]] == ["逾期利息（补充）"]
    assert body["pending_count"] == 0
    assert next(entry for entry in body["changes"]
                if entry["id"] == change["id"])["review_state"] == "reverted"


def test_http_accept_all_scope_and_sequence(client):
    case_id = new_case(client, "TEST-ONLY 批量复核")
    group = client.post(f"/api/v1/cases/{case_id}/claims/groups",
                        json=_group_body(case_id)).json()["groups"][0]
    from legal_workbench import mcp_server
    for index in range(3):
        mcp_server.claim_add_item(case_id, group["id"], f"候选{index}", "other",
                                  100 * (index + 1), f"bulk-{index}")
    shown = [change["id"] for change in _claims(client, case_id).json()["pending_changes"]]
    assert len(shown) == 3

    empty = client.post(f"/api/v1/cases/{case_id}/claims/changes/accept-all",
                        json={"case_id": case_id, "change_ids": []})
    assert empty.status_code == 422 and empty.json()["code"] == "invalid_input"
    no_scope = client.post(f"/api/v1/cases/{case_id}/claims/changes/accept-all",
                           json={"case_id": case_id})
    assert no_scope.status_code == 422 and no_scope.json()["code"] == "invalid_input"
    stale = client.post(f"/api/v1/cases/{case_id}/claims/changes/accept-all",
                        json={"case_id": case_id, "change_ids": shown, "expected_seq": 0})
    assert stale.status_code == 409 and stale.json()["code"] == "conflict"

    # 提交子集整体冲突：确认范围必须等于当前展示的全部待确认项（不静默漏项）。
    subset = client.post(f"/api/v1/cases/{case_id}/claims/changes/accept-all",
                         json={"case_id": case_id, "change_ids": shown[:2]})
    assert subset.status_code == 409, subset.text
    assert subset.json()["details"]["omitted_change_ids"] == [shown[2]]
    body = client.post(f"/api/v1/cases/{case_id}/claims/changes/accept-all",
                       json={"case_id": case_id, "change_ids": shown}).json()
    assert [item["title"] for item in body["items"]] == ["候选0", "候选1", "候选2"]
    assert body["pending_changes"] == []
    assert body["group_subtotals"][group["id"]] == 600


def test_http_archived_case_stays_read_only(client):
    case_id = new_case(client, "TEST-ONLY 归档只读")
    group = client.post(f"/api/v1/cases/{case_id}/claims/groups",
                        json=_group_body(case_id)).json()["groups"][0]
    revision = client.get(f"/api/v1/cases/{case_id}").json()["revision"]
    assert client.post(f"/api/v1/cases/{case_id}/archive",
                       json={"case_id": case_id, "expected_revision": revision}).status_code == 200
    blocked = client.post(f"/api/v1/cases/{case_id}/claims/items",
                          json=_item_body(case_id, group["id"], 100))
    assert blocked.status_code == 422
    assert "归档" in blocked.json()["message"]
    # 读仍然可用。
    assert _claims(client, case_id).status_code == 200


# --- sequence ---------------------------------------------------------------

def test_claims_sequence_is_separate_from_progress_over_http(client):
    case_id = new_case(client, "TEST-ONLY 序号")
    session = get_session_factory()()
    try:
        before = progress.get_progress(session, uuid.UUID(case_id)).seq
    finally:
        session.close()
    client.post(f"/api/v1/cases/{case_id}/claims/groups", json=_group_body(case_id))
    body = _claims(client, case_id).json()
    assert body["seq"] == 1
    seqs = client.get(f"/api/v1/cases/{case_id}/changes/seq").json()["seqs"]
    assert seqs["claims"] == 1 and seqs["progress"] == before


# --- MCP (agent) ------------------------------------------------------------

def test_mcp_exposes_reads_and_allowed_writes_but_no_review():
    import asyncio

    from legal_workbench import mcp_server
    tools = asyncio.run(mcp_server.server.list_tools())
    names = {tool.name for tool in tools}
    assert {"get_claims", "claim_amount_candidates", "claim_add_group",
            "claim_update_group", "claim_add_item", "claim_update_item"} <= names
    assert not any("accept" in name or "revert" in name for name in names)
    # 未提供删除/排序等人工命令（白名单之外）。
    assert not any(name.startswith("claim_remove") or name.startswith("claim_reorder")
                   for name in names)


def test_mcp_agent_writes_are_pending_and_honour_partial_updates(db):
    from legal_workbench import mcp_server
    case_id = str(cases.create_case(db, title="TEST-ONLY MCP 诉请").case_id)
    db.commit()  # MCP 工具用独立会话：案件必须先提交，否则它的写入会等本会话的行锁
    group = mcp_server.claim_add_group(case_id, "一审", "mcp-g1", note="原始备注")
    assert group["groups"] == []  # 候选未确认前不写入明细
    pending = group["pending_changes"][0]
    assert pending["actor"] == "agent" and pending["entry"] == "mcp"

    # 人工确认（服务层）后分组可见，Agent 再提交部分字段更新。
    snapshot = claims.accept_change(
        db, ChangeIdInput(case_id=uuid.UUID(case_id), change_id=uuid.UUID(pending["id"])),
        actor="human", entry="local-web")
    # 写命令返回快照时会在本会话留下 case_change_seq 的共享锁；MCP 工具用独立会话，
    # 必须先结束本事务，否则它的 FOR UPDATE 会等待（每个真实请求各自关闭会话，不受影响）。
    db.rollback()
    live = snapshot.groups[0]
    assert live.note == "原始备注"

    updated = mcp_server.claim_update_group(case_id, str(live.id), live.revision,
                                            "mcp-g2", title="一审（改）")
    proposal = updated["pending_changes"][0]
    assert proposal["diff"][0]["after"]["title"] == "一审（改）"
    # 未提供的 note 不被清空，业务行也尚未改变。
    assert proposal["diff"][0]["after"]["note"] == "原始备注"
    assert mcp_server.get_claims(case_id)["groups"][0]["title"] == "一审"

    # 同一分组已有待确认候选时，人工与 Agent 的再次修改都被服务层挡住。
    with pytest.raises(WorkbenchError) as blocked:
        mcp_server.claim_update_group(case_id, str(live.id), live.revision, "mcp-g3",
                                      clear_note=True)
    assert blocked.value.code == ErrorCode.CONFLICT
    # 人工撤回后可以继续提交；clear_note 显式清空。
    claims.revert_change(db, ChangeIdInput(case_id=uuid.UUID(case_id),
                                           change_id=uuid.UUID(proposal["id"])),
                         actor="human", entry="local-web")
    db.rollback()
    cleared = mcp_server.claim_update_group(case_id, str(live.id), live.revision, "mcp-g4",
                                            clear_note=True)
    assert cleared["pending_changes"][0]["diff"][0]["after"]["note"] is None
    with pytest.raises(WorkbenchError) as exc:
        mcp_server.claim_add_item(case_id, str(live.id), "坏类别", "bogus", 100, "mcp-bad")
    assert exc.value.code == ErrorCode.INVALID_INPUT


def test_mcp_source_round_trip_and_clear(db):
    """Agent 金额项出处：提供任一 id 即整体替换，clear_source 清空。"""
    from legal_workbench import mcp_server
    from .test_claims import _text_source

    case_id = create_case(db, "TEST-ONLY MCP 出处")
    source = _text_source(db, case_id=case_id)
    db.commit()
    candidates = mcp_server.claim_amount_candidates(str(case_id))
    assert len(candidates["candidates"]) == 1
    assert candidates["candidates"][0]["segment_id"] == str(source["segment_id"])

    group = claims.add_group(db, AddClaimGroupInput(case_id=case_id, title="本金组"),
                             actor="human", entry="local-web")
    group_id = group.groups[0].id
    item = claims.add_item(db, AddClaimItemInput(
        case_id=case_id, group_id=group_id, title="合同价款",
        category="principal", amount_cents=cents("1,280,000.00"),
        segment_id=source["segment_id"]), actor="human", entry="local-web")
    live = item.items[0]
    assert live.segment_id == source["segment_id"]
    db.commit()  # 提交后再交给 MCP 独立会话读同一行

    proposal = mcp_server.claim_update_item(str(case_id), str(live.id), live.revision,
                                            "mcp-i1", clear_source=True)
    assert proposal["pending_changes"][0]["diff"][0]["after"]["segment_id"] is None
    # 服务层仍持有原出处，直到人工确认。
    assert mcp_server.get_claims(str(case_id))["items"][0]["segment_id"] == str(source["segment_id"])


# --- CLI (agent) ------------------------------------------------------------

def test_cli_claims_commands_are_agent_and_review_free(db):
    from legal_workbench import cli
    case_id = str(cases.create_case(db, title="TEST-ONLY CLI 诉请").case_id)
    db.commit()  # CLI 走独立会话
    parser = cli.build_parser()

    snapshot = cli.dispatch(parser.parse_args(["claims", "--case-id", case_id]))
    assert snapshot.groups == []
    assert cli.dispatch(parser.parse_args(
        ["claim-candidates", "--case-id", case_id])).candidates == []

    created = cli.dispatch(parser.parse_args([
        "claim-add-group", "--case-id", case_id, "--title", "对被告一主诉请",
        "--request-key", "cli-g1", "--note", "一审"]))
    assert created.groups == []
    change = created.pending_changes[0]
    assert change.actor == "agent" and change.entry == "cli"

    # 人工确认后按 CLI 继续提交金额项与修改。
    accepted = claims.accept_change(
        db, ChangeIdInput(case_id=uuid.UUID(case_id), change_id=change.id),
        actor="human", entry="local-web")
    db.rollback()  # 结束本事务，释放共享锁，CLI 的独立会话才能取 FOR UPDATE
    group = accepted.groups[0]
    added = cli.dispatch(parser.parse_args([
        "claim-add-item", "--case-id", case_id, "--group-id", str(group.id),
        "--title", "合同价款", "--category", "principal",
        "--amount-cents", str(cents("1,280,000.00")), "--request-key", "cli-i1"]))
    assert added.pending_changes[0].actor == "agent"
    assert added.pending_changes[0].diff[0].after["amount_cents"] == cents("1,280,000.00")

    edited = cli.dispatch(parser.parse_args([
        "claim-update-group", "--case-id", case_id, "--group-id", str(group.id),
        "--expected-revision", str(group.revision), "--request-key", "cli-g2",
        "--no-include-in-total"]))
    assert edited.pending_changes[-1].diff[0].after["include_in_total"] is False

    # CLI 不提供确认/撤回；请求这类命令直接解析失败。
    with pytest.raises(SystemExit):
        parser.parse_args(["claim-accept-change", "--case-id", case_id])
    with pytest.raises(SystemExit):
        parser.parse_args(["claim-revert-change", "--case-id", case_id])


def test_cli_invalid_amount_and_missing_request_key_fail_cleanly(db):
    from legal_workbench import cli
    case_id = str(cases.create_case(db, title="TEST-ONLY CLI 拒绝").case_id)
    parser = cli.build_parser()
    # Agent 必须带 request_key（argparse 层就拒绝）。
    with pytest.raises(SystemExit):
        parser.parse_args(["claim-add-item", "--case-id", case_id, "--group-id",
                           str(uuid.uuid4()), "--title", "甲", "--category", "principal",
                           "--amount-cents", "100"])
    # 超出协议上界的整数分在构造 payload 时被拒（CLI 边界不静默截断）。
    args = parser.parse_args([
        "claim-add-item", "--case-id", case_id, "--group-id", str(uuid.uuid4()),
        "--title", "甲", "--category", "principal", "--amount-cents", str(2 ** 53),
        "--request-key", "cli-big"])
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        cli.dispatch(args)


def test_protocol_contract_and_generated_types_cover_claims():
    """生成物与路由一致：每个 claims 写操作都有 Origin 头要求，TS 类型含全部 operationId。"""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    spec = json.loads((root / "docs/openapi.json").read_text())
    types = (root / "frontend/src/api/types.ts").read_text()
    operations = {
        ("get_claims", "get", "/api/v1/cases/{case_id}/claims"),
        ("list_claim_amount_candidates", "get", "/api/v1/cases/{case_id}/claims/amount-candidates"),
        ("claims_add_group", "post", "/api/v1/cases/{case_id}/claims/groups"),
        ("claims_update_group", "patch", "/api/v1/cases/{case_id}/claims/groups/{group_id}"),
        ("claims_reorder_group", "post", "/api/v1/cases/{case_id}/claims/groups/{group_id}/move"),
        ("claims_remove_group", "post", "/api/v1/cases/{case_id}/claims/groups/{group_id}/remove"),
        ("claims_add_item", "post", "/api/v1/cases/{case_id}/claims/items"),
        ("claims_update_item", "patch", "/api/v1/cases/{case_id}/claims/items/{item_id}"),
        ("claims_reorder_item", "post", "/api/v1/cases/{case_id}/claims/items/{item_id}/move"),
        ("claims_remove_item", "post", "/api/v1/cases/{case_id}/claims/items/{item_id}/remove"),
        ("claims_restore_item", "post", "/api/v1/cases/{case_id}/claims/items/{item_id}/restore"),
        ("claims_accept_change",
         "post", "/api/v1/cases/{case_id}/claims/changes/{change_id}/accept"),
        ("claims_accept_all", "post", "/api/v1/cases/{case_id}/claims/changes/accept-all"),
        ("claims_revert_change",
         "post", "/api/v1/cases/{case_id}/claims/changes/{change_id}/revert"),
    }
    for operation, method, path in operations:
        item = spec["paths"][path][method]
        assert item["operationId"] == operation, path
        assert f"{operation}:" in types, operation
        if method == "get":
            assert all(param["name"] != "Origin" for param in item["parameters"])
        else:
            assert ("Origin", True) in [(param["name"], param["required"])
                                        for param in item["parameters"]]
            assert item["requestBody"]["required"] is True
        assert item["responses"]["409"]["content"]["application/json"]["schema"]["$ref"].endswith(
            "/ErrorResponse")
    # 金额是整数分：契约里是 integer，不是 number/string。
    schema = spec["components"]["schemas"]["ClaimItemOut"]["properties"]["amount_cents"]
    assert schema["type"] == "integer" and schema["minimum"] == 0
    # 响应类型必须来自 Pydantic 模型，而非泛型对象。
    assert "ClaimSnapshot-Output" in spec["components"]["schemas"]
    assert spec["components"]["schemas"]["ClaimSnapshot-Output"]["properties"]["items"][
        "items"]["$ref"].endswith("/ClaimItemOut")


def test_service_agent_whitelist_still_guards_transport_calls(db):
    """MCP/CLI 只能调用白名单命令；服务层角色守卫不被入口绕过。"""
    case_id = create_case(db, "TEST-ONLY 白名单")
    db.commit()
    snapshot = claims.add_group(db, AddClaimGroupInput(case_id=case_id, title="组"),
                                actor="human", entry="local-web")
    group = snapshot.groups[0]
    from legal_workbench.schemas import ClaimGroupRevisionInput
    assert _codes(lambda: claims.remove_group(
        db, ClaimGroupRevisionInput(case_id=case_id, group_id=group.id,
                                    expected_revision=group.revision),
        actor="agent", entry="mcp")) == ErrorCode.FORBIDDEN


def test_mcp_update_requires_request_key_and_amounts_are_strict(db):
    """MCP：update 必须带 request_key；金额入参是严格整数（schema 与模型两层）。"""
    import asyncio

    from pydantic import ValidationError

    from legal_workbench import mcp_server

    async def tool_schemas() -> dict[str, dict]:
        return {tool.name: tool.inputSchema for tool in await mcp_server.server.list_tools()}

    schemas = asyncio.run(tool_schemas())
    for name in ("claim_update_group", "claim_update_item"):
        assert "request_key" in schemas[name]["required"], name
    assert schemas["claim_add_item"]["properties"]["amount_cents"]["type"] == "integer"
    assert schemas["claim_add_item"]["properties"]["amount_cents"]["maximum"] == 2 ** 53 - 1
    assert schemas["claim_update_item"]["properties"]["amount_cents"]["anyOf"][0]["type"] == "integer"

    # 绕过框架校验直接调用时，模型层同样拒绝 bool / 字符串 / 小数。
    case_id = str(create_case(db, "TEST-ONLY MCP 严格整数"))
    db.commit()
    snapshot = claims.add_group(db, AddClaimGroupInput(case_id=uuid.UUID(case_id), title="组"),
                                actor="human", entry="local-web")
    group_id = str(snapshot.groups[0].id)
    db.commit()
    for bad in (True, "100", 1.5):
        with pytest.raises(ValidationError):
            mcp_server.claim_add_item(case_id, group_id, "甲", "principal", bad, "mcp-bad")


def test_cli_update_requires_request_key_and_reports_invalid_input(db, capsys):
    """CLI：update 缺 request_key 解析失败；越界金额给结构化 invalid_input，不抛 traceback。"""
    from legal_workbench import cli

    case_id = str(create_case(db, "TEST-ONLY CLI 结构化错误"))
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["claim-update-group", "--case-id", case_id, "--group-id",
                           str(uuid.uuid4()), "--expected-revision", "0", "--title", "甲"])
    with pytest.raises(SystemExit):
        parser.parse_args(["claim-update-item", "--case-id", case_id, "--item-id",
                           str(uuid.uuid4()), "--expected-revision", "0", "--title", "甲"])

    args = parser.parse_args([
        "claim-add-item", "--case-id", case_id, "--group-id", str(uuid.uuid4()),
        "--title", "甲", "--category", "principal", "--amount-cents", str(2 ** 53),
        "--request-key", "cli-big"])
    assert cli.main(["claim-add-item", "--case-id", args.case_id, "--group-id",
                     args.group_id, "--title", "甲", "--category", "principal",
                     "--amount-cents", str(2 ** 53), "--request-key", "cli-big"]) == 2
    err = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert err["code"] == "invalid_input"
    assert err["details"]["errors"]
