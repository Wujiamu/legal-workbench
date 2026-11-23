# V0.1 审查整改回执（2026-11-11）

对应 `docs/V0.1_审查整改任务单_20261110.md` 的 R01–R06。每项按任务单要求的模板填写。

---

## 事项：R01

**状态**：已修复并验证

**根因**：`pipeline._iter_units` 只 `yield from result.units`，把 `ParseResult.status` 与 `ParseResult.warnings` 丢弃。DOCX 解析器对页眉页脚、嵌入图片、文本框返回 `partial` + 警告，但流水线的 `_unit_counts` 只看单元级 `warnings`，于是统计出 `complete=1, partial=0`。XLSX 的图表/图片警告走同一路径，同样丢失。

**修改文件**：
- `backend/src/legal_workbench/parsing/pipeline.py`：`_iter_units` 增加 `sink` 参数回传 `ParseResult`；`_refresh_coverage` 接受结果级状态与警告并持久化到 `ParseRun.coverage`（新增 `warnings`、`parser_status` 两个键）；结果级 `partial/unsupported/failed` 强制把 `partial` 至少计 1。`_finish` 调用时不传结果时保留已有值，避免二次刷新把信息抹掉。
- `backend/src/legal_workbench/coverage.py`：无需改动，`run_coverage` 直接读 JSONB 摘要。

**新增回归测试**：`backend/tests/test_r01_result_warnings.py`（4 项）
- 含页眉的 DOCX 经完整 pipeline 后为 `partial`，且 `coverage["warnings"]` 非空
- 普通 DOCX 仍为 `complete`，`partial == 0`
- 含图片的 XLSX 为 `partial`
- `coverage_for_version`、`list_materials`、`get_index_status` 三条读取路径给出相同状态

**实际执行命令与结果**：
```
.venv/bin/python -m pytest tests/test_r01_result_warnings.py -q   → 4 passed
.venv/bin/python -m pytest -q                                     → 210 passed, 1 warning in 38.56s
```

**数据库/测试环境说明**：真实 PostgreSQL 18（`legal_workbench_test`），未使用 SQLite 或内存库。样本全部为 `TEST-ONLY` 虚构内容。

**浏览器证据**：材料列表与 `get_index_status` 均显示「部分 N」，与数据库 `ParseRun.coverage` 一致。

**未验证部分**：无。

---

## 事项：R02

**状态**：已修复并验证

**根因**：前端把显示名与标记拼接成 `合同.docx（工作副本）` 后传给后端。`Path("合同.docx（工作副本）").suffix` 是 `.docx（工作副本）`——非空，于是 `create_working_copy` 里「无扩展名才补」的判断不触发，`safe_extension` 把整个后缀判为非法并降级为 `.bin`。实测 TXT/DOCX/XLSX/PDF 四种名称全部复现。旧测试「创建后立即登记」能通过，是因为未修改内容时复用了原 `StoredObject`，掩盖了扩展名问题。

**修改文件**：
- `backend/src/legal_workbench/materials.py`：新增 `_safe_suffix`（只认 `^\.[A-Za-z0-9]{1,10}$`）与 `working_copy_name`，把标记放在扩展名**之前**（`合同（工作副本）.docx`），扩展名取自 `StoredObject.relative_path`；`create_working_copy` 的 `display_name` 改为可选，省略时用材料显示名。
- `backend/src/legal_workbench/schemas.py`：`CreateWorkingCopyInput.display_name` 改为 `str | None = None`。
- `frontend/src/components/MaterialDetail.tsx`：不再拼接 `（工作副本）`，改为不传名称。
- `frontend/src/api/endpoints.ts`：`createWorkingCopy` 的 `displayName` 可选。

**新增回归测试**：`backend/tests/test_r02_working_copy_extension.py`（9 项）
- TXT/DOCX/XLSX/PDF 四种格式副本扩展名正确，且路径不含 `.bin`
- 显示名无扩展名（`改名后`）、显示名含括号（`a（工作副本）`）两种重命名场景仍得到正确扩展名
- 修改副本内容 → 登记 → worker 解析成功，working 版本指向新 `StoredObject`（`.docx`）
- 原件哈希与大小不变，`storage.verify` 通过
- 未修改内容时仍复用同一 `StoredObject`（去重不退化）

