# V0.1 验收交接包（供主代理代码审查）

最新结果见 [修复验收与复审](V0.1_修复验收与复审_20261113.md)。下文保留第六轮及以前的交接记录。

日期：2026-11-12，2026-11-13 修订。用途：把验收证据交给主代理，供其开展代码审查。

> **2026-11-13 修订**：独立审查（`docs/V0.1_独立验收与代码审查_20261113.md`）推翻了本文档若干条「通过」结论，第二轮修复见 `docs/REMEDIATION_R2_20261113.md`；随后第二轮复审（`docs/V0.1_第二轮整改复审_20261113.md`）又推翻 5 项，第三轮修复见 `docs/REMEDIATION_R3_20261113.md`；第三轮复审（`docs/V0.1_第三轮整改复审_20261113.md`）再推翻 4 项，第四轮修复见 `docs/REMEDIATION_R4_20261113.md`；第四轮复审（`docs/V0.1_第四轮整改复审_20261113.md`）再推翻 3 项，第五轮修复见 `docs/REMEDIATION_R5_20261113.md`；第五轮复审（`docs/V0.1_第五轮整改复审_20261113.md`）再推翻 3 项，第六轮修复见 `docs/REMEDIATION_R6_20261113.md`。本文档下方「24 条全部通过」的表述是当时口径，已被取代；当前状态以第二节表格为准。

**责任边界**：验收（执行 + 留证）由接手 Agent 完成；代码审查由主代理在验收之后进行。本文件不含审查结论，只列证据与未验证项。

---

## 一、当前状态

| 项目 | 数值 |
|---|---|
| 后端测试 | **262 passed**（真实 PostgreSQL 18，无 SQLite） |
| 前端测试 | **23 passed**（vitest + jsdom） |
| 验收脚本 | `scripts/run_acceptance.py` 13 项判定全部通过 |
| 备份恢复演练 | `RESTORE_REHEARSAL_OK = True`，7 项 CHECKS 全 true |
| 一致性检查 | `ok = True issues = 0 orphans = 0` |
| 数据库 | PostgreSQL 18，端口 55432，数据目录 `var/postgres` |

复现命令：

```bash
cd backend && .venv/bin/python -m pytest -q          # 262
cd frontend && npx vitest run                        # 23
LC_ALL=en_US.UTF-8 LWB_PG_BIN=/opt/homebrew/opt/postgresql@18/bin \
  backend/.venv/bin/python scripts/run_acceptance.py # 13 项判定
LC_ALL=en_US.UTF-8 LWB_PG_BIN=/opt/homebrew/opt/postgresql@18/bin \
  backend/.venv/bin/python scripts/rehearse_backup_restore.py
```

---

## 二、当前验收状态（2026-11-13 修订后）

### 2.0 第五轮复审提出的 3 项（第六轮修复）

| 项 | 优先级 | 状态 | 证据 |
|---|---|---|---|
| 失败批次的清理删掉另一批已登记原件 | P1 | 已修 | `test_upload_dedupe.py` 第 10 项：乙登记的原件可校验、乙材料可解析、甲无残留 |
| 后续条目被拒时前序发布文件留在磁盘 | P2 | 已修 | `test_upload_dedupe.py` 第 11 项：返回 422、磁盘无文件、两表均空 |
| 切走后成功的改名不刷新列表 | P2 | 已修 | `MaterialSwitch.test.tsx` 第 8 项：乙不受污染、切回甲显示新名称 |

逐项修法与反向验证见 `docs/REMEDIATION_R6_20261113.md`。

### 2.1 第四轮复审提出的 3 项（第五轮修复）

| 项 | 优先级 | 状态 | 证据 |
|---|---|---|---|
| 原件校验失败提交半个批次、请求键不可重试 | P1 | 已修 | `test_upload_dedupe.py` 第 9 项：失败后批次与条目不变、标记仍在、无无引用文件、同请求键重试 200 |
| 两份副本并行登记同字节，一次失败且留无引用文件 | P2 | 已修 | `test_register_concurrency.py` 第 7 项：两版本共用一对象、两个解析任务、无无引用文件 |
| 甲的迟到保存响应改动乙详情 | P2 | 已修 | `MaterialSwitch.test.tsx` 第 7 项：乙不显示甲的提示、修订号与按钮状态不受影响 |

