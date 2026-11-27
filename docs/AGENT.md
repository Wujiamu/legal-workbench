# Agent 接入说明（CLI 与 MCP）

本工具为本地单用户案件工作台。Agent 通过两条通道调用同一业务服务：CLI（JSON 输出）与 MCP（stdio）。两条通道都不提供删除原件、执行任意 SQL 或覆盖恢复备份的能力。

## 前置

1. 首次按 README 安装并运行 `scripts/lwb init`（Windows 为 `scripts\lwb.cmd init`）。日常双击 `法律工作台.app`（Windows 为 `法律工作台.bat`）或运行 `scripts/lwb start` / `scripts\lwb.cmd start` 启动数据库与服务。
2. 需要索引材料时运行 worker：`backend/.venv/bin/python -m legal_workbench.worker --once`（Windows 为 `backend\.venv\Scripts\python.exe -m legal_workbench.worker --once`），或在长驻终端运行 `--once` 省略版本。
3. 服务器路径导入须在 `.env` 的 `LWB_IMPORT_ROOTS` 中显式列出允许目录（系统路径分隔符分隔）；未配置时一切服务器路径导入都会被拒绝。

## CLI

本机 Codex、Hermes 和 WorkBuddy 可使用 `legal-workbench-cli` 技能调用同一个 CLI。在对话中说「用 legal-workbench-cli 检查法律工作台是否连通，只返回验证结果」。连通性检查用 `list-cases --limit 0`，不返回案件条目。

```sh
cd backend
.venv/bin/python -m legal_workbench.cli <command> [options]          # macOS / Linux
# .venv\Scripts\python.exe -m legal_workbench.cli <command> [options] # Windows
```

- stdout 只有一行 JSON；日志与错误写 stderr。
- 成功退出码 0；业务错误退出码 2（stderr 为 `{code, message, details}`）；未预期错误退出码 1。
- 所有案件操作必须显式传 `--case-id`，没有"当前案件"。

常用子命令：

| 目的 | 命令 |
|---|---|
| 建案 | `create-case --title "…"` |
| 列案 | `list-cases --limit 20` |
| 导入 | `import --case-id ID --source-path /abs/dir --source-label L --request-key K` |
| 列材料 | `list-materials --case-id ID` |
| 改分类 | `update-classification --case-id ID --material-id M --category C --expected-revision R` |
| 检索 | `search --case-id ID --query 合同 --mode literal` |
| 金额检索 | `search --case-id ID --query 100 --mode amount --currency CNY` |
| 读来源 | `read-source --case-id ID --source-ref REF --context-chars 200` |
| 存分析 | `save-analysis --case-id ID --title T (--content 'Markdown 正文' \| --content-file path.md) --statement-kind agent_analysis --source-ref REF` |
| 任务状态 | `job-status --case-id ID --job-id J` |
| 跑队列 | `run-worker` |

`--request-key` 是幂等键：同一键重复导入返回同一 `intake_id` 与 `job_id`，不会新建批次。要新批次就换新键。

## MCP

```sh
cd backend
.venv/bin/python -m legal_workbench.mcp_server          # macOS / Linux
# .venv\Scripts\python.exe -m legal_workbench.mcp_server # Windows
```

stdio 传输；stdout 仅协议数据，日志走 stderr。客户端配置示例（Claude Desktop / Codex 等 `mcpServers` 条目；Windows 下将 `command` 改为 `.../backend/.venv/Scripts/python.exe`）：

```json
{
  "mcpServers": {
    "legal-workbench": {
      "command": "/path/to/legal-workbench/backend/.venv/bin/python",
      "args": ["-m", "legal_workbench.mcp_server"],
      "cwd": "/path/to/legal-workbench/backend",
      "env": { "PYTHONPATH": "/path/to/legal-workbench/backend/src" }
    }
  }
}
```

暴露的工具：`create_case`、`list_cases`、`import_materials`、`list_materials`、`update_classification`、`create_working_copy`、`register_file_version`、`search_case`、`read_source`、`get_job_status`、`get_index_status`、`save_analysis`、`review_analysis`、`get_analysis_version`。

