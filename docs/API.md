# V0.1 接口契约（待主代理审核）

本文件及 `openapi.json` 规定协议，不表示 HTTP 服务或业务已实现。Pydantic 定义为唯一类型来源：在项目根目录执行 `backend/.venv/bin/python scripts/generate_protocol.py`，再执行 `frontend/node_modules/.bin/openapi-typescript docs/openapi.json -o frontend/src/api/types.ts`。修改模型后两者均须重新生成，勿手工修改生成物。OpenAPI 包含路径、参数、输入输出与统一错误；实现阶段须保留该契约。

前端消费方式的约束：`frontend/src/api/endpoints.ts` 只能写 `type X = Schemas['X']` 别名或直接引用 `Schemas['…']`，不得手写字段清单，也不得用匿名对象类型描述响应。手写一套类型等于允许后端加字段而前端静默漏读。

时间字段（`created_at`）一律以 UTC 输出（`Contract` 基类的 `field_serializer` 强制 `astimezone(utc)`），前端用 `ui/time.ts` 的 `formatTimestamp` 按 `Asia/Shanghai` 显示。

| 操作 | 方法及路径（`/api/v1` 下） |
|---|---|
| create_case / list_cases | POST / GET `/cases` |
| dashboard / get_case | GET `/dashboard` / `/cases/{case_id}` |
| dashboard_deadlines | GET `/dashboard/deadlines`，全部未归档案件的期限，不分页（首页时间轴用） |
| update_schedule | PATCH `/cases/{case_id}/schedule`，按案件 revision 提交 |
| update_acquisition | PATCH `/cases/{case_id}/materials/{material_id}/acquisition`，按材料 revision 提交 |
| list_annotations / create_annotation | GET / POST `/cases/{case_id}/annotations` |
| review_annotation | POST `/cases/{case_id}/annotations/{annotation_id}/review` |
| import_materials | POST `/cases/{case_id}/intakes`（服务器路径） |
| upload_materials | POST `/cases/{case_id}/intakes/upload`（浏览器上传） |
| list_materials | GET `/cases/{case_id}/materials` |
| update_classification | PATCH `/cases/{case_id}/materials/{material_id}/classification` |
| update_display_name | PATCH `/cases/{case_id}/materials/{material_id}/display-name` |
| create_working_copy | POST `/cases/{case_id}/working-copies` |
| register_file_version | POST `/cases/{case_id}/working-copies/{working_copy_id}/file-versions` |
| reparse | POST `/cases/{case_id}/file-versions/{file_version_id}/parse-runs` |
| search_case | POST `/cases/{case_id}/search` |
| read_source | GET `/cases/{case_id}/sources/{source_ref}` |
| get_original | GET `/cases/{case_id}/materials/{material_id}/original` |
| get_job_status | GET `/cases/{case_id}/jobs/{job_id}` |
| get_index_status | GET `/cases/{case_id}/index-status` |
| save_analysis | POST `/cases/{case_id}/analyses` |
| review_analysis | POST `/cases/{case_id}/analyses/{analysis_id}/versions/{version_id}/review` |

路径参数和非 GET JSON 请求模型中重复的 ID 必须相同；HTTP 实现必须在调用共享服务前拒绝不一致。GET 的非路径输入为查询参数。浏览器上传使用 multipart/form-data，`case_id`、`source_label`、`request_key`、重复的 `relative_paths` 与 `files` 逐项按顺序对应，数量必须一致；每个相对路径都须验证，不得视作服务器绝对路径。浏览器来源的 source_root 可为空，不伪造服务器路径。服务器导入仅接收允许目录内的 source_path。

所有案件操作显式携带 case_id，分页 offset 从 0 起，列表 limit 默认 50、最大 200，检索默认 20、最大 100。read_source 的 context_chars 默认 200，范围 0–2000，按 Unicode 字符计，分别限定前、后上下文长度。source_ref 是不可变 Segment 的不透明 UUID；来源读取必须核对案件归属。scope 默认为 originals，working 仅指已登记且已解析的版本，analysis 单独标记。金额模式必须提供三位大写字母 currency（例如 CNY、USD），格式校验不声称验证 ISO 4217；未知币种不能当作 CNY。金额以 Decimal 表示并在 JSON 输出为字符串，不接受浮点金额。