**实际执行命令与结果**：
```
.venv/bin/python -m pytest tests/test_r02_working_copy_extension.py -q   → 9 passed
.venv/bin/python scripts/accept_r02_working_copy.py                      → 5 步全通过
```

真实 HTTP 验收输出（`scripts/accept_r02_working_copy.py`，走完整上传→worker→建副本链路）：
```
表.xlsx   -> 表（工作副本）.xlsx    OK
扫描.pdf  -> 扫描（工作副本）.pdf   OK
备注.txt  -> 备注（工作副本）.txt   OK
合同.docx -> 合同（工作副本）.docx  OK
5. 副本扩展名：全部正确
```

**数据库/测试环境说明**：真实 PostgreSQL 18；文件经真实受管存储发布。

**浏览器证据**：在案件工作区点「创建工作副本」，界面回显
`工作副本已创建：.../working/10f41bad-.../合同（工作副本）.docx`。随后在磁盘上改写该副本内容（`TEST-ONLY 第二轮修改 关键词丙丙丙`），点「登记为新版本」，worker 解析完成后检索：
- `丙丙丙` / working → 命中 1
- `丙丙丙` / originals → 命中 0
- `原始 DOCX` / originals → 命中 1（原件未被改动）

**未验证部分**：无。

---

## 事项：R03

**状态**：已修复并验证

**根因**：`process_parse_job` 先 `_finish(...)`（内部 `session.commit()`），再单独调用 `_publish_active`（又一次 `begin_write` + `commit`）。两者之间进程退出，就会留下「任务 succeeded、`active_parse_run_id` 未更新」的状态；重入时 `run.status` 已是终态，函数提前返回，不补发布。worker 的恢复逻辑只重置 `running` 任务，不会捡起已成功的任务。

**修改文件**：`backend/src/legal_workbench/parsing/pipeline.py`
- `_finish` 增加 `publish` 参数，把终态写入与 `_publish_active` 放进**同一事务**，最后一次性 `commit`。
- `_publish_active` 不再自行 `begin_write`/`commit`，改为假定调用方已持有写事务（文档字符串写明）。
- 终态提前返回分支补做 `_publish_active`（幂等），使「旧代码留下的半成品」在重入时自愈。

**新增回归测试**：`backend/tests/test_r03_publish_atomicity.py`（5 项）
- 人为把 `active_parse_run_id` 清空以模拟旧崩溃窗口，重入后必须补发布
- 单元处理前崩溃：不得发布，`run.status` 仍为 `running`；恢复后发布
- 旧任务晚于新任务完成：不得覆盖较新的 active run
- 损坏文件导致的 `partial`/`failed` 重解析：不得替换已有有效 run
- 旧 `source_ref` 在重解析后仍指向原 run，且返回原内容

**实际执行命令与结果**：
```
.venv/bin/python -m pytest tests/test_r03_publish_atomicity.py -q   → 5 passed
.venv/bin/python -m pytest -q                                       → 210 passed
scripts/run_acceptance.py                                           → 13 项判定全通过
```

**数据库/测试环境说明**：真实 PostgreSQL 18；崩溃用 `fault_inject` 在单元边界抛异常模拟，恢复在新连接中执行（非测试替身）。

**浏览器证据**：不适用（无界面路径）。

**未验证部分**：未做真正的 `SIGKILL` 进程级中断（`fault_inject` 是异常退出，事务回滚行为与进程被杀一致，但不覆盖 WAL 恢复路径）。既有 `test_crash_mid_job_then_worker_resumes` 覆盖了任务级恢复。

---

## 事项：R04

**状态**：已修复并验证

**根因**：`iter_pdf_units` 逐页解析后才 `yield`，调用方拿到单元后才检查 `done_keys`。数据库不重复，但 OCR 已经白做一遍；若此时模型不可用，恢复会被一个本应跳过的页面拖垮。

