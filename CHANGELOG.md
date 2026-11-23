# 变更记录

## 2026-11-23 · `v0.1.0` 界面交互与导航打磨（**源码与前端构建完成**）

- **跨平台共用前端与交互修复**（macOS / Windows 共用一套 `frontend/` 代码，沿用 `undoShortcutLabel()` 平台键位与 `scrollbar-gutter: stable` 滚动条占位）：
  - **首页找回归档与清理演示入口**：移除首页主操作区 3 个开发期「原型 · 演示数据」入口；新增「查看已归档 / 返回在办案件」切换按钮调用 `casesApi.list(..., true)` 展示已归档案件与归档日期。
  - **全工作区共享案件导航栏**：抽取 [CaseNavBar.tsx](frontend/src/components/CaseNavBar.tsx) 统一复用至主工作区、办案进度、关系图、证据目录与诉请金额 5 个案件路由（补入「办案进度」），切工作区页签同步写入 URL `?view=`，主工作区归档时增加带取消归档入口的只读提示横幅。
  - **办案进度与关系图排版及细节**：`.h-tree` 改为纵向分列多根程序并补 `flex-shrink: 0` 修复「二审」压盖「一审」；补齐进度页「结案归档」「一键确认/撤销」按钮样式；调大 `layout.ts` 节点高度与 `elk` 间距避免卡片遮挡边标签；修复选中图页签 `.muted` 对比度，限制 `.graph-review-list` `360px` 高度内滚动，默认折叠线型图例与真实案件页底部嵌套「案卷证据目录」；移除界面可见的 `import_material_id` 与 `seq=` 开发字段文案。
- **验证与未覆盖项**：前端 `npx tsc --noEmit`、`vitest run`（**57 文件 407 passed**）及 `npm run build` 全部通过；本轮未重启实机浏览器交互验证。

## 2026-11-23 · Windows 10/11 原生适配（**源码与 macOS 回归闭环，待 Windows 实机验收**）

- **版本基线与影响**：零数据库迁移（维持 `0008_claim_amounts`），兼容现有 macOS 运行环境与数据格式；已同步更新 [README.md](README.md)、[ARCHITECTURE](docs/ARCHITECTURE.md)、[AGENT](docs/AGENT.md)、[BACKUP_RESTORE](docs/BACKUP_RESTORE.md)、[TEST_REPORT](docs/TEST_REPORT.md)、[NEXT_STEPS](docs/NEXT_STEPS.md)、配套 Skill 模板及新增 [WINDOWS_CHECKLIST.md](docs/WINDOWS_CHECKLIST.md)，变更单见 [20261123-Windows适配](docs/changes/20261123-Windows适配.md)。
- **核心跨平台修复**：
  - **OCR 超时跨平台**：`parsing/ocr.py` 在 POSIX 主线程保留 `SIGALRM`，在无 `SIGALRM`（Windows）与非主线程下改用守护工作线程限时等待（`threading.Event.wait`），超时统一抛 `TimeoutError`、原业务异常原样传播。
  - **PostgreSQL 工具探测与区域设置**：`config.py` 新增 `Settings.pg_bin`、`resolve_pg_bin()` 与 `pg_env()`（读取 `.env`/环境变量 `LWB_PG_BIN`，自动探测 Homebrew / `C:\Program Files\PostgreSQL\18\bin` 等路径；Windows 不再强设 `LC_ALL=en_US.UTF-8` 并显式启用 `PGCLIENTENCODING=UTF8` / `PYTHONUTF8=1`）；`backup.py`、`cli.py`、`doctor.py` 统一采用 UTF-8 子进程解码与 `PG_BIN` 解析，`cli.py` 与 `mcp_server.py` 启动时重配标准流为 UTF-8。
  - **NTFS 路径安全与链接拦截**：`storage.py` 的 `safe_name` 先清理空白与截断后再结合 `ntpath.isreserved()` 拦截 Windows 保留设备名（含 `CON .txt`、`CONIN$`、`COM¹` 等变体）与非法字符；`managed_path`、`verify_backup`、`imports.py`、`materials.py` 拦截反斜杠/盘符越界、统一用 `is_relative_to()` 与 `is_link_or_junction`（覆盖 NTFS 目录 Junction），`app.py` 规范化 `frontend/dist` 校验。
  - **生命周期与桌面入口**：新增根目录双击启动器 `法律工作台.bat`（CRLF、纯 `goto` 分支避免括号块编码问题、通过 `pythonw.exe` 无黑框拉起）与命令行包装 `scripts/lwb.cmd`；`scripts/lwb` 支持 `.venv` Python 直调、Windows `initdb --locale-provider=builtin --builtin-locale=C.UTF-8`、`SIGBREAK` 清理、`subprocess.run` 替代 `os.execvpe` 及失败命令口令脱敏；`scripts/lwb-launch` 跨平台改用 `msvcrt.locking`（`LK_NBLCK`）、`CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`、`os.startfile` 与 `MessageBoxW`；语料与验收脚本（`build_corpus.py`、`generate_dossier.py`、`rehearse_backup_restore.py`、`run_acceptance.py`、`generate_protocol.py`）消除 `/tmp`、`rm -rf`、`ps` 与 macOS 专属字体硬编码。
  - **前端平台提示**：新增 `frontend/src/ui/shortcuts.ts`，界面撤销提示按平台自适应展示 `⌘Z` 或 `Ctrl+Z`，CSS 与 SVG 导出字体栈补充 `"Segoe UI"`，并重编 `frontend/dist`。
