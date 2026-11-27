"""表格读取与取值解析（纯零数据库）。

从工作台 ``legal_workbench/table_graph_import.py`` 迁出的底层工具：

* CSV 编码探测（UTF-8-SIG → UTF-8 → GB18030）与分隔符探测（`,` / TAB / `;`）；
* XLSX 工作表枚举与逐行取值（``datetime`` 归一为 ISO、布尔转 TRUE/FALSE、尾部空列剔除）；
* ``preview_table``：列名与前 N 行预览——非标对账单在映射前必须先看清表头；
* ``parse_amount_cents``（Decimal → 分，``ROUND_HALF_UP``）、``parse_share_pct``、``parse_date``。

错误消息与工作台逐字一致，薄适配层才能原样转回既有错误响应。
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from .errors import ParseError
from .textnorm import normalize_with_map

MAX_IMPORT_BYTES = 10 * 1024 * 1024
TABLE_SUFFIXES = frozenset({".xlsx", ".csv"})
_MAX_IMPORT_ROWS = 50_000

_CURRENCY_TOKEN_RE = re.compile(
    r"(?:(?:US|HK)\$|RMB|CNY|USD|EUR|HKD|JPY|GBP|人民币|美元|美金|欧元|港币|港元|日元|英镑|[¥￥$,€£元\s])",
    re.IGNORECASE,
)
_INLINE_CURRENCY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:HKD|港币|港元|HK\$)", re.IGNORECASE), "HKD"),
    (re.compile(r"(?:USD|美元|美金|US\$|\$)", re.IGNORECASE), "USD"),
    (re.compile(r"(?:CNY|RMB|人民币|[¥￥])", re.IGNORECASE), "CNY"),
    (re.compile(r"(?:EUR|欧元|€)", re.IGNORECASE), "EUR"),
    (re.compile(r"(?:JPY|日元)", re.IGNORECASE), "JPY"),
    (re.compile(r"(?:GBP|英镑|£)", re.IGNORECASE), "GBP"),
)
_CURRENCY_ALIASES = {
    "CNY": "CNY", "RMB": "CNY", "人民币": "CNY", "¥": "CNY", "￥": "CNY",
    "USD": "USD", "美元": "USD", "美金": "USD", "US$": "USD", "$": "USD",
    "EUR": "EUR", "欧元": "EUR", "€": "EUR",
    "HKD": "HKD", "港币": "HKD", "港元": "HKD", "HK$": "HKD",
    "JPY": "JPY", "日元": "JPY",
    "GBP": "GBP", "英镑": "GBP", "£": "GBP",
}
_CURRENCY_CODE_RE = re.compile(r"^[A-Za-z]{3}$")
_DECIMAL_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_DATE_SEP_RE = re.compile(r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?(?:[T\s].*)?$")
_DATE_COMPACT_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})(?:\s+\d{4,6})?$")

_ORG_CONTAINS = ("有限公司", "有限责任公司", "股份有限公司", "合伙企业", "律师事务所", "会计师事务所")
_ORG_SUFFIXES = (
    "有限公司", "股份有限公司", "有限责任公司", "公司", "集团", "银行", "支行", "分行",
    "信用社", "法院", "仲裁委员会", "检察院", "公安局", "事务所", "企业", "中心", "合伙",
    "合伙企业", "学校", "医院", "基金", "信托", "证券", "保险", "商行", "经营部", "工作室",
    "ltd", "ltd.", "limited", "inc", "inc.", "corp", "corp.", "co.", "llc", "llp", "lp", "bank", "group",
)
_TRAILING_PAREN_RE = re.compile(r"[（(][^（）()]*[)）]\s*$")


# --- 单元格与行 -----------------------------------------------------------


def cell_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        if value.hour == 0 and value.minute == 0 and value.second == 0 and value.microsecond == 0:
            return value.date().isoformat()
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value).strip()


def trim_row(cells: list[str]) -> list[str]:
    while cells and cells[-1] == "":
        cells.pop()
    return cells


def trim_trailing_empty_rows(rows: list[list[str]]) -> list[list[str]]:
    while rows and not rows[-1]:
        rows.pop()
    return rows


def decode_csv(data: bytes) -> str:
    if b"\x00" in data:
        raise ParseError("parse_failed", "CSV 文件包含空字节，无法按文本解析")
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ParseError("parse_failed", "无法以 UTF-8 或 GB18030 解码 CSV 文件")


def extract_sheet_rows(sheet: Any) -> list[list[str]]:
    rows: list[list[str]] = []
    for idx, row in enumerate(sheet.iter_rows(values_only=True)):
        if idx >= _MAX_IMPORT_ROWS:
            raise ParseError("invalid_input", f"表格行数超过 {_MAX_IMPORT_ROWS} 行上限，请拆分后导入")
        rows.append(trim_row([cell_str(value) for value in row]))
    return trim_trailing_empty_rows(rows)


def read_table_rows(data: bytes, suffix: str, sheet_name: str | None
                    ) -> tuple[list[str], str, list[list[str]]]:
    """Return ``(sheet_names, selected_sheet, rows)``; rows are already trimmed."""
    if len(data) > MAX_IMPORT_BYTES:
        raise ParseError("invalid_input", "表格文件超过 10 MB，请先裁剪或分拆", {"size": len(data)})
    suffix = suffix.lower()
    if suffix not in TABLE_SUFFIXES:
        raise ParseError("unsupported_format", "仅支持 .xlsx / .csv 表格导入关系图",
                         {"suffix": suffix})
    chosen = (sheet_name or "").strip() or None
    if suffix == ".csv":
        if chosen is not None and chosen != "CSV":
            raise ParseError("invalid_input", f"CSV 文件不存在工作表：{chosen}",
                             {"sheet_name": chosen})
        text = decode_csv(data)
        first_line = next((line for line in text.splitlines() if line.strip()), "")
        delimiter = ","
        if "," not in first_line:
            if "\t" in first_line:
                delimiter = "\t"
            elif ";" in first_line:
                delimiter = ";"
        try:
            reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
            rows = [trim_row([str(cell).strip() for cell in row]) for row in reader]
        except csv.Error as exc:
            raise ParseError("parse_failed", "无法解析 CSV 文件") from exc
        rows = trim_trailing_empty_rows(rows)
        if not any(rows):
            raise ParseError("unsupported_format", "CSV 中没有可读取的表格")
        return ["CSV"], "CSV", rows

    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise ParseError("parse_failed", "无法读取 XLSX 文件") from exc
    try:
        sheets = [sheet.title for sheet in workbook.worksheets]
        if not sheets:
            raise ParseError("unsupported_format", "XLSX 中没有工作表")
        if chosen is not None:
            if chosen not in workbook.sheetnames:
                raise ParseError("invalid_input", f"工作表不存在：{chosen}",
                                 {"sheet_name": chosen, "sheets": sheets})
            return sheets, chosen, extract_sheet_rows(workbook[chosen])
        for sheet in workbook.worksheets:
            rows = extract_sheet_rows(sheet)
            if any(rows):
                return sheets, sheet.title, rows
        raise ParseError("unsupported_format", "XLSX 中没有可读取的表格")
    finally:
        workbook.close()


def preview_table(path: Path, *, sheet_name: str | None = None, header_row: int = 1,
                  sample_rows: int = 20) -> dict[str, Any]:
    """列名与前 ``sample_rows`` 行预览；非标表格在映射前先看清表头。"""
    if not 1 <= header_row <= 100:
        raise ParseError("invalid_input", "表头行号须在 1 到 100 之间",
                         {"header_row": header_row})
    if sample_rows < 0:
        raise ParseError("invalid_input", "预览行数不能为负数", {"sample_rows": sample_rows})
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in TABLE_SUFFIXES:
        raise ParseError("unsupported_format", "仅支持 .xlsx / .csv 表格预览", {"suffix": suffix})
    data = path.read_bytes()
    sheets, selected_sheet, rows = read_table_rows(data, suffix, sheet_name)
    header_idx = header_row - 1
    raw_header = rows[header_idx] if header_idx < len(rows) else []
    data_rows = [row for row in rows[header_row:] if any(cell != "" for cell in row)]
    col_count = max([len(raw_header)] + [len(row) for row in data_rows], default=0)
    headers = [
        raw_header[i] if i < len(raw_header) and raw_header[i] else f"列{i + 1}"
        for i in range(col_count)
    ]
    sample = [
        [row[i] if i < len(row) else "" for i in range(col_count)]
        for row in data_rows[:sample_rows]
    ]
    return {
        "source": str(path),
        "suffix": suffix,
        "sheets": sheets,
        "selected_sheet": selected_sheet,
        "header_row": header_row,
        "headers": headers,
        "sample_rows": sample,
        "total_rows": len(data_rows),
    }


# --- 取值解析 -------------------------------------------------------------


def infer_entity_kind(name: str) -> str:
    normalized = normalize_with_map(name)[0].strip()
    lowered = normalized.lower()
    if any(token in normalized for token in _ORG_CONTAINS):
        return "org"
    stripped = _TRAILING_PAREN_RE.sub("", lowered).strip()
    for candidate in (lowered, stripped):
        if any(candidate.endswith(suffix) for suffix in _ORG_SUFFIXES):
            return "org"
    return "person"


def normalize_currency(raw: str, fallback: str | None) -> str | None:
    text = normalize_with_map(raw)[0].strip()
    if not text:
        return fallback
    alias = _CURRENCY_ALIASES.get(text) or _CURRENCY_ALIASES.get(text.upper())
    if alias is not None:
        return alias
    if _CURRENCY_CODE_RE.fullmatch(text):
        return text.upper()
    return None


def detect_inline_currency(raw: str) -> str | None:
    normalized = normalize_with_map(raw)[0]
    for pattern, code in _INLINE_CURRENCY_PATTERNS:
        if pattern.search(normalized):
            return code
    return None


def parse_amount_cents(raw: str) -> tuple[int, bool]:
    """Parse monetary text using Decimal only; returns ``(abs_cents, is_negative)``."""
    normalized = normalize_with_map(raw)[0].strip()
    cleaned = _CURRENCY_TOKEN_RE.sub("", normalized)
    if not cleaned or not _DECIMAL_RE.fullmatch(cleaned):
        raise ValueError(f"无法解析金额：{raw}")
    try:
        amount = Decimal(cleaned)
        if not amount.is_finite() or abs(amount) > Decimal("9000000000000"):
            raise ValueError(f"无法解析金额：{raw}")
        is_negative = amount < 0
        quantized = abs(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        cents = int(quantized * 100)
    except (InvalidOperation, ArithmeticError, OverflowError, ValueError) as exc:
        raise ValueError(f"无法解析金额：{raw}") from exc
    return cents, is_negative


def parse_share_pct(raw: str) -> float:
    normalized = normalize_with_map(raw)[0].strip()
    if normalized.endswith("%"):
        normalized = normalized[:-1].strip()
    if not normalized or not _DECIMAL_RE.fullmatch(normalized):
        raise ValueError(f"无法解析持股比例：{raw}")
    try:
        pct = Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError(f"无法解析持股比例：{raw}") from exc
    if not pct.is_finite() or pct < Decimal(0) or pct > Decimal(100):
        raise ValueError(f"持股比例超出 0..100 范围：{raw}")
    return float(pct)


def parse_date(raw: str) -> date | None:
    text = normalize_with_map(raw)[0].strip()
    if not text:
        return None
    for pattern in (_DATE_SEP_RE, _DATE_COMPACT_RE):
        match = pattern.match(text)
        if not match:
            continue
        year, month, day = (int(part) for part in match.groups())
        if pattern is _DATE_COMPACT_RE and not (1900 <= year <= 2100):
            continue
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None
