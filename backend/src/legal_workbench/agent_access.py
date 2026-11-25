"""Agent access policy: default-deny plus a per-case allowlist (hard fence).

The workbench is a single-user, offline machine, but an MCP client is *not* the
承办人: whatever model is on the other end of the stdio pipe can enumerate cases,
read excerpts and submit proposals.  A resident plugin therefore must not be able
to read the whole docket by default.  This module is the single decision point:

* **No policy file means ``deny_all``.**  A fresh checkout, a packaged install
  and a test machine all start closed; nothing is pre-generated.
* The policy file lives under the workbench home (``var/runtime/``), is written
  atomically with ``0600`` and holds **no credentials and no case data** — only
  a mode, a list of case UUIDs and a timestamp.
* A corrupt or hand-edited file degrades to ``deny_all`` instead of failing open.
* ``LWB_MCP_ALLOWED_CASES`` / ``LWB_MCP_ACCESS_MODE`` exist for a controlled
  single-case launch and for tests.  They can **widen the default** (no file at
  all) but can never widen an explicit file policy: when a file exists the
  stricter of file and environment wins, so ``scripts/lwb agent-lock`` cannot be
  undone by a stale variable in some shell.

Every MCP tool that takes a ``case_id`` calls :func:`require_case_allowed` before
it touches the database, and the error message names the exact command the
承办人 has to run on this machine.  Nothing here is exposed as an MCP tool: an
agent can read the policy (``get_access_policy``) but never change it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping
import uuid

from .config import _write_json_atomic, resolve_home_dir
from .errors import ErrorCode, WorkbenchError

POLICY_FILE_ENV = "LWB_AGENT_ACCESS_FILE"
ALLOWED_CASES_ENV = "LWB_MCP_ALLOWED_CASES"
ACCESS_MODE_ENV = "LWB_MCP_ACCESS_MODE"

DENY_ALL = "deny_all"
ALLOWLIST = "allowlist"
ALLOW_ALL = "allow_all"
MODES: tuple[str, ...] = (DENY_ALL, ALLOWLIST, ALLOW_ALL)

# Lower is stricter.  Two settings are combined by keeping the smaller value, so
# a lock always survives a looser override.
_STRICTNESS = {DENY_ALL: 0, ALLOWLIST: 1, ALLOW_ALL: 2}

DEFAULT_POLICY_PARTS = ("var", "runtime", "agent-access.json")
SOURCE_POLICY_FILE = "policy_file"
SOURCE_ENV = "env"
SOURCE_DEFAULT_DENY = "default_deny"
SOURCE_INVALID_CONFIG = "invalid_configuration"


@dataclass(frozen=True)
class AccessPolicy:
    """One resolved decision; ``source`` says where it came from."""

    mode: str
    allowed_case_ids: tuple[uuid.UUID, ...] = ()
    allow_create_case: bool = False
    updated_at: str = ""
    source: str = SOURCE_DEFAULT_DENY

    @property
    def is_locked(self) -> bool:
        return self.mode == DENY_ALL

    def allows(self, case_id: uuid.UUID) -> bool:
        if self.mode == ALLOW_ALL:
            return True
        if self.mode == ALLOWLIST:
            return case_id in self.allowed_case_ids
        return False


DEFAULT_POLICY = AccessPolicy(mode=DENY_ALL, source=SOURCE_DEFAULT_DENY)


# --- paths -----------------------------------------------------------------

def policy_file_path(path: Path | str | None = None) -> Path:
    """Where the policy lives: explicit argument → ``LWB_AGENT_ACCESS_FILE`` → home.

    Tests always set the variable (``conftest`` points it into ``tmp_path``), so a
    developer's real ``var/runtime/agent-access.json`` is never read or written.
    """
    if path is not None:
        return Path(path).expanduser()
    override = os.environ.get(POLICY_FILE_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return resolve_home_dir().joinpath(*DEFAULT_POLICY_PARTS)


def grant_command(case_id: uuid.UUID | str | None = None, *,
                  windows: bool | None = None) -> str:
    """The one command the 承办人 has to run, spelled for this platform."""
    is_windows = (os.name == "nt") if windows is None else windows
    script = "scripts\\lwb.cmd" if is_windows else "scripts/lwb"
    target = str(case_id) if case_id is not None else "<case_id>"
    return f"{script} agent-grant {target}"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --- reading ---------------------------------------------------------------

def _parse_case_ids(values: object) -> tuple[uuid.UUID, ...] | None:
    """Normalise a list of UUID strings; ``None`` means the input was unusable."""
    if not isinstance(values, list):
        return None
    parsed: list[uuid.UUID] = []
    for item in values:
        if not isinstance(item, str):
            return None
        try:
            parsed.append(uuid.UUID(item))
        except (ValueError, TypeError):
            return None
    # Stable order, no duplicates: the file is read by humans as often as by code.
    return tuple(sorted(set(parsed), key=str))


def _read_policy_file(target: Path) -> AccessPolicy | None:
    """Return the file policy, or ``None`` when absent or unusable.

    Anything unexpected (missing, truncated, wrong types, an unknown mode) is
    reported as "no usable file" so the caller falls back to ``deny_all``.  A
    policy file is the only thing that can widen access, so it must never be
    interpreted leniently.
    """
    try:
        raw = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    # Every documented key must be present: a file that only says
    # ``{"mode": "allow_all"}`` is a truncated or hand-written fragment, not a
    # decision, and must not be able to open the whole docket.
    if not {"mode", "allowed_case_ids", "allow_create_case", "updated_at"} <= set(data):
        return None
    mode = data.get("mode")
    if mode not in MODES:
        return None
    ids = _parse_case_ids(data.get("allowed_case_ids"))
    if ids is None:
        return None
    if not isinstance(data.get("allow_create_case"), bool):
        return None
    allow_create = data["allow_create_case"] is True
    if mode == ALLOWLIST and not ids:
        # An empty allowlist is the same decision as deny_all; normalising keeps
        # every message and every test on one shape.
        mode = DENY_ALL
    updated_at = data.get("updated_at")
    return AccessPolicy(
        mode=mode,
        allowed_case_ids=ids if mode != DENY_ALL else (),
        allow_create_case=allow_create and mode != DENY_ALL,
        updated_at=updated_at if isinstance(updated_at, str) else "",
        source=SOURCE_POLICY_FILE,
    )


def _parse_env_case_ids(text: str) -> tuple[uuid.UUID, ...]:
    """Parse ``LWB_MCP_ALLOWED_CASES``; a malformed token is a hard error.

    Silently dropping a typo would leave the operator believing one case is open
    while the list is empty (or, worse, open a case they did not mean to name).
    """
    tokens = [token.strip() for token in text.split(",") if token.strip()]
    if not tokens:
        return ()
    parsed: list[uuid.UUID] = []
    for token in tokens:
        try:
            parsed.append(uuid.UUID(token))
        except (ValueError, TypeError):
            raise WorkbenchError(
                ErrorCode.INVALID_INPUT,
                f"{ALLOWED_CASES_ENV} 含非法 UUID：{token}；应为本机授权的 case_id，多个用英文逗号分隔",
                {"env": ALLOWED_CASES_ENV, "value": token},
            ) from None
    return tuple(sorted(set(parsed), key=str))


def _env_policy(env: Mapping[str, str]) -> AccessPolicy | None:
    """Resolve the environment override, keeping the stricter of the two settings."""
    mode_raw = (env.get(ACCESS_MODE_ENV) or "").strip()
    cases_raw = (env.get(ALLOWED_CASES_ENV) or "").strip()
    candidate: AccessPolicy | None = None
    if mode_raw:
        if mode_raw not in MODES:
            raise WorkbenchError(
                ErrorCode.INVALID_INPUT,
                f"{ACCESS_MODE_ENV} 只支持 {' / '.join(MODES)}，收到：{mode_raw}",
                {"env": ACCESS_MODE_ENV, "value": mode_raw},
            )
        candidate = AccessPolicy(mode=mode_raw, source=SOURCE_ENV)
    if cases_raw:
        ids = _parse_env_case_ids(cases_raw)
        if ids:
            narrowed = AccessPolicy(mode=ALLOWLIST, allowed_case_ids=ids, source=SOURCE_ENV)
            if candidate is None or _STRICTNESS[narrowed.mode] < _STRICTNESS[candidate.mode]:
                candidate = narrowed
        elif candidate is None:
            # A variable that names no case is the same decision as a lock.
            candidate = AccessPolicy(mode=DENY_ALL, source=SOURCE_ENV)
    if candidate is not None and candidate.mode == ALLOWLIST and not candidate.allowed_case_ids:
        # ``LWB_MCP_ACCESS_MODE=allowlist`` without cases is a lock, not an empty
        # page: normalising here keeps one shape for every "nothing is open"
        # decision, so ``list_cases`` refuses instead of returning an empty list.
        candidate = AccessPolicy(mode=DENY_ALL, source=candidate.source)
    return candidate


def load_access_policy(path: Path | str | None = None, *,
                       env: Mapping[str, str] | None = None) -> AccessPolicy:
    """Resolve the effective policy: file ∩ environment, never wider than the file.

    * no file, no environment → ``deny_all`` (``source="default_deny"``);
    * environment only → it may open exactly what it names (``source="env"``);
    * file present → the stricter of file and environment wins, so a lock is not
      undone by a leftover variable, while an environment allowlist still narrows
      a broader file.
    """
    env_map = os.environ if env is None else env
    file_policy = _read_policy_file(policy_file_path(path))
    env_policy = _env_policy(env_map)
    if file_policy is None:
        return env_policy if env_policy is not None else DEFAULT_POLICY
    if env_policy is None:
        return file_policy
    if _STRICTNESS[env_policy.mode] < _STRICTNESS[file_policy.mode]:
        return env_policy
    return file_policy


# --- writing ---------------------------------------------------------------

def _payload(policy: AccessPolicy) -> dict[str, Any]:
    return {
        "mode": policy.mode,
        "allowed_case_ids": [str(item) for item in policy.allowed_case_ids],
        "allow_create_case": bool(policy.allow_create_case),
        "updated_at": _utc_stamp(),
    }


def save_access_policy(policy: AccessPolicy, path: Path | str | None = None) -> AccessPolicy:
    """Write ``policy`` atomically with ``0600``; returns what was actually stored."""
    if policy.mode not in MODES:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, f"不支持的访问模式：{policy.mode}")
    normalised = AccessPolicy(
        mode=DENY_ALL if (policy.mode == ALLOWLIST and not policy.allowed_case_ids) else policy.mode,
        allowed_case_ids=(tuple(sorted(set(policy.allowed_case_ids), key=str))
                          if policy.mode != DENY_ALL else ()),
        allow_create_case=bool(policy.allow_create_case) and policy.mode != DENY_ALL,
        source=SOURCE_POLICY_FILE,
    )
    target = policy_file_path(path)
    _write_json_atomic(target, _payload(normalised))
    return AccessPolicy(
        mode=normalised.mode,
        allowed_case_ids=normalised.allowed_case_ids,
        allow_create_case=normalised.allow_create_case,
        updated_at=_utc_stamp(),
        source=SOURCE_POLICY_FILE,
    )


def _current_file_policy(path: Path | str | None) -> AccessPolicy:
    """File state only (environment ignored): mutations must be predictable."""
    return _read_policy_file(policy_file_path(path)) or AccessPolicy(mode=DENY_ALL)


def parse_case_id(value: uuid.UUID | str) -> uuid.UUID:
    """Accept a UUID or its text form; anything else is an explicit error."""
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value).strip())
    except (ValueError, TypeError, AttributeError):
        raise WorkbenchError(ErrorCode.INVALID_INPUT, f"case_id 不是合法 UUID：{value}",
                             {"field": "case_id", "value": str(value)}) from None


def grant_case(case_id: uuid.UUID | str, path: Path | str | None = None) -> AccessPolicy:
    """Open exactly one case (``allowlist``); the 承办人 runs this on their machine.

    Granting from ``allow_all`` narrows the policy to this single case: that is
    the fail-safe direction and keeps one mental model ("grant means: this case,
    and only this case").
    """
    uid = parse_case_id(case_id)
    current = _current_file_policy(path)
    ids = tuple(sorted(set(current.allowed_case_ids) | {uid}, key=str))
    return save_access_policy(
        AccessPolicy(mode=ALLOWLIST, allowed_case_ids=ids,
                     allow_create_case=current.allow_create_case),
        path,
    )


def revoke_case(case_id: uuid.UUID | str, path: Path | str | None = None) -> AccessPolicy:
    """Close one case; an empty allowlist returns the whole workbench to ``deny_all``."""
    uid = parse_case_id(case_id)
    current = _current_file_policy(path)
    ids = tuple(item for item in current.allowed_case_ids if item != uid)
    if not ids:
        return lock_all(path)
    return save_access_policy(
        AccessPolicy(mode=ALLOWLIST, allowed_case_ids=ids,
                     allow_create_case=current.allow_create_case),
        path,
    )


def lock_all(path: Path | str | None = None) -> AccessPolicy:
    """Back to ``deny_all`` with an empty allowlist and no case creation."""
    return save_access_policy(AccessPolicy(mode=DENY_ALL), path)


def allow_all_cases(*, allow_create_case: bool = False,
                    path: Path | str | None = None) -> AccessPolicy:
    """Open every case — the explicit, warned-about escape hatch (local models only)."""
    return save_access_policy(
        AccessPolicy(mode=ALLOW_ALL, allow_create_case=allow_create_case), path)


# --- enforcement -----------------------------------------------------------

def _denied(case_id: uuid.UUID | None, policy: AccessPolicy, *, action: str) -> WorkbenchError:
    """Build the refusal. It never names a case the caller is not allowed to see."""
    details: dict[str, Any] = {
        "mode": policy.mode,
        "action": action,
        "remedy": grant_command(case_id),
    }
    if case_id is not None:
        details["case_id"] = str(case_id)
    reason = ("当前为默认锁死（deny_all）：Agent 未获任何案件授权"
              if policy.mode == DENY_ALL else
              "该案件不在承办人本机授权的单案白名单内")
    return WorkbenchError(
        ErrorCode.FORBIDDEN,
        f"禁止访问（forbidden）：{reason}。请由承办人在本机执行 "
        f"{grant_command(case_id)} 临时放行该案；任务结束后执行 "
        f"{_revoke_command(case_id)} 收回权限。未经授权不得改用数据库、"
        f".env 或改写 agent-access.json 绕过本门禁。",
        details,
    )


def _script_path() -> str:
    return "scripts\\lwb.cmd" if os.name == "nt" else "scripts/lwb"


def _revoke_command(case_id: uuid.UUID | None) -> str:
    return f"{_script_path()} agent-revoke {case_id if case_id is not None else '<case_id>'}"


def _lock_command() -> str:
    return f"{_script_path()} agent-lock"


def _resolved_for_gate() -> AccessPolicy:
    """Resolve the policy for a *data* call, failing closed on a bad config.

    A typo in the environment would otherwise surface as ``invalid_input`` from a
    tool that wants case data, which tells the model something about the
    machine's configuration.  A data tool has exactly two answers — allowed or
    ``forbidden`` — so a config error is reported as a lock; the read-only
    ``get_access_policy`` is where the operator gets the real explanation.
    """
    try:
        return load_access_policy()
    except WorkbenchError:
        return AccessPolicy(mode=DENY_ALL, source=SOURCE_INVALID_CONFIG)


def require_case_allowed(case_id: uuid.UUID | str) -> None:
    """Raise ``FORBIDDEN`` unless this case is authorised right now."""
    uid = parse_case_id(case_id)
    policy = _resolved_for_gate()
    if policy.allows(uid):
        return
    raise _denied(uid, policy, action="case_access")


def require_create_case_allowed() -> None:
    """Creating a case is off unless the operator explicitly turned it on."""
    policy = _resolved_for_gate()
    if policy.allow_create_case and policy.mode != DENY_ALL:
        return
    raise WorkbenchError(
        ErrorCode.FORBIDDEN,
        "禁止新建案件（forbidden）：Agent 默认无权在工作台新建案件。若只是需要某个已有案件，"
        f"请由承办人在本机执行 {grant_command()} 临时放行该案；确需由 Agent 建案时，"
        "还须承办人在本机显式开启建案权限（agent-allow-all --confirm 或在策略文件中置 "
        "allow_create_case=true），并确认当前接入的模型允许接触本案材料。",
        {"mode": policy.mode, "action": "create_case", "allow_create_case": False,
         "remedy": grant_command()},
    )


def is_case_allowed(case_id: uuid.UUID | str) -> bool:
    return _resolved_for_gate().allows(parse_case_id(case_id))


def filter_allowed_cases(case_ids: Iterable[uuid.UUID | str]) -> tuple[uuid.UUID, ...]:
    """Keep only authorised ids, preserving the caller's order and dropping repeats."""
    policy = _resolved_for_gate()
    kept: list[uuid.UUID] = []
    for value in case_ids:
        uid = parse_case_id(value)
        if policy.allows(uid) and uid not in kept:
            kept.append(uid)
    return tuple(kept)


def policy_summary(path: Path | str | None = None) -> dict[str, Any]:
    """Read-only self-check payload for the ``get_access_policy`` MCP tool.

    Deliberately contains no case ids, no titles and no counts of anything the
    caller is not authorised to see.

    A misconfigured environment (a typo in ``LWB_MCP_ALLOWED_CASES``) makes every
    other tool fail loudly — that is the right answer for a call that wants data,
    but it would also take away the one tool that explains *why*.  So this one
    reports the failure instead of raising, and still fails closed (``deny_all``).
    """
    try:
        policy = load_access_policy(path)
    except WorkbenchError as exc:
        return {
            "mode": DENY_ALL,
            "allowed_case_count": 0,
            "allow_create_case": False,
            "source": SOURCE_INVALID_CONFIG,
            "updated_at": "",
            "configuration_error": exc.message,
            "grant_command": grant_command(),
            "revoke_command": _revoke_command(None),
            "lock_command": _lock_command(),
            "notes": ["环境变量配置非法：门禁按默认锁死处理，修正后重试。"],
        }
    return {
        "mode": policy.mode,
        "allowed_case_count": len(policy.allowed_case_ids),
        "allow_create_case": bool(policy.allow_create_case),
        "source": policy.source,
        "updated_at": policy.updated_at,
        "grant_command": grant_command(),
        "revoke_command": _revoke_command(None),
        "lock_command": _lock_command(),
        "notes": [
            "默认锁死：未获承办人本机单案授权前，Agent 不得列举、检索或读取任何案件。",
            "授权只能由承办人在本机终端执行 agent-grant，MCP 不提供任何授权或解锁工具。",
            "涉密或当事人禁止上云的案卷，只能接入本地离线模型。",
        ],
    }


def describe_policy(policy: AccessPolicy) -> list[str]:
    """Human-readable lines for ``scripts/lwb agent-status``."""
    lines = [f"模式：{policy.mode}（来源：{policy.source}）"]
    if policy.mode == ALLOW_ALL:
        lines.append("风险提示：当前对所有案件开放（allow_all）。仅限本机离线模型，"
                     "涉密案卷不得接入云端模型。")
    elif policy.mode == DENY_ALL:
        lines.append("已全局锁死：Agent 无法列举、检索或读取任何案件。")
    else:
        lines.append(f"已授权案件数：{len(policy.allowed_case_ids)}")
        for item in policy.allowed_case_ids:
            lines.append(f"  - {item}")
        lines.append("提示：任务结束后执行 agent-revoke <case_id> 或 agent-lock 收回权限。")
    lines.append("Agent 建案权限：" + ("已开启（风险）" if policy.allow_create_case else "关闭"))
    if policy.updated_at:
        lines.append(f"最近更新：{policy.updated_at}")
    return lines


__all__ = [
    "ACCESS_MODE_ENV", "ALLOWED_CASES_ENV", "ALLOWLIST", "ALLOW_ALL", "AccessPolicy",
    "DEFAULT_POLICY", "DENY_ALL", "MODES", "POLICY_FILE_ENV", "allow_all_cases",
    "describe_policy", "filter_allowed_cases", "grant_case", "grant_command",
    "SOURCE_INVALID_CONFIG", "is_case_allowed", "load_access_policy", "lock_all",
    "parse_case_id", "policy_file_path", "policy_summary", "require_case_allowed",
    "require_create_case_allowed", "revoke_case", "save_access_policy",
]
