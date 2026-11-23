# 数据模型

当前正式运行基线为 `v0.1.0`（`0008_claim_amounts`，零新增迁移）。表结构由 `backend/migrations/versions/` 定义，接口协议见 `backend/src/legal_workbench/schemas.py`。早期业务规则见 `V0.1_实施任务包.md` 第 2.3、2.4、2.6 节。进度模型的设计依据见 [进度通用节点模型](decisions/20261116-进度通用节点模型.md) 与 [界面与 Agent 共用业务命令](decisions/20261116-界面与Agent共用业务命令.md)。

## 表

| 表 | 作用 | 可变 | 关键约束 |
|---|---|---|---|
| `cases` | 案件、当前节点、下一步、期限列表、归档时间 | 是 | `(id)` 主键，办理信息整体使用 revision CAS，旧记录的期限列表为空；`archived_at` 为空＝办理中，非空＝已归档（内容只读） |
| `intakes` | 一次导入批次 | 是 | `(case_id, request_key)` 唯一，重放不新建批次 |
| `intake_items` | 批次内单个文件 | 是 | `(intake_id, relative_source)` 唯一；`(case_id, material_id)` 唯一 |
| `stored_objects` | 原件字节（按内容哈希） | 否 | `(case_id, sha256)` 唯一；`relative_path` 指向 `originals/<sha256>/source<ext>` |
| `materials` | 材料及当前取得方式 | 是 | `(case_id, id)` 唯一；`revision` 乐观锁 |
| `file_versions` | 文件版本 | 否 | 组合外键到 `materials(case_id, id)`；`active_parse_run_id` 指向已发布的解析 |
| `working_copies` | 工作副本 | 是 | 组合外键到 `file_versions(case_id, material_id, id)`；`revision` |
| `parse_runs` | 一次解析尝试 | 否 | `(case_id, file_version_id)` 组合外键；`sequence` 递增；`coverage` JSONB |
| `parse_units` | 解析单元（页/块/sheet） | 否 | `(parse_run_id, unit_key)` 唯一 |
| `segments` | 可检索文本片段 | 否 | 组合外键到 `parse_units`、`parse_runs`；`offset_map` 支持归一化偏移回映射 |
| `exact_values` | 抽取出的金额/日期/编号 | 否 | 金额与日期检索的唯一来源 |
| `jobs` | 任务队列 | 是 | `status`、`attempt`、`total_units`/`completed_units` |
| `analyses` | 分析成果（标题、当前版本） | 是 | `revision` |
| `analysis_versions` | 不可变分析正文 | 否 | `(analysis_id, sequence)` 唯一；`review_status` 可改，正文不可改 |
| `annotations` | 文件标注及人工复核 | 仅一次人工复核 | 同案文件版本组合外键，PDF 页码从 1 起，Agent 原文保留，确认文字与 revision=1 同时写入 |
| `audit_events` | 操作留痕 | 否 | `operation` 由迁移维护闭集，当前范围见下方审计留痕 |
| `time_entries` | 工时记录（计时/补录） | 是（软删除） | `(case_id,id)` 唯一；`(case_id,node_id)` 组合外键到进度节点；`hours` 0–24 且按 0.25 步进（服务层校验）；`source` timer/manual，timer 必须有 `started_at`；同案最多一个未停止计时（服务层） |
| `maintenance_state` | 单行维护标志 | 是 | `id=1` 唯一；备份期间 `active=true` 阻断业务写入 |
| `progress_nodes` | 办案进度节点（通用树） | 是 | `(case_id,id)` 唯一；`(case_id,parent_id)` 自引用组合外键；`(case_id,request_key)` 唯一（幂等）；`revision` CAS；层级/种类/状态由 `progress_rules` 配置校验，DB 只查非空与长度 |
| `progress_relations` | 节点间非包含关系 | 是 | 起点/终点均为 `(case_id,…)` 组合外键；`from<>to`；类型由配置定义；软删除 |
| `progress_links` | 节点关联业务对象 | 否（可删） | 材料/分析/标注三选一（`num_nonnulls=1`）＋各自组合外键；`(node_id,material_id,analysis_id,annotation_id)` 唯一 |
| `progress_changes` | 命令变更记录（修改前/后） | 仅复核状态 | `(case_id,seq)` 唯一；`diff` JSONB 含每个受影响对象前后值与修订号；`review_state` none/pending/accepted/reverted；Agent 变更默认 pending |
| `case_change_seq` | 案件级各域变化序号 | 是 | `(case_id,domain)` 主键；进度、关系图、证据目录及诉请金额的写命令锁定各自序号并递增 |
| `case_entities` | 关系图共享的案件主体 | 是（软删除） | `(case_id,id)`、`(case_id,request_key)` 唯一，名称非空，revision CAS |
| `entity_aliases` | 主体别名 | 是（软删除） | 同案主体外键，同一主体同一活跃别名唯一 |
| `case_relations` | 有向主体关系 | 是（软删除） | 两端同案且不得自指，可信状态限 evidenced/suspected/disputed/ruled_out，revision CAS |
| `graph_sources` | 主体或关系的出处 | 是（软删除） | 主体/关系恰选一，片段/材料/标注恰选一，全部使用同案外键 |
| `relation_graphs` | 具名关系图 | 是（软删除） | `(case_id,id)`、`(case_id,request_key)` 唯一，revision CAS |
| `relation_graph_groups` | 图中分组和大小 | 是（软删除） | 同案图外键，宽高为正，revision CAS |
| `relation_graph_nodes` | 图中的主体成员与布局 | 是（可移出图） | 同案主体与图外键，分组须属于同一图，`(graph_id,entity_id)` 唯一，revision CAS |
| `relation_graph_edges` | 图中的关系成员 | 否（可移出图） | 同案关系与图外键，`(graph_id,relation_id)` 唯一 |
| `relation_graph_snapshots` | 冻结的图、出处和当时的证据编号 | 否 | 同图版本号唯一且从 1 起，进度节点须同案，仅人工创建，触发器阻止更新和删除 |
| `snapshot_consents` | 快照同意、更正与撤回 | 否 | 仅人工追加；更正、撤回引用同案同快照的前项且不得自指，支持同案材料出处，触发器阻止更新和删除 |
| `evidence_groups` | 证据目录分组及顺序 | 是（软删除） | 非负顺序，`(case_id,request_key)` 唯一，revision CAS |
| `evidence_items` | 证据条目及复核状态 | 是（软删除） | 同案分组与可选导入材料外键，manual/imported 来源，confirmed/pending/rejected 状态，revision CAS |
| `evidence_item_bindings` | 证据条目绑定材料及可选页段 | 是（软删除） | 条目与材料须同案，页码从 1 起，终页不得早于起页，confirmed/pending/rejected 状态 |
| `graph_changes` | 关系图变更及复核历史 | 仅复核状态 | `(case_id,seq)` 唯一，差异 JSONB，状态限 none/pending/accepted/reverted |
| `evidence_changes` | 证据目录变更及复核历史 | 仅复核状态 | `(case_id,seq)` 唯一，差异 JSONB，状态限 none/pending/accepted/reverted |
| `claim_groups` | 请求分组、币种及是否计入全案总额 | 是（软删除） | 三位大写币种，非负顺序，`(case_id,request_key)` 唯一，revision CAS |
| `claim_items` | 诉请项目金额及可选出处 | 是（软删除） | 金额为 0 至 9007199254740991 的整数分；分组同案，片段/材料/标注至多一个且须同案，revision CAS |
| `claim_changes` | 诉请金额变更及复核历史 | 仅复核状态 | `(case_id,seq)`、`(case_id,request_key)` 唯一，差异 JSONB，状态限 none/pending/accepted/reverted |

