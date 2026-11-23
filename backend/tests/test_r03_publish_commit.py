"""R03 (round 2): terminal status and index publication must be one commit.

The first fix moved `_publish_active` into `_finish` but left a commit inside
`_refresh_coverage`, so the crash window simply moved a few lines up.  These
tests inject the fault at the real commit boundary and recover with a normal
worker, not by calling the parse function again.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from legal_workbench import models, search, worker
from legal_workbench.db import get_session_factory
from legal_workbench.parsing import pipeline
from legal_workbench.schemas import SearchMode, SearchScope

from . import factories

pytestmark = pytest.mark.usefixtures("clean_db")


def _build(db, store, text: str, name: str = "边界.docx") -> dict:
    from .test_r02_working_copy_extension import docx_bytes  # shared helper
    return factories.build_material_with_storage(
        db, store, content=docx_bytes(text), name=name)


def _version(db, file_version_id):
    db.expire_all()
    return db.get(models.FileVersion, file_version_id)


@pytest.mark.parametrize("boundary", ["units_done", "before_finish", "before_publish"])
def test_crash_before_commit_leaves_nothing_durable_and_worker_recovers(
        db, store, app_engine, boundary):
    """Every pre-commit boundary must roll back completely.

    The failure mode under test: a durable ``complete`` run plus a ``succeeded``
    job with ``active_parse_run_id`` still null.  No worker would ever claim that
    job again, so the index stayed unpublished forever.
    """
    built = _build(db, store, "TEST-ONLY 边界正文 关键词庚庚庚")
    _run_id, job_id = pipeline.create_parse_run(
        db, case_id=built["case_id"], file_version_id=built["file_version_id"])

    def kill_at(stage):
        if stage == boundary:
            raise RuntimeError(f"simulated crash at {boundary}")

    with pytest.raises(RuntimeError):
        pipeline.process_parse_job(get_session_factory(), store, job_id,
                                   fault_inject=kill_at)

    # Nothing about the terminal state may be visible from a new connection.
    db.expire_all()
    run = db.execute(select(models.ParseRun)).scalars().one()
    job = db.get(models.Job, job_id)
    assert run.status == "running", (boundary, run.status)
    assert job.status == "running", (boundary, job.status)
    assert _version(db, built["file_version_id"]).active_parse_run_id is None

    # A normal worker — not a direct pipeline call — must finish the job.
    processed = worker.run_worker(app_engine, store, once=True)
    assert processed == 1

    db.expire_all()
    assert _version(db, built["file_version_id"]).active_parse_run_id == run.id
    assert db.get(models.Job, job_id).status == "succeeded"
    hits = search.search_case(db, case_id=built["case_id"], query="庚庚庚",
                              mode=SearchMode.LITERAL, scope=SearchScope.ORIGINALS)
    assert hits.total == 1, hits.total


def test_worker_publishes_legacy_succeeded_but_unpublished_run(db, store, app_engine):
    """Runs written by the old code must be repaired, not just new ones.

    Simulates the exact old crash state: run complete, job succeeded, active
    null.  Claiming jobs cannot reach it, so the repair must run on its own.
    """
    built = _build(db, store, "TEST-ONLY 旧现场 关键词辛辛辛")
    run_id, job_id = pipeline.create_parse_run(
        db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    pipeline.process_parse_job(get_session_factory(), store, job_id)

    # Rewind to the crash scene: unpublish without touching run or job status.
    version = _version(db, built["file_version_id"])
    assert version.active_parse_run_id == run_id
    version.active_parse_run_id = None
    db.commit()

    processed = worker.run_worker(app_engine, store, once=True)
    assert processed == 0, "不该靠领取任务修复，任务早已 succeeded"

    db.expire_all()
    assert _version(db, built["file_version_id"]).active_parse_run_id == run_id
    hits = search.search_case(db, case_id=built["case_id"], query="辛辛辛",
                              mode=SearchMode.LITERAL, scope=SearchScope.ORIGINALS)
    assert hits.total == 1, hits.total


def test_repair_does_not_override_a_newer_active_run(db, store):
    """Repair must respect ordering: an older terminal run cannot win."""
    built = _build(db, store, "TEST-ONLY 第一版 壬壬壬")
    first_run, first_job = pipeline.create_parse_run(
        db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    pipeline.process_parse_job(get_session_factory(), store, first_job)

    second_run, second_job = pipeline.create_parse_run(
        db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    pipeline.process_parse_job(get_session_factory(), store, second_job)

    db.expire_all()
    assert _version(db, built["file_version_id"]).active_parse_run_id == second_run

    repaired = pipeline.repair_unpublished_runs(db)
    assert repaired == 0
    db.expire_all()
    assert _version(db, built["file_version_id"]).active_parse_run_id == second_run
    assert second_run != first_run


def test_repair_leaves_partial_unpublished_when_a_complete_exists(db, store):
    """A partial run may become active only when nothing better is published."""
    built = _build(db, store, "TEST-ONLY 部分 癸癸癸")
    run_id, job_id = pipeline.create_parse_run(
        db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    pipeline.process_parse_job(get_session_factory(), store, job_id)

    version = _version(db, built["file_version_id"])
    version.active_parse_run_id = None
    db.commit()
    repaired = pipeline.repair_unpublished_runs(db)
    assert repaired == 1
    db.expire_all()
    assert _version(db, built["file_version_id"]).active_parse_run_id == run_id


def test_publish_still_keeps_history_reference_valid(db, store):
    """Historical source_ref must keep resolving to its own text after a reparse."""
    built = _build(db, store, "TEST-ONLY 历史 甲历史")
    run_id, job_id = pipeline.create_parse_run(
        db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    pipeline.process_parse_job(get_session_factory(), store, job_id)

    db.expire_all()
    segment = db.execute(select(models.Segment)).scalars().first()
    old_ref = segment.id
    old_text = segment.text

    second_run, second_job = pipeline.create_parse_run(
        db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    pipeline.process_parse_job(get_session_factory(), store, second_job)

    db.expire_all()
    assert _version(db, built["file_version_id"]).active_parse_run_id == second_run
    resolved = search.read_source(db, case_id=built["case_id"], source_ref=old_ref)
    assert resolved.text == old_text


def test_repair_publishes_newer_run_when_pointer_still_holds_the_old_one(db, store):
    """The reparse crash scene the review reproduced.

    Two complete runs exist for one file version; the pointer still names the
    older one because the crash happened after the newer run's terminal status.
    The old repair query excluded any version that already had *an* active run,
    so it found nothing, and the job was already succeeded so no worker claimed
    it either.  The version stayed on the stale index forever.
    """
    built = _build(db, store, "TEST-ONLY 重解析现场 子子子")
    first_run, first_job = pipeline.create_parse_run(
        db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    pipeline.process_parse_job(get_session_factory(), store, first_job)

    second_run, second_job = pipeline.create_parse_run(
        db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    pipeline.process_parse_job(get_session_factory(), store, second_job)

    db.expire_all()
    version = db.get(models.FileVersion, built["file_version_id"])
    assert version.active_parse_run_id == second_run

    # Rewind to the crash scene: the new run is terminal, its job succeeded, but
    # the pointer was never switched.
    version.active_parse_run_id = first_run
    db.commit()

    repaired = pipeline.repair_unpublished_runs(db)
    assert repaired == 1, repaired

    db.expire_all()
    assert db.get(models.FileVersion, built["file_version_id"]).active_parse_run_id \
        == second_run
    # And the newest content is the one now searchable.
    hits = search.search_case(db, case_id=built["case_id"], query="子子子",
                              mode=SearchMode.LITERAL, scope=SearchScope.ORIGINALS)
    assert hits.total == 1


def test_worker_start_repairs_stale_pointer(db, store, app_engine):
    """The same repair must happen on worker start, not only on an explicit call."""
    built = _build(db, store, "TEST-ONLY 启动补发 丑丑丑")
    first_run, first_job = pipeline.create_parse_run(
        db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    pipeline.process_parse_job(get_session_factory(), store, first_job)
    second_run, second_job = pipeline.create_parse_run(
        db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    pipeline.process_parse_job(get_session_factory(), store, second_job)

    db.expire_all()
    version = db.get(models.FileVersion, built["file_version_id"])
    version.active_parse_run_id = first_run
    db.commit()

    worker.run_worker(app_engine, store, once=True)

    db.expire_all()
    assert db.get(models.FileVersion, built["file_version_id"]).active_parse_run_id \
        == second_run


def test_repair_keeps_a_newer_active_run_and_history(db, store):
    """Protection from the review still holds: no downgrade, old refs stay valid."""
    built = _build(db, store, "TEST-ONLY 保护 寅寅寅")
    first_run, first_job = pipeline.create_parse_run(
        db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    pipeline.process_parse_job(get_session_factory(), store, first_job)

    db.expire_all()
    old_segment = db.execute(
        select(models.Segment).where(models.Segment.parse_run_id == first_run)
    ).scalars().first()
    old_ref, old_text = old_segment.id, old_segment.text

    second_run, second_job = pipeline.create_parse_run(
        db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    pipeline.process_parse_job(get_session_factory(), store, second_job)

    repaired = pipeline.repair_unpublished_runs(db)
    assert repaired == 0
    db.expire_all()
    assert db.get(models.FileVersion, built["file_version_id"]).active_parse_run_id \
        == second_run
    resolved = search.read_source(db, case_id=built["case_id"], source_ref=old_ref)
    assert resolved.text == old_text


def test_repair_skips_a_newer_partial_and_takes_the_complete_behind_it(db, store):
    """Review repro: a newer partial hid the complete run that should be published.

    Runs 0 (complete, currently active) and 1 (complete, never published) exist,
    plus run 2 (partial).  The repair took only the newest terminal run, found the
    partial, was refused (correctly — a partial must not displace a complete) and
    then stopped, leaving the version on run 0.
    """
    built = _build(db, store, "TEST-ONLY 混合终态 巳巳巳")
    run0, job0 = pipeline.create_parse_run(
        db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    pipeline.process_parse_job(get_session_factory(), store, job0)

    run1, job1 = pipeline.create_parse_run(
        db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    pipeline.process_parse_job(get_session_factory(), store, job1)

    run2, job2 = pipeline.create_parse_run(
        db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    pipeline.process_parse_job(get_session_factory(), store, job2)

    # Force the scene: run 2 is partial, run 1 was never published, pointer on run 0.
    db.expire_all()
    db.get(models.ParseRun, run2).status = "partial"
    version = db.get(models.FileVersion, built["file_version_id"])
    version.active_parse_run_id = run0
    db.commit()

    repaired = pipeline.repair_unpublished_runs(db)
    assert repaired == 1, repaired

    db.expire_all()
    assert db.get(models.FileVersion, built["file_version_id"]).active_parse_run_id == run1
    hits = search.search_case(db, case_id=built["case_id"], query="巳巳巳",
                              mode=SearchMode.LITERAL, scope=SearchScope.ORIGINALS)
    assert hits.total == 1


def test_repair_from_no_active_pointer_with_a_newer_partial(db, store):
    """Same mix, but the version has no active run at all."""
    built = _build(db, store, "TEST-ONLY 无指针混合 午午午")
    run0, job0 = pipeline.create_parse_run(
        db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    pipeline.process_parse_job(get_session_factory(), store, job0)
    run1, job1 = pipeline.create_parse_run(
        db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    pipeline.process_parse_job(get_session_factory(), store, job1)
    run2, job2 = pipeline.create_parse_run(
        db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    pipeline.process_parse_job(get_session_factory(), store, job2)

    db.expire_all()
    db.get(models.ParseRun, run2).status = "partial"
    db.get(models.FileVersion, built["file_version_id"]).active_parse_run_id = None
    db.commit()

    repaired = pipeline.repair_unpublished_runs(db)
    assert repaired == 1, repaired
    db.expire_all()
    assert db.get(models.FileVersion, built["file_version_id"]).active_parse_run_id == run1
    assert run1 != run0
