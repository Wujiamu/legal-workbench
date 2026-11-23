# V0.1 完成审核

逐项对照 `V0.1_实施任务包.md` 第 5 节。分四类标注：**已实现并验证** / **已实现待验证** / **未实现** / **待交接**。只有前两类且有证据的才计为通过。

## 第 5 节审核门

| 审核门 | 证据 | 结论 |
|---|---|---|
| 数据规则 | `test_db.py`(11)、`test_storage.py`(11)、`test_imports.py`(11)、`test_materials.py`(10)、`test_worker.py`(7)；T19 故障注入 | **已实现并验证** |
| 解析与定位 | `test_parsing_text_docx_xlsx.py`(9)、`test_parsing_pdf_ocr.py`(8)、`test_parsing_pipeline.py`(10)、`test_extracted_text.py`(4)、`test_coverage.py`(3)；T19 历史引用 | **已实现并验证** |
| 检索准确性 | `test_search.py`(15)、`test_search_offsets.py`(4)；T19 实测（100 vs 1000、CNY vs USD、1万元） | **已实现并验证**（本项目测试；原 22 条另计） |
| 权限边界 | `test_api.py`(21)：Host/Origin/CSRF/access key、路径穿越、跨案；T19 跨案读取被拒 | **已实现并验证** |
| 外部 Agent | `test_cli_mcp.py`(9)，含真实 `mcp.ClientSession` + `stdio_client` 往返 | **已实现并验证** |
| 网页闭环 | T15/T16/T17 浏览器交互验收；T20 `smoke_delivery.py` 真实 HTTP 全链路；T19 §10b 网页与 Agent 并发 | **已实现并验证** |
| 恢复能力 | `test_backup_consistency.py`(23)；`scripts/rehearse_backup_restore.py` 真实 pg_dump/pg_restore；T19 空环境恢复后重新检索命中 | **已实现并验证** |
| 性能 | `docs/reports/T19-evidence.md`：1000 文件 / 10000 页，检索 P95 ≤ 2s | **已实现并验证**（限实测规模） |
| 旧 22 条测试 | 无 | **待交接** |

## 任务包 T01–T20 逐包状态

| 任务包 | 内容 | 状态 | 证据 |
|---|---|---|---|
| T01 | 环境骨架 | 已实现并验证 | `test_config.py`、`doctor` 7 项（含 MAINTENANCE） |
| T02 | 协议契约 | 已实现并验证 | `test_protocol.py`(11)、`docs/openapi.json` |
| T03 | 数据库与迁移 | 已实现并验证 | `test_db.py`(11)、三角色权限实测、迁移 `0001`–`0003` |
| T04 | 受管存储 | 已实现并验证 | `test_storage.py`(11) |
| T05 | 批量导入 | 已实现并验证 | `test_imports.py`(11) |
| T06 | 材料分类与版本 | 已实现并验证 | `test_materials.py`(10)、`test_working_copy_versions.py`(2) |
| T07 | 单 worker | 已实现并验证 | `test_worker.py`(7) |
| T08 | TXT/DOCX/XLSX | 已实现并验证 | `test_parsing_text_docx_xlsx.py`(9) |
| T09 | PDF/图片/本地 OCR | 已实现并验证 | `test_parsing_pdf_ocr.py`(8)、T19 OCR 实测含内存与进程终止 |
| T10 | 解析版本与索引发布 | 已实现并验证 | `test_parsing_pipeline.py`(10) |
| T11 | PostgreSQL 检索与精确值 | 已实现并验证 | `test_search.py`(15)、`test_search_offsets.py`(4) |
| T12 | 带来源的分析成果 | 已实现并验证 | `test_analysis.py`(7) |
| T13 | HTTP API 与本地认证 | 已实现并验证 | `test_api.py`(21)；无云端组件（进程内会话、loopback、stdio MCP） |
| T14 | CLI/MCP | 已实现并验证 | `test_cli_mcp.py`(9) |
| T15 | 前端基础 | 已实现并验证 | `npm run build`；浏览器验收 |
| T16 | 材料工作区 | 已实现并验证 | 浏览器验收；`test_extracted_text.py`(4) |
| T17 | 检索与分析页 | 已实现并验证 | 浏览器验收（命中高亮、金额区分、分析审核） |
| T18 | 一致性/备份/恢复 | 已实现并验证 | `test_backup_consistency.py`(23，含覆盖一致性)、恢复演练、doctor 维护提示 |
| T19 | 端到端与性能验收 | 已实现并验证 | `docs/reports/T19-evidence.md` 判定全通过 |
| T20 | 文档与交付 | 已实现并验证 | 本文件 + 文档实跑核对 + `smoke_delivery.py` |

