"""Relation graph, evidence catalog, snapshots and consents.

Revision ID: 0007_graph_evidence
Revises: 0006_case_archive_time

The revision id is deliberately short: ``alembic_version.version_num`` is
VARCHAR(32) (created by Alembic itself in 0001) and must not be widened.
The parallel 0008 integration will chain onto this same id.

Structural decisions (docs/decisions/20261120-主体关系图与证据目录模型.md):
- D1: entities, aliases, relations and sources belong to the *case*; a graph is
  a named view that stores membership, per-graph layout coordinates and groups.
- D2: the evidence catalog is the only source of evidence numbers; graphs store
  stable references only (segment / material / annotation).
- D3: snapshots freeze one graph as immutable JSONB (including the evidence
  labels of that moment); consents attach to a snapshot and are append-only.
- D4: change logs are per domain (``graph_changes`` / ``evidence_changes``)
  sharing ``case_change_seq``; ``progress_changes`` is not touched.

The migration only adds tables, indexes and the extended audit CHECK.  Existing
tables keep their columns and constraints (checklist item 7).  Snapshot and
consent rows are additionally protected by row triggers so that immutability is
a database guarantee, not only a service convention.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007_graph_evidence"
down_revision = "0006_case_archive_time"
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

ARCHIVE_TIME_OPERATIONS = (
    "case.archive", "case.unarchive",
    "time.start", "time.stop", "time.add", "time.remove",
)

GRAPH_OPERATIONS = (
    "graph.add_entity", "graph.update_entity", "graph.remove_entity",
    "graph.restore_entity", "graph.add_alias", "graph.remove_alias",
    "graph.add_relation", "graph.update_relation", "graph.remove_relation",
    "graph.restore_relation", "graph.add_source", "graph.remove_source",
    "graph.create_graph", "graph.update_graph", "graph.remove_graph",
    "graph.add_to_graph", "graph.remove_from_graph", "graph.save_layout",
    "graph.upsert_group", "graph.remove_group",
    "graph.create_snapshot", "graph.restore_snapshot", "graph.add_snapshot_consent",
    "graph.accept_change", "graph.accept_all", "graph.revert_change",
)

EVIDENCE_OPERATIONS = (
    "evidence.add_group", "evidence.update_group", "evidence.reorder_groups",
    "evidence.remove_group", "evidence.add_item", "evidence.update_item",
    "evidence.move_item", "evidence.remove_item", "evidence.add_binding",
    "evidence.remove_binding", "evidence.confirm_item", "evidence.reject_item",
    "evidence.confirm_binding", "evidence.reject_binding", "evidence.import_catalog",
    "evidence.accept_change", "evidence.accept_all", "evidence.revert_change",
)

ACTORS_CHECK = "created_by IN ('human', 'agent')"


def _immutable_trigger(table: str, function: str, message: str) -> None:
    op.execute(
        f"CREATE FUNCTION {function}() RETURNS trigger AS $$\n"
        f"BEGIN\n"
        f"    RAISE EXCEPTION '{message}';\n"
        f"END;\n"
        f"$$ LANGUAGE plpgsql"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION {function}()"
    )


def upgrade() -> None:
    # --- A. case-level entities, aliases, relations, sources ------------------
    op.create_table(
        "case_entities",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("roles", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
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
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_case_entities_case"),
        sa.UniqueConstraint("case_id", "id", name="uq_case_entities_case_id"),
        sa.UniqueConstraint("case_id", "request_key", name="uq_case_entities_request_key"),
        sa.CheckConstraint(ACTORS_CHECK, name="ck_case_entities_created_by"),
        sa.CheckConstraint("updated_by IN ('human', 'agent')", name="ck_case_entities_updated_by"),
        sa.CheckConstraint("char_length(name) BETWEEN 1 AND 200", name="ck_case_entities_name"),
        sa.CheckConstraint("note IS NULL OR char_length(note) <= 4000", name="ck_case_entities_note"),
        sa.CheckConstraint("revision >= 0", name="ck_case_entities_revision"),
    )
    op.create_index("ix_case_entities_case", "case_entities", ["case_id"])

    op.create_table(
        "entity_aliases",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("alias", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_entity_aliases_case"),
        sa.ForeignKeyConstraint(["case_id", "entity_id"],
                                ["case_entities.case_id", "case_entities.id"],
                                name="fk_entity_aliases_entity"),
        sa.UniqueConstraint("case_id", "id", name="uq_entity_aliases_case_id"),
        sa.CheckConstraint(ACTORS_CHECK, name="ck_entity_aliases_created_by"),
        sa.CheckConstraint("char_length(alias) BETWEEN 1 AND 200", name="ck_entity_aliases_alias"),
    )
    op.create_index("uq_entity_aliases_active", "entity_aliases",
                    ["case_id", "entity_id", sa.text("lower(alias)")], unique=True,
                    postgresql_where=sa.text("removed_at IS NULL"))

    op.create_table(
        "case_relations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("from_entity_id", sa.Uuid(), nullable=False),
        sa.Column("to_entity_id", sa.Uuid(), nullable=False),
        sa.Column("directed", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("label", sa.Text()),
        sa.Column("credibility", sa.Text(), nullable=False),
        sa.Column("attrs", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
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
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_case_relations_case"),
        sa.ForeignKeyConstraint(["case_id", "from_entity_id"],
                                ["case_entities.case_id", "case_entities.id"],
                                name="fk_case_relations_from"),
        sa.ForeignKeyConstraint(["case_id", "to_entity_id"],
                                ["case_entities.case_id", "case_entities.id"],
                                name="fk_case_relations_to"),
        sa.UniqueConstraint("case_id", "id", name="uq_case_relations_case_id"),
        sa.UniqueConstraint("case_id", "request_key", name="uq_case_relations_request_key"),
        sa.CheckConstraint(ACTORS_CHECK, name="ck_case_relations_created_by"),
        sa.CheckConstraint("updated_by IN ('human', 'agent')", name="ck_case_relations_updated_by"),
        sa.CheckConstraint(
            "credibility IN ('evidenced', 'suspected', 'disputed', 'ruled_out')",
            name="ck_case_relations_credibility"),
        sa.CheckConstraint("from_entity_id <> to_entity_id", name="ck_case_relations_not_self"),
        sa.CheckConstraint("label IS NULL OR char_length(label) <= 200",
                           name="ck_case_relations_label"),
        sa.CheckConstraint("note IS NULL OR char_length(note) <= 4000", name="ck_case_relations_note"),
        sa.CheckConstraint("revision >= 0", name="ck_case_relations_revision"),
    )
    op.create_index(
        "uq_case_relations_active", "case_relations",
        ["case_id", "from_entity_id", "to_entity_id", "kind"],
        unique=True, postgresql_where=sa.text("removed_at IS NULL"),
    )

    op.create_table(
        "graph_sources",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("entity_id", sa.Uuid()),
        sa.Column("relation_id", sa.Uuid()),
        sa.Column("segment_id", sa.Uuid()),
        sa.Column("material_id", sa.Uuid()),
        sa.Column("annotation_id", sa.Uuid()),
        sa.Column("proves", sa.Text()),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_graph_sources_case"),
        sa.ForeignKeyConstraint(["case_id", "entity_id"],
                                ["case_entities.case_id", "case_entities.id"],
                                name="fk_graph_sources_entity"),
        sa.ForeignKeyConstraint(["case_id", "relation_id"],
                                ["case_relations.case_id", "case_relations.id"],
                                name="fk_graph_sources_relation"),
        sa.ForeignKeyConstraint(["case_id", "segment_id"], ["segments.case_id", "segments.id"],
                                name="fk_graph_sources_segment"),
        sa.ForeignKeyConstraint(["case_id", "material_id"], ["materials.case_id", "materials.id"],
                                name="fk_graph_sources_material"),
        sa.ForeignKeyConstraint(["case_id", "annotation_id"],
                                ["annotations.case_id", "annotations.id"],
                                name="fk_graph_sources_annotation"),
        sa.UniqueConstraint("case_id", "id", name="uq_graph_sources_case_id"),
        sa.CheckConstraint(ACTORS_CHECK, name="ck_graph_sources_created_by"),
        sa.CheckConstraint("num_nonnulls(entity_id, relation_id) = 1",
                           name="ck_graph_sources_one_target"),
        sa.CheckConstraint("num_nonnulls(segment_id, material_id, annotation_id) = 1",
                           name="ck_graph_sources_one_ref"),
        sa.CheckConstraint("proves IS NULL OR char_length(proves) <= 500",
                           name="ck_graph_sources_proves"),
    )
    op.create_index("ix_graph_sources_case_entity", "graph_sources", ["case_id", "entity_id"])
    op.create_index("ix_graph_sources_case_relation", "graph_sources", ["case_id", "relation_id"])

    # --- B. graphs, layout, snapshots, consents ------------------------------
    op.create_table(
        "relation_graphs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
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
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_relation_graphs_case"),
        sa.UniqueConstraint("case_id", "id", name="uq_relation_graphs_case_id"),
        sa.UniqueConstraint("case_id", "request_key", name="uq_relation_graphs_request_key"),
        sa.CheckConstraint(ACTORS_CHECK, name="ck_relation_graphs_created_by"),
        sa.CheckConstraint("updated_by IN ('human', 'agent')", name="ck_relation_graphs_updated_by"),
        sa.CheckConstraint("char_length(title) BETWEEN 1 AND 200", name="ck_relation_graphs_title"),
        sa.CheckConstraint("description IS NULL OR char_length(description) <= 2000",
                           name="ck_relation_graphs_description"),
        sa.CheckConstraint("revision >= 0", name="ck_relation_graphs_revision"),
    )

    op.create_table(
        "relation_graph_groups",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("graph_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("color", sa.Text()),
        sa.Column("x", sa.Double(), nullable=False, server_default="0"),
        sa.Column("y", sa.Double(), nullable=False, server_default="0"),
        sa.Column("width", sa.Double(), nullable=False, server_default="360"),
        sa.Column("height", sa.Double(), nullable=False, server_default="220"),
        sa.Column("collapsed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_relation_graph_groups_case"),
        sa.ForeignKeyConstraint(["case_id", "graph_id"],
                                ["relation_graphs.case_id", "relation_graphs.id"],
                                name="fk_relation_graph_groups_graph"),
        sa.UniqueConstraint("case_id", "id", name="uq_relation_graph_groups_case_id"),
        # 同图分组作用域：节点 group_id 的组合外键引用此键，跨图分组由数据库拒绝。
        sa.UniqueConstraint("case_id", "graph_id", "id",
                            name="uq_relation_graph_groups_graph_id"),
        sa.CheckConstraint("char_length(title) BETWEEN 1 AND 200",
                           name="ck_relation_graph_groups_title"),
        sa.CheckConstraint("color IS NULL OR char_length(color) <= 40",
                           name="ck_relation_graph_groups_color"),
        sa.CheckConstraint("width > 0 AND height > 0", name="ck_relation_graph_groups_size"),
        sa.CheckConstraint("revision >= 0", name="ck_relation_graph_groups_revision"),
    )

    op.create_table(
        "relation_graph_nodes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("graph_id", sa.Uuid(), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid()),
        sa.Column("x", sa.Double(), nullable=False, server_default="0"),
        sa.Column("y", sa.Double(), nullable=False, server_default="0"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_relation_graph_nodes_case"),
        sa.ForeignKeyConstraint(["case_id", "graph_id"],
                                ["relation_graphs.case_id", "relation_graphs.id"],
                                name="fk_relation_graph_nodes_graph"),
        sa.ForeignKeyConstraint(["case_id", "entity_id"],
                                ["case_entities.case_id", "case_entities.id"],
                                name="fk_relation_graph_nodes_entity"),
        sa.ForeignKeyConstraint(["case_id", "graph_id", "group_id"],
                                ["relation_graph_groups.case_id",
                                 "relation_graph_groups.graph_id",
                                 "relation_graph_groups.id"],
                                name="fk_relation_graph_nodes_group"),
        sa.UniqueConstraint("graph_id", "entity_id", name="uq_relation_graph_nodes_member"),
        sa.UniqueConstraint("case_id", "id", name="uq_relation_graph_nodes_case_id"),
        sa.CheckConstraint("revision >= 0", name="ck_relation_graph_nodes_revision"),
    )

    op.create_table(
        "relation_graph_edges",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("graph_id", sa.Uuid(), nullable=False),
        sa.Column("relation_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_relation_graph_edges_case"),
        sa.ForeignKeyConstraint(["case_id", "graph_id"],
                                ["relation_graphs.case_id", "relation_graphs.id"],
                                name="fk_relation_graph_edges_graph"),
        sa.ForeignKeyConstraint(["case_id", "relation_id"],
                                ["case_relations.case_id", "case_relations.id"],
                                name="fk_relation_graph_edges_relation"),
        sa.UniqueConstraint("graph_id", "relation_id", name="uq_relation_graph_edges_member"),
        sa.UniqueConstraint("case_id", "id", name="uq_relation_graph_edges_case_id"),
    )

    op.create_table(
        "relation_graph_snapshots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("graph_id", sa.Uuid(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("stage_label", sa.Text(), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("progress_node_id", sa.Uuid()),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("excluded_pending_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.Text(), nullable=False, server_default=sa.text("'human'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_relation_graph_snapshots_case"),
        sa.ForeignKeyConstraint(["case_id", "graph_id"],
                                ["relation_graphs.case_id", "relation_graphs.id"],
                                name="fk_relation_graph_snapshots_graph"),
        sa.ForeignKeyConstraint(["case_id", "progress_node_id"],
                                ["progress_nodes.case_id", "progress_nodes.id"],
                                name="fk_relation_graph_snapshots_progress_node"),
        sa.UniqueConstraint("graph_id", "version_no", name="uq_relation_graph_snapshots_version"),
        sa.UniqueConstraint("case_id", "id", name="uq_relation_graph_snapshots_case_id"),
        sa.CheckConstraint("version_no >= 1", name="ck_relation_graph_snapshots_version_no"),
        sa.CheckConstraint("char_length(stage_label) BETWEEN 1 AND 100",
                           name="ck_relation_graph_snapshots_stage"),
        sa.CheckConstraint("note IS NULL OR char_length(note) <= 2000",
                           name="ck_relation_graph_snapshots_note"),
        sa.CheckConstraint("excluded_pending_count >= 0",
                           name="ck_relation_graph_snapshots_pending"),
        sa.CheckConstraint("created_by = 'human'", name="ck_relation_graph_snapshots_actor"),
    )

    op.create_table(
        "snapshot_consents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("parent_consent_id", sa.Uuid()),
        sa.Column("consenter", sa.Text(), nullable=False),
        sa.Column("consented_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("source_detail", sa.Text(), nullable=False),
        sa.Column("evidence_material_id", sa.Uuid()),
        sa.Column("note", sa.Text()),
        sa.Column("created_by", sa.Text(), nullable=False, server_default=sa.text("'human'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_snapshot_consents_case"),
        sa.ForeignKeyConstraint(["case_id", "snapshot_id"],
                                ["relation_graph_snapshots.case_id",
                                 "relation_graph_snapshots.id"],
                                name="fk_snapshot_consents_snapshot"),
        sa.ForeignKeyConstraint(["case_id", "snapshot_id", "parent_consent_id"],
                                ["snapshot_consents.case_id",
                                 "snapshot_consents.snapshot_id",
                                 "snapshot_consents.id"],
                                name="fk_snapshot_consents_parent"),
        sa.ForeignKeyConstraint(["case_id", "evidence_material_id"],
                                ["materials.case_id", "materials.id"],
                                name="fk_snapshot_consents_material"),
        sa.UniqueConstraint("case_id", "id", name="uq_snapshot_consents_case_id"),
        # 同快照同意作用域：parent_consent_id 的组合外键引用此键，
        # 更正/撤回不能指向另一份快照上的同意记录。
        sa.UniqueConstraint("case_id", "snapshot_id", "id",
                            name="uq_snapshot_consents_snapshot_id"),
        sa.CheckConstraint("action IN ('confirm', 'amend', 'revoke')",
                           name="ck_snapshot_consents_action"),
        sa.CheckConstraint("char_length(consenter) BETWEEN 1 AND 200",
                           name="ck_snapshot_consents_consenter"),
        sa.CheckConstraint("char_length(source_detail) BETWEEN 1 AND 1000",
                           name="ck_snapshot_consents_source_detail"),
        sa.CheckConstraint("note IS NULL OR char_length(note) <= 2000",
                           name="ck_snapshot_consents_note"),
        sa.CheckConstraint("created_by = 'human'", name="ck_snapshot_consents_actor"),
        sa.CheckConstraint(
            "(action = 'confirm' AND parent_consent_id IS NULL) "
            "OR (action IN ('amend', 'revoke') AND parent_consent_id IS NOT NULL)",
            name="ck_snapshot_consents_parent"),
        # 同意记录不能指向自己（组合外键已保证同案同快照，这里再排除自指）。
        sa.CheckConstraint("parent_consent_id IS NULL OR parent_consent_id <> id",
                           name="ck_snapshot_consents_not_self"),
    )

    # D3: a frozen snapshot and a recorded consent can never be rewritten or
    # deleted; corrections are appended as new rows.
    _immutable_trigger("relation_graph_snapshots", "lwb_snapshot_immutable",
                       "快照不可修改：请追加新版本，不要改写已冻结快照")
    _immutable_trigger("snapshot_consents", "lwb_consent_immutable",
                       "客户同意记录不可修改：请追加更正或撤回记录")

    # --- C. evidence catalog and change logs ---------------------------------
    op.create_table(
        "evidence_groups",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text()),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("request_key", sa.Text()),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_evidence_groups_case"),
        sa.UniqueConstraint("case_id", "id", name="uq_evidence_groups_case_id"),
        sa.UniqueConstraint("case_id", "request_key", name="uq_evidence_groups_request_key"),
        sa.CheckConstraint(ACTORS_CHECK, name="ck_evidence_groups_created_by"),
        sa.CheckConstraint("char_length(title) BETWEEN 1 AND 200", name="ck_evidence_groups_title"),
        sa.CheckConstraint("purpose IS NULL OR char_length(purpose) <= 1000",
                           name="ck_evidence_groups_purpose"),
        sa.CheckConstraint("position >= 0", name="ck_evidence_groups_position"),
        sa.CheckConstraint("revision >= 0", name="ck_evidence_groups_revision"),
    )

    op.create_table(
        "evidence_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text()),
        sa.Column("source_kind", sa.Text(), nullable=False, server_default=sa.text("'manual'")),
        sa.Column("import_material_id", sa.Uuid()),
        sa.Column("review_state", sa.Text(), nullable=False, server_default=sa.text("'confirmed'")),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("request_key", sa.Text()),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_evidence_items_case"),
        sa.ForeignKeyConstraint(["case_id", "group_id"],
                                ["evidence_groups.case_id", "evidence_groups.id"],
                                name="fk_evidence_items_group"),
        sa.ForeignKeyConstraint(["case_id", "import_material_id"],
                                ["materials.case_id", "materials.id"],
                                name="fk_evidence_items_import_material"),
        sa.UniqueConstraint("case_id", "id", name="uq_evidence_items_case_id"),
        sa.UniqueConstraint("case_id", "request_key", name="uq_evidence_items_request_key"),
        sa.CheckConstraint(ACTORS_CHECK, name="ck_evidence_items_created_by"),
        sa.CheckConstraint("source_kind IN ('manual', 'imported')",
                           name="ck_evidence_items_source_kind"),
        sa.CheckConstraint("review_state IN ('confirmed', 'pending', 'rejected')",
                           name="ck_evidence_items_review"),
        sa.CheckConstraint("char_length(title) BETWEEN 1 AND 300", name="ck_evidence_items_title"),
        sa.CheckConstraint("purpose IS NULL OR char_length(purpose) <= 2000",
                           name="ck_evidence_items_purpose"),
        sa.CheckConstraint("position >= 0", name="ck_evidence_items_position"),
        sa.CheckConstraint("revision >= 0", name="ck_evidence_items_revision"),
    )
    op.create_index("ix_evidence_items_case_group", "evidence_items", ["case_id", "group_id"])

    op.create_table(
        "evidence_item_bindings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("material_id", sa.Uuid(), nullable=False),
        sa.Column("page_start", sa.Integer()),
        sa.Column("page_end", sa.Integer()),
        sa.Column("review_state", sa.Text(), nullable=False, server_default=sa.text("'confirmed'")),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name="fk_evidence_item_bindings_case"),
        sa.ForeignKeyConstraint(["case_id", "item_id"],
                                ["evidence_items.case_id", "evidence_items.id"],
                                name="fk_evidence_item_bindings_item"),
        sa.ForeignKeyConstraint(["case_id", "material_id"],
                                ["materials.case_id", "materials.id"],
                                name="fk_evidence_item_bindings_material"),
        sa.UniqueConstraint("case_id", "id", name="uq_evidence_item_bindings_case_id"),
        sa.CheckConstraint(ACTORS_CHECK, name="ck_evidence_item_bindings_created_by"),
        sa.CheckConstraint("review_state IN ('confirmed', 'pending', 'rejected')",
                           name="ck_evidence_item_bindings_review"),
        sa.CheckConstraint("page_start IS NULL OR page_start >= 1",
                           name="ck_evidence_item_bindings_page_start"),
        sa.CheckConstraint("page_end IS NULL OR (page_start IS NOT NULL AND page_end >= page_start)",
                           name="ck_evidence_item_bindings_page_end"),
    )
    op.create_index("ix_evidence_item_bindings_case_item", "evidence_item_bindings",
                    ["case_id", "item_id"])
    op.create_index("ix_evidence_item_bindings_case_material", "evidence_item_bindings",
                    ["case_id", "material_id"])

    for table, prefix in (("graph_changes", "graph"), ("evidence_changes", "evidence")):
        op.create_table(
            table,
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("case_id", sa.Uuid(), nullable=False),
            sa.Column("seq", sa.BigInteger(), nullable=False),
            sa.Column("command", sa.Text(), nullable=False),
            sa.Column("actor", sa.Text(), nullable=False),
            sa.Column("entry", sa.Text(), nullable=False),
            sa.Column("diff", JSONB, nullable=False),
            sa.Column("review_state", sa.Text(), nullable=False, server_default=sa.text("'none'")),
            sa.Column("reviewed_at", sa.DateTime(timezone=True)),
            sa.Column("reverted_by", sa.Uuid()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name=f"fk_{table}_case"),
            sa.UniqueConstraint("case_id", "id", name=f"uq_{table}_case_id"),
            sa.UniqueConstraint("case_id", "seq", name=f"uq_{table}_seq"),
            sa.CheckConstraint("actor IN ('human', 'agent')", name=f"ck_{table}_actor"),
            sa.CheckConstraint("review_state IN ('none', 'pending', 'accepted', 'reverted')",
                               name=f"ck_{table}_review"),
        )
        op.create_index(f"ix_{table}_case_review", table, ["case_id", "review_state"])

    # Extend the closed audit operation set with graph.* and evidence.* names.
    op.drop_constraint("ck_audit_events_operation", "audit_events", type_="check")
    values = ", ".join(f"'{name}'" for name in (
        *OLD_OPERATIONS, *PROGRESS_OPERATIONS, *ARCHIVE_TIME_OPERATIONS,
        *GRAPH_OPERATIONS, *EVIDENCE_OPERATIONS))
    op.create_check_constraint("ck_audit_events_operation", "audit_events",
                               f"operation IN ({values})")

    # App role gets DML on the new tables (same pattern as 0001).  Without this,
    # a fresh install where lwb_app exists would leave every 0007 table
    # unreadable/unwritable for the application until a backup/restore re-grant.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lwb_app') THEN
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO lwb_app';
                EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO lwb_app';
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    # graph/evidence seq rows describe domains this revision introduced; the
    # 0006-era code only knows 'progress', so drop our own rows and leave the
    # progress sequence untouched (same rule 0008 applies to 'claims').
    op.execute("DELETE FROM case_change_seq WHERE domain IN ('graph', 'evidence')")
    op.execute("DELETE FROM audit_events WHERE operation LIKE 'graph.%' "
               "OR operation LIKE 'evidence.%'")
    op.drop_constraint("ck_audit_events_operation", "audit_events", type_="check")
    values = ", ".join(f"'{name}'" for name in (
        *OLD_OPERATIONS, *PROGRESS_OPERATIONS, *ARCHIVE_TIME_OPERATIONS))
    op.create_check_constraint("ck_audit_events_operation", "audit_events",
                               f"operation IN ({values})")

    op.drop_table("evidence_changes")
    op.drop_table("graph_changes")
    op.drop_table("evidence_item_bindings")
    op.drop_table("evidence_items")
    op.drop_table("evidence_groups")

    for table, function in (("snapshot_consents", "lwb_consent_immutable"),
                            ("relation_graph_snapshots", "lwb_snapshot_immutable")):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS {function}()")

    op.drop_table("snapshot_consents")
    op.drop_table("relation_graph_snapshots")
    op.drop_table("relation_graph_edges")
    op.drop_table("relation_graph_nodes")
    op.drop_table("relation_graph_groups")
    op.drop_table("relation_graphs")
    op.drop_table("graph_sources")
    op.drop_table("case_relations")
    op.drop_table("entity_aliases")
    op.drop_table("case_entities")