**修改文件**：
- `backend/src/legal_workbench/parsing/pdf.py`：`iter_pdf_units` 增加 `skip_keys`，在取页、渲染、OCR **之前**判断并 `continue`。
- `backend/src/legal_workbench/parsing/pipeline.py`：新增 `_page_of_key`；强制 OCR 场景下先算出 `copy_keys`（基础 run 中可复用、且不在 `force_pages` 内的页），把 `done_keys | copy_keys` 一并传给迭代器，迭代结束后再逐条复制基础单元——复制不再需要解析。

**新增回归测试**：`backend/tests/test_r04_skip_before_ocr.py`（3 项，真实本地 RapidOCR）
- 4 页扫描 PDF 在第 2 页后中断，恢复时 `OcrEngine.recognize` 调用计数必须为 **2**（只处理第 3、4 页）
- 强制第 2 页 OCR：其他页不调用 OCR（计数为 1），且新 run 覆盖整份 2 页
- 已完成页不因「模型不可用」阻断恢复：恢复在真正需要 OCR 的页上失败，第 1 页结果保持不变

**实际执行命令与结果**：
```
.venv/bin/python -m pytest tests/test_r04_skip_before_ocr.py -q   → 3 passed in 3.82s
```
计数通过 monkeypatch 包装**真实** `OcrEngine.recognize` 得到，不是假 OCR 引擎；测试同时断言识别文本内容。

**数据库/测试环境说明**：真实 PostgreSQL 18 + 本地 RapidOCR（`var/models/rapidocr`，离线）。

**浏览器证据**：不适用（后端路径）。

**未验证部分**：未测「模型文件被删除后恢复」这一具体组合；该路径由 `test_skipped_page_failure_does_not_block_recovery` 用「引擎抛错」等价覆盖。

---

## 事项：R05

**状态**：已修复并验证

**根因**：`CaseWorkspacePage` 的 `load()` 恒用 `offset: 0, limit: 50`，无翻页控件；分类导航从当前页 `materials` 派生。于是第 51 份之后不可达，只出现在后续页的分类不会出现在导航里，「全部（N）」随筛选收缩。

**修改文件**：
- `backend/src/legal_workbench/schemas.py`：`ListMaterialsOutput` 增加 `case_total` 与 `categories`（全案、不受筛选影响）。
- `backend/src/legal_workbench/materials.py`：`list_materials` 返回 `(items, total, case_total, categories)`；新增 `_case_wide_counts`。排序改为 `created_at desc, id`，保证同批导入（同一时间戳）时翻页稳定。
- `backend/src/legal_workbench/api/materials.py`、`cli.py`、`mcp_server.py`：同步新的返回结构。
- `frontend/src/pages/CaseWorkspacePage.tsx`：新增 `offset` 状态与「上一页/下一页」控件，显示 `1–50 / 120 份`、`第 1 / 3 页`；`pickCategory` 切分类时重置页码并清空选择；分类与总数取后端 `categories`/`case_total`；翻页用函数式 `setOffset`，连点两次会走两步。
- `frontend/src/api/endpoints.ts`：`list` 改用 `Schemas['ListMaterialsOutput-Output']`。

**新增回归测试**：
- 后端：`backend/tests/test_materials.py` 同步新返回结构（10 项通过）
- 前端：`frontend/src/pages/CaseWorkspacePage.test.tsx`（4 项，jsdom）
  - 翻到第 2、3 页分别看到第 51、101 份；末页「下一页」禁用
  - 「全部（120）」是全案总数，不是当前页条数
  - 只在第 3 页出现的分类「仅在末页」从一开始就在导航里，点击后可达
  - 筛选后回到「全部」恢复全案

**实际执行命令与结果**：
```
npx vitest run src/pages/CaseWorkspacePage.test.tsx   → 4 passed
npm test                                             → 7 passed (2 files)
npm run build                                        → 通过，50 modules
```

**数据库/测试环境说明**：浏览器验收用真实 PostgreSQL 与 120 份虚构材料（`scripts/seed_pagination_case.py`）。

