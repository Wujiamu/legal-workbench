# 本地法律工作台 (Legal Workbench) `v0.1.0`

本地、离线、单用户的诉讼案件材料工作台。支持材料批量入库、内容哈希去重、全离线全文与 OCR 解析、四模式精确检索与原文出处溯源、办案进度树与工时记录、主体关系图/证据目录/诉请金额及一源三态导出，并通过统一服务层向本地 Agent 提供 CLI 与 MCP（stdio）接入。

所有案件数据与文件均保存在本机（单个 PostgreSQL 18 实例 + 受管文件目录 `var/data/`），不上传任何内容，无遥测或外部云依赖。

> **适用范围**：个人或小团队在单机 macOS 或 Windows 10/11 上整理和核验诉讼案卷。**不适用**：多人在线协作、细粒度权限分级、云端同步或实时法律推理生成。

---

## 核心能力

- **诉讼案件工作表与跨案期限**：在案件概览维护当前节点、外部/内部期限与下一步工作；首页汇总全部在办案件的跨案期限时间轴（单列较早逾期，支持窗口切换与直达案件）。
- **不可变材料库与全离线解析**：
  - 支持浏览器相对路径上传与显式白名单目录导入（`LWB_IMPORT_ROOTS`），原件按内容 SHA-256 去重存放，支持创建可编辑工作副本并登记为新版本，原件与历史版本均不可覆盖；损坏文件安全隔离记录为失败状态。
  - 全离线解析 TXT（UTF-8 / GB18030）、DOCX（含嵌套表格）、XLSX（值与公式缓存）、PDF（文本层提取 + RapidOCR 扫描识别）及图片（含多帧 TIFF）。OCR 模型经 SHA-256 清单校验，运行时绝不联网下载。
- **四模式精确检索与原件核对**：支持字面、编号、金额（分币种严格隔离，支持表格拆列币种金额）与日期检索；每条命中均携带不可变 `source_ref` 与字符偏移，支持 PDF 固定页预览、原图查看及 Agent/人工标注复核。
- **办案进度树、归档与工时**：大纲与图形双视图管理「程序 → 阶段 → 工作/事件」节点树，支持模板片段插入、节点关系与材料关联、Agent 待确认变更审阅与 `⌘Z` / `Ctrl+Z` 分区撤销；支持结案归档（全局只读防穿透与取消归档）及工时计时（15 分钟向上取整）/ 0.25h 补录。
- **主体关系图、证据目录与诉请金额**：
  - **一案多图**：主名＋别名全案共现秒查、阅卷/标注一键入图、材料覆盖健康矩阵与防漏强指纹审计（孤儿机构、手机号、统一社会信用代码、银行账号、邮箱碰撞）、候选主体消歧台，以及带多币种分桶与冲突拦截的 XLSX/CSV 通用列映射导入。
  - **证据目录与诉请金额**：目录集中维护编号、绑定与冻结快照；诉请按分组和币种用整数分精确汇总本金、费用、利息、违约金与抵扣，可排除备位重叠项。
  - **一源三态导出**：关系图与诉请瀑布图支持按 `working`（内部工作）、`client`（客户沟通）、`court`（当庭呈递）导出 SVG/PNG 及配套 Markdown/CSV《图示引用索引附表》，附带 `court` 硬门禁预检与 `export-chart-audit-bundle` 只读审计包。
- **外部 Agent 接入与运维**：CLI（单行 JSON 输出）与 MCP（stdio）直连共享业务服务层；内建一致性检查（`check-consistency`）、一致性修补与全量备份/校验/空环境恢复（`backup` / `verify-backup` / `restore`）。

---

## 快速开始（macOS Apple Silicon）

### 1. 环境前提

需要本机已安装：
- macOS（Apple Silicon 已验证）
- Python 3.13、`uv`
- Node.js `22.22.2+` / `24.15.0+` / `26+` 与 `npm`
- PostgreSQL 18（默认路径 `/opt/homebrew/opt/postgresql@18/bin`，若安装在其他路径可在 `.env` 配置 `LWB_PG_BIN`）

