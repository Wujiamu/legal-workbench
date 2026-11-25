# 架构

当前正式运行基线为 `v0.1.0` + `0009_case_metadata`（2026-11-24：素章 · PlainFolio 品牌重塑与五套实时主题切换，零新增迁移）。应用与验收状态见 [后续步骤总台](NEXT_STEPS.md)。

本地、单用户、离线的案件材料工作台。一个 PostgreSQL 18 实例、一个 Python 服务进程、一个单 worker、一个静态前端；没有容器、云服务、遥测或外部 SaaS。

## 进程与边界

```
┌──────────────────────────── 本机 127.0.0.1 ────────────────────────────┐
│                                                                        │
│  浏览器 ──HTTP(8765)──> FastAPI 应用 ──┐                                │
│                                        │                               │
│  外部 Agent ──stdio──> MCP 服务 ───────┤                               │
│  外部 Agent ──exec───> CLI ────────────┤                               │
│                                        ▼                               │
│                            共享服务层（cases/materials/imports/         │
│                            parsing/search/analysis/source_notes）                   │
│                                        │                               │
│                          ┌─────────────┴──────────────┐                │
│                          ▼                            ▼                │
│            PostgreSQL 18 (55432)              受管文件 var/data        │
│            单实例、三角色、advisory lock       原样字节 + 派生文本      │
└────────────────────────────────────────────────────────────────────────┘
```

三个入口（HTTP、MCP、CLI）都直接调用同一套服务函数，不复制业务逻辑，也不各自直连数据库。MCP 走 stdio，由调用方进程直接拉起，不监听端口。

## 模块

| 模块 | 职责 |
|---|---|
| `cases.py` | 案件列表、Dashboard 排序、办理信息 CAS；建案同事务实例化默认进度片段（K14） |
| `progress.py` / `progress_rules.py` | 办案进度命令服务（节点树/关系/关联/变更记录/撤回）与规则配置；写命令固定顺序：写锁→锁案序号→同案与规则校验→CAS→变更记录→序号+1→审计→提交→快照 |
| `source_notes.py` | 同案固定文件版本标注，Agent 原文及一次性人工复核 |
| `graph.py` | 案件主体、别名、关系及出处，具名图和布局，冻结快照、同意记录及关系图变更复核 |
| `graph_rules.py` | 主体和关系类型、关系可信状态、快照阶段、同意渠道以及 Agent 命令范围 |
| `evidence_catalog.py` | 证据目录分组、条目与材料页码绑定，合规导入、复核、现行编号解析 |
| `dossier_export.py` | 按中国法院纸质装订与电子立案标准生成双场景证据卷宗 ZIP（`00_证据合订卷宗.pdf`、`01_证据目录.docx/.csv`、`02_分项证据材料/*`） |
| `claims.py` | 诉请金额分组、项目及同案出处，按币种汇总整数分和抵扣，记录可复核变更 |
| `chart_export.py` | 跨图表“一源三态”（`working`/`client`/`court`）引用索引附表生成、硬门禁预检 `preflight_chart_export` 与只读复核审计包 `build_chart_audit_bundle` |
| `graph_assist.py` | 只读关系图程序化助手：双主体全案共现、片段主体/精确值匹配、材料覆盖健康矩阵、孤儿机构差集、强指纹碰撞、候选主体消歧提取 |
| `table_graph_import.py` | 通用 XLSX/CSV 列映射预览、上传解析、编译与幂等入图提案（`Decimal` 转整数分、`skipped_rows` 异常报告） |
| `domain_changes.py` | 关系图、证据目录和诉请金额共享的域序号事务、变更差异、待确认记录与复核状态 |
| `time_entries.py` | 案件工时开始、停止、补录、软删除与日汇总，跨日切段及 15 分钟计量 |
| `config.py` | 读取 `.env` 与环境变量；`Settings` 中所有凭据字段 `repr=False` |
| `runtime_paths.py` | 只读程序目录与可写数据目录的解析：打包根（`LWB_BUNDLE_ROOT` → 冻结资源根 → 仓库根）、平台用户数据目录、前端/Alembic/内嵌 PG18 与 OCR 模型的位置；平台数据目录**仅在打包态**启用 |
| `desktop_launcher.py` | 冻结桌面入口：`--run-server` / `--run-worker` / `--run-mcp` / `--run-cli` / `--run-doctor` 自调用，首次自动建库、升级前备份＋迁移、单实例文件锁、`--status` / `--stop` / `--headless` / `--init-only` |
| `db.py` | 引擎/会话、`begin_write`（维护共享锁）、`update_with_revision`（CAS） |
| `models.py` | 全部 ORM 实体；案件子表一律带 `(case_id, …)` 组合外键 |
| `storage.py` | 流式哈希、同盘暂存、`fsync` + `os.link` 无覆盖发布（POSIX / NTFS）、Windows 保留名清洗、回读校验、路径穿越与链接/Junction 拒绝 |
| `imports.py` | 导入批次、逐项状态机、上传批次、幂等 `request_key` |
| `materials.py` | 分类/显示名 CAS、工作副本、版本登记（同内容复用 `StoredObject`） |
| `jobs.py` / `worker.py` | 任务队列、`FOR UPDATE SKIP LOCKED` 领取、advisory lock 保证单 worker |
| `parsing/` | TXT/DOCX/XLSX/PDF/图片解析、本地 RapidOCR、逐 unit checkpoint、coverage 发布 |
| `search.py` | literal/identifier/amount/date 四种模式；金额与日期命中只来自 `ExactValue` |
| `analysis.py` | 不可变分析版本、`source_refs` 同案校验、审核状态 |
| `coverage.py` | 覆盖统计：把"从未解析"与"解析失败/不支持"分开报告 |
| `api/` | 路由、请求守卫（本机 Host 与同源 Origin）、文件响应头 |
| `agent_access.py` | Agent 访问策略：`deny_all` / `allowlist` / `allow_all` 三态、单案白名单读写（`var/runtime/agent-access.json`，原子写入 + `0600`，无凭据）、`require_case_allowed` / `require_create_case_allowed` 硬门禁；文件损坏一律降级 `deny_all`，文件策略优先于环境变量 |
| `cli.py` / `mcp_server.py` | 外部 Agent 入口。`mcp_server` 的全部 52 个带 `case_id` 工具都套 `_case_guard`（先校验 UUID 与授权，再开会话），导入期 `_enforce_case_fence()` 缺一个即启动失败；**不暴露任何授权/解锁工具**，只有只读自检 `get_access_policy` |
| `consistency.py` / `backup.py` | 只读一致性检查、备份/校验/恢复 |
| `frontend/src/api/types.ts` | 由 `docs/openapi.json` 生成，是前端类型的唯一来源；`endpoints.ts` 只做别名，不再手写字段 |