所有带 `case_id` 的工具（当前 52 个）在进入任何数据库读取或业务服务调用**之前**先过访问门禁，详见下节「Agent 访问门禁」。`create_case` 默认被拒。新增工具若带 `case_id` 却漏了门禁，MCP 服务在导入阶段就启动失败（`mcp_server._enforce_case_fence`）。

进度工具（P3）：`get_progress`、`get_progress_rules`、`progress_add_node`、`progress_update_node`、`progress_set_active_stage`、`progress_move_node`、`progress_insert_fragment`、`progress_remove_node`、`progress_add_relation`、`progress_link_material`。**不提供确认与撤回**——Agent 的进度修改默认待确认，由承办人在网页确认或撤回。`progress_add_node` 必须带 `request_key`（幂等）。CLI 对应子命令：`progress`、`progress-rules`、`progress-add-node`、`progress-set-active-stage`、`progress-insert-fragment`。

失败时返回结构化错误（`isError: true`，正文含错误码），不会把失败伪装成成功文本。

## 按需裁剪工具集（模块开关）

平台当前注册 **57 个**工具，分属 7 个模块：`core`（12，材料/检索/出处/访问自检）、`notes`（5，标注与分析）、`progress`（14）、`graph`（13）、`evidence`（6）、`claims`（6），以及归属 `graph`+`claims` 任一即可的 `export-chart-audit-bundle`（1）。

只给 Agent 它用得上的那部分，有两种等价方式：

```sh
# 1. 命令行（优先级最高，只影响本次进程）
.venv/bin/python -m legal_workbench.mcp_server --modules core,evidence

# 2. 环境变量（写进客户端配置或 .env）
LWB_MCP_MODULES=core,evidence
```

规则：

- 省略 `--modules` 时读 `LWB_MCP_MODULES`；两者都不设时等同 `all`，工具清单与描述与裁剪前逐字相同。
- `core` 始终被加入且排在首位；模块名不区分大小写，未知模块名直接报 `invalid_input`，不静默丢弃。
- 裁剪只影响**暴露哪些工具**，不影响访问门禁：复制出去的工具仍是带 `@_case_guard` 的同一函数，未授权案件照样 `forbidden`。
- 裁剪只作用于 MCP：`LWB_ENABLED_MODULES` 控制网页（HTTP）端的功能面，两者可分别设置（例如网页全开、只给 Agent 材料与检索）。

标注工具为 `annotate_file` 和 `list_annotations`。登记时提供同案 `case_id`、固定的 `file_version_id`、`kind`（`transcription` 或 `interpretation`）和 `body`。对 PDF 可指定从 1 开始的 `page`。Agent 标注默认待人工复核，`reviewed_text` 为空。承办人在网页对照原件并确认或修订，原标注仍保留。后续重新识别可登记一条待复核记录。

## Agent 访问门禁（默认锁死＋单案授权）

MCP 层默认**拒绝一切案件访问**。策略文件 `<工作台目录>/var/runtime/agent-access.json` 不存在时即 `deny_all`；源码与生产环境都不预先生成它。放行只能由承办人在本机终端按案打开：

```sh
scripts/lwb agent-status                  # 当前模式、已授权案件列表、风险提示
scripts/lwb agent-grant <case_id>         # 临时放行单个案件（切到 allowlist）
scripts/lwb agent-revoke <case_id>        # 收回该案；白名单清空即自动回到 deny_all
scripts/lwb agent-lock                    # 立即全局锁死、清空白名单、关闭建案权限
scripts/lwb agent-allow-all --confirm     # 对所有案件开放（风险，仅限本地离线模型）
```

（Windows 为 `scripts\lwb.cmd`；打包安装的目录里没有 `scripts/`，改用 `<可执行文件> --run-cli agent-status|agent-grant <case_id>|…`，实现同一份 `agent_access_cli`。）`agent-allow-all` 不带 `--confirm` 时拒绝执行并退出码 2；`agent-grant` / `agent-revoke` 对非法 UUID 直接拒绝，不写文件。