## 审计记录（AuditEvent）

计划第 2.4 节列出了 `AuditEvent` 实体但未规定哪些操作留痕。按用户决定实现**最小写入**：

- 15 个闭集操作名，由数据库 `CHECK` 约束（迁移 `0003`）与 Python 侧双重校验。
- 记录：登录/登出、建案、导入（服务器路径与浏览器上传）、分类、改名、工作副本、版本登记、分析保存/审核、备份、恢复、解除维护。
- 只存 `case_id / actor / operation / target_type / target_id / request_id / created_at`，**不存**正文、密钥、连接串。`test_audit.py` 有专门断言。
- 审计写入失败只记日志、不阻断业务。

## 未实现（V0.1 明确不做）

- 多用户与权限分级、云端同步、容器化、开机自启、向量检索、内置聊天、自动法律分析器、云端 OCR。
- 备份自动定时（只提供手动命令与演练脚本）。
- 审计事件的查询界面（数据已写入，未做 UI/CLI 查询命令）。

## 已实现待验证

| 项目 | 缺什么 |
|---|---|
| Linux 平台 | 未在 Linux 上运行过 |
| 干净机器从零安装 | README 命令逐条核对过，未做"新机器"完整演练 |
| 5 万页以上规模 | 只测到 1000 文件 / 10000 页 |
| 备份进程 `kill -9` | 只验证 `INCOMPLETE` 存在与拒绝恢复 |
| 加密 Office 文件 | 未构造样本断言错误码 |
| `.gitignore` 实际效果 | 当前目录不是 git 仓库，未做 `git init` 验证 |

## 待交接（不得记为通过）

| 项目 | 需要什么 |
|---|---|
| 原有 22 条检索测试 | 原样本与期望结果文件 |
| 原审查六项目 commit | 原审查的仓库/commit 清单 |

`docs/REUSE.md` 已明确区分"实际安装版本"与"已审查版本"，不把新拉取版本冒充已审查版本。

## 与计划的偏离（记录在案）

| 计划要求 | 实际 | 理由 |
|---|---|---|
| T01 要求 `compose.yaml` 与 Docker 健康检查 | 未使用 Docker，改为本机 `pg_ctl` + 项目内数据目录 | 用户明确要求本地 PostgreSQL、不上云端；Docker 亦属额外运行时依赖 |
| T19 要求 40 页混合 PDF | 已补足为 40 页 | 原实现 5 页，本轮修正 |
| T19 要求 OCR 进程退出故障 | 已实现真实子进程 `kill -9`（退出码 -9） | 原实现只测模型缺失，本轮修正 |
| T19 要求网页与 Agent 并发 | 已实现：真实 uvicorn + HTTP 客户端 + Agent 线程，恰好一个成功 | 原实现只测两个数据库连接，本轮修正 |
| T19 要求报告 OCR 内存 | 已报告 RSS 基线/峰值/增量与失败单元数 | 原实现缺此项，本轮修正 |
| 计划第 113 行要求"网页按 Asia/Shanghai 显示" | API 原不返回任何时间，界面也不显示 | 第三轮补：`CaseSummary` / `MaterialSummary` / `AnalysisSummary` / `AnalysisVersionSummary` 增 `created_at`，`Contract` 基类强制序列化为 UTC，前端 `formatTimestamp` 固定 `Asia/Shanghai` |
| 计划第 213 行"禁止手工维护两套不一致字段" | `endpoints.ts` 曾手写全套类型 | 第三轮改为从 `types.ts`（openapi 生成）派生别名，并删除全部匿名返回类型 |

## 第三轮复查（计划条款逐条核对）