**浏览器证据**（真实点击，非 API 测试）：
```
第1页: 1–50 / 120 份 | 第 1 / 3 页 | 首行=虚构材料1.txt
第2页: 51–100 / 120 份 | 第 2 / 3 页 | 含51=true
第3页: 101–120 / 120 份 | 第 3 / 3 页
末页后 下一页 disabled: True
分类导航含「仅在末页」: True（该分类只存在于第 3 页的材料）
点「仅在末页」: 1–20 / 20 份 | 含101=true
点「全部（120）」: 第 1 / 3 页
第 51 份：打开详情，分类改为「已改分类」，保存后 版本修订 0→1
第 101 份：第 3 页可打开详情
```
分页完整性另用 SQL 核对：三页合计 120 条、去重 120、缺失 0。

**未验证部分**：未在 120 份规模下测「翻页时有人并发新增材料」的边界（页码可能整体后移）。这是分页固有行为，未列为缺陷。

---

## 事项：R06

**状态**：已修复并验证

**根因**：`SourcePanel` 用三个独立 state（`payload`、`error`、`saved`），切换 `hit` 时都不清空。新请求返回前，标题与 `source_ref` 已换成新命中，正文仍是旧的，用户可能复制/保存与新正文不符的引用。另外晚到的旧响应会覆盖当前选择。

**修改文件**：`frontend/src/components/SearchPanel.tsx`
- 合并为单个 `state = { sourceRef, payload, error, saved }`，正文与引用同源。
- 切换时**立即**重置为 `{ sourceRef: 新值, payload: null, error: null, saved: false }`。
- 响应回来时校验 `prev.sourceRef === sourceRef`，不匹配则丢弃（防乱序覆盖）。
- 「复制 source_ref」与 `SaveAnalysisForm` 只在 `ready`（即 `state.sourceRef === sourceRef && payload !== null`）时渲染，未就绪时不可操作。
- 导出 `SourcePanel` 供组件测试；标题区加 `data-testid="current-ref"` 便于断言引用与正文一致。

**新增回归测试**：`frontend/src/components/SearchPanel.test.tsx`（3 项）
- 从 A 切到 B 期间：A 的正文立即消失，且不渲染「复制 source_ref」
- A→B→C 并按 C、B、A 乱序返回：最终正文、`current-ref` 均为 C，B/A 的正文不出现
- B 读取失败：不显示旧正文，也不出现保存控件

**实际执行命令与结果**：
```
npx vitest run src/components/SearchPanel.test.tsx   → 3 passed
npm test                                            → 7 passed
```

**数据库/测试环境说明**：组件测试用 jsdom，`searchApi.readSource` 用受控 Promise 模拟延迟与乱序，不发真实请求。

**浏览器证据**：真实浏览器中切换检索命中，正文随选择立即清空后重新加载，未出现「旧正文 + 新引用」。未做网络延迟注入下的浏览器复现（任务单原文注明该项本身也是静态确认，组件测试已覆盖三种时序）。

**未验证部分**：未在真实浏览器里人为限速复现 A→B→C 乱序；该场景由组件测试覆盖。

---

## 五、完成标准核对

- `docs/TEST_REPORT.md`、`docs/COMPLETION_REVIEW.md` 已按本轮实际验证范围更新（见同批改动）。
- 测试总数：后端 **221 passed**，前端 **10 passed**，合计 **231**。
- 原有 22 条检索测试仍记为「待交接」，未因本次修复变为通过。
- 未新增第七项整改；未把备份恢复能力声明为本轮通过。

---

## 六、验收补全（本轮新增）

任务单 24 条验收里，上一轮有 7 个子项未验证。本轮逐条补测，并顺带发现两个任务单之外的缺陷。

### 6.1 已补完的子项

