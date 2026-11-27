"""Optional module registry: one place deciding which feature areas a build serves.

The workbench ships as one local process, but a 承办人 may only ever use a subset
(材料与检索 vs. 关系图 vs. 诉请金额).  Modules are a *surface* switch, not a
security boundary: the access fence (``agent_access``) still decides per case.

Naming is stable on purpose — the names appear in ``LWB_ENABLED_MODULES``,
``LWB_MCP_MODULES``, ``--modules``, ``GET /api/v1/modules`` and the plugin docs.
"""
from __future__ import annotations

from collections.abc import Iterable

CORE_MODULE = "core"
OPTIONAL_MODULES = ("notes", "evidence", "graph", "claims", "progress", "time")
ALL_MODULES = (CORE_MODULE, *OPTIONAL_MODULES)


def parse_enabled_modules(raw: str | None) -> tuple[str, ...]:
    """Parse a comma-separated module list into a canonical, ordered tuple.

    ``None``, an empty string, or ``all`` (any case, surrounding blanks ignored)
    means every module.  ``core`` is always included and always first: materials,
    search and the access policy are what every other surface builds on, and a
    build without them is not a workbench.  Unknown names are rejected rather
    than silently dropped — a typo in ``.env`` must not look like a working
    trimmed build.
    """
    text = ("" if raw is None else raw).strip()
    if not text or text.lower() == "all":
        return ALL_MODULES
    wanted = {item.strip().lower() for item in text.split(",") if item.strip()}
    if not wanted:
        return ALL_MODULES
    unknown = sorted(wanted - set(ALL_MODULES))
    if unknown:
        name = unknown[0]
        try:
            # 延迟导入 errors（依赖 pydantic）：scripts/lwb 在系统裸 python3 下会先导入 config → modules。
            from .errors import ErrorCode, WorkbenchError
        except ModuleNotFoundError as exc:
            raise ValueError(f"未知模块：{name}") from exc
        raise WorkbenchError(ErrorCode.INVALID_INPUT, f"未知模块：{name}", {"module": name})
    wanted.add(CORE_MODULE)
    return tuple(name for name in ALL_MODULES if name in wanted)


def is_module_enabled(module: str, enabled: Iterable[str] | None = None) -> bool:
    """True when ``module`` is served; without ``enabled`` the settings decide."""
    if enabled is None:
        from .config import load_settings  # local import: config imports this module

        names: Iterable[str] = load_settings().enabled_modules
    else:
        names = enabled
    return any(name == module for name in names)
