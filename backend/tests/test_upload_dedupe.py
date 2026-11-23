"""Identical bytes are one managed object and two materials.

`create_upload_intake` inserted a StoredObject per item unconditionally, so two
uploads with the same content hit ``uq_stored_objects_case_sha`` and the whole
batch failed.  Deduplication must not collapse the materials themselves.
"""
from __future__ import annotations

import uuid

import io
import pytest
from sqlalchemy import select

from legal_workbench import models, worker
from legal_workbench.db import get_engine, get_session_factory

from . import factories
from .test_api import new_case, upload

pytestmark = pytest.mark.usefixtures("clean_db")

BODY = b"TEST-ONLY identical payload"


def _drain(store):
    worker.run_worker(get_engine(), store, once=True)


def test_same_batch_identical_bytes_share_one_object(client, api):
    case_id = new_case(client, "TEST-ONLY 同批去重")
    response = upload(client, case_id, [("a.txt", BODY), ("b.txt", BODY)])
    assert response.status_code == 200, response.text

    db = get_session_factory()()
    try:
        objects = db.execute(select(models.StoredObject)).scalars().all()
        items = db.execute(select(models.IntakeItem)).scalars().all()
        assert len(objects) == 1, [o.relative_path for o in objects]
        assert len(items) == 2
        assert {item.relative_source for item in items} == {"a.txt", "b.txt"}
        assert {item.stored_object_id for item in items} == {objects[0].id}
    finally:
        db.close()

    _drain(api.app.state.storage)

    db = get_session_factory()()
    try:
        materials = db.execute(select(models.Material)).scalars().all()
        assert len(materials) == 2
        assert {m.display_name for m in materials} == {"a.txt", "b.txt"}
        # Separate identities, separate paths, shared bytes.
        assert len({m.id for m in materials}) == 2
        assert len({m.intake_item_id for m in materials}) == 2
    finally:
        db.close()


def test_cross_batch_identical_bytes_also_share_one_object(client, api):
    case_id = new_case(client, "TEST-ONLY 跨批去重")
    first = upload(client, case_id, [("one.txt", BODY)], request_key=uuid.uuid4().hex)
    second = upload(client, case_id, [("two.txt", BODY)], request_key=uuid.uuid4().hex)
    assert first.status_code == 200 and second.status_code == 200, (first.text, second.text)

    db = get_session_factory()()
    try:
        assert len(db.execute(select(models.StoredObject)).scalars().all()) == 1
        assert len(db.execute(select(models.IntakeItem)).scalars().all()) == 2
    finally:
        db.close()


def test_identical_bytes_keep_independent_categories(client, api):
    """Sharing bytes must not share classification."""
    from legal_workbench import materials

    case_id = new_case(client, "TEST-ONLY 分类独立")
    upload(client, case_id, [("x.txt", BODY), ("y.txt", BODY)])
    _drain(api.app.state.storage)

    db = get_session_factory()()
    try:
        rows = db.execute(select(models.Material)).scalars().all()
        by_name = {m.display_name: m for m in rows}
        materials.update_classification(db, case_id=uuid.UUID(case_id),
                                        material_id=by_name["x.txt"].id,
                                        category="证据", expected_revision=0)
        db.expire_all()
        rows = db.execute(select(models.Material)).scalars().all()
        by_name = {m.display_name: m for m in rows}
        assert by_name["x.txt"].category == "证据"
        assert by_name["y.txt"].category != "证据"
    finally:
        db.close()


def test_identical_bytes_in_one_directory_name_still_one_object(client, api):
    """Two different relative paths, same name: still two materials."""
    case_id = new_case(client, "TEST-ONLY 同批目录")
    response = upload(client, case_id, [("a/合同.txt", BODY), ("b/合同.txt", BODY)],
                      paths=["a/合同.txt", "b/合同.txt"])
    assert response.status_code == 200, response.text
    db = get_session_factory()()
    try:
        assert len(db.execute(select(models.StoredObject)).scalars().all()) == 1
        items = db.execute(select(models.IntakeItem)).scalars().all()
        assert {i.relative_source for i in items} == {"a/合同.txt", "b/合同.txt"}
    finally:
        db.close()


