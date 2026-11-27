"""检索归一化与回指原始文本的偏移映射（零依赖，与工作台 ``legal_workbench.textnorm`` 一致）。

规则：全角 ASCII → 半角；换行变体 → ``\\n``；任意空白串 → 一个空格；标点保留
（``HT-001`` 这类编号不被拆开）。
"""
from __future__ import annotations

from typing import Any

FULLWIDTH_START = 0xFF01
FULLWIDTH_END = 0xFF5E
FULLWIDTH_OFFSET = 0xFEE0


def _normalize_char(ch: str) -> str:
    code = ord(ch)
    if FULLWIDTH_START <= code <= FULLWIDTH_END:
        return chr(code - FULLWIDTH_OFFSET)
    if ch == "\u3000":
        return " "
    if ch in "\r\n\t\f\v":
        return " "
    if ch.isspace():
        return " "
    return ch


def normalize_with_map(text: str) -> tuple[str, list[list[int]]]:
    """Return normalized text and [[norm_start, original_start, length], ...] runs."""
    out: list[str] = []
    mapping: list[int] = []  # normalized index -> original index
    last_space = False
    for index, ch in enumerate(text):
        if ch == "\r" and index + 1 < len(text) and text[index + 1] == "\n":
            continue  # the \n handles the pair
        normalized = _normalize_char(ch)
        if normalized == " ":
            if last_space:
                continue
            last_space = True
        else:
            last_space = False
        out.append(normalized)
        mapping.append(index)
    runs: list[list[int]] = []
    for norm_index, original_index in enumerate(mapping):
        if runs and runs[-1][1] + runs[-1][2] == original_index:
            runs[-1][2] += 1
        else:
            runs.append([norm_index, original_index, 1])
    return "".join(out), runs


def original_index(runs: list[Any], norm_index: int) -> int | None:
    """Map a normalized character index back to the original text index."""
    for start, original, length in runs:
        if start <= norm_index < start + length:
            return original + (norm_index - start)
    return None