```sh
brew install python@3.13 uv node postgresql@18
```

> 不需要执行 `brew services start`；工作台使用项目内独立数据目录 `var/postgres/`，监听 `127.0.0.1:55432`。启动前请确认 `55432`、`8765`、`5173` 端口未被占用。

### 2. 安装依赖与初始化

```sh
cd backend && uv sync --locked && cd ..
cd frontend && npm ci && npm run build && cd ..

# 下载并写入离线 OCR 模型与 SHA-256 清单到 var/models/rapidocr/（仅此一步需联网）
scripts/install_ocr_models.py

# 初始化 PostgreSQL 数据目录、生成 0600 权限 .env 凭据、建库迁移并前台启动服务与 worker
scripts/lwb init
```

- `scripts/lwb init` 仅用于全新环境；若 `.env` 或 `var/postgres/` 已存在会拒绝覆盖。
- 完成后可通过 `http://127.0.0.1:8765/` 访问工作台，或双击根目录 `法律工作台.app` 桌面启动器（若网页在线但数据库曾停止，启动器会自动尝试拉起数据库并恢复解析 worker）。

### 3. 日常维护命令

```sh
scripts/lwb start      # 拉起 PostgreSQL、执行待应用迁移，并启动 FastAPI (8765) + 单实例 worker
scripts/lwb start-db   # 仅幂等拉起本地 PostgreSQL (55432)
scripts/lwb stop       # 停止本地 PostgreSQL
scripts/lwb migrate    # 单独执行 Alembic 迁移（alembic.ini 不含连接串，读取 LWB_MIGRATION_URL）
scripts/lwb worker     # 单独运行一个解析 worker（受 PostgreSQL advisory lock 保护）
scripts/lwb doctor     # 检查 Python / Node / PostgreSQL / 数据目录 / OCR 模型 / 维护标志
```

---

## 快速开始（Windows 10/11 原生环境）

> 注意：请将项目与 `LWB_DATA_ROOT` 放在 **NTFS 本地磁盘**（无覆盖发布依赖同卷硬链接 `os.link`，请勿放到同步盘或 FAT32/exFAT）；静态数据明文保存，建议启用 Windows BitLocker 全盘加密。

### 1. 环境前提

需要本机已安装：
- Windows 10/11（x64）
- Python 3.13（含 `pythonw.exe`）、`uv`
- Node.js `22.22.2+` / `24.15.0+` / `26+` 与 `npm`
- PostgreSQL 18（默认自动探测 `C:\Program Files\PostgreSQL\18\bin` 或 `PATH`；若安装在其他目录，可在初始化前设 `$env:LWB_PG_BIN` 或写入 `.env`）

> 不需要开启 PostgreSQL Windows 系统服务；工作台使用项目内独立数据目录 `var\postgres\`，监听 `127.0.0.1:55432`。

### 2. 安装依赖与初始化（PowerShell）

```powershell
cd backend; uv sync --locked; cd ..
cd frontend; npm ci; npm run build; cd ..

# 写入离线 OCR 模型与 SHA-256 清单到 var\models\rapidocr\（仅此一步需联网）
backend\.venv\Scripts\python.exe scripts\install_ocr_models.py

# 初始化 PostgreSQL 18（UTF-8 + builtin C.UTF-8 区域设置）、生成 .env 凭据、迁移并启动
scripts\lwb.cmd init
```

- 完成后可通过 `http://127.0.0.1:8765/` 访问工作台，或**双击根目录 `法律工作台.bat`**（支持无黑框后台启动、断库自愈与独占锁防重；也可右键「发送到桌面快捷方式」）。
- 日常命令将上节 `scripts/lwb <子命令>` 替换为 `scripts\lwb.cmd <子命令>`，后端 Python 入口使用 `backend\.venv\Scripts\python.exe`。
- Windows 实机验收自检步骤见 [docs/WINDOWS_CHECKLIST.md](docs/WINDOWS_CHECKLIST.md)。

