"""Record the published object on the intake item for crash recovery.

Revision ID: 0002_intake_stored_object
Revises: 0001_initial
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_intake_stored_object"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("intake_items", sa.Column("stored_object_id", postgresql.UUID(as_uuid=True)))
    op.create_foreign_key(
        "fk_intake_items_stored_object", "intake_items", "stored_objects",
        ["case_id", "stored_object_id"], ["case_id", "id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_intake_items_stored_object", "intake_items", type_="foreignkey")
    op.drop_column("intake_items", "stored_object_id")
