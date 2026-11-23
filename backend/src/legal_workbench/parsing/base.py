"""Common parse result model shared by all format parsers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PARSER_VERSION = "1.0"


@dataclass
class ParsedUnit:
    unit_key: str
    locator: dict[str, Any]
    text: str
    method: str = "native"
    status: str = "extracted"
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class ParseResult:
    units: list[ParsedUnit]
    parser_version: str
    config: dict[str, Any]
    status: str = "complete"  # complete | partial | unsupported | failed
    warnings: list[str] = field(default_factory=list)


def make_result(units: list[ParsedUnit], parser_version: str, config: dict[str, Any],
                warnings: list[str] | None = None) -> ParseResult:
    status = "complete"
    if any(unit.status in ("failed", "unsupported") for unit in units):
        status = "partial"
    if not units:
        status = "failed"
    if warnings:
        status = "partial" if status == "complete" else status
    return ParseResult(units=units, parser_version=parser_version, config=config,
                       status=status, warnings=warnings or [])
