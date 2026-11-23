"""T14: CLI JSON contract and a real MCP stdio client round trip."""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import uuid

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy import select

from legal_workbench import cli, models
from legal_workbench.config import load_settings
from legal_workbench.db import get_session_factory

from . import factories

BACKEND = __import__("pathlib").Path(__file__).resolve().parents[1]


@pytest.fixture
def env(monkeypatch, tmp_path, import_root, test_database):
    settings = dataclasses.replace(load_settings(), import_roots=(import_root,),
                                   data_root=tmp_path / "data")
    # Subprocesses must talk to the test database, never the development one.
    monkeypatch.setenv("LWB_DATABASE_URL", test_database[1])
    monkeypatch.setenv("LWB_MIGRATION_URL", test_database[0])
    monkeypatch.setenv("LWB_IMPORT_ROOTS", str(import_root))
    monkeypatch.setenv("LWB_DATA_ROOT", str(settings.data_root))
    return settings


@pytest.fixture
def import_root(tmp_path):
    root = tmp_path / "import-root"
    root.mkdir()
    (root / "合同.txt").write_bytes("TEST-ONLY 合同约定金额100元".encode())
    return root


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "legal_workbench.cli", *args],
        cwd=BACKEND, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(BACKEND / "src")},
    )


def test_cli_create_and_list_case_is_json(env):
    created = run_cli("create-case", "--title", "TEST-ONLY 虚构案件")
    assert created.returncode == 0, created.stderr
    payload = json.loads(created.stdout)
    assert set(payload) == {"case_id", "revision"}
    listed = run_cli("list-cases")
    assert listed.returncode == 0
    body = json.loads(listed.stdout)
    assert body["total"] == 1 and body["items"][0]["case_id"] == payload["case_id"]


def test_cli_error_goes_to_stderr_with_nonzero_exit(env):
    result = run_cli("list-materials", "--case-id", "not-a-uuid")
    assert result.returncode == 2
    assert result.stdout == ""
    error = json.loads(result.stderr.strip().splitlines()[-1])
    assert error["code"] == "invalid_input"


def test_cli_import_search_read_source_round_trip(env, import_root):
    case_id = json.loads(run_cli("create-case", "--title", "TEST-ONLY 案件").stdout)["case_id"]
    imported = run_cli("import", "--case-id", case_id, "--source-path", str(import_root),
                       "--source-label", "TEST-ONLY", "--request-key", "cli-1")
    assert imported.returncode == 0, imported.stderr
    job_id = json.loads(imported.stdout)["job_id"]
    assert run_cli("run-worker").returncode == 0
    status = json.loads(run_cli("job-status", "--case-id", case_id, "--job-id", job_id).stdout)
    assert status["status"] in ("succeeded", "partial")
    found = json.loads(run_cli("search", "--case-id", case_id, "--query", "合同",
                               "--mode", "literal").stdout)
    assert found["total"] == 1
    ref = found["hits"][0]["source"]["source_ref"]
    source = json.loads(run_cli("read-source", "--case-id", case_id,
                                "--source-ref", ref).stdout)
    assert "TEST-ONLY" in source["text"]
    amount = json.loads(run_cli("search", "--case-id", case_id, "--query", "100",
                                "--mode", "amount", "--currency", "CNY").stdout)
    assert amount["total"] == 1 and amount["hits"][0]["currency"] == "CNY"


def test_cli_analysis_save_and_review(env, import_root):
    case_id = json.loads(run_cli("create-case", "--title", "TEST-ONLY 案件").stdout)["case_id"]
    run_cli("import", "--case-id", case_id, "--source-path", str(import_root),
            "--source-label", "TEST-ONLY", "--request-key", "cli-2")
    run_cli("run-worker")
    hit = json.loads(run_cli("search", "--case-id", case_id, "--query", "合同",
                             "--mode", "literal").stdout)["hits"][0]
    ref = hit["source"]["source_ref"]
    saved = json.loads(run_cli("save-analysis", "--case-id", case_id, "--title", "虚构分析",
                               "--content", "分析结论：合同有效。", "--statement-kind",
                               "agent_analysis", "--source-ref", ref).stdout)
    assert saved["revision"] == 0
    reviewed = json.loads(run_cli("review-analysis", "--case-id", case_id,
                                  "--analysis-id", saved["analysis_id"],
                                  "--version-id", saved["version_id"],
                                  "--expected-revision", "0",
                                  "--review-status", "approved").stdout)
    assert reviewed["review_status"] == "approved"


def _mcp_params() -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable, args=["-m", "legal_workbench.mcp_server"],
        cwd=str(BACKEND), env={**os.environ, "PYTHONPATH": str(BACKEND / "src"),
                               "LWB_DATA_ROOT": os.environ["LWB_DATA_ROOT"],
                               "LWB_IMPORT_ROOTS": os.environ["LWB_IMPORT_ROOTS"]},
    )


