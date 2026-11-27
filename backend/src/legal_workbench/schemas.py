"""Shared protocol definitions. Transport and business logic live elsewhere."""
from enum import StrEnum


class IntakeStatus(StrEnum):
    PENDING = "pending"
    COPYING = "copying"
    PUBLISHED = "published"
    READY = "ready"
    FAILED = "failed"
    SKIPPED = "skipped"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class ParseUnitStatus(StrEnum):
    PENDING = "pending"
    EXTRACTED = "extracted"
    OCR_DONE = "ocr_done"
    NO_TEXT_DETECTED = "no_text_detected"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


class ParseRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


class SearchMode(StrEnum):
    LITERAL = "literal"
    IDENTIFIER = "identifier"
    AMOUNT = "amount"
    DATE = "date"


class SearchScope(StrEnum):
    ORIGINALS = "originals"
    WORKING = "working"
    ANALYSIS = "analysis"


class StatementKind(StrEnum):
    EXCERPT = "excerpt"
    PARTY_STATEMENT = "party_statement"
    AGENT_ANALYSIS = "agent_analysis"
    HUMAN_OPINION = "human_opinion"

from decimal import Decimal
from typing import Any, Annotated, Literal
from datetime import date, datetime, timezone
from uuid import UUID

from pydantic import (BaseModel, ConfigDict, Field, PlainSerializer, field_validator,
                       model_validator)


UtcDatetime = Annotated[datetime, PlainSerializer(
    lambda value: value.astimezone(timezone.utc), return_type=datetime, when_used="json")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Timestamps go out as UTC.  Rows come back from psycopg carrying the
    # server's session offset, so the same instant would otherwise appear as
    # ``+08:00`` here and ``+00:00`` elsewhere.  A per-field ``UtcDatetime``
    # alias is used instead of a catch-all ``field_serializer("*")``: the
    # catch-all serialises to ``Any``, which erases every field type from the
    # generated OpenAPI schema and turns the frontend contract into ``unknown``.


class ReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class Pagination(Contract):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=200)


class SearchPagination(Contract):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


class CaseInput(Contract):
    case_id: UUID


CaseWorkStatus = Literal["normal", "need_materials", "need_sync", "waiting_external", "paused"]
CaseDeadlineFilter = Literal["all", "overdue", "due_soon", "has_open"]
CaseSortBy = Literal["next_due", "created_at", "case_number", "work_status", "title"]
CaseSortDir = Literal["asc", "desc"]


class CreateCaseInput(Contract):
    title: str = Field(min_length=1)
    description: str | None = None
    case_number: str | None = Field(default=None, max_length=120)
    plaintiff: str | None = Field(default=None, max_length=200)
    defendant: str | None = Field(default=None, max_length=200)
    work_status: CaseWorkStatus = "normal"


class Deadline(Contract):
    title: str = Field(min_length=1, max_length=200)
    due: date
    kind: Literal["external", "internal"]
    completed: bool = False

    @field_validator("title")
    @classmethod
    def nonblank_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("期限事项不能为空")
        return value.strip()


class CaseScheduleInput(CaseInput):
    expected_revision: int = Field(ge=0)
    stage: str | None = Field(default=None, max_length=120)
    next_action: str | None = Field(default=None, max_length=500)
    deadlines: list[Deadline] = Field(max_length=200)


class CaseMetadataInput(CaseInput):
    expected_revision: int = Field(ge=0)
    case_number: str | None = Field(default=None, max_length=120)
    plaintiff: str | None = Field(default=None, max_length=200)
    defendant: str | None = Field(default=None, max_length=200)
    work_status: CaseWorkStatus = "normal"


class CaseSummary(Contract):
    case_id: UUID
    title: str
    description: str | None
    case_number: str | None = None
    plaintiff: str | None = None
    defendant: str | None = None
    work_status: CaseWorkStatus = "normal"
    revision: int = Field(ge=0)
    created_at: UtcDatetime
    # 结案归档时间；非空即已归档（进度与材料只读）。
    archived_at: UtcDatetime | None = None


class CaseOverview(CaseSummary):
    stage: str | None
    next_action: str | None
    deadlines: list[Deadline]
    # P4：当前进行中阶段（来自进度树，K14）；旧 stage 字段保留供过渡显示。
    active_stage: str | None = None
    # 全部进行中阶段标题（并行程序各有一个当前阶段，K3；首页按计划第 6 节全部展示）。
    active_stages: list[str] = Field(default_factory=list)
    # 各进行中阶段下（含嵌套）未完成工作标题（最多展示几条由前端决定）。
    pending_work: list[str] = Field(default_factory=list)


class ArchiveCaseInput(CaseInput):
    """结案归档/取消归档：案件级操作，用 revision 做 CAS，不是进度命令。"""
    expected_revision: int = Field(ge=0)


class DashboardOutput(Contract):
    items: list[CaseOverview]
    offset: int
    limit: int
    total: int


class DashboardDeadlineItem(Contract):
    """首页跨案期限时间轴的一行：期限仍存放在 ``cases.deadlines`` JSONB 中。"""

    case_id: UUID
    case_title: str
    title: str
    due: date
    kind: Literal["external", "internal"]
    completed: bool = False


class DashboardDeadlinesInput(Contract):
    """GET /dashboard/deadlines takes no input; placeholder for the protocol generator."""

    pass


class DashboardDeadlinesOutput(Contract):
    """未归档案件的全部期限（不按 50 条分页），按日期升序。

    ``as_of`` 是服务端按 ``Asia/Shanghai`` 算出的“今天”，供前端锚定窗口与
    “今天”竖线，避免客户端系统时区把日期算偏；``case_total`` 是参与统计的
    未归档案件数，用于核对时间轴覆盖了全部在办案件而不是某一页。
    """

    items: list[DashboardDeadlineItem]
    as_of: date
    case_total: int


class CreateCaseOutput(Contract):
    case_id: UUID
    revision: int = Field(ge=0)


class DashboardInput(Pagination):
    q: str | None = None
    work_status: CaseWorkStatus | None = None
    deadline_filter: CaseDeadlineFilter | None = None
    sort_by: CaseSortBy | None = None
    sort_dir: CaseSortDir | None = None


class ListCasesInput(DashboardInput):
    archived: bool | None = None


class ListCasesOutput(Contract):
    items: list[CaseSummary]
    offset: int
    limit: int
    total: int