按计划原文逐条核对，确认以下条款原本已满足，无需改动：

- 第 105 行"原件与工作副本不得使用硬链接"：工作副本是独立字节复制。
- 第 109 行"损坏原件标 `integrity_error`，禁止当作已验证原件返回"：`api/files.py` 在返回前检查 `integrity_status`。
- 第 135 行"原始文件版本属于对应材料的约束必须有测试"：由 `check_material_original_version` 触发器（含 `kind='original'`）强制，`test_db.py::test_original_version_must_belong_to_same_material` 覆盖。曾尝试改为组合外键，因触发器已表达外键无法表达的条件而撤回。
- 第 266–265 行：DOCX 嵌入图片、XLSX 图片/图表均记 warning 并标 `partial`。
- 第 274 行：缺模型 `dependency_missing`、超时 `timeout`、像素超限 `max_image_pixels` 三种错误可区分。
- 第 352 行：`docs/BACKUP_RESTORE.md` 已明确维护标志挡不住外部编辑。

## 审查整改（R01–R06）

`docs/V0.1_审查整改任务单_20261110.md` 的六项已全部修复并验证，逐项证据见 `docs/REMEDIATION_R01_R06.md`。

| 项 | 缺陷 | 状态 | 回归测试 |
|---|---|---|---|
| R01 | 解析不完整却显示完整（结果级警告被丢弃） | 已修复并验证 | `test_r01_result_warnings.py` 4 项 |
| R02 | 网页工作副本扩展名失效（`.docx（工作副本）` → `.bin`） | 已修复并验证 | `test_r02_working_copy_extension.py` 9 项 |
| R03 | 终态与索引发布之间崩溃后漏发布 | 已修复并验证 | `test_r03_publish_atomicity.py` 5 项 |
| R04 | 跳过已完成页发生在实际 OCR 之后 | 已修复并验证 | `test_r04_skip_before_ocr.py` 3 项（真实 OCR 计数） |
| R05 | 材料列表固定为前 50 份 | 已修复并验证 | `CaseWorkspacePage.test.tsx` 4 项 + 浏览器 120 份实操 |
| R06 | 切换来源时旧正文与新引用混搭 | 已修复并验证 | `SearchPanel.test.tsx` 3 项 |

R03 的恢复用异常注入模拟崩溃，未做进程级 `SIGKILL` 的 WAL 恢复验证；R06 未在真实浏览器限速下复现乱序（由组件测试覆盖）。两项均在回执的「未验证部分」列明。

## 结论

**V0.1 可用**，用于本地、单用户、虚构或已备份的案件材料归档与检索。

这个结论的范围：

- 依据 187 项自动化测试（真实 PostgreSQL）加一次端到端验收（1000 文件 / 10000 页）。
- **不包含**"原有 22 条检索测试已通过"——那是待交接项。
- **不包含**"经过安全审计"——六项目审查的 commit 未提供，`docs/REUSE.md` 只记录实际版本。
- **不包含**性能在 5 万页以上仍达标。

首次用于真实案件前：先做完整备份，并跑一次恢复演练（`scripts/rehearse_backup_restore.py`）。


## 第二轮整改（2026-11-13，独立审查后）

独立审查（`docs/V0.1_独立验收与代码审查_20261113.md`）判定「验收不通过」，列出 7 项 P1、3 项 P2、1 项 P3。第二轮逐项修复，证据见 `docs/REMEDIATION_R2_20261113.md`。

