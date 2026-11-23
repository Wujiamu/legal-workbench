"""One-off rehearsal: build a clean case, back it up, restore into an empty
environment, and print a comparison the operator can eyeball.

TEST-ONLY synthetic data. Deletes only the two scratch databases it creates.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from datetime import date

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

import psycopg  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from legal_workbench import analysis, backup, cases, consistency, imports, parsing, search, time_entries, worker  # noqa: E402
from legal_workbench.config import load_settings, pg_env, resolve_pg_bin  # noqa: E402
from legal_workbench.db import configure_engine, get_engine, get_session_factory  # noqa: E402
from legal_workbench.schemas import (AddTimeEntryInput, ArchiveCaseInput, SearchMode,
                                     StatementKind)  # noqa: E402
from legal_workbench.storage import Storage  # noqa: E402

SCRATCH_DB = "legal_workbench_rehearsal"
RESTORE_DB = "legal_workbench_rehearsal_restored"
WORK = pathlib.Path(tempfile.gettempdir()) / "lwb-rehearsal"
SOURCE = WORK / "source"
DATA = WORK / "data"
BACKUP = WORK / "backup"
RESTORED_DATA = WORK / "restored-data"


def make_url_for(base: str, database: str) -> str:
    return make_url(base).set(database=database).render_as_string(hide_password=False)


def main() -> int:
    settings = load_settings()
    admin_url = make_url_for(settings.migration_url, "postgres")
    scratch_url = make_url_for(settings.migration_url, SCRATCH_DB)
    scratch_app_url = make_url_for(settings.database_url, SCRATCH_DB)
    restore_url = make_url_for(settings.migration_url, RESTORE_DB)

    shutil.rmtree(WORK, ignore_errors=True)
    SOURCE.mkdir(parents=True)
    (SOURCE / "虚构合同.txt").write_bytes(
        "TEST-ONLY 合同约定金额100元。\n\n第二段：交付期限为2024年1月1日。".encode())
    (SOURCE / "虚构附件.txt").write_bytes("TEST-ONLY 附件正文，金额1000元。".encode())

    with psycopg.connect(admin_url, autocommit=True) as conn:
        for name in (SCRATCH_DB, RESTORE_DB):
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            conn.execute(f'CREATE DATABASE "{name}"')

    env = pg_env(extra={"LWB_MIGRATION_URL": scratch_url})
    migrated = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                              cwd=ROOT / "backend", env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    if migrated.returncode != 0:
        print("migration failed:", migrated.stderr[-800:])
        return 1

    os.environ["LWB_DATABASE_URL"] = scratch_app_url
    os.environ["LWB_MIGRATION_URL"] = scratch_url
    os.environ["LWB_DATA_ROOT"] = str(DATA)
    os.environ["LWB_IMPORT_ROOTS"] = str(SOURCE)
    configure_engine(scratch_app_url)
    storage = Storage(root=DATA)
    session = get_session_factory()()

    created = cases.create_case(session, title="TEST-ONLY 恢复演练案件")
    archived_case = cases.create_case(session, title="TEST-ONLY 已归档案件")
    cases.archive_case(session, ArchiveCaseInput(case_id=archived_case.case_id,
                                                 expected_revision=archived_case.revision))
    timed = time_entries.add_manual(session, AddTimeEntryInput(
        case_id=created.case_id, node_title="TEST-ONLY 阅卷", hours=0.25,
        work_date=date.today()))
    intake = imports.create_intake(session, storage, case_id=created.case_id,
                                   source_path=str(SOURCE), source_label="TEST-ONLY",
                                   request_key="rehearsal-1")
    worker.run_worker(get_engine(), storage, once=True)
    materials_list, _total, _case_total, _cats = (
        __import__("legal_workbench.materials", fromlist=["m"]).list_materials(
            session, case_id=created.case_id))
    hits = search.search_case(session, case_id=created.case_id, query="合同",
                              mode=SearchMode.LITERAL)
    amount = search.search_case(session, case_id=created.case_id, query="100",
                                mode=SearchMode.AMOUNT, currency="CNY")
    saved = analysis.save_analysis(session, storage, case_id=created.case_id,
                                   title="虚构分析", content="结论：合同有效。",
                                   statement_kind=StatementKind.AGENT_ANALYSIS,
                                   source_refs=[hits.hits[0].source.source_ref])

    before = {
        "case": str(created.case_id),
        "materials": len(materials_list),
        "literal_hits": hits.total,
        "amount_hits": amount.total,
        "amount_value": str(amount.hits[0].amount) if amount.hits else None,
        "analysis": str(saved.analysis_id),
        "analysis_version": str(saved.version_id),
    }
    consistency_before = consistency.check_consistency(session, storage)
    print("BEFORE:", json.dumps(before, ensure_ascii=False))
    print("CONSISTENCY_BEFORE ok =", consistency_before.ok,
          "issues =", len(consistency_before.issues),
          "orphans =", len(consistency_before.orphan_files))

    pg_bin_path = resolve_pg_bin(settings=settings)
    pg_bin = str(pg_bin_path) if pg_bin_path else None
    result = backup.create_backup(session, storage, BACKUP,
                                  database_url=scratch_app_url,
                                  pg_bin=pg_bin)
    print("BACKUP:", json.dumps({"files": result.file_count, "bytes": result.total_bytes,
                                 "db_sha256": result.database_sha256[:16],
                                 "complete": result.complete}, ensure_ascii=False))
    manifest = backup.verify_backup(BACKUP)
    print("MANIFEST verified: schema", manifest["schema_version"],
          "files", len(manifest["files"]))

    report = backup.restore_backup(BACKUP, data_root=RESTORED_DATA,
                                   database_url=restore_url,
                                   pg_bin=pg_bin)
    print("RESTORE:", json.dumps(report, ensure_ascii=False))

    with psycopg.connect(restore_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, title FROM cases")
            case_rows = cur.fetchall()
            cur.execute("SELECT id, display_name, category FROM materials ORDER BY display_name")
            material_rows = cur.fetchall()
            cur.execute("SELECT id FROM segments ORDER BY id")
            segment_ids = [str(row[0]) for row in cur.fetchall()]
            cur.execute("SELECT id, sha256, size FROM stored_objects ORDER BY sha256")
            object_rows = cur.fetchall()
            cur.execute("SELECT id, review_status FROM analysis_versions")
            analysis_rows = cur.fetchall()
            cur.execute("SELECT count(*) FROM jobs WHERE status='running'")
            running = cur.fetchone()[0]
            cur.execute("SELECT active FROM maintenance_state WHERE id=1")
            maintenance = cur.fetchone()[0]
            cur.execute("SELECT archived_at FROM cases WHERE id=%s",
                        (str(archived_case.case_id),))
            archived_at = cur.fetchone()[0]
            cur.execute("SELECT node_title, hours, source FROM time_entries WHERE id=%s",
                        (str(timed.id),))
            time_row = cur.fetchone()
    print("RESTORED CASES:", case_rows)
    print("RESTORED MATERIALS:", material_rows)
    print("RESTORED SEGMENTS:", len(segment_ids))
    print("RESTORED OBJECTS:", [(row[1][:12], row[2]) for row in object_rows])
    print("RESTORED ANALYSIS:", analysis_rows)
    print("RESTORED ARCHIVE/TIME:", archived_at, time_row)
    print("RUNNING JOBS:", running, "MAINTENANCE:", maintenance)

    checks = {
        "case_id_identical": any(str(row[0]) == before["case"] for row in case_rows),
        "case_count_identical": len(case_rows) == 2,
        "material_count_identical": len(material_rows) == before["materials"],
        "analysis_version_identical": any(str(row[0]) == before["analysis_version"]
                                          for row in analysis_rows),
        "no_running_jobs": running == 0,
        "maintenance_cleared": maintenance is False,
        "archived_case_restored": archived_at is not None,
        "time_entry_restored": (time_row is not None and float(time_row[1]) == 0.25
                                and time_row[2] == "manual"),
    }
    for entry in manifest["files"]:
        path = RESTORED_DATA / entry["relative_path"]
        checks[f"file_{path.name}"] = path.is_file() and path.stat().st_size == entry["size"]
    print("CHECKS:", json.dumps(checks, ensure_ascii=False, indent=1))
    ok = all(checks.values())
    print("RESTORE_REHEARSAL_OK =", ok)

    # Searching the restored database proves the index survived, not just the rows.
    configure_engine(make_url_for(settings.database_url, RESTORE_DB))
    restored_session = get_session_factory()()
    restored_storage = Storage(root=RESTORED_DATA)
    restored_hits = search.search_case(restored_session, case_id=created.case_id,
                                       query="合同", mode=SearchMode.LITERAL)
    restored_amount = search.search_case(restored_session, case_id=created.case_id,
                                         query="100", mode=SearchMode.AMOUNT, currency="CNY")
    restored_consistency = consistency.check_consistency(restored_session, restored_storage)
    print("RESTORED SEARCH literal =", restored_hits.total,
          "amount =", restored_amount.total,
          "amount_value =", str(restored_amount.hits[0].amount) if restored_amount.hits else None)
    print("RESTORED CONSISTENCY ok =", restored_consistency.ok,
          "issues =", [i.kind for i in restored_consistency.issues],
          "orphans =", len(restored_consistency.orphan_files))
    restored_session.close()

    from legal_workbench.db import reset_engine

    reset_engine()
    with psycopg.connect(admin_url, autocommit=True) as conn:
        for name in (SCRATCH_DB, RESTORE_DB):
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
