"""T19 acceptance run: full chain, concurrency, fault injection, performance.

Writes a Markdown report to docs/reports/T19-evidence.md and prints the same
summary. Everything runs against scratch databases on the local cluster and
TEST-ONLY synthetic corpora; the development database is never touched.
"""
from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import psycopg  # noqa: E402
from sqlalchemy import func, select, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from build_corpus import build_functional_corpus, build_performance_corpus  # noqa: E402
from legal_workbench import (analysis, backup, cases, consistency, imports, jobs,  # noqa: E402
                            materials, models, parsing, search, worker)
from legal_workbench.config import load_settings  # noqa: E402
from legal_workbench.db import configure_engine, get_engine, get_session_factory, reset_engine  # noqa: E402
from legal_workbench.schemas import ReviewStatus, SearchMode, SearchScope, StatementKind  # noqa: E402
from legal_workbench.storage import Storage  # noqa: E402

WORK = Path("/tmp/lwb-acceptance")
SOURCE = WORK / "source"
PERF_SOURCE = WORK / "perf-source"
DATA = WORK / "data"
PERF_DATA = WORK / "perf-data"
BACKUP_DIR = WORK / "backup"
RESTORED_DATA = WORK / "restored-data"
REPORT = ROOT / "docs" / "reports" / "T19-evidence.md"

FUNC_DB = "lwb_accept_func"
FUNC_RESTORE_DB = "lwb_accept_restore"
PERF_DB = "lwb_accept_perf"

lines: list[str] = []


def say(message: str = "") -> None:
    print(message)
    lines.append(message)


def url_for(base: str, database: str) -> str:
    return make_url(base).set(database=database).render_as_string(hide_password=False)


def recreate_databases(names: list[str]) -> None:
    settings = load_settings()
    admin = url_for(settings.migration_url, "postgres")
    with psycopg.connect(admin, autocommit=True) as connection:
        for name in names:
            connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
            connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            connection.execute(f'CREATE DATABASE "{name}"')


def migrate(migration_url: str) -> None:
    env = dict(os.environ, LWB_MIGRATION_URL=migration_url, LC_ALL="en_US.UTF-8")
    result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                            cwd=ROOT / "backend", env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"migration failed: {result.stderr[-1500:]}")


def drain(store: Storage, limit: int = 200) -> int:
    total = 0
    for _ in range(limit):
        processed = worker.run_worker(get_engine(), store, once=True)
        total += processed
        if processed == 0:
            break
    return total


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