- **验证与已知边界**：
  - macOS 本机全量后端 **622 passed, 1 skipped**（唯一 skip 为仅限真 Windows 的 `test_real_windows_ntfs_junction_rejected`）、前端 **57 文件 405 passed** + `tsc --noEmit && vite build` 通过、`scripts/lwb doctor` 与 `generate_dossier` PDF 烟测通过；经过两轮独立代码审查并修复全部中/低优先级问题后签收闭环。
  - **未验证边界**：因当前工作机为 macOS，尚未在真实 Windows 10/11 宿主机执行实机全流程运行，待按 [WINDOWS_CHECKLIST.md](docs/WINDOWS_CHECKLIST.md) 完成真机验收。

## v0.1.0 (2026-11-23) · 首个开源里程碑发布准备

- **桌面启动器断库恢复修复**：`scripts/lwb` 新增幂等 `start-db` 子命令；`scripts/lwb-launch` 在检测到网页在线（`/api/v1/health` 200）但数据库未就绪（`/api/v1/cases` 失败）时自动执行 `scripts/lwb start-db` 并恢复 `scripts/lwb worker`，拉起失败时立即提示而非空等 45 秒；补充 [test_lwb_launch.py](backend/tests/test_lwb_launch.py) 4 项回归单测。
- **GitHub Public 开源仓库准备**：新增 MIT [LICENSE](LICENSE)，重写面向外部读者的 [README.md](README.md)，加固 `.gitignore`（排除 `node_modules/`、`docs/reports/`、`docs/backups/`、`.impeccable/` 等本机大体积验收归档与历史配置备份），完成文档及桌面脚本中个人本机路径与个案名称脱敏，并同步 L0/L1 基线至 `v0.1.0`。

## 2026-11-23 · 实机验收九项缺陷修复（**源码交付，零迁移已应用至本机 127.0.0.1:8765**）