## 案件隔离

所有案件子表都带 `case_id`，并以 `(case_id, …)` 组合外键指向父表。跨案引用由 PostgreSQL 拒绝，例如把 A 案的文件版本挂到 B 案的材料上，数据库直接报外键错误。服务层另有 `case_mismatch` 检查，两层都在。

## 不可变性

- `stored_objects`、`file_versions`、`parse_runs`、`parse_units`、`segments`、`exact_values`、`analysis_versions` 一旦写入不修改。
- 重解析产生新 `parse_runs` / `parse_units` / `segments`，旧记录保留。`source_ref` 指向旧 `Segment`，因此历史引用在重做 OCR 后仍能读到当时的内容。
- `file_versions.active_parse_run_id` 是唯一被更新的字段：`complete` 按 `sequence` 发布，已有有效版本不被新的 `partial`/`failed` 自动替换。

## 并发控制

可变行带 `revision`，更新走 `WHERE revision = expected`。冲突返回 `conflict` / HTTP 409。客户端必须提交自己读到的 revision，服务端不做"最后一次写入获胜"。

## 状态机

- 导入项：`pending → copying → published → ready`，异常终止为 `failed`，系统垃圾/链接为 `skipped`。每阶段独立提交。
- 任务：`queued → running → succeeded | partial | failed`。重试复用同一 `job_id`。
- 解析单元：`pending → extracted | ocr_done | no_text_detected | failed | unsupported`。
- 解析版本：`queued → running → complete | partial | failed | unsupported`。
- 分析审核：`pending | approved | rejected`。审核状态不证明观点真实性。