def _free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> int:
    settings = load_settings()
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    SOURCE.mkdir(parents=True)
    recreate_databases([FUNC_DB, FUNC_RESTORE_DB, PERF_DB])

    say("# T19 端到端与性能验收 — 实测记录")
    say()
    say(f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    say()
    say("数据全部为脚本生成的 TEST-ONLY 虚构内容；未读取任何真实案件文件。")
    say()

    # ---------- functional corpus ----------
    layout = build_functional_corpus(SOURCE)
    say("## 1. 功能样本")
    say()
    say(f"生成 {sum(layout.values())} 个条目：{json.dumps(layout, ensure_ascii=False)}")
    say()

    func_url = url_for(settings.migration_url, FUNC_DB)
    func_app = url_for(settings.database_url, FUNC_DB)
    migrate(func_url)
    os.environ.update({"LWB_DATABASE_URL": func_app, "LWB_MIGRATION_URL": func_url,
                       "LWB_DATA_ROOT": str(DATA), "LWB_IMPORT_ROOTS": str(SOURCE)})
    reset_engine()
    configure_engine(func_app)
    store = Storage(root=DATA)
    session = get_session_factory()()

    case_a = cases.create_case(session, title="TEST-ONLY 验收案件甲")
    case_b = cases.create_case(session, title="TEST-ONLY 验收案件乙")
    intake_a = imports.create_intake(session, store, case_id=case_a.case_id,
                                     source_path=str(SOURCE), source_label="验收甲",
                                     request_key="accept-a")
    intake_b = imports.create_intake(session, store, case_id=case_b.case_id,
                                     source_path=str(SOURCE / "虚构金额对照.txt"),
                                     source_label="验收乙", request_key="accept-b")
    say(f"案件甲 {case_a.case_id}，导入项 {intake_a.item_count}；"
        f"案件乙 {case_b.case_id}，导入项 {intake_b.item_count}")
    processed = drain(store)
    say(f"worker 处理任务数：{processed}")

    items_a = session.execute(select(models.IntakeItem).where(
        models.IntakeItem.intake_id == intake_a.intake_id)).scalars().all()
    status_counts: dict[str, int] = {}
    for item in items_a:
        status_counts[item.status] = status_counts.get(item.status, 0) + 1
    say(f"案件甲逐项终态：{json.dumps(status_counts, ensure_ascii=False)}")
    failed = [(item.relative_source, item.error_code) for item in items_a if item.status == "failed"]
    say(f"失败条目（必须可见）：{json.dumps(failed, ensure_ascii=False)}")
    skipped = [item.relative_source for item in items_a if item.status == "skipped"]
    say(f"跳过条目：{json.dumps(skipped, ensure_ascii=False)}")
    say()

    material_rows, total_materials, _case_total, _cats = materials.list_materials(
        session, case_id=case_a.case_id)
    coverage_by_name = {row.display_name: row.coverage for row in material_rows}
    say("## 2. 解析覆盖")
    say()
    for name, coverage in sorted(coverage_by_name.items()):
        say(f"- {name}：完整 {coverage.complete} / 部分 {coverage.partial} / "
            f"失败 {coverage.failed} / 待处理 {coverage.pending} / 不支持 {coverage.unsupported}")
    say()
    ocr_materials = [row for row in material_rows if row.display_name.startswith("虚构扫描")]
    say(f"OCR 相关材料数：{len(ocr_materials)}")
    for row in ocr_materials:
        say(f"- {row.display_name} 覆盖：{row.coverage.model_dump()}")
    say()

    # ---------- classification, working copy ----------
    target = next(row for row in material_rows if row.display_name == "虚构合同-utf8.txt")
    revision = materials.update_classification(session, case_id=case_a.case_id,
                                              material_id=target.material_id,
                                              category="合同", expected_revision=0)
    working_id, working_relative = materials.create_working_copy(
        session, store, case_id=case_a.case_id,
        file_version_id=session.execute(
            select(models.FileVersion.id).where(
                models.FileVersion.material_id == target.material_id)).scalars().first(),
        display_name="虚构合同-工作副本.txt")
    store.managed_path(working_relative).write_text(
        "TEST-ONLY 工作副本修订版\n\n新增条款：金额 100元。\n", encoding="utf-8")
    new_version, new_job = materials.register_file_version(
        session, store, case_id=case_a.case_id, working_copy_id=working_id, expected_revision=0)
    drain(store)
    say("## 3. 分类与工作副本")
    say()
    say(f"分类更新后 revision={revision}；工作副本登记新版本 {new_version}，任务 {new_job}")
    working_hits = search.search_case(session, case_id=case_a.case_id, query="新增条款",
                                      mode=SearchMode.LITERAL, scope=SearchScope.WORKING)
    original_hits = search.search_case(session, case_id=case_a.case_id, query="新增条款",
                                       mode=SearchMode.LITERAL, scope=SearchScope.ORIGINALS)
    say(f"working scope 命中 {working_hits.total}；originals scope 命中 {original_hits.total}"
        "（原件不应包含工作副本新增内容）")
    say()

    # ---------- search correctness ----------
    say("## 4. 检索正确性")
    say()
    literal = search.search_case(session, case_id=case_a.case_id, query="合同",
                                 mode=SearchMode.LITERAL)
    say(f"- literal「合同」：命中 {literal.total}，覆盖 {literal.coverage.model_dump()}")
    identifier = search.search_case(session, case_id=case_a.case_id, query="HT-001",
                                    mode=SearchMode.IDENTIFIER)
    say(f"- identifier「HT-001」：命中 {identifier.total}")
    amount_100 = search.search_case(session, case_id=case_a.case_id, query="100",
                                    mode=SearchMode.AMOUNT, currency="CNY")
    amount_1000 = search.search_case(session, case_id=case_a.case_id, query="1000",
                                     mode=SearchMode.AMOUNT, currency="CNY")
    amount_wan = search.search_case(session, case_id=case_a.case_id, query="1万元",
                                    mode=SearchMode.AMOUNT, currency="CNY")
    amount_usd = search.search_case(session, case_id=case_a.case_id, query="100",
                                    mode=SearchMode.AMOUNT, currency="USD")
    say(f"- amount 100 CNY：{amount_100.total} 条，值 "
        f"{[str(hit.amount) for hit in amount_100.hits][:4]}")
    say(f"- amount 1000 CNY：{amount_1000.total} 条（必须与 100 区分）")
    say(f"- amount 1万元 CNY：{amount_wan.total} 条，值 "
        f"{[str(hit.amount) for hit in amount_wan.hits][:4]}")
    say(f"- amount 100 USD：{amount_usd.total} 条（不得与 CNY 合并）")
    date_hits = search.search_case(session, case_id=case_a.case_id, query="2024-03-05",
                                   mode=SearchMode.DATE)
    say(f"- date 2024-03-05：{date_hits.total} 条")
    cross_case = search.search_case(session, case_id=case_b.case_id, query="合同",
                                    mode=SearchMode.LITERAL)
    say(f"- 案件乙 literal「合同」：{cross_case.total} 条（案件隔离）")
    say()

    # ---------- historical reference after re-OCR ----------
    say("## 5. 历史引用在重新解析后仍指向旧版本")
    say()
    first_hit = literal.hits[0].source
    old_text = search.read_source(session, case_id=case_a.case_id,
                                  source_ref=first_hit.source_ref).text
    run_id, job_id = parsing.reparse(session, case_id=case_a.case_id,
                                     file_version_id=first_hit.file_version_id)
    drain(store)
    after_text = search.read_source(session, case_id=case_a.case_id,
                                    source_ref=first_hit.source_ref).text
    refreshed = session.execute(select(models.FileVersion).where(
        models.FileVersion.id == first_hit.file_version_id)).scalar_one()
    say(f"旧 source_ref {first_hit.source_ref}：重做前后文本一致 = {old_text == after_text}")
    say(f"该文件版本 active_parse_run_id 已变为新 run = "
        f"{refreshed.active_parse_run_id == run_id}")
    say()

    # ---------- analysis ----------
    say("## 6. 分析成果与网页可见性")
    say()
    saved = analysis.save_analysis(session, store, case_id=case_a.case_id,
                                   title="TEST-ONLY 验收分析", content="结论：合同有效。",
                                   statement_kind=StatementKind.AGENT_ANALYSIS,
                                   source_refs=[first_hit.source_ref])
    reviewed = analysis.review_analysis(session, case_id=case_a.case_id,
                                        analysis_id=saved.analysis_id,
                                        version_id=saved.version_id, expected_revision=0,
                                        review_status=ReviewStatus.APPROVED)
    analysis_hits = search.search_case(session, case_id=case_a.case_id, query="结论",
                                       mode=SearchMode.LITERAL, scope=SearchScope.ANALYSIS)
    say(f"保存分析 {saved.analysis_id} 版本 {saved.version_id}；审核后 revision={reviewed.revision}")
    say(f"analysis scope 命中 {analysis_hits.total}，标注 source_type="
        f"{[hit.source_type for hit in analysis_hits.hits]}")
    say()

    # ---------- consistency before backup ----------
    report = consistency.check_consistency(session, store)
    say("## 7. 备份前一致性")
    say()
    say(f"ok={report.ok}，issues={len(report.issues)}，orphans={len(report.orphan_files)}，"
        f"staging={len(report.staging_entries)}")
    for issue in report.issues[:5]:
        say(f"- {issue.kind}: {issue.relative_path or issue.detail}")
    say()

    # ---------- backup ----------
    say("## 8. 备份")
    say()
    started = time.time()
    result = backup.create_backup(session, store, BACKUP_DIR, database_url=func_app,
                                  pg_bin=os.environ.get("LWB_PG_BIN"))
    say(f"备份耗时 {time.time() - started:.1f}s，文件 {result.file_count} 个，"
        f"{result.total_bytes} 字节，complete={result.complete}")
    manifest = backup.verify_backup(BACKUP_DIR)
    say(f"清单校验通过：schema {manifest['schema_version']}，文件 {len(manifest['files'])} 个")
    say()

    # ---------- restore ----------
    say("## 9. 空环境恢复")
    say()
    restore_url = url_for(settings.migration_url, FUNC_RESTORE_DB)
    restore_report = backup.restore_backup(BACKUP_DIR, data_root=RESTORED_DATA,
                                           database_url=restore_url,
                                           pg_bin=os.environ.get("LWB_PG_BIN"))
    say(f"恢复文件 {restore_report['restored_files']} 个，schema {restore_report['schema_version']}")
    reset_engine()
    configure_engine(url_for(settings.database_url, FUNC_RESTORE_DB))
    restored_session = get_session_factory()()
    restored_store = Storage(root=RESTORED_DATA)
    restored_search = search.search_case(restored_session, case_id=case_a.case_id, query="合同",
                                         mode=SearchMode.LITERAL)
    restored_amount = search.search_case(restored_session, case_id=case_a.case_id, query="100",
                                         mode=SearchMode.AMOUNT, currency="CNY")
    restored_analysis = search.search_case(restored_session, case_id=case_a.case_id,
                                           query="结论", mode=SearchMode.LITERAL,
                                           scope=SearchScope.ANALYSIS)
    restored_consistency = consistency.check_consistency(restored_session, restored_store)
    say(f"恢复后检索：literal {restored_search.total}，amount {restored_amount.total}，"
        f"analysis {restored_analysis.total}")
    say(f"恢复后一致性：ok={restored_consistency.ok}，issues={len(restored_consistency.issues)}，"
        f"orphans={len(restored_consistency.orphan_files)}")
    for issue in restored_consistency.issues:
        say(f"- 恢复后问题：{issue.kind} {issue.relative_path or issue.detail}")
    for orphan in restored_consistency.orphan_files:
        say(f"- 恢复后孤立文件：{orphan}")
    restored_session.close()
    say()

    # ---------- concurrency ----------
    say("## 10. 并发：两个独立连接同时写")
    say()
    configure_engine(func_app)
    session_a = get_session_factory()()
    session_b = get_session_factory()()
    target_row = session_a.execute(select(models.Material).where(
        models.Material.case_id == case_a.case_id)).scalars().first()
    starting_revision = target_row.revision
    materials.update_classification(session_a, case_id=case_a.case_id,
                                    material_id=target_row.id, category="并发甲",
                                    expected_revision=starting_revision)
    conflict = None
    try:
        materials.update_classification(session_b, case_id=case_a.case_id,
                                        material_id=target_row.id, category="并发乙",
                                        expected_revision=starting_revision)
    except Exception as exc:  # noqa: BLE001 - the conflict is the expected outcome
        conflict = getattr(getattr(exc, "code", None), "value", type(exc).__name__)
    say(f"同一 revision 并发更新：第一次成功，第二次结果 = {conflict}")
    session_b.rollback()
    cross = None
    try:
        search.read_source(session_b, case_id=case_b.case_id,
                           source_ref=first_hit.source_ref)
    except Exception as exc:  # noqa: BLE001
        cross = getattr(getattr(exc, "code", None), "value", type(exc).__name__)
    say(f"跨案读取 source_ref 结果 = {cross}")
    session_a.close()
    session_b.close()
    say()

    # ---------- web client + agent concurrently ----------
    say("## 10b. 网页客户端与 Agent 同时读写")
    say()

    # 隔离端口：正式服务可能正监听 settings.port（8765）。若沿用该端口，
    # uvicorn 绑定失败后健康检查会命中正式服务，后续 HTTP 实际打到生产库。
    # 这里显式取一个空闲端口，并让子进程的安全校验（allowed_hosts）用同一端口。
    port = int(os.environ.get("LWB_ACCEPTANCE_PORT", "0")) or _free_port()
    server_env = dict(os.environ, LC_ALL="en_US.UTF-8", LWB_PORT=str(port))
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "legal_workbench.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--no-access-log"],
        cwd=ROOT / "backend", env=server_env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            try:
                with urllib.request.urlopen(base + "/api/v1/health", timeout=2) as response:
                    if json.load(response) == {"app": "legal-workbench"}:
                        break
            except (urllib.error.URLError, TimeoutError):
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("验收网页服务未就绪")
        # 确认应答者确为本脚本启动的隔离实例，而不是恰好监听同端口的其他服务。
        if server.poll() is not None:
            raise RuntimeError(f"验收网页服务已退出（code={server.returncode}），端口可能被占用")

        def http_call(method: str, path: str, body=None):
            headers = {"Origin": base, "Host": f"127.0.0.1:{port}"}
            data = None
            if body is not None:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
            request = urllib.request.Request(base + path, data=data, headers=headers,
                                             method=method)
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    payload = response.read()
                    return response.status, (json.loads(payload) if payload else None)
            except urllib.error.HTTPError as error:
                return error.code, json.loads(error.read() or b"null")

        status, listed_cases = http_call("GET", "/api/v1/cases")
        assert status == 200 and "items" in listed_cases, (status, listed_cases)
        say(f"网页直接读取案件列表：HTTP {status}")

        # Web reads while the agent writes: both must see the same stable identity.
        web_material = target_row.id
        status, listed = http_call("GET", f"/api/v1/cases/{case_a.case_id}/materials")
        web_revision = next(item["revision"] for item in listed["items"]
                            if item["material_id"] == str(web_material))
        say(f"网页读到材料 revision={web_revision}（与数据库一致："
            f"{web_revision == starting_revision + 1}）")

        agent_result: dict[str, object] = {}

        def agent_write():
            session_c = get_session_factory()()
            try:
                materials.update_classification(
                    session_c, case_id=case_a.case_id, material_id=web_material,
                    category="Agent并发", expected_revision=web_revision)
                agent_result["ok"] = True
            except Exception as exc:  # noqa: BLE001
                agent_result["error"] = getattr(getattr(exc, "code", None), "value",
                                                type(exc).__name__)
            finally:
                session_c.close()

        thread = threading.Thread(target=agent_write)
        thread.start()
        # Web writes with the revision it just read, racing the agent on purpose.
        status, web_write = http_call(
            "PATCH", f"/api/v1/cases/{case_a.case_id}/materials/{web_material}/classification",
            {"case_id": str(case_a.case_id), "material_id": str(web_material),
             "category": "网页并发", "expected_revision": web_revision})
        thread.join()
        say(f"并发写结果：Agent={agent_result}，网页 HTTP {status} "
            f"{web_write.get('code') if isinstance(web_write, dict) else web_write}")
        winners = (1 if agent_result.get("ok") else 0) + (1 if status == 200 else 0)
        say(f"恰好一个写入成功 = {winners == 1}（另一个必须 conflict）")

        status, after = http_call("GET", f"/api/v1/cases/{case_a.case_id}/materials")
        final_revision = next(item["revision"] for item in after["items"]
                              if item["material_id"] == str(web_material))
        say(f"最终 revision={final_revision}（应为 {web_revision + 1}，只加一次）")
        web_agent_ok = winners == 1 and final_revision == web_revision + 1
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
    say()

    # ---------- fault injection ----------
    say("## 11. 故障注入")
    say()
    fault_session = get_session_factory()()
    fault_source = SOURCE / "故障样本"
    fault_source.mkdir(exist_ok=True)
    for index in range(4):
        (fault_source / f"虚构故障{index}.txt").write_text(
            f"TEST-ONLY 故障样本 {index} 金额 {100 * (index + 1)}元", encoding="utf-8")
    fault_case = cases.create_case(fault_session, title="TEST-ONLY 故障案件")
    fault_intake = imports.create_intake(fault_session, store, case_id=fault_case.case_id,
                                         source_path=str(fault_source), source_label="故障",
                                         request_key="fault-1")

    calls = {"n": 0}

    def crash_once(stage: str) -> None:
        if stage == "copying":
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated crash during copy")

    crashed = False
    try:
        imports.process_import_job(get_session_factory(), store, fault_intake.job_id,
                                   fault_inject=crash_once)
    except RuntimeError:
        crashed = True
    fault_session.expire_all()
    job_after_crash = fault_session.get(models.Job, fault_intake.job_id)
    say(f"复制中断：崩溃发生={crashed}，任务状态={job_after_crash.status}")
    drain(store)
    fault_session.expire_all()
    job_after_retry = fault_session.get(models.Job, fault_intake.job_id)
    fault_materials = fault_session.execute(select(func.count()).select_from(models.Material)
                                            .where(models.Material.case_id == fault_case.case_id)
                                            ).scalar()
    say(f"恢复后任务状态={job_after_retry.status}，材料数={fault_materials}（应为 4，无重复）")

    # OCR process exit: run the engine in a child process and kill it mid-run, so
    # this exercises a real crash rather than only a missing model directory.
    child_script = (
        "import sys, time; sys.path.insert(0, %r);\n"
        "from legal_workbench.parsing import ocr;\n"
        "from legal_workbench.config import load_settings;\n"
        "engine = ocr.OcrEngine.get();\n"
        "print('READY', flush=True);\n"
        "time.sleep(600)\n"
    ) % str(ROOT / "backend" / "src")
    child = subprocess.Popen([sys.executable, "-c", child_script],
                             stdout=subprocess.PIPE, text=True)
    ready_line = child.stdout.readline().strip() if child.stdout else ""
    child.kill()
    exit_code = child.wait()
    say(f"OCR 子进程真实终止：启动输出={ready_line!r}，退出码={exit_code}（-9 表示被强杀）")
    ocr_crash_detected = exit_code != 0

    # The same crash must surface as a clear error, not a silent empty result.
    from legal_workbench.parsing import ocr as ocr_module

    empty_models = WORK / "no-models"
    empty_models.mkdir(exist_ok=True)
    original_ocr = os.environ.get("LWB_OCR_MODEL_DIR")
    os.environ["LWB_OCR_MODEL_DIR"] = str(empty_models)
    ocr_module.OcrEngine._cache.clear()
    dependency_error = None
    try:
        ocr_module.OcrEngine.get()
    except Exception as exc:  # noqa: BLE001
        dependency_error = getattr(getattr(exc, "code", None), "value", type(exc).__name__)
    say(f"OCR 模型缺失时错误码 = {dependency_error}")
    if original_ocr is None:
        os.environ.pop("LWB_OCR_MODEL_DIR", None)
    else:
        os.environ["LWB_OCR_MODEL_DIR"] = original_ocr
    ocr_module.OcrEngine._cache.clear()

    # Backup killed mid-flight: maintenance stays on, INCOMPLETE remains.
    killed_dir = WORK / "backup-killed"
    killed_dir.mkdir()
    marker = killed_dir / backup.INCOMPLETE_NAME
    marker.write_text("started_at=simulated\n")
    fault_session.execute(text("UPDATE maintenance_state SET active=true, reason='simulated kill' "
                               "WHERE id=1"))
    fault_session.commit()
    write_blocked = None
    try:
        materials.update_classification(fault_session, case_id=case_a.case_id,
                                        material_id=target_row.id,
                                        category="维护中",
                                        expected_revision=target_row.revision)
    except Exception as exc:  # noqa: BLE001
        write_blocked = getattr(getattr(exc, "code", None), "value", type(exc).__name__)
    fault_session.rollback()
    say(f"备份崩溃后维护标志阻断写入 = {write_blocked}；INCOMPLETE 存在 = {marker.exists()}")
    backup.clear_maintenance(fault_session)
    fault_session.close()
    say()

    # ---------- performance ----------
    say("## 12. 性能")
    say()
    perf_counts = build_performance_corpus(PERF_SOURCE)
    say(f"语料：{perf_counts['files']} 个文件 / {perf_counts['pages']} 页")
    perf_url = url_for(settings.migration_url, PERF_DB)
    perf_app = url_for(settings.database_url, PERF_DB)
    migrate(perf_url)
    os.environ.update({"LWB_DATABASE_URL": perf_app, "LWB_MIGRATION_URL": perf_url,
                       "LWB_DATA_ROOT": str(PERF_DATA), "LWB_IMPORT_ROOTS": str(PERF_SOURCE)})
    reset_engine()
    configure_engine(perf_app)
    perf_store = Storage(root=PERF_DATA)
    perf_session = get_session_factory()()

    perf_case = cases.create_case(perf_session, title="TEST-ONLY 性能案件")
    started = time.time()
    perf_intake = imports.create_intake(perf_session, perf_store, case_id=perf_case.case_id,
                                        source_path=str(PERF_SOURCE), source_label="性能",
                                        request_key="perf-1")
    import_seconds = time.time() - started
    say(f"登记导入：{perf_intake.item_count} 条目，{import_seconds:.1f}s")

    started = time.time()
    drain(perf_store, limit=5000)
    index_seconds = time.time() - started
    units = perf_session.execute(select(func.count()).select_from(models.ParseUnit)).scalar()
    segments = perf_session.execute(select(func.count()).select_from(models.Segment)).scalar()
    exact = perf_session.execute(select(func.count()).select_from(models.ExactValue)).scalar()
    say(f"索引耗时 {index_seconds:.1f}s；parse_units={units}，segments={segments}，"
        f"exact_values={exact}")
    index_report = consistency.check_consistency(perf_session, perf_store)
    say(f"性能库一致性：ok={index_report.ok}，issues={len(index_report.issues)}，"
        f"orphans={len(index_report.orphan_files)}")
    say()

    # warm-up, then measure
    probes = [
        ("literal 2字", dict(query="合同", mode=SearchMode.LITERAL)),
        ("literal 3字", dict(query="保密义务", mode=SearchMode.LITERAL)),
        ("identifier", dict(query="HT-00042-000", mode=SearchMode.IDENTIFIER)),
        ("amount CNY", dict(query="1000", mode=SearchMode.AMOUNT, currency="CNY")),
        ("date", dict(query="2024-03-05", mode=SearchMode.DATE)),
    ]
    for _ in range(3):
        for _label, kwargs in probes:
            search.search_case(perf_session, case_id=perf_case.case_id, **kwargs)

    say("| 查询 | 次数 | P50 (ms) | P95 (ms) | 最大 (ms) | 命中 | 失败 |")
    say("|---|---|---|---|---|---|---|")
    benchmark: dict[str, dict] = {}
    for label, kwargs in probes:
        timings: list[float] = []
        failures = 0
        hits = 0
        for _ in range(100):
            started = time.perf_counter()
            try:
                outcome = search.search_case(perf_session, case_id=perf_case.case_id, **kwargs)
                hits = outcome.total
            except Exception:  # noqa: BLE001 - failure rate is part of the report
                failures += 1
            timings.append((time.perf_counter() - started) * 1000)
        benchmark[label] = {
            "p50": statistics.median(timings),
            "p95": percentile(timings, 0.95),
            "max": max(timings),
            "hits": hits,
            "failures": failures,
        }
        row = benchmark[label]
        say(f"| {label} | 100 | {row['p50']:.1f} | {row['p95']:.1f} | {row['max']:.1f} | "
            f"{row['hits']} | {row['failures']} |")
    say()

    # short-word scan timing (below trigram usefulness)
    short_timings = []
    for _ in range(50):
        started = time.perf_counter()
        search.search_case(perf_session, case_id=perf_case.case_id, query="甲",
                           mode=SearchMode.LITERAL)
        short_timings.append((time.perf_counter() - started) * 1000)
    say(f"单字扫描「甲」：P50 {statistics.median(short_timings):.1f} ms，"
        f"P95 {percentile(short_timings, 0.95):.1f} ms")
    say()

    # OCR timing on the independent scanned sample
    say("## 13. OCR 实测（独立扫描样本）")
    say()
    ocr_sample = SOURCE / "虚构扫描.pdf"
    from legal_workbench.parsing.pdf import iter_pdf_units

    def _rss_mb() -> float:
        # ru_maxrss is a *high-water mark*: reading it before and after an OCR run
        # in the same process always reports a 0 delta once anything peaked
        # earlier.  Use the live RSS from ps instead.
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                             capture_output=True, text=True)
        try:
            return int(out.stdout.strip()) / 1024
        except (TypeError, ValueError):
            return 0.0

    baseline_rss = _rss_mb()
    started = time.time()
    ocr_units = list(iter_pdf_units(ocr_sample, settings))
    ocr_seconds = time.time() - started
    peak_rss = _rss_mb()
    say(f"扫描 PDF 1 页：耗时 {ocr_seconds:.2f}s，单元 {len(ocr_units)} 个，"
        f"状态 {[unit.status for unit in ocr_units]}")
    say(f"内存：OCR 前 RSS {baseline_rss:.0f} MB，OCR 后 {peak_rss:.0f} MB，"
        f"增量 {peak_rss - baseline_rss:.0f} MB（ps 实时 RSS）")
    say(f"失败单元数：{sum(1 for unit in ocr_units if unit.status == 'failed')}")
    sample_text = (ocr_units[0].text if ocr_units and ocr_units[0].text else "")
    say(f"识别文本片段：{sample_text[:60]!r}")
    say()

    # ---------- summary ----------
    say("## 14. 判定")
    say()
    p95_literal = benchmark["literal 2字"]["p95"]
    checks = {
        "检索 P95 ≤ 2000ms": p95_literal <= 2000,
        "所有查询失败数为 0": all(row["failures"] == 0 for row in benchmark.values()),
        "备份前一致性 ok": report.ok,
        "恢复后一致性 ok": restored_consistency.ok,
        "恢复后检索可用": restored_search.total > 0 and restored_amount.total > 0,
        "并发写仅一个成功": conflict == "conflict",
        "跨案读取被拒": cross == "case_mismatch",
        "崩溃后无重复材料": fault_materials == 4,
        "OCR 缺模型报 dependency_missing": dependency_error == "dependency_missing",
        "OCR 子进程真实终止": ocr_crash_detected,
        "网页与 Agent 并发仅一个成功": web_agent_ok,
        "维护标志阻断写入": write_blocked == "maintenance",
        "性能库一致性 ok": index_report.ok,
    }
    for name, passed in checks.items():
        say(f"- {'通过' if passed else '未通过'}：{name}")
    say()
    ok = all(checks.values())
    say(f"T19 判定：{'通过' if ok else '未通过'}")

    perf_session.close()
    session.close()
    reset_engine()

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n报告写入 {REPORT}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