class ImportMaterialsInput(CaseInput):
    source_path: str = Field(min_length=1)
    source_label: str = Field(min_length=1)
    request_key: str = Field(min_length=1)


class ImportMaterialsOutput(Contract):
    intake_id: UUID
    job_id: UUID


class Coverage(Contract):
    complete: int = Field(ge=0)
    partial: int = Field(ge=0)
    failed: int = Field(ge=0)
    pending: int = Field(ge=0)
    unsupported: int = Field(ge=0)


class MaterialSummary(Contract):
    acquisition: str | None = None
    material_id: UUID
    display_name: str
    category: str
    revision: int = Field(ge=0)
    intake_id: UUID
    source_label: str
    relative_source: str
    status: IntakeStatus
    coverage: Coverage
    created_at: UtcDatetime


class ListMaterialsInput(CaseInput, Pagination):
    category: str | None = None
    status: IntakeStatus | None = None


class ListMaterialsOutput(Contract):
    items: list[MaterialSummary]
    offset: int
    limit: int
    total: int
    # Case-wide, independent of the current page and category filter: the
    # navigation must show a category that only appears on page 3, and "全部"
    # must keep meaning the whole case.
    case_total: int
    categories: list[str]


class UpdateClassificationInput(CaseInput):
    material_id: UUID
    category: str
    expected_revision: int = Field(ge=0)


class UpdateAcquisitionInput(CaseInput):
    material_id: UUID
    acquisition: str | None = Field(default=None, max_length=500)
    expected_revision: int = Field(ge=0)


class RevisionOutput(Contract):
    revision: int = Field(ge=0)


class CreateWorkingCopyInput(CaseInput):
    file_version_id: UUID
    # Optional: when omitted the server derives the name from the material's
    # display name and the stored object's extension, so a client cannot produce
    # an unparseable copy by concatenating "（工作副本）" after the extension.
    display_name: str | None = None


class CreateWorkingCopyOutput(Contract):
    working_copy_id: UUID
    relative_path: str


class RegisterFileVersionInput(CaseInput):
    working_copy_id: UUID
    expected_revision: int = Field(ge=0)


class RegisterFileVersionOutput(Contract):
    file_version_id: UUID
    job_id: UUID


class ReparseInput(CaseInput):
    file_version_id: UUID
    force_ocr_pages: list[Annotated[int, Field(ge=1)]] = Field(default_factory=list)


class ReparseOutput(Contract):
    parse_run_id: UUID
    job_id: UUID


class SearchCaseInput(CaseInput, SearchPagination):
    query: str = Field(min_length=1)
    mode: SearchMode
    scope: SearchScope = SearchScope.ORIGINALS
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")

    @model_validator(mode="after")
    def amount_requires_currency(self):
        if self.mode == SearchMode.AMOUNT and self.currency is None:
            raise ValueError("currency is required for amount mode")
        return self


class SourceLocation(Contract):
    source_ref: UUID
    material_id: UUID
    file_version_id: UUID
    parse_run_id: UUID
    locator: dict[str, Any]
    text: str
    method: str


class SearchHit(Contract):
    source: SourceLocation | None
    analysis_id: UUID | None = None
    analysis_version_id: UUID | None = None
    source_type: SearchScope
    context: str
    match_start: int = Field(ge=0)
    match_end: int = Field(ge=0)
    amount: Decimal | None = None
    currency: str | None = None

    @model_validator(mode="after")
    def valid_hit_location(self):
        if self.match_start > self.match_end or self.match_end > len(self.context):
            raise ValueError("match offsets must be within context")
        if self.source_type == SearchScope.ANALYSIS:
            if self.analysis_id is None or self.analysis_version_id is None or self.source is not None:
                raise ValueError("analysis hit requires analysis and version identifiers")
        elif self.source is None or self.analysis_id is not None or self.analysis_version_id is not None:
            raise ValueError("material hit requires a source")
        return self

    @field_validator("amount", mode="before", json_schema_input_type=str | int | None)
    @classmethod
    def reject_float_amount(cls, value):
        if isinstance(value, float):
            raise ValueError("amount must not be a floating-point number")
        return value


class SearchCaseOutput(Contract):
    hits: list[SearchHit]
    coverage: Coverage
    mode_used: SearchMode
    offset: int
    limit: int
    total: int


class ReadSourceInput(CaseInput):
    source_ref: UUID
    context_chars: int = Field(default=200, ge=0, le=2000)


class ReadSourceOutput(SourceLocation):
    acquisition: str | None = None
    display_name: str | None = None
    source_label: str | None = None
    relative_source: str | None = None
    is_original: bool = True
    context_before: str
    context_after: str


class GetOriginalInput(CaseInput):
    material_id: UUID


class GetOriginalOutput(Contract):
    file_version_id: UUID
    integrity_status: str
    browse_url: str


class GetJobStatusInput(CaseInput):
    job_id: UUID


class JobItemError(Contract):
    item_id: UUID
    error_code: str
    message: str


class GetJobStatusOutput(Contract):
    job_id: UUID
    status: JobStatus
    completed_units: int = Field(ge=0)
    total_units: int = Field(ge=0)
    errors: list[JobItemError]


class GetIndexStatusInput(CaseInput):
    pass


class GetIndexStatusOutput(Coverage):
    pass


class SaveAnalysisInput(CaseInput):
    title: str = Field(min_length=1)
    content: str
    statement_kind: StatementKind
    source_refs: list[UUID]
    analysis_id: UUID | None = None
    expected_revision: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def update_requires_revision(self):
        if (self.analysis_id is None) != (self.expected_revision is None):
            raise ValueError("analysis_id and expected_revision must be supplied together")
        return self


class SaveAnalysisOutput(Contract):
    analysis_id: UUID
    version_id: UUID
    revision: int = Field(ge=0)


class ReviewAnalysisInput(CaseInput):
    analysis_id: UUID
    version_id: UUID
    expected_revision: int = Field(ge=0)
    review_status: ReviewStatus


class ReviewAnalysisOutput(RevisionOutput):
    review_status: ReviewStatus


class UpdateDisplayNameInput(CaseInput):
    material_id: UUID
    display_name: str = Field(min_length=1)
    expected_revision: int = Field(ge=0)