## 覆盖统计口径

`coverage` 描述某个文件版本**最近一次**解析的结果，不是"当前在跑什么"：

- `complete`：单元成功抽取（含 OCR 成功与确认无文字）。
- `partial`：有单元成功但有告警（例如公式无缓存值、页眉页脚未覆盖）。
- `failed`：解析失败。
- `unsupported`：格式不支持或文件损坏。
- `pending`：从未解析过，或解析任务尚未完成。

关键点：解析结果为 `unsupported`/`failed` 的运行不会成为 `active_parse_run_id`。若只看 active run，损坏文件会永远显示"待处理"，因此覆盖统计取"active run，否则最近一次 run"，见 `coverage.py`。

## 时间与数值

主键为 UUID。时间存 UTC `timestamptz`，JSON 输出统一序列化为 UTC（`Contract` 基类上的 `field_serializer` 强制 `astimezone(utc)`，避免同一时刻在服务器时区不同时出现 `+08:00` / `+00:00` 两种写法），界面按 `Asia/Shanghai` 显示（`frontend/src/ui/time.ts` 的 `formatTimestamp`，用 `Intl.DateTimeFormat` 固定时区，不依赖浏览器本地时区）。金额用 `Decimal` / PostgreSQL `NUMERIC`，JSON 输出为字符串，不接受浮点。

## 审计留痕

`audit_events` 登记以下闭集操作。带前缀的行中，每项后缀与前缀组成一个完整操作名。