- **版本基线与升级影响**：零数据库迁移（维持 `0008_claim_amounts`），既有业务数据与已归档专案快照完好；升级前完整备份位于 `var/backups/p6-20261123-130158/`（`complete: true`，`files: 13`，含 `previous-source.tar.gz` 与 `previous-dist/`）。回退时停止服务、恢复 `previous-source.tar.gz` 与 `previous-dist/` 并重启 `scripts/lwb start`。
- **修复与增强**：
  - **归档只读防穿透**：`source_notes.py`（`create_annotation` 在 PDFium 前预检 `archived_at` 并与 `review_annotation` 统一用 `lock_case_writable`）及 `analysis.py`（`save_analysis`/`review_analysis` 同步改用 `lock_case_writable`）阻止已归档案件通过 MCP/CLI 写入；工作台首页、材料详情、案件概览、检索保存、标注与进度页面全部禁用写控件。
  - **多币种表格导入与拆列检索**：`table_graph_import.py` 与 `TableImportDialog.tsx` 新增 `currency_col`、按币种分桶汇总及 `currency_conflict_same_pair` 同对跨币种拦截（支持 `HK$`/`US$` 与存量无币种关系续写）；`search.py` 新增表格拆列币种金额提取（支持 `=>` 公式缓存并防止相邻年份/回单号误抽）。
  - **解析分类、引证定位与画布修复**：损坏 PDF 改归 `PARSE_FAILED`；`chart_export.py` 扩展导出定位字段并输出 DOCX 段落与 TXT 行号；`GraphCanvas.tsx` 修复实体卡 `出处` 计数并实现同对双向/多边曲线及标签分道偏移；`cli.py` 支持 `save-analysis --content-file` 单独传参并互斥校验。
- **验证与已知边界**：后端 **611 passed**（含 `test_acceptance_fixes_20261123.py` 10 项）、前端 **56 文件 403 passed** + `tsc --noEmit && vite build` 通过、两轮独立代码审查签收闭环；正式 `127.0.0.1:8765` 升级后 `check-consistency`（`ok: true`）、CLI/服务拒写及只读核对、内置 Chromium 四页实机复测通过。已知边界：已归档专案未对历史解析快照重跑覆盖（拆列检索与损坏 PDF 分类以隔离 PG18 + Worker 端到端测试为准）；Safari 实机未测。详见 [2026-11-23 TEST-ONLY 实机验收修复与正式应用报告](docs/reports/20261123-TEST-ONLY-实机验收修复与正式应用.md)。

## 2026-11-22 · 图表程序化能力与对外引用导出（**源码交付，零迁移已应用至本机 127.0.0.1:8765**）

- **版本基线与升级影响**：保持零数据库迁移（`0008_claim_amounts` 不变），不改动现有表结构与既有材料/案件数据；升级前已校验备份位于 `var/backups/p6-20261122-164747/`（`complete: true`，`files: 2`，含 `previous-source.tar.gz` 与 `previous-dist/`）。回退时停止 Web/Worker，恢复 `previous-source.tar.gz` 与 `previous-dist/` 并重启 `scripts/lwb start`（或以 `restore` 在空库空目录恢复）。
- **已应用核心能力（Wave 1–3）**：
  - **一源三态导出与硬门禁（1A）**：支持关系图与诉请金额瀑布图按 `working`（工作简洁态）、`client`（客户沟通态，自动剔除待定提案/`ruled_out`/内部备注并附 `[1]..[N]` 角标与精简脚注）、`court`（当庭呈递态，强制绑定不可变快照与硬门禁拦截，调用 `evidence_catalog.resolve_labels()` 解析实时编号）导出 SVG/PNG 及配套 Markdown/CSV《图示引用索引附表》，新增只读审计包 CLI/MCP 命令 `export-chart-audit-bundle`。
  - **共现秒查与阅卷入图（1B）**：支持画布双主体主名＋别名全案共现秒查（同段优先＋金额/日期精确值关联），以及检索命中/原文预览/标注旁“送入关系图”一键预填建边。
  - **只读防漏审计与健康格子图（1C）**：新增材料覆盖缺口分类与健康格子图、高确定性孤儿机构差集、跨材料强指纹碰撞（手机号、统一社会信用代码、含银行上下文银行账号、电子邮箱；排除 18 位身份证号等误报）。
  - **候选主体消歧台与表格列映射（Wave 2）**：文书首部主体与简称自动提取消歧；XLSX/CSV 通用列映射导入（`Decimal` 精确计算转分，上限 50000 行，拦截覆盖已排除关系并单独汇总异常 `skipped_rows`）。
  - **外部 Skill 模板（Wave 3）**：新增 `docs/skills/chart-substantive-review/SKILL.md` 与 `docs/skills/nonstandard-table-script-sop/SKILL.md`。
