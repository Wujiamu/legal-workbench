"""Concurrent working-copy registration: exactly one attempt may win.

The pre-check ``working.revision != expected_revision`` is only a read.  Two
connections that both loaded revision=0 pass it, so the CAS update inside
``register_file_version`` is what actually decides — and its return value used to
be ignored, letting both commits through.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from legal_workbench import materials, models
from legal_workbench.db import get_session_factory
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.parsing import pipeline
from legal_workbench.storage import hash_file

from . import factories
from .test_r02_working_copy_extension import docx_bytes

pytestmark = pytest.mark.usefixtures("clean_db")


def _prepare(store, text: str = "TEST-ONLY 并发登记"):
    db = get_session_factory()()
    try:
        built = factories.build_material_with_storage(
            db, store, content=docx_bytes(text), name="并发.docx")
        working_id, relative = materials.create_working_copy(
            db, store, case_id=built["case_id"], file_version_id=built["file_version_id"])
    finally:
        db.close()
    store.managed_path(relative).write_bytes(docx_bytes("TEST-ONLY 修改后 并发并发"))
    return built, working_id


def test_two_connections_cannot_register_the_same_revision(store):
    """Both connections read revision=0; only the first commit may land."""
    built, working_id = _prepare(store)

    first = get_session_factory()()
    second = get_session_factory()()
    try:
        # Both load the row before either writes: the stale read is the premise.
        first_working = first.get(models.WorkingCopy, working_id)
        second_working = second.get(models.WorkingCopy, working_id)
        assert first_working.revision == 0 and second_working.revision == 0

        winner_version, _winner_job = materials.register_file_version(
            first, store, case_id=built["case_id"], working_copy_id=working_id,
            expected_revision=0)

        with pytest.raises(WorkbenchError) as caught:
            materials.register_file_version(
                second, store, case_id=built["case_id"], working_copy_id=working_id,
                expected_revision=0)
        assert caught.value.code == ErrorCode.CONFLICT
        assert caught.value.details["working_copy_id"] == str(working_id)
    finally:
        first.close()
        second.close()

    db = get_session_factory()()
    try:
        versions = db.execute(
            select(models.FileVersion).where(models.FileVersion.kind == "working")
        ).scalars().all()
        assert len(versions) == 1, [v.id for v in versions]
        assert versions[0].id == winner_version
        assert db.get(models.WorkingCopy, working_id).revision == 1
        # The loser must not have queued a parse job either.
        jobs = db.execute(select(models.Job).where(models.Job.kind == "parse")).scalars().all()
        assert len(jobs) == 1, [j.id for j in jobs]
    finally:
        db.close()


def test_losing_registration_leaves_no_orphan_file(store):
    """A rolled-back registration must not leave a published file behind."""
    built, working_id = _prepare(store, "TEST-ONLY 无孤儿")

    first = get_session_factory()()
    second = get_session_factory()()
    try:
        second.get(models.WorkingCopy, working_id)
        materials.register_file_version(first, store, case_id=built["case_id"],
                                        working_copy_id=working_id, expected_revision=0)
        with pytest.raises(WorkbenchError):
            materials.register_file_version(second, store, case_id=built["case_id"],
                                            working_copy_id=working_id,
                                            expected_revision=0)
    finally:
        first.close()
        second.close()

    db = get_session_factory()()
    try:
        referenced = {
            row.relative_path for row in db.execute(select(models.StoredObject)).scalars()
        }
    finally:
        db.close()
    on_disk = {
        store.relative(path) for path in store.iter_case_files(built["case_id"])
        if "working" not in store.relative(path)
    }
    assert on_disk <= referenced, f"无数据库引用的文件: {sorted(on_disk - referenced)}"


def test_register_rejects_stale_revision_without_touching_storage(store):
    """A plainly stale revision must fail before anything is published."""
    built, working_id = _prepare(store, "TEST-ONLY 陈旧修订")
    db = get_session_factory()()
    try:
        materials.register_file_version(db, store, case_id=built["case_id"],
                                        working_copy_id=working_id, expected_revision=0)
        before = len(list(store.iter_case_files(built["case_id"])))
        with pytest.raises(WorkbenchError) as caught:
            materials.register_file_version(db, store, case_id=built["case_id"],
                                            working_copy_id=working_id,
                                            expected_revision=0)
        assert caught.value.code == ErrorCode.CONFLICT
        after = len(list(store.iter_case_files(built["case_id"])))
        assert after == before
    finally:
        db.close()


def test_two_truly_parallel_registrations(store):
    """Both connections run at the same time, not one after the other.

    The earlier test called the second registration after the first had already
    committed, so the loser could simply reuse the winner's StoredObject and the
    uq_stored_objects_case_sha path was never exercised.  Here both threads pass
    the pre-check before either commits.
    """
    import threading

    built, working_id = _prepare(store, "TEST-ONLY 真并行")
    barrier = threading.Barrier(2, timeout=20)
    results: dict[str, object] = {}

    def attempt(tag: str) -> None:
        session = get_session_factory()()
        try:
            # Load the row, then wait for the other thread: both see revision=0.
            session.get(models.WorkingCopy, working_id)
            barrier.wait()
            results[tag] = materials.register_file_version(
                session, store, case_id=built["case_id"],
                working_copy_id=working_id, expected_revision=0)
        except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
            results[tag] = exc
        finally:
            session.close()

    threads = [threading.Thread(target=attempt, args=(tag,)) for tag in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert set(results) == {"a", "b"}, results
    winners = [tag for tag, value in results.items() if isinstance(value, tuple)]
    losers = [tag for tag, value in results.items() if isinstance(value, WorkbenchError)]
    assert len(winners) == 1, results
    assert len(losers) == 1, results
    loser_error = results[losers[0]]
    assert loser_error.code == ErrorCode.CONFLICT, loser_error
    # The loser must not have died on a unique constraint instead.
    assert "uq_stored_objects_case_sha" not in str(loser_error), loser_error

    db = get_session_factory()()
    try:
        versions = db.execute(
            select(models.FileVersion).where(models.FileVersion.kind == "working")
        ).scalars().all()
        assert len(versions) == 1, [v.id for v in versions]
        jobs = db.execute(select(models.Job).where(models.Job.kind == "parse")).scalars().all()
        assert len(jobs) == 1, [j.id for j in jobs]
        assert db.get(models.WorkingCopy, working_id).revision == 1
        referenced = {row.relative_path for row in
                      db.execute(select(models.StoredObject)).scalars()}
    finally:
        db.close()

    orphans = [str(path) for path in store.iter_case_files(built["case_id"])
               if "working" not in store.relative(path)
               and store.relative(path) not in referenced]
    assert orphans == [], orphans


def test_failure_after_parse_run_leaves_no_partial_state(store, monkeypatch):
    """Review repro: a failure after create_parse_run committed left a half state.

    ``create_parse_run`` committed on its own, so injecting a failure in the audit
    write afterwards produced: version rolled back, but revision=1, one working
    version and one queued job still visible — plus a deleted file.
    """
    from legal_workbench import audit as audit_module

    built, working_id = _prepare(store, "TEST-ONLY 登记失败")

    def explode(*args, **kwargs):
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr(audit_module, "record", explode)

    db = get_session_factory()()
    try:
        with pytest.raises(RuntimeError):
            materials.register_file_version(db, store, case_id=built["case_id"],
                                            working_copy_id=working_id,
                                            expected_revision=0)
    finally:
        db.close()

    db = get_session_factory()()
    try:
        # Nothing from the failed attempt may be visible from a new connection.
        assert db.get(models.WorkingCopy, working_id).revision == 0, "失败登记消耗了修订号"
        assert db.execute(select(models.FileVersion)
                          .where(models.FileVersion.kind == "working")).scalars().all() == []
        assert db.execute(select(models.Job)
                          .where(models.Job.kind == "parse")).scalars().all() == []
        referenced = {row.relative_path for row in
                      db.execute(select(models.StoredObject)).scalars()}
    finally:
        db.close()

    orphans = [str(path) for path in store.iter_case_files(built["case_id"])
               if "working" not in store.relative(path)
               and store.relative(path) not in referenced]
    assert orphans == [], orphans

    # And the same registration must still succeed afterwards, using revision 0.
    monkeypatch.undo()
    db = get_session_factory()()
    try:
        version_id, job_id = materials.register_file_version(
            db, store, case_id=built["case_id"], working_copy_id=working_id,
            expected_revision=0)
        assert version_id and job_id
    finally:
        db.close()


def test_integrity_failure_does_not_consume_a_revision(store):
    """A failed registration must not consume a revision.

    The reuse branch raises ``INTEGRITY_ERROR`` when the stored bytes no longer
    match the recorded hash.  Getting there needs a row whose recorded sha equals
    the bytes being registered while the file on disk does not — the ordinary
    "someone edited the original" case.
    """
    built, working_id = _prepare(store, "TEST-ONLY 完整性失败")

    db = get_session_factory()()
    try:
        working = db.get(models.WorkingCopy, working_id)
        staged_hash, staged_size = hash_file(store.managed_path(working.relative_path))
        # Register a row for exactly these bytes, but point it at a file whose
        # content differs, so verify() must fail.
        other = store.stage_bytes(b"TEST-ONLY different bytes", "other.txt")
        published = store.publish_original(built["case_id"], other)
        stored = models.StoredObject(
            id=uuid.uuid4(), case_id=built["case_id"], sha256=staged_hash,
            size=staged_size, relative_path=published.relative_path,
        )
        db.add(stored)
        db.commit()
        revision = db.get(models.WorkingCopy, working_id).revision
    finally:
        db.close()

    db = get_session_factory()()
    try:
        with pytest.raises(WorkbenchError) as caught:
            materials.register_file_version(db, store, case_id=built["case_id"],
                                            working_copy_id=working_id,
                                            expected_revision=revision)
        assert caught.value.code == ErrorCode.INTEGRITY_ERROR
        db.expire_all()
        assert db.get(models.WorkingCopy, working_id).revision == revision, \
            "完整性失败消耗了修订号"
        versions = db.execute(select(models.FileVersion)
                              .where(models.FileVersion.kind == "working")).scalars().all()
        assert versions == []
    finally:
        db.close()


def test_two_copies_of_one_material_registering_identical_bytes(store):
    """Review repro: two working copies, same edited content, one insert loses.

    The CAS locks different WorkingCopy rows, so both get through it; the loser then
    hit uq_stored_objects_case_sha outside any savepoint and left an unreferenced
    version file.
    """
    import threading

    built, first_copy = _prepare(store, "TEST-ONLY 双副本")
    db = get_session_factory()()
    try:
        second_copy, second_relative = materials.create_working_copy(
            db, store, case_id=built["case_id"], file_version_id=built["file_version_id"])
        first_relative = db.get(models.WorkingCopy, first_copy).relative_path
    finally:
        db.close()

    # Edit both copies to identical bytes.
    same = docx_bytes("TEST-ONLY 相同内容 双双双")
    store.managed_path(first_relative).write_bytes(same)
    store.managed_path(second_relative).write_bytes(same)

    barrier = threading.Barrier(2, timeout=20)
    outcomes: dict[str, object] = {}

    def attempt(tag: str, copy_id) -> None:
        session = get_session_factory()()
        try:
            session.get(models.WorkingCopy, copy_id)
            barrier.wait()
            outcomes[tag] = materials.register_file_version(
                session, store, case_id=built["case_id"], working_copy_id=copy_id,
                expected_revision=0)
        except BaseException as exc:  # noqa: BLE001
            outcomes[tag] = exc
        finally:
            session.close()

    threads = [threading.Thread(target=attempt, args=("first", first_copy)),
               threading.Thread(target=attempt, args=("second", second_copy))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    errors = {tag: value for tag, value in outcomes.items() if isinstance(value, Exception)}
    assert errors == {}, errors

    db = get_session_factory()()
    try:
        versions = db.execute(select(models.FileVersion)
                              .where(models.FileVersion.kind == "working")).scalars().all()
        assert len(versions) == 2, [v.id for v in versions]
        assert len({v.stored_object_id for v in versions}) == 1, "同内容未复用对象"
        jobs = db.execute(select(models.Job).where(models.Job.kind == "parse")).scalars().all()
        assert len(jobs) == 2, [j.id for j in jobs]
        referenced = {row.relative_path for row in
                      db.execute(select(models.StoredObject)).scalars()}
        for version in versions:
            stored = db.get(models.StoredObject, version.stored_object_id)
            assert store.verify(stored.relative_path, stored.sha256, stored.size)
    finally:
        db.close()

    orphans = [str(path) for path in store.iter_case_files(built["case_id"])
               if "working" not in store.relative(path)
               and store.relative(path) not in referenced]
    assert orphans == [], orphans