def test_different_extensions_same_bytes_leave_no_orphan(client, api):
    """a.txt and a.md hold identical bytes: one object, no file on disk for the other.

    Review repro: the batch succeeded and the database held one StoredObject, but
    an unreferenced ``source.md`` was left behind, because the file was published
    by extension before the hash was consulted.
    """
    case_id = new_case(client, "TEST-ONLY 异扩展名")
    response = upload(client, case_id, [("a.txt", BODY), ("a.md", BODY)])
    assert response.status_code == 200, response.text

    db = get_session_factory()()
    try:
        objects = db.execute(select(models.StoredObject)).scalars().all()
        assert len(objects) == 1, [o.relative_path for o in objects]
        referenced = {o.relative_path for o in objects}
        items = db.execute(select(models.IntakeItem)).scalars().all()
        assert {item.relative_source for item in items} == {"a.txt", "a.md"}
        assert {item.stored_object_id for item in items} == {objects[0].id}
    finally:
        db.close()

    on_disk = {api.app.state.storage.relative(path)
               for path in api.app.state.storage.iter_case_files(case_id)}
    assert on_disk <= referenced, f"无引用文件: {sorted(on_disk - referenced)}"


def test_two_parallel_batches_of_identical_bytes_both_succeed(store):
    """Two batches racing on the same content must both complete, one object.

    Review repro: one batch won and the other died on
    ``uq_stored_objects_case_sha``.
    """
    import threading
    import uuid as uuid_module

    from legal_workbench import imports

    setup = get_session_factory()()
    try:
        case_id = factories.create_case(setup)
        setup.commit()  # other connections must see the case
    finally:
        setup.close()
    barrier = threading.Barrier(2, timeout=20)
    outcomes: dict[str, object] = {}

    def batch(tag: str) -> None:
        session = get_session_factory()()
        try:
            stream = io.BytesIO(BODY)
            barrier.wait()
            created = imports.create_upload_intake(
                session, store, case_id=case_id, source_label="TEST-ONLY 并行",
                request_key=uuid_module.uuid4().hex, uploads=[(f"{tag}.txt", stream)])
            outcomes[tag] = created.intake_id
        except BaseException as exc:  # noqa: BLE001 - asserted below
            outcomes[tag] = exc
        finally:
            session.close()

    threads = [threading.Thread(target=batch, args=(tag,)) for tag in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    errors = {tag: value for tag, value in outcomes.items() if isinstance(value, Exception)}
    assert errors == {}, errors
    assert len(outcomes) == 2

    db = get_session_factory()()
    try:
        objects = db.execute(select(models.StoredObject)).scalars().all()
        assert len(objects) == 1, [o.relative_path for o in objects]
        items = db.execute(select(models.IntakeItem)).scalars().all()
        assert len(items) == 2
        referenced = {o.relative_path for o in objects}
    finally:
        db.close()

    on_disk = {store.relative(path) for path in store.iter_case_files(case_id)}
    assert on_disk <= referenced, f"无引用文件: {sorted(on_disk - referenced)}"


def test_parallel_same_extension_keeps_the_shared_original(store):
    """Review repro: the loser deleted the winner's file, so both materials broke.

    Two connections publish identical bytes with the *same* extension, so both
    land on the same path.  The loser hit the unique constraint inside a savepoint
    and unconditionally deleted the path — which was the file the winner had just
    registered.  Both batches "succeeded" and the database held one object, but
    that object failed verification and the worker then failed both materials.
    """
    import threading
    import uuid as uuid_module

    from legal_workbench import imports, worker

    setup = get_session_factory()()
    try:
        case_id = factories.create_case(setup)
        setup.commit()
    finally:
        setup.close()

    barrier = threading.Barrier(2, timeout=20)
    outcomes: dict[str, object] = {}

    def batch(tag: str) -> None:
        session = get_session_factory()()
        try:
            stream = io.BytesIO(BODY)
            barrier.wait()
            outcomes[tag] = imports.create_upload_intake(
                session, store, case_id=case_id, source_label="TEST-ONLY 同扩展名",
                request_key=uuid_module.uuid4().hex, uploads=[(f"{tag}.txt", stream)])
        except BaseException as exc:  # noqa: BLE001
            outcomes[tag] = exc
        finally:
            session.close()

    threads = [threading.Thread(target=batch, args=(tag,)) for tag in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    errors = {tag: value for tag, value in outcomes.items() if isinstance(value, Exception)}
    assert errors == {}, errors

    db = get_session_factory()()
    try:
        objects = db.execute(select(models.StoredObject)).scalars().all()
        assert len(objects) == 1, [o.relative_path for o in objects]
        stored = objects[0]
        # The object must still be verifiable: this is what the loser broke.
        assert store.verify(stored.relative_path, stored.sha256, stored.size), \
            f"共用原件校验失败: {stored.relative_path}"
        assert store.managed_path(stored.relative_path).is_file()
        assert len(db.execute(select(models.IntakeItem)).scalars().all()) == 2
    finally:
        db.close()

    # And both materials must actually parse.
    worker.run_worker(get_engine(), store, once=True)
    db = get_session_factory()()
    try:
        jobs = db.execute(select(models.Job).where(models.Job.kind == "import")).scalars().all()
        assert len(jobs) == 2
        assert all(job.status == "succeeded" for job in jobs), \
            [(j.status, j.error) for j in jobs]
        materials = db.execute(select(models.Material)).scalars().all()
        assert len(materials) == 2, [m.display_name for m in materials]
    finally:
        db.close()


def test_parallel_different_extension_keeps_every_referenced_file(store):
    """Same race, but the two publishes land on different paths.

    Here the loser may legitimately delete its own file; what must hold is that no
    registered object loses its bytes.
    """
    import threading
    import uuid as uuid_module

    from legal_workbench import imports, worker

    setup = get_session_factory()()
    try:
        case_id = factories.create_case(setup)
        setup.commit()
    finally:
        setup.close()

    barrier = threading.Barrier(2, timeout=20)
    outcomes: dict[str, object] = {}
    names = {"a": "a.txt", "b": "b.md"}

    def batch(tag: str) -> None:
        session = get_session_factory()()
        try:
            stream = io.BytesIO(BODY)
            barrier.wait()
            outcomes[tag] = imports.create_upload_intake(
                session, store, case_id=case_id, source_label="TEST-ONLY 异扩展名",
                request_key=uuid_module.uuid4().hex, uploads=[(names[tag], stream)])
        except BaseException as exc:  # noqa: BLE001
            outcomes[tag] = exc
        finally:
            session.close()

    threads = [threading.Thread(target=batch, args=(tag,)) for tag in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    errors = {tag: value for tag, value in outcomes.items() if isinstance(value, Exception)}
    assert errors == {}, errors

    db = get_session_factory()()
    try:
        objects = db.execute(select(models.StoredObject)).scalars().all()
        assert len(objects) == 1, [o.relative_path for o in objects]
        stored = objects[0]
        assert store.verify(stored.relative_path, stored.sha256, stored.size)
    finally:
        db.close()

    worker.run_worker(get_engine(), store, once=True)
    db = get_session_factory()()
    try:
        jobs = db.execute(select(models.Job).where(models.Job.kind == "import")).scalars().all()
        assert all(job.status == "succeeded" for job in jobs), \
            [(j.status, j.error) for j in jobs]
        materials = db.execute(select(models.Material)).scalars().all()
        assert len(materials) == 2, [m.display_name for m in materials]
    finally:
        db.close()


def test_integrity_failure_rolls_back_the_batch_and_the_request_key_still_works(
        client, api, store):
    """Review repro: a failed batch was committed, so its request key was dead.

    Registering one object, then uploading [new file, file whose bytes match the
    corrupted object] hit the integrity branch, which committed the transaction to
    persist the flag.  That also committed the intake and one item with no job, and
    a retry with the same request key answered "既有接收批次缺少任务记录".
    """
    from legal_workbench.db import get_engine

    case_id = new_case(client, "TEST-ONLY 失败批次")
    seeded = upload(client, case_id, [("seed.txt", b"TEST-ONLY seed")])
    assert seeded.status_code == 200
    worker.run_worker(get_engine(), api.app.state.storage, once=True)

    db = get_session_factory()()
    try:
        stored = db.execute(select(models.StoredObject)).scalars().first()
        stored_id, relative = stored.id, stored.relative_path
        before_items = {row.id for row in
                        db.execute(select(models.IntakeItem)).scalars()}
        before_intakes = {row.id for row in db.execute(select(models.Intake)).scalars()}
    finally:
        db.close()

    # Corrupt the registered bytes so reuse fails verification.
    api.app.state.storage.managed_path(relative).write_bytes(b"TEST-ONLY tampered")

    request_key = uuid.uuid4().hex
    response = upload(client, case_id,
                      [("fresh.txt", b"TEST-ONLY fresh"),
                       ("bad.txt", b"TEST-ONLY seed")],
                      request_key=request_key)
    assert response.status_code == 422 or response.status_code >= 400, response.text
    assert response.json()["code"] == "integrity_error", response.text

    db = get_session_factory()()
    try:
        # The failed batch must not be left behind, half-built.
        assert {row.id for row in db.execute(select(models.Intake)).scalars()} \
            == before_intakes, "失败批次留下了接收记录"
        assert {row.id for row in db.execute(select(models.IntakeItem)).scalars()} \
            == before_items, "失败批次留下了条目"
        # The integrity flag on the pre-existing object is still recorded.
        assert db.get(models.StoredObject, stored_id).integrity_status == "integrity_error"
    finally:
        db.close()

    # No orphan file from the failed batch.
    db = get_session_factory()()
    try:
        referenced = {row.relative_path for row in
                      db.execute(select(models.StoredObject)).scalars()}
    finally:
        db.close()
    on_disk = {api.app.state.storage.relative(path)
               for path in api.app.state.storage.iter_case_files(case_id)}
    assert on_disk <= referenced, f"无引用文件: {sorted(on_disk - referenced)}"

    # And the same request key must be usable again (the object is still corrupt,
    # so use fresh bytes to prove the key itself is free).
    retry = upload(client, case_id, [("retry.txt", b"TEST-ONLY retry bytes")],
                   request_key=request_key)
    assert retry.status_code == 200, retry.text
    db = get_session_factory()()
    try:
        intakes = {row.id for row in db.execute(select(models.Intake)).scalars()}
        assert len(intakes) == len(before_intakes) + 1
    finally:
        db.close()


def test_failure_cleanup_never_deletes_a_path_another_batch_registered(
        client, api, monkeypatch):
    """Review repro: A rolls back, B registers the same path, A's cleanup deletes it.

    Identical bytes and extension publish to one managed path.  A failed on a
    corrupted object after publishing a new TXT; its cleanup removed the path by its
    own list, which by then was B's registered original — B's material could never
    be parsed.
    """
    import threading
    import time

    from legal_workbench.db import get_engine

    case_id = new_case(client, "TEST-ONLY 清理竞态")
    seeded = upload(client, case_id, [("seed.txt", b"TEST-ONLY seed")])
    assert seeded.status_code == 200
    worker.run_worker(get_engine(), api.app.state.storage, once=True)

    db = get_session_factory()()
    try:
        stored = db.execute(select(models.StoredObject)).scalars().first()
        relative = stored.relative_path
    finally:
        db.close()
    api.app.state.storage.managed_path(relative).write_bytes(b"TEST-ONLY tampered")

    shared = b"TEST-ONLY shared payload"
    deleting = threading.Event()
    storage = api.app.state.storage
    original_delete = storage.delete_unpublished

    def slow_delete(path: str) -> None:
        # Hold A inside its cleanup long enough for B to publish and commit.
        deleting.set()
        time.sleep(0.4)
        original_delete(path)

    monkeypatch.setattr(storage, "delete_unpublished", slow_delete)

    outcomes: dict[str, object] = {}

    def failing_batch() -> None:
        try:
            outcomes["a"] = upload(client, case_id,
                                   [("new.txt", shared), ("bad.txt", b"TEST-ONLY seed")])
        except BaseException as exc:  # noqa: BLE001
            outcomes["a"] = exc

    def succeeding_batch() -> None:
        deleting.wait(timeout=10)
        try:
            outcomes["b"] = upload(client, case_id, [("same.txt", shared)])
        except BaseException as exc:  # noqa: BLE001
            outcomes["b"] = exc

    threads = [threading.Thread(target=failing_batch),
               threading.Thread(target=succeeding_batch)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=40)

    assert not isinstance(outcomes["a"], Exception), outcomes["a"]
    assert outcomes["a"].status_code == 409, outcomes["a"].text
    assert outcomes["a"].json()["code"] == "integrity_error"
    assert not isinstance(outcomes["b"], Exception), outcomes["b"]
    assert outcomes["b"].status_code == 200, outcomes["b"].text

    # B's registered original must still be intact and parseable.
    db = get_session_factory()()
    try:
        objects = db.execute(select(models.StoredObject)).scalars().all()
        shared_obj = next(o for o in objects if o.sha256 and o.size == len(shared))
        assert storage.verify(shared_obj.relative_path, shared_obj.sha256, shared_obj.size), \
            "乙登记的原件被甲的清理删掉了"
    finally:
        db.close()

    worker.run_worker(get_engine(), storage, once=True)
    db = get_session_factory()()
    try:
        materials = db.execute(select(models.Material)).scalars().all()
        assert any(m.display_name == "same.txt" for m in materials)
    finally:
        db.close()

    # A left nothing behind: no unreferenced file.
    db = get_session_factory()()
    try:
        referenced = {row.relative_path for row in
                      db.execute(select(models.StoredObject)).scalars()}
    finally:
        db.close()
    on_disk = {storage.relative(path) for path in storage.iter_case_files(case_id)}
    assert on_disk <= referenced, f"无引用文件: {sorted(on_disk - referenced)}"


def test_duplicate_relative_path_leaves_no_files(client, api):
    """Review repro: the duplicate check ran after earlier files were published."""
    case_id = new_case(client, "TEST-ONLY 重复路径")
    response = upload(client, case_id, [("a.txt", b"TEST-ONLY one"),
                                        ("b.txt", b"TEST-ONLY two"),
                                        ("c.txt", b"TEST-ONLY three")],
                      paths=["a.txt", "b.txt", "b.txt"])
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_input"

    storage = api.app.state.storage
    assert list(storage.iter_case_files(case_id)) == [], "失败批次留下了文件"

    db = get_session_factory()()
    try:
        assert db.execute(select(models.Intake)).scalars().all() == []
        assert db.execute(select(models.StoredObject)).scalars().all() == []
    finally:
        db.close()


@pytest.mark.parametrize("failure_point", ["readback", "audit", "commit"])
def test_upload_failure_after_publication_cleans_up_and_can_retry(store, monkeypatch, failure_point):
    from legal_workbench import audit, cases, imports, storage

    with get_session_factory()() as db:
        case_id = cases.create_case(db, title="TEST-ONLY failed upload").case_id

    def fail(*_args, **_kwargs):
        raise OSError("TEST-ONLY upload failure")

    with get_session_factory()() as db:
        with monkeypatch.context() as patch:
            if failure_point == "readback":
                patch.setattr(storage, "hash_file", fail)
            elif failure_point == "audit":
                patch.setattr(audit, "record", fail)
            else:
                patch.setattr(db, "commit", fail)
            with pytest.raises(OSError, match="TEST-ONLY upload failure"):
                imports.create_upload_intake(
                    db, store, case_id=case_id, source_label="TEST-ONLY",
                    request_key="retry", uploads=[("new.txt", io.BytesIO(BODY))])

    with get_session_factory()() as db:
        for model in (models.Intake, models.IntakeItem, models.StoredObject, models.Job):
            assert db.scalars(select(model).where(model.case_id == case_id)).first() is None
    assert not list(store.iter_case_files(case_id))
    assert not list(store.iter_staging())

    with get_session_factory()() as db:
        intake = imports.create_upload_intake(
            db, store, case_id=case_id, source_label="TEST-ONLY",
            request_key="retry", uploads=[("new.txt", io.BytesIO(BODY))])
    _drain(store)
    with get_session_factory()() as db:
        assert db.get(models.Job, intake.job_id).status == "succeeded"
        parse_job = db.scalars(select(models.Job).where(
            models.Job.case_id == case_id, models.Job.kind == "parse")).one()
        assert parse_job.status == "succeeded"


def test_upload_rollback_waits_for_backup_lock_and_preserves_integrity_error(store, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import text

    from legal_workbench import backup, imports
    from legal_workbench.errors import ErrorCode, WorkbenchError

    with get_session_factory()() as db:
        built = factories.build_material_with_storage(db, store, content=BODY, name="seed.txt")
        stored = db.get(models.StoredObject, built["stored_object_id"])
        stored_id, relative = stored.id, stored.relative_path
    store.managed_path(relative).write_bytes(b"TEST-ONLY damaged")

    checking = Event()
    maintenance_locked = Event()
    allow_cleanup = Event()
    cleanup_started = Event()
    verify = store.verify
    take_cleanup_lock = imports.begin_exclusive

    def pause_verify(*args):
        checking.set()
        assert maintenance_locked.wait(8)
        return verify(*args)

    def cleanup_lock(db):
        assert allow_cleanup.wait(8)
        cleanup_started.set()
        take_cleanup_lock(db)

    def upload_failed():
        with get_session_factory()() as db, pytest.raises(WorkbenchError) as caught:
            imports.create_upload_intake(
                db, store, case_id=built["case_id"], source_label="TEST-ONLY",
                request_key="fails", uploads=[("new.txt", io.BytesIO(b"TEST-ONLY fresh")),
                                             ("bad.txt", io.BytesIO(BODY))])
        return caught.value.code

    monkeypatch.setattr(store, "verify", pause_verify)
    monkeypatch.setattr(imports, "begin_exclusive", cleanup_lock)
    with ThreadPoolExecutor(1) as pool, get_session_factory()() as maintenance:
        upload = pool.submit(upload_failed)
        assert checking.wait(8)
        try:
            maintenance.execute(text("UPDATE maintenance_state SET active=true WHERE id=1"))
            maintenance.commit()
            # Take the same exclusive lock as backup after the upload releases
            # its shared lock, before cleanup can proceed.
            maintenance_locked.set()
            backup.take_exclusive_lock(maintenance, timeout_ms=8000)
            allow_cleanup.set()
            assert cleanup_started.wait(8)
            assert not upload.done()
        finally:
            maintenance_locked.set()
            maintenance.rollback()
            allow_cleanup.set()
        assert upload.result(timeout=8) == ErrorCode.INTEGRITY_ERROR
        maintenance.execute(text("UPDATE maintenance_state SET active=false WHERE id=1"))
        maintenance.commit()

    with get_session_factory()() as db:
        assert db.get(models.StoredObject, stored_id).integrity_status == "integrity_error"
        assert db.scalars(select(models.Intake).where(models.Intake.request_key == "fails")).first() is None
    assert {store.relative(path) for path in store.iter_case_files(built["case_id"])} == {relative}
    assert not list(store.iter_staging())


def test_browser_and_directory_imports_share_a_concurrent_publication(store, monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace
    from threading import Barrier

    from legal_workbench import cases, imports
    from legal_workbench.config import load_settings

    source = tmp_path / "input"
    source.mkdir()
    file = source / "local.txt"
    file.write_bytes(BODY)
    settings = replace(load_settings(), import_roots=(source,))
    with get_session_factory()() as db:
        case_id = cases.create_case(db, title="TEST-ONLY competing imports").case_id
        local = imports.create_intake(db, store, case_id=case_id, source_path=str(file),
                                      source_label="TEST-ONLY", request_key="local", settings=settings)

    publish = store.publish_original
    barrier = Barrier(2, timeout=8)

    def synchronize_publication(*args):
        result = publish(*args)
        barrier.wait()
        return result

    def browser_upload():
        with get_session_factory()() as db:
            return imports.create_upload_intake(
                db, store, case_id=case_id, source_label="TEST-ONLY", request_key="web",
                uploads=[("web.txt", io.BytesIO(BODY))])

    with monkeypatch.context() as patch:
        patch.setattr(store, "publish_original", synchronize_publication)
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(browser_upload)
            _drain(store)
            browser = pending.result(timeout=8)
    _drain(store)
    with get_session_factory()() as db:
        assert db.get(models.Job, local.job_id).status == "succeeded"
        assert db.get(models.Job, browser.job_id).status == "succeeded"
        assert len(db.scalars(select(models.Material)).all()) == 2
        assert len(db.scalars(select(models.Job).where(
            models.Job.kind == "parse", models.Job.status == "succeeded")).all()) == 2
        obj = db.scalars(select(models.StoredObject)).one()
        assert store.verify(obj.relative_path, obj.sha256, obj.size)
        assert {store.relative(path) for path in store.iter_case_files(case_id)} == {obj.relative_path}


def test_interrupted_parse_enqueue_retries_material_association(client, api, monkeypatch):
    from legal_workbench.parsing import pipeline

    case_id = new_case(client, "TEST-ONLY parse enqueue recovery")
    assert upload(client, case_id, [("original.txt", BODY)]).status_code == 200
    with monkeypatch.context() as patch:
        def interrupt(*_args, **_kwargs):
            raise RuntimeError("TEST-ONLY interrupted parse enqueue")

        patch.setattr(pipeline, "create_parse_run", interrupt)
        with pytest.raises(RuntimeError, match="interrupted parse enqueue"):
            _drain(api.app.state.storage)
    with get_session_factory()() as db:
        assert db.scalars(select(models.Material)).first() is None
        assert db.scalars(select(models.IntakeItem)).one().status == "published"
    _drain(api.app.state.storage)
    with get_session_factory()() as db:
        assert db.scalars(select(models.Material)).one().display_name == "original.txt"
        assert db.scalars(select(models.Job).where(models.Job.kind == "parse")).one().status == "succeeded"


def test_parallel_retries_with_the_same_key_return_the_same_batch(store, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from legal_workbench import cases, imports

    with get_session_factory()() as db:
        case_id = cases.create_case(db, title="TEST-ONLY concurrent retry").case_id

    barrier = Barrier(2, timeout=8)
    claim = imports._claim_intake

    def simultaneous_claim(*args):
        barrier.wait()
        return claim(*args)

    def upload_once():
        with get_session_factory()() as db:
            return imports.create_upload_intake(
                db, store, case_id=case_id, source_label="TEST-ONLY", request_key="same-key",
                uploads=[("same.txt", io.BytesIO(BODY))])

    monkeypatch.setattr(imports, "_claim_intake", simultaneous_claim)
    with ThreadPoolExecutor(2) as pool:
        first, second = pool.map(lambda _: upload_once(), range(2))
    assert first.intake_id == second.intake_id
    assert first.job_id == second.job_id
    assert first.created != second.created
    _drain(store)
    with get_session_factory()() as db:
        assert db.scalars(select(models.Material)).one().display_name == "same.txt"
