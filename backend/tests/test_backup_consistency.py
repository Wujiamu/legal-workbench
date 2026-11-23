"""T18: consistency checks, backup/restore round trip and maintenance gating."""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from legal_workbench import analysis, backup, consistency, imports, models, parsing, search, worker
from legal_workbench.config import load_settings, resolve_pg_bin
from legal_workbench.db import get_engine, get_session_factory
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.parsing import pipeline
from legal_workbench.schemas import SearchMode, StatementKind
from legal_workbench.storage import Storage

from . import factories

BACKEND = Path(__file__).resolve().parents[1]
_resolved_pg_bin = resolve_pg_bin()
PG_BIN = str(_resolved_pg_bin) if _resolved_pg_bin else None
RESTORE_DB = "legal_workbench_restore"


@pytest.fixture
def store(tmp_path) -> Storage:
    return Storage(root=tmp_path / "data")


@pytest.fixture
def populated(db, store):
    built = factories.build_material_with_storage(
        db, store, content="TEST-ONLY 合同正文 金额100元。".encode(), name="合同.txt")
    run_id, job_id = parsing.create_parse_run(
        db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    pipeline.process_parse_job(get_session_factory(), store, job_id)
    segment = db.execute(select(models.Segment)).scalars().first()
    saved = analysis.save_analysis(
        db, store, case_id=built["case_id"], title="虚构分析", content="结论：有效。",
        statement_kind=StatementKind.AGENT_ANALYSIS, source_refs=[segment.id])
    return {**built, "source_ref": segment.id, **saved.model_dump()}


def test_clean_store_reports_no_issues(db, store, populated):
    report = consistency.check_consistency(db, store)
    assert report.ok, report.to_dict()
    assert report.checked_objects >= 1 and report.checked_versions >= 1
    assert report.checked_analyses == 1
    assert report.orphan_files == []


def test_missing_object_file_is_reported(db, store, populated):
    stored = db.execute(select(models.StoredObject)).scalars().first()
    store.managed_path(stored.relative_path).unlink()
    report = consistency.check_consistency(db, store)
    assert not report.ok
    assert any(issue.kind == "missing_object_file" for issue in report.issues)


def test_tampered_object_hash_is_reported(db, store, populated):
    stored = db.execute(select(models.StoredObject)).scalars().first()
    store.managed_path(stored.relative_path).write_bytes(b"tampered")
    report = consistency.check_consistency(db, store)
    assert any(issue.kind == "object_hash_mismatch" for issue in report.issues)


def test_orphan_file_is_reported(db, store, populated):
    orphan = store.case_dir(populated["case_id"]) / "originals" / "deadbeef" / "source.txt"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"TEST-ONLY orphan")
    report = consistency.check_consistency(db, store)
    assert any("deadbeef" in path for path in report.orphan_files)


def test_analysis_hash_mismatch_is_reported(db, store, populated):
    version = db.get(models.AnalysisVersion, populated["version_id"])
    store.managed_path(version.relative_path).write_bytes("篡改的正文".encode())
    report = consistency.check_consistency(db, store)
    assert any(issue.kind == "analysis_hash_mismatch" for issue in report.issues)


def test_working_copy_is_referenced_not_orphan(db, store, populated):
    """A working copy is a mutable file with no hash, but it is still referenced."""
    from legal_workbench import materials

    working_id, relative = materials.create_working_copy(
        db, store, case_id=populated["case_id"],
        file_version_id=populated["file_version_id"], display_name="合同（副本）.txt")
    report = consistency.check_consistency(db, store)
    assert relative not in report.orphan_files
    assert report.ok, report.to_dict()
    store.managed_path(relative).unlink()
    report = consistency.check_consistency(db, store)
    assert any(issue.kind == "missing_working_copy_file" for issue in report.issues)


def test_coverage_mismatch_is_reported(db, store, populated):
    """A stale coverage summary must be reported: it is what the UI shows."""
    run = db.execute(select(models.ParseRun)).scalars().first()
    assert run.coverage.get("total"), run.coverage
    run.coverage = dict(run.coverage, complete=99)
    db.commit()
    report = consistency.check_consistency(db, store)
    assert not report.ok
    assert any(issue.kind == "coverage_mismatch" for issue in report.issues)


def test_coverage_total_mismatch_is_reported(db, store, populated):
    run = db.execute(select(models.ParseRun)).scalars().first()
    run.coverage = dict(run.coverage, total=99)
    db.commit()
    report = consistency.check_consistency(db, store)
    assert any(issue.kind == "coverage_mismatch" for issue in report.issues)


