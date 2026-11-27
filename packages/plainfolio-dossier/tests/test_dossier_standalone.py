"""零数据库独立包验收：方案 A 围栏、清单、CLI 装卷、真实 stdio MCP、插件包。

这些测试不连数据库、不启动工作台，只用临时目录里的合成材料（TEST-ONLY）。
在三处真实边界上取证据：

1. **围栏**：默认只有 CWD 子树；`..` 穿越、外部符号链接、覆盖原件都必须被拒；
2. **产物**：用 `acceptance_simulation/dossier` 的真实虚构样本跑 CLI，打开 ZIP 核对
   四件套、页数与"原件未被改写"；
3. **协议**：真实 stdio 握手看到 4 个工具，并真的装出一份卷宗。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image
from pypdf import PdfReader
import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas as rl_canvas

from plainfolio_dossier import (
    DossierError,
    allowed_roots,
    init_manifest,
    load_manifest,
    resolve_readable_file,
    resolve_writable_output,
    validate_manifest,
)
from plainfolio_dossier.path_fence import PLAINFOLIO_ALLOWED_ROOTS_ENV

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_ROOT = REPO_ROOT / "packages" / "plainfolio-dossier"
PLUGIN_ROOT = REPO_ROOT / "plugins" / "plainfolio-dossier"
LAUNCHER = PLUGIN_ROOT / "scripts" / "run-mcp.py"
MARKETPLACE = REPO_ROOT / ".agents" / "plugins" / "marketplace.json"
ACCEPTANCE_DOSSIER = REPO_ROOT / "acceptance_simulation" / "dossier"

PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
FORBIDDEN_MARKERS = ("/Users/", "C:\\Users\\", "C:/Users/")

MCP_TOOLS = ["dossier_build", "dossier_init_manifest", "dossier_policy", "dossier_validate"]


# --- 合成材料 ---------------------------------------------------------------

def _pdf_bytes(page_texts: list[str]) -> bytes:
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    for text in page_texts:
        c.drawString(72, 720, text)
        c.showPage()
    c.save()
    return buf.getvalue()


def _png_bytes(width: int = 320, height: int = 480) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (215, 232, 226)).save(buf, format="PNG")
    return buf.getvalue()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def workdir(tmp_path, monkeypatch):
    """A fenced working directory: CWD-relative roots, no inherited env override."""
    root = (tmp_path / "work").resolve()
    root.mkdir(parents=True)
    monkeypatch.chdir(root)
    monkeypatch.delenv(PLAINFOLIO_ALLOWED_ROOTS_ENV, raising=False)
    return root


def _materials(root: Path) -> dict[str, Path]:
    """Two groups, three files (pdf 2 pages / pdf 1 page / png), plus a hidden file."""
    contract_dir = root / "材料" / "01_合同"
    photo_dir = root / "材料" / "02_履行"
    contract_dir.mkdir(parents=True)
    photo_dir.mkdir(parents=True)
    files = {
        "contract": contract_dir / "采购合同.pdf",
        "annex": contract_dir / "补充协议.pdf",
        "photo": photo_dir / "现场照片.png",
    }
    files["contract"].write_bytes(_pdf_bytes(["CONTRACT_P1", "CONTRACT_P2"]))
    files["annex"].write_bytes(_pdf_bytes(["ANNEX_P1"]))
    files["photo"].write_bytes(_png_bytes())
    (root / "材料" / ".DS_Store").write_bytes(b"hidden")     # 隐藏文件必须被跳过
    (photo_dir / ".临时锁").write_bytes(b"hidden")
    return files


def _cli(root: Path, *args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ}
    env.pop(PLAINFOLIO_ALLOWED_ROOTS_ENV, None)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-m", "plainfolio_dossier.cli", *args],
        cwd=str(root), env=env, capture_output=True, text=True, timeout=300,
    )


# --- 1. 方案 A 文件访问围栏 -------------------------------------------------

def test_fence_defaults_to_the_working_directory(workdir):
    assert allowed_roots() == (workdir,)

    inside = workdir / "材料.txt"
    inside.write_text("TEST-ONLY", encoding="utf-8")
    assert resolve_readable_file("材料.txt", allowed_roots()) == inside

    outside = workdir.parent / "外部材料.txt"
    outside.write_text("TEST-ONLY", encoding="utf-8")
    with pytest.raises(DossierError) as caught:
        resolve_readable_file(outside, allowed_roots())
    assert caught.value.code == "forbidden"
    assert "不在允许的根目录内" in caught.value.message
    assert caught.value.details["allowed_roots"] == [str(workdir)]


def test_fence_rejects_parent_traversal(workdir):
    (workdir.parent / "外部材料.txt").write_text("TEST-ONLY", encoding="utf-8")
    (workdir / "sub").mkdir()
    for candidate in ("../外部材料.txt", "sub/../../外部材料.txt"):
        with pytest.raises(DossierError) as caught:
            resolve_readable_file(candidate, allowed_roots())
        assert caught.value.code == "forbidden", candidate


def test_fence_rejects_symlinks_even_when_they_point_inside(workdir):
    outside = workdir.parent / "外部材料.txt"
    outside.write_text("TEST-ONLY", encoding="utf-8")
    inside = workdir / "内部材料.txt"
    inside.write_text("TEST-ONLY", encoding="utf-8")

    escaping = workdir / "逃逸链接.pdf"
    internal = workdir / "内部链接.pdf"
    try:
        escaping.symlink_to(outside)
        internal.symlink_to(inside)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows 无权限建链接
        pytest.skip("当前环境不允许创建符号链接")

    for candidate in (escaping, internal):
        with pytest.raises(DossierError) as caught:
            resolve_readable_file(candidate, allowed_roots())
        assert caught.value.code == "forbidden"
        assert "符号链接" in caught.value.message


def test_fence_honours_the_allowed_roots_override(workdir, tmp_path, monkeypatch):
    extra = (tmp_path / "外部卷宗").resolve()
    extra.mkdir()
    material = extra / "证据.pdf"
    material.write_bytes(_pdf_bytes(["P1"]))

    monkeypatch.setenv(PLAINFOLIO_ALLOWED_ROOTS_ENV, str(extra))
    assert allowed_roots() == (extra,)
    assert resolve_readable_file("证据.pdf", allowed_roots(), base_dir=extra) == material

    inside = workdir / "材料.txt"
    inside.write_text("TEST-ONLY", encoding="utf-8")
    with pytest.raises(DossierError) as caught:
        resolve_readable_file(inside, allowed_roots())
    assert caught.value.code == "forbidden"

    # 白名单里的目录不存在 → 配置错误必须报出来，不能默默退化成 CWD。
    monkeypatch.setenv(PLAINFOLIO_ALLOWED_ROOTS_ENV, str(tmp_path / "不存在"))
    with pytest.raises(DossierError) as caught:
        allowed_roots()
    assert caught.value.code == "invalid_input"
    assert "不存在" in caught.value.message


def test_fence_handles_macos_tmp_alias_and_rejects_intermediate_dir_symlink(workdir, monkeypatch):
    real_dir = workdir / "真实子目录"
    real_dir.mkdir()
    real_file = real_dir / "证据.pdf"
    real_file.write_bytes(_pdf_bytes(["P1"]))
    link_dir = workdir / "链接子目录"
    try:
        link_dir.symlink_to(real_dir)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")

    # 即使经由 macOS /tmp -> /private/tmp 别名构造未解析路径，中间符号链接目录仍必须被拒
    str_work = str(workdir)
    alias_path = (
        Path("/tmp" + str_work.removeprefix("/private/tmp")) / "链接子目录" / "证据.pdf"
        if sys.platform == "darwin" and str_work.startswith("/private/tmp")
        else link_dir / "证据.pdf"
    )
    with pytest.raises(DossierError) as caught:
        resolve_readable_file(alias_path, allowed_roots())
    assert caught.value.code == "forbidden"

    if sys.platform == "darwin" and str_work.startswith("/private/tmp"):
        alias_root = Path("/tmp" + str_work.removeprefix("/private/tmp")) / "真实子目录"
        monkeypatch.setenv(PLAINFOLIO_ALLOWED_ROOTS_ENV, str(alias_root))
        assert allowed_roots() == (real_dir,)
        monkeypatch.setenv(
            PLAINFOLIO_ALLOWED_ROOTS_ENV,
            str(Path("/tmp" + str_work.removeprefix("/private/tmp")) / "链接子目录"),
        )
        with pytest.raises(DossierError) as caught_root:
            allowed_roots()
        assert caught_root.value.code == "invalid_input"


def test_fence_refuses_to_overwrite_an_input_file(workdir):
    files = _materials(workdir)
    roots = allowed_roots()

    with pytest.raises(DossierError) as caught:
        resolve_writable_output(files["contract"], roots, forbidden_inputs=[files["contract"]])
    assert caught.value.code == "forbidden"
    assert caught.value.message == "输出路径不得覆盖输入源文件"

    # 相对路径写法同样命中（同一位置的不同拼法不能绕过红线）。
    relative = os.path.relpath(files["contract"], workdir)
    with pytest.raises(DossierError):
        resolve_writable_output(relative, roots, forbidden_inputs=[files["contract"]])

    output = resolve_writable_output(workdir / "dist" / "卷宗.zip", roots)
    assert output == workdir / "dist" / "卷宗.zip"

    with pytest.raises(DossierError) as caught:
        resolve_writable_output(workdir.parent / "逃逸.zip", roots)
    assert caught.value.code == "forbidden"


# --- 2. 清单：生成 / 加载 / 预检 --------------------------------------------

def test_init_manifest_scans_sorted_tree_and_writes_a_draft(workdir):
    _materials(workdir)
    output = workdir / "清单.json"

    draft = init_manifest(workdir / "材料", output, case_title="虚构合同纠纷案",
                          case_number="(2026)沪0105民初1号")

    assert [group["title"] for group in draft["groups"]] == ["01_合同", "02_履行"]
    titles = [item["title"] for group in draft["groups"] for item in group["items"]]
    assert titles == ["补充协议", "采购合同", "现场照片"]
    assert draft["case"]["case_number"] == "(2026)沪0105民初1号"
    first_binding = draft["groups"][0]["items"][0]["bindings"][0]
    assert first_binding["file_path"] == "材料/01_合同/补充协议.pdf"  # 相对清单所在目录
    assert first_binding["ext"] == ".pdf"

    on_disk = json.loads(output.read_text(encoding="utf-8"))
    assert on_disk == draft
    # 原子写入不得留下临时文件
    assert [path.name for path in workdir.iterdir() if path.name.endswith(".tmp")] == []


def test_init_manifest_never_overwrites_a_scanned_material(workdir):
    files = _materials(workdir)
    with pytest.raises(DossierError) as caught:
        init_manifest(workdir / "材料", files["contract"])
    assert caught.value.code == "forbidden"
    assert caught.value.message == "输出路径不得覆盖输入源文件"


def test_manifest_loads_json_and_csv_equivalently(workdir):
    files = _materials(workdir)

    json_path = workdir / "清单.json"
    json_path.write_text(json.dumps({
        "case": {"title": "虚构合同纠纷案", "plaintiff": "甲公司", "defendant": "乙公司"},
        "groups": [{
            "title": "合同",
            "items": [{
                "title": "采购合同（节选）", "purpose": "证明合同成立",
                "bindings": [{"file_path": "材料/01_合同/采购合同.pdf",
                              "material_name": "采购合同.pdf",
                              "page_start": 2, "page_end": 2}],
            }],
        }],
    }, ensure_ascii=False), encoding="utf-8")

    spec, manifest_path, resolved = load_manifest(json_path)
    assert manifest_path == json_path
    assert spec.case.title == "虚构合同纠纷案"
    binding = spec.groups[0].items[0].bindings[0]
    assert binding.page_start == 2 and binding.page_end == 2
    assert Path(binding.file_path) == files["contract"]  # 已改成绝对解析路径
    assert resolved == [files["contract"]]

    csv_path = workdir / "清单.csv"
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["group", "title", "purpose", "file_path", "page_start", "page_end", "acquisition"])
    writer.writerow(["合同", "采购合同（节选）", "证明合同成立", "材料/01_合同/采购合同.pdf", "1", "2", "扫描件"])
    writer.writerow(["合同", "采购合同（节选）", "", "材料/01_合同/补充协议.pdf", "", "", "原件"])
    writer.writerow(["履行", "现场照片", "证明交货状态", "材料/02_履行/现场照片.png", "", "", ""])
    csv_path.write_bytes(("\ufeff" + buffer.getvalue()).encode("utf-8"))

    csv_spec, _csv_manifest, csv_resolved = load_manifest(csv_path)
    assert [group.title for group in csv_spec.groups] == ["合同", "履行"]
    first_item = csv_spec.groups[0].items[0]
    assert first_item.purpose == "证明合同成立"
    assert len(first_item.bindings) == 2                      # 同一 (group,title) 合并
    assert first_item.bindings[0].page_start == 1
    assert first_item.bindings[1].acquisition == "原件"
    assert csv_spec.groups[1].items[0].title == "现场照片"
    assert len(csv_resolved) == 3

    broken = workdir / "缺列.csv"
    broken.write_text("group,title\n合同,采购合同\n", encoding="utf-8")
    with pytest.raises(DossierError) as caught:
        load_manifest(broken)
    assert caught.value.code == "invalid_input"
    assert "file_path" in caught.value.message


def test_validate_manifest_reports_clean_and_broken_inputs(workdir):
    files = _materials(workdir)

    clean = workdir / "干净清单.json"
    clean.write_text(json.dumps({
        "case": {"title": "虚构合同纠纷案"},
        "groups": [{
            "title": "合同",
            "items": [{
                "title": "采购合同", "purpose": "证明合同成立",
                "bindings": [{"file_path": "材料/01_合同/采购合同.pdf",
                              "material_name": "采购合同.pdf", "page_start": 1, "page_end": 2}],
            }],
        }],
    }, ensure_ascii=False), encoding="utf-8")

    result = validate_manifest(clean)
    assert result["valid"] is True
    assert result["issues"] == []
    assert (result["group_count"], result["item_count"], result["binding_count"]) == (1, 1, 1)

    broken = workdir / "问题清单.json"
    broken.write_text(json.dumps({
        "case": {"title": "虚构合同纠纷案"},
        "groups": [
            {"title": "空分组", "items": []},
            {"title": "合同", "items": [
                {"title": "页码倒挂", "bindings": [
                    {"file_path": "材料/01_合同/采购合同.pdf", "material_name": "采购合同.pdf",
                     "page_start": 2, "page_end": 1}]},
                {"title": "页码越界", "bindings": [
                    {"file_path": "材料/01_合同/补充协议.pdf", "material_name": "补充协议.pdf",
                     "page_start": 1, "page_end": 9}]},
                {"title": "文件缺失", "bindings": [
                    {"file_path": "材料/01_合同/不存在.pdf", "material_name": "不存在.pdf"}]},
                {"title": "未绑定条目", "bindings": []},
            ]},
        ],
    }, ensure_ascii=False), encoding="utf-8")

    broken_result = validate_manifest(broken)
    codes = [issue["code"] for issue in broken_result["issues"]]
    assert broken_result["valid"] is False
    assert "empty_group" in codes
    assert "page_range_inverted" in codes
    assert "page_range_exceeds_pdf" in codes
    assert "file_not_found" in codes
    assert "unbound_item" in codes
    # 错误项必须能定位到具体证据条目与文件
    missing = next(issue for issue in broken_result["issues"] if issue["code"] == "file_not_found")
    assert missing["item_code"] == "证据2-3"
    assert missing["file_path"].endswith("不存在.pdf")
    assert files["contract"].is_file()  # 预检只读


# --- 3. CLI + 真实仿真样本 --------------------------------------------------

def test_cli_builds_a_compliant_dossier_from_the_acceptance_samples(tmp_path):
    case_dir = (tmp_path / "case").resolve()
    case_dir.mkdir()
    materials = case_dir / "材料"
    shutil.copytree(ACCEPTANCE_DOSSIER, materials)
    originals = {
        path.relative_to(materials).as_posix(): _sha256(path)
        for path in materials.rglob("*") if path.is_file()
    }
    assert len(originals) == 10, "仿真材料应为 10 份"

    init = _cli(case_dir, "init-manifest", "--source-dir", "材料",
                "--case-title", "虚构设备买卖纠纷案", "--case-number", "(2026)沪0105民初1号",
                "--plaintiff", "甲公司", "--defendant", "乙公司", "--output", "清单.json")
    assert init.returncode == 0, init.stderr
    assert init.stdout.count("\n") == 1, "stdout 必须只有一行 JSON"
    init_payload = json.loads(init.stdout)
    assert init_payload["item_count"] == 10
    assert init_payload["group_count"] == 4
    assert (case_dir / "清单.json").is_file()

    validate = _cli(case_dir, "validate", "--manifest", "清单.json")
    assert validate.returncode == 0, validate.stderr
    validate_payload = json.loads(validate.stdout)
    assert validate_payload["valid"] is True
    assert validate_payload["binding_count"] == 10

    build = _cli(case_dir, "build", "--manifest", "清单.json", "--output", "证据卷宗.zip")
    assert build.returncode == 0, build.stderr
    payload = json.loads(build.stdout)
    assert payload["item_count"] == 10
    assert payload["total_body_pages"] >= 10
    assert payload["size_bytes"] > 100_000
    assert payload["warnings"], "损坏 PDF 必须留下可读告警"

    output = Path(payload["output_path"])
    assert output.is_file()
    with zipfile.ZipFile(output) as zf:
        names = set(zf.namelist())
        assert {"00_装卷说明.txt", "00_证据合订卷宗.pdf", "01_证据目录.docx",
                "01_证据目录.csv"} <= names
        splits = sorted(name for name in names if name.startswith("02_分项证据材料/"))
        assert len(splits) == 10
        assert any("01_民事起诉状.docx" in name for name in splits)

        merged = PdfReader(io.BytesIO(zf.read("00_证据合订卷宗.pdf")))
        assert len(merged.pages) == payload["total_pages"]
        for page in merged.pages:
            width, height = float(page.mediabox.width), float(page.mediabox.height)
            assert (abs(width - 595.28) < 2 and abs(height - 841.89) < 2) or (
                abs(width - 841.89) < 2 and abs(height - 595.28) < 2)
        text = "\n".join((page.extract_text() or "") for page in merged.pages)
        assert "虚构设备买卖纠纷案" in text
        assert "第 1 页" in text

        catalog = zf.read("01_证据目录.csv").decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(catalog)))
        assert rows[0][:3] == ["组号", "组名", "证据编号"]
        assert len(rows) == 11

    # 原件只读：装卷前后每一份材料字节一致
    after = {
        path.relative_to(materials).as_posix(): _sha256(path)
        for path in materials.rglob("*") if path.is_file()
    }
    assert after == originals


def test_cli_reports_business_errors_with_exit_code_two(tmp_path):
    case_dir = (tmp_path / "case").resolve()
    case_dir.mkdir()
    materials = case_dir / "材料"
    shutil.copytree(ACCEPTANCE_DOSSIER, materials)
    assert _cli(case_dir, "init-manifest", "--source-dir", "材料", "--output", "清单.json").returncode == 0

    outside = (tmp_path / "逃逸.zip")
    escaped = _cli(case_dir, "build", "--manifest", "清单.json", "--output", str(outside))
    assert escaped.returncode == 2
    assert escaped.stdout == ""
    assert "forbidden" in escaped.stderr
    assert not outside.exists()

    overwrite = _cli(case_dir, "build", "--manifest", "清单.json",
                     "--output", "材料/02_核心合同与担保/02_成套设备采购主合同.pdf")
    assert overwrite.returncode == 2
    assert "输出路径不得覆盖输入源文件" in overwrite.stderr

    broken = case_dir / "空清单.json"
    broken.write_text('{"case": {"title": "空案"}, "groups": []}', encoding="utf-8")
    empty = _cli(case_dir, "build", "--manifest", "空清单.json", "--output", "空.zip")
    assert empty.returncode == 2
    assert "暂无已确认的证据条目" in empty.stderr
    # 业务错误不得留下半个产物
    assert not (case_dir / "空.zip").exists()


def test_package_imports_no_database_or_http_stack():
    """独立包一旦拖进工作台依赖就失去"零数据库"意义，静态与运行期都查一遍。"""
    banned = ("sqlalchemy", "psycopg", "fastapi", "rapidocr", "alembic", "uvicorn")
    for path in sorted((PACKAGE_ROOT / "src" / "plainfolio_dossier").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for name in banned:
            assert f"import {name}" not in text, f"{path.name} 引入了 {name}"
            assert f"from {name}" not in text, f"{path.name} 引入了 {name}"

    probe = subprocess.run(
        [sys.executable, "-c",
         "import json, sys, plainfolio_dossier; "
         "print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] in "
         "('sqlalchemy','psycopg','fastapi','rapidocr_onnxruntime','alembic','uvicorn'))))"],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": str(PACKAGE_ROOT / "src")},
    )
    assert probe.returncode == 0, probe.stderr
    assert json.loads(probe.stdout) == []


# --- 4. 真实 stdio MCP ------------------------------------------------------

def _stdio_params(case_dir: Path) -> StdioServerParameters:
    env = {**os.environ, "PYTHONPATH": str(PACKAGE_ROOT / "src")}
    env.pop(PLAINFOLIO_ALLOWED_ROOTS_ENV, None)
    return StdioServerParameters(
        command=sys.executable, args=["-m", "plainfolio_dossier.mcp_server"],
        cwd=str(case_dir), env=env,
    )


def _tool_payload(result) -> dict:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        if set(structured) == {"result"} and isinstance(structured["result"], dict):
            return structured["result"]
        return structured
    return json.loads(result.content[0].text)


def test_stdio_mcp_exposes_four_tools_and_builds_a_dossier(tmp_path):
    case_dir = (tmp_path / "case").resolve()
    case_dir.mkdir()
    _materials(case_dir)
    manifest = case_dir / "清单.json"
    manifest.write_text(json.dumps({
        "case": {"title": "虚构合同纠纷案"},
        "groups": [{
            "title": "合同",
            "items": [{"title": "采购合同", "purpose": "证明合同成立",
                       "bindings": [{"file_path": "材料/01_合同/采购合同.pdf",
                                     "material_name": "采购合同.pdf"}]}],
        }],
    }, ensure_ascii=False), encoding="utf-8")

    async def body():
        async with stdio_client(_stdio_params(case_dir)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = sorted(tool.name for tool in (await session.list_tools()).tools)
                policy = await session.call_tool("dossier_policy", {})
                validated = await session.call_tool("dossier_validate", {"manifest_path": "清单.json"})
                built = await session.call_tool(
                    "dossier_build", {"manifest_path": "清单.json", "output_path": "卷宗.zip"})
                escaped = await session.call_tool("dossier_validate",
                                                  {"manifest_path": "../外面.json"})
                inited = await session.call_tool(
                    "dossier_init_manifest", {"source_dir": "材料", "output_path": "草案.json"})
                return tools, policy, validated, built, escaped, inited

    tools, policy, validated, built, escaped, inited = anyio.run(body)
    assert tools == MCP_TOOLS
    assert "allowed_roots" in _tool_payload(policy)
    assert _tool_payload(validated)["valid"] is True
    built_payload = _tool_payload(built)
    assert built_payload["item_count"] == 1
    assert Path(built_payload["output_path"]).is_file()
    with zipfile.ZipFile(built_payload["output_path"]) as zf:
        assert "00_证据合订卷宗.pdf" in zf.namelist()
    assert _tool_payload(escaped)["valid"] is False
    assert _tool_payload(inited)["item_count"] == 3


# --- 5. 插件包与市场登记 ----------------------------------------------------

def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_plugin_manifest_declares_a_portable_dossier_server():
    manifest = _read_json(PLUGIN_ROOT / "plugin.json")
    assert manifest["$schema"] == PLUGIN_SCHEMA
    assert manifest["name"] == "plainfolio-dossier"
    assert manifest["version"] == "0.1.0"
    assert manifest["description"].strip()
    assert manifest["skills"] == "./skills/"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert (PLUGIN_ROOT / "skills").is_dir()
    assert (PLUGIN_ROOT / ".mcp.json").is_file()
    openai = manifest["extensions"]["com.openai"]
    for key in ("category", "displayName", "shortDescription", "longDescription"):
        assert openai[key].strip(), key
    assert (PLUGIN_ROOT / openai["onboardingSkill"]).is_file()


def test_both_mcp_manifests_launch_the_portable_wrapper():
    portable = _read_json(PLUGIN_ROOT / "mcp.json")
    assert portable["$schema"] == MCP_SCHEMA
    server = portable["mcpServers"]["plainfolio-dossier"]
    assert server["type"] == "stdio"
    assert server["command"] == "python3"
    assert server["args"] == ["${PLUGIN_ROOT}/scripts/run-mcp.py"]
    assert not {"PLUGIN_ROOT", "PLUGIN_DATA"} & set(server.get("env", {}))

    codex = _read_json(PLUGIN_ROOT / ".mcp.json")["mcpServers"]["plainfolio-dossier"]
    assert {key: server.get(key) for key in ("command", "args", "env")} == {
        key: codex.get(key) for key in ("command", "args", "env")}
    assert LAUNCHER.is_file()


def test_plugin_ships_no_machine_specific_path():
    files = sorted(
        path for path in PLUGIN_ROOT.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )
    assert files
    offenders: list[str] = []
    for path in [*files, MARKETPLACE]:
        text = path.read_text(encoding="utf-8", errors="replace")
        for marker in FORBIDDEN_MARKERS:
            if marker in text:
                offenders.append(f"{path.name} -> {marker}")
    assert offenders == []


def test_launcher_print_plan_resolves_a_real_interpreter():
    result = subprocess.run(
        [sys.executable, str(LAUNCHER), "--print-plan"],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": ""},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("\n") == 1
    plan = json.loads(result.stdout)
    assert plan["source"] in {"PLAINFOLIO_DOSSIER_BIN", "PATH", "仓库虚拟环境", "当前解释器"}
    assert plan["cwd"] is None
    assert Path(plan["command"][0]).exists()
    assert plan["command"][-2:] == ["-m", "plainfolio_dossier.mcp_server"]

    unknown = subprocess.run(
        [sys.executable, str(LAUNCHER), "--serve"],
        capture_output=True, text=True, timeout=120,
    )
    assert unknown.returncode == 2
    assert unknown.stdout == ""
    assert "未知参数" in unknown.stderr


def test_marketplace_indexes_the_dossier_plugin_after_plainfolio():
    catalog = _read_json(MARKETPLACE)
    names = [entry["name"] for entry in catalog["plugins"]]
    assert names[0] == "plainfolio"
    assert catalog["plugins"][0]["version"] == "0.1.0"
    assert "plainfolio-dossier" in names
    assert len(names) == len(set(names))

    entry = next(item for item in catalog["plugins"] if item["name"] == "plainfolio-dossier")
    assert entry["version"] == _read_json(PLUGIN_ROOT / "plugin.json")["version"] == "0.1.0"
    assert entry["source"] == {"source": "local", "path": "./plugins/plainfolio-dossier"}
    resolved = (MARKETPLACE.parents[2] / entry["source"]["path"]).resolve()
    assert resolved == PLUGIN_ROOT.resolve()
    assert (resolved / "plugin.json").is_file()