| 前缀 | 操作 |
|---|---|
| 基础及归档/工时 | `session.login`、`session.logout`、`case.create`、`intake.import`、`intake.upload`、`material.classify`、`material.rename`、`material.working_copy`、`file_version.register`、`parse.reparse`、`analysis.save`、`analysis.review`、`backup.create`、`backup.restore`、`maintenance.clear`、`case.archive`、`case.unarchive`、`time.start`、`time.stop`、`time.add`、`time.remove` |
| `progress.` | `add_node`、`update_node`、`move_node`、`set_active_stage`、`insert_fragment`、`remove_node`、`restore_node`、`add_relation`、`remove_relation`、`link`、`unlink`、`accept_change`、`accept_all`、`revert_change` |
| `graph.` | `add_entity`、`update_entity`、`remove_entity`、`restore_entity`、`add_alias`、`remove_alias`、`add_relation`、`update_relation`、`remove_relation`、`restore_relation`、`add_source`、`remove_source`、`create_graph`、`update_graph`、`remove_graph`、`add_to_graph`、`remove_from_graph`、`save_layout`、`upsert_group`、`remove_group`、`create_snapshot`、`restore_snapshot`、`add_snapshot_consent`、`accept_change`、`accept_all`、`revert_change` |
| `evidence.` | `add_group`、`update_group`、`reorder_groups`、`remove_group`、`add_item`、`update_item`、`move_item`、`remove_item`、`add_binding`、`remove_binding`、`confirm_item`、`reject_item`、`confirm_binding`、`reject_binding`、`import_catalog`、`accept_change`、`accept_all`、`revert_change` |
| `claims.` | `add_group`、`update_group`、`reorder_groups`、`remove_group`、`add_item`、`update_item`、`reorder_items`、`remove_item`、`restore_item`、`accept_change`、`accept_all`、`revert_change` |

字段只有 `case_id`、`actor`、`operation`、`target_type`、`target_id`、`request_id`、`created_at`。**不记录**材料正文、提取文本、访问密钥、数据库连接串。`actor` 取值：`local-session`（网页）、`cli`、`mcp`。

数据库 `CHECK` 与 Python `audit.OPERATIONS` 使用同一闭集。`0007_graph_evidence` 加入关系图与证据目录操作，`0008_claim_amounts` 加入诉请金额操作。审计写入失败只记日志，不阻断业务操作。

降级时先删除对应迁移新增操作的审计行，再恢复较窄的 `CHECK`。0008 删除 `claims.%`，0007 删除 `graph.%` 与 `evidence.%`，0006 删除归档及工时 6 项，0005 删除 `progress.%`。

尚未提供查询界面：数据已写入，但没有 UI 或 CLI 命令读取它。

## 结案归档与工时（迁移 0006）

- 归档是**案件级**操作，不是进度命令：`cases.archived_at` 置位＋`case.archive` 审计，按案件 revision CAS；归档前若存在未停止的计时器则拒绝（不允许把进行中的计时锁在只读案件里）。
- 归档后案件内容只读：进度写命令、材料改名/分类/取得方式/工作副本/新版本、导入批次、办理信息与工时写入统一走 `cases.ensure_case_writable`，返回 `invalid_input`「案件已归档…」。取消归档恢复写入。
- 已归档案件从首页（dashboard）隐藏，`GET /cases?archived=false|true` 可按办理中/已归档过滤，默认不过滤。
- `time_entries` 只由人工写入（HTTP 与网页），MCP/CLI 不暴露工时；`created_by` 记录审计 actor（`local-web` 等），不做闭集约束。
- 计时停止时按 15 分钟向上取整（最少 0.25h）；补录必须是 0.25 的整数倍。删除为软删除。日视图返回当日全部未删除记录（含进行中的计时，用于显示「计时中」），合计按小时数求和（进行中为 0）；按标题汇总只统计补录与已停止的计时。

## 关系图、证据目录与诉请金额（迁移 0007–0008）

- `0007_graph_evidence` 新增案件主体、关系、出处、具名图及图内布局、快照和同意记录。同案外键保护引用归属。数据库触发器防止改写已冻结快照及已登记的同意记录。
- `evidence_groups`、`evidence_items` 和 `evidence_item_bindings` 保存目录结构与复核状态。现行编号从已确认条目及顺序计算，历史编号随快照冻结。关系图引用片段、材料或标注。
- `0008_claim_amounts` 新增 `claim_groups`、`claim_items`、`claim_changes`。金额以非负整数分存储，抵扣在汇总中扣减。分组的计入标记决定是否进入所属币种的全案总额。
- 关系图、证据目录与诉请金额共享 `case_change_seq` 序号表，各自维护变更历史和复核状态。迁移降级按域清理对应序号，并保留其它域。
