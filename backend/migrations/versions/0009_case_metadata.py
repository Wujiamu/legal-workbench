"""Add case_number, plaintiff, defendant and work_status to cases.

Revision ID: 0009_case_metadata
Revises: 0008_claim_amounts
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_case_metadata"
down_revision = "0008_claim_amounts"
branch_labels = None
depends_on = None

CASE_WORK_STATUSES = ("normal", "need_materials", "need_sync", "waiting_external", "paused")


def upgrade() -> None:
    op.add_column("cases", sa.Column("case_number", sa.Text(), nullable=True))
    op.add_column("cases", sa.Column("plaintiff", sa.Text(), nullable=True))
    op.add_column("cases", sa.Column("defendant", sa.Text(), nullable=True))
    op.add_column(
        "cases",
        sa.Column(
            "work_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'normal'"),
        ),
    )
    joined = ", ".join(f"'{value}'" for value in CASE_WORK_STATUSES)
    op.create_check_constraint(
        "ck_cases_work_status",
        "cases",
        f"work_status IN ({joined})",
    )


def downgrade() -> None:
    op.drop_constraint("ck_cases_work_status", "cases", type_="check")
    op.drop_column("cases", "work_status")
    op.drop_column("cases", "defendant")
    op.drop_column("cases", "plaintiff")
    op.drop_column("cases", "case_number")
