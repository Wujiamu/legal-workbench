---
name: legal-workbench-cli
description: 使用本机法律工作台 CLI 与同源 HTTP 上传入口查询已入库案件、材料、索引与来源，操作办案进度、主体关系图、证据目录、诉请金额及导出复核审计包。用户要求在本机法律工作台上查案、写回待确认提案或验收工作台时使用。
---

# 法律工作台 CLI 接入规范（基线 `0008_claim_amounts` + 图表程序化能力）

在本机工作台目录运行命令（无需依赖当前工作目录）：

```sh
"<PROJECT_ROOT>/backend/.venv/bin/python" -m legal_workbench.cli <子命令> [参数]
```

> 成功输出 stdout 单行 JSON（退出码 `0`）；业务错误输出 `{code, message, details}` 至 stderr（退出码 `2`）；未预期异常退出码 `1`。支持通过 `<子命令> --help` 核对精确参数。

## 1. 连通性与案件管理

- **只读连通检查**：`list-cases --limit 0`（确认 PostgreSQL 在线，不拉取正式案件列表）。若数据库未运行，可先启动桌面「法律工作台.app」或运行 `scripts/lwb start` 后重试。
- **建案 / 列案 / 列材料 / 查覆盖**：
  - `create-case --title "案件名称"`
  - `list-cases --limit 20`
  - `list-materials --case-id <UUID>`
  - `index-status --case-id <UUID>`
  - `job-status --case-id <UUID> --job-id <UUID>` / `retry-job --case-id <UUID> --job-id <UUID>`
  - `run-worker`（仅当长驻服务未运行 worker 时手动消费一次队列；若正式 `scripts/lwb start` 已在线，单 worker 由 PostgreSQL advisory lock 守护，第二实例会自动安全退出）

## 2. 导入与浏览器上传入口说明

- `import --case-id <UUID> --source-path <DIR> --source-label <L> --request-key <KEY>` 仅读取 `.env` 中 `LWB_IMPORT_ROOTS` 显式允许的服务器目录；**当 `LWB_IMPORT_ROOTS` 未配置（为空）时，服务器目录导入默认被安全拒绝**。
- 在 `LWB_IMPORT_ROOTS` 为空时，应优先通过网页界面（`http://127.0.0.1:8765`）上传，或调用同源 multipart 接口 `POST http://127.0.0.1:8765/api/v1/cases/{case_id}/intakes/upload`（脚本调用非 GET 写接口须显式带请求头 `Origin: http://127.0.0.1:8765`，参数含 `case_id`、`source_label`、`request_key`、`relative_paths`、`files`）。

## 3. 检索、原件回读与分析留痕

- **四模式检索**：`search --case-id <UUID> --query <词> --mode literal|identifier|amount|date`（金额模式须显式传 `--currency CNY` 等三位大写币种）。
- **精确回读原文**：`read-source --case-id <UUID> --source-ref <SEGMENT_UUID> [--context-chars 200]`。
- **保存带引用分析**：`save-analysis --case-id <UUID> --title "..." (--content "..." | --content-file path.md) --statement-kind agent_analysis --source-ref <UUID>`（`--content` 与 `--content-file` 互斥必传其一；每条出处传一次 `--source-ref`；更新已有分析须带 `--analysis-id` 与最新 `--expected-revision`）。
- 另有 `update-classification`、`update-display-name`、`create-working-copy`、`register-file-version`、`reparse`、`get-original`、`list-analyses`、`get-analysis`、`list-analysis-versions`、`get-analysis-version`。

## 4. 办案进度（`progress*`）

- 只读：`progress --case-id <UUID>`、`progress-rules`
- 待确认提案写命令（全部进入 `pending`，必须携带幂等 `--request-key`）：`progress-add-node`、`progress-set-active-stage`、`progress-insert-fragment`、`progress-update-node`、`progress-move-node`、`progress-remove-node`、`progress-add-relation`、`progress-remove-relation`、`progress-link`、`progress-unlink`。**CLI 不提供确认与撤回**，须由人工在网页点击确认或撤回。

## 5. 主体关系图、证据目录与诉请金额

- **主体关系图**：`graph --case-id <UUID>`、`graph-rules`、`graph-add-entity`、`graph-update-entity`、`graph-add-alias`、`graph-add-relation`、`graph-update-relation`、`graph-add-source`、`graph-create-graph`、`graph-update-graph`、`graph-add-to-graph`、`graph-removal-impact`。
- **证据目录**：`evidence-catalog --case-id <UUID>`、`evidence-add-group`、`evidence-add-item`、`evidence-update-item`、`evidence-add-binding`、`evidence-import-catalog`。
- **诉请金额（整数分）**：`claims --case-id <UUID>`、`claim-candidates --case-id <UUID>`、`claim-add-group`、`claim-update-group`、`claim-add-item`、`claim-update-item`。

## 6. 对外图表复核审计包与运维检查

- **只读审计包**：
  `export-chart-audit-bundle --case-id <UUID> --chart-type graph|claims --target-id <UUID> --profile client|court [--snapshot-id <UUID>]`
  严格只读校验 `case_id` 归属；供配合 `chart-substantive-review` 执行文图实质核对。
- **只读一致性检查**：`check-consistency`（核对数据库、受管文件与哈希一致性，不改业务数据）。

## 7. 操作纪律

1. 除专门的 `TEST-ONLY` 验收专案外，严禁向任何既有真实案件写入测试数据。
2. 所有写操作均带唯一 `--request-key` 与最新 `--expected-revision`；遇 `conflict` 时重新读取快照获取最新修订号后再提交，不得盲目重试。
3. 未经用户确认，不得擅自修改 `.env`、删除 `var/` 或跨案引用 `source_ref`。
