"""Generate the frozen OpenAPI contract from Pydantic models, without running an API."""
import json
from pathlib import Path

from pydantic.json_schema import models_json_schema
from legal_workbench import chart_export, graph_assist, schemas, table_graph_import
from legal_workbench.errors import ErrorResponse

ROOT = Path(__file__).resolve().parents[1]
# operation, HTTP method, path, input, output
OPERATIONS = [
    ('create_case', 'post', '/api/v1/cases', 'CreateCaseInput', 'CreateCaseOutput'),
    ('list_cases', 'get', '/api/v1/cases', 'ListCasesInput', 'ListCasesOutput'),
    ('dashboard', 'get', '/api/v1/dashboard', 'DashboardInput', 'DashboardOutput'),
    # 首页跨案期限时间轴：只读投影，全部未归档案件、不分页。
    ('dashboard_deadlines', 'get', '/api/v1/dashboard/deadlines',
     'DashboardDeadlinesInput', 'DashboardDeadlinesOutput'),
    ('get_case', 'get', '/api/v1/cases/{case_id}', 'CaseInput', 'CaseOverview'),
    # 模块开关：前端据此隐藏页签并跳过必然 404 的请求。
    ('list_modules', 'get', '/api/v1/modules', 'ModulesQuery', 'ModulesOutput'),
    ('update_case_metadata', 'patch', '/api/v1/cases/{case_id}/metadata', 'CaseMetadataInput', 'CaseOverview'),
    ('update_schedule', 'patch', '/api/v1/cases/{case_id}/schedule', 'CaseScheduleInput', 'CaseOverview'),
    ('list_annotations', 'get', '/api/v1/cases/{case_id}/annotations', 'ListAnnotationsInput', 'ListAnnotationsOutput'),
    ('create_annotation', 'post', '/api/v1/cases/{case_id}/annotations', 'CreateAnnotationInput', 'AnnotationOutput'),
    ('review_annotation', 'post', '/api/v1/cases/{case_id}/annotations/{annotation_id}/review', 'ReviewAnnotationInput', 'AnnotationOutput'),
    ('import_materials', 'post', '/api/v1/cases/{case_id}/intakes', 'ImportMaterialsInput', 'ImportMaterialsOutput'),
    ('upload_materials', 'post', '/api/v1/cases/{case_id}/intakes/upload', 'BrowserUploadInput', 'ImportMaterialsOutput'),
    ('update_acquisition', 'patch', '/api/v1/cases/{case_id}/materials/{material_id}/acquisition', 'UpdateAcquisitionInput', 'RevisionOutput'),
    ('update_display_name', 'patch', '/api/v1/cases/{case_id}/materials/{material_id}/display-name', 'UpdateDisplayNameInput', 'RevisionOutput'),
    ('list_intake_items', 'get', '/api/v1/cases/{case_id}/intakes/{intake_id}/items', 'ListIntakeItemsInput', 'ListIntakeItemsOutput'),
    ('list_materials', 'get', '/api/v1/cases/{case_id}/materials', 'ListMaterialsInput', 'ListMaterialsOutput'),
    ('update_classification', 'patch', '/api/v1/cases/{case_id}/materials/{material_id}/classification', 'UpdateClassificationInput', 'RevisionOutput'),
    ('create_working_copy', 'post', '/api/v1/cases/{case_id}/working-copies', 'CreateWorkingCopyInput', 'CreateWorkingCopyOutput'),
    ('register_file_version', 'post', '/api/v1/cases/{case_id}/working-copies/{working_copy_id}/file-versions', 'RegisterFileVersionInput', 'RegisterFileVersionOutput'),
    ('reparse', 'post', '/api/v1/cases/{case_id}/file-versions/{file_version_id}/parse-runs', 'ReparseInput', 'ReparseOutput'),
    ('search_case', 'post', '/api/v1/cases/{case_id}/search', 'SearchCaseInput', 'SearchCaseOutput'),
    ('read_source', 'get', '/api/v1/cases/{case_id}/sources/{source_ref}', 'ReadSourceInput', 'ReadSourceOutput'),
    ('get_original', 'get', '/api/v1/cases/{case_id}/materials/{material_id}/original', 'GetOriginalInput', 'GetOriginalOutput'),
    ('get_job_status', 'get', '/api/v1/cases/{case_id}/jobs/{job_id}', 'GetJobStatusInput', 'GetJobStatusOutput'),
    ('retry_job', 'post', '/api/v1/cases/{case_id}/jobs/{job_id}/retry', 'RetryJobInput', 'RetryJobOutput'),
    ('preview_file_version', 'get', '/api/v1/cases/{case_id}/file-versions/{file_version_id}/preview', 'FilePreviewInput', 'FileContentOutput'),
    ('preview_pdf_page', 'get', '/api/v1/cases/{case_id}/file-versions/{file_version_id}/page-preview', 'PdfPageInput', 'FileContentOutput'),
    ('download_file_version', 'get', '/api/v1/cases/{case_id}/file-versions/{file_version_id}/download', 'FileVersionAccessInput', 'FileContentOutput'),
    ('get_extracted_text', 'get', '/api/v1/cases/{case_id}/file-versions/{file_version_id}/text', 'FileVersionAccessInput', 'ExtractedTextOutput'),
    ('list_analyses', 'get', '/api/v1/cases/{case_id}/analyses', 'ListAnalysesInput', 'ListAnalysesOutput'),
    ('get_analysis', 'get', '/api/v1/cases/{case_id}/analyses/{analysis_id}', 'GetAnalysisInput', 'GetAnalysisOutput'),
    ('list_analysis_versions', 'get', '/api/v1/cases/{case_id}/analyses/{analysis_id}/versions', 'ListAnalysisVersionsInput', 'ListAnalysisVersionsOutput'),
    ('get_analysis_version', 'get', '/api/v1/cases/{case_id}/analyses/{analysis_id}/versions/{version_id}', 'GetAnalysisVersionInput', 'GetAnalysisVersionOutput'),
    ('get_index_status', 'get', '/api/v1/cases/{case_id}/index-status', 'GetIndexStatusInput', 'GetIndexStatusOutput'),
    ('save_analysis', 'post', '/api/v1/cases/{case_id}/analyses', 'SaveAnalysisInput', 'SaveAnalysisOutput'),
    ('review_analysis', 'post', '/api/v1/cases/{case_id}/analyses/{analysis_id}/versions/{version_id}/review', 'ReviewAnalysisInput', 'ReviewAnalysisOutput'),
    # 办案进度（P2/P3）：写命令返回完整快照；确认/撤回仅人工入口。
    ('get_progress_rules', 'get', '/api/v1/progress/rules', 'ProgressRulesQuery', 'ProgressRulesOutput'),
    ('get_progress', 'get', '/api/v1/cases/{case_id}/progress', 'CaseInput', 'ProgressSnapshot'),
    ('get_change_seqs', 'get', '/api/v1/cases/{case_id}/changes/seq', 'CaseInput', 'ChangeSeqsOutput'),
    ('progress_add_node', 'post', '/api/v1/cases/{case_id}/progress/nodes', 'AddNodeInput', 'ProgressSnapshot'),
    ('progress_update_node', 'patch', '/api/v1/cases/{case_id}/progress/nodes/{node_id}', 'UpdateNodeInput', 'ProgressSnapshot'),
    ('progress_move_node', 'post', '/api/v1/cases/{case_id}/progress/nodes/{node_id}/move', 'MoveNodeInput', 'ProgressSnapshot'),
    ('progress_set_active_stage', 'post', '/api/v1/cases/{case_id}/progress/active-stage', 'SetActiveStageInput', 'ProgressSnapshot'),
    ('progress_insert_fragment', 'post', '/api/v1/cases/{case_id}/progress/fragments', 'InsertFragmentInput', 'ProgressSnapshot'),
    ('progress_remove_node', 'post', '/api/v1/cases/{case_id}/progress/nodes/{node_id}/remove', 'NodeRevisionInput', 'ProgressSnapshot'),
    ('progress_restore_node', 'post', '/api/v1/cases/{case_id}/progress/nodes/{node_id}/restore', 'NodeRevisionInput', 'ProgressSnapshot'),
    ('progress_add_relation', 'post', '/api/v1/cases/{case_id}/progress/relations', 'AddRelationInput', 'ProgressSnapshot'),
    ('progress_remove_relation', 'post', '/api/v1/cases/{case_id}/progress/relations/{relation_id}/remove', 'RemoveRelationInput', 'ProgressSnapshot'),
    ('progress_link', 'post', '/api/v1/cases/{case_id}/progress/links', 'LinkNodeInput', 'ProgressSnapshot'),
    ('progress_unlink', 'post', '/api/v1/cases/{case_id}/progress/links/{link_id}/remove', 'UnlinkInput', 'ProgressSnapshot'),
    ('progress_accept_change', 'post', '/api/v1/cases/{case_id}/progress/changes/{change_id}/accept', 'ChangeIdInput', 'ProgressSnapshot'),
    ('progress_accept_all', 'post', '/api/v1/cases/{case_id}/progress/changes/accept-all', 'AcceptAllInput', 'ProgressSnapshot'),
    ('progress_revert_change', 'post', '/api/v1/cases/{case_id}/progress/changes/{change_id}/revert', 'ChangeIdInput', 'ProgressSnapshot'),
    # 诉请金额（P3）：查询与候选、分组/金额项增改删排；确认/撤回仅人工入口。
    ('get_claims', 'get', '/api/v1/cases/{case_id}/claims', 'CaseInput', 'ClaimSnapshot'),
    ('list_claim_amount_candidates', 'get', '/api/v1/cases/{case_id}/claims/amount-candidates',
     'ClaimCandidatesQuery', 'ClaimAmountCandidates'),
    ('claims_add_group', 'post', '/api/v1/cases/{case_id}/claims/groups', 'AddClaimGroupInput', 'ClaimSnapshot'),
    ('claims_update_group', 'patch', '/api/v1/cases/{case_id}/claims/groups/{group_id}', 'UpdateClaimGroupInput', 'ClaimSnapshot'),
    ('claims_reorder_group', 'post', '/api/v1/cases/{case_id}/claims/groups/{group_id}/move', 'ReorderClaimGroupInput', 'ClaimSnapshot'),
    ('claims_remove_group', 'post', '/api/v1/cases/{case_id}/claims/groups/{group_id}/remove', 'ClaimGroupRevisionInput', 'ClaimSnapshot'),
    ('claims_add_item', 'post', '/api/v1/cases/{case_id}/claims/items', 'AddClaimItemInput', 'ClaimSnapshot'),
    ('claims_update_item', 'patch', '/api/v1/cases/{case_id}/claims/items/{item_id}', 'UpdateClaimItemInput', 'ClaimSnapshot'),
    ('claims_reorder_item', 'post', '/api/v1/cases/{case_id}/claims/items/{item_id}/move', 'ReorderClaimItemInput', 'ClaimSnapshot'),
    ('claims_remove_item', 'post', '/api/v1/cases/{case_id}/claims/items/{item_id}/remove', 'ClaimItemRevisionInput', 'ClaimSnapshot'),
    ('claims_restore_item', 'post', '/api/v1/cases/{case_id}/claims/items/{item_id}/restore', 'ClaimItemRevisionInput', 'ClaimSnapshot'),
    ('claims_accept_change', 'post', '/api/v1/cases/{case_id}/claims/changes/{change_id}/accept', 'ChangeIdInput', 'ClaimSnapshot'),
    ('claims_accept_all', 'post', '/api/v1/cases/{case_id}/claims/changes/accept-all', 'ClaimAcceptAllInput', 'ClaimSnapshot'),
    ('claims_revert_change', 'post', '/api/v1/cases/{case_id}/claims/changes/{change_id}/revert', 'ChangeIdInput', 'ClaimSnapshot'),
    # 结案归档与工时持久化（P2 正式范围）。
    ('archive_case', 'post', '/api/v1/cases/{case_id}/archive', 'ArchiveCaseInput', 'CaseOverview'),
    ('unarchive_case', 'post', '/api/v1/cases/{case_id}/unarchive', 'ArchiveCaseInput', 'CaseOverview'),
    ('get_case_time', 'get', '/api/v1/cases/{case_id}/time', 'GetTimeDayInput', 'TimeDayOutput'),
    ('start_timer', 'post', '/api/v1/cases/{case_id}/time/start', 'StartTimerInput', 'TimeEntryOut'),
    ('stop_timer', 'post', '/api/v1/cases/{case_id}/time/entries/{entry_id}/stop', 'StopTimerInput', 'TimeEntryOut'),
    ('add_time_entry', 'post', '/api/v1/cases/{case_id}/time/entries', 'AddTimeEntryInput', 'TimeEntryOut'),
    ('remove_time_entry', 'post', '/api/v1/cases/{case_id}/time/entries/{entry_id}/remove', 'RemoveTimeEntryInput', 'TimeEntryOut'),
    # 主体关系图（P3 入口）：HTTP 固定 actor=human；快照、客户同意、复核只在此暴露。
    ('get_graph_rules', 'get', '/api/v1/graph/rules', 'GraphRulesQuery', 'GraphRulesOutput'),
    ('get_graph_workspace', 'get', '/api/v1/cases/{case_id}/graphs', 'CaseInput', 'GraphWorkspace'),
    ('get_graph', 'get', '/api/v1/cases/{case_id}/graphs/{graph_id}', 'CaseInput', 'GraphWorkspace'),
    ('list_graph_snapshots', 'get', '/api/v1/cases/{case_id}/graphs/{graph_id}/snapshots', 'CaseInput', 'SnapshotListOutput'),
    ('get_graph_snapshot', 'get', '/api/v1/cases/{case_id}/snapshots/{snapshot_id}', 'CaseInput', 'SnapshotOut'),
    ('diff_graph_snapshot', 'get', '/api/v1/cases/{case_id}/snapshots/{snapshot_id}/diff', 'SnapshotDiffQuery', 'SnapshotDiffOut'),
    ('graph_removal_impact', 'get', '/api/v1/cases/{case_id}/graph/removal-impact', 'RemovalImpactQuery', 'RemovalImpactOut'),
    ('graph_add_entity', 'post', '/api/v1/cases/{case_id}/graph/entities', 'AddEntityInput', 'GraphWorkspace'),
    ('graph_update_entity', 'patch', '/api/v1/cases/{case_id}/graph/entities/{entity_id}', 'UpdateEntityInput', 'GraphWorkspace'),
    ('graph_remove_entity', 'post', '/api/v1/cases/{case_id}/graph/entities/{entity_id}/remove', 'EntityRevisionInput', 'GraphWorkspace'),
    ('graph_restore_entity', 'post', '/api/v1/cases/{case_id}/graph/entities/{entity_id}/restore', 'EntityRevisionInput', 'GraphWorkspace'),
    ('graph_add_alias', 'post', '/api/v1/cases/{case_id}/graph/entities/{entity_id}/aliases', 'AddAliasInput', 'GraphWorkspace'),
    ('graph_remove_alias', 'post', '/api/v1/cases/{case_id}/graph/aliases/{alias_id}/remove', 'RemoveAliasInput', 'GraphWorkspace'),
    ('graph_add_relation', 'post', '/api/v1/cases/{case_id}/graph/relations', 'AddGraphRelationInput', 'GraphWorkspace'),
    ('graph_update_relation', 'patch', '/api/v1/cases/{case_id}/graph/relations/{relation_id}', 'UpdateRelationInput', 'GraphWorkspace'),
    ('graph_remove_relation', 'post', '/api/v1/cases/{case_id}/graph/relations/{relation_id}/remove', 'RelationRevisionInput', 'GraphWorkspace'),
    ('graph_restore_relation', 'post', '/api/v1/cases/{case_id}/graph/relations/{relation_id}/restore', 'RelationRevisionInput', 'GraphWorkspace'),
    ('graph_add_source', 'post', '/api/v1/cases/{case_id}/graph/sources', 'AddSourceInput', 'GraphWorkspace'),
    ('graph_remove_source', 'post', '/api/v1/cases/{case_id}/graph/sources/{source_id}/remove', 'RemoveSourceInput', 'GraphWorkspace'),
    ('graph_create_graph', 'post', '/api/v1/cases/{case_id}/graph/graphs', 'CreateGraphInput', 'GraphWorkspace'),
    ('graph_update_graph', 'patch', '/api/v1/cases/{case_id}/graph/graphs/{graph_id}', 'UpdateGraphInput', 'GraphWorkspace'),
    ('graph_remove_graph', 'post', '/api/v1/cases/{case_id}/graph/graphs/{graph_id}/remove', 'GraphRevisionInput', 'GraphWorkspace'),
    ('graph_add_to_graph', 'post', '/api/v1/cases/{case_id}/graph/graphs/{graph_id}/members', 'AddToGraphInput', 'GraphWorkspace'),
    ('graph_remove_from_graph', 'post', '/api/v1/cases/{case_id}/graph/graphs/{graph_id}/members/remove', 'RemoveFromGraphInput', 'GraphWorkspace'),
    ('graph_save_layout', 'post', '/api/v1/cases/{case_id}/graph/graphs/{graph_id}/layout', 'SaveLayoutInput', 'GraphWorkspace'),
    ('graph_upsert_group', 'post', '/api/v1/cases/{case_id}/graph/graphs/{graph_id}/groups', 'UpsertGroupInput', 'GraphWorkspace'),
    ('graph_remove_group', 'post', '/api/v1/cases/{case_id}/graph/groups/{group_id}/remove', 'RemoveGroupInput', 'GraphWorkspace'),
    ('graph_set_node_group', 'post', '/api/v1/cases/{case_id}/graph/graphs/{graph_id}/nodes/{entity_id}/group', 'SetNodeGroupInput', 'GraphWorkspace'),
    ('graph_create_snapshot', 'post', '/api/v1/cases/{case_id}/graph/graphs/{graph_id}/snapshots', 'CreateSnapshotInput', 'GraphWorkspace'),
    ('graph_restore_snapshot', 'post', '/api/v1/cases/{case_id}/graph/snapshots/{snapshot_id}/restore', 'RestoreSnapshotInput', 'GraphWorkspace'),
    ('graph_add_snapshot_consent', 'post', '/api/v1/cases/{case_id}/graph/snapshots/{snapshot_id}/consents', 'AddConsentInput', 'GraphWorkspace'),
    ('graph_accept_change', 'post', '/api/v1/cases/{case_id}/graph/changes/{change_id}/accept', 'GraphChangeIdInput', 'GraphWorkspace'),
    ('graph_accept_all', 'post', '/api/v1/cases/{case_id}/graph/changes/accept-all', 'GraphAcceptAllInput', 'GraphWorkspace'),
    ('graph_revert_change', 'post', '/api/v1/cases/{case_id}/graph/changes/{change_id}/revert', 'GraphChangeIdInput', 'GraphWorkspace'),
    ('preflight_chart_export', 'post', '/api/v1/cases/{case_id}/charts/preflight', 'ChartRequest', 'ChartPreflight'),
    ('export_chart_citations', 'post', '/api/v1/cases/{case_id}/charts/citation-table', 'CitationRequest', 'ChartCitationTable'),
    ('get_chart_audit_bundle', 'get', '/api/v1/cases/{case_id}/charts/audit-bundle', 'ChartAuditQuery', 'ChartAuditBundle'),
    ('graph_cooccurrence', 'get', '/api/v1/cases/{case_id}/graphs/assist/cooccurrence', 'CooccurrenceQuery', 'CooccurrenceOutput'),
    ('graph_segment_hints', 'get', '/api/v1/cases/{case_id}/graphs/assist/segment-hints', 'SegmentHintsQuery', 'SegmentHints'),
    ('graph_assist_audit', 'get', '/api/v1/cases/{case_id}/graphs/assist/audit', 'GraphAuditQuery', 'GraphAuditOutput'),
    ('graph_entity_candidates', 'get', '/api/v1/cases/{case_id}/graphs/assist/entity-candidates', 'EntityCandidatesQuery', 'EntityCandidateOutput'),
    ('materials_coverage_matrix', 'get', '/api/v1/cases/{case_id}/materials/coverage-matrix', 'CoverageMatrixQuery', 'CorpusCoverageMatrix'),
    ('graph_import_table_preview', 'post', '/api/v1/cases/{case_id}/graphs/import-table/preview', 'TablePreviewInput', 'TablePreviewOutput'),
    ('graph_import_table_preview_upload', 'post', '/api/v1/cases/{case_id}/graphs/import-table/preview-upload', 'TablePreviewInput', 'TablePreviewOutput'),
    ('graph_import_table_compile', 'post', '/api/v1/cases/{case_id}/graphs/import-table/compile', 'TableCompileInput', 'TableCompileOutput'),
    ('graph_import_table_commit', 'post', '/api/v1/cases/{case_id}/graphs/import-table/commit', 'TableCommitInput', 'TableCommitOutput'),
    # 证据目录（P3 入口）。
    ('get_evidence_catalog', 'get', '/api/v1/cases/{case_id}/evidence-catalog', 'CaseInput', 'EvidenceCatalogOut'),
    ('export_evidence_catalog', 'get', '/api/v1/cases/{case_id}/evidence-catalog/export', 'CaseInput', 'EvidenceCatalogOut'),
    ('export_evidence_bundle', 'get', '/api/v1/cases/{case_id}/evidence-catalog/bundle', 'CaseInput', 'EvidenceCatalogOut'),
    ('preview_evidence_catalog_import', 'post', '/api/v1/cases/{case_id}/evidence-catalog/import/preview', 'CaseInput', 'ImportPreviewOutput'),
    ('import_evidence_catalog', 'post', '/api/v1/cases/{case_id}/evidence-catalog/import', 'ImportEvidenceCatalogInput', 'EvidenceCatalogOut'),
    ('evidence_add_group', 'post', '/api/v1/cases/{case_id}/evidence-catalog/groups', 'AddEvidenceGroupInput', 'EvidenceCatalogOut'),
    ('evidence_update_group', 'patch', '/api/v1/cases/{case_id}/evidence-catalog/groups/{group_id}', 'UpdateEvidenceGroupInput', 'EvidenceCatalogOut'),
    ('evidence_reorder_groups', 'post', '/api/v1/cases/{case_id}/evidence-catalog/groups/reorder', 'ReorderEvidenceGroupsInput', 'EvidenceCatalogOut'),
    ('evidence_remove_group', 'post', '/api/v1/cases/{case_id}/evidence-catalog/groups/{group_id}/remove', 'EvidenceGroupRevisionInput', 'EvidenceCatalogOut'),
    ('evidence_add_item', 'post', '/api/v1/cases/{case_id}/evidence-catalog/items', 'AddEvidenceItemInput', 'EvidenceCatalogOut'),
    ('evidence_update_item', 'patch', '/api/v1/cases/{case_id}/evidence-catalog/items/{item_id}', 'UpdateEvidenceItemInput', 'EvidenceCatalogOut'),
    ('evidence_move_item', 'post', '/api/v1/cases/{case_id}/evidence-catalog/items/{item_id}/move', 'MoveEvidenceItemInput', 'EvidenceCatalogOut'),
    ('evidence_remove_item', 'post', '/api/v1/cases/{case_id}/evidence-catalog/items/{item_id}/remove', 'EvidenceItemRevisionInput', 'EvidenceCatalogOut'),
    ('evidence_confirm_item', 'post', '/api/v1/cases/{case_id}/evidence-catalog/items/{item_id}/confirm', 'EvidenceItemRevisionInput', 'EvidenceCatalogOut'),
    ('evidence_reject_item', 'post', '/api/v1/cases/{case_id}/evidence-catalog/items/{item_id}/reject', 'EvidenceItemRevisionInput', 'EvidenceCatalogOut'),
    ('evidence_add_binding', 'post', '/api/v1/cases/{case_id}/evidence-catalog/bindings', 'AddEvidenceBindingInput', 'EvidenceCatalogOut'),
    ('evidence_remove_binding', 'post', '/api/v1/cases/{case_id}/evidence-catalog/bindings/{binding_id}/remove', 'RemoveEvidenceBindingInput', 'EvidenceCatalogOut'),
    ('evidence_confirm_binding', 'post', '/api/v1/cases/{case_id}/evidence-catalog/bindings/{binding_id}/confirm', 'EvidenceBindingRevisionInput', 'EvidenceCatalogOut'),
    ('evidence_reject_binding', 'post', '/api/v1/cases/{case_id}/evidence-catalog/bindings/{binding_id}/reject', 'EvidenceBindingRevisionInput', 'EvidenceCatalogOut'),
    ('evidence_accept_change', 'post', '/api/v1/cases/{case_id}/evidence-catalog/changes/{change_id}/accept', 'EvidenceChangeIdInput', 'EvidenceCatalogOut'),
    ('evidence_accept_all', 'post', '/api/v1/cases/{case_id}/evidence-catalog/changes/accept-all', 'EvidenceAcceptAllInput', 'EvidenceCatalogOut'),
    ('evidence_revert_change', 'post', '/api/v1/cases/{case_id}/evidence-catalog/changes/{change_id}/revert', 'EvidenceChangeIdInput', 'EvidenceCatalogOut'),
]
def contract(name):
    for module in (schemas, chart_export, graph_assist, table_graph_import):
        model = getattr(module, name, None)
        if model is not None:
            return model
    raise AttributeError(name)


