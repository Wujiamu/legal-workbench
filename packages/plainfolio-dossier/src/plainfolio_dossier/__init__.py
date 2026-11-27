"""素章（PlainFolio）法院自动装卷器 —— 零数据库独立包。

公开入口只有四个概念：

* :func:`build_dossier`：把 :class:`DossierSpec` 渲染成合规卷宗 ZIP；
* :func:`init_manifest` / :func:`load_manifest` / :func:`validate_manifest`：
  清单的生成、加载与只读预检；
* :mod:`plainfolio_dossier.path_fence`：方案 A 文件访问围栏（默认只允许
  当前工作目录，``PLAINFOLIO_ALLOWED_ROOTS`` 显式放行）。

本包不导入 sqlalchemy / psycopg / fastapi，也不读写任何数据库。
"""
from __future__ import annotations

from .builder import build_dossier
from .errors import DossierError
from .manifest import init_manifest, load_manifest, validate_manifest
from .models import (
    BindingSpec,
    CaseMeta,
    DossierResult,
    DossierSpec,
    GroupSpec,
    ItemSpec,
)
from .path_fence import (
    PLAINFOLIO_ALLOWED_ROOTS_ENV,
    allowed_roots,
    resolve_readable_dir,
    resolve_readable_file,
    resolve_writable_output,
)

__version__ = "0.1.0"

__all__ = [
    "BindingSpec",
    "CaseMeta",
    "DossierError",
    "DossierResult",
    "DossierSpec",
    "GroupSpec",
    "ItemSpec",
    "PLAINFOLIO_ALLOWED_ROOTS_ENV",
    "__version__",
    "allowed_roots",
    "build_dossier",
    "init_manifest",
    "load_manifest",
    "resolve_readable_dir",
    "resolve_readable_file",
    "resolve_writable_output",
    "validate_manifest",
]