class BrowserUploadInput(CaseInput):
    source_label: str = Field(min_length=1)
    request_key: str = Field(min_length=1)
    relative_paths: list[str] = Field(min_length=1)


class ListIntakeItemsInput(CaseInput, Pagination):
    intake_id: UUID
    status: IntakeStatus | None = None


class IntakeItem(Contract):
    intake_item_id: UUID
    relative_source: str
    status: IntakeStatus
    material_id: UUID | None
    error_code: str | None
    error_message: str | None


class ListIntakeItemsOutput(Contract):
    items: list[IntakeItem]
    offset: int
    limit: int
    total: int


class RetryJobInput(CaseInput):
    job_id: UUID


class RetryJobOutput(Contract):
    job_id: UUID
    status: JobStatus


class FileVersionAccessInput(CaseInput):
    file_version_id: UUID


class ExtractedSegment(Contract):
    """One immutable segment of the active parse run, with its locator."""

    source_ref: UUID
    locator: dict[str, Any]
    text: str
    method: str
    status: ParseUnitStatus


class ExtractedTextOutput(Contract):
    file_version_id: UUID
    parse_run_id: UUID | None
    coverage: Coverage
    segments: list[ExtractedSegment]


class FileContentOutput(Contract):
    """Placeholder for HTTP binary content; HTTP schema is overridden by generator."""
    content: bytes


class AnalysisSummary(Contract):
    analysis_id: UUID
    title: str
    revision: int = Field(ge=0)
    current_version_id: UUID
    review_status: ReviewStatus
    created_at: UtcDatetime


class ListAnalysesInput(CaseInput, Pagination):
    pass


class ListAnalysesOutput(Contract):
    items: list[AnalysisSummary]
    offset: int
    limit: int
    total: int


class GetAnalysisInput(CaseInput):
    analysis_id: UUID


class GetAnalysisOutput(AnalysisSummary):
    pass


class GetAnalysisVersionInput(GetAnalysisInput):
    version_id: UUID


class GetAnalysisVersionOutput(Contract):
    analysis_id: UUID
    version_id: UUID
    title: str
    content: str
    statement_kind: StatementKind
    source_refs: list[UUID]
    review_status: ReviewStatus


class ListAnalysisVersionsInput(GetAnalysisInput, Pagination):
    pass


class AnalysisVersionSummary(Contract):
    version_id: UUID
    statement_kind: StatementKind
    review_status: ReviewStatus
    created_at: UtcDatetime


class ListAnalysisVersionsOutput(Contract):
    items: list[AnalysisVersionSummary]
    offset: int
    limit: int
    total: int


class FilePreviewInput(FileVersionAccessInput):
    frame: int | None = Field(default=None, ge=0)


class PdfPageInput(FileVersionAccessInput):
    page: int = Field(ge=1)


class ListAnnotationsInput(CaseInput, Pagination):
    query: str | None = Field(default=None, max_length=500)
    file_version_id: UUID | None = None


class CreateAnnotationInput(CaseInput):
    file_version_id: UUID
    page: int | None = Field(default=None, ge=1, le=2147483647)
    kind: Literal["transcription", "interpretation"]
    body: str = Field(min_length=1, max_length=20000)


class ReviewAnnotationInput(CaseInput):
    annotation_id: UUID
    reviewed_text: str = Field(min_length=1, max_length=20000)


class AnnotationOutput(CreateAnnotationInput):
    annotation_id: UUID
    display_name: str
    author: Literal["agent", "human"]
    reviewed_text: str | None
    revision: int
    created_at: UtcDatetime


class ListAnnotationsOutput(Contract):
    items: list[AnnotationOutput]
    offset: int
    limit: int
    total: int


# --- 办案进度协议（P2）-------------------------------------------------------

ProgressActor = Literal["human", "agent"]
ProgressEntry = Literal["local-web", "mcp", "cli"]
ProgressReviewState = Literal["none", "pending", "accepted", "reverted"]


class ProgressNodeOut(Contract):
    id: UUID
    case_id: UUID
    parent_id: UUID | None
    position: int
    kind: str
    state: str
    title: str
    note: str | None
    date_start: date | None
    date_end: date | None
    template_ref: str | None
    created_by: ProgressActor
    updated_by: ProgressActor
    request_key: str | None
    removed_at: UtcDatetime | None
    revision: int = Field(ge=0)
    created_at: UtcDatetime
    updated_at: UtcDatetime


class ProgressRelationOut(Contract):
    id: UUID
    case_id: UUID
    from_node_id: UUID
    to_node_id: UUID
    type: str
    note: str | None
    created_by: ProgressActor
    removed_at: UtcDatetime | None
    created_at: UtcDatetime


class ProgressLinkOut(Contract):
    id: UUID
    case_id: UUID
    node_id: UUID
    material_id: UUID | None
    analysis_id: UUID | None
    annotation_id: UUID | None
    created_by: ProgressActor
    created_at: UtcDatetime
    display_name: str


class ChangeDiffEntry(Contract):
    target: Literal["node", "relation", "link"]
    id: UUID
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    before_rev: int | None
    after_rev: int | None


class ProgressChangeOut(Contract):
    id: UUID
    case_id: UUID
    seq: int
    command: str
    actor: ProgressActor
    entry: str
    diff: list[ChangeDiffEntry]
    review_state: ProgressReviewState
    reviewed_at: UtcDatetime | None
    reverted_by: UUID | None
    created_at: UtcDatetime


class ProgressSnapshot(Contract):
    case_id: UUID
    seq: int
    nodes: list[ProgressNodeOut]
    relations: list[ProgressRelationOut]
    links: list[ProgressLinkOut]
    pending_changes: list[ProgressChangeOut]
    changes: list[ProgressChangeOut]
    # 审查 N3：写命令响应携带本次产生的变更 ID，前端按此入撤销栈，
    # 不再用「最后一条同型 human 变更」猜测（并发下会错认）。读快照为 None。
    last_change_id: UUID | None = None


class NodeChanges(Contract):
    """字段三态：未提供＝不动；显式 null＝清空（note/日期）；值＝设置。

    title/state 不接受 null（标题非空、状态必为合法值），用 model_fields_set
    区分「未提供」与「显式传值」，避免把缺省 None 误当成清空。
    """
    title: str | None = Field(default=None, max_length=200)
    state: str | None = None
    note: str | None = Field(default=None, max_length=4000)
    date_start: date | None = None
    date_end: date | None = None


