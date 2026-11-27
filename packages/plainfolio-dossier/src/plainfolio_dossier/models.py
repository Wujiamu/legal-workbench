"""公开数据契约：清单（manifest）与装卷结果。

工作台与独立模式共用这一套模型——工作台把数据库快照转成同样的
:class:`DossierSpec`，独立模式直接从 JSON/CSV 清单读出来，因此渲染内核只有
一条输入路径。``extra="ignore"`` 让律师手写的清单可以带备注字段而不报错。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field


class CaseMeta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    case_number: str | None = None
    plaintiff: str | None = None
    defendant: str | None = None


class BindingSpec(BaseModel):
    """一条"证据条目 → 材料文件（可含页码范围）"的绑定。"""

    model_config = ConfigDict(extra="ignore")

    file_path: str
    material_name: str
    page_start: int | None = None
    page_end: int | None = None
    acquisition: str | None = None
    sha256: str | None = None
    size: int | None = None
    ext: str | None = None


class ItemSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    purpose: str | None = None
    bindings: list[BindingSpec] = Field(default_factory=list)


class GroupSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    items: list[ItemSpec] = Field(default_factory=list)


class DossierSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    case: CaseMeta
    groups: list[GroupSpec] = Field(default_factory=list)


@dataclass(frozen=True)
class DossierResult:
    """装卷产物：ZIP 字节 + 文件名 + 用户可核对的页数与提示。

    前六个字段是稳定契约；其余为附加统计，工作台适配层需要它们还原历史摘要
    （``group_count`` / ``standalone_file_count`` / ``reminders``），独立模式
    不打印即可。
    """

    zip_bytes: bytes
    filename: str
    item_count: int
    total_body_pages: int
    total_pages: int
    warnings: list[str] = field(default_factory=list)
    group_count: int = 0
    standalone_file_count: int = 0
    reminders: list[str] = field(default_factory=list)
