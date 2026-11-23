# 第三轮整改：第二轮复审问题的修复证据

日期：2026-11-13。依据：`docs/V0.1_第二轮整改复审_20261113.md` 第 1 节（5 项阻断 + 1 处文档偏差）。

本文档只记录改了什么、用什么证明、还剩什么没验证；不含审查结论。

---

## 一、总览

| 项 | 优先级 | 复审结论 | 本轮修法 | 证据 |
|---|---|---|---|---|
| 旧索引补发漏重解析、可能选错版本 | P1 | 补发查询排除已有活动索引的版本，且按 sequence 升序取首个 | 改为按「每个版本取最新终态 run，交给 `_publish_active` 裁决」 | `test_r03_publish_commit.py` 10 项 |
| 合法改名把 DOCX 副本变成不可解析 | P1 | `合同.v2` → `合同（工作副本）.v2`，解析失败 | 后缀只有等于真实扩展名时才剥离，其余算名字 | `test_r02_working_copy_extension.py` 22 项 |
| 并行登记失败方绕过冲突处理 | P1 | 两个连接同时发布，后者撞 `uq_stored_objects_case_sha` 并留下孤儿文件 | CAS 提到任何副作用之前；后续失败清理已发布文件 | `test_register_concurrency.py` 4 项（含真并行） |
| 切换材料仍显示上一份的正文和副本 | P1 | 文本代次只在发新请求时推进；副本回包直接更新状态 | 切换时同时作废文本与副本代次；回包校验材料归属 | `MaterialSwitch.test.tsx` 5 项 |
| 上传去重仍留孤儿、并行撞唯一约束 | P2 | 先按扩展名发布，再查哈希 | 先查哈希再发布；并发插入用 savepoint 兜底 | `test_upload_dedupe.py` 6 项 |
| README 与 `init` 行为不符 | — | README 写「建库后停库」 | 改为「建库、迁移、前台启动」 | 本文第二节 |

当前：后端 **256 passed**，前端 **22 passed**，验收 13/13，恢复演练 7/7。

---

## 二、逐项说明

### P1-1 旧索引补发（`parsing/pipeline.py`）

**复审复现**：同一版本两个 complete run，指针恢复为旧 run。正常 worker 启动后新任务仍 `succeeded`、指针仍指旧 run；把指针清空重试也只补发旧 run。根因是查询条件 `active_parse_run_id IS NULL` 排除了所有已有活动索引的版本，又按 `sequence` 升序取首个。

**修法**：`repair_unpublished_runs` 不再自己写查询条件去猜，而是：

1. 列出所有存在终态 run 的文件版本；
2. 每个版本取 `sequence` 最大的终态 run 作为候选；
3. 调用 `_publish_active` 裁决——它本身就是发布规则：不比当前活动 run 新就拒绝，partial 不得顶替 complete。

这样「重解析后崩溃」和「首次发布后崩溃」用同一条路径修复，规则只有一处。

**证据**（`test_r03_publish_commit.py`，共 10 项）：

- `test_repair_publishes_newer_run_when_pointer_still_holds_the_old_one`：直接构造复审场景，补发 1 条，指针指向新 run，新内容可检索。
- `test_worker_start_repairs_stale_pointer`：同一场景走 `worker.run_worker`，证明是启动时修复而不是靠手工调用。
- `test_repair_keeps_a_newer_active_run_and_history`：已正确发布时补发 0 条，旧 `source_ref` 仍返回原文本。
- 原有 4 项（提交边界注入、正常 worker 恢复、旧现场补发、不覆盖更新版本）继续通过。

### P1-2 合法改名（`materials.py`）

**复审复现**：把 `合同.docx` 改显示名为 `合同.v2`，建副本得 `合同（工作副本）.v2`，改内容、登记后解析失败。

**上一轮的错误**：上一轮改成「扩展名取完整显示名自带的那一个」，方向是对的但用错了判据——`.v2` 在字符形式上确实像扩展名。这一轮改为**只有等于真实扩展名时才剥离**：

| 显示名 | 真实格式 | 结果 |
|---|---|---|
| `合同.docx` | DOCX | `合同（工作副本）.docx` |
| `合同.v2.docx` | DOCX | `合同.v2（工作副本）.docx` |
| `合同.v2` | DOCX | `合同.v2（工作副本）.docx` |
| `附件.txt` | DOCX | `附件.txt（工作副本）.docx` |
| `改名后` | DOCX | `改名后（工作副本）.docx` |

**证据**（22 项）：参数表新增 `合同.v2`、`附件.txt`（实际 DOCX）、`证据.pdf`（实际 XLSX）；`test_name_without_real_extension_full_flow` 走完建副本→改内容→登记→worker 解析→working 检索命中。

浏览器实测：上传 DOCX，改显示名为 `合同.v2`，点「创建工作副本」，返回路径为 `.../合同.v2（工作副本）.docx`。

### P1-3 并行登记（`materials.py`）

**复审复现**：两个独立连接使用相同 revision，都确认修改后的字节尚无 `StoredObject`，再同时发布。一次成功，另一次在写 `StoredObject` 时撞 `uq_stored_objects_case_sha`，并留下无引用的版本文件。

