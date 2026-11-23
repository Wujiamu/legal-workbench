"""Add claim groups, claim items and staged claim changes (诉请金额 P2).

Revision ID: 0008_claim_amounts
Revises: 0007_relation_graph

Structural decisions (docs/decisions/20261120-主体关系图与证据目录模型.md D5,
docs/changes/20261120-期限时间轴与诉请金额.md §2):

- amounts are exact integer cents in ``BIGINT`` with ``>= 0``; the sign is
  decided by ``category`` (``deduction`` subtracts), never stored as a negative
  number.  No NUMERIC/float column takes part in claim arithmetic.
- every table is case-scoped and references parents through ``(case_id, …)``
  composite foreign keys, so PostgreSQL itself rejects cross-case links.
- ``claim_changes`` mirrors ``progress_changes`` (seq from the shared
  ``case_change_seq`` with domain ``claims``) plus ``request_key``: an agent
  submission is *staged* here and not written to ``claim_groups`` /
  ``claim_items`` until a human accepts it, so idempotent replay has to be
  keyed on the change row, not on a business row.
- ``segment_id`` uses the default RESTRICT: provenance is immutable.  A normal
  reparse creates a *new* run and a retry only rewrites work units that never
  reached a terminal status, so published segments are never deleted; a hard
  delete of a cited segment must therefore fail loudly instead of silently
  clearing the citation.  ``material_id`` / ``annotation_id`` are RESTRICT too.
- ``amount_cents`` is bounded by the published contract (``2**53 - 1``), not just
  by ``>= 0``: the same number travels to the browser as JSON, so a value above
  the JS safe integer could never be displayed exactly.
- the audit operation CHECK is extended from the constraint actually present
  in the database, not from a copied list, so this migration merges with 0007
  regardless of which operations that revision added.
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008_claim_amounts"
down_revision = "0007_graph_evidence"
branch_labels = None
depends_on = None

CLAIM_OPERATIONS = (
    "claims.add_group",
    "claims.update_group",
    "claims.reorder_groups",
    "claims.remove_group",
    "claims.add_item",
    "claims.update_item",
    "claims.reorder_items",
    "claims.remove_item",
    "claims.restore_item",
    "claims.accept_change",
    "claims.accept_all",
    "claims.revert_change",
)

CLAIM_CATEGORIES = (
    "principal", "interest", "liquidated_damages", "expense", "deduction", "other",
)


def _audit_operations() -> list[str]:
    """Read the closed set from the live CHECK instead of duplicating a list."""
    definition = op.get_bind().execute(sa.text(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'ck_audit_events_operation' "
        "AND conrelid = 'audit_events'::regclass"
    )).scalar_one()
    return re.findall(r"'([^']+)'", definition)


def _write_audit_operations(operations: list[str]) -> None:
    values = ", ".join(f"'{name}'" for name in operations)
    op.drop_constraint("ck_audit_events_operation", "audit_events", type_="check")
    op.create_check_constraint("ck_audit_events_operation", "audit_events",
                               f"operation IN ({values})")


def upgrade() -> None:
    op.create_table(
        "claim_groups",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False, server_default="CNY"),
        sa.Column("include_in_total", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("note", sa.Text()),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.Column("request_key", sa.Text()),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_claim_groups_case"),
        sa.UniqueConstraint("case_id", "id", name="uq_claim_groups_case_id"),
        sa.UniqueConstraint("case_id", "request_key", name="uq_claim_groups_request_key"),
        sa.CheckConstraint("created_by IN ('human', 'agent')", name="ck_claim_groups_created_by"),
        sa.CheckConstraint("updated_by IN ('human', 'agent')", name="ck_claim_groups_updated_by"),
        sa.CheckConstraint("char_length(title) BETWEEN 1 AND 200", name="ck_claim_groups_title"),
        sa.CheckConstraint("note IS NULL OR char_length(note) <= 2000", name="ck_claim_groups_note"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_claim_groups_currency"),
        sa.CheckConstraint("position >= 0", name="ck_claim_groups_position"),
        sa.CheckConstraint("revision >= 0", name="ck_claim_groups_revision"),
    )
    op.create_index("ix_claim_groups_case_position", "claim_groups", ["case_id", "position"])

    op.create_table(
        "claim_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("calc_note", sa.Text()),
        sa.Column("segment_id", sa.Uuid()),
        sa.Column("material_id", sa.Uuid()),
        sa.Column("annotation_id", sa.Uuid()),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.Column("request_key", sa.Text()),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_claim_items_case"),
        sa.ForeignKeyConstraint(["case_id", "group_id"],
                                ["claim_groups.case_id", "claim_groups.id"],
                                name="fk_claim_items_group"),
        sa.ForeignKeyConstraint(["case_id", "segment_id"], ["segments.case_id", "segments.id"],
                                name="fk_claim_items_segment"),
        sa.ForeignKeyConstraint(["case_id", "material_id"], ["materials.case_id", "materials.id"],
                                name="fk_claim_items_material"),
        sa.ForeignKeyConstraint(["case_id", "annotation_id"], ["annotations.case_id", "annotations.id"],
                                name="fk_claim_items_annotation"),
        sa.UniqueConstraint("case_id", "id", name="uq_claim_items_case_id"),
        sa.UniqueConstraint("case_id", "request_key", name="uq_claim_items_request_key"),
        sa.CheckConstraint(
            "category IN ('principal', 'interest', 'liquidated_damages', "
            "'expense', 'deduction', 'other')", name="ck_claim_items_category"),
        sa.CheckConstraint("created_by IN ('human', 'agent')", name="ck_claim_items_created_by"),
        sa.CheckConstraint("updated_by IN ('human', 'agent')", name="ck_claim_items_updated_by"),
        sa.CheckConstraint("char_length(title) BETWEEN 1 AND 200", name="ck_claim_items_title"),
        sa.CheckConstraint("calc_note IS NULL OR char_length(calc_note) <= 2000",
                           name="ck_claim_items_calc_note"),
        sa.CheckConstraint("amount_cents BETWEEN 0 AND 9007199254740991",
                           name="ck_claim_items_amount"),
        sa.CheckConstraint("position >= 0", name="ck_claim_items_position"),
        sa.CheckConstraint("revision >= 0", name="ck_claim_items_revision"),
        sa.CheckConstraint("num_nonnulls(segment_id, material_id, annotation_id) <= 1",
                           name="ck_claim_items_one_source"),
    )
    op.create_index("ix_claim_items_case_group", "claim_items", ["case_id", "group_id"])

    op.create_table(
        "claim_changes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("entry", sa.Text(), nullable=False),
        sa.Column("diff", JSONB, nullable=False),
        sa.Column("review_state", sa.Text(), nullable=False, server_default="none"),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("reverted_by", sa.Uuid()),
        sa.Column("request_key", sa.Text()),
        sa.Column("request_fingerprint", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_claim_changes_case"),
        sa.UniqueConstraint("case_id", "id", name="uq_claim_changes_case_id"),
        sa.UniqueConstraint("case_id", "seq", name="uq_claim_changes_seq"),
        sa.UniqueConstraint("case_id", "request_key", name="uq_claim_changes_request_key"),
        sa.CheckConstraint("actor IN ('human', 'agent')", name="ck_claim_changes_actor"),
        sa.CheckConstraint("review_state IN ('none', 'pending', 'accepted', 'reverted')",
                           name="ck_claim_changes_review"),
    )
    op.create_index("ix_claim_changes_case_review", "claim_changes",
                    ["case_id", "review_state"])

    operations = _audit_operations()
    for name in CLAIM_OPERATIONS:
        if name not in operations:
            operations.append(name)
    _write_audit_operations(operations)


def downgrade() -> None:
    # Claim audit rows and the claims sequence rows describe data dropped below;
    # the parent-era CHECK cannot hold them.  Remove only claims.* / domain
    # 'claims' and keep whatever the parent revision registered (0007's
    # graph/evidence operations and other domains survive a 0008 downgrade).
    op.execute("DELETE FROM audit_events WHERE operation LIKE 'claims.%'")
    op.execute("DELETE FROM case_change_seq WHERE domain = 'claims'")
    operations = [name for name in _audit_operations() if not name.startswith("claims.")]
    _write_audit_operations(operations)
    op.drop_table("claim_changes")
    op.drop_table("claim_items")
    op.drop_table("claim_groups")
