# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir spec for the 素章 / PlainFolio desktop application.

Why ``onedir`` and not ``onefile``: a one-file build unpacks ~80 MB of
PostgreSQL binaries and ONNX models into a temp directory on every launch,
which is slow enough to look like a hang and is a well-known trigger for
antivirus heuristics.  ``onedir`` also keeps the payload inspectable, so a
support engineer can check ``_internal/runtime`` without running anything.

Layout produced by this spec (``packaging/dist/PlainFolio/``):

    PlainFolio(.exe)          the launcher (console subsystem: MCP needs stdio)
    _internal/                Python runtime, packages, collected data
    _internal/frontend/dist   built single-page app
    _internal/backend/…       alembic.ini + migrations
    _internal/plugins/…       portable agent plugin
    _internal/runtime/…       added afterwards by packaging/stage_bundle.py

``_internal`` is what ``sys._MEIPASS`` points at, and therefore what
``legal_workbench.runtime_paths.resolve_bundle_root()`` returns at runtime.

Build (from the repository root):

    python -m PyInstaller --noconfirm --clean \
        --distpath packaging/dist --workpath packaging/build packaging/plainfolio.spec
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

SPEC_DIR = Path(SPECPATH).resolve()          # noqa: F821 - injected by PyInstaller
REPO_ROOT = SPEC_DIR.parent
SRC = REPO_ROOT / "backend" / "src"
# 仓库内第一方独立包（法院自动装卷器 / 离线阅卷解析内核）：可编辑安装时由 path 源
# 引入，冻结时必须显式把两处源码目录放进 pathex，否则运行期才 ImportError。
PACKAGES_SRC = (
    REPO_ROOT / "packages" / "plainfolio-dossier" / "src",
    REPO_ROOT / "packages" / "plainfolio-parse" / "src",
)
ENTRY = SRC / "legal_workbench" / "desktop_launcher.py"

datas = [
    (str(REPO_ROOT / "frontend" / "dist"), "frontend/dist"),
    (str(REPO_ROOT / "backend" / "migrations"), "backend/migrations"),
    (str(REPO_ROOT / "backend" / "alembic.ini"), "backend"),
    (str(REPO_ROOT / "plugins" / "plainfolio"), "plugins/plainfolio"),
]
binaries = []

# Data files that live inside third-party packages (ONNX runtime libraries, the
# PDFium shared library, OCR dictionaries) and are not found by static analysis.
for package in ("rapidocr_onnxruntime", "onnxruntime", "pypdfium2", "pypdfium2_raw",
                "psycopg", "psycopg_binary", "alembic", "mcp", "reportlab",
                "docx", "openpyxl", "lxml"):
    try:
        datas += collect_data_files(package)
    except Exception:  # noqa: BLE001 - a missing optional package is not fatal here
        pass

for package in ("pypdfium2_raw", "pypdfium2", "onnxruntime", "rapidocr_onnxruntime",
                "psycopg_binary", "lxml"):
    try:
        binaries += collect_dynamic_libs(package)
    except Exception:  # noqa: BLE001 - a missing optional package is not fatal here
        pass

hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "sqlalchemy.dialects.postgresql",
    "sqlalchemy.dialects.postgresql.psycopg",
    # Declared explicitly because several of these are imported lazily or only
    # for their data files; a missing one shows up as a runtime ImportError in
    # a frozen build, which is the most expensive place to find it.
    "psycopg",
    "psycopg_binary",
    "rapidocr_onnxruntime",
    "rapidocr_onnxruntime.main",
    "onnxruntime",
    "pypdfium2",
    "pypdfium2_raw",
    "pypdf",
    "docx",
    "openpyxl",
    "lxml",
    "lxml.etree",
    "lxml._elementpath",
    "reportlab",
    "reportlab.pdfbase._cidfontdata",
    "reportlab.pdfbase.cidfonts",
    "alembic.runtime.migration",
    "alembic.script",
    "mcp.server",
    "mcp.server.fastmcp",
    "mcp.server.stdio",
    "legal_workbench.desktop_launcher",
    "legal_workbench.agent_access",
    "legal_workbench.agent_access_cli",
    "legal_workbench.dossier_export",
    "legal_workbench.chart_export",
    "legal_workbench.mcp_server",
    "legal_workbench.worker",
    "legal_workbench.cli",
    "legal_workbench.doctor",
    "legal_workbench.backfill_progress",
    # 仓库内第一方独立包（法院自动装卷器）：可编辑安装时由 path 源引入，
    # 静态分析未必跟得上，显式登记每个子模块，避免冻结后运行期才 ImportError。
    "plainfolio_dossier",
    "plainfolio_dossier.builder",
    "plainfolio_dossier.models",
    "plainfolio_dossier.errors",
    "plainfolio_dossier.path_fence",
    "plainfolio_dossier.manifest",
    "plainfolio_dossier.cli",
    "plainfolio_dossier.mcp_server",
    # 零数据库离线阅卷解析内核：解析器、精确值、强指纹与表格工具都要逐个登记，
    # 这些模块大量使用惰性导入（按扩展名分派），静态分析跟不到。
    "plainfolio_parse",
    "plainfolio_parse.config",
    "plainfolio_parse.errors",
    "plainfolio_parse.textnorm",
    "plainfolio_parse.path_fence",
    "plainfolio_parse.exact_values",
    "plainfolio_parse.identifiers",
    "plainfolio_parse.tables",
    "plainfolio_parse.markdown_export",
    "plainfolio_parse.formats",
    "plainfolio_parse.formats.base",
    "plainfolio_parse.formats.text",
    "plainfolio_parse.formats.docx",
    "plainfolio_parse.formats.xlsx",
    "plainfolio_parse.formats.pdf",
    "plainfolio_parse.formats.images",
    "plainfolio_parse.formats.ocr",
    "plainfolio_parse.formats.dispatch",
    "plainfolio_parse.cli",
    "plainfolio_parse.mcp_server",
]

a = Analysis(  # noqa: F821 - injected by PyInstaller
    [str(ENTRY)],
    pathex=[str(SRC), *(str(path) for path in PACKAGES_SRC)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PyQt5", "PySide6", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PlainFolio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # MCP runs over stdio; a windowed exe would lose it
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="PlainFolio",
)