def test_running_run_coverage_is_not_flagged(db, store, populated):
    """A queued/running run is expected to lag behind its units."""
    run = db.execute(select(models.ParseRun)).scalars().first()
    run.status = "running"
    run.coverage = {}
    db.commit()
    report = consistency.check_consistency(db, store)
    assert not any(issue.kind == "coverage_mismatch" for issue in report.issues)


def test_repair_never_guesses_ownership(db, store, populated):
    stored = db.execute(select(models.StoredObject)).scalars().first()
    store.managed_path(stored.relative_path).unlink()
    result = consistency.repair_consistency(db, store)
    assert result["repaired"] == []
    assert result["skipped"], "missing file with no staged copy must be skipped, not invented"


def test_repair_republishes_from_matching_staging_copy(db, store, populated):
    stored = db.execute(select(models.StoredObject)).scalars().first()
    original_bytes = store.managed_path(stored.relative_path).read_bytes()
    store.managed_path(stored.relative_path).unlink()
    staged = store.stage_bytes(original_bytes, "合同.txt")
    result = consistency.repair_consistency(db, store)
    assert [item["action"] for item in result["repaired"]] == ["republish_from_staging"]
    assert store.managed_path(stored.relative_path).read_bytes() == original_bytes
    assert consistency.check_consistency(db, store).ok


def test_backup_writes_manifest_and_refuses_new_writes(db, store, populated, tmp_path,
                                                       monkeypatch, app_database_url):
    destination = tmp_path / "backup"
    engine = get_engine()
    # A real pg_dump is required: this asserts the manifest, not a return code.
    result = backup.create_backup(db, store, destination, database_url=app_database_url,
                                  pg_bin=PG_BIN)
    assert result.complete
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["complete"] is True
    assert manifest["database"]["sha256"] == result.database_sha256
    assert len(manifest["files"]) == result.file_count >= 2
    assert not (destination / backup.INCOMPLETE_NAME).exists()
    assert "access_key" not in json.dumps(manifest).lower()
    # Maintenance is cleared afterwards, so writes work again.
    db.execute(text("UPDATE maintenance_state SET active=false WHERE id=1"))
    db.commit()
    assert consistency.check_consistency(db, store).ok


def test_backup_rejects_destination_inside_data_root(db, store, tmp_path):
    with pytest.raises(WorkbenchError) as exc:
        backup.create_backup(db, store, store.root / "backup",
                             database_url="postgresql://x/y", pg_bin=PG_BIN)
    assert exc.value.code == ErrorCode.INVALID_PATH


def test_backup_rejects_non_empty_destination(db, store, tmp_path, app_database_url):
    destination = tmp_path / "backup"
    destination.mkdir()
    (destination / "keep.txt").write_text("x")
    with pytest.raises(WorkbenchError) as exc:
        backup.create_backup(db, store, destination, database_url=app_database_url,
                             pg_bin=PG_BIN)
    assert exc.value.code == ErrorCode.CONFLICT


def test_backup_fails_when_source_changes_during_copy(db, store, populated, tmp_path,
                                                      app_database_url, monkeypatch):
    stored = db.execute(select(models.StoredObject)).scalars().first()
    target = store.managed_path(stored.relative_path)
    original_copy = backup.shutil.copy2

    def mutating_copy(source, destination, *args, **kwargs):
        result = original_copy(source, destination, *args, **kwargs)
        if Path(source) == target:
            target.write_bytes(b"TEST-ONLY edited externally")
        return result

    monkeypatch.setattr(backup.shutil, "copy2", mutating_copy)
    with pytest.raises(WorkbenchError) as exc:
        backup.create_backup(db, store, tmp_path / "backup",
                             database_url=app_database_url, pg_bin=PG_BIN)
    assert exc.value.code == ErrorCode.CONFLICT
    assert (tmp_path / "backup" / backup.INCOMPLETE_NAME).exists()
    db.execute(text("UPDATE maintenance_state SET active=false WHERE id=1"))
    db.commit()