新分析版本 review_status 默认为 pending，另有 approved、rejected；状态不证明观点真实性。更新 analysis 必须同时提交 analysis_id 与 expected_revision；审核不修改正文。UUID 在 JSON 中为字符串。服务必须返回统一 `{code, message, details, request_id}`；详见 `errors.py` 的状态码映射。CLI 输出 JSON，失败返回非零；MCP 返回结构化错误。

## 后续流程路由

| 操作 | 方法及路径（`/api/v1` 下） |
|---|---|
| list_intake_items | GET `/cases/{case_id}/intakes/{intake_id}/items`；分页、可按状态筛选，返回相对来源、状态、可空 material_id 和错误 |
| retry_job | POST `/cases/{case_id}/jobs/{job_id}/retry`；复用原 job_id 和对应接收记录 |
| preview_file_version / download_file_version | GET `/cases/{case_id}/file-versions/{file_version_id}/preview` 或 `/download` |
| preview_pdf_page | GET `/cases/{case_id}/file-versions/{file_version_id}/page-preview?page=2`，固定版本的一基页码，返回 PNG |
| get_extracted_text | GET `/cases/{case_id}/file-versions/{file_version_id}/text`；返回该版本最近一次解析的分段文本、locator、方法与覆盖统计，按文档顺序 |
| list_analyses | GET `/cases/{case_id}/analyses` |
| get_analysis | GET `/cases/{case_id}/analyses/{analysis_id}` |
| list_analysis_versions | GET `/cases/{case_id}/analyses/{analysis_id}/versions`，分页列出版本及审核状态 |
| get_analysis_version | GET `/cases/{case_id}/analyses/{analysis_id}/versions/{version_id}` |

文件访问须校验案件与版本归属。preview 仅内联服务端识别的 PDF、安全图片或经转义的提取文本，响应分别使用对应 MIME 或 UTF-8 `text/plain`；HTML、SVG 及其他活动内容不得以内联活动内容提供。不支持预览的类型走 download，使用 `application/octet-stream`、`Content-Disposition: attachment`；不得以用户提供的文件名作为未校验的响应头。无权或失败时返回统一 JSON 错误。

本机单用户模式无需登录。API 校验 Host，只接受工作台的回环地址及端口。写请求还须提供与请求地址同源的 `Origin`（协议、主机和端口一致）；浏览器会自动提供，脚本调用须显式设置。不开放跨源访问。

Host 或 Origin 检查失败使用 `forbidden` / 403，输入校验失败使用 `invalid_input` / 422；均使用统一错误结构。跨案资源沿用 `case_mismatch` / 404。

每个搜索命中的 `match_start`（含）和 `match_end`（不含）均是相对于同一命中 `context` 字段的 Unicode 码点偏移；`context[match_start:match_end]` 为显示的命中文本。前端 JavaScript 使用 UTF-16 字符串时需转换，不能直接将此偏移用于 slice。材料命中带固定版本 source_ref；分析命中必须含 analysis_id 和不可变的 analysis_version_id，其 context 来自该版本。引用只证明定位存在。


Dashboard 按最近一项未完成期限的到期日排序，无未完成期限排在最后。期限是本地日历日期，类别为 `external` 或 `internal`。办理信息按案件整体更新。来源详情返回材料显示名、当前取得方式、对应原件的导入记录及命中版本是否为原件。图片预览的 `frame` 从 0 起。

`GET /dashboard/deadlines` 是首页跨案期限时间轴的只读投影，不新增表：直接读 `cases.deadlines` JSONB，固定排除已归档案件（`archived_at IS NULL`），**不按 50 条分页**，因此第 2 页及以后案件的期限同样完整。返回 `items`（`case_id`、`case_title`、`title`、`due`、`kind`、`completed`，按 `due` 升序，同日按案件标题、事项标题、`case_id` 稳定排序）、`as_of`（服务端按 `Asia/Shanghai` 计算的今天，供前端锚定窗口与“今天”锚点）和 `case_total`（参与统计的未归档案件数）。窗口裁剪（默认前 7 天至后 60 天）、更早逾期汇总与完成状态显隐都由前端完成，接口只读且不改变 `GET /dashboard` 的既有排序与分页行为。JSONB 中不符合 `Deadline` 契约的存量条目被跳过，不影响其余期限返回。