- **实机联调修复**：定位并修复关系图自动排版 `save_layout` 返回 404（「主体不在本图」）缺陷（`httpExecutor.ts` 与 `graph.py` 统一兼容 `RelationGraphNode.entity_id`/`id`）。
- **验证与已知限制**：全量后端 **601 passed**、前端 **54 文件 398 passed**（`tsc --noEmit && vite build` 通过）；AC10 外部复核 Skill 实测准确检出全部 3 类预设错误；AC12 前后端独立双审闭环；隔离 Chromium 浏览器两轮 E2E 通过；正式环境升级后 `check-consistency`（`ok: true`）、40 张业务表行数与只读 HTTP 验收通过。未验证项：Safari 实机自动测试（60ms Safari 输入法保护窗口由组件单测覆盖）、760px 布局使用元素 `boundingBox().width` 与截图核对（沙箱禁 `page.evaluate`）、真实案件尚未试用。详见 [图表程序化能力验收与正式应用](docs/reports/20261122-图表程序化能力验收与正式应用.md)。

## 2026-11-22 · 关系图、证据目录、诉请金额及跨案期限正式上线

- 正式工作台从 `0006_case_archive_time` 升级到 `0008_claim_amounts`。首页启用真实跨案期限，案件工作区新增关系图、证据目录和诉请金额。
- 关系图显示实时目录编号，快照保留冻结时的编号和出处。目录可逐条复核、处理余项并保留已确认条目。定位页加载失败时可读原文件。
- 已校验升级前备份，旧案与受管文件数量一致。升级、只读验收和回退路径见 [联合验收与正式升级记录](docs/reports/20261122-P5联合隔离验收.md)。

## 2026-11-21 · 跨案期限时间轴及两项 P1 原型（源码已交付，隔离评审）