models = {contract(name) for _, _, _, a, b in OPERATIONS for name in (a, b)} | {ErrorResponse}
# 上传/下载类操作在 OpenAPI 里不是 JSON 请求/响应；与既有 upload_materials、
# preview_* 保持同一处理方式。
MULTIPART = {
    'upload_materials': 'files',
    'preview_evidence_catalog_import': 'file',
    'graph_import_table_preview_upload': 'file',
}
BINARY_RESPONSE = {'preview_file_version', 'preview_pdf_page', 'download_file_version',
                   'export_evidence_catalog', 'export_evidence_bundle'}
_, schema = models_json_schema([(model, 'serialization') for model in models] + [(model, 'validation') for model in models], ref_template='#/components/schemas/{model}')
components = schema['$defs']
doc = {'openapi': '3.1.0', 'info': {'title': 'Legal Workbench API contract', 'version': '0.1.0'}, 'paths': {}, 'components': {'schemas': components}}
for operation, method, path, input_name, output_name in OPERATIONS:
    model = contract(input_name)
    # Models are output in both modes: choose mode-specific names where they differ.
    in_schema = model.model_json_schema(ref_template='#/components/schemas/{model}')
    path_names = [part.split('}')[0] for part in path.split('{')[1:]]
    params = [{'name': name, 'in': 'path', 'required': True, 'schema': {'type': 'string', 'format': 'uuid'}} for name in path_names]
    props = in_schema.get('properties', {})
    if method == 'get':
        params += [{'name': name, 'in': 'query', 'required': name in in_schema.get('required', []), 'schema': value} for name, value in props.items() if name not in path_names]
    output_key = output_name + '-Output' if output_name + '-Output' in components else output_name
    input_key = input_name + '-Input' if input_name + '-Input' in components else input_name
    item = {'operationId': operation, 'parameters': params, 'responses': {'200': {'description': 'Success', 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/' + output_key}}}}, 'default': {'description': 'Structured error', 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/ErrorResponse'}}}}}}
    for status in ('400', '403', '404', '409', '422', '503'):
        item['responses'][status] = {'description': 'Structured error', 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/ErrorResponse'}}}}
    if method != 'get':
        item['parameters'].append({'name': 'Origin', 'in': 'header', 'required': True, 'schema': {'type': 'string'}})
        item['requestBody'] = {'required': True, 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/' + input_key}}}}
    if operation == 'upload_materials':
        upload_schema = dict(in_schema)
        upload_schema.pop('$defs', None)
        upload_schema['properties'] = dict(in_schema['properties'], files={'type': 'array', 'minItems': 1, 'items': {'type': 'string', 'format': 'binary'}})
        upload_schema['required'] = [*in_schema['required'], 'files']
        item['requestBody']['content'] = {'multipart/form-data': {'schema': upload_schema}}
    if operation == 'preview_evidence_catalog_import':
        # case_id 只出现在路径上；正文是一个目录文件，与 upload_materials 同类。
        item['requestBody'] = {'required': True, 'content': {'multipart/form-data': {'schema': {
            'type': 'object', 'additionalProperties': False, 'required': ['file'],
            'properties': {'file': {'type': 'string', 'format': 'binary'}}}}}}
    if operation == 'graph_import_table_preview_upload':
        item['requestBody'] = {'required': True, 'content': {'multipart/form-data': {'schema': {
            'type': 'object', 'additionalProperties': False, 'required': ['file'],
            'properties': {
                'file': {'type': 'string', 'format': 'binary'},
                'sheet_name': {'type': ['string', 'null']},
                'header_row': {'type': 'integer', 'minimum': 1, 'maximum': 100},
            }}}}}
    if operation in ('preview_file_version', 'preview_pdf_page', 'download_file_version'):
        media_types = ('application/pdf', 'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'text/plain') if operation == 'preview_file_version' else ('application/octet-stream',)
        if operation == 'preview_pdf_page':
            media_types = ('image/png',)
        item['responses']['200']['content'] = {media: {'schema': {'type': 'string', 'format': 'binary'}} for media in media_types}
    if operation == 'export_chart_citations':
        item['responses']['200']['content'].update(
            {media: {'schema': {'type': 'string'}} for media in ('text/markdown', 'text/csv')})
    if operation == 'export_evidence_catalog':
        item['responses']['200']['content'] = {'text/csv': {'schema': {'type': 'string'}}}
    if operation == 'export_evidence_bundle':
        item['responses']['200']['content'] = {'application/zip': {'schema': {'type': 'string', 'format': 'binary'}}}

    doc['paths'].setdefault(path, {})[method] = item
(ROOT / 'docs').mkdir(exist_ok=True)
(ROOT / 'docs' / 'openapi.json').write_text(
    json.dumps(doc, ensure_ascii=False, indent=2) + '\n',
    encoding='utf-8',
)