class AddNodeInput(CaseInput):
    parent_id: UUID | None = None
    kind: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=200)
    position: int | None = Field(default=None, ge=0)
    state: str | None = None
    note: str | None = Field(default=None, max_length=4000)
    date_start: date | None = None
    date_end: date | None = None
    request_key: str | None = None


class UpdateNodeInput(CaseInput):
    node_id: UUID
    expected_revision: int = Field(ge=0)
    changes: NodeChanges


class MoveNodeInput(CaseInput):
    node_id: UUID
    expected_revision: int = Field(ge=0)
    new_parent_id: UUID | None = None
    before_node_id: UUID | None = None


class SetActiveStageInput(CaseInput):
    stage_id: UUID
    expected_revision: int = Field(ge=0)


class InsertFragmentInput(CaseInput):
    parent_id: UUID | None = None
    fragment_key: str = Field(min_length=1)
    before_node_id: UUID | None = None


class NodeRevisionInput(CaseInput):
    node_id: UUID
    expected_revision: int = Field(ge=0)


class AddRelationInput(CaseInput):
    from_node_id: UUID
    to_node_id: UUID
    relation_type: str = Field(min_length=1)
    note: str | None = Field(default=None, max_length=4000)


class RemoveRelationInput(CaseInput):
    relation_id: UUID


class LinkTarget(Contract):
    material_id: UUID | None = None
    analysis_id: UUID | None = None
    annotation_id: UUID | None = None

    @model_validator(mode="after")
    def exactly_one(self):
        if sum(v is not None for v in (self.material_id, self.analysis_id, self.annotation_id)) != 1:
            raise ValueError("关联必须且只能指向一个对象")
        return self


class LinkNodeInput(CaseInput):
    node_id: UUID
    target: LinkTarget


class UnlinkInput(CaseInput):
    link_id: UUID


class ChangeIdInput(CaseInput):
    change_id: UUID


class AcceptAllInput(CaseInput):
    """一键确认须提交客户端已展示的变更 ID 集合，或提交打开确认框时的序号（expected_seq）。

    服务端在案锁内只确认这些条目；其间新增的待审修改不被一并确认（审查 04）。
    显式提交 change_ids=[] 一律拒绝；省略 change_ids 时必须携带匹配的 expected_seq，
    否则无法证明确认范围经过人工核对（审查末节）。
    """
    change_ids: list[UUID] = Field(default_factory=list)
    expected_seq: int | None = Field(default=None, ge=0)


class TimeEntryOut(Contract):
    id: UUID
    case_id: UUID
    node_id: UUID | None
    node_title: str
    work_date: date
    hours: float
    source: Literal["timer", "manual"]
    started_at: UtcDatetime | None
    stopped_at: UtcDatetime | None
    note: str | None
    created_at: UtcDatetime


class StartTimerInput(CaseInput):
    node_id: UUID | None = None
    node_title: str = Field(min_length=1, max_length=200)


class StopTimerInput(CaseInput):
    entry_id: UUID


class AddTimeEntryInput(CaseInput):
    node_id: UUID | None = None
    node_title: str = Field(min_length=1, max_length=200)
    hours: float = Field(gt=0, le=24)
    work_date: date
    note: str | None = Field(default=None, max_length=2000)


class RemoveTimeEntryInput(CaseInput):
    entry_id: UUID


class TimeSummaryRow(Contract):
    title: str
    hours: float
    entries: int


class TimeDayOutput(Contract):
    work_date: date
    entries: list[TimeEntryOut]
    total_hours: float
    summary: list[TimeSummaryRow]


class GetTimeDayInput(CaseInput):
    """GET query contract for the day view (date alias in the endpoint)."""
    work_date: date


class ChangeSeqsOutput(Contract):
    seqs: dict[str, int]


class ProgressRulesOutput(Contract):
    rules: dict[str, Any]


class ProgressRulesQuery(Contract):
    """GET /progress/rules takes no input; placeholder for the protocol generator."""
    pass


class ModulesOutput(Contract):
    """Enabled/optional module surfaces of this installation (read-only)."""

    enabled_modules: list[str]
    all_modules: list[str]


class ModulesQuery(Contract):
    """GET /modules takes no input; placeholder for the protocol generator."""
    pass


# --- 主体关系图与证据目录协议（P2）-------------------------------------------
# 与 docs/changes/20261120-案件主体关系图.md 5.2/5.3 的命令契约对应。
# actor 由入口固定，输入里不出现 actor/entry。

GraphActor = Literal["human", "agent"]
Credibility = Literal["evidenced", "suspected", "disputed", "ruled_out"]
EvidenceReviewState = Literal["confirmed", "pending", "rejected"]
ConsentAction = Literal["confirm", "amend", "revoke"]


class EntityOut(Contract):
    id: UUID
    case_id: UUID
    kind: str
    name: str
    roles: list[str]
    note: str | None
    created_by: GraphActor
    updated_by: GraphActor
    request_key: str | None
    removed_at: UtcDatetime | None
    revision: int = Field(ge=0)
    created_at: UtcDatetime
    updated_at: UtcDatetime


class AliasOut(Contract):
    id: UUID
    case_id: UUID
    entity_id: UUID
    alias: str
    created_by: GraphActor
    removed_at: UtcDatetime | None
    created_at: UtcDatetime


class RelationAttrs(Contract):
    """关系属性：持股比例 / 金额（分）/ 币种 / 起止日期。

    金额用整数分存储，避免浮点误差；方向由类别决定，不在这里带正负号。
    """

    share_pct: float | None = Field(default=None, ge=0, le=100)
    amount_cents: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, max_length=8)
    date_start: date | None = None
    date_end: date | None = None

    @model_validator(mode="after")
    def ordered_dates(self):
        if self.date_start and self.date_end and self.date_end < self.date_start:
            raise ValueError("关系结束日期不能早于开始日期")
        return self


class RelationOut(Contract):
    id: UUID
    case_id: UUID
    from_entity_id: UUID
    to_entity_id: UUID
    directed: bool
    kind: str
    label: str | None
    credibility: Credibility
    attrs: RelationAttrs
    note: str | None
    created_by: GraphActor
    updated_by: GraphActor
    request_key: str | None
    removed_at: UtcDatetime | None
    revision: int = Field(ge=0)
    created_at: UtcDatetime
    updated_at: UtcDatetime


