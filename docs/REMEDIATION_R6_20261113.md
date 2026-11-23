# 第六轮整改回执（第五轮复审 1 项 P1 + 2 项 P2）

依据 `docs/V0.1_第五轮整改复审_20261113.md`。三项均先复现、再修、再做反向验证（把修复还原，对应测试必须失败）。

## 1（P1）失败批次的清理会删掉另一批已登记的原件

**根因**：`create_upload_intake` 回滚后按本批次的路径列表逐个删除文件。相同字节、相同扩展名发布到**同一个受管路径**，所以回滚释放事务后，另一批次可以发布并提交指向该路径的 `StoredObject`；甲再删，删掉的就是乙已登记的原件，乙的材料此后无法通过校验、worker 拒绝解析。

**修法**（`backend/src/legal_workbench/imports.py`）：
- 新增 `_discard_published`：先 `rollback()`，再取 `begin_exclusive`（`pg_advisory_xact_lock`，与 `begin_write` 共用 `MAINTENANCE_KEY`）——它会等所有在途业务事务提交或回滚；随后**逐路径查库**，只有该路径没有任何 `StoredObject` 引用时才删文件，最后再 `rollback` 释放锁。
- `_ensure_stored_object` 里发布文件改到 `begin_write` **之后**，否则“已写盘但未提交”的文件在清理方看来是无引用的，仍会被删。
- 上传循环整体包进 `try/except BaseException`，任何发布后的失败都走同一收尾，不再只有原件校验失败一条分支有清理。

**验证**：`test_upload_dedupe.py::test_failure_cleanup_never_deletes_a_path_another_batch_registered`——用 monkeypatch 把甲的 `delete_unpublished` 卡住 0.4 秒，等乙在独立连接上发布并提交同路径后放行：乙的对象可校验、乙的材料可解析、甲整批失败无残留。还原修复后失败于「乙登记的原件被甲的清理删掉了」。

## 2（P2）后续条目被拒时前序发布文件留在磁盘

**根因**：重复相对路径校验在发布循环内部，前两个合法条目已经写盘，第三个才被拒；清理只覆盖原件校验失败分支。

**修法**：把整批路径校验提到 `begin_write` **之前**，任何发布动作之前就拒绝；配合上一项的 `try/except` 兜底，其余失败分支同样清理。

**验证**：`test_upload_dedupe.py::test_duplicate_relative_path_leaves_no_files`——3 个文件、第 2、3 个路径相同：返回 `422 invalid_input`，`iter_case_files` 为空，`Intake`/`StoredObject` 表均为空。

## 3（P2）切走后成功的改名不刷新材料列表

**根因**：上一轮加的归属守卫 `if (!mine()) return;` 放在 `onChanged()` **之前**，切走后的成功响应直接返回，跳过了列表刷新。分类与改名共用这一结构。

**修法**（`frontend/src/components/MaterialDetail.tsx`）：把 `onChanged()` 提到归属检查之前。列表属于案件、不属于当前详情，任何成功写入都该刷新；`revision`/`notice`/`busy` 仍受归属守卫约束。

**验证**：`MaterialSwitch.test.tsx::切走后的保存仍更新列表`——甲改名挂起 → 切乙 → 列表响应改为新名称 → 甲成功返回：乙不显示甲的提示；切回甲显示「甲材料（改名后）.txt」。还原修复后失败于 `Unable to find role="heading" and name "甲材料（改名后）.txt"`。

## 回归

| 项 | 结果 |
|---|---|
| 后端 pytest | **266 passed**（+2） |
| 前端 vitest | **25 passed**（5 文件，+1） |
| `tsc --noEmit` | 无错误 |
| `run_acceptance.py` | **13/13 通过**，单字扫描 P95 **264.1 ms** |
| `rehearse_backup_restore.py` | **7/7**，`RESTORE_REHEARSAL_OK = True`，恢复后检索与一致性正常 |

## 新增的机制说明

`begin_exclusive` 是 `db.py` 新增的函数：与 `begin_write` 共用同一 advisory 锁键，但取**独占**模式，语义是“本事务结束前不许任何业务写入”。它只用于失败清理这一处，不改变正常写入路径（仍是共享锁）。备份流程用的也是独占锁，因此备份与清理互斥，两者不会同时改文件。

## 仍不覆盖

- 8 项环境与规模未验证项不变。
- 旧 22 条检索测试仍待原样本与预期结果文件。
- 清理路径的竞争窗口用 `time.sleep(0.4)` 固定住，未在真实多进程、跨机并发下压测；清理期间写入方会被阻塞至多一个清理事务的长度（毫秒级），未测极端慢盘下的阻塞时长。
