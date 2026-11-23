"""Add case progress: node tree, relations, links, change log, change sequence.

Revision ID: 0005_case_progress
Revises: 0004_case_workbench

Structural decisions (see docs/decisions/20261116-进度通用节点模型.md):
- hierarchy/kinds/states/relation types/fragments live in progress_rules config,
  not in DB constraints; the DB only enforces non-null, same-case composite FKs
  and a few structural checks.
- every write records a before/after change row; agent changes are pending review.
- case_change_seq drives frontend polling.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_case_progress"
down_revision = "0004_case_workbench"
branch_labels = None
depends_on = None

PROGRESS_OPERATIONS = (
    "progress.add_node", "progress.update_node", "progress.move_node",
    "progress.set_active_stage", "progress.insert_fragment", "progress.remove_node",
    "progress.restore_node", "progress.add_relation", "progress.remove_relation",
    "progress.link", "progress.unlink", "progress.accept_change", "progress.accept_all",
    "progress.revert_change",
)

OLD_OPERATIONS = (
    "session.login", "session.logout", "case.create", "intake.import",
    "intake.upload", "material.classify", "material.rename",
    "material.working_copy", "file_version.register", "parse.reparse",
    "analysis.save", "analysis.review", "backup.create", "backup.restore",
    "maintenance.clear",
)


def upgrade() -> None:
    # annotations needs UNIQUE(case_id, id) for the progress_links composite FK.
    op.create_unique_constraint("uq_annotations_case_id", "annotations", ["case_id", "id"])

    op.create_table(
        "progress_nodes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("parent_id", sa.Uuid()),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("date_start", sa.DateTime(timezone=True)),
        sa.Column("date_end", sa.DateTime(timezone=True)),
        sa.Column("template_ref", sa.Text()),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.Column("request_key", sa.Text()),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_progress_nodes_case"),
        sa.ForeignKeyConstraint(["case_id", "parent_id"],
                                ["progress_nodes.case_id", "progress_nodes.id"],
                                name="fk_progress_nodes_parent"),
        sa.UniqueConstraint("case_id", "id", name="uq_progress_nodes_case_id"),
        sa.UniqueConstraint("case_id", "request_key", name="uq_progress_nodes_request_key"),
        sa.CheckConstraint("created_by IN ('human', 'agent')", name="ck_progress_nodes_created_by"),
        sa.CheckConstraint("updated_by IN ('human', 'agent')", name="ck_progress_nodes_updated_by"),
        sa.CheckConstraint("char_length(title) BETWEEN 1 AND 200", name="ck_progress_nodes_title"),
        sa.CheckConstraint("note IS NULL OR char_length(note) <= 4000", name="ck_progress_nodes_note"),
        sa.CheckConstraint("position >= 0", name="ck_progress_nodes_position"),
        sa.CheckConstraint("revision >= 0", name="ck_progress_nodes_revision"),
    )
    op.create_index("ix_progress_nodes_case_parent", "progress_nodes", ["case_id", "parent_id"])

    op.create_table(
        "progress_relations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("from_node_id", sa.Uuid(), nullable=False),
        sa.Column("to_node_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_progress_relations_case"),
        sa.ForeignKeyConstraint(["case_id", "from_node_id"],
                                ["progress_nodes.case_id", "progress_nodes.id"],
                                name="fk_progress_relations_from"),
        sa.ForeignKeyConstraint(["case_id", "to_node_id"],
                                ["progress_nodes.case_id", "progress_nodes.id"],
                                name="fk_progress_relations_to"),
        sa.UniqueConstraint("case_id", "id", name="uq_progress_relations_case_id"),
        sa.CheckConstraint("created_by IN ('human', 'agent')", name="ck_progress_relations_created_by"),
        sa.CheckConstraint("from_node_id <> to_node_id", name="ck_progress_relations_not_self"),
    )
    # 审查 06：同案同向同类型的有效关系唯一（软删除后可重建，部分索引只约束未删除行）。
    op.create_index(
        "uq_progress_relations_active", "progress_relations",
        ["case_id", "from_node_id", "to_node_id", "type"],
        unique=True, postgresql_where=sa.text("removed_at IS NULL"),
    )

    op.create_table(
        "progress_links",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("material_id", sa.Uuid()),
        sa.Column("analysis_id", sa.Uuid()),
        sa.Column("annotation_id", sa.Uuid()),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_progress_links_case"),
        sa.ForeignKeyConstraint(["case_id", "node_id"],
                                ["progress_nodes.case_id", "progress_nodes.id"],
                                name="fk_progress_links_node"),
        sa.ForeignKeyConstraint(["case_id", "material_id"],
                                ["materials.case_id", "materials.id"],
                                name="fk_progress_links_material"),
        sa.ForeignKeyConstraint(["case_id", "analysis_id"],
                                ["analyses.case_id", "analyses.id"],
                                name="fk_progress_links_analysis"),
        sa.ForeignKeyConstraint(["case_id", "annotation_id"],
                                ["annotations.case_id", "annotations.id"],
                                name="fk_progress_links_annotation"),
        sa.UniqueConstraint("case_id", "id", name="uq_progress_links_case_id"),
        sa.CheckConstraint("created_by IN ('human', 'agent')", name="ck_progress_links_created_by"),
        sa.CheckConstraint("num_nonnulls(material_id, analysis_id, annotation_id) = 1",
                           name="ck_progress_links_one_target"),
    )
    # 审查 06：同节点同对象唯一。原四列普通 UNIQUE 含两个 NULL 时 PG 视为不同，
    # 允许重复；改用三个部分唯一索引，各自只约束对应目标非空的行。
    op.create_index(
        "uq_progress_links_material", "progress_links", ["node_id", "material_id"],
        unique=True, postgresql_where=sa.text("material_id IS NOT NULL"),
    )
    op.create_index(
        "uq_progress_links_analysis", "progress_links", ["node_id", "analysis_id"],
        unique=True, postgresql_where=sa.text("analysis_id IS NOT NULL"),
    )
    op.create_index(
        "uq_progress_links_annotation", "progress_links", ["node_id", "annotation_id"],
        unique=True, postgresql_where=sa.text("annotation_id IS NOT NULL"),
    )

    op.create_table(
        "progress_changes",
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
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_progress_changes_case"),
        sa.UniqueConstraint("case_id", "id", name="uq_progress_changes_case_id"),
        sa.UniqueConstraint("case_id", "seq", name="uq_progress_changes_seq"),
        sa.CheckConstraint("actor IN ('human', 'agent')", name="ck_progress_changes_actor"),
        sa.CheckConstraint("review_state IN ('none', 'pending', 'accepted', 'reverted')",
                           name="ck_progress_changes_review"),
    )
    op.create_index("ix_progress_changes_case_review", "progress_changes",
                    ["case_id", "review_state"])

    op.create_table(
        "case_change_seq",
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_case_change_seq_case"),
        sa.PrimaryKeyConstraint("case_id", "domain"),
    )

    # Extend the closed audit operation set with progress.* names.
    op.drop_constraint("ck_audit_events_operation", "audit_events", type_="check")
    values = ", ".join(f"'{name}'" for name in (*OLD_OPERATIONS, *PROGRESS_OPERATIONS))
    op.create_check_constraint("ck_audit_events_operation", "audit_events",
                               f"operation IN ({values})")

    # K14: instantiate the default fragment for existing cases. Done in the
    # service layer after migration via progress.backfill_default_fragments so
    # the rule config stays the single source of truth (no SQL duplication).


def downgrade() -> None:
    # Progress audit rows reference operations outside the 0004 closed set;
    # remove them before restoring the narrower CHECK (they describe progress
    # data that is being dropped anyway).
    op.execute("DELETE FROM audit_events WHERE operation LIKE 'progress.%'")
    op.drop_constraint("ck_audit_events_operation", "audit_events", type_="check")
    values = ", ".join(f"'{name}'" for name in OLD_OPERATIONS)
    op.create_check_constraint("ck_audit_events_operation", "audit_events",
                               f"operation IN ({values})")
    op.drop_table("case_change_seq")
    op.drop_table("progress_changes")
    op.drop_table("progress_links")
    op.drop_table("progress_relations")
    op.drop_table("progress_nodes")
    op.drop_constraint("uq_annotations_case_id", "annotations", type_="unique")