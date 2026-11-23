"""Add case scheduling and source annotations."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004_case_workbench"
down_revision = "0003_audit_operation_check"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cases", sa.Column("stage", sa.Text()))
    op.add_column("cases", sa.Column("next_action", sa.Text()))
    op.add_column("cases", sa.Column("deadlines", JSONB, nullable=False,
                                    server_default=sa.text("'[]'::jsonb")))
    op.add_column("materials", sa.Column("acquisition", sa.Text()))
    op.create_table(
        "annotations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("case_id", sa.Uuid(), sa.ForeignKey("cases.id"), nullable=False),
        sa.Column("file_version_id", sa.Uuid(), nullable=False),
        sa.Column("page", sa.Integer()),
        sa.Column("author", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("reviewed_text", sa.Text()),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["case_id", "file_version_id"],
                                ["file_versions.case_id", "file_versions.id"],
                                name="fk_annotations_file_version"),
        sa.CheckConstraint("author IN ('agent', 'human')", name="ck_annotations_author"),
        sa.CheckConstraint("kind IN ('transcription', 'interpretation')", name="ck_annotations_kind"),
        sa.CheckConstraint("page IS NULL OR page >= 1", name="ck_annotations_page"),
        sa.CheckConstraint(
            "(revision = 0 AND reviewed_text IS NULL) OR "
            "(author = 'agent' AND revision = 1 AND reviewed_text IS NOT NULL)",
            name="ck_annotations_review"),
    )
    op.create_index("ix_annotations_case_id", "annotations", ["case_id"])


def downgrade() -> None:
    op.drop_table("annotations")
    op.drop_column("materials", "acquisition")
    op.drop_column("cases", "deadlines")
    op.drop_column("cases", "next_action")
    op.drop_column("cases", "stage")
