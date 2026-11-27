"""方案 A 文件访问围栏（与 ``plainfolio-dossier`` 同规格）。

零数据库场景下唯一可依据的授权事实是"用户交给了哪些目录"：

1. 默认只允许当前工作目录（CWD）及其子目录；``PLAINFOLIO_ALLOWED_ROOTS``
   （``os.pathsep`` 分隔）可显式覆盖白名单，未列出的一律拒绝。
2. 路径先做词法归一（消解 ``..``）再与白名单比对；从根目录到目标的任意一级
   符号链接或 NTFS Junction 都拒绝，不做"跟随链接但再校验"的宽松处理。
3. 原件只读：输入文件只以只读方式打开；写盘路径不得与任何输入源文件同路径
   （同一路径写入即是错误）。

所有失败都抛 :class:`~plainfolio_parse.errors.ParseError`，绝不用
``PermissionError``/``FileNotFoundError`` 等内置异常——上游要按固定码把它们
映射回各自的错误响应。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable

from .errors import ParseError

PLAINFOLIO_ALLOWED_ROOTS_ENV = "PLAINFOLIO_ALLOWED_ROOTS"
_DARWIN_SYSTEM_ALIAS_ROOTS = (Path("/tmp"), Path("/var"), Path("/etc"))


def is_link_or_junction(path: Path) -> bool:
    """True for a symlink, or for an NTFS junction/directory reparse point."""
    if path.is_symlink():
        return True
    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is None:  # Python < 3.12: POSIX 没有 junction 概念
        return False
    try:
        return bool(isjunction(path))
    except OSError:
        # 判断不了就当作链接：围栏失败必须关闭，不能失败开放。
        return True


def _canonical(path: Path) -> Path:
    """Fully resolved path (``realpath``), tolerating a nonexistent tail."""
    return Path(os.path.realpath(os.path.expanduser(str(path))))


def _lexical(path: Path) -> Path:
    """Absolute, lexically normalized path that keeps symlink components intact."""
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _match_root(canonical: Path, roots: tuple[Path, ...]) -> Path | None:
    for root in roots:
        try:
            canonical.relative_to(root)
        except ValueError:
            continue
        return root
    return None


def _link_below(lexical: Path, root: Path) -> Path | None:
    """First symlink/junction on the literal path ``root -> lexical`` (root excluded)."""
    try:
        relative = lexical.relative_to(root)
    except ValueError:
        return None
    node = root
    for part in relative.parts:
        node = node / part
        if is_link_or_junction(node):
            return node
    return None


def _allowed_root_link(lexical: Path) -> Path | None:
    """First symlink/junction along ``lexical`` (allowing macOS /tmp, /var, /etc aliases)."""
    start = Path(lexical.anchor)
    if sys.platform == "darwin":
        for alias in _DARWIN_SYSTEM_ALIAS_ROOTS:
            try:
                lexical.relative_to(alias)
            except ValueError:
                continue
            if lexical == alias:
                return None
            start = alias
            break
    return _link_below(lexical, start)


def allowed_roots(cwd: Path | None = None) -> tuple[Path, ...]:
    """Whitelisted roots, from ``PLAINFOLIO_ALLOWED_ROOTS`` or the working directory."""
    base = _canonical(cwd if cwd is not None else Path.cwd())
    raw = os.environ.get(PLAINFOLIO_ALLOWED_ROOTS_ENV) or ""
    if not raw.strip():
        return (base,)

    roots: list[Path] = []
    for item in raw.split(os.pathsep):
        text = item.strip()
        if not text:
            continue
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = base / candidate
        lexical = _lexical(candidate)
        if not lexical.exists():
            raise ParseError(
                "invalid_input",
                f"{PLAINFOLIO_ALLOWED_ROOTS_ENV} 中的目录不存在：{text}",
                {"root": text},
            )
        if not lexical.is_dir():
            raise ParseError(
                "invalid_input",
                f"{PLAINFOLIO_ALLOWED_ROOTS_ENV} 中的路径不是目录：{text}",
                {"root": text},
            )
        link = _allowed_root_link(lexical)
        if link is not None:
            raise ParseError(
                "invalid_input",
                f"{PLAINFOLIO_ALLOWED_ROOTS_ENV} 中的目录包含符号链接或联接点：{text}",
                {"root": text, "link": str(link)},
            )
        roots.append(_canonical(lexical))

    if not roots:  # 环境变量只有分隔符，等同于未设置
        return (base,)
    return tuple(dict.fromkeys(roots))


def _roots_or_default(roots: tuple[Path, ...] | None) -> tuple[Path, ...]:
    return allowed_roots() if roots is None else tuple(roots)


def _locate(
    raw: str | Path,
    roots: tuple[Path, ...],
    *,
    base_dir: Path | None = None,
) -> tuple[Path, Path, Path, str]:
    """Resolve ``raw`` lexically and canonically, enforcing the root fence.

    Returns ``(lexical, canonical, matched_root, text)``; raises on anything that
    is not inside a whitelisted root.
    """
    text = str(raw).strip()
    if not text:
        raise ParseError("invalid_input", "路径不能为空")
    base = _canonical(base_dir if base_dir is not None else Path.cwd())
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    lexical = _lexical(candidate)

    for root in roots:
        link = _link_below(lexical, root)
        if link is not None:
            raise ParseError(
                "forbidden",
                f"路径经由符号链接或联接点，已拒绝访问：{text}",
                {"path": str(lexical), "link": str(link)},
            )

    canonical = _canonical(lexical)
    root = _match_root(canonical, roots)
    if root is None:
        raise ParseError(
            "forbidden",
            f"路径不在允许的根目录内：{text}",
            {"path": str(lexical), "allowed_roots": [str(item) for item in roots]},
        )
    cur = lexical
    while True:
        if cur == root or (
            sys.platform == "darwin"
            and cur in _DARWIN_SYSTEM_ALIAS_ROOTS
            and _canonical(cur) == root
        ):
            break
        if is_link_or_junction(cur):
            raise ParseError(
                "forbidden",
                f"路径经由符号链接或联接点，已拒绝访问：{text}",
                {"path": str(lexical), "link": str(cur)},
            )
        if _canonical(cur) == root or cur.parent == cur:
            break
        cur = cur.parent
    return lexical, canonical, root, text


def resolve_readable_file(
    raw: str | Path,
    roots: tuple[Path, ...] | None = None,
    *,
    base_dir: Path | None = None,
) -> Path:
    """Fenced read path for one material file; must exist and be a regular file."""
    _lexical_path, canonical, _root, text = _locate(raw, _roots_or_default(roots),
                                                  base_dir=base_dir)
    if not canonical.exists():
        raise ParseError("not_found", f"文件不存在：{text}", {"path": str(canonical)})
    if canonical.is_dir():
        raise ParseError(
            "invalid_input", f"路径是目录，不是文件：{text}", {"path": str(canonical)}
        )
    if not canonical.is_file():
        raise ParseError(
            "forbidden", f"不是普通文件（可能是设备或管道）：{text}", {"path": str(canonical)}
        )
    return canonical


def resolve_readable_dir(
    raw: str | Path,
    roots: tuple[Path, ...] | None = None,
    *,
    base_dir: Path | None = None,
) -> Path:
    """Fenced read path for one directory (folder scans)."""
    _lexical_path, canonical, _root, text = _locate(raw, _roots_or_default(roots),
                                                  base_dir=base_dir)
    if not canonical.exists():
        raise ParseError("not_found", f"目录不存在：{text}", {"path": str(canonical)})
    if not canonical.is_dir():
        raise ParseError(
            "invalid_input", f"路径不是目录：{text}", {"path": str(canonical)}
        )
    return canonical


def resolve_writable_output(
    raw: str | Path,
    roots: tuple[Path, ...] | None = None,
    *,
    forbidden_inputs: Iterable[Path] = (),
    base_dir: Path | None = None,
) -> Path:
    """Fenced write path; refuses links, out-of-root targets and overwriting inputs.

    ``forbidden_inputs`` 是原件只读红线：材料一旦参与解析就再也不能被同名输出
    覆盖，宁可报错让用户改文件名。
    """
    _lexical_path, canonical, _root, text = _locate(raw, _roots_or_default(roots),
                                                   base_dir=base_dir)

    ancestor = canonical.parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if is_link_or_junction(ancestor):
        raise ParseError(
            "forbidden",
            f"输出目录经由符号链接或联接点，已拒绝写入：{ancestor}",
            {"path": str(canonical), "link": str(ancestor)},
        )

    if canonical.exists() and canonical.is_dir():
        raise ParseError(
            "invalid_input", f"输出路径是目录，无法写入文件：{text}", {"path": str(canonical)}
        )

    for item in forbidden_inputs:
        if _same_location(canonical, item):
            raise ParseError(
                "forbidden",
                "输出路径不得覆盖输入源文件",
                {"output_path": str(canonical), "input_path": str(item)},
            )
    return canonical


def _same_location(left: Path, right: Path) -> bool:
    left_canonical = _canonical(left)
    right_canonical = _canonical(right)
    if left_canonical == right_canonical:
        return True
    try:
        return left.exists() and right.exists() and os.path.samefile(left, right)
    except OSError:
        return False