---

## 使用内置仿真案卷试用

在导入任何真实案件材料之前，强烈建议先用随仓库附带的合成全格式案卷 [acceptance_simulation](acceptance_simulation/README.md) 走通完整流程（包含扫描 PDF、加密/损坏 PDF、多币种台账、多表头对账单、DOCX 诉状与图片证据，全部为虚构 `TEST-ONLY` 数据）：

- 查看说明与生成脚本：[acceptance_simulation/README.md](acceptance_simulation/README.md)
- 如需重新生成干净测试文件：

```sh
backend/.venv/bin/python acceptance_simulation/generate_dossier.py
```

> **提示**：使用真实案件材料前，请先创建并校验一次完整备份。

---

## 架构与存储目录

HTTP（FastAPI `127.0.0.1:8765`）、CLI 与 MCP（stdio）共用同一套共享服务层，不复制业务规则：

```text
浏览器 ──HTTP(8765)──┐
外部 Agent (CLI) ────┼──> FastAPI / CLI / MCP ──> 共享服务层 ──> PostgreSQL 18 (55432)
外部 Agent (MCP) ────┘                                         └──> 受管文件 var/data/
```

本地运行目录布局（均已在 `.gitignore` 中忽略，严禁提交到公共版本库）：

```text
.env                            本地连接凭据（0600）与运行配置
var/postgres/                   PostgreSQL 18 数据目录
var/runtime/                    postgres.log、launcher.log、pgpass（0600）
var/data/cases/<case_id>/       originals/ | versions/ | working/ | derived/ | analysis/
var/models/rapidocr/            离线 OCR 模型 + manifest.json
var/backups/                    本地备份目录
```

---

## 配置项（`.env.example`）

完整环境变量模板见 [.env.example](.env.example)，环境变量优先覆盖 `.env`：

| 变量 | 默认 / 说明 |
|---|---|
| `LWB_DATABASE_URL` | 应用角色连接串（`lwb_app`，仅 DML 权限） |
| `LWB_MIGRATION_URL` | 迁移角色连接串（`lwb_migrator`，DDL 权限；缺省回退上一项） |
| `LWB_DATA_ROOT` | 受管文件根目录，默认 `var/data` |
| `LWB_IMPORT_ROOTS` | 允许服务端导入的本地目录白名单，默认空（拒绝一切服务端路径导入） |
| `LWB_HOST` / `LWB_PORT` | 默认 `127.0.0.1` / `8765` |
| `LWB_OCR_MODEL_DIR` | OCR 模型目录，默认 `var/models` |
| `LWB_MAX_FILE_BYTES` | 单文件大小上限，默认 `2147483648`（2 GiB） |
| `LWB_PARSE_TIMEOUT_SECONDS` | 单页/单单元解析超时，默认 `120` |
| `LWB_MAX_IMAGE_PIXELS` | 图像像素上限，默认 `50000000` |
| `LWB_PG_BIN` | PostgreSQL `bin` 目录路径 |

---

## 测试、一致性与备份恢复

### 自动化测试

> 后端集测 fixture 会清理并重建独立测试库 `legal_workbench_test`，运行前请勿将该连接指向业务库。

```sh
# 后端测试（需本地隔离 PostgreSQL 实例与 LWB_DATABASE_URL / LWB_MIGRATION_URL）
cd backend && .venv/bin/python -m pytest -q

# 启动器纯单测（无需数据库）
cd backend && .venv/bin/python -m pytest tests/test_lwb_launch.py -v

# 前端测试与生产构建
cd frontend && npm test && npm run build
```

### 一致性检查与备份恢复

