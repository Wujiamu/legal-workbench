"""Plain-text parsing: UTF-8/BOM and GB18030, block-level locators."""
from __future__ import annotations

from pathlib import Path

from ..errors import ErrorCode, WorkbenchError
from .base import ParseResult, ParsedUnit, make_result

PARSER = "text-1.0"


def decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise WorkbenchError(ErrorCode.UNSUPPORTED_FORMAT, "文本编码无法识别（仅支持 UTF-8/GB18030）")


def parse_text(path: Path) -> ParseResult:
    raw = path.read_bytes()
    text = decode_text(raw).replace("\r\n", "\n").replace("\r", "\n")
    units: list[ParsedUnit] = []
    block: list[str] = []
    block_start = 1
    block_index = 0
    line_no = 0
    for line_no, line in enumerate(text.split("\n"), start=1):
        if line.strip():
            if not block:
                block_start = line_no
            block.append(line)
        elif block:
            units.append(_unit(block_index, block_start, line_no - 1, block))
            block_index += 1
            block = []
    if block:
        units.append(_unit(block_index, block_start, line_no, block))
    if not units:
        return ParseResult(
            units=[ParsedUnit(unit_key="text:0", locator={"type": "text", "block": 0},
                              text="", method="native", status="no_text_detected")],
            parser_version=PARSER, config={"encoding": "utf-8/gb18030"},
            status="complete",
        )
    return make_result(units, PARSER, {"encoding": "utf-8/gb18030"})


def _unit(index: int, start: int, end: int, lines: list[str]) -> ParsedUnit:
    return ParsedUnit(
        unit_key=f"text:{index}",
        locator={"type": "text", "block": index, "line_start": start, "line_end": end},
        text="\n".join(lines), method="native", status="extracted",
    )
