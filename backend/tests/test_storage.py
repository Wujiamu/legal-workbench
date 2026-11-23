"""T04: managed storage, atomic publish, path safety, crash diagnostics."""
from __future__ import annotations

import os
import threading
import uuid

import pytest

from legal_workbench import storage as storage_module
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.storage import Storage, safe_extension, safe_name


@pytest.fixture
def store(tmp_path) -> Storage:
    return Storage(root=tmp_path / "data")


def test_publish_original_layout_and_hash(store):
    case_id = uuid.uuid4()
    staged = store.stage_bytes(b"TEST-ONLY contract text", "合同.txt")
    result = store.publish_original(case_id, staged)
    assert result.created is True
    assert result.relative_path == f"cases/{case_id}/originals/{result.sha256}/source.txt"
    path = store.managed_path(result.relative_path)
    assert path.read_bytes() == b"TEST-ONLY contract text"
    assert store.verify(result.relative_path, result.sha256, result.size)
    assert list(store.iter_staging()) == []


def test_concurrent_same_content_publish_reuses_without_overwrite(store):
    case_id = uuid.uuid4()
    results = []
    barrier = threading.Barrier(2)

    def publish():
        staged = store.stage_bytes(b"same bytes", "a.txt")
        barrier.wait()
        results.append(store.publish_original(case_id, staged))

    threads = [threading.Thread(target=publish) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(r.created for r in results) == [False, True]
    target = store.managed_path(results[0].relative_path)
    assert target.read_bytes() == b"same bytes"
    assert list(store.iter_staging()) == []


def test_same_target_different_content_is_integrity_error(store):
    case_id = uuid.uuid4()
    version_id = uuid.uuid4()
    first = store.stage_bytes(b"first", "a.txt")
    published = store.publish_version(case_id, version_id, first)
    second = store.stage_bytes(b"second", "a.txt")
    with pytest.raises(WorkbenchError) as exc:
        store.publish_version(case_id, version_id, second)
    assert exc.value.code == ErrorCode.INTEGRITY_ERROR
    assert store.managed_path(published.relative_path).read_bytes() == b"first"


@pytest.mark.parametrize("relative", [
    "../escape.txt", "/etc/passwd", "cases/../../escape.txt", "cases/x/../y.txt", "",
])
def test_path_traversal_rejected(store, relative):
    with pytest.raises(WorkbenchError) as exc:
        store.managed_path(relative)
    assert exc.value.code == ErrorCode.INVALID_PATH


def test_symlink_component_rejected(store, tmp_path):
    case_id = uuid.uuid4()
    store.case_dir(case_id)
    link = store.root / "cases" / str(case_id) / "linkdir"
    link.symlink_to("/tmp")
    with pytest.raises(WorkbenchError) as exc:
        store.managed_path(f"cases/{case_id}/linkdir/file.txt")
    assert exc.value.code == ErrorCode.INVALID_PATH
    final = store.root / "cases" / str(case_id) / "working" / "w1" / "alias.txt"
    final.parent.mkdir(parents=True)
    final.symlink_to("/etc/hosts")
    with pytest.raises(WorkbenchError):
        store.managed_path(f"cases/{case_id}/working/w1/alias.txt")


def test_working_copy_is_independent_inode(store):
    case_id = uuid.uuid4()
    staged = store.stage_bytes(b"original bytes", "原件.txt")
    published = store.publish_original(case_id, staged)
    working_id = uuid.uuid4()
    copied = store.copy_to_working(case_id, working_id, published.relative_path, "编辑稿.txt")
    original_stat = os.stat(store.managed_path(published.relative_path))
    working_stat = os.stat(store.managed_path(copied.relative_path))
    assert original_stat.st_ino != working_stat.st_ino
    assert store.managed_path(copied.relative_path).read_bytes() == b"original bytes"
    assert working_stat.st_nlink == 1


def test_source_change_during_copy_is_rejected(store, tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    source.write_bytes(b"initial")
    original_stage = Storage._stage

    def tampering(self, stream, name, operation_id, max_bytes=None):
        result = original_stage(self, stream, name, operation_id, max_bytes)
        os.utime(source, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns + 1_000_000_000))
        return result

    monkeypatch.setattr(Storage, "_stage", tampering)
    with pytest.raises(WorkbenchError) as exc:
        store.stage_path(source)
    assert exc.value.code == ErrorCode.CONFLICT


def test_size_limit_rejected_without_leftovers(store, tmp_path):
    source = tmp_path / "big.bin"
    source.write_bytes(b"0123456789")
    with pytest.raises(WorkbenchError) as exc:
        store.stage_path(source, max_bytes=5)
    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert list(store.iter_staging()) == []


def test_write_failure_keeps_diagnostic_state(store):
    class Exploding:
        def __init__(self):
            self.calls = 0

        def read(self, size=-1):
            self.calls += 1
            if self.calls == 1:
                return b"partial"
            raise OSError("disk vanished")

    with pytest.raises(OSError):
        store._stage(Exploding(), "broken.txt", uuid.uuid4())
    entries = list(store.iter_staging())
    assert len(entries) == 1
    assert entries[0]["status"] == "failed"
    assert entries[0]["error"] == "OSError"
    part = store.root / entries[0]["relative_path"].replace(".json", ".part")
    assert part.read_bytes() == b"partial"


def test_verify_detects_tampering(store):
    case_id = uuid.uuid4()
    result = store.publish_original(case_id, store.stage_bytes(b"trusted", "a.txt"))
    target = store.managed_path(result.relative_path)
    target.write_bytes(b"tampered")
    assert not store.verify(result.relative_path, result.sha256, result.size)


def test_safe_names_and_extensions():
    assert safe_name("../../etc/passwd") == "passwd"
    assert safe_name("a/b/合同.docx") == "合同.docx"
    assert safe_name("..hidden") == "hidden"
    assert safe_name("bad\x00name.txt") == "badname.txt"
    assert len(safe_name("长" * 300 + ".pdf")) <= 120
    assert safe_extension("合同.PDF") == ".pdf"
    assert safe_extension("noext") == ".bin"
    assert safe_extension("a.tar.gz") == ".gz"
    assert safe_extension("x.verylongextension") == ".bin"
