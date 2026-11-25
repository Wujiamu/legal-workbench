---
name: plainfolio-workbench
description: 用素章（PlainFolio）本地法律工作台办案：在用户明确授权具体案件并由本机 agent-grant 放行后，通过 MCP 工具（list_cases、search_case、read_source、save_analysis、export_chart_audit_bundle 等）或 CLI 查询案件、材料、索引与原文出处，提交待确认提案并留痕。用户提到素章、PlainFolio、法律工作台，或要求查案卷、找原文、写办案分析、导图表复核包时使用。工作台默认锁死（deny_all），未获授权前禁止连接、探测或列举任何案件。
---

# 素章（PlainFolio）办案操作指南

素章是一台电脑上、离线运行的单用户案件工作台：PostgreSQL 存案件与索引，`var/data` 存原件，网页在 `http://127.0.0.1:8765`，Agent 通过 **MCP（stdio）** 或 **CLI（单行 JSON）** 调用同一套服务。原件只读、按内容哈希发布，没有删除原件、任意 SQL、覆盖式恢复的入口。

## 授权前置与硬围栏纪律（必须先执行）

1. **禁止默认连接、探测或列举工作台**。即使对话里提到案卷，在用户明确授权前也不得调用 `list_cases`、`list-cases`、`search_case` 或任何带 `case_id` 的工具与命令。工作台默认 `deny_all`（全局锁死），未获授权时除 `list_cases(limit=0)` 连通探活外一律返回 `forbidden`。
2. **先向用户申请明确授权**，一次问清三件事：
   - 本次允许访问的**具体目标案件**（`case_id` 或案件名称）；
   - 当前所连接的对话模型**是否合规**接触该案材料——涉密、涉个人隐私或当事人禁止上云的案卷，**严禁**接入云端模型，只能用本机离线模型；
   - 请用户**本人在本机终端**执行 `scripts/lwb agent-grant <case_id>`（Windows 为 `scripts\lwb.cmd agent-grant <case_id>`；桌面安装包用 `<可执行文件> --run-cli agent-grant <case_id>`）临时放行该单案。授权只能由承办人本人执行，你不得代跑，也不得要求用户把命令交给你来执行。
3. **禁止绕过硬围栏**。若工具返回 `forbidden`（未获授权或处于 `deny_all`），立即停止该案的全部操作并如实告知用户「需由您在本机授权」。**严禁**改用 Shell / Python 直连数据库、读取 `var/data`、读取 `.env`、改写 `var/runtime/agent-access.json`，或改走 CLI / HTTP / 另一个客户端绕过门禁；这些做法一律视为越权。
4. **用完即锁**。任务结束回复用户时，提醒用户按需执行 `scripts/lwb agent-revoke <case_id>`（Windows 为 `scripts\lwb.cmd agent-revoke <case_id>`）收回该案权限，或执行 `scripts/lwb agent-lock` 全局锁死、收回全部权限。不要替用户判断「下次还要用，先留着」。

## 一、隐私边界（先读这一段）

- 素章本身**完全离线**：不联网、无账号、无遥测，材料与索引都只在本机硬盘。
- **一旦把素章接进云端大模型，工具返回的内容就进入该模型的上下文。**`search_case` 的命中上下文、`read_source` 的原文片段、`get_progress` / `get_graph` / `get_claims` 的主体与金额、`save_analysis` 的正文，都会随本次会话上传到你接入的那个模型服务。
- 因此：涉密、涉个人隐私、当事人要求不出境/不上云的案卷，只在**本地模型**或**不接 Agent** 的纯网页模式下处理；接云端模型前，先确认该案允许这样处理，并只检索必要的最小片段，不要把整案正文批量灌进对话。
- 客户端配置、实例指针文件（`~/.plainfolio/instance.json`）只存路径与时间戳，不含数据库口令；不要把 `.env`、连接串、口令贴进对话或写进任何笔记。

## 二、人工把关红线（不可越过）

1. **Agent 只提交待确认提案**：进度节点、主体/关系、证据目录条目与绑定、诉请金额的新增与修改，全部进入待确认收件箱（`pending_changes`），**不计入**正式编号、小计与总计。
2. **确认、撤回、复核、冻结、同意、归档只能由承办人在网页完成**：MCP 与 CLI 都没有这些入口，不要尝试构造请求绕过，也不要让用户「口头授权」后由你代点。
3. **不删原件、不改原件、不做覆盖式恢复**：没有这些工具。工作副本可以改，改完登记为新版本，旧版本与历史 `source_ref` 永远可读。
4. **结论归承办人**：你的输出是待核对的草稿与线索，不是法律意见；交付时明确写出「须由承办人核对原件后确认」。

## 三、连接方式

### MCP（首选）

客户端已按插件包配置好 `plainfolio` 服务时即可调用工具，但**先完成上面的授权前置**：未获单案授权时，除探活与策略自检外都会返回 `forbidden`。工具名与用途：

