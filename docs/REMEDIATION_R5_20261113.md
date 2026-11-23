# 第五轮整改回执（第四轮复审 1 项 P1 + 2 项 P2）

依据 `docs/V0.1_第四轮整改复审_20261113.md`。三项均先复现、再修、再做反向验证（把修复还原，对应测试必须失败）。

## 1（P1）原件校验失败提交半个批次，原请求键无法重试

**根因**：`create_upload_intake` 的校验失败分支为了持久化 `integrity_error`，直接 `session.commit()`。该提交把此前已处理的条目、接收批次一起落库，批次有 1 条条目、0 个任务；重试同请求键命中 `existing items 无 job` 分支，返回 `conflict`「既有接收批次缺少任务记录」。物理文件也没人清。

**修法**（`backend/src/legal_workbench/imports.py`）：失败分支改为先 `session.rollback()`，删掉本批次自己发布的文件（新增 `published_paths` 累积），丢弃 staged，再单独用一个短事务把完整性标记写到既有对象上。语义选“整批失败、不留痕”，因为半成品批次无法恢复也无法重试。

**验证**：`test_upload_dedupe.py::test_integrity_failure_rolls_back_the_batch_and_the_request_key_still_works`——失败后接收批次与条目集合不变、完整性标记仍在、无无引用文件，同请求键重试返回 200。还原修复后该测试在“失败批次留下了接收记录”处失败。

## 2（P2）不同工作副本并行登记相同字节，一次失败并留下无引用文件

**根因**：`register_file_version` 的 CAS 锁在 `WorkingCopy` 行上，两份副本是不同行，两个请求都通过 CAS；`StoredObject` 插入在异常清理范围之外，撞 `uq_stored_objects_case_sha` 直接抛 `IntegrityError`，已发布的版本文件留在磁盘。

**修法**（`backend/src/legal_workbench/materials.py`）：插入包进 `begin_nested()`；输家捕获 `IntegrityError` 后查出赢家对象，若自己发布的路径不是赢家的路径则 `delete_unpublished`，校验赢家对象完整性（失败按既有口径回滚并标记），然后照常建自己的 `FileVersion` 与解析任务。两个版本共用同一对象。

**验证**：`test_register_concurrency.py::test_two_copies_of_one_material_registering_identical_bytes`——两线程各自 `revision=0` 同时登记同字节：无异常、两个 working 版本、一个对象、两个解析任务、对象可校验、无无引用文件。还原修复后 `{'second': IntegrityError('uq_stored_objects_case_sha')}`。

## 3（P2）甲材料的迟到保存响应改动乙详情

**根因**：`saveCategory` / `saveName` 直接用响应写 `revision`、`notice`、`busy`，没有副本操作已有的 generation + 归属守卫。

**修法**（`frontend/src/components/MaterialDetail.tsx`）：加 `editGeneration` ref，切换材料时自增；两个保存函数捕获发起时的 generation 与 `material_id`，`mine()` 同时校验两者，成功、失败、`finally` 三处都只在归属成立时改界面。

**验证**：`MaterialSwitch.test.tsx::编辑响应的归属 > 甲的分类保存迟到时…`——挂起甲、切乙、挂起乙，让甲先返回 `revision=7`：乙不显示「分类已保存」、修订号仍 0、按钮仍 disabled；乙自己返回 `revision=1` 后才更新。还原修复后测试在「分类已保存」出现处失败。

## 回归

| 项 | 结果 |
|---|---|
| 后端 pytest | **264 passed**（+2） |
| 前端 vitest | **24 passed**（5 文件，+1） |
| `tsc --noEmit` | 无错误 |
| `run_acceptance.py` | **13/13 通过**，单字扫描 P95 **284.0 ms** |
| `rehearse_backup_restore.py` | **7/7**，`RESTORE_REHEARSAL_OK = True`，恢复后检索/一致性正常 |

## 仍不覆盖

- 8 项环境与规模未验证项不变（Linux、真实案件材料、5 万页规模、备份进程 `kill -9`、加密 Office、多用户并发写、真实浏览器限速、真实 SIGKILL）。
- 旧 22 条检索测试仍待原样本与预期结果文件。
- 三项修复均在单机 macOS + 独立 PG 实例上验证；P2 第 2 项的竞争窗口由 `threading.Barrier` 同步到插入点，未做跨机压测。
