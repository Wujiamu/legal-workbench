"""Relation-graph and evidence-catalog rule configuration (P2).

The single source of truth for entity kinds, litigation roles, relation kinds,
the four credibility tiers (including their line style), snapshot stage presets,
consent channels and the agent command whitelist.  Mirrors
``frontend/src/graph/rules.ts`` (P1 prototype) so the view layer keeps no
hard-coded vocabulary; adding a kind or a stage is a config change, not a
migration (change record §4 rule 1, review checklist item 1).

The database only enforces non-null columns, same-case composite foreign keys,
closed enum sets that are *structural* (actor, review state, consent action,
source kind) and length/range checks.  Every kind/role/credibility/stage
decision is validated here.
"""
from __future__ import annotations

import copy
from typing import Any

# --- command set -------------------------------------------------------------
# Closed set of write commands.  Freeze/restore, consent and accept/revert are
# human-only and deliberately absent from AGENT_COMMANDS (D3, checklist 2).
COMMANDS = (
    "add_entity", "update_entity", "remove_entity", "restore_entity",
    "add_alias", "remove_alias",
    "add_relation", "update_relation", "remove_relation", "restore_relation",
    "add_source", "remove_source",
    "create_graph", "update_graph", "remove_graph",
    "add_to_graph", "remove_from_graph", "save_layout",
    "upsert_group", "remove_group",
    "create_snapshot", "restore_snapshot", "add_snapshot_consent",
    "accept_change", "accept_all", "revert_change",
)

AGENT_COMMANDS = (
    "add_entity", "update_entity", "add_alias",
    "add_relation", "update_relation", "add_source",
    "create_graph", "update_graph", "add_to_graph",
    "add_evidence_group", "add_evidence_item", "add_evidence_binding",
    "update_evidence_item", "import_evidence_catalog",
)

EVIDENCE_COMMANDS = (
    "add_group", "update_group", "reorder_groups", "remove_group",
    "add_item", "update_item", "move_item", "remove_item",
    "add_binding", "remove_binding",
    "confirm_item", "reject_item", "confirm_binding", "reject_binding",
    "import_catalog",
    "accept_change", "accept_all", "revert_change",
)

EVIDENCE_AGENT_COMMANDS = (
    "add_group", "add_item", "update_item", "add_binding", "import_catalog",
)

# --- limits ------------------------------------------------------------------
ENTITY_NAME_MAX = 200
ALIAS_MAX = 200
NOTE_MAX = 4000
RELATION_LABEL_MAX = 200
PROVES_MAX = 500
GRAPH_TITLE_MAX = 200
GRAPH_DESC_MAX = 2000
GROUP_TITLE_MAX = 200
GROUP_COLOR_MAX = 40
SNAPSHOT_STAGE_MAX = 100
SNAPSHOT_NOTE_MAX = 2000
CONSENTER_MAX = 200
SOURCE_DETAIL_MAX = 1000
CONSENT_NOTE_MAX = 2000
EVIDENCE_GROUP_TITLE_MAX = 200
EVIDENCE_GROUP_PURPOSE_MAX = 1000
EVIDENCE_ITEM_TITLE_MAX = 300
EVIDENCE_ITEM_PURPOSE_MAX = 2000
MAX_PAGE = 100000
CURRENCY_MAX = 8
MAX_GRAPH_POSITION = 10000
MAX_LAYOUT_COORD = 1_000_000.0

ENTITY_KINDS = (
    {"value": "person", "label": "自然人"},
    {"value": "org", "label": "法人或组织"},
    {"value": "agency", "label": "机关"},
    {"value": "court", "label": "法院"},
    {"value": "other", "label": "其他"},
)

ROLES = (
    {"value": "plaintiff", "label": "原告"},
    {"value": "defendant", "label": "被告"},
    {"value": "third_party", "label": "第三人"},
    {"value": "witness", "label": "证人"},
    {"value": "outsider", "label": "案外人"},
    {"value": "legal_rep", "label": "法定代表人"},
    {"value": "controller", "label": "实际控制人"},
    {"value": "shareholder", "label": "股东"},
    {"value": "guarantor", "label": "担保人"},
    {"value": "creditor", "label": "债权人"},
    {"value": "debtor", "label": "债务人"},
)

RELATION_KINDS = (
    {"value": "equity_control", "label": "股权 / 控制"},
    {"value": "office_rep", "label": "任职 / 代表"},
    {"value": "contract", "label": "合同相对方"},
    {"value": "guarantee", "label": "担保"},
    {"value": "debt", "label": "债权债务"},
    {"value": "fund_flow", "label": "资金往来"},
    {"value": "agency", "label": "代理"},
    {"value": "kinship", "label": "亲属"},
    {"value": "affiliate", "label": "关联"},
    {"value": "other", "label": "其他"},
)