| 目的 | 工具 |
|---|---|
| 连通性探活 | `list_cases(limit=0)`：唯一在未授权时也可用的调用，返回空列表 |
| 当前策略自检 | `get_access_policy`（只读；不返回案件标题，也不提供任何授权/解锁入口） |
| 列案 | `list_cases(limit>0)`：仅在已授权该案后可用；`allowlist` 下只返回已授权案件 |
| 建案 | `create_case`：**默认禁止**；须承办人在本机显式开启建案权限后才可用 |
| 导入材料 | `import_materials`（仅 `LWB_IMPORT_ROOTS` 允许的服务器路径） |
| 列材料/改分类 | `list_materials`、`update_classification` |
| 检索 | `search_case`（`literal` / `identifier` / `amount`（需 `currency`）/ `date`） |
| 读原文 | `read_source`（按 `source_ref` 取不可变片段与上下文） |
| 任务与索引 | `get_job_status`、`get_index_status` |
| 分析留痕 | `save_analysis`、`get_analysis_version`、`review_analysis` |
| 标注 | `annotate_file`、`list_annotations` |
| 办案进度 | `get_progress`、`get_progress_rules`、`progress_*` |
| 关系图 | `get_graph`、`get_graph_rules`、`graph_*`、`graph_removal_impact` |
| 证据目录 | `get_evidence_catalog`、`evidence_*` |
| 诉请金额 | `get_claims`、`claim_amount_candidates`、`claim_*` |
| 图表复核包 | `export-chart-audit-bundle`（函数名 `export_chart_audit_bundle`；严格只读） |

找不到工作台时（服务启动即失败、stderr 有安装指引），让用户在工作台目录执行一次 `scripts/lwb register-instance`（Windows 为 `scripts\lwb.cmd register-instance`），或设置 `LWB_HOME` 指向工作台目录。

### CLI（兜底）

MCP 不可用、或需要脚本化批处理时，在工作台目录运行：

```sh
backend/.venv/bin/python -m legal_workbench.cli <子命令> [参数]           # macOS / Linux
backend\.venv\Scripts\python.exe -m legal_workbench.cli <子命令> [参数]   # Windows
```

装了入口脚本时也可以用 `lwb-cli <子命令>`。约定：stdout 单行 JSON（退出码 `0`）；业务错误为 `{code, message, details}` 写 stderr（退出码 `2`）；未预期异常退出码 `1`。`lwb-doctor` 检查本机依赖，`lwb-mcp` 直接起 MCP 服务。

CLI 与本机网页不受 Agent 门禁约束（那是承办人自己的终端），但**你不得把 CLI 当作绕过 MCP `forbidden` 的手段**：只有在用户明确授权该案、并已说明由你在本机终端执行时，才可用 CLI 访问该案。

常用：`list-cases --limit 20`、`search --case-id <UUID> --query 合同 --mode literal`、`read-source --case-id <UUID> --source-ref <UUID>`、`save-analysis --case-id <UUID> --title ... --content-file body.md --statement-kind agent_analysis --source-ref <UUID>`、`export-chart-audit-bundle --case-id <UUID> --chart-type graph|claims --target-id <UUID> --profile client|court`。

## 四、办案纪律

- **所有案件操作必须显式带 `case_id`**，没有「当前案件」；跨案引用会被拒绝（`case_mismatch`），不要重试。
- **写操作带幂等键**：`request_key` 相同、命令相同则返回原结果；命令不同则冲突。换内容就换新键。
- **写操作带 `expected_revision`**：过期返回 `conflict`，重新读快照取最新修订号再提交，不要盲目重试同一数值。
- **引用纪律**：`source_ref` 是不透明 UUID，指向不可变片段；保存分析时把用到的 `source_ref` 全部列入 `source_refs`。解析成功只证明定位存在，不证明观点正确。
- **检索命中偏移**：`match_start` / `match_end` 是相对 `context` 的 Unicode 码点偏移，用 JavaScript 时要先转换，不能直接 `slice`。
- **测试隔离**：除专门的 `TEST-ONLY` 专案外，不向既有真实案件写入测试数据。

## 五、排错

| 现象 | 处理 |
|---|---|
| 返回 `forbidden` / 提示 `agent-grant` | 正常门禁。停止该案操作，请用户本人在本机执行 `scripts/lwb agent-grant <case_id>` 放行单案；不得改走数据库、`.env` 或改策略文件绕过 |
| `get_access_policy` 显示 `deny_all` | 工作台处于默认锁死，未授权任何案件；先请用户授权具体案件 |
| 工具报数据库不可用 | 让用户启动桌面应用或 `scripts/lwb start`（仅起库用 `scripts/lwb start-db`）；不要自己跑 `init`/`stop` |
| MCP 启动即退出、stderr 有指引 | 工作台未登记或路径失效，按指引 `scripts/lwb register-instance` 或设 `LWB_HOME` |
| 检索命中为 0 | 先 `get_index_status` 看是否已解析；未解析时让 worker 跑完（`run-worker` 或长驻 `scripts/lwb start`） |
| `conflict` | 重新读快照，取最新 `revision` 后再提交 |
| 服务器路径导入被拒 | `LWB_IMPORT_ROOTS` 为空时服务器路径导入一律拒绝，改走网页上传 |

需要图表实质复核用 `chart-substantive-review`，需要处理非标银行流水/多表头对账用 `nonstandard-table-script-sop`。
