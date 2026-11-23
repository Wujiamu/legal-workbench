# 测试报告

当前正式运行基线为 `v0.1.0`（`0008_claim_amounts` + 2026-11-23 实机验收九项缺陷修复与启动器/开源发布收尾，零新增迁移）；本地原件验收记录归档于本机 `docs/reports/`（不随开源仓库分发）。

## 汇总

| 项目 | 结果 |
|---|---|
| 最近全量通过 | **1029 项**（后端 **622 passed, 1 skipped, 1 warning in 156.06s** 真实 PG18 全量，唯一 skip 为仅限真 Windows 的 `test_real_windows_ntfs_junction_rejected`；前端 **407** 项 57 个测试文件 + `tsc --noEmit && vite build` 通过） |
| 数据库 | 真实 PostgreSQL 18（隔离验证），无 SQLite、无内存库 |
| Windows 10/11 原生适配（2026-11-23） | 后端新增 `test_windows_compat.py`（8 项：Windows `PG_BIN`/`pg_env`、无 `SIGALRM` 线程超时、保留名/NTFS 路径守卫与真实 junction、脚本生命周期与 `lwb-launch` 锁/窗口/Browser/MessageBox、备份清单元/环境隔离）；前端新增 `src/ui/shortcuts.test.tsx`（2 项：`Win32` `Ctrl+Z` / `MacIntel` `⌘Z`）。经两轮独立代码审查闭环；`scripts/lwb doctor` 与 `generate_dossier` PDF 烟测通过；真实 Windows 宿主机实机验收按 [WINDOWS_CHECKLIST.md](WINDOWS_CHECKLIST.md) 留作待办 |
| `v0.1.0` 收尾与实机九项修复（2026-11-23） | 后端新增 `test_acceptance_fixes_20261123.py`（10 项全链路回归）与 `test_lwb_launch.py`（4 项断库恢复单测），前端新增 `CaseArchiveUiGuards.test.tsx`、`GraphCanvas.test.tsx` 等；两轮独立代码审查签收闭环；正式 `127.0.0.1:8765` 升级后 `check-consistency`（`ok: true`）、CLI/服务只读与拒写、内置 Chromium 四页实机复测通过 |
| 图表程序化与对外引用验收（2026-11-22） | 后端新增 `test_chart_export.py`、`test_graph_assist.py`、`test_graph_assist_audit.py`、`test_table_graph_import.py`、`test_chart_assist_transports.py`，前端新增 `ChartExportDialog.test.tsx`、`CoverageMatrix.test.tsx`、`SendToGraphDialog.test.tsx`、`GraphAuditDrawer.test.tsx`、`CaseGraphWorkspaceAssist.test.tsx`、`exportSvg.test.ts`、`httpExecutor.test.ts` 等；AC10 外部复核实测检出 3 类预设错误、AC12 前后端独立双审闭环、Chromium 隔离两轮 E2E 通过（含 `save_layout` 修复复测），正式应用后 `check-consistency`（`ok: true`）及只读 HTTP 验收通过。Safari 实机未测、真实案件尚未试用。见 [图表程序化能力验收与正式应用](reports/20261122-图表程序化能力验收与正式应用.md) |
| 联合回归与后续专项 | 后端全量后新增独立材料绑定保护，证据目录及关系图定向 65 项通过。前端全量、TypeScript 检查及生产构建通过。见 [整改复测](reports/20261122-P5联合隔离验收.md#2026-11-22-整改复测) |
| 最新正式升级验收 | 2026-11-22 从 `0006_case_archive_time` 迁移到 `0008_claim_amounts`，正式服务只读复核通过。见 [备份与升级记录](reports/20261122-P5联合隔离验收.md#p6-正式升级与只读验收)和[隔离恢复演练](reports/20261122-P5联合隔离验收.md#联合测试及演练) |
| 诉讼 UI 验收 | 独立 Chromium 连接合成案卷服务，见 [重构验收](changes/20261114-诉讼工作台UI重构.md) |
| V0.1 端到端验收 | `scripts/run_acceptance.py`，13 项判定全部通过（见 [本轮 T19 实测](reports/T19-修复验收实测-20261113.md)） |
| 备份恢复演练 | `scripts/rehearse_backup_restore.py`，真实 `pg_dump`/`pg_restore`，恢复后重新检索命中；2026-11-20 扩展覆盖 0006（归档案件与工时记录往返），见 [P5 隔离验收与恢复演练](reports/20261120-P5隔离验收与恢复演练.md) |
| 生产升级验收 | 2026-11-20 `0004_case_workbench → 0006_case_archive_time` 后只读验收：`/api/v1/progress/rules` 200、案件概览含 `active_stages`/`archived_at`、进度快照 6 个默认节点、`/` 下发的 `index.html` 与构建产物 sha256 一致、Chromium 打开进度页正常且工时/结案归档入口可见；未做业务写入。见 [P6 正式升级与验收](reports/20261120-P6正式升级与验收.md) |
| P6 后验收 | 2026-11-20 对尚未验收部分实测：全量回归重跑、T19 13 项判定通过（修复脚本端口隔离缺陷后）、备份恢复演练 OK、正式服务 8765 上归档/工时/进度/关联/撤销/徽标/回收站端到端（TEST-ONLY 案件）；发现并修复图形视图父节点顺序缺陷（D1），重建应用后零重叠。见 [P6 后验收实测记录](reports/20261120-P6后验收实测记录.md) |
| 测试数据 | 全部为脚本生成的 TEST-ONLY 虚构内容 |

测试库每个 session 用真实 Alembic 迁移重建，不是 `create_all`，因此迁移本身也在被验证。

## 按任务包分布

| 文件 | 条数 | 覆盖内容 |
|---|---|---|
| `test_protocol.py` | 9 | Pydantic 协议、错误结构与状态码、OpenAPI 一致性 |
| `test_config.py` | 2 | 配置加载、凭据不出现在 repr |
| `test_db.py` | 11 | 表与扩展存在、维护标志、唯一约束（`request_key`、`content_hash`）、原件版本必须同材料、迁移可重入 |
| `test_storage.py` | 11 | 流式哈希、无覆盖发布、并发同内容复用、路径穿越（5 例）、符号链接、复制期间源变化、大小上限、暂存诊断、篡改检测 |
| `test_imports.py` | 11 | 混合批次终态、`request_key` 幂等、新批次共享原件但新建材料、三处崩溃点重试无重复、越界来源、符号链接来源、子目录来源可解析、源文件变化 |
| `test_materials.py` | 10 | 分类不改原件、空分类归"待分类"、过期 revision 冲突、名称去路径、编辑工作副本不影响历史、快照固定、parent 链、跨案版本拒绝、缺失/链接工作文件、列表分页与 coverage |
| `test_worker.py` | 7 | 第二 worker 立即退出、`running` 恢复、崩溃后续跑无重复、维护标志拒绝写入、失败任务同 ID 重试、坏任务不阻塞、领取即 `running` |
| `test_parsing_text_docx_xlsx.py` | 9 | UTF-8-BOM / GB18030 解码、损坏 DOCX、空文件、嵌套表格顺序、合并单元格、公式无缓存、隐藏 sheet、源文件未被改写 |
| `test_parsing_pdf_ocr.py` | 10 | 原生文本 PDF、扫描页转 OCR、旋转页、40+ 页、模型哈希篡改拒绝、多帧 TIFF、超大图像拒绝、包装型超时归类、真实缩放错误不被误判 |
| `test_parsing_pipeline.py` | 10 | 逐 unit checkpoint、coverage 计数、active run 发布规则、旧 run 清理、offset_map |
| `test_search.py` | 15 | 四种模式正反例、金额币种隔离、`100` vs `1000`、覆盖统计、文件名精确命中排序、跨案隔离、空查询 |
| `test_search_offsets.py` | 4 | 归一化偏移回映射到原文、`context[match_start:match_end]` 正确 |
| `test_analysis.py` | 7 | 不可变版本、跨案 `source_ref` 拒绝、审核状态、分页、revision 冲突、分析范围检索 |
| `test_extracted_text.py` | 4 | 按文档顺序返回、`parse_run_id` 指向正确 run、coverage、空 run |
| `test_working_copy_versions.py` | 2 | 未修改工作副本登记时复用 `StoredObject`、版本链正确 |
| `test_r01_result_warnings.py` | 4 | 结果级警告传递：页眉 DOCX / 含图 XLSX 为 partial、普通文档仍 complete、三条读取路径一致 |
| `test_r02_working_copy_extension.py` | 22 | 四种格式副本扩展名、多点文件名（`合同.v2.docx`）、重命名后仍正确、改内容登记后解析成功、原件不变、未改动仍去重 |
| `test_r03_publish_atomicity.py` | 5 | 终态与发布同事务、崩溃后补发布、旧任务不覆盖新版本、partial 不替换有效版本、旧 source_ref 仍可读 |
| `test_r03_publish_commit.py` | 12 | 真实提交边界（`before_publish`）注入、正常 worker 恢复、旧现场补发、不覆盖更新版本、历史引用 |
| `test_r04_skip_before_ocr.py` | 5 | 恢复不重做已完成页（真实 OCR 计数）、强制页不 OCR 其他页、跳过页异常不阻断恢复、空白页不丢失 |
| `test_register_concurrency.py` | 7 | 两个连接同修订号登记只成功一次、失败方不留孤儿文件、陈旧修订不发布、两份副本并行登记同字节复用对象 |
| `test_upload_dedupe.py` | 18 | 同批/跨批相同字节复用存储对象、材料与分类独立、同目录同名、校验失败整批撤销且请求键可重试、清理不删他批已登记路径、重复路径整批拒绝无残留、发布和提交故障清理、备份锁互斥、两入口并发去重、解析入队中断恢复、并行重试复用批次 |
| `test_coverage.py` | 3 | 不支持/失败不伪装成"待处理"、从未解析仍报 pending、失败 run 报 failed |
| `test_audit.py` | 7 | 审计事件：各操作各记一条、不含正文/密钥/连接串、未知操作被 Python 与数据库双重拒绝、非 UUID target 稳定哈希 |
| `test_api.py` | 22 | 未登录 401、错误 Host/Origin、错误 access key、CSRF 缺失/错误、cookie 属性、案件 CRUD、上传→worker→检索→read_source→下载、附件头、预览转义、中文文件名 ASCII fallback |
| `test_case_workbench.py` | 5 | 办理期限排序及 CAS、PDF 对应页、图像对应帧、标注复核及跨案边界、取得方式回读；首页并行程序：两个程序各有进行中阶段全部展示、嵌套未完成工作按 position 正序计入（审查末节） |
| `test_cli_mcp.py` | 9 | CLI 建案/列案 JSON、错误退出码 2、完整链路、分析保存审核、输出不含密钥、真实 MCP `stdio_client` 往返、跨案拒绝 |
| `test_backup_consistency.py` | 23 | 一致性检查各故障类型、备份清单与哈希、备份期间源变化失败、`INCOMPLETE` 拒绝恢复、越界路径拒绝、空环境恢复往返、非空目标拒绝、维护标志阻断与解除、工作副本纳入备份 |
| `test_web_cache.py` | 4 | index.html 与 SPA 路由 no-store、哈希产物 immutable 长缓存、API 不加静态缓存头（回归 2026-11-18 旧壳缓存缺陷） |
| `test_progress.py` | 46 | 进度服务：默认片段实例化、规则注入扩展（新种类＋四层不改代码）、包含/标题/状态校验、CAS 冲突、跨案父节点与关联拒绝、移动成环与重排、set_active_stage 连带原阶段一条变更、Agent 待确认/确认/撤回（含已确认可撤回、后续修改拒绝）、accept_all 仅人工/空态拒绝/只确认已展示条目/expected_seq 冲突、request_key 幂等、片段插入（含 before_node_id 让位）与整体撤回、软删除恢复、关系与关联生命周期、审计闭集只存 ID、变化序号、同案并发串行、backfill 幂等＋旧阶段精确匹配激活；审查整改回归：撤回新建遇后续子节点拒绝、移动撤回还原同级顺序、移除关系/关联后重建再撤回冲突、普通编辑/新增联动单一进行中阶段、显式 null 清空备注日期、有效关系/关联部分唯一索引、CLI/MCP 建案 actor 来源；审查末节：accept_all 显式空集拒绝、省略 IDs 须 expected_seq、批量审计逐条可还原确认集合 |
| `test_progress_transports.py` | 21 | HTTP 规则/快照/写命令返回快照/actor=human、409 冲突、路径与 payload 案件一致性、确认撤回人工流程、批量确认空集/省略 IDs/陈旧序号 422/409 与未展示条目不被确认（审查末节）；MCP 工具集含进度（含移除关系/解除关联/分析/标注关联）且无 accept/revert；CLI progress 命令 actor=agent 待确认＋全命令覆盖（编辑/移动/删除/关系/关联）；复核整改回归：MCP 部分更新字段三态＋clear_note、MCP 片段 before_node_id、写响应 last_change_id、撤回 remove 位置让位不重复＋让位条目 before/after/rev 数值＋二次撤回还原、「撤回的撤回」软删遇子节点拒绝、撤回移除关系/关联端点已删拒绝、add 让位同级入 diff 撤回还原、撤回删除恢复进行中阶段不自联动抵消（R4-2）、普通编辑撤回还原字段不误联动（R5-2）、让位同级原本进行中既让位又被联动两条 diff（R4-2 边界） |
| `test_progress_migration.py` | 1 | 0004 旧库（含 stage/next_action/deadlines）升级 0005：进度表建立、旧字段保留、backfill 默认片段、审计 CHECK 接受 progress.*、backfill 幂等、downgrade 回 0004 删进度表且旧案 intact |
| `test_progress_consistency.py` | 6 | 进度一致性检查：正常树不误报、悬空父节点、重复同级位置、多进行中阶段、悬空关系端点均报出具体案件与节点；整库检查按案件分组，不同案件的根节点/进行中阶段不互相误报 |
| `test_case_archive_time.py` | 10 | 结案归档与工时：归档后进度/材料/办理信息写入被拒、列表过滤与首页隐藏、取消归档恢复；归档 CAS 冲突；有未停止计时拒绝归档、归档后工时只读；计时 0.25h 向上取整、重复计时拒绝、已停止不能再停；补录 0.25 步进校验、日视图合计与软删除；补录不被当成进行中计时（不挡计时与归档）；停止补录被拒且时长不变；日视图含进行中条目但标题汇总只计已完成；审计记录 case.archive/time.start/time.stop；跨案工时记录拒绝 |
| `test_acceptance_fixes_20261123.py` | 10 | 实机验收九项缺陷＋审查复核回归：归档阻断标注与分析创建/复核、多币种 XLSX 分桶与 `currency_conflict_same_pair`（含 `HK$`/`US$` 与存量无币种关系）、表格拆列币种金额精确检索（防误抽年份/回单号＋公式缓存）、损坏 PDF 归类 `PARSE_FAILED`、诉请引证附表 DOCX 段落与 TXT 行号定位、CLI `save-analysis --content-file` 互斥与读异常 `invalid_input`、畸形 `locator` 容错 |
| `test_lwb_launch.py` | 4 | 桌面启动器 `scripts/lwb-launch`：正常就绪免拉起、未运行时拉起 `lwb start`、网页在线但数据库停止时自动执行 `lwb start-db` 与恢复 `lwb worker`、数据库拉起失败立即报错不空等 |
| `test_windows_compat.py` | 8 | Windows 跨平台适配（7 项跨平台模拟 + 1 项真实 NTFS Junction 在非 Windows 跳过）：`LWB_PG_BIN` 与 Windows 候选目录探测、`pg_env()` 不强制 `LC_ALL`、无 `SIGALRM` 线程版 OCR 超时与异常保留、`safe_name` Windows 保留名/空格/截断边界与 NTFS 路径守卫、Junction 拦截、`lwb` builtin `C.UTF-8` 与错误脱敏、`lwb-launch` `msvcrt` 锁/`CREATE_NO_WINDOW`/`os.startfile`/`MessageBoxW`、备份清单越界拦截 |
| `test_archive_time_migration.py` | 1 | 0005 → head：`cases.archived_at` 与 `time_entries` 建立、旧案件保留且归档时间为空、app 角色可写计时与归档审计、手动工时无需 started_at；downgrade 回 0005 删表删列且进度数据与旧案件不受影响；再升级成功 |

## 前端测试

`frontend` 使用 Vitest CLI、jsdom 和 Testing Library，截至 2026-11-23 共 **407 项**（57 个测试文件；除下方历史逐文件清单外，后续新增关系图/证据目录/诉请金额/图表程序化/跨平台快捷键 `src/ui/shortcuts.test.tsx`、首页归档切换及共享导航 `?view=` 同步等回归见 `CaseArchiveUiGuards.test.tsx`、`CasesPage.test.tsx`、`GraphCanvas.test.tsx` 与 `CHANGELOG.md`；本机历史报告归档于 `docs/reports/` 不随开源仓库分发）：

| 文件 | 条数 | 覆盖内容 |
|---|---|---|
| `src/pages/CaseWorkspacePage.test.tsx` | 6 | 翻页到第 51/101 份、末页禁用下一页、全案总数不随筛选收缩、仅末页出现的分类仍在导航、`?ref=` 直达检索页签、旧分页回包不能覆盖当前页 |
| `src/pages/MaterialSwitch.test.tsx` | 9 | 切换材料不显示上一份的原件/正文、迟到响应不覆盖当前材料、甲的副本回包不启用乙的登记、甲的迟到保存不改乙的修订号与按钮、切走后成功改名仍按当前分类刷新 |
| `src/components/SearchPanel.test.tsx` | 3 | 切换来源不显示旧正文、乱序响应不覆盖当前选择、读取失败不残留旧正文且不可保存 |
| `src/components/SearchDeepLink.test.tsx` | 4 | 中文组词回车不检索、完成输入后可用回车提交、选中命中写入 `?ref=`、带参数直达片段、复制可打开的链接 |
| `src/components/UploadPanel.test.tsx` | 6 | 终态才通知、自动轮询、不重复通知、重挂载续跟踪、超一页计数、重试再次通知 |

进度原型（2026-11-16，P1，内存执行器不接真实接口）：

| 文件 | 条数 | 覆盖内容 |
|---|---|---|
| `src/progress/localExecutor.test.ts` | 49 | 命令语义：包含规则、CAS 冲突、Agent 待确认/确认/撤回（含连带阶段与后续修改拒绝、已确认变更可撤回）、request_key 幂等、移动成环、片段实例化与整体撤回、软删除恢复、关系与关联（含跨案拒绝）、规则注入四层扩展、accept_all 仅人工/空态拒绝/只确认已展示条目/expected_seq 冲突、显式空集拒绝、省略 IDs 无序号拒绝；审查整改回归：普通编辑/新增联动单一进行中阶段、撤回新建遇后续子节点拒绝、移动撤回还原同级顺序、移除关系后重建再撤回冲突、片段 before_node_id 让位；原型与后端同口径（R4-1/R4-2）：撤回 remove 还原 position＋让位入 reverse_diff（含 before/after/rev 数值）＋二次撤回还原、「撤回的撤回」软删遇子节点拒绝、撤回移除关系/关联端点已删拒绝、add 让位入 diff 撤回还原、撤回删除恢复进行中阶段不自联动抵消、普通编辑撤回还原字段不误联动（R5-2）、让位同级原本进行中既让位又被联动两条 diff（R4-2 边界） |
| `src/progress/CaseProgressPage.test.tsx` | 6 | 正式页关联材料（审查末节）：首屏 200 项＋加载更多、加载后可选并关联第 201 项；读取失败与空列表分开呈现、错误可重试且重试成功后出现材料。结案归档：确认归档调用 archive 并转只读提示、归档后进度写入被本地拦截且不发请求、取消归档恢复写入入口、409 时刷新概览并用新修订号重试成功 |
| `src/progress/CaseTimePanel.test.tsx` | 4 | 工时持久化：开始计时后显示运行中记录、停止调用 stop；补录 0.25h 写入服务端、日视图显示合计并可删除；归档后所有写操作禁用并提示只读；补录条目（`stopped_at` 为空）不算进行中的计时 |
| `src/progress/useCaseProgress.test.tsx` | 6 | 初始快照、命令替换、冲突重读、10 秒轮询、回到前台检查、迟到响应丢弃 |
| `src/progress/NodeEditorDialog.test.tsx` | 10 | 预填与规则状态选项、命令带修订号、输入法组词回车不提交、冲突保留草稿、重新加载才覆盖、新建模式、标题预设下拉（预设直选/其他自定义/空值禁提交/编辑与阶段仍自由输入） |
| `src/progress/OutlineView.test.tsx` | 17 | 横向树 aria-level、节点即按钮（种类/状态/待确认/关联）、单击选中双击编辑、右键菜单（设为进行中/编辑/移至/删除/收起）、悬浮加号最近边与规则种类菜单、收起子树、根级添加菜单、键盘同级导航；回收站删除→恢复回树、关系折叠列表移除、解除关联按钮在节点按钮外（不嵌套）点击发 unlink（审查 R3-2/R3-5） |
| `src/progress/interactions.test.tsx` | 4 | 最近边计算、菜单项执行与关闭、Escape/外点关闭、禁用项 |
| `src/progress/ChangeReviewPanel.test.tsx` | 6 | 前后状态对照、确认/撤回命令、撤回冲突保留条目、空态、新建类变更 |
| `src/progress/graphModel.test.ts` | 8 | 与大纲同一快照节点集、包含→嵌套/关系→虚线边、elk 输入嵌套、布局结果映射、收起子孙隐藏与紧凑卡片、关系边改挂收起祖先、elk 叶子化与端点改挂、删除带关系节点后布局边不引用已删除端点（审查 08） |
| `src/progress/layout.test.ts` | 1 | 真实 elkjs 计算：程序内阶段横排、阶段下工作纵排、父节点包住子节点 |
| `src/progress/GraphView.test.tsx` | 3 | React Flow 渲染冒烟（jsdom 桩）、关系边标签、点击选中、分组收起后子节点不渲染、右键菜单设为进行中、双击编辑 |
| `src/progress/ProgressPage.test.tsx` | 7 | 深层分组默认收起、展开全部/收起全部工具、结案归档树外按钮、归档只读与取消归档恢复、一键确认二次对话框与徽标变色、⌘Z 分区撤销互不干扰、工时按钮弹窗与关闭 |
| `src/progress/rules.test.ts` | 2 | 默认片段不含结案归档（案件级操作）、片段草稿齐全带版本 |
| `src/progress/fixtures.test.ts` | 2 | 合成案卷为通用诉讼（无无效程序等专门类型节点）、保留三层嵌套与 Agent 待确认场景 |
| `src/progress/titlePresets.test.ts` | 2 | 工作/事件有预设、程序/阶段无预设、注入新种类预设不改代码生效 |
| `src/progress/timeLog.test.ts` | 7 | 计时器开始/停止（0.25h 粒度向上取整）、重复开始拒绝、快捷时长覆盖、补录校验、日视图合计、按标题 billable 汇总、删除 |
| `src/progress/undo.test.ts` | 4 | 人工新增逆操作删除、graph/review 分栈互不干扰、空栈拒绝、修改逆操作恢复旧值 |
| `src/progress/agentBadge.test.ts` | 4 | 待确认黄、确认绿、撤回/人工无徽标、同节点 pending 优先 |

运行：`cd frontend && npm test`

2026-11-15 前端验收使用本机 Node 26.8.2 与 Vitest 5.1.0。28 项组件测试、TypeScript 检查和 Vite 构建通过。完整前端锁文件的 npm 审计为 0 项。Modern Web Guidance 的确定性旧模式扫描从 1 项降到 0 项，扫描覆盖其约 20% 指南。

2026-11-16 进度原型（P1）：137 项测试、TypeScript 检查、Vite 构建通过，npm 审计 0 项。真实 Chromium 在正式服务 8765 实测大纲/图形/待确认复核/输入法合成事件/760px 半屏/节点收起/横向树与节点按钮交互/结案归档只读/通用案卷与零重叠/标题预设下拉/Agent 徽标黄绿/一键确认二次对话框/⌘Z 分区撤销/工时计时器与日视图汇总（用户反馈七轮），证据见 [变更记录](changes/20261116-办案进度可视化.md) 与 `reports/ui-20261116/`。Safari 与真实输入法待承办人验证。**P2 冻结：用户确认前不做后端。**

独立 Chromium 使用构建产物和合成 API 响应验证分段输入事件、正常回车和按钮检索、材料标签聚焦与保存、导入提示关联及 1440px 和 760px 布局。在本机运行实例只读验证服务已加载新构建、案件工作表和新建案件入口正常显示。macOS 真实输入法、Safari 和真实案卷仍待验。

2026-11-20 P5 隔离验收：隔离实例（副本源码＋独立库 `legal_workbench_p5`＋独立构建，端口 8799）上，Chromium（Browser Use CLI）与 WebKit（Playwright 1.65.1）各跑完整流程——树渲染、开始/停止计时、日视图 0.25h、按标题汇总、结案归档、归档后进度写入被拦、工时只读、取消归档后写入恢复，两引擎均无页面脚本错误；截图在 `reports/p5-20261120/`。**Safari 实机未完成**（需在 Safari 设置→开发者勾选「允许远程自动化」或「允许 JavaScript 来自 Apple 事件」），以 WebKit 引擎结果替代，不等同 Safari 实机。详见 [P5 隔离验收与恢复演练](reports/20261120-P5隔离验收与恢复演练.md)。

## 关键验证点

以下不是"测试通过"的泛泛结论，而是具体断言过的事实：

1. **app 角色不能建表**。`lwb_app` 执行 `CREATE TABLE` 抛 `InsufficientPrivilege`，DDL 只在 `lwb_migrator`。
2. **跨案引用被数据库拒绝**。组合外键在 SQL 层拦截，不只是服务层检查。
3. **无覆盖发布**。目标已存在时 `os.link` 失败；同内容并发发布一创建一复用。
4. **崩溃恢复无重复**。在 `copying`/`published`/`associated` 三个阶段分别注入异常，重试后材料数与文件数一致。
5. **历史引用不失效**。重解析后旧 `source_ref` 仍返回旧文本，`active_parse_run_id` 已指向新 run。
6. **金额精确**。`100` 不匹配 `1000`；`100 USD` 不进 CNY 结果；`1万元` 归一为 `10000`。
7. **OCR 全离线**。模型哈希被篡改时抛 `dependency_missing`，运行时不下载。
8. **恢复后可用**。新库新目录恢复后重新检索仍命中，一致性检查 `ok=True`，`lwb_app` 权限已重新授予。

## 未验证（不得记为通过）

| 项目 | 状态 | 原因 |
|---|---|---|
| 原有 22 条检索测试 | **待交接** | 未拿到原样本与期望结果。本项目测试是另行编写的，不能替代 |
| 原审查六项目 | **未提供** | 无 commit 清单，`docs/REUSE.md` 只记录实际安装版本 |
| Linux 平台 | **未验证** | 只在 macOS Apple Silicon 上跑过实机 |
| Windows 10/11 宿主机实机 | **待真机验收** | 跨平台源码、启动器与模拟单测已通过（含两轮独立审查），因本机为 macOS 尚未在真 Windows 宿主机实跑 [WINDOWS_CHECKLIST.md](WINDOWS_CHECKLIST.md) |
| 真实案件材料 | **未使用** | 全部为合成数据；首次真实导入前需先备份 |
| 5 万页以上规模 | **未验证** | 只测到 1000 文件 / 10000 页 |
| 备份进程被 `kill -9` | **部分验证** | 验证了 `INCOMPLETE` 标记存在与拒绝恢复，未验证完整恢复路径 |
| 加密 Office 文件 | **未验证** | 未构造样本断言错误码 |
| 多用户并发写 | **未验证** | 设计为单用户；多客户端并发只验证了 CAS 冲突 |

## 如何复现

```sh
cd backend
.venv/bin/python -m pytest -q                    # 后端全套
.venv/bin/python -m pytest tests/test_search.py -v   # 单文件

cd ../frontend
npm test                                          # 前端组件测试
npm run build                                     # 类型检查 + 构建
```

端到端与性能：

```sh
cd /项目根
LC_ALL=en_US.UTF-8 LWB_PG_BIN=/opt/homebrew/opt/postgresql@18/bin \
  backend/.venv/bin/python scripts/run_acceptance.py
```

备份恢复演练：

```sh
LC_ALL=en_US.UTF-8 LWB_PG_BIN=/opt/homebrew/opt/postgresql@18/bin \
  backend/.venv/bin/python scripts/rehearse_backup_restore.py
```

两个脚本都自建并清理临时数据库，不碰开发库。

## 测试告警

上轮后端有一条 Starlette 使用 AnyIO 已弃用别名的告警。2026-11-15 前端测试输出无告警。