- 新增首页跨案期限时间轴与只读接口，覆盖全部在办案件。默认前 7 天至后 60 天，较早逾期另列，可筛选已完成期限并跳转案件概览。
- 新增主体关系图和诉请金额内存原型，首页设有演示入口。业务数据结构的实施从 P2 开始。
- 当时可从 [本机合成评审版](http://127.0.0.1:18170/) 打开。工作区源码可从验收记录所列基线回退，次日完成正式应用。详见 [验收记录](docs/reports/20261121-期限时间轴与可视化原型验收.md)。

## 2026-11-20 · P6 后验收：图形视图排序修复与验收脚本端口隔离（**已应用到本机运行环境**）

- 对尚未验收部分做实测验收：全量回归（后端 372、前端 170）、T19 验收脚本 13 项判定、备份恢复演练、正式服务 8765 上归档/工时/进度命令/关联/撤销/Agent 徽标/回收站端到端（TEST-ONLY 案件，真实案件只读核对未变）。证据见 [P6 后验收实测记录](docs/reports/20261120-P6后验收实测记录.md)。
- **修复 D1（用户可见）**：正式案件图形视图节点重叠——后端快照按创建序返回时子节点先于父节点，React Flow 按数组顺序解析父子关系失败。`buildGraphModel` 改为树先序重排；新增回归并反向验证。重建 `frontend/dist`（新壳 `index-CkqSnLXQ.js`）并由 8765 下发，旧产物另存 `var/backups/p6-20261120-184126/frontend-dist-pre-graphfix.tar.gz`。浏览器若仍显示旧版请硬刷新一次（⌘⇧R）。
- **修复 D2（验收脚本）**：`run_acceptance.py` 网页并发节改用空闲端口并校验子进程存活，杜绝正式服务在跑时误打生产库。
- **Safari 实机已补齐（2026-11-20 晚）**：用户开启「允许远程自动化」后，safaridriver（Safari 27.2）直连 8765 实测 14 类检查全过——图形零重叠（D1 修复生效）、归档只读、工时弹窗、`<dialog>` 模态焦点、Esc、输入法合成事件、⌘Z 撤销、黄/绿徽标、760px 半屏。截图与数据见 [P6 后验收实测记录第 6 节](docs/reports/20261120-P6后验收实测记录.md)。
- 遗留：真实中文输入法、真实案卷试用仍需用户侧条件；迁移 0006/归档守卫/工时口径的独立复核已委派，结论待回。

## 2026-11-20 · 办案进度、结案归档与工时（**已应用到本机运行环境**）

- 办案进度可视化 P2–P4：迁移 `0005_case_progress`（通用节点树、类型化关系、业务关联、变更记录、案件级序号）、进度服务与 HTTP/MCP/CLI 入口、案件进度页（大纲/图形视图、待确认面板、分区撤销）。迁移 `0006_case_archive_time` 新增结案归档（`cases.archived_at`）与工时记录（`time_entries`）。
- 结案归档是案件级操作，按钮在树状图之外；归档后进度、材料、办理信息与工时只读，可从首页隐藏并取消归档。
- 工时：计时器（0.25h 向上取整）、补录（0.25 步进）、日视图与按标题汇总，均由标题行「工时」按钮弹出。
- 后端 372 项、前端 169 项测试通过；隔离实例（端口 8799、独立库 `legal_workbench_p5`）上 Chromium 与 WebKit 双引擎实测通过；备份恢复演练覆盖归档与工时数据。证据见 [P5 隔离验收与恢复演练](docs/reports/20261120-P5隔离验收与恢复演练.md)。
- **已应用（2026-11-20）**：升级前备份 `var/backups/p6-20261120-184126/`（`complete: true`，另存旧 `frontend/dist`）；`0004_case_workbench → 0005 → 0006`，`progress backfill: 1 case(s) instantiated`；重建 `frontend/dist` 并启动 8765（uvicorn＋worker）。现有 1 个案件保留、进度树 6 个默认节点，材料与业务数据未变。只读验收通过：`/api/v1/progress/rules` 由 404 变 200，进度页在 Chromium 渲染正常，工时与结案归档入口可见。升级步骤与回退办法见 [变更记录](docs/changes/20261116-办案进度可视化.md)。
- 遗留：Safari 实机（macOS 自动化权限所限，以 WebKit 引擎替代）、真实中文输入法、真实案卷试用；本轮改动未获独立 Agent 复核（Codex 额度用尽）。

## 2026-11-15 · 前端选型与兼容修复

- 在 AGENTS.md 记录前端技术栈、浏览器兼容和本地构建规则。
- 案内检索采用原生表单提交，修复中文组词回车提前检索。材料字段使用单独的标签关联，启动样式与响应式布局归入 CSS。
- 升级至 Vitest 5.1.0 并移除 Vitest UI。Vite 6.4.6 由构建和测试共用。前端 28 项测试和构建通过，锁文件 npm 审计为 0 项。
- 新静态构建已在本机 8765 提供，浏览器只读验证通过。依赖和构建回退副本在 `/private/tmp/lwb-web-modern.s8UlXv/baseline`，旧哈希静态资源仍可读。使用真实案卷的验收按原安排暂缓。详见 [测试报告](docs/TEST_REPORT.md#前端测试)。

## 2026-11-14 · 诉讼工作台 UI 重构

- 新增案件工作表、案件概览、节点和期限登记，首页呈现下一步工作及最近一条未完成期限。
- 新增固定版本的 PDF 页和图像预览、材料取得方式，以及 Agent 与人工标注和人工复核。
- 已升级本机工作台至 `0004_case_workbench`。案件工作表通过实际页面的只读检查。应用与备份记录见 [变更记录](docs/changes/20261114-诉讼工作台UI重构.md)。
- 恢复使用升级前的数据库及受管目录备份。升级操作见 [README](README.md)。