class SourceRefInput(Contract):
    """出处三选一：不可变片段 / 材料 / 标注。"""

    segment_id: UUID | None = None
    material_id: UUID | None = None
    annotation_id: UUID | None = None

    @model_validator(mode="after")
    def exactly_one(self):
        if sum(v is not None for v in (self.segment_id, self.material_id, self.annotation_id)) != 1:
            raise ValueError("出处必须且只能指向一个对象（片段/材料/标注）")
        return self


class SourceOut(Contract):
    id: UUID
    case_id: UUID
    entity_id: UUID | None
    relation_id: UUID | None
    segment_id: UUID | None
    material_id: UUID | None
    annotation_id: UUID | None
    proves: str | None
    created_by: GraphActor
    removed_at: UtcDatetime | None
    created_at: UtcDatetime


class GraphOut(Contract):
    id: UUID
    case_id: UUID
    title: str
    description: str | None
    position: int
    created_by: GraphActor
    updated_by: GraphActor
    request_key: str | None
    removed_at: UtcDatetime | None
    revision: int = Field(ge=0)
    created_at: UtcDatetime
    updated_at: UtcDatetime


class GraphGroupOut(Contract):
    id: UUID
    case_id: UUID
    graph_id: UUID
    title: str
    color: str | None
    x: float
    y: float
    width: float
    height: float
    collapsed: bool
    revision: int = Field(ge=0)
    removed_at: UtcDatetime | None
    created_at: UtcDatetime
    updated_at: UtcDatetime


class GraphNodeOut(Contract):
    id: UUID
    case_id: UUID
    graph_id: UUID
    entity_id: UUID
    group_id: UUID | None
    x: float
    y: float
    revision: int = Field(ge=0)
    created_at: UtcDatetime
    updated_at: UtcDatetime


class GraphEdgeOut(Contract):
    id: UUID
    case_id: UUID
    graph_id: UUID
    relation_id: UUID
    created_at: UtcDatetime


class SnapshotOut(Contract):
    id: UUID
    case_id: UUID
    graph_id: UUID
    version_no: int = Field(ge=1)
    stage_label: str
    note: str | None
    progress_node_id: UUID | None
    payload: dict[str, Any]
    excluded_pending_count: int = Field(ge=0)
    created_by: Literal["human"]
    created_at: UtcDatetime


class ConsentOut(Contract):
    id: UUID
    case_id: UUID
    snapshot_id: UUID
    action: ConsentAction
    parent_consent_id: UUID | None
    consenter: str
    consented_at: UtcDatetime
    channel: str
    source_detail: str
    evidence_material_id: UUID | None
    note: str | None
    created_by: Literal["human"]
    created_at: UtcDatetime


class EvidenceGroupOut(Contract):
    id: UUID
    case_id: UUID
    position: int
    title: str
    purpose: str | None
    created_by: GraphActor
    request_key: str | None
    removed_at: UtcDatetime | None
    revision: int = Field(ge=0)
    created_at: UtcDatetime
    updated_at: UtcDatetime


class EvidenceItemOut(Contract):
    id: UUID
    case_id: UUID
    group_id: UUID
    position: int
    title: str
    purpose: str | None
    source_kind: Literal["manual", "imported"]
    import_material_id: UUID | None
    review_state: EvidenceReviewState
    created_by: GraphActor
    request_key: str | None
    removed_at: UtcDatetime | None
    revision: int = Field(ge=0)
    created_at: UtcDatetime
    updated_at: UtcDatetime


class EvidenceBindingOut(Contract):
    id: UUID
    case_id: UUID
    item_id: UUID
    material_id: UUID
    page_start: int | None
    page_end: int | None
    review_state: EvidenceReviewState
    created_by: GraphActor
    removed_at: UtcDatetime | None
    created_at: UtcDatetime


class CatalogMaterial(Contract):
    id: UUID
    display_name: str


class EvidenceCatalogOut(Contract):
    case_id: UUID
    seq: int
    groups: list[EvidenceGroupOut]
    items: list[EvidenceItemOut]
    bindings: list[EvidenceBindingOut]
    materials: list[CatalogMaterial]
    # item_id → 「证据1-1」；仅已确认且未删除的分组/条目参与。
    numbers: dict[str, str]
    # group_id → 1-based 组号。
    group_numbers: dict[str, int]
    # 写命令响应携带本次变更 ID（前端入撤销栈）；读快照为 None。
    last_change_id: UUID | None = None
    notice: str | None = None
    # P4 复核面板：待确认候选与最近变更（与关系图工作区同形）。
    pending_changes: list["GraphChangeOut"] = []
    changes: list["GraphChangeOut"] = []


class ResolvedLabelOut(Contract):
    labels: list[str]
    text: str
    material_id: UUID | None
    material_title: str
    page: int | None
    text_excerpt: str | None
    # P4 来源面板定位：片段出处直接给出不可变 source_ref；其余出处给出
    # 材料当前原件版本，前端再用 /file-versions/{id}/page-preview 或
    # /materials/{id}/original 打开对应位置。
    source_ref: UUID | None = None
    file_version_id: UUID | None = None


GraphDiffTarget = Literal[
    "entity", "alias", "relation", "source", "graph", "graph_node", "graph_edge",
    "graph_group", "layout", "snapshot", "consent",
    "evidence_group", "evidence_item", "evidence_binding",
]


class GraphDiffEntry(Contract):
    target: GraphDiffTarget
    id: UUID
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    before_rev: int | None
    after_rev: int | None


class GraphChangeOut(Contract):
    id: UUID
    case_id: UUID
    seq: int
    command: str
    actor: GraphActor
    entry: str
    diff: list[GraphDiffEntry]
    review_state: ProgressReviewState
    reviewed_at: UtcDatetime | None
    reverted_by: UUID | None
    created_at: UtcDatetime


# EvidenceCatalogOut 在 GraphChangeOut 之前定义（目录先于图契约），这里补解析前向引用。
EvidenceCatalogOut.model_rebuild()


