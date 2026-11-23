"""audit_events.operation check constraint

Restricts the audit trail to a closed set of operation names so the table stays
queryable and typos cannot silently create a new category.

Revision ID: 0003_audit_operation_check
Revises: 0002_intake_stored_object
"""
from __future__ import annotations

from alembic import op

revision = "0003_audit_operation_check"
down_revision = "0002_intake_stored_object"
branch_labels = None
depends_on = None

OPERATIONS = (
    "session.login", "session.logout", "case.create", "intake.import",
    "intake.upload", "material.classify", "material.rename",
    "material.working_copy", "file_version.register", "parse.reparse",
    "analysis.save", "analysis.review", "backup.create", "backup.restore",
    "maintenance.clear",
)


def upgrade() -> None:
    values = ", ".join(f"'{name}'" for name in OPERATIONS)
    op.create_check_constraint(
        "ck_audit_events_operation", "audit_events", f"operation IN ({values})")


def downgrade() -> None:
    op.drop_constraint("ck_audit_events_operation", "audit_events", type_="check")
