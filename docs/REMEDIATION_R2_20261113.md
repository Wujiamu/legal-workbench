# 第二轮整改：独立验收与代码审查问题的修复证据

日期：2026-11-13。依据：`docs/V0.1_独立验收与代码审查_20261113.md` 第 2、3 节。

上一轮交接声称「24 条验收全部通过」，独立审查在同一天推翻了其中若干条。本文档只记录本轮改了什么、用什么证明、还剩什么没验证；不含审查结论。

---

## 一、总览

| 编号 | 优先级 | 问题 | 状态 | 证据 |
|---|---|---|---|---|
| R03-2 | P1 | 任务终态与索引发布仍分两次提交 | 已修 | `test_r03_publish_commit.py` 7 项 |
| R02-2 | P1 | 多点文件名生成不可解析副本 | 已修 | `test_r02_working_copy_extension.py` 17 项 |
| CAS | P1 | 同修订号可并发登记两次 | 已修 | `test_register_concurrency.py` 3 项 |
| DEDUP | P1 | 相同字节上传中止整批 | 已修 | `test_upload_dedupe.py` 4 项 |
| INIT | P1 | 空目录初始化失败 | 已修 | 空目录实跑（见第五节） |
| SWITCH | P1 | 切换材料时原件错配 | 已修 | `MaterialSwitch.test.tsx` 3 项 |
| TESTDB | P1 | 回归测试未绑定测试库 | 已修 | 空默认库实跑（见第五节） |
| R04-2 | P2 | 指定页 OCR 丢页却报完整 | 已修 | `test_r04_skip_before_ocr.py` 第 5 项 |
| OCR-T | P2 | 超时错误分类不稳定 | 已修 | `test_parsing_pdf_ocr.py` 10 项 |
| T16/T17 | P2 | 上传跟踪与检索定位缺口 | 已修 | `UploadPanel.test.tsx` 6 项、`SearchDeepLink.test.tsx` 3 项 |
| BK-VER | P3 | 备份清单 schema 标识漂移 | 已修 | `test_backup_consistency.py` 第 24 项 |

本轮后端 **245 passed**（上一轮 220 passed + 1 failed），前端 **20 passed**，验收脚本 13/13，备份恢复演练 7/7。

---

## 二、逐项说明

### R03-2 终态与发布同事务（P1）

**审查结论**：`_finish` 调用的 `_refresh_coverage` 内部会 `session.commit()`，所以在「终态提交」和「切换活动索引」之间仍有一个真实提交点。在 `_publish_active` 入口退出，新连接看到 run=`complete`、job=`succeeded`、active=null，而 worker 只领取 `queued` 任务，永远不会补发。

**修法**（`parsing/pipeline.py`）：

1. `_refresh_coverage` 增加 `commit` 参数，默认保持原行为；`_finish` 传 `commit=False`，只 `flush()`。
2. `_finish` 内顺序变为：写终态 → 刷新覆盖（不提交）→ `_publish_active` → 唯一一次 `session.commit()`。
3. 新增 `repair_unpublished_runs(session)`：启动时扫描「版本无活动 run，但存在终态 run」的记录并补发。这类记录的 job 已是 `succeeded`，靠领取任务永远到不了。
4. `worker.run_worker` 在 `recover_interrupted_jobs` 之后调用它。
5. 新增注入点 `before_publish`（`_publish_active` 入口），正是审查注入的位置。

**证据**：`test_r03_publish_commit.py`

- `test_crash_before_commit_leaves_nothing_durable_and_worker_recovers[units_done|before_finish|before_publish]`：三个边界退出后，新连接读到的 run/job 都是 `running`、active 为 null；随后用**正常 worker**（`worker.run_worker`）恢复，断言 active 指向该 run、任务 `succeeded`、检索命中 1 条。
- `test_worker_publishes_legacy_succeeded_but_unpublished_run`：人工把 active 置空模拟旧代码留下的现场，worker 领取 0 个任务却完成补发，检索命中。
- `test_repair_does_not_override_a_newer_active_run`：已有更新的活动版本时补发为 0。
- `test_publish_still_keeps_history_reference_valid`：旧 `source_ref` 仍返回原文本。

### R02-2 多点文件名（P1）

**审查结论**：`working_copy_name` 先去掉 `.docx`，再从剩下的 stem 取后缀；`合同.v2` 的后缀是 `.v2`，于是得到 `合同.v2（工作副本）.v2`，登记后解析 `unsupported_format`。

**修法**（`materials.py`）：扩展名直接取完整显示名已经带的那一个；只有显示名没有可用扩展名时才回退到存储对象。`_extension_for` 不再接收被剥掉扩展名的 stem。

