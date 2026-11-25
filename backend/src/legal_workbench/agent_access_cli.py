"""The 承办人's own access-policy commands, shared by every entry point.

Two callers need these: ``scripts/lwb`` (a source checkout) and ``--run-cli`` in a
frozen desktop build, which has no ``scripts/`` directory at all.  Keeping one
implementation here is what makes the grant path work in both, and it is also
what stops the two from drifting on wording or exit codes.

Output is human text, not JSON: this is a terminal command run by the person who
owns the machine, and the whole point of ``agent-allow-all`` is that a human reads
the warning.  Exit codes follow the rest of the CLI (0 ok, 2 refused/bad input).
"""
from __future__ import annotations

from typing import Callable, Sequence
import sys
import uuid

from . import agent_access

COMMANDS: tuple[str, ...] = ("agent-status", "agent-grant", "agent-revoke", "agent-lock",
                             "agent-allow-all")

USAGE = (
    "用法:\n"
    "  scripts/lwb agent-status                     查看当前策略与已授权案件\n"
    "  scripts/lwb agent-grant <case_id>            临时放行单个案件（切到 allowlist）\n"
    "  scripts/lwb agent-revoke <case_id>           收回单个案件（白名单空则回到 deny_all）\n"
    "  scripts/lwb agent-lock                       立即全局锁死并清空白名单\n"
    "  scripts/lwb agent-allow-all --confirm        对所有案件开放（风险，仅限本地离线模型）"
)


def _case_id(raw: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw.strip())
    except (ValueError, TypeError, AttributeError):
        print(f"case_id 不是合法 UUID：{raw}", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return None


def status() -> int:
    policy = agent_access.load_access_policy()
    print(f"策略文件：{agent_access.policy_file_path()}")
    if policy.source == agent_access.SOURCE_DEFAULT_DENY:
        print("（策略文件不存在：按默认锁死处理）")
    for line in agent_access.describe_policy(policy):
        print(line)
    return 0


def grant(argv: Sequence[str]) -> int:
    if len(argv) != 1:
        print(USAGE, file=sys.stderr)
        return 2
    case_id = _case_id(argv[0])
    if case_id is None:
        return 2
    policy = agent_access.grant_case(case_id)
    print(f"已授权单个案件：{case_id}")
    print(f"当前模式：{policy.mode}，已授权案件数：{len(policy.allowed_case_ids)}")
    print("该授权仅对本机 Agent 生效，且只放行此案；任务结束后请执行 "
          f"scripts/lwb agent-revoke {case_id} 或 scripts/lwb agent-lock 收回权限。")
    return 0


def revoke(argv: Sequence[str]) -> int:
    if len(argv) != 1:
        print(USAGE, file=sys.stderr)
        return 2
    case_id = _case_id(argv[0])
    if case_id is None:
        return 2
    policy = agent_access.revoke_case(case_id)
    print(f"已收回案件授权：{case_id}")
    if policy.mode == agent_access.DENY_ALL:
        print("白名单已空：已自动退回默认锁死（deny_all）。")
    else:
        print(f"当前模式：{policy.mode}，剩余已授权案件数：{len(policy.allowed_case_ids)}")
    return 0


def lock(argv: Sequence[str]) -> int:
    if argv:
        print(USAGE, file=sys.stderr)
        return 2
    agent_access.lock_all()
    print("已全局锁死（deny_all）：白名单清空，Agent 无法列举、检索或读取任何案件。")
    return 0


def allow_all(argv: Sequence[str]) -> int:
    """Opening every case needs an explicit ``--confirm``, never a default."""
    if list(argv) != ["--confirm"]:
        print("拒绝执行：agent-allow-all 必须显式带 --confirm。", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2
    agent_access.allow_all_cases()
    print("=" * 68)
    print("警告：已对所有案件开放（allow_all）。")
    print("Agent 现在可以列举、检索并读取本机全部案件材料。")
    print("案卷片段会随会话进入所接入模型的上下文——涉密或当事人禁止上云的案卷，")
    print("只允许接入本机离线模型；接入云端模型前必须确认该案允许这样处理。")
    print("任务结束后请立即执行 scripts/lwb agent-lock 收回全部权限。")
    print("=" * 68)
    return 0


_HANDLERS: dict[str, Callable[[Sequence[str]], int]] = {
    "agent-status": lambda _argv: status(),
    "agent-grant": grant,
    "agent-revoke": revoke,
    "agent-lock": lock,
    "agent-allow-all": allow_all,
}


def run(command: str, argv: Sequence[str]) -> int:
    """Dispatch one agent command; unknown names are a usage error, not a crash."""
    handler = _HANDLERS.get(command)
    if handler is None:
        print(USAGE, file=sys.stderr)
        return 2
    return handler(list(argv))