逐项修法与反向验证见 `docs/REMEDIATION_R5_20261113.md`。

### 2.2 第三轮复审提出的 4 项（第四轮修复）

| 项 | 优先级 | 状态 | 证据 |
|---|---|---|---|
| 并行上传删除共用原件 | P1 | 已修 | `test_upload_dedupe.py` 11 项，含「对象哈希可校验 + 两份材料均可解析」 |
| 登记失败已提交再删内容 | P1 | 已修 | `test_register_concurrency.py` 7 项，含审计注入与完整性失败 |
| 部分覆盖挡住应补发的完整索引 | P1 | 已修 | `test_r03_publish_commit.py` 12 项，含混合终态两种指针 |
| 切换材料后按钮一直禁用 | P2 | 已修 | `MaterialSwitch.test.tsx` 8 项 |

逐项修法与反向验证见 `docs/REMEDIATION_R4_20261113.md`。

### 2.3 第二轮复审提出的 5 项（第三轮修复）

| 项 | 优先级 | 状态 | 证据 |
|---|---|---|---|
| 旧索引补发漏重解析、可能选错版本 | P1 | 已修 | `test_r03_publish_commit.py` 10 项，含「指针仍指旧 run」与 worker 启动补发 |
| 合法改名把 DOCX 副本变成不可解析 | P1 | 已修 | `test_r02_working_copy_extension.py` 22 项，含 `合同.v2`、`附件.txt` |
| 并行登记失败方绕过冲突处理 | P1 | 已修 | `test_register_concurrency.py` 4 项，`Barrier` 真并行 |
| 切换材料仍显示上一份正文和副本 | P1 | 已修 | `MaterialSwitch.test.tsx` 5 项，两条受控时序 |
| 上传去重留孤儿、并行撞唯一约束 | P2 | 已修 | `test_upload_dedupe.py` 6 项，含异扩展名与并行批次 |
| README 与 `init` 行为不符 | — | 已改 | README 安装节 |

逐项修法与反向验证见 `docs/REMEDIATION_R3_20261113.md`。

### 2.3 第一轮独立审查提出的 11 项

| 项 | 优先级 | 状态 | 证据位置 |
|---|---|---|---|
| R03 终态与发布两次提交 | P1 | 已修 | `test_r03_publish_commit.py`（7 项） |
| R02 多点文件名 | P1 | 已修 | `test_r02_working_copy_extension.py`（17 项） |
| 同修订号并发登记 | P1 | 已修 | `test_register_concurrency.py`（3 项） |
| 相同字节上传中止批次 | P1 | 已修 | `test_upload_dedupe.py`（4 项） |
| 空目录初始化失败 | P1 | 已修 | 空目录实跑，见 REMEDIATION_R2 第五节 |
| 材料切换原件错配 | P1 | 已修 | `MaterialSwitch.test.tsx`（3 项） |
| 测试未绑定测试库 | P1 | 已修 | 空默认库实跑 43 项通过、库仍为 0 表 |
| R04 指定页丢页 | P2 | 已修 | `test_r04_skip_before_ocr.py` 第 5 项 |
| OCR 超时分类 | P2 | 已修 | `test_parsing_pdf_ocr.py`（10 项） |
| T16/T17 网页缺口 | P2 | 已修 | `UploadPanel.test.tsx`、`SearchDeepLink.test.tsx` |
| 备份清单 schema 漂移 | P3 | 已修 | `test_backup_consistency.py` 第 24 项 |

### 2.4 原任务单 24 条

| 编号 | 验收条数 | 已补完 | 说明 |
|---|---|---|---|
| R01 | 4 | 4 | 本轮独立审查复核通过 |
| R02 | 4 | 4 | 第 2 条「多点文件名」在第二轮才真正修好 |
| R03 | 4 | 4 | 第 1 条「实际提交边界注入」在第二轮才成立 |
| R04 | 3 | 3 | 第 3 条对应 P2 丢页问题，已修 |
| R05 | 4 | 4 | 浏览器实操证据保留在 REMEDIATION_R01_R06 第六节 |
| R06 | 4 | 4 | 第 4 条改为 URL 参数 + 刷新定位，已浏览器实测 |