async def _mcp_session():
    return stdio_client(_mcp_params())


def test_mcp_lists_tools_without_stdout_noise(env):
    async def body():
        async with stdio_client(_mcp_params()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                return sorted(tool.name for tool in tools.tools)

    names = anyio.run(body)
    assert {"create_case", "import_materials", "search_case", "read_source",
            "save_analysis", "get_job_status", "list_materials"} <= set(names)
    # Destructive or unrestricted operations must never be exposed.
    for forbidden in ("delete_original", "run_sql", "restore_backup"):
        assert forbidden not in names


def test_mcp_agent_workflow_end_to_end(env, import_root):
    case_id = json.loads(run_cli("create-case", "--title", "TEST-ONLY MCP 案件").stdout)["case_id"]

    async def body():
        async with stdio_client(_mcp_params()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                imported = await session.call_tool("import_materials", {
                    "case_id": case_id, "source_path": str(import_root),
                    "source_label": "TEST-ONLY", "request_key": "mcp-1"})
                assert not imported.isError, imported.content
                job_id = json.loads(imported.content[0].text)["job_id"]

                run_cli("run-worker")

                status = await session.call_tool("get_job_status",
                                                 {"case_id": case_id, "job_id": job_id})
                assert json.loads(status.content[0].text)["status"] == "succeeded"

                found = await session.call_tool("search_case", {
                    "case_id": case_id, "query": "合同", "mode": "literal"})
                hits = json.loads(found.content[0].text)["hits"]
                assert len(hits) == 1
                ref = hits[0]["source"]["source_ref"]

                source = await session.call_tool("read_source",
                                                 {"case_id": case_id, "source_ref": ref})
                assert "TEST-ONLY" in json.loads(source.content[0].text)["text"]

                version = hits[0]["source"]["file_version_id"]
                annotated = await session.call_tool("annotate_file", {
                    "case_id": case_id, "file_version_id": version, "kind": "transcription",
                    "body": "TEST-ONLY alternate transcript"})
                assert not annotated.isError, annotated.content
                created = json.loads(annotated.content[0].text)
                assert created["author"] == "agent" and created["reviewed_text"] is None
                listed = await session.call_tool("list_annotations", {
                    "case_id": case_id, "query": "alternate transcript", "file_version_id": version})
                assert not listed.isError, listed.content
                assert json.loads(listed.content[0].text)["items"][0]["annotation_id"] == created["annotation_id"]

                saved = await session.call_tool("save_analysis", {
                    "case_id": case_id, "title": "虚构分析", "content": "结论：有效。",
                    "statement_kind": "agent_analysis", "source_refs": [ref]})
                assert not saved.isError, saved.content
                return json.loads(saved.content[0].text)

    saved = anyio.run(body)
    # A second, independent connection must see the same row.
    second = json.loads(run_cli("get-analysis-version", "--case-id", case_id,
                                "--analysis-id", saved["analysis_id"],
                                "--version-id", saved["version_id"]).stdout)
    assert second["content"] == "结论：有效。"


def test_mcp_error_is_structured_not_success_text(env):
    async def body():
        async with stdio_client(_mcp_params()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("search_case", {
                    "case_id": str(uuid.uuid4()), "query": "x", "mode": "literal"})
                return result

    result = anyio.run(body)
    assert result.isError
    text = result.content[0].text
    assert "not_found" in text or "不存在" in text


def test_mcp_rejects_cross_case_source_ref(env, import_root):
    case_id = json.loads(run_cli("create-case", "--title", "TEST-ONLY A").stdout)["case_id"]
    other = json.loads(run_cli("create-case", "--title", "TEST-ONLY B").stdout)["case_id"]
    run_cli("import", "--case-id", case_id, "--source-path", str(import_root),
            "--source-label", "TEST-ONLY", "--request-key", "mcp-2")
    run_cli("run-worker")
    ref = json.loads(run_cli("search", "--case-id", case_id, "--query", "合同",
                             "--mode", "literal").stdout)["hits"][0]["source"]["source_ref"]

    async def body():
        async with stdio_client(_mcp_params()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool("read_source",
                                               {"case_id": other, "source_ref": ref})

    result = anyio.run(body)
    assert result.isError
    assert "case_mismatch" in result.content[0].text or "不属于该案件" in result.content[0].text


def test_cli_does_not_print_secrets(env):
    result = run_cli("list-cases")
    combined = result.stdout + result.stderr
    assert "TEST-ONLY-key" not in combined
    assert "lwb_app" not in combined
