# 备份与恢复

本工具只做本地、单用户备份。备份是数据库自定义格式转储加上全部受管文件的副本，并附带一份可校验的清单。恢复只接受本工具生成的备份，且只写入空数据库与空数据目录。

## 命令

```sh
cd backend
# 备份（目标目录必须为空，且不得位于 DATA_ROOT 内）
.venv/bin/python -m legal_workbench.cli backup --destination /Volumes/备份/lwb-2026-11-10

# 只校验，不恢复
.venv/bin/python -m legal_workbench.cli verify-backup --destination /Volumes/备份/lwb-2026-11-10

# 恢复到空数据库与空目录（两者都必须为空）
.venv/bin/python -m legal_workbench.cli restore \
  --destination /Volumes/备份/lwb-2026-11-10 \
  --data-root /Volumes/新盘/lwb-data
```

`pg_dump` / `pg_restore` 由 `LWB_PG_BIN` 或 PATH 解析；缺失时报 `dependency_missing`。

## 备份做了什么

顺序固定，不可调换：

1. 写入持久维护标志 `maintenance_state.active = true`，此后所有业务写入立即返回 `maintenance`（HTTP 503）。
2. 取维护排他锁，等待已在进行中的写入退出共享锁（默认 `lock_timeout` 60 秒）。
3. `pg_dump --format=custom --no-owner --no-acl`。
4. 逐个复制受管文件（`StoredObject` 与 `AnalysisVersion` 引用的文件），复制前后各读一次源文件哈希；任何一次不一致即整次备份失败。
5. 写 `manifest.json`（schema 版本、应用版本、时间、数据库转储哈希与大小、每个文件的相对路径/大小/哈希）。
6. 删除 `INCOMPLETE` 标记，表示备份完整。
7. 解除维护标志。

失败时保留 `INCOMPLETE` 文件，该目录不可恢复。

### 备份不能阻止外部编辑

工作目录里的文件可能被 Finder、Word 或其他软件直接改写。维护标志只阻止**本服务**的写入，不会阻止外部程序。因此备份期间的校验只能保证"复制那一刻源文件与登记哈希一致"；若外部程序在复制后继续编辑，本次备份仍可能包含已被改动的字节。需要绝对一致时，先关闭其他会写入该目录的软件。

## 恢复做了什么

1. 校验清单必需字段、`complete` 标志、数据库转储哈希、每个文件哈希与路径是否越界。
2. 目标 `DATA_ROOT` 必须不存在或为空；目标数据库 `public` schema 必须没有任何表。
3. 复制文件到新目录，逐个回读校验。
4. `pg_restore --no-owner --no-acl --exit-on-error`。
5. 重新把 `SELECT/INSERT/UPDATE/DELETE` 与序列权限授予 `lwb_app`（转储本身不带 ACL，不做这一步恢复出的库对应用角色不可用）。
6. 把 `running` 状态任务重置为 `queued`，解除维护标志。

恢复**不会**自动清空非空目标，也不会导入任意 SQL。

## 崩溃后如何安全解除维护

如果备份进程被强杀，数据库会停在 `maintenance_state.active = true`，服务拒绝一切写入。先确认没有备份仍在运行：

```sh
ps aux | grep -c "[l]wb.*backup"
ls /目标备份目录/INCOMPLETE   # 存在说明该次备份未完成，不可恢复
```

确认无备份进程后：

```sh
.venv/bin/python -m legal_workbench.cli clear-maintenance
.venv/bin/python -m legal_workbench.cli check-consistency
```

`check-consistency` 是只读检查：核对数据库引用、文件存在与哈希、孤立文件、staging 记录。`repair-consistency` 只完成**已有数据库记录支持**的未完成发布（`published` 状态的导入项、有哈希一致暂存副本的缺失对象），绝不猜测某个文件属于哪个案件。

## 演练记录

`scripts/rehearse_backup_restore.py` 会建立两个临时数据库，造两文件案件、跑 worker、存一条分析，然后备份、恢复到全新库与全新目录，并打印逐项比对（2026-11-20 起还覆盖已归档案件与 0.25h 补录工时）：

```
CONSISTENCY_BEFORE ok = True issues = 0 orphans = 0
MANIFEST verified: schema 0006_case_archive_time files 3
RESTORE: {"restored_files": 3, "schema_version": "0006_case_archive_time", ...}
RESTORED ARCHIVE/TIME: 2026-11-20 ... ('TEST-ONLY 阅卷', Decimal('0.25'), 'manual')
CHECKS: {case_id_identical: true, case_count_identical: true, material_count_identical: true,
         analysis_version_identical: true, no_running_jobs: true, maintenance_cleared: true,
         archived_case_restored: true, time_entry_restored: true, ...}
RUNNING JOBS: 0  MAINTENANCE: False
RESTORE_REHEARSAL_OK = True
RESTORED SEARCH literal = 1 amount = 1 amount_value = 100.000000
RESTORED CONSISTENCY ok = True issues = [] orphans = 0
```

最后一行是关键：恢复后**在新库上重新检索**仍能命中，说明索引与 segment 一起恢复了，而不只是行数对得上。

## 本机实际备份

| 日期 | 目录 | 用途 |
|---|---|---|
| 2026-11-20 18:41 | `var/backups/p6-20261120-184126/` | P6 升级前备份（schema `0004_case_workbench`，`complete: true`，另含升级前 `frontend/dist` 归档）。恢复需空库与空目录；升级已应用，保留至下一次完整备份后再议清理 |
| 2026-11-22 16:47 | `var/backups/p6-20261122-164747/` | 2026-11-22 图表程序化能力与对外引用导出正式应用前备份（schema `0008_claim_amounts`，`files: 2`，`verify-backup` 通过 `complete: true`，另含升级前 `previous-source.tar.gz` 与 `previous-dist/`）。回退时可停 Web/Worker 恢复代码构建或用空库空目录还原 |
| 2026-11-23 13:01 | `var/backups/p6-20261123-130158/` | 2026-11-23 实机验收九项缺陷修复正式应用前备份（schema `0008_claim_amounts`，`files: 13`，`verify-backup` 通过 `complete: true`，另含改前 `previous-source.tar.gz` 与 `previous-dist/`）。回退时停 Web/Worker 恢复代码构建并重启即可，或用空库空目录还原 |