标注检索支持正文子串和文件版本过滤。人工作业通过网页入口，Agent 标注通过 MCP 登记，两者共用案件归属和页码校验。复核只接受待复核的 Agent 标注，提交确认正文并保留原文，重复提交返回 `conflict`。

## 办案进度（P2/P3，迁移 0005）

进度是通用节点树＋类型化关系＋业务关联＋变更记录，设计依据见 [进度通用节点模型](decisions/20261116-进度通用节点模型.md) 与 [界面与 Agent 共用业务命令](decisions/20261116-界面与Agent共用业务命令.md)。规则配置（种类、状态、包含关系、关系类型、模板片段、Agent 可用命令、标题预设）由 `GET /progress/rules` 提供，是层级与种类校验的唯一来源；数据库只保证非空、长度与同案组合外键。

| 操作 | 方法及路径（`/api/v1` 下） |
|---|---|
| get_progress_rules | GET `/progress/rules` |
| get_progress | GET `/cases/{case_id}/progress`，返回完整快照（节点、关系、关联含显示名、待确认变更、全部变更、seq） |
| get_change_seqs | GET `/cases/{case_id}/changes/seq`，返回 `{domain: seq}`，前端轮询用 |
| progress_add_node | POST `/cases/{case_id}/progress/nodes` |
| progress_update_node | PATCH `/cases/{case_id}/progress/nodes/{node_id}`，带 expected_revision |
| progress_move_node | POST `/cases/{case_id}/progress/nodes/{node_id}/move`，拒绝成环与违反包含规则 |
| progress_set_active_stage | POST `/cases/{case_id}/progress/active-stage`，同程序原进行中阶段记为已完成，一条变更 |
| progress_insert_fragment | POST `/cases/{case_id}/progress/fragments` |
| progress_remove_node / restore_node | POST `/cases/{case_id}/progress/nodes/{node_id}/remove` 或 `/restore` |
| progress_add_relation | POST `/cases/{case_id}/progress/relations` |
| progress_remove_relation | POST `/cases/{case_id}/progress/relations/{relation_id}/remove` |
| progress_link / unlink | POST `/cases/{case_id}/progress/links` 或 `/links/{link_id}/remove` |
| progress_accept_change | POST `/cases/{case_id}/progress/changes/{change_id}/accept`（仅人工） |
| progress_accept_all | POST `/cases/{case_id}/progress/changes/accept-all`（仅人工） |
| progress_revert_change | POST `/cases/{case_id}/progress/changes/{change_id}/revert`（仅人工） |

所有写命令返回完整快照；冲突返回 `conflict`/409，客户端保留草稿并重读。actor 由入口固定：HTTP＝human/local-web，MCP＝agent/mcp，CLI＝agent/cli，调用方不能自报。Agent 写命令进入待确认（review_state=pending），且必须带 request_key（幂等重放不新增变更与序号）；确认与撤回不在 Agent 白名单，MCP/CLI 不暴露这两个工具。撤回是一条新的反向命令，仅当受影响节点此后未再变化时允许，否则 `conflict` 由人工处理；已确认的变更也可撤回。建案同事务实例化默认片段（K14），旧案由 `scripts/lwb migrate` 后的 backfill 补齐（幂等）。结案归档是案件级操作，不是进度节点。

## 结案归档与工时（P2，迁移 0006）

| 操作 | 方法及路径（`/api/v1` 下） |
|---|---|
| archive_case / unarchive_case | POST `/cases/{case_id}/archive` 或 `/unarchive`，按案件 revision 提交 |
| get_case_time | GET `/cases/{case_id}/time?work_date=YYYY-MM-DD`，返回当日记录、合计与全案按标题汇总 |
| start_timer / stop_timer | POST `/cases/{case_id}/time/start`、`/time/entries/{entry_id}/stop` |
| add_time_entry / remove_time_entry | POST `/cases/{case_id}/time/entries`、`/time/entries/{entry_id}/remove` |