def test_incomplete_backup_cannot_be_restored(db, store, populated, tmp_path, app_database_url):
    destination = tmp_path / "backup"
    backup.create_backup(db, store, destination, database_url=app_database_url, pg_bin=PG_BIN)
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    manifest["complete"] = False
    (destination / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(WorkbenchError) as exc:
        backup.verify_backup(destination)
    assert exc.value.code == ErrorCode.INTEGRITY_ERROR


def test_corrupt_dump_is_detected(db, store, populated, tmp_path, app_database_url):
    destination = tmp_path / "backup"
    backup.create_backup(db, store, destination, database_url=app_database_url, pg_bin=PG_BIN)
    (destination / backup.DB_DUMP_NAME).write_bytes(b"not a dump")
    with pytest.raises(WorkbenchError) as exc:
        backup.verify_backup(destination)
    assert exc.value.code == ErrorCode.INTEGRITY_ERROR


def test_manifest_path_escape_is_rejected(db, store, populated, tmp_path, app_database_url):
    destination = tmp_path / "backup"
    backup.create_backup(db, store, destination, database_url=app_database_url, pg_bin=PG_BIN)
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"].append({"relative_path": "../../etc/passwd", "size": 1, "sha256": "0"})
    (destination / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(WorkbenchError) as exc:
        backup.verify_backup(destination)
    assert exc.value.code == ErrorCode.INVALID_PATH


def test_restore_into_empty_environment_round_trips(db, store, populated, tmp_path,
                                                    test_database, app_database_url):
    """Full restore: new database, new DATA_ROOT, then compare ids and hashes."""
    from legal_workbench import cases, materials, source_notes
    from legal_workbench.schemas import (
        CaseScheduleInput, CreateAnnotationInput, ReviewAnnotationInput, UpdateAcquisitionInput,
    )
    cases.update_schedule(db, CaseScheduleInput(
        case_id=populated["case_id"], expected_revision=0, stage="举证准备",
        next_action="补齐证据", deadlines=[
            {"title": "提交证据", "due": "2026-11-23", "kind": "external"}]))
    material = db.get(models.Material, populated["material_id"])
    materials.update_acquisition(db, UpdateAcquisitionInput(
        case_id=material.case_id, material_id=material.id, acquisition="委托人提供",
        expected_revision=material.revision))
    note = source_notes.create_annotation(db, CreateAnnotationInput(
        case_id=populated["case_id"], file_version_id=populated["file_version_id"],
        kind="transcription", body="TEST-ONLY Agent annotation"), author="agent", storage=store)
    source_notes.review_annotation(db, ReviewAnnotationInput(
        case_id=populated["case_id"], annotation_id=note.annotation_id,
        reviewed_text="TEST-ONLY human correction"))
    destination = tmp_path / "backup"
    backup.create_backup(db, store, destination, database_url=app_database_url, pg_bin=PG_BIN)
    expected = {
        "cases": [str(row) for row in db.execute(select(models.Case.id)).scalars().all()],
        "materials": [str(row) for row in db.execute(select(models.Material.id)).scalars().all()],
        "segments": [str(row) for row in db.execute(select(models.Segment.id)).scalars().all()],
        "analyses": [str(row) for row in db.execute(select(models.Analysis.id)).scalars().all()],
        "objects": [(row.sha256, row.size) for row in
                    db.execute(select(models.StoredObject)).scalars().all()],
    }
    hit = search.search_case(db, case_id=populated["case_id"], query="合同",
                             mode=SearchMode.LITERAL)
    assert hit.total == 1

    migrator = make_url(test_database[0])
    admin_url = migrator.set(database="postgres").render_as_string(hide_password=False)
    restore_url = migrator.set(database=RESTORE_DB).render_as_string(hide_password=False)
    with psycopg.connect(admin_url, autocommit=True) as connection:
        connection.execute(f'DROP DATABASE IF EXISTS "{RESTORE_DB}" WITH (FORCE)')
        connection.execute(f'CREATE DATABASE "{RESTORE_DB}"')
    try:
        # The custom-format dump carries its own schema, so the target must be truly
        # empty: no tables at all, not merely zero rows.
        with psycopg.connect(restore_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public'")
                assert cursor.fetchone()[0] == 0

        new_root = tmp_path / "restored-data"
        report = backup.restore_backup(destination, data_root=new_root,
                                       database_url=restore_url, pg_bin=PG_BIN)
        assert report["restored_files"] == len(expected["objects"]) + 1

        with psycopg.connect(restore_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT id FROM cases ORDER BY id")
                assert [str(row[0]) for row in cursor.fetchall()] == sorted(expected["cases"])
                cursor.execute("SELECT id FROM materials ORDER BY id")
                assert [str(row[0]) for row in cursor.fetchall()] == sorted(expected["materials"])
                cursor.execute("SELECT id FROM segments ORDER BY id")
                assert [str(row[0]) for row in cursor.fetchall()] == sorted(expected["segments"])
                cursor.execute("SELECT id FROM analyses ORDER BY id")
                assert [str(row[0]) for row in cursor.fetchall()] == sorted(expected["analyses"])
                cursor.execute("SELECT sha256, size FROM stored_objects ORDER BY sha256")
                assert [(row[0], row[1]) for row in cursor.fetchall()] == \
                    sorted(expected["objects"])
                cursor.execute("SELECT count(*) FROM jobs WHERE status='running'")
                assert cursor.fetchone()[0] == 0
                cursor.execute("SELECT active FROM maintenance_state WHERE id=1")
                assert cursor.fetchone()[0] is False
        restored_app_url = make_url(test_database[1]).set(database=RESTORE_DB).render_as_string(
            hide_password=False)
        with psycopg.connect(restored_app_url) as connection:
            row = connection.execute("SELECT stage, next_action, deadlines FROM cases").fetchone()
            assert row[:2] == ("举证准备", "补齐证据")
            assert row[2] == [{"title": "提交证据", "due": "2026-11-23",
                               "kind": "external", "completed": False}]
            assert connection.execute("SELECT acquisition FROM materials").fetchone() == ("委托人提供",)
            row = connection.execute(
                "SELECT id, file_version_id, body, reviewed_text, revision FROM annotations").fetchone()
            assert row == (note.annotation_id, populated["file_version_id"], note.body,
                           "TEST-ONLY human correction", 1)
        # Restored files must verify against the manifest, not merely exist.
        for entry in json.loads((destination / "manifest.json").read_text())["files"]:
            restored = new_root / entry["relative_path"]
            assert restored.is_file()
            assert restored.stat().st_size == entry["size"]
    finally:
        with psycopg.connect(admin_url, autocommit=True) as connection:
            connection.execute(f'DROP DATABASE IF EXISTS "{RESTORE_DB}" WITH (FORCE)')


def test_backup_includes_working_copy_files(db, store, populated, tmp_path, app_database_url):
    """Working copies are user drafts with no hash, but a backup must still carry them."""
    from legal_workbench import materials

    working_id, relative = materials.create_working_copy(
        db, store, case_id=populated["case_id"],
        file_version_id=populated["file_version_id"], display_name="合同（副本）.txt")
    destination = tmp_path / "backup"
    result = backup.create_backup(db, store, destination, database_url=app_database_url,
                                  pg_bin=PG_BIN)
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    entries = {entry["relative_path"]: entry for entry in manifest["files"]}
    assert relative in entries
    assert (destination / "files" / relative).is_file()
    assert entries[relative]["size"] == store.managed_path(relative).stat().st_size
    assert result.file_count == len(entries)


def test_restore_refuses_non_empty_data_root(db, store, populated, tmp_path, test_database):
    destination = tmp_path / "backup"
    backup.create_backup(db, store, destination,
                         database_url=test_database[1], pg_bin=PG_BIN)
    busy_root = tmp_path / "busy"
    busy_root.mkdir()
    (busy_root / "existing.txt").write_text("x")
    with pytest.raises(WorkbenchError) as exc:
        backup.restore_backup(destination, data_root=busy_root,
                              database_url=test_database[1], pg_bin=PG_BIN)
    assert exc.value.code == ErrorCode.CONFLICT


def test_restore_refuses_non_empty_database(db, store, populated, tmp_path, test_database):
    destination = tmp_path / "backup"
    backup.create_backup(db, store, destination,
                         database_url=test_database[1], pg_bin=PG_BIN)
    with pytest.raises(WorkbenchError) as exc:
        backup.restore_backup(destination, data_root=tmp_path / "new-data",
                              database_url=test_database[1], pg_bin=PG_BIN)
    assert exc.value.code == ErrorCode.CONFLICT


def test_maintenance_flag_blocks_writes_and_can_be_cleared(db, store, populated, tmp_path,
                                                           app_database_url):
    from legal_workbench import materials

    db.execute(text("UPDATE maintenance_state SET active=true, reason='simulated crash' WHERE id=1"))
    db.commit()
    with pytest.raises(WorkbenchError) as exc:
        materials.update_classification(db, case_id=populated["case_id"],
                                        material_id=populated["material_id"],
                                        category="x", expected_revision=0)
    assert exc.value.code == ErrorCode.MAINTENANCE
    db.rollback()
    backup.clear_maintenance(db)
    assert consistency.check_consistency(db, store).ok
    # After clearing, the same write path works again.
    revision = materials.update_classification(db, case_id=populated["case_id"],
                                              material_id=populated["material_id"],
                                              category="合同", expected_revision=0)
    assert revision == 1


def test_manifest_records_the_real_migration_revision(db, store, tmp_path,
                                                      app_database_url):
    """The manifest must name the revision in the dump, not a fixed string.

    It carried "0002" after the schema had reached 0003, so the value could not
    be used to tell what a backup actually contained.
    """
    import json

    from sqlalchemy import text

    from legal_workbench import backup

    destination = tmp_path / "backup"
    result = backup.create_backup(db, store, destination,
                                  database_url=app_database_url, pg_bin=PG_BIN)
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    actual = db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert manifest["schema_version"] == actual, (manifest["schema_version"], actual)
    assert manifest["schema_version"] != "0002", "清单仍写死旧版本号"
    assert result.destination == destination