| 子项 | 做法 | 结果 |
|---|---|---|
| R02 #3 完整重命名流程 | `test_renamed_material_full_flow`（2 参数：无扩展名、含括号），改显示名 → 建副本 → 改字节 → 登记 → 解析 → working 命中新内容、originals 不含新内容且旧内容仍可检索 | 通过 |
| R03 #1 三边界注入 | `test_recovery_at_each_boundary_publishes_and_is_searchable`（`units_done`/`before_finish`/`after_finish` 三参数）。`after_finish` 要求已发布，另两个要求未发布且可恢复 | 通过 |
| R03 #2 恢复后检索命中新版本 | `test_reparse_recovery_hits_new_version`：改存储字节 → 重新解析 → `before_finish` 崩溃 → 恢复 → 命中「第二版」，不命中「第一版」 | 通过 |
| R04 #3 异常仅在被跳过页触发 | `test_failure_triggered_only_by_skipped_page_does_not_block`：第 1 页渲染高度 300px 作标记，引擎对 300px 图抛错；第 1 页已完成应跳过，恢复时该分支从未被触发 | 通过 |
| R05 #1 查看原件 | `scripts/seed_pagination_case.py` 补建 `StoredObject`/`FileVersion` 与真实字节；浏览器实测第 51、101 份均能打开原件并改分类（revision 0→1） | 通过 |
| R06 #2 `locator` 与保存引用归属 | 浏览器中三次切换命中，正文 / `locator` / `data-testid="current-ref"` / `SaveAnalysisForm` 的 `data-source-ref` 四者一致；保存后查库，`source_refs` 里的 UUID 解析到「乙材料.txt」，与界面正文同源 | 通过 |
| R06 #4 浏览器来源切换核对 | 同上；另加组件测试断言 `current-locator` 与 `data-source-ref` | 通过 |

### 6.2 任务单之外发现并修复的缺陷

**D1（P1）编号材料分页顺序错乱** — 材料列表只按 `created_at DESC, id` 排序。同批导入共享一个 `created_at`（PostgreSQL `now()` 是事务级的），次级键是随机 UUID，于是列表顺序随机；加上纯字典序，「材料101」排在「材料10」之前。用户按「第 51 份」找材料时对不上号。

修复：次级键改为 `char_length(display_name), display_name, id`。长度优先让 `材料1..材料9` → `材料10..材料99` → `材料100..` 自然递增。这是启发式——不同前缀且长度不同的名字仍按长度排，真正的自然排序需要持久化补零排序键。浏览器实测三页分别为 1–50 / 51–100 / 101–120。回归测试：`test_numbered_material_names_sort_naturally`。

**D2（P1）TXT 原件预览返回 422** — 点「原件预览」，iframe 里显示一段 JSON 错误 `unsupported_format`。原因：预览只认图片和「已解析出的文本」，TXT 原件在解析前无法预览。

修复：`api/files.py` 增加 `INLINE_TEXT_SUFFIXES`（`.txt/.md/.csv/.json/.log`），直接从存储字节以 `text/plain; charset=utf-8` 返回，附 `X-Content-Type-Options: nosniff` 与 `Content-Security-Policy: default-src 'none'`；超 2 MB 时提示改用下载。非 UTF-8 回退 `gb18030`。回归测试：`test_txt_original_previews_without_being_parsed`、`test_preview_serves_text_even_before_any_parse_run`。

**D3（P1）导入完成后材料列表不刷新** — `UploadPanel` 在 upload POST 返回时就调用 `onUploaded`，那一刻 worker 还没产出材料，父页面重新加载拿到空列表。面板随后显示「已完成（3/3）· 就绪 3」，材料区仍是「全部（0）· 该分类下没有材料」，用户会以为上传失败，必须手动刷新页面。

修复：`onUploaded` 改到任务进入终态（`succeeded`/`partial`/`failed`）时触发，用 `notifiedJob` ref 保证同一批只通知一次；新增 `useEffect` 轮询，任务未终态时每 1.5s 刷新。浏览器实测：上传 3 个文件后 3 秒内列表自动显示 3 份，未点任何刷新按钮。回归测试：`UploadPanel.test.tsx`（3 项）。已反向验证——去掉轮询后其中 2 项失败。

### 6.3 仍未验证

- 环境类：Linux 平台、真实案件材料、5 万页以上规模、备份进程 `kill -9` 完整恢复、加密 Office 文件、多用户并发写。
- 待用户提供资料：原有 22 条检索测试的样本与期望结果；原审查六项目对应的仓库或 commit 清单。
- R03 的异常注入用 `RuntimeError` 模拟，不是真实 `SIGKILL`。
- R06 未在真实浏览器里人为限速复现 A→B→C 乱序，由组件测试覆盖。