逐条证据见 `docs/REMEDIATION_R01_R06.md` 第六节与 `docs/REMEDIATION_R2_20261113.md`。

---

## 三、任务单之外发现并修复的缺陷

审查时请特别关注这三处，它们不在原任务单里，是验收过程中实操发现的。

### D1 编号材料分页顺序错乱（P1）

- **文件**：`backend/src/legal_workbench/materials.py` `list_materials`
- **现象**：同批导入的材料共享 `created_at`（PostgreSQL `now()` 事务级），次级键是随机 UUID，列表顺序随机；叠加纯字典序后「材料101」排在「材料10」前面，用户按「第 51 份」找不到对应材料。
- **修法**：次级键改为 `char_length(display_name), display_name, id`。
- **请审查**：这是启发式排序，不同前缀且长度不同的名字仍按长度优先。是否接受这个折衷，还是要求持久化补零排序键做真自然排序？
- **测试**：`test_numbered_material_names_sort_naturally`

### D2 TXT 原件预览返回 422（P1）

- **文件**：`backend/src/legal_workbench/api/files.py`
- **现象**：点「原件预览」，iframe 显示 JSON 错误 `unsupported_format`。预览只认图片和已解析文本，TXT 原件在解析前无法预览。
- **修法**：新增 `INLINE_TEXT_SUFFIXES`，直接返回存储字节，`text/plain; charset=utf-8` + `nosniff` + `CSP default-src 'none'`，2 MB 上限，非 UTF-8 回退 `gb18030`。
- **请审查**：直接回显未解析的用户文件是否有其他风险；2 MB 阈值是否合适；是否应改为始终走「提取文本」路径。
- **测试**：`test_txt_original_previews_without_being_parsed`、`test_preview_serves_text_even_before_any_parse_run`

### D3 导入完成后材料列表不刷新（P1）

- **文件**：`frontend/src/components/UploadPanel.tsx`
- **现象**：`onUploaded` 在 upload POST 返回时就触发，那一刻 worker 未产出材料，父页面拿到空列表。面板显示「已完成（3/3）· 就绪 3」，材料区仍「全部（0）」，用户以为上传失败。
- **修法**：`onUploaded` 移到任务终态触发，`notifiedJob` ref 防重复；新增 `useEffect` 轮询，未终态时每 1.5s 刷新。
- **请审查**：1.5s 轮询是否合适（是否应改为服务端推送或更长间隔）；`notifiedJob` 在组件卸载/重新挂载时的行为。
- **测试**：`UploadPanel.test.tsx` 3 项；已反向验证去掉轮询后有 2 项失败。

---

## 四、未验证事项（不影响交付，但审查时需知情）

### 4.1 环境与规模

| 项 | 说明 |
|---|---|
| Linux 平台 | 仅在 macOS 27.0.1 验证 |
| 真实案件材料 | 全部使用 `TEST-ONLY` 合成数据 |
| 5 万页以上规模 | 验收用 1000 文件 / 10000 页 |
| 备份进程 `kill -9` | 演练用正常终止，未测进程被强杀的完整恢复 |
| 加密 Office 文件 | 未测 |
| 多用户并发写 | 单用户设计，未测 |

### 4.2 方法学限制

- R03 的崩溃注入用 `RuntimeError` 模拟，不是真实 `SIGKILL`；三个边界（`units_done`/`before_finish`/`after_finish`）均覆盖。
- R06 未在真实浏览器里人为限速复现 A→B→C 乱序响应，该场景由组件测试的受控 Promise 覆盖。

### 4.3 待用户提供资料

- 原有 22 条检索测试的样本文件与期望结果——缺这两样无法判定该组测试是否应通过。
- 原审查六项目对应的仓库或 commit 清单——缺清单无法比对「源码复用审查」结论。

---

## 五、交付物清单

### 文档

