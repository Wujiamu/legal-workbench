"""Case archive flag and persisted time entries.

Revision ID: 0006_case_archive_time
Revises: 0005_case_progress

- ``cases.archived_at`` marks a closed case: progress and material writes are
  rejected at the service layer, the row itself is kept for history.
- ``time_entries`` persists the billable-hour prototype: timer or manual
  entries, 0.25h granularity, soft delete, optional progress-node reference
  with a same-case composite foreign key.
- The audit operation set gains case.archive / case.unarchive / time.*.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_case_archive_time"
down_revision = "0005_case_progress"
branch_labels = None
depends_on = None

OLD_OPERATIONS = (
    "session.login", "session.logout", "case.create", "intake.import",
    "intake.upload", "material.classify", "material.rename",
    "material.working_copy", "file_version.register", "parse.reparse",
    "analysis.save", "analysis.review", "backup.create", "backup.restore",
    "maintenance.clear",
)

PROGRESS_OPERATIONS = (
    "progress.add_node", "progress.update_node", "progress.move_node",
    "progress.set_active_stage", "progress.insert_fragment", "progress.remove_node",
    "progress.restore_node", "progress.add_relation", "progress.remove_relation",
    "progress.link", "progress.unlink", "progress.accept_change", "progress.accept_all",
    "progress.revert_change",
)

NEW_OPERATIONS = (
    "case.archive", "case.unarchive",
    "time.start", "time.stop", "time.add", "time.remove",
)


def upgrade() -> None:
    op.add_column("cases", sa.Column("archived_at", sa.DateTime(timezone=True)))

    op.create_table(
        "time_entries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid()),
        sa.Column("node_title", sa.Text(), nullable=False),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("hours", sa.Numeric(4, 2), nullable=False, server_default="0"),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("stopped_at", sa.DateTime(timezone=True)),
        sa.Column("note", sa.Text()),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_time_entries_case"),
        sa.ForeignKeyConstraint(["case_id", "node_id"],
                                ["progress_nodes.case_id", "progress_nodes.id"],
                                name="fk_time_entries_node"),
        sa.UniqueConstraint("case_id", "id", name="uq_time_entries_case_id"),
        sa.CheckConstraint("source IN ('timer', 'manual')", name="ck_time_entries_source"),
        # created_by 记录审计 actor（local-web/cli/mcp/human/agent），不做闭集约束。
        sa.CheckConstraint("hours >= 0 AND hours <= 24", name="ck_time_entries_hours"),
        sa.CheckConstraint("source = 'manual' OR started_at IS NOT NULL",
                           name="ck_time_entries_timer_start"),
    )
    op.create_index("ix_time_entries_case_date", "time_entries", ["case_id", "work_date"])

    op.drop_constraint("ck_audit_events_operation", "audit_events", type_="check")
    values = ", ".join(f"'{name}'" for name in (*OLD_OPERATIONS, *PROGRESS_OPERATIONS,
                                                 *NEW_OPERATIONS))
    op.create_check_constraint("ck_audit_events_operation", "audit_events",
                               f"operation IN ({values})")


def downgrade() -> None:
    op.execute("DELETE FROM audit_events WHERE operation IN "
               "('case.archive', 'case.unarchive', 'time.start', 'time.stop', "
               "'time.add', 'time.remove')")
    op.drop_constraint("ck_audit_events_operation", "audit_events", type_="check")
    values = ", ".join(f"'{name}'" for name in (*OLD_OPERATIONS, *PROGRESS_OPERATIONS))
    op.create_check_constraint("ck_audit_events_operation", "audit_events",
                               f"operation IN ({values})")
    op.drop_table("time_entries")
    op.drop_column("cases", "archived_at")