PDF 页预览复用存储完整性校验及 PDFium。来源读取使用历史解析片段的文件版本和页码，材料取得方式来自当前材料登记。MCP 与网页调用同一标注服务。见 [案件期限与证据标注](decisions/20261114-案件期限与证据标注.md)。

## 数据不变量

1. **案件隔离由数据库强制**。每张案件子表都有 `(case_id, …)` 组合外键，跨案引用会被 PostgreSQL 拒绝，而不是靠服务层记得检查。服务层仍会核对并返回 `case_mismatch`，两层都在。
2. **原件不可变**。发布用 `os.link`，目标已存在即失败，绝不覆盖；同内容重复发布复用一个 `StoredObject`（每案 `(case_id, sha256)` 唯一）。
3. **可变行带 `revision`**，更新走 `WHERE revision = expected` 的 CAS，过期即 `conflict`。
4. **文本位置指向不可变 `Segment`**。重解析产生新 `ParseRun` 与新 `Segment`；旧的 `source_ref` 仍能读到旧版本内容，历史引用不会因重做 OCR 而失效。
5. **金额与日期只来自 `ExactValue`**。不用正则从正文里"猜"数字，避免把 `1000` 当成 `100` 或把 `100 USD` 并进 CNY。
6. **崩溃可恢复**。导入项 `pending → copying → published → ready` 每阶段独立提交；解析逐 unit checkpoint；重试复用同一 `job_id`。
7. **进度命令化、变更记录与序号同事务**。每条进度写命令在同一短事务内写节点 CAS、变更记录（修改前/后值与修订号）、案件变化序号 +1 与审计；Agent 变更默认待确认，确认/撤回仅人工入口。同案写入由 `case_change_seq` 行 `FOR UPDATE` 串行化。层级/种类/状态只在 `progress_rules` 配置，数据库不写死。见 [界面与 Agent 共用业务命令](decisions/20261116-界面与Agent共用业务命令.md)、[进度通用节点模型](decisions/20261116-进度通用节点模型.md)。
8. **单 worker**。`pg_try_advisory_lock` 抢不到立即退出；启动时把遗留 `running` 任务改回 `queued`。
9. **写入顺序固定**：维护共享锁 → worker 会话锁 → 短事务。备份取维护排他锁，因此备份期间业务写入返回 `maintenance`。
10. **Agent 默认无权限**。MCP 层的授权判断在数据库之前，且不依赖数据库状态：策略文件缺失或损坏都是 `deny_all`。授权状态与案件数据分开存放（`var/runtime/agent-access.json`），因此备份、恢复与迁移都不会把「某次临时授权」带进新环境。