class GraphWorkspace(Contract):
    case_id: UUID
    case_title: str
    archived: bool
    seq: int
    seqs: dict[str, int]
    entities: list[EntityOut]
    aliases: list[AliasOut]
    relations: list[RelationOut]
    sources: list[SourceOut]
    graphs: list[GraphOut]
    nodes: list[GraphNodeOut]
    edges: list[GraphEdgeOut]
    groups: list[GraphGroupOut]
    snapshots: list[SnapshotOut]
    consents: list[ConsentOut]
    evidence: EvidenceCatalogOut
    # 出处 id → 现行编号（仅已确认目录）。
    labels: dict[str, ResolvedLabelOut]
    pending_changes: list[GraphChangeOut]
    changes: list[GraphChangeOut]
    last_change_id: UUID | None = None
    # 本次命令的提示（如「已降级为待查」），不参与数据语义。
    notice: str | None = None


class SnapshotDiffEntry(Contract):
    target: Literal["entity", "relation", "source", "group", "layout"]
    id: str
    change: Literal["added", "removed", "changed"]
    before: dict[str, Any] | None
    after: dict[str, Any] | None


class SnapshotLabelDrift(Contract):
    source_id: str
    frozen: str
    current: str | None


class SnapshotDiffOut(Contract):
    base: str
    target: str
    entries: list[SnapshotDiffEntry]
    label_drift: list[SnapshotLabelDrift]


class RemovalImpactOut(Contract):
    graphs: list[GraphOut]
    count: int


class RemovalImpactQuery(Contract):
    """GET query for the graph-removal impact preview."""

    entity_id: UUID | None = None
    relation_id: UUID | None = None


class SnapshotListOutput(Contract):
    snapshots: list[SnapshotOut]


class SnapshotDiffQuery(Contract):
    """GET query: omit target_id to compare the snapshot against the draft."""

    target_id: UUID | None = None


class ImportDraftRow(Contract):
    """Preview row parsed from an external DOCX/XLSX/CSV catalog.

    Material names are matched by the caller; the commit endpoint only accepts
    confirmed material ids (``ImportCatalogRow``).
    """

    group_title: str
    item_title: str
    item_purpose: str | None = None
    material_title: str | None = None
    page_start: int | None = None
    page_end: int | None = None


class ImportPreviewOutput(Contract):
    rows: list[ImportDraftRow]
    warnings: list[str] = Field(default_factory=list)


# --- graph write inputs -------------------------------------------------------

class AddEntityInput(CaseInput):
    kind: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=200)
    roles: list[str] = Field(default_factory=list, max_length=50)
    note: str | None = Field(default=None, max_length=4000)
    graph_id: UUID | None = None
    request_key: str | None = Field(default=None, max_length=200)


class EntityChanges(Contract):
    kind: str | None = None
    name: str | None = Field(default=None, max_length=200)
    roles: list[str] | None = Field(default=None, max_length=50)
    note: str | None = Field(default=None, max_length=4000)


class UpdateEntityInput(CaseInput):
    entity_id: UUID
    expected_revision: int = Field(ge=0)
    changes: EntityChanges


class EntityRevisionInput(CaseInput):
    entity_id: UUID
    expected_revision: int = Field(ge=0)


class AddAliasInput(CaseInput):
    entity_id: UUID
    alias: str = Field(min_length=1, max_length=200)


class RemoveAliasInput(CaseInput):
    alias_id: UUID


class AddGraphRelationInput(CaseInput):
    """关系图的关系新增（与进度域 AddRelationInput 区分，避免同名覆盖）。"""

    from_entity_id: UUID
    to_entity_id: UUID
    kind: str = Field(min_length=1)
    label: str | None = Field(default=None, max_length=200)
    credibility: Credibility
    directed: bool = True
    attrs: RelationAttrs = Field(default_factory=RelationAttrs)
    note: str | None = Field(default=None, max_length=4000)
    graph_id: UUID | None = None
    request_key: str | None = Field(default=None, max_length=200)


class RelationChanges(Contract):
    kind: str | None = None
    label: str | None = Field(default=None, max_length=200)
    credibility: Credibility | None = None
    directed: bool | None = None
    attrs: RelationAttrs | None = None
    note: str | None = Field(default=None, max_length=4000)


class UpdateRelationInput(CaseInput):
    relation_id: UUID
    expected_revision: int = Field(ge=0)
    changes: RelationChanges


class RelationRevisionInput(CaseInput):
    relation_id: UUID
    expected_revision: int = Field(ge=0)


class AddSourceInput(CaseInput):
    entity_id: UUID | None = None
    relation_id: UUID | None = None
    ref: SourceRefInput
    proves: str | None = Field(default=None, max_length=500)
    request_key: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def exactly_one_target(self):
        if sum(v is not None for v in (self.entity_id, self.relation_id)) != 1:
            raise ValueError("出处必须且只能挂在一个主体或一条关系上")
        return self


class RemoveSourceInput(CaseInput):
    source_id: UUID
    downgrade_to: Credibility | None = None


class CreateGraphInput(CaseInput):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    request_key: str | None = Field(default=None, max_length=200)


class GraphChanges(Contract):
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class UpdateGraphInput(CaseInput):
    graph_id: UUID
    expected_revision: int = Field(ge=0)
    changes: GraphChanges


class GraphRevisionInput(CaseInput):
    graph_id: UUID
    expected_revision: int = Field(ge=0)


class AddToGraphInput(CaseInput):
    graph_id: UUID
    entity_ids: list[UUID] = Field(default_factory=list)
    relation_ids: list[UUID] = Field(default_factory=list)


class RemoveFromGraphInput(CaseInput):
    graph_id: UUID
    entity_id: UUID | None = None
    relation_id: UUID | None = None

    @model_validator(mode="after")
    def exactly_one(self):
        if sum(v is not None for v in (self.entity_id, self.relation_id)) != 1:
            raise ValueError("一次只能移除一个主体或一条关系")
        return self


class LayoutNode(Contract):
    node_id: UUID
    x: float = Field(ge=-1_000_000, le=1_000_000)
    y: float = Field(ge=-1_000_000, le=1_000_000)


class LayoutGroup(Contract):
    group_id: UUID
    x: float = Field(ge=-1_000_000, le=1_000_000)
    y: float = Field(ge=-1_000_000, le=1_000_000)
    width: float | None = Field(default=None, gt=0, le=1_000_000)
    height: float | None = Field(default=None, gt=0, le=1_000_000)