| 项 | 修法要点 | 回归 |
|---|---|---|
| R03 两次提交 | `_refresh_coverage(commit=False)`，终态与发布同一事务；`before_publish` 注入点；worker 启动补发旧现场 | `test_r03_publish_commit.py` 7 |
| R02 多点文件名 | 扩展名取完整显示名自带的那一个，不再从剥壳后的 stem 重取 | `test_r02_working_copy_extension.py` 17 |
| 并发登记 | 校验 CAS 返回值，失败回滚并删除无引用文件 | `test_register_concurrency.py` 3 |
| 相同字节上传 | 按 `(case_id, sha256)` 复用 `StoredObject` | `test_upload_dedupe.py` 4 |
| 空目录初始化 | `psql`/`createdb` 传 env；`init` 一次完成建角色→迁移→启动 | 空目录实跑 |
| 材料切换 | 切换即清空原件与片段，generation 守卫丢弃迟到响应 | `MaterialSwitch.test.tsx` 3 |
| 测试绑定 | session 级 autouse 绑定全局引擎；`db` 不再 reset | 空默认库 43 项通过、库仍 0 表 |
| R04 丢页 | 无 Segment 的终态页仍复制 unit，保留 `no_text_detected` | `test_r04_skip_before_ocr.py` 5 |
| OCR 超时 | `Deadline.expired` 标志区分包装型超时与真实缩放错误 | `test_parsing_pdf_ocr.py` 10 |
| T16/T17 | 批次存 `sessionStorage`、分页拉全、重试重新通知；`?ref=` 写入 URL 并直达 | 前端 6 + 3 + 5 |
| 备份清单 | `schema_version` 读 `alembic_version` | `test_backup_consistency.py` 24 |

当前：后端 **245 passed**，前端 **20 passed**，验收 13/13，恢复演练 7/7，清单显示 `0003_audit_operation_check`。

口径修订：上一轮「24 条全部通过」的判定偏松——测试存在不等于覆盖了那条验收。本轮起每条验收都要求「把修复还原后测试必须失败」。


## 第三轮整改（2026-11-13，第二轮复审后）

第二轮复审（`docs/V0.1_第二轮整改复审_20261113.md`）判定仍不能签收，列出 4 项 P1、1 项 P2 和 1 处文档偏差。第三轮逐项修复，证据见 `docs/REMEDIATION_R3_20261113.md`。

| 项 | 修法要点 | 回归 |
|---|---|---|
| 旧索引补发 | 每版本取最新终态 run，交给 `_publish_active` 裁决；覆盖「指针仍指旧 run」 | `test_r03_publish_commit.py` 10 |
| 合法改名 | 后缀仅在与真实扩展名相同时剥离；`合同.v2` → `合同.v2（工作副本）.docx` | `test_r02_working_copy_extension.py` 22 |
| 并行登记 | CAS 提到发布之前；后续失败清理已发布文件 | `test_register_concurrency.py` 4（`Barrier` 真并行） |
| 材料切换 | 切换时同时作废文本与副本代次；回包校验材料归属 | `MaterialSwitch.test.tsx` 5 |
| 上传去重 | 先查哈希再发布；并发插入用 savepoint 兜底 | `test_upload_dedupe.py` 6 |
| README | 按 `init` 实际行为改写（建库、迁移、前台启动） | — |

当前：后端 **256 passed**，前端 **22 passed**，验收 13/13，恢复演练 7/7。

判据再次收紧：复审两次推翻「已修复」，共同原因是「测试存在但没触发被修的路径」。本轮每条修复都做了还原验证，结果记在 `REMEDIATION_R3_20261113.md` 第三节。


## 第四轮整改（2026-11-13，第三轮复审后）

第三轮复审（`docs/V0.1_第三轮整改复审_20261113.md`）判定仍不能签收，列出 3 项 P1、1 项 P2。第四轮逐项修复，证据见 `docs/REMEDIATION_R4_20261113.md`。

| 项 | 修法要点 | 回归 |
|---|---|---|
| 并行上传删除共用原件 | 是否删除取决于胜者对象是否指向该路径，不取决于谁创建 | `test_upload_dedupe.py` 8 |
| 登记失败已提交 | `create_parse_run(commit=False)`；完整性失败先回滚再单独标记 | `test_register_concurrency.py` 6 |
| 部分覆盖挡住完整索引 | 候选排序「complete 优先、同状态取最新」，裁决仍在 `_publish_active` | `test_r03_publish_commit.py` 12 |
| 切换后按钮禁用 | 切换时 `setBusy(false)` | `MaterialSwitch.test.tsx` 6 |

当前：后端 **266 passed**，前端 **25 passed**，验收 13/13，恢复演练 7/7。

三轮复审的共同教训已记入 `REMEDIATION_R4_20261113.md` 第四节：断言要问「用户能感知的结果」，不要问「我以为的那个中间状态」。