## 文件布局

```
var/
  postgres/                 PostgreSQL 数据目录（脚本创建，不托管给 brew services）
  runtime/                  postgres.log、pgpass(0600)、agent-access.json(0600，仅存在时；默认不存在)
  data/                     受管文件根（DATA_ROOT）
    cases/<case_id>/originals/<sha256>/source<ext>
    cases/<case_id>/versions/<file_version_id>/
    cases/<case_id>/working/<working_copy_id>/
    cases/<case_id>/derived/<parse_run_id>/
    cases/<case_id>/analysis/<analysis_version_id>/content.md
    staging/<operation_id>/
  models/rapidocr/          det.onnx、rec.onnx、cls.onnx、manifest.json
```

原件按内容哈希存放，因此同案两份相同文件只占一份磁盘，但仍是两条材料、两个版本，各自可独立分类与重命名。

打包态下这套 `var/` 整体位于平台用户数据目录（macOS `~/Library/Application Support/PlainFolio`，Windows `%LOCALAPPDATA%\PlainFolio`），程序文件在只读的安装目录内；两者的解析与首次初始化、升级前备份流程见 [桌面安装包文档](PACKAGING.md)。

## 外部 Agent 的数据流向

Agent 通过 CLI 或 MCP 调用，进程在本机运行，读的是同一套数据库与受管目录：

- 传入：案件 ID、材料 ID、查询词、分析正文、`source_ref`。
- 传出：材料摘要、检索命中与上下文、提取文本、分析版本正文、任务状态。
- 不传出：数据库连接串或凭据、其他案件的内容。
- Agent 无法：删除原件、执行任意 SQL、覆盖式恢复备份、跨案读取（数据库与服务层都拒绝）。
- **默认锁死**：MCP 层在进入任何数据库读取前先过 `agent_access` 门禁。策略文件不存在即 `deny_all`，除 `list_cases(limit=0)` 探活与两组规则外一律 `forbidden`；`allowlist` 下 `list_cases` 只返回已授权案件，`total` 只计授权范围，不泄露库中其他案件的存在与数量。授权只能由承办人在本机执行 `scripts/lwb agent-grant <case_id>`，用完 `agent-revoke` / `agent-lock` 收回。见 [Agent 访问硬围栏](changes/20261125-Agent访问硬围栏与授权收紧.md)。
- 边界说明：工作台不内置 LLM 调用或任意代码执行沙箱；图表实质复核（[chart-substantive-review](skills/chart-substantive-review/SKILL.md)）与非标表格外部 Python 聚合（[nonstandard-table-script-sop](skills/nonstandard-table-script-sop/SKILL.md)）均通过 `docs/skills/` 模板交由外部 Agent 侧执行，经 `export-chart-audit-bundle` 与幂等待确认命令交互。

## 支持范围

- 分发形态：源码态（现有方式）与桌面安装包（macOS `.dmg` / Windows `.exe`）。打包工程已实现并通过干跑校验，**未在真机构建、签名或安装**，实机清单见 [PACKAGING.md](PACKAGING.md)。
- 平台：macOS（Apple Silicon 已实机验证）；Windows 10/11 已完成跨平台代码适配、桌面与生命周期启动器（`法律工作台.bat` / `scripts/lwb.cmd`）、线程版 OCR 超时保护及模拟单测，待在真实 Windows 宿主机按 [WINDOWS_CHECKLIST.md](WINDOWS_CHECKLIST.md) 执行实机验收；Linux 未验证。
- 规模：单机单用户。实测 1000 文件 / 10000 页 / 33100 段落 / 120305 个精确值，索引 128 秒，检索 P95 415ms（单字扫描 545ms）。
- 并发：单 worker 串行处理；多客户端可并发读，写走 CAS 与数据库锁。
- 未覆盖：多用户、权限分级、跨机同步、云存储、加密静态数据、审计合规留存。