三种模式的 MCP 行为：

| 模式 | `list_cases` | 其余带 `case_id` 的工具 | `create_case` |
|---|---|---|---|
| `deny_all`（默认） | 仅 `limit=0` 返回空列表作连通探活；`limit>0` 返回 `forbidden` | 一律 `forbidden` | `forbidden` |
| `allowlist` | 只返回白名单内且确实存在的案件，`total` 仅等于已授权案件数 | 白名单内正常，其余 `forbidden` | `forbidden`（除非 `allow_create_case=true`） |
| `allow_all` | 原全库行为 | 全部放行 | 需 `allow_create_case=true` |

`get_progress_rules`、`get_graph_rules` 不含案件数据，始终可只读；`get_access_policy` 只读自检，返回 `mode`、`allowed_case_count`、`allow_create_case`、`source`（`policy_file` / `env` / `default_deny`）与授权命令说明，**不返回案件标题或 case_id**。

**MCP 侧没有任何授权、解锁或改写策略的工具**，这是刻意的：授权必须由人完成。错误信息里给出可直接照抄的 `agent-grant` 命令，且不泄露未授权案件的标题或元信息。

策略文件结构（原子写入、`0600`、无凭据、无案件正文）：

```json
{ "mode": "allowlist", "allowed_case_ids": ["<uuid>"], "allow_create_case": false,
  "updated_at": "2026-11-25T03:34:39Z" }
```

文件损坏、无法解析、缺字段或含非法 UUID 时一律安全降级为 `deny_all`。

环境变量（供受控单案启动与测试）：`LWB_AGENT_ACCESS_FILE` 改策略文件路径；`LWB_MCP_ALLOWED_CASES`（逗号分隔 UUID，非法值显式报错）与 `LWB_MCP_ACCESS_MODE`（`deny_all` / `allowlist` / `allow_all`）覆盖为对应策略。两者同时存在时取更严格的一个；**文件策略优先于环境变量**，所以 `agent-lock` 不会被某个残留 shell 变量撤销。

## 通用插件包接入（Codex / MCP clients）

除手工填写 `mcpServers` 外，仓库自带一个便携 Agent 插件包 [plugins/plainfolio](../plugins/plainfolio/README.md)，把同一个 stdio MCP 服务与三个业务 Skill 一起分发给 Codex、DSH（DeepSeek Harness）、Claude Desktop / Antigravity 等客户端。**不再提供常驻的 DSH Cordis 插件**（早期 `plugins/dsh-plainfolio` 已废弃移除：常驻宿主插件与高保密案件按需隔离的原则冲突）：

- `plugin.json`（便携 Agent Plugins 清单，保留 Codex 兼容的 `skills` / `mcpServers` 字段）、`mcp.json` 与 `.mcp.json`（同一个 `plainfolio` stdio 服务）、`skills/`（`plainfolio-workbench`、`chart-substantive-review`、`nonstandard-table-script-sop`）。内部端到端验收规程不随插件分发。
- `scripts/run-mcp.py`：纯标准库启动器。按 `LWB_HOME` → 实例指针 `~/.plainfolio/instance.json`（`project_root` 优先、其次 `home`）→ 脚本所在仓库 的顺序定位工作台；优先使用工作台的虚拟环境解释器（`backend/.venv/bin/python`，Windows 为 `backend\.venv\Scripts\python.exe`，也接受仓库根 `.venv`），找不到时回退 PATH 中的 `lwb-mcp`；`LWB_MCP_AUTO_START_DB=1` 时先执行 `scripts/lwb start-db`，其输出全部重定向到 stderr。stdout 严格只留 MCP 协议。
- 本地插件市场清单：`.agents/plugins/marketplace.json`，`codex plugin marketplace add <仓库目录>` 即可添加。

接入前先在本机登记一次实例指针，插件与工作台本体据此解析到同一安装：