class SaveLayoutInput(CaseInput):
    graph_id: UUID
    nodes: list[LayoutNode] = Field(default_factory=list, max_length=500)
    groups: list[LayoutGroup] = Field(default_factory=list, max_length=200)


class SetNodeGroupInput(CaseInput):
    graph_id: UUID
    entity_id: UUID
    group_id: UUID | None = None


class UpsertGroupInput(CaseInput):
    graph_id: UUID
    group_id: UUID | None = None
    expected_revision: int | None = Field(default=None, ge=0)
    title: str = Field(min_length=1, max_length=200)
    color: str | None = Field(default=None, max_length=40)
    x: float = Field(default=0, ge=-1_000_000, le=1_000_000)
    y: float = Field(default=0, ge=-1_000_000, le=1_000_000)
    width: float = Field(default=360, gt=0, le=1_000_000)
    height: float = Field(default=220, gt=0, le=1_000_000)
    collapsed: bool = False
    entity_ids: list[UUID] = Field(default_factory=list, max_length=500)


class RemoveGroupInput(CaseInput):
    group_id: UUID
    expected_revision: int = Field(ge=0)


class CreateSnapshotInput(CaseInput):
    graph_id: UUID
    stage_label: str = Field(min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=2000)
    progress_node_id: UUID | None = None
    # 有未确认 Agent 变更时，须显式确认「按已确认状态继续冻结」。
    acknowledge_pending: bool = False


class RestoreSnapshotInput(CaseInput):
    snapshot_id: UUID


class AddConsentInput(CaseInput):
    snapshot_id: UUID
    action: ConsentAction
    parent_consent_id: UUID | None = None
    consenter: str = Field(min_length=1, max_length=200)
    consented_at: datetime
    channel: str = Field(min_length=1)
    source_detail: str = Field(min_length=1, max_length=1000)
    evidence_material_id: UUID | None = None
    note: str | None = Field(default=None, max_length=2000)


class GraphChangeIdInput(CaseInput):
    change_id: UUID


class GraphAcceptAllInput(CaseInput):
    change_ids: list[UUID] = Field(default_factory=list)
    expected_seq: int | None = Field(default=None, ge=0)


# --- evidence catalog inputs --------------------------------------------------

class AddEvidenceGroupInput(CaseInput):
    title: str = Field(min_length=1, max_length=200)
    purpose: str | None = Field(default=None, max_length=1000)
    position: int | None = Field(default=None, ge=0)
    request_key: str | None = Field(default=None, max_length=200)


class EvidenceGroupChanges(Contract):
    title: str | None = Field(default=None, max_length=200)
    purpose: str | None = Field(default=None, max_length=1000)


class UpdateEvidenceGroupInput(CaseInput):
    group_id: UUID
    expected_revision: int = Field(ge=0)
    changes: EvidenceGroupChanges


class ReorderEvidenceGroupsInput(CaseInput):
    group_ids: list[UUID] = Field(min_length=1)


class EvidenceGroupRevisionInput(CaseInput):
    group_id: UUID
    expected_revision: int = Field(ge=0)


class AddEvidenceItemInput(CaseInput):
    group_id: UUID
    title: str = Field(min_length=1, max_length=300)
    purpose: str | None = Field(default=None, max_length=2000)
    position: int | None = Field(default=None, ge=0)
    request_key: str | None = Field(default=None, max_length=200)


class EvidenceItemChanges(Contract):
    title: str | None = Field(default=None, max_length=300)
    purpose: str | None = Field(default=None, max_length=2000)


class UpdateEvidenceItemInput(CaseInput):
    item_id: UUID
    expected_revision: int = Field(ge=0)
    changes: EvidenceItemChanges


class MoveEvidenceItemInput(CaseInput):
    item_id: UUID
    to_group_id: UUID
    before_item_id: UUID | None = None


class EvidenceItemRevisionInput(CaseInput):
    item_id: UUID
    expected_revision: int = Field(ge=0)


class AddEvidenceBindingInput(CaseInput):
    item_id: UUID
    material_id: UUID
    page_start: int | None = Field(default=None, ge=1, le=100000)
    page_end: int | None = Field(default=None, ge=1, le=100000)
    request_key: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def ordered_pages(self):
        if self.page_end is not None and self.page_start is None:
            raise ValueError("指定结束页时必须同时指定开始页")
        if self.page_start is not None and self.page_end is not None and self.page_end < self.page_start:
            raise ValueError("结束页不能早于开始页")
        return self


class RemoveEvidenceBindingInput(CaseInput):
    binding_id: UUID


class EvidenceBindingRevisionInput(CaseInput):
    binding_id: UUID
    expected_revision: int = Field(ge=0)


class ImportBindingRow(Contract):
    material_id: UUID
    page_start: int | None = Field(default=None, ge=1, le=100000)
    page_end: int | None = Field(default=None, ge=1, le=100000)


class ImportCatalogRow(Contract):
    group_title: str = Field(min_length=1, max_length=200)
    group_purpose: str | None = Field(default=None, max_length=1000)
    item_title: str = Field(min_length=1, max_length=300)
    item_purpose: str | None = Field(default=None, max_length=2000)
    bindings: list[ImportBindingRow] = Field(default_factory=list, max_length=50)


class ImportEvidenceCatalogInput(CaseInput):
    """外部 Word/Excel 目录导入：只生成待确认草稿，不参与现行编号。"""

    import_material_id: UUID
    rows: list[ImportCatalogRow] = Field(min_length=1, max_length=500)
    request_key: str | None = Field(default=None, max_length=200)


class EvidenceChangeIdInput(CaseInput):
    change_id: UUID


class EvidenceAcceptAllInput(CaseInput):
    change_ids: list[UUID] = Field(default_factory=list)
    expected_seq: int | None = Field(default=None, ge=0)


class GraphRulesOutput(Contract):
    rules: dict[str, Any]


class GraphRulesQuery(Contract):
    """GET /graph/rules takes no input; placeholder for the protocol generator."""
    pass


# --- 诉请金额协议（P2）-------------------------------------------------------

ClaimCategory = Literal["principal", "interest", "liquidated_damages", "expense",
                        "deduction", "other"]
ClaimActor = ProgressActor
ClaimReviewState = ProgressReviewState
ClaimDirection = Literal["up", "down"]

