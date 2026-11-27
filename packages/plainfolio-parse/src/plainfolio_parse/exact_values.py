"""精确值提取：日期、分币种金额（含表格单元格配对）与编号。

从工作台 ``legal_workbench/search.py`` 完整迁出的纯函数版本——原来这些逻辑直接
构造 ORM 行并 ``session.add``，无法在零数据库场景复用。核对规则逐条保留：

* 金额必须带**币种前缀或后缀**（``USD`` / ``CNY`` / ``美元`` / ``元`` …）才算数，
  裸数字不提取，避免把页码、案号、条款号当金额；
* 同一段文本里先日期、再金额、后编号，先命中者占位，后命中不得重叠；
* 表格单元格：一个币种单元格只与同行**至多一个**相邻数字单元格配对，优先带小数
  或千分位的那一个，4 位数且落在 1900–2100 的按年份降权（否则日期会被当金额）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

_AMOUNT_PREFIX = {"USD": "USD", "美元": "USD", "CNY": "CNY", "RMB": "CNY", "人民币": "CNY"}
_AMOUNT_RE = re.compile(
    r"(?<![\dA-Za-z])"
    r"(?:(USD|CNY|RMB|人民币|美元)\s*)?"
    r"(\d[\d,]*(?:\.\d{1,6})?)"
    r"\s*(万元|万|元|美元)?"
    r"(?![\d])"
)
_DATE_RE = re.compile(r"(?<!\d)(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?(?!\d)")
_CELL_LINE_RE = re.compile(r"^([A-Z]{1,3})(\d+):[ \t]*(.+?)[ \t]*$", re.MULTILINE)
_TABULAR_NUM_RE = re.compile(r"^(?:.*=>[ \t]*)?(\d[\d,]*(?:\.\d{1,6})?)$")
_TABULAR_CURRENCIES = {
    "CNY": "CNY", "RMB": "CNY", "人民币": "CNY",
    "USD": "USD", "美元": "USD", "美金": "USD",
    "EUR": "EUR", "欧元": "EUR",
    "HKD": "HKD", "港币": "HKD", "港元": "HKD",
    "JPY": "JPY", "日元": "JPY",
    "GBP": "GBP", "英镑": "GBP",
}
_IDENTIFIER_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:[A-Za-z]+[-_/]?\d+[A-Za-z0-9\-_/]*|\d{6,})(?![A-Za-z0-9_-])"
)

IDENTIFIER_KIND = "identifier"
AMOUNT_KIND = "amount"
DATE_KIND = "date"


@dataclass
class ExactValueHit:
    """一个原始文本跨度的提取结果（偏移是原文下标，不是归一化文本下标）。"""

    kind: str
    raw_text: str
    start: int
    end: int
    canonical: dict[str, Any] = field(default_factory=dict)
    amount: Decimal | None = None
    currency: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "raw_text": self.raw_text,
            "span": {"start": self.start, "end": self.end},
            "canonical": self.canonical,
            "amount": str(self.amount) if self.amount is not None else None,
            "currency": self.currency,
        }


def _overlaps(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(start < existing_end and end > existing_start
               for existing_start, existing_end in spans)


def extract_exact_values(text: str) -> list[ExactValueHit]:
    """Extract dates, currency amounts and identifiers with original-text spans."""
    spans: list[tuple[int, int]] = []
    values: list[ExactValueHit] = []

    def add(kind: str, raw: str, start: int, end: int, canonical: dict,
            amount: Decimal | None = None, currency: str | None = None) -> None:
        values.append(ExactValueHit(kind=kind, raw_text=raw, start=start, end=end,
                                    canonical=canonical, amount=amount, currency=currency))
        spans.append((start, end))

    for match in _DATE_RE.finditer(text):
        try:
            iso = date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
        except ValueError:
            continue
        if not _overlaps(spans, match.start(), match.end()):
            add(DATE_KIND, match.group(0), match.start(), match.end(), {"date": iso})

    for match in _AMOUNT_RE.finditer(text):
        prefix, number, unit = match.group(1), match.group(2), match.group(3)
        if not prefix and not unit:
            continue
        try:
            amount = Decimal(number.replace(",", ""))
        except InvalidOperation:
            continue
        if unit in ("万", "万元"):
            amount *= Decimal(10000)
        currency = _AMOUNT_PREFIX.get(prefix or "")
        if currency is None and (unit in ("元", "万", "万元")):
            currency = "CNY"
        if unit == "美元" or prefix == "美元":
            currency = "USD"
        if currency is None:
            continue
        if _overlaps(spans, match.start(), match.end()):
            continue
        add(AMOUNT_KIND, match.group(0).strip(), match.start(), match.end(),
            {"value": str(amount), "currency": currency}, amount=amount, currency=currency)

    extract_tabular_cell_amounts(text, spans, add)

    for match in _IDENTIFIER_RE.finditer(text):
        if _overlaps(spans, match.start(), match.end()):
            continue
        add(IDENTIFIER_KIND, match.group(0), match.start(), match.end(),
            {"value": match.group(0)})

    return values


def parse_tabular_currency(cell_val: str) -> str | None:
    """``'=C2 => CNY'`` / ``'人民币'`` → 币种代码；不是币种单元格则为 ``None``。"""
    text = cell_val.strip()
    if "=>" in text:
        text = text.rsplit("=>", 1)[-1].strip()
    return _TABULAR_CURRENCIES.get(text.upper()) or _TABULAR_CURRENCIES.get(text)


def extract_tabular_cell_amounts(
    text: str, spans: list[tuple[int, int]],
    add: Callable[..., None],
) -> None:
    """Pair each currency spreadsheet cell with at most one adjacent numeric cell on the same row."""
    cells = list(_CELL_LINE_RE.finditer(text))
    if len(cells) < 2:
        return
    used_cell_indices: set[int] = set()
    for c_idx, cell_match in enumerate(cells):
        currency = parse_tabular_currency(cell_match.group(3))
        if currency is None:
            continue
        row_id = cell_match.group(2)
        candidates: list[tuple[tuple[int, int, int], int, re.Match[str], re.Match[str]]] = []
        # Check left (amount | currency) first, then right (currency | amount), preferring decimal/comma format.
        for side_rank, n_idx in enumerate((c_idx - 1, c_idx + 1)):
            if not (0 <= n_idx < len(cells)) or n_idx in used_cell_indices:
                continue
            neighbor = cells[n_idx]
            if neighbor.group(2) != row_id:
                continue
            num_match = _TABULAR_NUM_RE.fullmatch(neighbor.group(3))
            if num_match is None:
                continue
            raw_num = num_match.group(1)
            num_start = neighbor.start(3) + num_match.start(1)
            num_end = neighbor.start(3) + num_match.end(1)
            if _overlaps(spans, num_start, num_end):
                continue
            format_score = 0 if ("." in raw_num or "," in raw_num) else 1
            year_penalty = 1 if (format_score == 1 and len(raw_num) == 4 and 1900 <= int(raw_num) <= 2100) else 0
            candidates.append(((format_score, year_penalty, side_rank), n_idx, neighbor, num_match))
        if not candidates:
            continue
        candidates.sort(key=lambda item: item[0])
        _, chosen_idx, chosen_cell, chosen_num = candidates[0]
        raw_num = chosen_num.group(1)
        num_start = chosen_cell.start(3) + chosen_num.start(1)
        num_end = chosen_cell.start(3) + chosen_num.end(1)
        try:
            amount = Decimal(raw_num.replace(",", ""))
        except InvalidOperation:
            continue
        used_cell_indices.add(chosen_idx)
        add(AMOUNT_KIND, raw_num, num_start, num_end,
            {"value": str(amount), "currency": currency}, amount=amount, currency=currency)


def group_amounts(hits: list[ExactValueHit]) -> dict[str, dict[str, Any]]:
    """按币种汇总金额（**绝不跨币种相加**）。"""
    totals: dict[str, dict[str, Any]] = {}
    for hit in hits:
        if hit.kind != AMOUNT_KIND or hit.amount is None or hit.currency is None:
            continue
        bucket = totals.setdefault(hit.currency, {"currency": hit.currency,
                                                "total": Decimal(0), "count": 0})
        bucket["total"] += hit.amount
        bucket["count"] += 1
    return {
        currency: {"currency": currency, "total": str(bucket["total"]),
                   "count": bucket["count"]}
        for currency, bucket in sorted(totals.items())
    }


def group_dates(hits: list[ExactValueHit]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for hit in hits:
        if hit.kind != DATE_KIND:
            continue
        iso = str(hit.canonical.get("date", ""))
        if iso:
            counts[iso] = counts.get(iso, 0) + 1
    return dict(sorted(counts.items()))