```sh
scripts/lwb register-instance          # macOS / Linux
scripts\lwb.cmd register-instance      # Windows
```

该命令把 `home`、`project_root` 与 `updated_at` 写入 `~/.plainfolio/instance.json`（`LWB_INSTANCE_CONFIG` 可改路径），**不写数据库口令或任何凭据**。工作台本体按同一优先级解析目录：`LWB_HOME` → 实例指针 `home`（须为已存在目录）→ 源码仓库根（`config.ROOT`）。

隐私边界与人工把关与 CLI / MCP 完全一致：素章本身离线，但一旦接入云端大模型，工具返回的案卷片段就会进入该模型上下文；Agent 写入一律为待确认提案，确认、撤回与复核仍只在网页由承办人完成。客户端配置示例（Codex、DSH、Claude Desktop）见 [插件包 README](../plugins/plainfolio/README.md)。

在 DSH 中按需临时使用时，走通用 `@deepseek-ai/dsh-mcp-client` stdio 配置（不装常驻插件、不注册设置页面板），并且必须同时满足：由承办人在本机 `agent-grant` 只放行本次要用的那一个案件、该案卷允许接触当前所连接的模型（**涉密或禁止上云的案卷只能用纯本地离线模型**）、会话结束后 `agent-revoke` 或 `agent-lock` 收回权限。

## 引用纪律

- `source_ref` 是不透明 UUID，指向不可变片段；解析成功只证明定位存在，不证明观点正确。
- 保存分析时把用到的 `source_ref` 全部列入 `source_refs`；跨案引用会被拒绝（`case_mismatch`）。
- 检索命中的 `match_start`/`match_end` 是相对 `context` 的 Unicode 码点偏移；用 JavaScript 时须先转换，不能直接 `slice`。

## 并发与冲突

材料、分析、工作副本都带 `revision`。写入须带 `expected_revision`；过期返回 `conflict`（HTTP 409 / CLI 退出码 2），此时重新读取最新 revision 再提交，不要盲目重试同一数值。

## 关系图、证据目录与诉请金额

CLI 用 `graph --case-id <UUID>`、`evidence-catalog --case-id <UUID>` 和 `claims --case-id <UUID>` 读取工作区。`claim-candidates` 只返回本案可追溯的金额候选。查阅 `--help` 获得字段及命令一览。MCP 提供同一服务层的读取和提案入口。

新增或更新时按命令携带幂等键和当前修订号。诉请金额使用整数分，币种与分组一致。Agent 写入以待确认提案留痕，确认与撤回由人员在工作台完成。引用遵守上文的 `source_ref` 规则。

## 图表对外审计包与外部 Skill 模板

- **只读审计包（CLI 与 MCP）**：
  - CLI 子命令：`export-chart-audit-bundle --case-id <UUID> --chart-type graph|claims --target-id <UUID> --profile client|court [--snapshot-id <UUID>]`
  - MCP 工具：`export_chart_audit_bundle`（注册工具名 `export-chart-audit-bundle`，参数同上）
  - 该入口严格只读并校验 `case_id` 归属（跨案拒绝），返回对应图/诉请组的对外断言（已排除待定提案、`ruled_out` 与内部备注）、最新正式证据编号、`segment_id` 原文上下文及程序硬门禁 `preflight` 结果；Agent 不得跳过硬门禁豁免逻辑。
- **配套外部 Skill 模板**：
  - [chart-substantive-review](skills/chart-substantive-review/SKILL.md)：约定 4 步实质复核 SOP，消费 `export-chart-audit-bundle` 检查文图字面/金额/日期一致性、证明力越界与口径自认预警，结论交承办人终审。
  - [nonstandard-table-script-sop](skills/nonstandard-table-script-sop/SKILL.md)：遇到多表头/非标银行流水时，约定外部 Agent 先读取前 20 行样本，编写使用 `Decimal` 转分的透明本地 Python 聚合脚本计算汇总与 `skipped_rows`，再经 `save-analysis` 留痕并按幂等键写入工作台待确认提案。