# Line style lives here so the canvas and the legend stay in one vocabulary.
CREDIBILITY = (
    {"value": "evidenced", "label": "有证据", "badge": "实线",
     "dash": "", "stroke": "#2f5d50", "width": 2.2, "hint": "至少关联一条有效出处"},
    {"value": "suspected", "label": "待查 / 存疑", "badge": "红粗虚线",
     "dash": "10 5", "stroke": "#b3261e", "width": 3.4, "hint": "怀疑存在，尚无证据"},
    {"value": "disputed", "label": "对方主张 / 有争议", "badge": "橙点线",
     "dash": "2 6", "stroke": "#c2650a", "width": 2.6, "hint": "对方主张，我方不认可"},
    {"value": "ruled_out", "label": "已排除", "badge": "灰删除线",
     "dash": "4 4", "stroke": "#8b8f8b", "width": 1.6, "hint": "查证不成立，保留备查"},
)

CREDIBILITY_VALUES = tuple(entry["value"] for entry in CREDIBILITY)
DOWNGRADE_TARGETS = tuple(value for value in CREDIBILITY_VALUES if value != "evidenced")

# Q3/Q4 default wording confirmed with the P1 prototype review.
SNAPSHOT_STAGES = ("初次沟通", "方案沟通", "起诉前", "庭前", "庭后", "二审")

CONSENT_CHANNELS = (
    {"value": "meeting", "label": "会议"},
    {"value": "phone", "label": "电话"},
    {"value": "wechat", "label": "微信"},
    {"value": "email", "label": "邮件"},
    {"value": "written", "label": "书面"},
    {"value": "other", "label": "其他"},
)

CONSENT_ACTIONS = ("confirm", "amend", "revoke")

# Relation attribute schema (``case_relations.attrs``).
RELATION_ATTR_KEYS = ("share_pct", "amount_cents", "currency", "date_start", "date_end")

DEFAULT_RULES: dict[str, Any] = {
    "entity_kinds": [dict(entry) for entry in ENTITY_KINDS],
    "roles": [dict(entry) for entry in ROLES],
    "relation_kinds": [dict(entry) for entry in RELATION_KINDS],
    "credibility": [dict(entry) for entry in CREDIBILITY],
    "snapshot_stages": list(SNAPSHOT_STAGES),
    "consent_channels": [dict(entry) for entry in CONSENT_CHANNELS],
    "consent_actions": list(CONSENT_ACTIONS),
    "agent_commands": list(AGENT_COMMANDS),
    "evidence_agent_commands": list(EVIDENCE_AGENT_COMMANDS),
    "relation_attr_keys": list(RELATION_ATTR_KEYS),
    "max_path_depth": 8,
    "max_paths": 20,
}


def get_rules() -> dict[str, Any]:
    """Return a deep copy so callers cannot mutate the shared config."""
    return copy.deepcopy(DEFAULT_RULES)


# --- helpers -----------------------------------------------------------------

def _values(options: tuple[dict[str, str], ...] | list[dict[str, Any]]) -> set[str]:
    return {entry["value"] for entry in options}


def valid_entity_kind(rules: dict[str, Any], kind: str) -> bool:
    return kind in _values(rules["entity_kinds"])


def valid_role(rules: dict[str, Any], role: str) -> bool:
    return role in _values(rules["roles"])


def valid_relation_kind(rules: dict[str, Any], kind: str) -> bool:
    return kind in _values(rules["relation_kinds"])


def valid_credibility(rules: dict[str, Any], value: str) -> bool:
    return value in _values(rules["credibility"])


def credibility_rule(rules: dict[str, Any], value: str) -> dict[str, Any] | None:
    return next((entry for entry in rules["credibility"] if entry["value"] == value), None)


def valid_snapshot_stage(rules: dict[str, Any], stage: str) -> bool:
    # Custom stages are allowed: the presets are suggestions, not a closed set
    # (Q4).  Only the length is enforced; empty labels are rejected by the caller.
    return bool(stage.strip()) and len(stage.strip()) <= SNAPSHOT_STAGE_MAX


def stage_presets(rules: dict[str, Any]) -> list[str]:
    return list(rules["snapshot_stages"])


def valid_consent_channel(rules: dict[str, Any], channel: str) -> bool:
    return channel in _values(rules["consent_channels"])


def agent_allowed(rules: dict[str, Any], command: str) -> bool:
    return command in rules["agent_commands"]


def evidence_agent_allowed(rules: dict[str, Any], command: str) -> bool:
    return command in rules["evidence_agent_commands"]