- `docs/REMEDIATION_R01_R06.md` — R01–R06 修复证据 + 第六节验收补全
- `docs/REMEDIATION_R2_20261113.md` — 独立审查 11 项的第二轮修复证据
- `docs/REMEDIATION_R3_20261113.md` — 第二轮复审 5 项 + 文档偏差的第三轮修复证据
- `docs/REMEDIATION_R4_20261113.md` — 第三轮复审 4 项的第四轮修复证据
- `docs/REMEDIATION_R5_20261113.md` — 第四轮复审 1 P1 + 2 P2 的第五轮修复证据
- `docs/REMEDIATION_R6_20261113.md` — 第五轮复审 1 P1 + 2 P2 的第六轮修复证据
- `docs/TEST_REPORT.md` — 291 项测试明细与复现命令
- `docs/COMPLETION_REVIEW.md` — 完成度核对
- `docs/SCHEMA.md`、`docs/API.md`、`docs/ARCHITECTURE.md` — 契约与架构
- `docs/BACKUP_RESTORE.md` — 备份恢复
- `docs/reports/T19-evidence.md` — 验收脚本输出（本轮重跑）
- `docs/openapi.json` — 契约

### 新增测试

| 文件 | 项数 | 覆盖 |
|---|---|---|
| `backend/tests/test_r01_result_warnings.py` | 5 | 结果级 warnings 传递与持久化 |
| `backend/tests/test_r02_working_copy_extension.py` | 17 | 扩展名、多点文件名、完整流程 |
| `backend/tests/test_r03_publish_atomicity.py` | 9 | 三边界注入、恢复后可检索 |
| `backend/tests/test_r03_publish_commit.py` | 7 | 真实提交边界、worker 补发、历史引用 |
| `backend/tests/test_register_concurrency.py` | 3 | 并发登记只成功一次、无孤儿文件 |
| `backend/tests/test_upload_dedupe.py` | 4 | 相同字节复用对象、材料与分类独立 |
| `backend/tests/test_r04_skip_before_ocr.py` | 5 | OCR 前跳过、被跳过页异常、丢页 |
| `frontend/src/components/SearchPanel.test.tsx` | 3 | 来源切换同源 |
| `frontend/src/components/SearchDeepLink.test.tsx` | 3 | `?ref=` 写入、直达、复制链接 |
| `frontend/src/components/UploadPanel.test.tsx` | 6 | 终态通知、轮询、续跟踪、计数、重试 |
| `frontend/src/pages/CaseWorkspacePage.test.tsx` | 5 | 分页、分类导航、`?ref=` 直达 |
| `frontend/src/pages/MaterialSwitch.test.tsx` | 3 | 切换材料不串原件与正文 |

### 脚本

- `scripts/run_acceptance.py` — 13 项验收判定
- `scripts/rehearse_backup_restore.py` — 备份恢复演练
- `scripts/seed_pagination_case.py` — 120 份分页数据（含真实原件）
- `scripts/install_ocr_models.py` — OCR 模型安装与哈希清单
- `scripts/lwb` — 启动 / 停止 / 迁移 / 诊断

---

## 六、审查建议顺序

1. 先跑一遍 `pytest -q` 与 `vitest run`，确认 266 + 25 通过。
2. 读 `docs/REMEDIATION_R01_R06.md` 第六节，对照 24 条验收逐条核。
3. 重点看第三节三处任务单之外的缺陷（D1/D2/D3），判断修法是否可接受。
4. 检查第四节未验证项是否在可接受范围内。
5. 如需扩大到 Linux 或真实材料，另行安排环境。

---

## 七、环境注意事项

- 项目**不是 git 仓库**，无版本控制兜底，改动前请自行备份。
- 数据库启动必须带 `LC_ALL=en_US.UTF-8`，否则 postmaster 多线程中止。
- 测试会 `DROP DATABASE legal_workbench_test WITH (FORCE)`，确认没有其他 Agent 在用。
- 服务只绑 `127.0.0.1`，认证为本地访问密钥（`.env` 的 `LWB_ACCESS_KEY`）。
- 凭据不入仓库：`backend/alembic.ini` 的 `sqlalchemy.url` 留空，由 `LWB_MIGRATION_URL` 注入。