**证据**：`test_r02_working_copy_extension.py` 参数化新增 `合同.v2.docx`、`证据 3.1 终稿.xlsx`、`报告 v1.0.0.pdf`、`记录.2024.01.txt`；`test_multi_dot_name_full_flow` 走完「建副本 → 改内容 → 登记 → worker 解析 → working 命中新内容 → originals 不含新内容、原内容仍可检索」。

### CAS 并发登记（P1）

**审查结论**：`register_file_version` 只做读取式预检，`update_with_revision` 的返回值被丢弃，两个连接都能提交。

**修法**：校验 CAS 返回值；为 0 时回滚、删除已发布的版本文件（该版本行没有提交，文件已无引用）、抛 `conflict`。

**证据**：`test_register_concurrency.py`

- 两个连接都先读到 revision=0，再先后登记：第二个抛 `conflict`，最终只有 1 个 working 版本、1 个解析任务、revision=1。
- 失败方不留下无引用文件。
- 明显陈旧修订在发布前即被拒绝。

### DEDUP 相同字节上传（P1）

**审查结论**：`create_upload_intake` 每项无条件新增 `StoredObject`，撞 `uq_stored_objects_case_sha`，整批失败。

**修法**：发布后按 `(case_id, sha256)` 查既有对象，命中则复用（并校验），否则新增。文件本身按内容寻址，重复发布是幂等的。

**证据**：`test_upload_dedupe.py` 4 项——同批、跨批、同目录不同路径都只产生 1 个存储对象和 2 份材料；两份材料的分类互相独立。

### INIT 空目录初始化（P1）

**审查结论**：`scripts/lwb` 算好了 `PGHOST/PGPORT/PGUSER/PGPASSFILE`，两次 `psql CREATE ROLE` 却没传该环境，psql 转去默认 socket 5432。

**修法**：两次 `psql` 与 `createdb` 都传 `env`；失败时停止本次启动的数据库。同时把 `init` 补成一次完成——建角色、建库、写 `.env`、迁移、启动，而不是留下半装状态让用户再跑两条命令。

**证据**：临时目录完整拷贝 + 改端口 55499 + `env -u PGHOST -u PGPORT -u PGUSER -u PGPASSWORD -u PGPASSFILE ... ./scripts/lwb init`：

```
CREATE ROLE ×2 → 角色与数据库已创建
alembic: 0001 → 0002 → 0003 全部 upgrade
Uvicorn running on http://127.0.0.1:8791 → HTTP 200
pg_roles: lwb_app(f) / lwb_migrator(t CREATEDB) / lwb_super
public 表数 17，.env 权限 -rw-------
```

### SWITCH 材料切换原件错配（P1）

**审查结论**：`MaterialDetail` 在 A→B 时复用组件，保留 A 的 `original` 与 `segments`；B 请求未返回期间，B 标题下挂着 A 的预览、下载、建副本、重新解析。

**修法**（`MaterialDetail.tsx`）：

1. 切换时立即清空 `original`、`segments`，并置 `originalLoading`。
2. 用 generation ref 丢弃迟到响应（原件与提取文本各自一个），避免 A 的响应覆盖 C。
3. 原件未就绪时预览区显示「正在读取原件…」，不是「尚无原件版本」。
4. 相关按钮本就以 `!original` 禁用，清空后自然不可点。

**证据**：`MaterialSwitch.test.tsx` 3 项——B 未返回时不得出现 A 的 iframe/下载/可点按钮；A、B 迟到响应不得覆盖 C；A 的迟到提取文本不得贴到 B 上。反向验证：去掉清空与 generation 守卫后 3 项全部失败。

### TESTDB 测试绑定测试库（P1）

**审查结论**：只有 `db` fixture 切换全局引擎，`clean_db` 只清 fixture 库；直接调 `get_session_factory()` 的测试连的是配置库。

**修法**：新增 session 级 autouse fixture `bind_test_engine`，整个会话把全局引擎指向 fixture 库；`db` fixture 不再 `reset_engine()`（否则后续直接调用会退回配置库）。`store` fixture 移入 conftest 供各模块共用。

**证据**：建一个**没有任何表**的空库，把 `LWB_DATABASE_URL` / `LWB_MIGRATION_URL` 指向它，跑 R01–R04 五个文件：

```
空库表数 = 0
43 passed
跑完后默认库表数 = 0
```

即：默认库无业务表仍全部通过，且没有向配置库写入。

### R04-2 指定页 OCR 丢页（P2）

**审查结论**：`copy_keys` 包含没有 Segment 的已完成页，`_copy_base_unit` 遇到这类页返回 None，于是空白页从新 run 消失；两页 PDF 只对第 2 页强制 OCR，新 run 只剩第 2 页、却报完整、进度 1/2。