# 存储是 BIGINT，但金额要经 JSON 交给浏览器：超过 JS 安全整数就无法精确表示，
# 因此在协议边界就拒绝，而不是让前端悄悄丢分。约 90 万亿元。
CLAIM_MAX_CENTS = 2**53 - 1

# 请求字段严格整数分：Pydantic 默认会把 true 转成 1、把 "100" 转成 100，静默
# 改变含义。金额只接受真正的 int（且拒绝 bool），与数据库 CHECK 的上限一致。
StrictCents = Annotated[int, Field(strict=True, ge=0, le=CLAIM_MAX_CENTS)]


class ClaimGroupOut(Contract):
    id: UUID
    case_id: UUID
    title: str
    currency: str
    include_in_total: bool
    position: int
    note: str | None
    created_by: ClaimActor
    updated_by: ClaimActor
    request_key: str | None
    removed_at: UtcDatetime | None
    revision: int = Field(ge=0)
    created_at: UtcDatetime
    updated_at: UtcDatetime


class ClaimItemOut(Contract):
    id: UUID
    case_id: UUID
    group_id: UUID
    title: str
    category: ClaimCategory
    # 整数分；抵扣方向由 category 决定，不存负数。
    amount_cents: int = Field(ge=0)
    calc_note: str | None
    segment_id: UUID | None
    material_id: UUID | None
    annotation_id: UUID | None
    position: int
    created_by: ClaimActor
    updated_by: ClaimActor
    request_key: str | None
    removed_at: UtcDatetime | None
    revision: int = Field(ge=0)
    created_at: UtcDatetime
    updated_at: UtcDatetime


class ClaimDiffEntry(Contract):
    target: Literal["group", "item"]
    id: UUID
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    before_rev: int | None
    after_rev: int | None


class ClaimChangeOut(Contract):
    id: UUID
    case_id: UUID
    seq: int
    command: str
    actor: ClaimActor
    entry: str
    diff: list[ClaimDiffEntry]
    review_state: ClaimReviewState
    reviewed_at: UtcDatetime | None
    reverted_by: UUID | None
    request_key: str | None
    created_at: UtcDatetime


class ClaimCurrencyTotal(Contract):
    """同一币种内、勾选“计入全案总计”的分组小计之和；不同币种不混算。"""

    currency: str
    total_cents: int
    included_group_ids: list[UUID]


class ClaimSnapshot(Contract):
    case_id: UUID
    seq: int
    # 含软删除行：前端按 removed_at 过滤，恢复金额项需要原始行。
    groups: list[ClaimGroupOut]
    items: list[ClaimItemOut]
    group_subtotals: dict[str, int]
    totals_by_currency: list[ClaimCurrencyTotal]
    pending_changes: list[ClaimChangeOut]
    changes: list[ClaimChangeOut]
    pending_count: int
    excluded_group_count: int
    included_group_count: int
    last_change_id: UUID | None = None


class ClaimAmountCandidate(Contract):
    """本案 exact_values(kind='amount') 的一条候选，出处只给稳定引用。"""

    exact_value_id: UUID
    segment_id: UUID
    material_id: UUID
    material_title: str
    page: int | None
    locator: dict[str, Any]
    raw_text: str
    currency: str
    # 超过两位小数时无法精确到分，返回 None，由人工录入。
    amount_cents: int | None
    excerpt: str
    span: dict[str, Any]


class ClaimAmountCandidates(Contract):
    case_id: UUID
    candidates: list[ClaimAmountCandidate]


class ClaimCandidatesQuery(Contract):
    """GET 候选金额检索的查询参数（query 为空时返回全部候选）。"""

    query: str | None = Field(default=None, max_length=200)


class ClaimGroupChanges(Contract):
    """字段三态：未提供＝不动；显式 null＝清空 note；值＝设置。"""

    title: str | None = Field(default=None, max_length=200)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    include_in_total: bool | None = None
    note: str | None = Field(default=None, max_length=2000)


class AddClaimGroupInput(CaseInput):
    title: str = Field(min_length=1, max_length=200)
    currency: str = Field(default="CNY", min_length=3, max_length=3)
    # 新建默认计入全案总计（重叠程序如一审/二审由人工取消勾选）。
    include_in_total: bool = True
    note: str | None = Field(default=None, max_length=2000)
    request_key: str | None = None


class UpdateClaimGroupInput(CaseInput):
    group_id: UUID
    expected_revision: int = Field(ge=0)
    changes: ClaimGroupChanges
    # Agent 提交必须带幂等键；同一键重试返回原变更，不重复写入。
    request_key: str | None = None


class ClaimGroupRevisionInput(CaseInput):
    group_id: UUID
    expected_revision: int = Field(ge=0)


class ReorderClaimGroupInput(CaseInput):
    group_id: UUID
    expected_revision: int = Field(ge=0)
    direction: ClaimDirection


class AddClaimItemInput(CaseInput):
    group_id: UUID
    title: str = Field(min_length=1, max_length=200)
    category: ClaimCategory
    amount_cents: StrictCents
    calc_note: str | None = Field(default=None, max_length=2000)
    segment_id: UUID | None = None
    material_id: UUID | None = None
    annotation_id: UUID | None = None
    request_key: str | None = None


class ClaimItemChanges(Contract):
    """出处三选一：提供任一字段即整体替换出处（其余置空）；都不提供则不动。"""

    title: str | None = Field(default=None, max_length=200)
    category: ClaimCategory | None = None
    amount_cents: StrictCents | None = None
    calc_note: str | None = Field(default=None, max_length=2000)
    segment_id: UUID | None = None
    material_id: UUID | None = None
    annotation_id: UUID | None = None


class UpdateClaimItemInput(CaseInput):
    item_id: UUID
    expected_revision: int = Field(ge=0)
    changes: ClaimItemChanges
    # Agent 提交必须带幂等键；同一键重试返回原变更，不重复写入。
    request_key: str | None = None


class ClaimItemRevisionInput(CaseInput):
    item_id: UUID
    expected_revision: int = Field(ge=0)


class ReorderClaimItemInput(CaseInput):
    item_id: UUID
    expected_revision: int = Field(ge=0)
    direction: ClaimDirection


class ClaimAcceptAllInput(CaseInput):
    """与进度一致：只确认客户端已展示的条目，或提交打开确认框时的序号。"""

    change_ids: list[UUID] = Field(default_factory=list)
    expected_seq: int | None = Field(default=None, ge=0)
