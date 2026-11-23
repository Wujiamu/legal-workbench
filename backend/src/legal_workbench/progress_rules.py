"""Progress rule configuration (K1/K2/K3/K4/K6).

The single source of truth for node kinds, legal states, containment, relation
types, template fragments, agent-allowed commands and title presets. The
database only enforces non-null and same-case composite foreign keys; every
hierarchy/kind/state decision is validated here, so adding a kind, a fourth
level or a new case type is a config change, not a schema change.

Mirrors frontend/src/progress/rules.ts (P1 prototype default). GET /progress/rules
serves this to the frontend in P3.
"""
from __future__ import annotations

from typing import Any

# Closed set of write commands. accept/revert are human-only and deliberately
# absent from AGENT_COMMANDS (K5 / plan 6.3).
COMMANDS = (
    "add_node", "update_node", "move_node", "set_active_stage", "insert_fragment",
    "remove_node", "restore_node", "add_relation", "remove_relation", "link",
    "unlink", "accept_change", "accept_all", "revert_change",
)

AGENT_COMMANDS = (
    "add_node", "update_node", "move_node", "set_active_stage", "insert_fragment",
    "remove_node", "add_relation", "remove_relation", "link", "unlink",
)

TITLE_MAX = 200
NOTE_MAX = 4000

_STAGE_STATES = [
    {"value": "not_started", "label": "未开始"},
    {"value": "in_progress", "label": "进行中"},
    {"value": "completed", "label": "已完成"},
    {"value": "skipped", "label": "已跳过"},
]

DEFAULT_RULES: dict[str, Any] = {
    "kinds": {
        "program": {
            "label": "程序",
            "states": [
                {"value": "not_started", "label": "未开始"},
                {"value": "in_progress", "label": "进行中"},
                {"value": "completed", "label": "已完成"},
            ],
            "default_state": "not_started",
        },
        "stage": {"label": "阶段", "states": _STAGE_STATES, "default_state": "not_started"},
        "event": {
            "label": "事件",
            "states": [
                {"value": "planned", "label": "计划"},
                {"value": "occurred", "label": "已发生"},
                {"value": "cancelled", "label": "取消"},
            ],
            "default_state": "planned",
        },
        "work": {
            "label": "工作",
            "states": [
                {"value": "pending", "label": "未完成"},
                {"value": "done", "label": "已完成"},
                {"value": "cancelled", "label": "取消"},
            ],
            "default_state": "pending",
        },
    },
    # parent kind (root uses 'case') -> allowed child kinds; hierarchy lives here only.
    "containment": {
        "case": ["program", "stage"],
        "program": ["program", "stage", "event", "work"],
        "stage": ["work", "event"],
        "event": ["work"],
        "work": ["work"],
    },
    "relation_types": [
        {"value": "causes", "label": "引起"},
        {"value": "relates", "label": "关联"},
    ],
    "fragments": {
        "litigation-default": {
            "label": "诉讼默认",
            "version": 1,
            "roots": [
                {"kind": "stage", "title": "诉前准备"},
                {"kind": "program", "title": "一审", "children": [
                    {"kind": "stage", "title": "立案"},
                    {"kind": "stage", "title": "庭前程序"},
                    {"kind": "stage", "title": "庭审"},
                    {"kind": "stage", "title": "裁判"},
                ]},
                # 结案归档是案件级操作（树外按钮），不是进度节点。
            ],
        },
        "second-instance": {
            "label": "二审", "version": 1,
            "roots": [{"kind": "program", "title": "二审", "children": [
                {"kind": "stage", "title": "上诉"},
                {"kind": "stage", "title": "二审审理"},
                {"kind": "stage", "title": "二审裁判"},
            ]}],
        },
        "remand": {
            "label": "发回重审", "version": 1,
            "roots": [{"kind": "program", "title": "发回重审", "children": [
                {"kind": "stage", "title": "重审立案"},
                {"kind": "stage", "title": "重审庭前程序"},
                {"kind": "stage", "title": "重审庭审"},
                {"kind": "stage", "title": "重审裁判"},
            ]}],
        },
        "retrial": {
            "label": "再审", "version": 1,
            "roots": [{"kind": "program", "title": "再审", "children": [
                {"kind": "stage", "title": "再审申请"},
                {"kind": "stage", "title": "再审审查"},
                {"kind": "stage", "title": "再审审理"},
                {"kind": "stage", "title": "再审裁判"},
            ]}],
        },
        "enforcement": {
            "label": "执行", "version": 1,
            "roots": [{"kind": "program", "title": "执行", "children": [
                {"kind": "stage", "title": "执行申请"},
                {"kind": "stage", "title": "执行立案"},
                {"kind": "stage", "title": "执行实施"},
                {"kind": "stage", "title": "执行结案"},
            ]}],
        },
    },
    "default_fragment": "litigation-default",
    "agent_commands": list(AGENT_COMMANDS),
    "title_presets": {
        "work": [
            "证据整理", "证据清单起草", "质证意见起草", "起诉状起草", "答辩状起草",
            "代理词起草", "法律检索", "案例检索", "阅卷", "当事人沟通",
            "出庭准备", "保全申请准备", "鉴定申请准备", "和解方案评估", "执行申请准备",
        ],
        "event": [
            "证据交换", "庭前会议", "开庭", "谈话", "询问", "质证",
            "调解", "和解谈判", "宣判", "送达", "保全执行", "鉴定听证",
        ],
    },
}


def get_rules() -> dict[str, Any]:
    """Return a deep-ish copy so callers cannot mutate the shared config."""
    import copy
    return copy.deepcopy(DEFAULT_RULES)


def kind_rule(rules: dict[str, Any], kind: str) -> dict[str, Any] | None:
    return rules["kinds"].get(kind)


def allowed_children(rules: dict[str, Any], parent_kind: str) -> list[str]:
    return rules["containment"].get(parent_kind, [])


def valid_state(rules: dict[str, Any], kind: str, state: str) -> bool:
    rule = kind_rule(rules, kind)
    return bool(rule) and any(entry["value"] == state for entry in rule["states"])


def default_state(rules: dict[str, Any], kind: str) -> str:
    return rules["kinds"][kind]["default_state"]


def valid_relation_type(rules: dict[str, Any], relation_type: str) -> bool:
    return any(entry["value"] == relation_type for entry in rules["relation_types"])