**修法**：`_copy_base_unit` 对无 Segment 的终态页仍返回一个 `ParsedUnit`（保留原状态、method、error、warnings，locator 由 unit_key 还原），让空白页以 `no_text_detected` 留在新 run 里。

**证据**：`test_forced_page_keeps_pages_without_reusable_text`——新 run 的 unit 键为 `["pdf:1","pdf:2"]`，`pdf:1` 仍是 `no_text_detected`，job 进度 2/2。反向验证：还原旧逻辑后断言得到 `['pdf:2']` 并失败。

### OCR-T 超时分类（P2）

**审查结论**：`time_limit` 抛 `TimeoutError`，但 RapidOCR 的缩放路径会把它包成 `ResizeImgError`；`pdf.py` 只捕获直接异常，真实超时被报成普通失败。

**修法**：`time_limit` 改为 yield 一个 `Deadline`，处理器先置 `expired=True` 再抛；`OcrEngine.recognize` 捕获任何异常时若 `expired` 为真则改抛 `TimeoutError`，否则原样抛出。

**证据**：`test_parsing_pdf_ocr.py` 新增两项——包装型超时报 `TimeoutError`；真实缩放错误仍保留自己的类型。原有 `test_pdf_timeout_reported` 从失败转为通过（这正是审查报告的失败项）。

### T16/T17 网页闭环缺口（P2）

**审查结论**：上传面板只在组件状态保存任务 ID；逐项状态只取 200 条；重试沿用 job ID 导致不再通知；检索命中只在 React 状态，复制的是裸 UUID，没有 `source_ref` 链接参数。

**修法**：

- `UploadPanel`：用 `sessionStorage`（键 `lwb.intake.<caseId>`）记住批次，挂载时恢复并继续轮询；逐项状态分页拉全；重试前清空 `notifiedJob` 与 job 状态，使成功后再次通知父级刷新。
- `SearchPanel`：选中命中写入 `?ref=<source_ref>`；`SourcePanel` 改为接收 `sourceRef`，可直接从 URL 打开；复制按钮改为复制带 `ref` 的完整链接。
- `CaseWorkspacePage`：带 `?ref=` 打开时默认进入检索页签，否则参数要等用户手点页签才生效。

**证据**：`UploadPanel.test.tsx` 6 项（新增：重挂载续跟踪、超一页统计、重试再次通知）、`SearchDeepLink.test.tsx` 3 项（写 URL、URL 直达、复制的是链接）、`CaseWorkspacePage.test.tsx` 第 5 项（`?ref=` 直达检索页签）。

浏览器实测：上传 2 份后**不手动刷新**，2 秒内材料列表出现「全部（2）」；检索后地址栏变为 `?ref=<uuid>`，刷新后直接显示该片段正文。

### BK-VER 清单 schema 标识（P3）

**修法**：清单的 `schema_version` 改为备份时从源库 `alembic_version` 读取，读不到写 `unknown`。

**证据**：`test_manifest_records_the_real_migration_revision`；恢复演练输出 `MANIFEST verified: schema 0003_audit_operation_check`。

---

## 三、口径修订

上一轮 `ACCEPTANCE_HANDOFF.md` 写「24 条验收全部通过」「7 个遗留子项已全部补测」。独立审查在同一天用内存复现和真实并发推翻了其中多条，说明当时的判定标准偏松：测试存在，但不等于测试覆盖了那条验收。

本轮起改用两条判据：

1. 每条验收必须有一个**能失败的测试**——修完后要反向验证：把修复还原，测试必须失败。
2. 崩溃/并发类验收必须在**真实 PostgreSQL + 独立连接/独立 worker** 上验证，不用测试替身替代。

本轮所有新增回归都做了反向验证（R04、SWITCH 两项明确记录；其余由代码结构决定：去掉 CAS 校验、去掉去重分支、去掉 `commit=False` 都会让对应断言失败）。

---

## 四、仍未验证

| 项 | 说明 |
|---|---|
| Linux 平台 | 仅 macOS 27.0.1 |
| 真实案件材料 | 全部 `TEST-ONLY` 合成数据 |
| 5 万页以上 | 验收规模 1000 文件 / 10000 页 |
| 备份进程 `kill -9` | 演练用正常终止 |
| 加密 Office | 未测 |
| 多用户并发写 | 单用户设计，未测 |
| 真实浏览器限速 | 材料切换用组件测试的受控 Promise 覆盖，未在浏览器里人为限速 |
| 崩溃注入方式 | `RuntimeError` 模拟，不是真实 `SIGKILL` |

## 五、待用户提供

- 原有 22 条检索测试的样本与期望结果。
- 原审查六项目对应的仓库或 commit 清单。
