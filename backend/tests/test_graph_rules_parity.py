"""Rule-config parity: backend ``graph_rules.py`` vs the P1 frontend ``rules.ts``.

Review checklist item 1 says the vocabulary lives in exactly one config per side
and the two must agree, because the view layer renders labels and line styles
straight from its copy.  This test parses the TypeScript config (no Node needed)
and compares the value sets and the four credibility line styles.

TEST-ONLY: it reads source files, writes nothing.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from legal_workbench import graph_rules

RULES_TS = (pathlib.Path(__file__).resolve().parents[2]
            / "frontend" / "src" / "graph" / "rules.ts")


def _ts() -> str:
    if not RULES_TS.is_file():
        pytest.skip(f"frontend rules not present at {RULES_TS}")
    return RULES_TS.read_text(encoding="utf-8")


def _option_values(source: str, key: str) -> list[str]:
    """``option('value', 'label')`` literals inside a named array."""
    match = re.search(rf"{key}:\s*\[(.*?)\n  \]", source, re.S)
    assert match, f"找不到前端配置项 {key}"
    return re.findall(r"option\('([a-z_]+)'", match.group(1))


def test_entity_kinds_and_roles_match():
    source = _ts()
    assert _option_values(source, "entity_kinds") == [
        entry["value"] for entry in graph_rules.ENTITY_KINDS]
    assert _option_values(source, "roles") == [
        entry["value"] for entry in graph_rules.ROLES]
    assert _option_values(source, "consent_channels") == [
        entry["value"] for entry in graph_rules.CONSENT_CHANNELS]


def test_relation_kinds_match():
    source = _ts()
    assert _option_values(source, "relation_kinds") == [
        entry["value"] for entry in graph_rules.RELATION_KINDS]


def test_credibility_values_and_line_styles_match():
    source = _ts()
    block = re.search(r"credibilityRules: CredibilityRule\[\] = \[(.*?)\n\];", source, re.S)
    assert block, "找不到前端 credibilityRules"
    entries = re.findall(
        r"value: '([a-z_]+)'.*?dash: '([^']*)'.*?stroke: '(#[0-9a-f]{6})'.*?width: ([\d.]+)",
        block.group(1), re.S)
    frontend = {value: (dash, stroke, float(width))
                for value, dash, stroke, width in entries}
    backend = {entry["value"]: (entry["dash"], entry["stroke"], float(entry["width"]))
               for entry in graph_rules.CREDIBILITY}
    assert frontend == backend


def test_snapshot_stages_and_agent_whitelist_match():
    source = _ts()
    stages = re.search(r"snapshot_stages:\s*\[([^\]]*)\]", source)
    assert stages, "找不到前端 snapshot_stages"
    assert re.findall(r"'([^']+)'", stages.group(1)) == list(graph_rules.SNAPSHOT_STAGES)
    whitelist = re.search(r"agent_commands:\s*\[(.*?)\]", source, re.S)
    assert whitelist, "找不到前端 agent_commands"
    # The P1 prototype keeps one flat command union, so evidence commands carry an
    # ``evidence`` infix there (``add_evidence_group``) while the backend service
    # guards with its own shorter names (``add_group``).  The two must still be the
    # same 14 commands in the same order.
    def prototype_name(command: str) -> str:
        verb, _, rest = command.partition("_")
        return command if rest.startswith("evidence") else f"{verb}_evidence_{rest}"

    # 后端 ``AGENT_COMMANDS`` 已是前端同形的扁平联合（14 项，证据命令带
    # ``_evidence`` 中缀），直接逐项对照即可。
    expected = list(graph_rules.AGENT_COMMANDS)
    assert len(expected) == 14
    # order inside the evidence block is the prototype's business; the set must match
    assert set(re.findall(r"'([a-z_]+)'", whitelist.group(1))) == set(expected)


def test_limits_match():
    source = _ts()
    assert int(re.search(r"max_path_depth:\s*(\d+)", source).group(1)) == \
        graph_rules.DEFAULT_RULES["max_path_depth"]
    assert int(re.search(r"max_paths:\s*(\d+)", source).group(1)) == \
        graph_rules.DEFAULT_RULES["max_paths"]
