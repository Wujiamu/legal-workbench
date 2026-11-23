"""Initial schema: case-scoped entities, composite foreign keys, trgm search.

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-10
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "cases",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "intakes",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("case_id", UUID, nullable=False),
        sa.Column("request_key", sa.Text(), nullable=False),
        sa.Column("source_label", sa.Text(), nullable=False),
        sa.Column("source_root", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_intakes_case"),
        sa.UniqueConstraint("case_id", "request_key", name="uq_intakes_case_request_key"),
        sa.UniqueConstraint("case_id", "id", name="uq_intakes_case_id"),
    )

    op.create_table(
        "stored_objects",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("case_id", UUID, nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("integrity_status", sa.Text(), nullable=False, server_default=sa.text("'ok'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_stored_objects_case"),
        sa.UniqueConstraint("case_id", "sha256", name="uq_stored_objects_case_sha"),
        sa.UniqueConstraint("case_id", "id", name="uq_stored_objects_case_id"),
        sa.CheckConstraint("integrity_status IN ('ok', 'integrity_error')", name="ck_stored_objects_integrity"),
        sa.CheckConstraint("size >= 0", name="ck_stored_objects_size"),
    )

    op.create_table(
        "materials",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("case_id", UUID, nullable=False),
        sa.Column("intake_item_id", UUID, nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False, server_default=sa.text("'待分类'")),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("original_file_version_id", UUID),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("case_id", "id", name="uq_materials_case_id"),
        sa.UniqueConstraint("intake_item_id", name="uq_materials_intake_item"),
    )

    op.create_table(
        "intake_items",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("case_id", UUID, nullable=False),
        sa.Column("intake_id", UUID, nullable=False),
        sa.Column("relative_source", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("material_id", UUID),
        sa.Column("error_code", sa.Text()),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["case_id", "intake_id"], ["intakes.case_id", "intakes.id"],
                                name="fk_intake_items_intake"),
        sa.ForeignKeyConstraint(["case_id", "material_id"], ["materials.case_id", "materials.id"],
                                name="fk_intake_items_material", deferrable=True, initially="DEFERRED"),
        sa.UniqueConstraint("intake_id", "relative_source", name="uq_intake_items_source"),
        sa.UniqueConstraint("case_id", "id", name="uq_intake_items_case_id"),
        sa.CheckConstraint(
            "status IN ('pending','copying','published','ready','failed','skipped')",
            name="ck_intake_items_status",
        ),
    )
    op.create_foreign_key(
        "fk_materials_intake_item", "materials", "intake_items",
        ["case_id", "intake_item_id"], ["case_id", "id"],
    )

    op.create_table(
        "file_versions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("case_id", UUID, nullable=False),
        sa.Column("material_id", UUID, nullable=False),
        sa.Column("stored_object_id", UUID, nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("parent_version_id", UUID),
        sa.Column("active_parse_run_id", UUID),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["case_id", "material_id"], ["materials.case_id", "materials.id"],
                                name="fk_file_versions_material"),
        sa.ForeignKeyConstraint(["case_id", "stored_object_id"], ["stored_objects.case_id", "stored_objects.id"],
                                name="fk_file_versions_stored_object"),
        sa.ForeignKeyConstraint(
            ["case_id", "material_id", "parent_version_id"],
            ["file_versions.case_id", "file_versions.material_id", "file_versions.id"],
            name="fk_file_versions_parent",
        ),
        sa.UniqueConstraint("case_id", "id", name="uq_file_versions_case_id"),
        sa.UniqueConstraint("case_id", "material_id", "id", name="uq_file_versions_material_id"),
        sa.CheckConstraint("kind IN ('original','working')", name="ck_file_versions_kind"),
    )
    op.create_foreign_key(
        "fk_materials_original_version", "materials", "file_versions",
        ["case_id", "original_file_version_id"], ["case_id", "id"],
        deferrable=True, initially="DEFERRED",
    )

    op.create_table(
        "working_copies",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("case_id", UUID, nullable=False),
        sa.Column("material_id", UUID, nullable=False),
        sa.Column("base_version_id", UUID, nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["case_id", "material_id", "base_version_id"],
            ["file_versions.case_id", "file_versions.material_id", "file_versions.id"],
            name="fk_working_copies_base_version",
        ),
        sa.UniqueConstraint("case_id", "id", name="uq_working_copies_case_id"),
        sa.CheckConstraint("revision >= 0", name="ck_working_copies_revision"),
    )

    op.create_table(
        "parse_runs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("case_id", UUID, nullable=False),
        sa.Column("file_version_id", UUID, nullable=False),
        sa.Column("parser_version", sa.Text(), nullable=False),
        sa.Column("config", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("coverage", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["case_id", "file_version_id"], ["file_versions.case_id", "file_versions.id"],
                                name="fk_parse_runs_file_version"),
        sa.UniqueConstraint("case_id", "id", name="uq_parse_runs_case_id"),
        sa.UniqueConstraint("case_id", "file_version_id", "sequence", name="uq_parse_runs_sequence"),
        sa.CheckConstraint(
            "status IN ('queued','running','complete','partial','failed','unsupported')",
            name="ck_parse_runs_status",
        ),
        sa.CheckConstraint("sequence >= 0", name="ck_parse_runs_sequence"),
    )
    op.create_foreign_key(
        "fk_file_versions_active_parse_run", "file_versions", "parse_runs",
        ["case_id", "active_parse_run_id"], ["case_id", "id"],
        deferrable=True, initially="DEFERRED",
    )

    op.create_table(
        "parse_units",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("case_id", UUID, nullable=False),
        sa.Column("parse_run_id", UUID, nullable=False),
        sa.Column("unit_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("method", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column("warnings", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.ForeignKeyConstraint(["case_id", "parse_run_id"], ["parse_runs.case_id", "parse_runs.id"],
                                name="fk_parse_units_run"),
        sa.UniqueConstraint("parse_run_id", "unit_key", name="uq_parse_units_unit_key"),
        sa.UniqueConstraint("case_id", "id", name="uq_parse_units_case_id"),
        sa.CheckConstraint(
            "status IN ('pending','extracted','ocr_done','no_text_detected','failed','unsupported')",
            name="ck_parse_units_status",
        ),
    )

    op.create_table(
        "segments",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("case_id", UUID, nullable=False),
        sa.Column("parse_run_id", UUID, nullable=False),
        sa.Column("parse_unit_id", UUID, nullable=False),
        sa.Column("locator", JSONB, nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("normalized_text", sa.Text(), nullable=False),
        sa.Column("offset_map", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.ForeignKeyConstraint(["case_id", "parse_run_id"], ["parse_runs.case_id", "parse_runs.id"],
                                name="fk_segments_run"),
        sa.ForeignKeyConstraint(["case_id", "parse_unit_id"], ["parse_units.case_id", "parse_units.id"],
                                name="fk_segments_unit"),
        sa.UniqueConstraint("case_id", "id", name="uq_segments_case_id"),
    )
    op.create_index("ix_segments_run", "segments", ["parse_run_id"])
    op.create_index(
        "ix_segments_normalized_trgm", "segments", ["normalized_text"],
        postgresql_using="gin", postgresql_ops={"normalized_text": "gin_trgm_ops"},
    )

    op.create_table(
        "exact_values",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("case_id", UUID, nullable=False),
        sa.Column("segment_id", UUID, nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("canonical_value", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("amount_value", sa.Numeric(24, 6)),
        sa.Column("currency", sa.Text()),
        sa.Column("span", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.ForeignKeyConstraint(["case_id", "segment_id"], ["segments.case_id", "segments.id"],
                                name="fk_exact_values_segment"),
        sa.UniqueConstraint("case_id", "id", name="uq_exact_values_case_id"),
        sa.CheckConstraint("kind IN ('literal','identifier','amount','date')", name="ck_exact_values_kind"),
    )
    op.create_index("ix_exact_values_amount", "exact_values", ["case_id", "kind", "currency", "amount_value"])

    op.create_table(
        "jobs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("case_id", UUID, nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("target_id", UUID, nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("completed_units", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("total_units", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error", sa.Text()),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_jobs_case"),
        sa.UniqueConstraint("case_id", "id", name="uq_jobs_case_id"),
        sa.CheckConstraint("status IN ('queued','running','succeeded','partial','failed')",
                           name="ck_jobs_status"),
    )
    op.create_index("ix_jobs_case_status", "jobs", ["case_id", "status"])

    op.create_table(
        "analyses",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("case_id", UUID, nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("current_version_id", UUID),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_analyses_case"),
        sa.UniqueConstraint("case_id", "id", name="uq_analyses_case_id"),
    )

    op.create_table(
        "analysis_versions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("case_id", UUID, nullable=False),
        sa.Column("analysis_id", UUID, nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("statement_kind", sa.Text(), nullable=False),
        sa.Column("source_refs", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("review_status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["case_id", "analysis_id"], ["analyses.case_id", "analyses.id"],
                                name="fk_analysis_versions_analysis"),
        sa.UniqueConstraint("case_id", "id", name="uq_analysis_versions_case_id"),
        sa.UniqueConstraint("case_id", "analysis_id", "id", name="uq_analysis_versions_analysis_id"),
        sa.CheckConstraint(
            "statement_kind IN ('excerpt','party_statement','agent_analysis','human_opinion')",
            name="ck_analysis_versions_kind",
        ),
        sa.CheckConstraint("review_status IN ('pending','approved','rejected')",
                           name="ck_analysis_versions_review"),
    )
    op.create_index(
        "ix_analysis_versions_content_trgm", "analysis_versions", ["content"],
        postgresql_using="gin", postgresql_ops={"content": "gin_trgm_ops"},
    )
    op.create_foreign_key(
        "fk_analyses_current_version", "analyses", "analysis_versions",
        ["case_id", "current_version_id"], ["case_id", "id"],
        deferrable=True, initially="DEFERRED",
    )

    op.create_table(
        "audit_events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("case_id", UUID),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_id", UUID),
        sa.Column("request_id", UUID),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_audit_events_case"),
    )
    op.create_index("ix_audit_events_case_created", "audit_events", ["case_id", "created_at"])

    op.create_table(
        "maintenance_state",
        sa.Column("id", sa.SmallInteger(), primary_key=True, server_default=sa.text("1")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("reason", sa.Text()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("id = 1", name="ck_maintenance_singleton"),
    )
    op.execute("INSERT INTO maintenance_state (id, active) VALUES (1, false)")

    # The original version pointer must reference an original version of the
    # same material; a plain composite FK cannot express this.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION check_material_original_version() RETURNS trigger AS $$
        BEGIN
            IF NEW.original_file_version_id IS NOT NULL THEN
                IF NOT EXISTS (
                    SELECT 1 FROM file_versions fv
                    WHERE fv.id = NEW.original_file_version_id
                      AND fv.case_id = NEW.case_id
                      AND fv.material_id = NEW.id
                      AND fv.kind = 'original'
                ) THEN
                    RAISE EXCEPTION 'original_file_version_id must reference an original version of the same material'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_materials_original_version
        BEFORE INSERT OR UPDATE OF original_file_version_id ON materials
        FOR EACH ROW EXECUTE FUNCTION check_material_original_version()
        """
    )

    # App role gets DML only; DDL stays with the migration role (the owner).
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lwb_app') THEN
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO lwb_app';
                EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO lwb_app';
                EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO lwb_app';
                EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO lwb_app';
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_materials_original_version ON materials")
    op.execute("DROP FUNCTION IF EXISTS check_material_original_version()")
    for table in (
        "maintenance_state", "audit_events", "analysis_versions", "analyses", "jobs",
        "exact_values", "segments", "parse_units", "parse_runs", "working_copies",
        "file_versions", "intake_items", "materials", "stored_objects", "intakes", "cases",
    ):
        op.drop_table(table)
