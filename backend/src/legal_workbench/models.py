"""SQLAlchemy models implementing the fixed data rules in section 2.4.

Case-scoped tables carry composite foreign keys on (case_id, ...) so that the
database itself rejects cross-case references.  Mutable rows use a revision
column; updates are CAS via db.update_with_revision.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    ForeignKey,
    CheckConstraint,
    DateTime,
    Double,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .schemas import CLAIM_MAX_CENTS

INTAKE_STATUSES = ("pending", "copying", "published", "ready", "failed", "skipped")
JOB_STATUSES = ("queued", "running", "succeeded", "partial", "failed")
PARSE_UNIT_STATUSES = (
    "pending", "extracted", "ocr_done", "no_text_detected", "failed", "unsupported",
)
PARSE_RUN_STATUSES = ("queued", "running", "complete", "partial", "failed", "unsupported")
REVIEW_STATUSES = ("pending", "approved", "rejected")
STATEMENT_KINDS = ("excerpt", "party_statement", "agent_analysis", "human_opinion")
EXACT_VALUE_KINDS = ("literal", "identifier", "amount", "date")
INTEGRITY_STATUSES = ("ok", "integrity_error")
TIME_SOURCES = ("timer", "manual")
CASE_WORK_STATUSES = ("normal", "need_materials", "need_sync", "waiting_external", "paused")


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def enum_check(column: str, values: tuple[str, ...], name: str) -> CheckConstraint:
    joined = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({joined})", name=name)


class Case(Base):
    __tablename__ = "cases"
    __table_args__ = (
        enum_check("work_status", CASE_WORK_STATUSES, "ck_cases_work_status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    case_number: Mapped[str | None] = mapped_column(Text)
    plaintiff: Mapped[str | None] = mapped_column(Text)
    defendant: Mapped[str | None] = mapped_column(Text)
    work_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sa_text("'normal'"),
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    stage: Mapped[str | None] = mapped_column(Text)
    next_action: Mapped[str | None] = mapped_column(Text)
    deadlines: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=sa_text("'[]'::jsonb"))
    # 结案归档：非空即已归档，进度与材料写入在服务层被拒。
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Intake(Base):
    __tablename__ = "intakes"
    __table_args__ = (
        ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_intakes_case"),
        UniqueConstraint("case_id", "request_key", name="uq_intakes_case_request_key"),
        UniqueConstraint("case_id", "id", name="uq_intakes_case_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    request_key: Mapped[str] = mapped_column(Text, nullable=False)
    source_label: Mapped[str] = mapped_column(Text, nullable=False)
    source_root: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class IntakeItem(Base):
    __tablename__ = "intake_items"
    __table_args__ = (
        ForeignKeyConstraint(["case_id", "intake_id"], ["intakes.case_id", "intakes.id"],
                             name="fk_intake_items_intake"),
        ForeignKeyConstraint(["case_id", "material_id"], ["materials.case_id", "materials.id"],
                             name="fk_intake_items_material", deferrable=True, initially="DEFERRED"),
        ForeignKeyConstraint(["case_id", "stored_object_id"], ["stored_objects.case_id", "stored_objects.id"],
                             name="fk_intake_items_stored_object"),
        UniqueConstraint("intake_id", "relative_source", name="uq_intake_items_source"),
        UniqueConstraint("case_id", "id", name="uq_intake_items_case_id"),
        enum_check("status", INTAKE_STATUSES, "ck_intake_items_status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    intake_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    relative_source: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'pending'"))
    material_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    stored_object_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class StoredObject(Base):
    __tablename__ = "stored_objects"
    __table_args__ = (
        ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_stored_objects_case"),
        UniqueConstraint("case_id", "sha256", name="uq_stored_objects_case_sha"),
        UniqueConstraint("case_id", "id", name="uq_stored_objects_case_id"),
        enum_check("integrity_status", INTEGRITY_STATUSES, "ck_stored_objects_integrity"),
        CheckConstraint("size >= 0", name="ck_stored_objects_size"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    integrity_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sa_text("'ok'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Material(Base):
    __tablename__ = "materials"
    __table_args__ = (
        ForeignKeyConstraint(["case_id", "intake_item_id"], ["intake_items.case_id", "intake_items.id"],
                             name="fk_materials_intake_item"),
        ForeignKeyConstraint(["case_id", "original_file_version_id"],
                             ["file_versions.case_id", "file_versions.id"],
                             name="fk_materials_original_version", deferrable=True, initially="DEFERRED"),
        UniqueConstraint("case_id", "id", name="uq_materials_case_id"),
        UniqueConstraint("intake_item_id", name="uq_materials_intake_item"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    intake_item_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    acquisition: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'待分类'"))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    original_file_version_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class FileVersion(Base):
    __tablename__ = "file_versions"
    __table_args__ = (
        ForeignKeyConstraint(["case_id", "material_id"], ["materials.case_id", "materials.id"],
                             name="fk_file_versions_material"),
        ForeignKeyConstraint(["case_id", "stored_object_id"], ["stored_objects.case_id", "stored_objects.id"],
                             name="fk_file_versions_stored_object"),
        ForeignKeyConstraint(["case_id", "material_id", "parent_version_id"],
                             ["file_versions.case_id", "file_versions.material_id", "file_versions.id"],
                             name="fk_file_versions_parent"),
        ForeignKeyConstraint(["case_id", "active_parse_run_id"], ["parse_runs.case_id", "parse_runs.id"],
                             name="fk_file_versions_active_parse_run", deferrable=True, initially="DEFERRED"),
        UniqueConstraint("case_id", "id", name="uq_file_versions_case_id"),
        UniqueConstraint("case_id", "material_id", "id", name="uq_file_versions_material_id"),
        CheckConstraint("kind IN ('original', 'working')", name="ck_file_versions_kind"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    material_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    stored_object_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    active_parse_run_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WorkingCopy(Base):
    __tablename__ = "working_copies"
    __table_args__ = (
        ForeignKeyConstraint(["case_id", "material_id", "base_version_id"],
                             ["file_versions.case_id", "file_versions.material_id", "file_versions.id"],
                             name="fk_working_copies_base_version"),
        UniqueConstraint("case_id", "id", name="uq_working_copies_case_id"),
        CheckConstraint("revision >= 0", name="ck_working_copies_revision"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    material_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    base_version_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ParseRun(Base):
    __tablename__ = "parse_runs"
    __table_args__ = (
        ForeignKeyConstraint(["case_id", "file_version_id"], ["file_versions.case_id", "file_versions.id"],
                             name="fk_parse_runs_file_version"),
        UniqueConstraint("case_id", "id", name="uq_parse_runs_case_id"),
        UniqueConstraint("case_id", "file_version_id", "sequence", name="uq_parse_runs_sequence"),
        enum_check("status", PARSE_RUN_STATUSES, "ck_parse_runs_status"),
        CheckConstraint("sequence >= 0", name="ck_parse_runs_sequence"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    file_version_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    parser_version: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa_text("'{}'::jsonb")
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'queued'"))
    coverage: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa_text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ParseUnit(Base):
    __tablename__ = "parse_units"
    __table_args__ = (
        ForeignKeyConstraint(["case_id", "parse_run_id"], ["parse_runs.case_id", "parse_runs.id"],
                             name="fk_parse_units_run"),
        UniqueConstraint("parse_run_id", "unit_key", name="uq_parse_units_unit_key"),
        UniqueConstraint("case_id", "id", name="uq_parse_units_case_id"),
        enum_check("status", PARSE_UNIT_STATUSES, "ck_parse_units_status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    parse_run_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    unit_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'pending'"))
    method: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    warnings: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=sa_text("'[]'::jsonb"))


class Segment(Base):
    __tablename__ = "segments"
    __table_args__ = (
        ForeignKeyConstraint(["case_id", "parse_run_id"], ["parse_runs.case_id", "parse_runs.id"],
                             name="fk_segments_run"),
        ForeignKeyConstraint(["case_id", "parse_unit_id"], ["parse_units.case_id", "parse_units.id"],
                             name="fk_segments_unit"),
        UniqueConstraint("case_id", "id", name="uq_segments_case_id"),
        Index("ix_segments_run", "parse_run_id"),
        Index(
            "ix_segments_normalized_trgm",
            "normalized_text",
            postgresql_using="gin",
            postgresql_ops={"normalized_text": "gin_trgm_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    parse_run_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    parse_unit_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    locator: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False)
    offset_map: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=sa_text("'[]'::jsonb"))


class ExactValue(Base):
    __tablename__ = "exact_values"
    __table_args__ = (
        ForeignKeyConstraint(["case_id", "segment_id"], ["segments.case_id", "segments.id"],
                             name="fk_exact_values_segment"),
        UniqueConstraint("case_id", "id", name="uq_exact_values_case_id"),
        enum_check("kind", EXACT_VALUE_KINDS, "ck_exact_values_kind"),
        Index("ix_exact_values_amount", "case_id", "kind", "currency", "amount_value"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    segment_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_value: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa_text("'{}'::jsonb")
    )
    amount_value: Mapped[Decimal | None] = mapped_column(Numeric(24, 6))
    currency: Mapped[str | None] = mapped_column(Text)
    span: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=sa_text("'{}'::jsonb"))


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_jobs_case"),
        UniqueConstraint("case_id", "id", name="uq_jobs_case_id"),
        enum_check("status", JOB_STATUSES, "ck_jobs_status"),
        Index("ix_jobs_case_status", "case_id", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'queued'"))
    completed_units: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    total_units: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    error: Mapped[str | None] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Analysis(Base):
    __tablename__ = "analyses"
    __table_args__ = (
        ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_analyses_case"),
        ForeignKeyConstraint(["case_id", "current_version_id"],
                             ["analysis_versions.case_id", "analysis_versions.id"],
                             name="fk_analyses_current_version", deferrable=True, initially="DEFERRED"),
        UniqueConstraint("case_id", "id", name="uq_analyses_case_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AnalysisVersion(Base):
    __tablename__ = "analysis_versions"
    __table_args__ = (
        ForeignKeyConstraint(["case_id", "analysis_id"], ["analyses.case_id", "analyses.id"],
                             name="fk_analysis_versions_analysis"),
        UniqueConstraint("case_id", "id", name="uq_analysis_versions_case_id"),
        UniqueConstraint("case_id", "analysis_id", "id", name="uq_analysis_versions_analysis_id"),
        enum_check("statement_kind", STATEMENT_KINDS, "ck_analysis_versions_kind"),
        enum_check("review_status", REVIEW_STATUSES, "ck_analysis_versions_review"),
        Index(
            "ix_analysis_versions_content_trgm",
            "content",
            postgresql_using="gin",
            postgresql_ops={"content": "gin_trgm_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    analysis_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    statement_kind: Mapped[str] = mapped_column(Text, nullable=False)
    source_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=sa_text("'[]'::jsonb"))
    review_status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'pending'"))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Annotation(Base):
    __tablename__ = "annotations"
    __table_args__ = (
        ForeignKeyConstraint(["case_id", "file_version_id"],
                             ["file_versions.case_id", "file_versions.id"],
                             name="fk_annotations_file_version"),
        UniqueConstraint("case_id", "id", name="uq_annotations_case_id"),
        enum_check("author", ("agent", "human"), "ck_annotations_author"),
        enum_check("kind", ("transcription", "interpretation"), "ck_annotations_kind"),
        CheckConstraint("page IS NULL OR page >= 1", name="ck_annotations_page"),
        CheckConstraint(
            "(revision = 0 AND reviewed_text IS NULL) OR "
            "(author = 'agent' AND revision = 1 AND reviewed_text IS NOT NULL)",
            name="ck_annotations_review"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id"), index=True)
    file_version_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True))
    page: Mapped[int | None] = mapped_column(Integer)
    author: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    reviewed_text: Mapped[str | None] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, server_default=sa_text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    request_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_audit_events_case"),
        Index("ix_audit_events_case_created", "case_id", "created_at"),
        CheckConstraint(
            "operation IN ('session.login', 'session.logout', 'case.create', "
            "'intake.import', 'intake.upload', 'material.classify', 'material.rename', "
            "'material.working_copy', 'file_version.register', 'parse.reparse', "
            "'analysis.save', 'analysis.review', 'backup.create', 'backup.restore', "
            "'maintenance.clear', 'progress.add_node', 'progress.update_node', "
            "'progress.move_node', 'progress.set_active_stage', 'progress.insert_fragment', "
            "'progress.remove_node', 'progress.restore_node', 'progress.add_relation', "
            "'progress.remove_relation', 'progress.link', 'progress.unlink', "
            "'progress.accept_change', 'progress.accept_all', 'progress.revert_change', "
            "'case.archive', 'case.unarchive', 'time.start', 'time.stop', "
            "'time.add', 'time.remove', "
            "'graph.add_entity', 'graph.update_entity', 'graph.remove_entity', "
            "'graph.restore_entity', 'graph.add_alias', 'graph.remove_alias', "
            "'graph.add_relation', 'graph.update_relation', 'graph.remove_relation', "
            "'graph.restore_relation', 'graph.add_source', 'graph.remove_source', "
            "'graph.create_graph', 'graph.update_graph', 'graph.remove_graph', "
            "'graph.add_to_graph', 'graph.remove_from_graph', 'graph.save_layout', "
            "'graph.upsert_group', 'graph.remove_group', 'graph.create_snapshot', "
            "'graph.restore_snapshot', 'graph.add_snapshot_consent', "
            "'graph.accept_change', 'graph.accept_all', 'graph.revert_change', "
            "'evidence.add_group', 'evidence.update_group', 'evidence.reorder_groups', "
            "'evidence.remove_group', 'evidence.add_item', 'evidence.update_item', "
            "'evidence.move_item', 'evidence.remove_item', 'evidence.add_binding', "
            "'evidence.remove_binding', 'evidence.confirm_item', 'evidence.reject_item', "
            "'evidence.confirm_binding', 'evidence.reject_binding', "
            "'evidence.import_catalog', 'evidence.accept_change', 'evidence.accept_all', "
            "'evidence.revert_change', "
            "'claims.add_group', 'claims.update_group', 'claims.reorder_groups', "
            "'claims.remove_group', 'claims.add_item', 'claims.update_item', "
            "'claims.reorder_items', 'claims.remove_item', 'claims.restore_item', "
            "'claims.accept_change', 'claims.accept_all', 'claims.revert_change')",
            name="ck_audit_events_operation"),
    )


class MaintenanceState(Base):
    __tablename__ = "maintenance_state"
    __table_args__ = (CheckConstraint("id = 1", name="ck_maintenance_singleton"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, server_default=sa_text("1"))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sa_text("false"))
    reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# --- 办案进度（0005_case_progress）-------------------------------------------
# 通用节点树 + 类型化关系 + 业务关联 + 变更记录 + 案件级变化序号。
# 层级、种类、状态、关系类型、模板片段全部由 progress_rules 配置校验，
# 数据库只保证非空、组合外键（同案隔离）与少量结构约束。

PROGRESS_ACTORS = ("human", "agent")
PROGRESS_REVIEW_STATES = ("none", "pending", "accepted", "reverted")


class ProgressNode(Base):
    __tablename__ = "progress_nodes"
    __table_args__ = (
        ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_progress_nodes_case"),
        ForeignKeyConstraint(["case_id", "parent_id"], ["progress_nodes.case_id", "progress_nodes.id"],
                             name="fk_progress_nodes_parent"),
        UniqueConstraint("case_id", "id", name="uq_progress_nodes_case_id"),
        UniqueConstraint("case_id", "request_key", name="uq_progress_nodes_request_key"),
        enum_check("created_by", PROGRESS_ACTORS, "ck_progress_nodes_created_by"),
        enum_check("updated_by", PROGRESS_ACTORS, "ck_progress_nodes_updated_by"),
        CheckConstraint("char_length(title) BETWEEN 1 AND 200", name="ck_progress_nodes_title"),
        CheckConstraint("note IS NULL OR char_length(note) <= 4000", name="ck_progress_nodes_note"),
        CheckConstraint("position >= 0", name="ck_progress_nodes_position"),
        CheckConstraint("revision >= 0", name="ck_progress_nodes_revision"),
        Index("ix_progress_nodes_case_parent", "case_id", "parent_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    date_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    date_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    template_ref: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)
    request_key: Mapped[str | None] = mapped_column(Text)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())


class ProgressRelation(Base):
    __tablename__ = "progress_relations"
    __table_args__ = (
        ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_progress_relations_case"),
        ForeignKeyConstraint(["case_id", "from_node_id"], ["progress_nodes.case_id", "progress_nodes.id"],
                             name="fk_progress_relations_from"),
        ForeignKeyConstraint(["case_id", "to_node_id"], ["progress_nodes.case_id", "progress_nodes.id"],
                             name="fk_progress_relations_to"),
        UniqueConstraint("case_id", "id", name="uq_progress_relations_case_id"),
        enum_check("created_by", PROGRESS_ACTORS, "ck_progress_relations_created_by"),
        CheckConstraint("from_node_id <> to_node_id", name="ck_progress_relations_not_self"),
        # 审查 06：同案同向同类型的有效关系唯一（部分索引只约束未删除行）。
        Index("uq_progress_relations_active", "case_id", "from_node_id", "to_node_id", "type",
              unique=True, postgresql_where=sa_text("removed_at IS NULL")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    from_node_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    to_node_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())


class ProgressLink(Base):
    __tablename__ = "progress_links"
    __table_args__ = (
        ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_progress_links_case"),
        ForeignKeyConstraint(["case_id", "node_id"], ["progress_nodes.case_id", "progress_nodes.id"],
                             name="fk_progress_links_node"),
        ForeignKeyConstraint(["case_id", "material_id"], ["materials.case_id", "materials.id"],
                             name="fk_progress_links_material"),
        ForeignKeyConstraint(["case_id", "analysis_id"], ["analyses.case_id", "analyses.id"],
                             name="fk_progress_links_analysis"),
        ForeignKeyConstraint(["case_id", "annotation_id"], ["annotations.case_id", "annotations.id"],
                             name="fk_progress_links_annotation"),
        UniqueConstraint("case_id", "id", name="uq_progress_links_case_id"),
        enum_check("created_by", PROGRESS_ACTORS, "ck_progress_links_created_by"),
        CheckConstraint(
            "num_nonnulls(material_id, analysis_id, annotation_id) = 1",
            name="ck_progress_links_one_target"),
        # 审查 06：同节点同对象唯一（三个部分索引，各自只约束对应目标非空的行）。
        Index("uq_progress_links_material", "node_id", "material_id",
              unique=True, postgresql_where=sa_text("material_id IS NOT NULL")),
        Index("uq_progress_links_analysis", "node_id", "analysis_id",
              unique=True, postgresql_where=sa_text("analysis_id IS NOT NULL")),
        Index("uq_progress_links_annotation", "node_id", "annotation_id",
              unique=True, postgresql_where=sa_text("annotation_id IS NOT NULL")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    node_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    material_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    analysis_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    annotation_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())


class ProgressChange(Base):
    __tablename__ = "progress_changes"
    __table_args__ = (
        ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_progress_changes_case"),
        UniqueConstraint("case_id", "id", name="uq_progress_changes_case_id"),
        UniqueConstraint("case_id", "seq", name="uq_progress_changes_seq"),
        enum_check("actor", PROGRESS_ACTORS, "ck_progress_changes_actor"),
        enum_check("review_state", PROGRESS_REVIEW_STATES, "ck_progress_changes_review"),
        Index("ix_progress_changes_case_review", "case_id", "review_state"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    command: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    entry: Mapped[str] = mapped_column(Text, nullable=False)
    diff: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    review_state: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'none'"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reverted_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())


class CaseChangeSeq(Base):
    __tablename__ = "case_change_seq"
    __table_args__ = (
        ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_case_change_seq_case"),
    )

    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    domain: Mapped[str] = mapped_column(Text, primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=sa_text("0"))


# --- 诉请金额（0008_claim_amounts）-------------------------------------------
# 金额一律整数分（BIGINT, >= 0）；加减方向由 category 决定（deduction 减）。
# Agent 提交先落在 claim_changes（pending），人工确认后才写入业务表，
# 因此待确认候选不会进入小计与按币种总计。

CLAIM_CATEGORIES = (
    "principal", "interest", "liquidated_damages", "expense", "deduction", "other",
)
CLAIM_REVIEW_STATES = PROGRESS_REVIEW_STATES


class ClaimGroup(Base):
    __tablename__ = "claim_groups"
    __table_args__ = (
        ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_claim_groups_case"),
        UniqueConstraint("case_id", "id", name="uq_claim_groups_case_id"),
        UniqueConstraint("case_id", "request_key", name="uq_claim_groups_request_key"),
        enum_check("created_by", PROGRESS_ACTORS, "ck_claim_groups_created_by"),
        enum_check("updated_by", PROGRESS_ACTORS, "ck_claim_groups_updated_by"),
        CheckConstraint("char_length(title) BETWEEN 1 AND 200", name="ck_claim_groups_title"),
        CheckConstraint("note IS NULL OR char_length(note) <= 2000", name="ck_claim_groups_note"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_claim_groups_currency"),
        CheckConstraint("position >= 0", name="ck_claim_groups_position"),
        CheckConstraint("revision >= 0", name="ck_claim_groups_revision"),
        Index("ix_claim_groups_case_position", "case_id", "position"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'CNY'"))
    include_in_total: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("true"))
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)
    request_key: Mapped[str | None] = mapped_column(Text)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())


class ClaimItem(Base):
    __tablename__ = "claim_items"
    __table_args__ = (
        ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_claim_items_case"),
        ForeignKeyConstraint(["case_id", "group_id"],
                             ["claim_groups.case_id", "claim_groups.id"],
                             name="fk_claim_items_group"),
        # 引用不可变：片段是金额出处的稳定锚点，删除它会让已确认金额静默失去
        # 出处。正常重解析新建运行、只重写未完成的工作单元，不删除已发布段；
        # 因此这里用默认 RESTRICT，任何会销毁出处的删除都必须先显式处理引用。
        ForeignKeyConstraint(["case_id", "segment_id"], ["segments.case_id", "segments.id"],
                             name="fk_claim_items_segment"),
        ForeignKeyConstraint(["case_id", "material_id"], ["materials.case_id", "materials.id"],
                             name="fk_claim_items_material"),
        ForeignKeyConstraint(["case_id", "annotation_id"], ["annotations.case_id", "annotations.id"],
                             name="fk_claim_items_annotation"),
        UniqueConstraint("case_id", "id", name="uq_claim_items_case_id"),
        UniqueConstraint("case_id", "request_key", name="uq_claim_items_request_key"),
        enum_check("category", CLAIM_CATEGORIES, "ck_claim_items_category"),
        enum_check("created_by", PROGRESS_ACTORS, "ck_claim_items_created_by"),
        enum_check("updated_by", PROGRESS_ACTORS, "ck_claim_items_updated_by"),
        CheckConstraint("char_length(title) BETWEEN 1 AND 200", name="ck_claim_items_title"),
        CheckConstraint("calc_note IS NULL OR char_length(calc_note) <= 2000",
                        name="ck_claim_items_calc_note"),
        # 上限与公开契约一致（JS 安全整数）：绕过服务层也不能存下前端无法精确表示的分。
        CheckConstraint(f"amount_cents BETWEEN 0 AND {CLAIM_MAX_CENTS}",
                        name="ck_claim_items_amount"),
        CheckConstraint("position >= 0", name="ck_claim_items_position"),
        CheckConstraint("revision >= 0", name="ck_claim_items_revision"),
        CheckConstraint("num_nonnulls(segment_id, material_id, annotation_id) <= 1",
                        name="ck_claim_items_one_source"),
        Index("ix_claim_items_case_group", "case_id", "group_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    group_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    calc_note: Mapped[str | None] = mapped_column(Text)
    segment_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    material_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    annotation_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)
    request_key: Mapped[str | None] = mapped_column(Text)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())


class ClaimChange(Base):
    __tablename__ = "claim_changes"
    __table_args__ = (
        ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_claim_changes_case"),
        UniqueConstraint("case_id", "id", name="uq_claim_changes_case_id"),
        UniqueConstraint("case_id", "seq", name="uq_claim_changes_seq"),
        UniqueConstraint("case_id", "request_key", name="uq_claim_changes_request_key"),
        enum_check("actor", PROGRESS_ACTORS, "ck_claim_changes_actor"),
        enum_check("review_state", CLAIM_REVIEW_STATES, "ck_claim_changes_review"),
        Index("ix_claim_changes_case_review", "case_id", "review_state"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    command: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    entry: Mapped[str] = mapped_column(Text, nullable=False)
    diff: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    review_state: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'none'"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reverted_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    request_key: Mapped[str | None] = mapped_column(Text)
    # 幂等重放的指纹：同一 request_key 只有命令、来源与内容完全一致才算重试。
    request_fingerprint: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())


class TimeEntry(Base):
    """Persisted billable-hour entry (0.25h granularity, soft delete)."""

    __tablename__ = "time_entries"
    __table_args__ = (
        ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_time_entries_case"),
        ForeignKeyConstraint(["case_id", "node_id"],
                             ["progress_nodes.case_id", "progress_nodes.id"],
                             name="fk_time_entries_node"),
        UniqueConstraint("case_id", "id", name="uq_time_entries_case_id"),
        enum_check("source", TIME_SOURCES, "ck_time_entries_source"),
        CheckConstraint("hours >= 0 AND hours <= 24", name="ck_time_entries_hours"),
        CheckConstraint("source = 'manual' OR started_at IS NOT NULL",
                        name="ck_time_entries_timer_start"),
        Index("ix_time_entries_case_date", "case_id", "work_date"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    node_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    node_title: Mapped[str] = mapped_column(Text, nullable=False)
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    hours: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False, server_default=sa_text("0"))
    source: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --- 案件主体关系图与证据目录（0007_graph_evidence）---------------------------
# 主体/别名/关系/出处属于案件（D1）；图只是命名视图，布局按图持久化；
# 证据目录是编号唯一来源（D2）；快照按图冻结且不可变，客户同意追加留痕（D3）。
# 变更记录按领域独立建表（D4），与 progress_changes 结构一致。

GRAPH_ACTORS = ("human", "agent")
GRAPH_REVIEW_STATES = ("none", "pending", "accepted", "reverted")
EVIDENCE_REVIEW_STATES = ("confirmed", "pending", "rejected")
CREDIBILITY_VALUES = ("evidenced", "suspected", "disputed", "ruled_out")
CONSENT_ACTIONS = ("confirm", "amend", "revoke")


class CaseEntity(Base):
    __tablename__ = "case_entities"
    __table_args__ = (
        ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_case_entities_case"),
        UniqueConstraint("case_id", "id", name="uq_case_entities_case_id"),
        UniqueConstraint("case_id", "request_key", name="uq_case_entities_request_key"),
        enum_check("created_by", GRAPH_ACTORS, "ck_case_entities_created_by"),
        enum_check("updated_by", GRAPH_ACTORS, "ck_case_entities_updated_by"),
        CheckConstraint("char_length(name) BETWEEN 1 AND 200", name="ck_case_entities_name"),
        CheckConstraint("note IS NULL OR char_length(note) <= 4000", name="ck_case_entities_note"),
        CheckConstraint("revision >= 0", name="ck_case_entities_revision"),
        Index("ix_case_entities_case", "case_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    roles: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=sa_text("'[]'::jsonb"))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)
    request_key: Mapped[str | None] = mapped_column(Text)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())


class EntityAlias(Base):
    __tablename__ = "entity_aliases"
    __table_args__ = (
        ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_entity_aliases_case"),
        ForeignKeyConstraint(["case_id", "entity_id"], ["case_entities.case_id", "case_entities.id"],
                             name="fk_entity_aliases_entity"),
        UniqueConstraint("case_id", "id", name="uq_entity_aliases_case_id"),
        enum_check("created_by", GRAPH_ACTORS, "ck_entity_aliases_created_by"),
        CheckConstraint("char_length(alias) BETWEEN 1 AND 200", name="ck_entity_aliases_alias"),
        Index("uq_entity_aliases_active", "case_id", "entity_id", sa_text("lower(alias)"),
              unique=True, postgresql_where=sa_text("removed_at IS NULL")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    alias: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())


class CaseRelation(Base):
    __tablename__ = "case_relations"
    __table_args__ = (
        ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_case_relations_case"),
        ForeignKeyConstraint(["case_id", "from_entity_id"],
                             ["case_entities.case_id", "case_entities.id"],
                             name="fk_case_relations_from"),
        ForeignKeyConstraint(["case_id", "to_entity_id"],
                             ["case_entities.case_id", "case_entities.id"],
                             name="fk_case_relations_to"),
        UniqueConstraint("case_id", "id", name="uq_case_relations_case_id"),
        UniqueConstraint("case_id", "request_key", name="uq_case_relations_request_key"),
        enum_check("created_by", GRAPH_ACTORS, "ck_case_relations_created_by"),
        enum_check("updated_by", GRAPH_ACTORS, "ck_case_relations_updated_by"),
        enum_check("credibility", CREDIBILITY_VALUES, "ck_case_relations_credibility"),
        CheckConstraint("from_entity_id <> to_entity_id", name="ck_case_relations_not_self"),
        CheckConstraint("label IS NULL OR char_length(label) <= 200", name="ck_case_relations_label"),
        CheckConstraint("note IS NULL OR char_length(note) <= 4000", name="ck_case_relations_note"),
        CheckConstraint("revision >= 0", name="ck_case_relations_revision"),
        # 同案同向同类型的有效关系唯一（软删除后可重建）。
        Index("uq_case_relations_active", "case_id", "from_entity_id", "to_entity_id", "kind",
              unique=True, postgresql_where=sa_text("removed_at IS NULL")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    from_entity_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    to_entity_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    directed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sa_text("true"))
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str | None] = mapped_column(Text)
    credibility: Mapped[str] = mapped_column(Text, nullable=False)
    attrs: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa_text("'{}'::jsonb"))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)
    request_key: Mapped[str | None] = mapped_column(Text)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())


class GraphSource(Base):
    __tablename__ = "graph_sources"
    __table_args__ = (
        ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_graph_sources_case"),
        ForeignKeyConstraint(["case_id", "entity_id"], ["case_entities.case_id", "case_entities.id"],
                             name="fk_graph_sources_entity"),
        ForeignKeyConstraint(["case_id", "relation_id"],
                             ["case_relations.case_id", "case_relations.id"],
                             name="fk_graph_sources_relation"),
        ForeignKeyConstraint(["case_id", "segment_id"], ["segments.case_id", "segments.id"],
                             name="fk_graph_sources_segment"),
        ForeignKeyConstraint(["case_id", "material_id"], ["materials.case_id", "materials.id"],
                             name="fk_graph_sources_material"),
        ForeignKeyConstraint(["case_id", "annotation_id"], ["annotations.case_id", "annotations.id"],
                             name="fk_graph_sources_annotation"),
        UniqueConstraint("case_id", "id", name="uq_graph_sources_case_id"),
        enum_check("created_by", GRAPH_ACTORS, "ck_graph_sources_created_by"),
        CheckConstraint("num_nonnulls(entity_id, relation_id) = 1",
                        name="ck_graph_sources_one_target"),
        CheckConstraint("num_nonnulls(segment_id, material_id, annotation_id) = 1",
                        name="ck_graph_sources_one_ref"),
        CheckConstraint("proves IS NULL OR char_length(proves) <= 500",
                        name="ck_graph_sources_proves"),
        Index("ix_graph_sources_case_entity", "case_id", "entity_id"),
        Index("ix_graph_sources_case_relation", "case_id", "relation_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    relation_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    segment_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    material_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    annotation_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    proves: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())


class RelationGraph(Base):
    __tablename__ = "relation_graphs"
    __table_args__ = (
        ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_relation_graphs_case"),
        UniqueConstraint("case_id", "id", name="uq_relation_graphs_case_id"),
        UniqueConstraint("case_id", "request_key", name="uq_relation_graphs_request_key"),
        enum_check("created_by", GRAPH_ACTORS, "ck_relation_graphs_created_by"),
        enum_check("updated_by", GRAPH_ACTORS, "ck_relation_graphs_updated_by"),
        CheckConstraint("char_length(title) BETWEEN 1 AND 200", name="ck_relation_graphs_title"),
        CheckConstraint("description IS NULL OR char_length(description) <= 2000",
                        name="ck_relation_graphs_description"),
        CheckConstraint("revision >= 0", name="ck_relation_graphs_revision"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)
    request_key: Mapped[str | None] = mapped_column(Text)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())


class RelationGraphGroup(Base):
    __tablename__ = "relation_graph_groups"
    __table_args__ = (
        ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_relation_graph_groups_case"),
        ForeignKeyConstraint(["case_id", "graph_id"],
                             ["relation_graphs.case_id", "relation_graphs.id"],
                             name="fk_relation_graph_groups_graph"),
        UniqueConstraint("case_id", "id", name="uq_relation_graph_groups_case_id"),
        # 同图分组作用域：节点 group_id 的组合外键引用此键，跨图分组由数据库拒绝。
        UniqueConstraint("case_id", "graph_id", "id", name="uq_relation_graph_groups_graph_id"),
        CheckConstraint("char_length(title) BETWEEN 1 AND 200", name="ck_relation_graph_groups_title"),
        CheckConstraint("color IS NULL OR char_length(color) <= 40", name="ck_relation_graph_groups_color"),
        CheckConstraint("width > 0 AND height > 0", name="ck_relation_graph_groups_size"),
        CheckConstraint("revision >= 0", name="ck_relation_graph_groups_revision"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    graph_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    color: Mapped[str | None] = mapped_column(Text)
    x: Mapped[float] = mapped_column(Double, nullable=False, server_default=sa_text("0"))
    y: Mapped[float] = mapped_column(Double, nullable=False, server_default=sa_text("0"))
    width: Mapped[float] = mapped_column(Double, nullable=False, server_default=sa_text("360"))
    height: Mapped[float] = mapped_column(Double, nullable=False, server_default=sa_text("220"))
    collapsed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sa_text("false"))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())


class RelationGraphNode(Base):
    __tablename__ = "relation_graph_nodes"
    __table_args__ = (
        ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_relation_graph_nodes_case"),
        ForeignKeyConstraint(["case_id", "graph_id"],
                             ["relation_graphs.case_id", "relation_graphs.id"],
                             name="fk_relation_graph_nodes_graph"),
        ForeignKeyConstraint(["case_id", "entity_id"],
                             ["case_entities.case_id", "case_entities.id"],
                             name="fk_relation_graph_nodes_entity"),
        ForeignKeyConstraint(["case_id", "graph_id", "group_id"],
                             ["relation_graph_groups.case_id",
                              "relation_graph_groups.graph_id",
                              "relation_graph_groups.id"],
                             name="fk_relation_graph_nodes_group"),
        UniqueConstraint("graph_id", "entity_id", name="uq_relation_graph_nodes_member"),
        UniqueConstraint("case_id", "id", name="uq_relation_graph_nodes_case_id"),
        CheckConstraint("revision >= 0", name="ck_relation_graph_nodes_revision"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    graph_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    group_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    x: Mapped[float] = mapped_column(Double, nullable=False, server_default=sa_text("0"))
    y: Mapped[float] = mapped_column(Double, nullable=False, server_default=sa_text("0"))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())


class RelationGraphEdge(Base):
    __tablename__ = "relation_graph_edges"
    __table_args__ = (
        ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_relation_graph_edges_case"),
        ForeignKeyConstraint(["case_id", "graph_id"],
                             ["relation_graphs.case_id", "relation_graphs.id"],
                             name="fk_relation_graph_edges_graph"),
        ForeignKeyConstraint(["case_id", "relation_id"],
                             ["case_relations.case_id", "case_relations.id"],
                             name="fk_relation_graph_edges_relation"),
        UniqueConstraint("graph_id", "relation_id", name="uq_relation_graph_edges_member"),
        UniqueConstraint("case_id", "id", name="uq_relation_graph_edges_case_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    graph_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    relation_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())


class RelationGraphSnapshot(Base):
    __tablename__ = "relation_graph_snapshots"
    __table_args__ = (
        ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_relation_graph_snapshots_case"),
        ForeignKeyConstraint(["case_id", "graph_id"],
                             ["relation_graphs.case_id", "relation_graphs.id"],
                             name="fk_relation_graph_snapshots_graph"),
        ForeignKeyConstraint(["case_id", "progress_node_id"],
                             ["progress_nodes.case_id", "progress_nodes.id"],
                             name="fk_relation_graph_snapshots_progress_node"),
        UniqueConstraint("graph_id", "version_no", name="uq_relation_graph_snapshots_version"),
        UniqueConstraint("case_id", "id", name="uq_relation_graph_snapshots_case_id"),
        CheckConstraint("version_no >= 1", name="ck_relation_graph_snapshots_version_no"),
        CheckConstraint("char_length(stage_label) BETWEEN 1 AND 100",
                        name="ck_relation_graph_snapshots_stage"),
        CheckConstraint("note IS NULL OR char_length(note) <= 2000",
                        name="ck_relation_graph_snapshots_note"),
        CheckConstraint("excluded_pending_count >= 0",
                        name="ck_relation_graph_snapshots_pending"),
        CheckConstraint("created_by = 'human'", name="ck_relation_graph_snapshots_actor"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    graph_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    stage_label: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    progress_node_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    excluded_pending_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    created_by: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'human'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())


class SnapshotConsent(Base):
    __tablename__ = "snapshot_consents"
    __table_args__ = (
        ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_snapshot_consents_case"),
        ForeignKeyConstraint(["case_id", "snapshot_id"],
                             ["relation_graph_snapshots.case_id", "relation_graph_snapshots.id"],
                             name="fk_snapshot_consents_snapshot"),
        ForeignKeyConstraint(["case_id", "snapshot_id", "parent_consent_id"],
                             ["snapshot_consents.case_id",
                              "snapshot_consents.snapshot_id",
                              "snapshot_consents.id"],
                             name="fk_snapshot_consents_parent"),
        ForeignKeyConstraint(["case_id", "evidence_material_id"],
                             ["materials.case_id", "materials.id"],
                             name="fk_snapshot_consents_material"),
        UniqueConstraint("case_id", "id", name="uq_snapshot_consents_case_id"),
        # 同快照同意作用域：parent_consent_id 的组合外键引用此键。
        UniqueConstraint("case_id", "snapshot_id", "id", name="uq_snapshot_consents_snapshot_id"),
        enum_check("action", CONSENT_ACTIONS, "ck_snapshot_consents_action"),
        CheckConstraint("char_length(consenter) BETWEEN 1 AND 200", name="ck_snapshot_consents_consenter"),
        CheckConstraint("char_length(source_detail) BETWEEN 1 AND 1000",
                        name="ck_snapshot_consents_source_detail"),
        CheckConstraint("note IS NULL OR char_length(note) <= 2000", name="ck_snapshot_consents_note"),
        CheckConstraint("created_by = 'human'", name="ck_snapshot_consents_actor"),
        # 更正/撤回必须指向原记录，首次同意不得带父记录（D3 追加留痕）。
        CheckConstraint("(action = 'confirm' AND parent_consent_id IS NULL) "
                        "OR (action IN ('amend', 'revoke') AND parent_consent_id IS NOT NULL)",
                        name="ck_snapshot_consents_parent"),
        # 同意记录不能指向自己（组合外键已保证同案同快照，这里再排除自指）。
        CheckConstraint("parent_consent_id IS NULL OR parent_consent_id <> id",
                        name="ck_snapshot_consents_not_self"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    parent_consent_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    consenter: Mapped[str] = mapped_column(Text, nullable=False)
    consented_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    source_detail: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_material_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'human'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())


class EvidenceGroup(Base):
    __tablename__ = "evidence_groups"
    __table_args__ = (
        ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_evidence_groups_case"),
        UniqueConstraint("case_id", "id", name="uq_evidence_groups_case_id"),
        UniqueConstraint("case_id", "request_key", name="uq_evidence_groups_request_key"),
        enum_check("created_by", GRAPH_ACTORS, "ck_evidence_groups_created_by"),
        CheckConstraint("char_length(title) BETWEEN 1 AND 200", name="ck_evidence_groups_title"),
        CheckConstraint("purpose IS NULL OR char_length(purpose) <= 1000",
                        name="ck_evidence_groups_purpose"),
        CheckConstraint("position >= 0", name="ck_evidence_groups_position"),
        CheckConstraint("revision >= 0", name="ck_evidence_groups_revision"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    request_key: Mapped[str | None] = mapped_column(Text)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())


class EvidenceItem(Base):
    __tablename__ = "evidence_items"
    __table_args__ = (
        ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_evidence_items_case"),
        ForeignKeyConstraint(["case_id", "group_id"], ["evidence_groups.case_id", "evidence_groups.id"],
                             name="fk_evidence_items_group"),
        ForeignKeyConstraint(["case_id", "import_material_id"],
                             ["materials.case_id", "materials.id"],
                             name="fk_evidence_items_import_material"),
        UniqueConstraint("case_id", "id", name="uq_evidence_items_case_id"),
        UniqueConstraint("case_id", "request_key", name="uq_evidence_items_request_key"),
        enum_check("created_by", GRAPH_ACTORS, "ck_evidence_items_created_by"),
        CheckConstraint("source_kind IN ('manual', 'imported')", name="ck_evidence_items_source_kind"),
        enum_check("review_state", EVIDENCE_REVIEW_STATES, "ck_evidence_items_review"),
        CheckConstraint("char_length(title) BETWEEN 1 AND 300", name="ck_evidence_items_title"),
        CheckConstraint("purpose IS NULL OR char_length(purpose) <= 2000",
                        name="ck_evidence_items_purpose"),
        CheckConstraint("position >= 0", name="ck_evidence_items_position"),
        CheckConstraint("revision >= 0", name="ck_evidence_items_revision"),
        Index("ix_evidence_items_case_group", "case_id", "group_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    group_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str | None] = mapped_column(Text)
    source_kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'manual'"))
    import_material_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    review_state: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'confirmed'"))
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    request_key: Mapped[str | None] = mapped_column(Text)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())


class EvidenceItemBinding(Base):
    __tablename__ = "evidence_item_bindings"
    __table_args__ = (
        ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_evidence_item_bindings_case"),
        ForeignKeyConstraint(["case_id", "item_id"], ["evidence_items.case_id", "evidence_items.id"],
                             name="fk_evidence_item_bindings_item"),
        ForeignKeyConstraint(["case_id", "material_id"], ["materials.case_id", "materials.id"],
                             name="fk_evidence_item_bindings_material"),
        UniqueConstraint("case_id", "id", name="uq_evidence_item_bindings_case_id"),
        enum_check("created_by", GRAPH_ACTORS, "ck_evidence_item_bindings_created_by"),
        enum_check("review_state", EVIDENCE_REVIEW_STATES, "ck_evidence_item_bindings_review"),
        CheckConstraint("page_start IS NULL OR page_start >= 1",
                        name="ck_evidence_item_bindings_page_start"),
        CheckConstraint("page_end IS NULL OR (page_start IS NOT NULL AND page_end >= page_start)",
                        name="ck_evidence_item_bindings_page_end"),
        Index("ix_evidence_item_bindings_case_item", "case_id", "item_id"),
        Index("ix_evidence_item_bindings_case_material", "case_id", "material_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    item_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    material_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    review_state: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'confirmed'"))
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())


class GraphChange(Base):
    __tablename__ = "graph_changes"
    __table_args__ = (
        ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_graph_changes_case"),
        UniqueConstraint("case_id", "id", name="uq_graph_changes_case_id"),
        UniqueConstraint("case_id", "seq", name="uq_graph_changes_seq"),
        enum_check("actor", GRAPH_ACTORS, "ck_graph_changes_actor"),
        enum_check("review_state", GRAPH_REVIEW_STATES, "ck_graph_changes_review"),
        Index("ix_graph_changes_case_review", "case_id", "review_state"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    command: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    entry: Mapped[str] = mapped_column(Text, nullable=False)
    diff: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    review_state: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'none'"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reverted_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())


class EvidenceChange(Base):
    __tablename__ = "evidence_changes"
    __table_args__ = (
        ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_evidence_changes_case"),
        UniqueConstraint("case_id", "id", name="uq_evidence_changes_case_id"),
        UniqueConstraint("case_id", "seq", name="uq_evidence_changes_seq"),
        enum_check("actor", GRAPH_ACTORS, "ck_evidence_changes_actor"),
        enum_check("review_state", GRAPH_REVIEW_STATES, "ck_evidence_changes_review"),
        Index("ix_evidence_changes_case_review", "case_id", "review_state"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    command: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    entry: Mapped[str] = mapped_column(Text, nullable=False)
    diff: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    review_state: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'none'"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reverted_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