归档/取消归档返回 `CaseOverview`（含 `archived_at`、`revision`）；归档前存在未停止的计时则 `invalid_input`，已归档案件的进度/材料/导入/办理信息/工时以及标注与分析写入一律 `invalid_input`。工时为人工入口（HTTP 与网页），MCP/CLI 不暴露。`GET /cases` 增加可选 `archived=true|false` 过滤（省略＝全部），`GET /dashboard` 固定排除已归档。停止计时按 15 分钟向上取整（最少 0.25h）；补录必须是 0.25 的整数倍；删除为软删除。

## 关系图、证据目录及诉请金额（0007–0008）

| 读取入口 | 响应 |
|---|---|
| GET `/cases/{case_id}/graphs` | 案件关系图工作区、快照、同意记录及现行目录编号 |
| GET `/cases/{case_id}/evidence-catalog` | 分组、条目、材料绑定、编号及证据域待确认提案 |
| GET `/cases/{case_id}/claims` | 诉请分组、整数分明细、各币种总额及待确认提案 |
| GET `/cases/{case_id}/changes/seq` | `progress`、`graph`、`evidence`、`claims` 的独立变化序号 |

完整写入路径与字段以 [OpenAPI 契约](openapi.json) 为准。目录导入先预览、再选择已登记的同案材料提交。已确认条目按活动分组与排序编号，逐条确认后立即生效。批量撤回保留逐条确认的结果。归档案件仍可读取和导出。

## 图表程序化能力与对外引用导出（2026-11-22，零迁移）

在 `0008_claim_amounts` 基础上新增的只读分析、通用表格列映射及跨图表导出/预检端点如下（完整请求与响应模型见 [OpenAPI 契约](openapi.json)）：

| 能力 | 方法及路径（`/api/v1` 下） |
|---|---|
| chart_preflight | POST `/cases/{case_id}/charts/preflight`（`working`/`client`/`court` 硬门禁预检与豁免检查） |
| chart_citation_table | POST `/cases/{case_id}/charts/citation-table`（返回 JSON / Markdown / CSV《图示引用索引附表》） |
| chart_audit_bundle | GET `/cases/{case_id}/charts/audit-bundle`（只读复核审计包 JSON） |
| graph_assist_cooccurrence | GET `/cases/{case_id}/graphs/assist/cooccurrence`（双主体主名＋别名全案共现与同段金额/日期） |
| graph_assist_segment_hints | GET `/cases/{case_id}/graphs/assist/segment-hints`（阅卷/标注片段一键入图预填线索） |
| graph_assist_audit | GET `/cases/{case_id}/graphs/assist/audit`（覆盖缺口、孤儿机构差集与强指纹碰撞全案审计） |
| graph_assist_entity_candidates | GET `/cases/{case_id}/graphs/assist/entity-candidates`（文书首部/简称候选主体与消歧清单） |
| materials_coverage_matrix | GET `/cases/{case_id}/materials/coverage-matrix`（材料覆盖状态与健康格子图分类统计） |
| graph_import_table_preview | POST `/cases/{case_id}/graphs/import-table/preview`（服务器同案材料表格列预览） |
| graph_import_table_preview_upload | POST `/cases/{case_id}/graphs/import-table/preview-upload`（浏览器上传 XLSX/CSV 列预览） |
| graph_import_table_compile | POST `/cases/{case_id}/graphs/import-table/compile`（按列映射含可选 `currency_col` 按币种分桶编译汇总边与异常 `skipped_rows`） |
| graph_import_table_commit | POST `/cases/{case_id}/graphs/import-table/commit`（幂等写入主体/关系与原表格出处，同对跨币种冲突记入 `currency_conflict_same_pair`） |

说明：以上只读分析接口严格校验 `case_id` 归属并在跨案引用时返回 `case_mismatch`/404；`court` 模式存在未豁免 `blocker` 时预检与附表导出予以阻断；外部公式注入风险单元在 CSV 导出时强制转义。
