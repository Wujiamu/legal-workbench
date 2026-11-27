"""卷宗清单（manifest）的生成、加载与预检。

一份清单就是一份 :class:`~plainfolio_dossier.models.DossierSpec`：

* **JSON**：直接对应 ``DossierSpec``，字段与 MCP/CLI 输出一致；
* **CSV**：给习惯用 Excel 的承办人，列为 ``group,title,purpose,file_path,
  page_start,page_end,acquisition``，同一 ``(group,title)`` 的多行表示同一证据
  条目绑定多份材料。

相对 ``file_path`` 一律相对清单文件所在目录解析，并统一过
:mod:`plainfolio_dossier.path_fence` 的方案 A 围栏；越权或指向链接的路径在读
清单时就报错，不会留到装卷过程中才发现。
"""
from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from pypdf import PdfReader

from . import path_fence
from .errors import DossierError
from .models import BindingSpec, CaseMeta, DossierSpec, GroupSpec, ItemSpec

CSV_COLUMNS = ("group", "title", "purpose", "file_path", "page_start", "page_end", "acquisition")
ROOT_LABEL = "根目录材料"
_HIDDEN_PREFIX = "."


def _atomic_write(path: Path, payload: bytes) -> Path:
    """Write in the same directory then rename, so a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return path


def atomic_write_bytes(path: str | Path, payload: bytes) -> Path:
    """Public atomic writer used by the CLI for the dossier ZIP."""
    return _atomic_write(Path(path), payload)


def _scan(source: Path) -> list[Path]:
    """Read-only recursive scan: hidden files/dirs skipped, stable name order."""
    found: list[Path] = []
    for current, dir_names, file_names in os.walk(source):
        dir_names[:] = sorted(name for name in dir_names if not name.startswith(_HIDDEN_PREFIX))
        for name in sorted(file_names):
            if name.startswith(_HIDDEN_PREFIX):
                continue
            found.append(Path(current) / name)
    return found


def _manifest_relative(path: Path, anchor: Path | None) -> str:
    if anchor is None:
        return str(path)
    try:
        relative = os.path.relpath(path, anchor)
    except ValueError:  # different drives on Windows: keep the absolute path
        return str(path)
    return Path(relative).as_posix()


def init_manifest(
    source_dir: str | Path,
    output_path: str | Path | None = None,
    *,
    case_title: str = "未命名案件",
    case_number: str | None = None,
    plaintiff: str | None = None,
    defendant: str | None = None,
    roots: tuple[Path, ...] | None = None,
) -> dict[str, Any]:
    """Scan a material directory into a draft ``DossierSpec`` awaiting lawyer review."""
    fenced_roots = tuple(roots or path_fence.allowed_roots())
    source = path_fence.resolve_readable_dir(source_dir, fenced_roots)
    files = _scan(source)
    if not files:
        raise DossierError(
            "invalid_input",
            f"扫描目录中没有可用的材料文件：{source}",
            {"source_dir": str(source)},
        )

    output: Path | None = None
    anchor: Path | None = None
    if output_path is not None:
        output = path_fence.resolve_writable_output(
            output_path, fenced_roots, forbidden_inputs=files,
        )
        anchor = output.parent

    grouped: dict[str, list[dict[str, Any]]] = {}
    for path in files:
        try:
            relative = path.relative_to(source)
        except ValueError:  # pragma: no cover - _scan only yields children of source
            relative = Path(path.name)
        group_title = ROOT_LABEL if str(relative.parent) in ("", ".") else relative.parent.as_posix()
        grouped.setdefault(group_title, []).append({
            "title": path.stem,
            "purpose": None,
            "bindings": [{
                "file_path": _manifest_relative(path, anchor),
                "material_name": path.name,
                "page_start": None,
                "page_end": None,
                "acquisition": None,
                "ext": path.suffix.lower() or None,
            }],
        })

    draft: dict[str, Any] = {
        "case": {
            "title": case_title,
            "case_number": case_number,
            "plaintiff": plaintiff,
            "defendant": defendant,
        },
        "groups": [
            {"title": title, "items": items} for title, items in grouped.items()
        ],
    }

    if output is not None:
        text = json.dumps(draft, ensure_ascii=False, indent=2) + "\n"
        _atomic_write(output, text.encode("utf-8"))
    return draft


def _decode_csv(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise DossierError("invalid_input", "清单 CSV 无法按 UTF-8 / GB18030 解码")


def _int_or_none(value: str, *, column: str, row_no: int) -> int | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise DossierError(
            "invalid_input",
            f"清单第 {row_no} 行的 {column} 不是整数：{text}",
            {"row": row_no, "column": column},
        ) from exc


def _spec_from_csv(text: str) -> DossierSpec:
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = [(name or "").strip() for name in (reader.fieldnames or [])]
    missing = [name for name in ("group", "title", "file_path") if name not in fieldnames]
    if missing:
        raise DossierError(
            "invalid_input",
            f"清单 CSV 缺少必需列：{', '.join(missing)}",
            {"missing_columns": missing, "columns": fieldnames},
        )

    groups: list[GroupSpec] = []
    index: dict[str, GroupSpec] = {}
    problems: list[str] = []
    for row_no, raw_row in enumerate(reader, start=2):
        row = {(key or "").strip(): (value or "").strip() for key, value in raw_row.items()}
        group_title = row.get("group") or ""
        item_title = row.get("title") or ""
        file_path = row.get("file_path") or ""
        if not group_title or not item_title or not file_path:
            problems.append(f"第 {row_no} 行缺少 group/title/file_path")
            continue
        group = index.get(group_title)
        if group is None:
            group = GroupSpec(title=group_title, items=[])
            index[group_title] = group
            groups.append(group)
        item = next((entry for entry in group.items if entry.title == item_title), None)
        if item is None:
            item = ItemSpec(title=item_title, purpose=row.get("purpose") or None, bindings=[])
            group.items.append(item)
        item.bindings.append(BindingSpec(
            file_path=file_path,
            material_name=Path(file_path.replace("\\", "/")).name,
            page_start=_int_or_none(row.get("page_start", ""), column="page_start", row_no=row_no),
            page_end=_int_or_none(row.get("page_end", ""), column="page_end", row_no=row_no),
            acquisition=row.get("acquisition") or None,
            ext=Path(file_path.replace("\\", "/")).suffix.lower() or None,
        ))
    # 跳行等于"少了一份证据"，必须当场报错而不是静默装出缺页卷宗。
    if problems:
        raise DossierError(
            "invalid_input",
            "清单 CSV 存在不完整行：" + "；".join(problems[:5]),
            {"skipped_rows": problems},
        )
    if not groups:
        raise DossierError("invalid_input", "清单 CSV 中没有可用行（group/title/file_path 均为空）")
    return DossierSpec(case=CaseMeta(title="未命名案件"), groups=groups)



def _parse_spec(path: Path) -> DossierSpec:
    """Parse a JSON/CSV manifest into a spec **without** touching binding files.

    Splitting parse from resolution is what lets :func:`validate_manifest` report
    one issue per bad binding instead of aborting on the first missing file.
    """
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise DossierError(
                "invalid_input", f"清单 JSON 解析失败：{exc}", {"path": str(path)}
            ) from exc
        if isinstance(payload, dict) and "case" not in payload and "groups" in payload:
            payload = {"case": {"title": "未命名案件"}, **payload}
        try:
            return DossierSpec.model_validate(payload)
        except ValidationError as exc:
            raise DossierError(
                "invalid_input",
                f"清单 JSON 结构不符合装卷契约：{exc.error_count()} 处错误",
                {"path": str(path), "errors": exc.errors(include_url=False)[:5]},
            ) from exc
    if suffix == ".csv":
        return _spec_from_csv(_decode_csv(path.read_bytes()))
    raise DossierError(
        "invalid_input", f"不支持的清单格式（仅 JSON / CSV）：{path.name}", {"path": str(path)}
    )


def load_manifest(
    manifest_path: str | Path,
    *,
    roots: tuple[Path, ...] | None = None,
) -> tuple[DossierSpec, Path, list[Path]]:
    """Load a JSON/CSV manifest and fence-resolve every binding file.

    Returned ``DossierSpec`` has each ``file_path`` rewritten to the resolved
    absolute path, so a later :func:`~plainfolio_dossier.builder.build_dossier`
    call does not depend on the process working directory.
    """
    fenced_roots = tuple(roots or path_fence.allowed_roots())
    path = path_fence.resolve_readable_file(manifest_path, fenced_roots)
    spec = _parse_spec(path)
    base_dir = path.parent

    resolved_files: list[Path] = []
    for group in spec.groups:
        for item in group.items:
            for binding in item.bindings:
                resolved = path_fence.resolve_readable_file(
                    binding.file_path, fenced_roots, base_dir=base_dir,
                )
                binding.file_path = str(resolved)
                if resolved not in resolved_files:
                    resolved_files.append(resolved)
    return spec, path, resolved_files


def _pdf_page_count(path: Path) -> int | None:
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                if reader.decrypt("") == 0:
                    return None
            except Exception:
                return None
        return len(reader.pages)
    except Exception:
        return None


def _issue(level: str, code: str, message: str, **context: Any) -> dict[str, Any]:
    return {"level": level, "code": code, "message": message, **context}


def validate_manifest(
    manifest_path: str | Path,
    *,
    roots: tuple[Path, ...] | None = None,
) -> dict[str, Any]:
    """Read-only pre-flight check; never builds, never writes."""
    fenced_roots = tuple(roots or path_fence.allowed_roots())
    try:
        path = path_fence.resolve_readable_file(manifest_path, fenced_roots)
    except DossierError as exc:
        return {
            "valid": False,
            "manifest_path": str(manifest_path),
            "group_count": 0,
            "item_count": 0,
            "binding_count": 0,
            "issues": [_issue("error", "manifest_unavailable", exc.message)],
        }

    try:
        spec = _parse_spec(path)
    except DossierError as exc:
        return {
            "valid": False,
            "manifest_path": str(path),
            "group_count": 0,
            "item_count": 0,
            "binding_count": 0,
            "issues": [_issue("error", exc.code, exc.message, **exc.details)],
        }

    base_dir = path.parent
    issues: list[dict[str, Any]] = []
    group_count = len(spec.groups)
    item_count = 0
    binding_count = 0

    for group_index, group in enumerate(spec.groups, start=1):
        if not group.items:
            issues.append(_issue(
                "warning", "empty_group",
                f"第 {group_index} 组「{group.title}」没有任何证据条目，装卷时会被跳过",
                group=group.title,
            ))
            continue
        for item_index, item in enumerate(group.items, start=1):
            item_count += 1
            label = f"证据{group_index}-{item_index}"
            if not item.bindings:
                issues.append(_issue(
                    "warning", "unbound_item",
                    f"{label}「{item.title}」未绑定任何材料，合订本将插入占位提示页",
                    group=group.title, item=item.title, item_code=label,
                ))
                continue
            for binding in item.bindings:
                binding_count += 1
                try:
                    resolved = path_fence.resolve_readable_file(
                        binding.file_path, fenced_roots, base_dir=base_dir,
                    )
                except DossierError as exc:
                    code = {"not_found": "file_not_found",
                            "forbidden": "file_forbidden"}.get(exc.code, "file_invalid")
                    issues.append(_issue(
                        "error", code,
                        f"{label}「{item.title}」的材料「{binding.material_name}」不可用：{exc.message}",
                        group=group.title, item=item.title, item_code=label,
                        file_path=str(binding.file_path), **exc.details,
                    ))
                    continue

                issues.extend(_page_issues(
                    resolved, binding, label=label, group=group.title, item=item.title,
                ))
                if binding.size is not None:
                    actual = resolved.stat().st_size
                    if actual != binding.size:
                        issues.append(_issue(
                            "warning", "size_mismatch",
                            f"{label} 的材料「{binding.material_name}」实际 {actual} 字节，与清单登记的 {binding.size} 字节不一致",
                            group=group.title, item=item.title, item_code=label,
                            file_path=str(resolved),
                        ))

    valid = not any(issue["level"] == "error" for issue in issues)
    return {
        "valid": valid,
        "manifest_path": str(path),
        "group_count": group_count,
        "item_count": item_count,
        "binding_count": binding_count,
        "issues": issues,
    }


def _page_issues(path: Path, binding: BindingSpec, *, label: str, group: str,
                 item: str) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    start = binding.page_start
    end = binding.page_end
    if start is not None and start < 1:
        issues.append(_issue(
            "error", "page_invalid",
            f"{label} 的起始页必须 ≥ 1（当前 {start}）",
            group=group, item=item, item_code=label, file_path=str(path),
        ))
        return issues
    if end is not None and end < 1:
        issues.append(_issue(
            "error", "page_invalid",
            f"{label} 的结束页必须 ≥ 1（当前 {end}）",
            group=group, item=item, item_code=label, file_path=str(path),
        ))
        return issues
    if end is not None and start is None:
        issues.append(_issue(
            "error", "page_range_incomplete",
            f"{label} 只填了结束页 {end}，缺起始页",
            group=group, item=item, item_code=label, file_path=str(path),
        ))
        return issues
    if start is not None and end is not None and end < start:
        issues.append(_issue(
            "error", "page_range_inverted",
            f"{label} 的页码范围倒挂：第 {start} 页 > 第 {end} 页",
            group=group, item=item, item_code=label, file_path=str(path),
        ))
        return issues

    ext = (binding.ext or path.suffix.lower())
    if ext in (".pdf",) and start is not None:
        total = _pdf_page_count(path)
        if total is None:
            issues.append(_issue(
                "warning", "pdf_unreadable",
                f"{label} 的材料「{binding.material_name}」无法解析页数（加密或损坏）",
                group=group, item=item, item_code=label, file_path=str(path),
            ))
            return issues
        last = end if end is not None else start
        if last > total:
            issues.append(_issue(
                "warning", "page_range_exceeds_pdf",
                f"{label} 绑定页码第 {start}-{last} 页超出原件「{binding.material_name}」总页数（{total} 页），装卷时会截断或插入提示页",
                group=group, item=item, item_code=label, file_path=str(path),
                pdf_pages=total,
            ))
    return issues