**上一轮的错误**：CAS 校验放在发布之后。虽然校验了返回值，但失败发生在更早的唯一约束上，冲突处理路径根本没走到。

**修法**：

1. `begin_write` 后**立刻**做 CAS（`update_with_revision`），失败即回滚并抛 `conflict`——此时还没有任何文件写入。
2. 之后的任何失败（含 `create_parse_run`、审计）都在 `except BaseException` 里回滚并删除已发布的版本文件。

**证据**（`test_register_concurrency.py` 4 项）：

- `test_two_truly_parallel_registrations`：两个线程用 `threading.Barrier` 同步起跑，各自先读 revision=0 再同时登记。断言恰好一个成功、一个 `CONFLICT`，且失败方**不是**唯一约束错误；最终 1 个版本、1 个解析任务、revision=1、零孤儿文件。
- 反向验证：把 CAS 移回发布之后，该测试得到 `IntegrityError: ... already exists`，与复审描述一致。

### P1-4 材料切换（`MaterialDetail.tsx`）

**复审复现两条**：

1. 甲的提取文本在乙原件到达前返回；乙原件到达后，甲正文显示在乙标题下，直到乙正文返回。
2. 甲点「创建工作副本」，切到乙，再让甲请求返回；乙详情中「登记为新版本」变为可点，所用 ID 仍是甲副本。

**修法**：

- 切换材料时同时递增 `originalGeneration`、`textGeneration`、`copyGeneration`，并把 `scopeMaterial` 记为新材料 ID。上一轮只递增了原件的代次，所以迟到的文本响应仍被接受。
- `makeCopy` / `registerCopy` / `reparse` 回包都校验「代次未变 且 材料未变」，否则直接丢弃；`busy` 也只在归属未变时复位。

**证据**（`MaterialSwitch.test.tsx` 5 项，其中 2 项为本轮新增）：

- 甲的正文在乙原件到达前返回 → 乙原件到达后，甲正文不得出现；随后乙正文正常显示。
- 甲的副本回包迟到 → 乙详情的「登记为新版本」保持禁用，且甲的副本路径不得出现。
- 反向验证：去掉 `textGeneration.current += 1` 后第一条失败（`expected <p> to be null`）；去掉副本归属校验后第二条失败。

### P2 上传去重（`imports.py`）

**复审复现**：同批上传字节相同的 `a.txt` 和 `a.md` 成功，库中只有一条 `StoredObject`，磁盘却多出无引用的 `source.md`；两连接同时上传同内容时一个撞唯一约束。

**修法**：

1. 先按 `staged.sha256` 查既有对象，命中则 `storage.discard(staged)`，**根本不发布**。
2. 未命中才发布；插入 `StoredObject` 包在 `session.begin_nested()` savepoint 里，撞唯一约束时删除刚发布的文件并复用胜者的行。

**证据**（`test_upload_dedupe.py` 6 项，其中 2 项为本轮新增）：

- `test_different_extensions_same_bytes_leave_no_orphan`：`a.txt` + `a.md` → 1 个对象、2 条材料、磁盘无无引用文件。
- `test_two_parallel_batches_of_identical_bytes_both_succeed`：两线程 `Barrier` 同步起跑的两个独立批次都成功，1 个对象、2 条材料、零孤儿文件。
- 反向验证：把「先查哈希」改回「先发布再查」，第一条得到 `无引用文件: [.../source.md]`，与复审描述一致。

### 文档 README

`init` 现在会迁移并前台启动服务，README 仍写「建库、然后停库」。已改为按当前行为描述，并说明因此要先装 OCR 模型、失败时会停掉本次启动的数据库。

---

## 三、本轮的反向验证清单

复审两次推翻「已修复」，共同原因是判据太松：测试存在，但没有真正触发被修的那条路径。本轮每条都做了还原验证：

| 修复 | 还原后应失败 | 实测 |
|---|---|---|
| CAS 提前 | `test_two_truly_parallel_registrations` | `IntegrityError: ... already exists` |
| 先查哈希再发布 | `test_different_extensions_same_bytes_leave_no_orphan` | `无引用文件: [.../source.md]` |
| 切换时作废文本代次 | 甲正文不得出现在乙标题下 | `expected <p> to be null` |
| 副本回包归属校验 | 甲副本不得启用乙的登记 | 1 failed |
| 空白页复制（上一轮） | 强制页后仍有两页 | `['pdf:2']` |

---

## 四、仍未验证

与上一轮相同，未新增：

| 项 | 说明 |
|---|---|
| Linux 平台 | 仅 macOS 27.0.1 |
| 真实案件材料 | 全部 `TEST-ONLY` 合成数据 |
| 5 万页以上 | 验收规模 1000 文件 / 10000 页 |
| 备份进程 `kill -9` | 演练用正常终止 |
| 加密 Office | 未测 |
| 多用户并发写 | 单用户设计，未测 |
| 真实浏览器限速 | 材料切换用受控 Promise；未在浏览器里人为限速 |
| 崩溃注入方式 | `RuntimeError` 模拟，不是真实 `SIGKILL` |

## 五、待用户提供

- 原有 22 条检索测试的样本与期望结果。
- 原审查六项目对应的仓库或 commit 清单。