```sh
cd backend
.venv/bin/python -m legal_workbench.cli check-consistency
.venv/bin/python -m legal_workbench.cli backup --destination /外部盘/lwb-backup
.venv/bin/python -m legal_workbench.cli verify-backup --destination /外部盘/lwb-backup
.venv/bin/python -m legal_workbench.cli restore --destination /外部盘/lwb-backup --data-root /新盘/lwb-data
```

备份过程会启用持久化维护标志（业务写入返回 `503 maintenance`）；若强行中断需先核查无残留备份进程及无 `INCOMPLETE` 标记，再执行 `clear-maintenance` 与 `check-consistency`。详情见 [docs/BACKUP_RESTORE.md](docs/BACKUP_RESTORE.md)。

---

## `v0.1.0` 发布状态与已知边界

- **当前版本基线**：`v0.1.0`（数据库迁移基线 `0008_claim_amounts`）。
- **验证覆盖**：
  - 后端：622 项通过、1 项按平台跳过（`test_real_windows_ntfs_junction_rejected`，仅在真实 Windows 运行；真实 PostgreSQL 18 隔离运行）；
  - 前端：407 项组件测试（57 个测试文件）＋ `tsc --noEmit` ＋ Vite 生产构建通过；
  - `scripts/lwb doctor` 与 `acceptance_simulation/generate_dossier.py` 跨平台字体/PDF 烟测通过；本机 macOS 实机联调与 Chromium 端到端核验通过（本地验收截图与运行日志归档于 `docs/reports/`，默认由 `.gitignore` 忽略不随仓库分发）。
- **明确边界**：
  - macOS Apple Silicon 已完成全量实机运行验证；Windows 10/11 已完成原生源码适配、桌面/命令行启动器与跨平台模拟单测，尚待在真实 Windows 宿主机按 [docs/WINDOWS_CHECKLIST.md](docs/WINDOWS_CHECKLIST.md) 完成实机端到端核验；Linux 桌面与 `pg_ctl` 路径未实机验证；不支持多用户并发协作或云同步。
  - 真实涉案材料的小规模试用、新版关系图/诉请页的 Safari 实机手测与每日定时自动备份建议在日常使用前按需补齐（详见 [docs/NEXT_STEPS.md](docs/NEXT_STEPS.md)）。
  - 历史规划中提及的「原有 22 条检索测试样本」因未取得原样本仍标为待交接。

---

## 文档导航

| 文档 | 内容 |
|---|---|
| [CHANGELOG.md](CHANGELOG.md) | 版本与交付变更流水 |
| [AGENTS.md](AGENTS.md) | 项目协作规范、四层文档权责与前端技术约束 |
| [PRODUCT.md](PRODUCT.md) | 产品定位、用户角色与交互边界 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 进程边界、模块职责、数据不变量与支持范围 |
| [docs/SCHEMA.md](docs/SCHEMA.md) | PostgreSQL 表结构、约束、状态机与审计闭集 |
| [docs/API.md](docs/API.md) | HTTP 接口契约（含 [docs/openapi.json](docs/openapi.json)） |
| [docs/AGENT.md](docs/AGENT.md) | CLI 与 MCP（stdio）接入规范与内置 Skill 说明 |
| [docs/BACKUP_RESTORE.md](docs/BACKUP_RESTORE.md) | 备份校验、空环境恢复与维护标志应急处理 |
| [docs/WINDOWS_CHECKLIST.md](docs/WINDOWS_CHECKLIST.md) | Windows 10/11 原生环境实机验收自检清单 |
| [docs/TEST_REPORT.md](docs/TEST_REPORT.md) | 测试覆盖矩阵、关键验证点与未验证边界 |
| [docs/NEXT_STEPS.md](docs/NEXT_STEPS.md) | 现行运行基线、已知限制、候选路线与技术债 |
| [docs/REUSE.md](docs/REUSE.md) | 第三方依赖清单与许可证信息 |

---

## 开源协议

本项目基于 [MIT License](LICENSE) 开源。
