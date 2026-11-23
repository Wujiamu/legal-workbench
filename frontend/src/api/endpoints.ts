/** Typed wrappers over the frozen API contract; UI never builds raw URLs. */
import { api, query } from './client';
import type { Page } from './client';
import type { components } from './types';

/**
 * Types are aliases of `types.ts`, which `openapi-typescript` generates from
 * `docs/openapi.json`.  Re-declaring shapes here is how the UI silently drifts
 * from the frozen contract, so every name below resolves to the generated one.
 */
type Schemas = components['schemas'];
export type { Schemas };

export type CaseOverview = Schemas['CaseOverview'];
export type Deadline = Schemas['Deadline'];
export type Annotation = Schemas['AnnotationOutput'];
export type CaseSummary = Schemas['CaseSummary'];
export type Coverage = Schemas['Coverage'];
export type IntakeStatus = Schemas['IntakeStatus'];
export type JobStatus = Schemas['JobStatus'];
export type MaterialSummary = Schemas['MaterialSummary'];
export type JobItemError = Schemas['JobItemError'];
export type JobStatusOutput = Schemas['GetJobStatusOutput'];
export type SourceLocation = Schemas['SourceLocation'];
export type ReadSourceOutput = Schemas['ReadSourceOutput'];
export type SearchHit = Schemas['SearchHit-Output'];
export type SearchOutput = Schemas['SearchCaseOutput-Output'];
export type IntakeItem = Schemas['IntakeItem'];
export type AnalysisSummary = Schemas['AnalysisSummary'];
export type AnalysisVersionSummary = Schemas['AnalysisVersionSummary'];
export type AnalysisVersionOutput = Schemas['GetAnalysisVersionOutput'];
export type GetOriginalOutput = Schemas['GetOriginalOutput'];
export type CreateWorkingCopyOutput = Schemas['CreateWorkingCopyOutput'];
export type TimeEntryOut = Schemas['TimeEntryOut'];
export type TimeDayOutput = Schemas['TimeDayOutput'];
export type DashboardDeadlineItem = Schemas['DashboardDeadlineItem'];
export type DashboardDeadlinesOutput = Schemas['DashboardDeadlinesOutput'];

export const casesApi = {
  dashboard: (offset: number, limit: number, signal?: AbortSignal) =>
    api.get<Page<CaseOverview>>(`/api/v1/dashboard${query({ offset, limit })}`, signal),
  /** 首页跨案期限时间轴：全部未归档案件的期限，不分页（不受 dashboard 50 条限制）。 */
  deadlines: (signal?: AbortSignal) =>
    api.get<DashboardDeadlinesOutput>('/api/v1/dashboard/deadlines', signal),
  overview: (caseId: string, signal?: AbortSignal) =>
    api.get<CaseOverview>(`/api/v1/cases/${caseId}`, signal),
  schedule: (caseId: string, body: Omit<Schemas['CaseScheduleInput'], 'case_id'>) =>
    api.patch<CaseOverview>(`/api/v1/cases/${caseId}/schedule`, { case_id: caseId, ...body }),
  create: (title: string, description?: string) =>
    api.post<Schemas['CreateCaseOutput']>('/api/v1/cases', { title, description }),
  list: (offset: number, limit: number, signal?: AbortSignal, archived?: boolean) =>
    api.get<Page<CaseSummary>>(
      `/api/v1/cases${query({ offset, limit,
        archived: archived === undefined ? undefined : String(archived) })}`, signal),
  archive: (caseId: string, expectedRevision: number) =>
    api.post<CaseOverview>(`/api/v1/cases/${caseId}/archive`,
      { case_id: caseId, expected_revision: expectedRevision }),
  unarchive: (caseId: string, expectedRevision: number) =>
    api.post<CaseOverview>(`/api/v1/cases/${caseId}/unarchive`,
      { case_id: caseId, expected_revision: expectedRevision }),
};

/** 工时（P2 持久化）：计时/补录/日视图/按标题汇总。 */
export const timeApi = {
  day: (caseId: string, workDate: string, signal?: AbortSignal) =>
    api.get<TimeDayOutput>(`/api/v1/cases/${caseId}/time${query({ work_date: workDate })}`,
      signal),
  start: (caseId: string, body: { node_id?: string | null; node_title: string }) =>
    api.post<TimeEntryOut>(`/api/v1/cases/${caseId}/time/start`, { case_id: caseId, ...body }),
  stop: (caseId: string, entryId: string) =>
    api.post<TimeEntryOut>(`/api/v1/cases/${caseId}/time/entries/${entryId}/stop`,
      { case_id: caseId, entry_id: entryId }),
  add: (caseId: string, body: { node_id?: string | null; node_title: string; hours: number;
                                work_date: string; note?: string | null }) =>
    api.post<TimeEntryOut>(`/api/v1/cases/${caseId}/time/entries`, { case_id: caseId, ...body }),
  remove: (caseId: string, entryId: string) =>
    api.post<TimeEntryOut>(`/api/v1/cases/${caseId}/time/entries/${entryId}/remove`,
      { case_id: caseId, entry_id: entryId }),
};

export const materialsApi = {
  list: (caseId: string, params: { category?: string; status?: IntakeStatus; offset: number; limit: number },
         signal?: AbortSignal) =>
    api.get<Schemas['ListMaterialsOutput-Output']>(
      `/api/v1/cases/${caseId}/materials${query(params)}`, signal),
  acquisition: (caseId: string, materialId: string, acquisition: string, expectedRevision: number) =>
    api.patch<Schemas['RevisionOutput']>(
      `/api/v1/cases/${caseId}/materials/${materialId}/acquisition`,
      { case_id: caseId, material_id: materialId, acquisition, expected_revision: expectedRevision }),
  classify: (caseId: string, materialId: string, category: string, expectedRevision: number) =>
    api.patch<Schemas['RevisionOutput']>(
      `/api/v1/cases/${caseId}/materials/${materialId}/classification`,
      { case_id: caseId, material_id: materialId, category, expected_revision: expectedRevision }),
  rename: (caseId: string, materialId: string, displayName: string, expectedRevision: number) =>
    api.patch<Schemas['RevisionOutput']>(
      `/api/v1/cases/${caseId}/materials/${materialId}/display-name`,
      { case_id: caseId, material_id: materialId, display_name: displayName,
        expected_revision: expectedRevision }),
  intakeItems: (caseId: string, intakeId: string, offset: number, limit: number,
                signal?: AbortSignal) =>
    api.get<Page<IntakeItem>>(
      `/api/v1/cases/${caseId}/intakes/${intakeId}/items${query({ offset, limit })}`, signal),
  job: (caseId: string, jobId: string, signal?: AbortSignal) =>
    api.get<JobStatusOutput>(`/api/v1/cases/${caseId}/jobs/${jobId}`, signal),
  retry: (caseId: string, jobId: string) =>
    api.post<Schemas['RetryJobOutput']>(
      `/api/v1/cases/${caseId}/jobs/${jobId}/retry`, {}),
  upload: (caseId: string, form: FormData, signal?: AbortSignal) =>
    api.upload<Schemas['BrowserUploadInput'] & { intake_id: string; job_id: string }>(
      `/api/v1/cases/${caseId}/intakes/upload`, form, signal),
  createWorkingCopy: (caseId: string, fileVersionId: string, displayName?: string) =>
    api.post<Schemas['CreateWorkingCopyOutput']>(
      `/api/v1/cases/${caseId}/working-copies`,
      { case_id: caseId, file_version_id: fileVersionId,
        ...(displayName === undefined ? {} : { display_name: displayName }) }),
  registerVersion: (caseId: string, workingCopyId: string, expectedRevision: number) =>
    api.post<Schemas['RegisterFileVersionOutput']>(
      `/api/v1/cases/${caseId}/working-copies/${workingCopyId}/file-versions`,
      { case_id: caseId, working_copy_id: workingCopyId, expected_revision: expectedRevision }),
  reparse: (caseId: string, fileVersionId: string, forceOcrPages: number[] = []) =>
    api.post<Schemas['ReparseOutput']>(
      `/api/v1/cases/${caseId}/file-versions/${fileVersionId}/parse-runs`,
      { case_id: caseId, file_version_id: fileVersionId, force_ocr_pages: forceOcrPages }),
  original: (caseId: string, materialId: string, signal?: AbortSignal) =>
    api.get<Schemas['GetOriginalOutput']>(
      `/api/v1/cases/${caseId}/materials/${materialId}/original`, signal),
  /** 目录导入前需要已上传目录文件的字节（提交时只认 import_material_id）。 */
  downloadBlob: (caseId: string, fileVersionId: string, signal?: AbortSignal) =>
    api.blob(`/api/v1/cases/${caseId}/file-versions/${fileVersionId}/download`, signal),
  coverageMatrix: (caseId: string, graphId?: string, signal?: AbortSignal) =>
    api.get<Schemas['CorpusCoverageMatrix-Output']>(
      `/api/v1/cases/${caseId}/materials/coverage-matrix${query({ graph_id: graphId })}`, signal),
};

export const searchApi = {
  search: (caseId: string, body: { query: string; mode: string; scope?: string;
                                  currency?: string; offset?: number; limit?: number },
           signal?: AbortSignal) =>
    api.post<SearchOutput>(`/api/v1/cases/${caseId}/search`, { case_id: caseId, ...body }, signal),
  readSource: (caseId: string, sourceRef: string, contextChars = 200, signal?: AbortSignal) =>
    api.get<ReadSourceOutput>(
      `/api/v1/cases/${caseId}/sources/${sourceRef}${query({ context_chars: contextChars })}`,
      signal),
  extractedText: (caseId: string, fileVersionId: string, signal?: AbortSignal) =>
    api.get<Schemas['ExtractedTextOutput-Output']>(
      `/api/v1/cases/${caseId}/file-versions/${fileVersionId}/text`, signal),
  indexStatus: (caseId: string, signal?: AbortSignal) =>
    api.get<Coverage>(`/api/v1/cases/${caseId}/index-status`, signal),
};

export const analysisApi = {
  list: (caseId: string, offset: number, limit: number, signal?: AbortSignal) =>
    api.get<Page<AnalysisSummary>>(
      `/api/v1/cases/${caseId}/analyses${query({ offset, limit })}`, signal),
  save: (caseId: string, body: { title: string; content: string; statement_kind: string;
                                 source_refs: string[]; analysis_id?: string;
                                 expected_revision?: number }) =>
    api.post<Schemas['SaveAnalysisOutput']>(
      `/api/v1/cases/${caseId}/analyses`, { case_id: caseId, ...body }),
  review: (caseId: string, analysisId: string, versionId: string, expectedRevision: number,
           reviewStatus: string) =>
    api.post<Schemas['ReviewAnalysisOutput']>(
      `/api/v1/cases/${caseId}/analyses/${analysisId}/versions/${versionId}/review`,
      { case_id: caseId, analysis_id: analysisId, version_id: versionId,
        expected_revision: expectedRevision, review_status: reviewStatus }),
  versions: (caseId: string, analysisId: string, offset: number, limit: number,
             signal?: AbortSignal) =>
    api.get<Page<AnalysisVersionSummary>>(
      `/api/v1/cases/${caseId}/analyses/${analysisId}/versions${query({ offset, limit })}`,
      signal),
  version: (caseId: string, analysisId: string, versionId: string, signal?: AbortSignal) =>
    api.get<AnalysisVersionOutput>(
      `/api/v1/cases/${caseId}/analyses/${analysisId}/versions/${versionId}`, signal),
};

/** Code-point offsets from the API, converted for JavaScript's UTF-16 strings. */
export function codePointToUtf16(text: string, codePointOffset: number): number {
  let units = 0;
  let seen = 0;
  for (const char of text) {
    if (seen >= codePointOffset) break;
    units += char.length;
    seen += 1;
  }
  return units;
}

export const annotationsApi = {
  list: (caseId: string, term: string, offset: number, limit: number,
         fileVersionId?: string, signal?: AbortSignal) =>
    api.get<Page<Annotation>>(`/api/v1/cases/${caseId}/annotations${query({
      query: term, file_version_id: fileVersionId, offset, limit,
    })}`, signal),
  create: (caseId: string, body: Omit<Schemas['CreateAnnotationInput'], 'case_id'>) =>
    api.post<Annotation>(`/api/v1/cases/${caseId}/annotations`, { case_id: caseId, ...body }),
  review: (item: Annotation, text: string) =>
    api.post<Annotation>(`/api/v1/cases/${item.case_id}/annotations/${item.annotation_id}/review`,
      { case_id: item.case_id, annotation_id: item.annotation_id, reviewed_text: text }),
};

/** 办案进度（P4）：写命令返回完整快照；类型全部来自生成契约。 */
export type ProgressSnapshot = Schemas['ProgressSnapshot-Output'];
export type ProgressNodeOut = Schemas['ProgressNodeOut'];
export type ProgressChangeOut = Schemas['ProgressChangeOut'];
export type ProgressRulesOutput = Schemas['ProgressRulesOutput'];
export type ChangeSeqsOutput = Schemas['ChangeSeqsOutput'];

/** 主体关系图（P3 契约）：读取整案工作区，写命令返回同一工作区快照。 */
export type GraphWorkspaceOut = Schemas['GraphWorkspace-Output'];
export type GraphRulesResponse = Schemas['GraphRulesOutput'];
export type SnapshotOutSchemas = Schemas['SnapshotOut'];
export type RemovalImpact = Schemas['RemovalImpactOut'];

export const graphApi = {
  rules: (signal?: AbortSignal) =>
    api.get<GraphRulesResponse>('/api/v1/graph/rules', signal),
  /** 整案工作区；传 graphId 只收窄该图的成员、分组与快照。 */
  workspace: (caseId: string, graphId?: string, signal?: AbortSignal) =>
    api.get<GraphWorkspaceOut>(
      `/api/v1/cases/${caseId}/graphs${graphId ? `/${graphId}` : ''}`, signal),
  snapshots: (caseId: string, graphId: string, signal?: AbortSignal) =>
    api.get<Schemas['SnapshotListOutput']>(
      `/api/v1/cases/${caseId}/graphs/${graphId}/snapshots`, signal),
  snapshot: (caseId: string, snapshotId: string, signal?: AbortSignal) =>
    api.get<SnapshotOutSchemas>(`/api/v1/cases/${caseId}/snapshots/${snapshotId}`, signal),
  diff: (caseId: string, snapshotId: string, targetId?: string | null,
         signal?: AbortSignal) =>
    api.get<Schemas['SnapshotDiffOut']>(
      `/api/v1/cases/${caseId}/snapshots/${snapshotId}/diff${query({ target_id: targetId })}`,
      signal),
  removalImpact: (caseId: string, target: { entity_id?: string; relation_id?: string },
                  signal?: AbortSignal) =>
    api.get<RemovalImpact>(
      `/api/v1/cases/${caseId}/graph/removal-impact${query(target)}`, signal),

  addEntity: (caseId: string, body: Omit<Schemas['AddEntityInput'], 'case_id'>) =>
    api.post<GraphWorkspaceOut>(`/api/v1/cases/${caseId}/graph/entities`,
      { case_id: caseId, ...body }),
  updateEntity: (caseId: string, entityId: string,
                 body: Omit<Schemas['UpdateEntityInput'], 'case_id' | 'entity_id'>) =>
    api.patch<GraphWorkspaceOut>(`/api/v1/cases/${caseId}/graph/entities/${entityId}`,
      { case_id: caseId, entity_id: entityId, ...body }),
  removeEntity: (caseId: string, entityId: string, expectedRevision: number) =>
    api.post<GraphWorkspaceOut>(`/api/v1/cases/${caseId}/graph/entities/${entityId}/remove`,
      { case_id: caseId, entity_id: entityId, expected_revision: expectedRevision }),
  restoreEntity: (caseId: string, entityId: string, expectedRevision: number) =>
    api.post<GraphWorkspaceOut>(`/api/v1/cases/${caseId}/graph/entities/${entityId}/restore`,
      { case_id: caseId, entity_id: entityId, expected_revision: expectedRevision }),
  addAlias: (caseId: string, entityId: string, alias: string) =>
    api.post<GraphWorkspaceOut>(`/api/v1/cases/${caseId}/graph/entities/${entityId}/aliases`,
      { case_id: caseId, entity_id: entityId, alias }),
  removeAlias: (caseId: string, aliasId: string) =>
    api.post<GraphWorkspaceOut>(`/api/v1/cases/${caseId}/graph/aliases/${aliasId}/remove`,
      { case_id: caseId, alias_id: aliasId }),

  addRelation: (caseId: string, body: Omit<Schemas['AddGraphRelationInput'], 'case_id'>) =>
    api.post<GraphWorkspaceOut>(`/api/v1/cases/${caseId}/graph/relations`,
      { case_id: caseId, ...body }),
  updateRelation: (caseId: string, relationId: string,
                   body: Omit<Schemas['UpdateRelationInput-Input'], 'case_id' | 'relation_id'>) =>
    api.patch<GraphWorkspaceOut>(`/api/v1/cases/${caseId}/graph/relations/${relationId}`,
      { case_id: caseId, relation_id: relationId, ...body }),
  removeRelation: (caseId: string, relationId: string, expectedRevision: number) =>
    api.post<GraphWorkspaceOut>(`/api/v1/cases/${caseId}/graph/relations/${relationId}/remove`,
      { case_id: caseId, relation_id: relationId, expected_revision: expectedRevision }),
  restoreRelation: (caseId: string, relationId: string, expectedRevision: number) =>
    api.post<GraphWorkspaceOut>(`/api/v1/cases/${caseId}/graph/relations/${relationId}/restore`,
      { case_id: caseId, relation_id: relationId, expected_revision: expectedRevision }),

  addSource: (caseId: string, body: Omit<Schemas['AddSourceInput'], 'case_id'>) =>
    api.post<GraphWorkspaceOut>(`/api/v1/cases/${caseId}/graph/sources`,
      { case_id: caseId, ...body }),
  removeSource: (caseId: string, sourceId: string, downgradeTo?: string | null) =>
    api.post<GraphWorkspaceOut>(`/api/v1/cases/${caseId}/graph/sources/${sourceId}/remove`,
      { case_id: caseId, source_id: sourceId, downgrade_to: downgradeTo ?? null }),

  createGraph: (caseId: string, body: Omit<Schemas['CreateGraphInput'], 'case_id'>) =>
    api.post<GraphWorkspaceOut>(`/api/v1/cases/${caseId}/graph/graphs`,
      { case_id: caseId, ...body }),
  updateGraph: (caseId: string, graphId: string,
                body: Omit<Schemas['UpdateGraphInput'], 'case_id' | 'graph_id'>) =>
    api.patch<GraphWorkspaceOut>(`/api/v1/cases/${caseId}/graph/graphs/${graphId}`,
      { case_id: caseId, graph_id: graphId, ...body }),
  removeGraph: (caseId: string, graphId: string, expectedRevision: number) =>
    api.post<GraphWorkspaceOut>(`/api/v1/cases/${caseId}/graph/graphs/${graphId}/remove`,
      { case_id: caseId, graph_id: graphId, expected_revision: expectedRevision }),
  addToGraph: (caseId: string, graphId: string,
               body: Omit<Schemas['AddToGraphInput'], 'case_id' | 'graph_id'>) =>
    api.post<GraphWorkspaceOut>(`/api/v1/cases/${caseId}/graph/graphs/${graphId}/members`,
      { case_id: caseId, graph_id: graphId, ...body }),
  removeFromGraph: (caseId: string, graphId: string,
                    body: Omit<Schemas['RemoveFromGraphInput'], 'case_id' | 'graph_id'>) =>
    api.post<GraphWorkspaceOut>(`/api/v1/cases/${caseId}/graph/graphs/${graphId}/members/remove`,
      { case_id: caseId, graph_id: graphId, ...body }),
  saveLayout: (caseId: string, graphId: string,
               body: Omit<Schemas['SaveLayoutInput'], 'case_id' | 'graph_id'>) =>
    api.post<GraphWorkspaceOut>(`/api/v1/cases/${caseId}/graph/graphs/${graphId}/layout`,
      { case_id: caseId, graph_id: graphId, ...body }),
  upsertGroup: (caseId: string, graphId: string,
                body: Omit<Schemas['UpsertGroupInput'], 'case_id' | 'graph_id'>) =>
    api.post<GraphWorkspaceOut>(`/api/v1/cases/${caseId}/graph/graphs/${graphId}/groups`,
      { case_id: caseId, graph_id: graphId, ...body }),
  removeGroup: (caseId: string, groupId: string, expectedRevision: number) =>
    api.post<GraphWorkspaceOut>(`/api/v1/cases/${caseId}/graph/groups/${groupId}/remove`,
      { case_id: caseId, group_id: groupId, expected_revision: expectedRevision }),
  setNodeGroup: (caseId: string, graphId: string, entityId: string, groupId: string | null) =>
    api.post<GraphWorkspaceOut>(
      `/api/v1/cases/${caseId}/graph/graphs/${graphId}/nodes/${entityId}/group`,
      { case_id: caseId, graph_id: graphId, entity_id: entityId, group_id: groupId }),

  createSnapshot: (caseId: string, graphId: string,
                   body: Omit<Schemas['CreateSnapshotInput'], 'case_id' | 'graph_id'>) =>
    api.post<GraphWorkspaceOut>(`/api/v1/cases/${caseId}/graph/graphs/${graphId}/snapshots`,
      { case_id: caseId, graph_id: graphId, ...body }),
  restoreSnapshot: (caseId: string, snapshotId: string) =>
    api.post<GraphWorkspaceOut>(`/api/v1/cases/${caseId}/graph/snapshots/${snapshotId}/restore`,
      { case_id: caseId, snapshot_id: snapshotId }),
  addConsent: (caseId: string, snapshotId: string,
               body: Omit<Schemas['AddConsentInput'], 'case_id' | 'snapshot_id'>) =>
    api.post<GraphWorkspaceOut>(`/api/v1/cases/${caseId}/graph/snapshots/${snapshotId}/consents`,
      { case_id: caseId, snapshot_id: snapshotId, ...body }),
  acceptChange: (caseId: string, changeId: string) =>
    api.post<GraphWorkspaceOut>(`/api/v1/cases/${caseId}/graph/changes/${changeId}/accept`,
      { case_id: caseId, change_id: changeId }),
  acceptAll: (caseId: string, changeIds?: string[], expectedSeq?: number | null) =>
    api.post<GraphWorkspaceOut>(`/api/v1/cases/${caseId}/graph/changes/accept-all`,
      { case_id: caseId, change_ids: changeIds ?? [], expected_seq: expectedSeq ?? null }),
  revertChange: (caseId: string, changeId: string) =>
    api.post<GraphWorkspaceOut>(`/api/v1/cases/${caseId}/graph/changes/${changeId}/revert`,
      { case_id: caseId, change_id: changeId }),
};

/** 证据目录（P3 契约）：上传预览只解析不落库，提交导入全部为待确认草稿。 */
export type EvidenceCatalog = Schemas['EvidenceCatalogOut-Output'];

export const evidenceApi = {
  catalog: (caseId: string, signal?: AbortSignal) =>
    api.get<EvidenceCatalog>(`/api/v1/cases/${caseId}/evidence-catalog`, signal),
  exportUrl: (caseId: string) => `/api/v1/cases/${caseId}/evidence-catalog/export`,
  /** 解析已上传到本案的目录文件（DOCX / XLSX / CSV），不写库。 */
  previewImport: (caseId: string, form: FormData, signal?: AbortSignal) =>
    api.upload<Schemas['ImportPreviewOutput']>(
      `/api/v1/cases/${caseId}/evidence-catalog/import/preview`, form, signal),
  importCatalog: (caseId: string,
                  body: Omit<Schemas['ImportEvidenceCatalogInput-Input'], 'case_id'>) =>
    api.post<EvidenceCatalog>(`/api/v1/cases/${caseId}/evidence-catalog/import`,
      { case_id: caseId, ...body }),

  addGroup: (caseId: string,
             body: Omit<Schemas['AddEvidenceGroupInput'], 'case_id'>) =>
    api.post<EvidenceCatalog>(`/api/v1/cases/${caseId}/evidence-catalog/groups`,
      { case_id: caseId, ...body }),
  updateGroup: (caseId: string, groupId: string,
                body: Omit<Schemas['UpdateEvidenceGroupInput'], 'case_id' | 'group_id'>) =>
    api.patch<EvidenceCatalog>(`/api/v1/cases/${caseId}/evidence-catalog/groups/${groupId}`,
      { case_id: caseId, group_id: groupId, ...body }),
  reorderGroups: (caseId: string, groupIds: string[]) =>
    api.post<EvidenceCatalog>(`/api/v1/cases/${caseId}/evidence-catalog/groups/reorder`,
      { case_id: caseId, group_ids: groupIds }),
  removeGroup: (caseId: string, groupId: string, expectedRevision: number) =>
    api.post<EvidenceCatalog>(
      `/api/v1/cases/${caseId}/evidence-catalog/groups/${groupId}/remove`,
      { case_id: caseId, group_id: groupId, expected_revision: expectedRevision }),

  addItem: (caseId: string, body: Omit<Schemas['AddEvidenceItemInput'], 'case_id'>) =>
    api.post<EvidenceCatalog>(`/api/v1/cases/${caseId}/evidence-catalog/items`,
      { case_id: caseId, ...body }),
  updateItem: (caseId: string, itemId: string,
               body: Omit<Schemas['UpdateEvidenceItemInput'], 'case_id' | 'item_id'>) =>
    api.patch<EvidenceCatalog>(`/api/v1/cases/${caseId}/evidence-catalog/items/${itemId}`,
      { case_id: caseId, item_id: itemId, ...body }),
  moveItem: (caseId: string, itemId: string,
             body: Omit<Schemas['MoveEvidenceItemInput'], 'case_id' | 'item_id'>) =>
    api.post<EvidenceCatalog>(`/api/v1/cases/${caseId}/evidence-catalog/items/${itemId}/move`,
      { case_id: caseId, item_id: itemId, ...body }),
  removeItem: (caseId: string, itemId: string, expectedRevision: number) =>
    api.post<EvidenceCatalog>(`/api/v1/cases/${caseId}/evidence-catalog/items/${itemId}/remove`,
      { case_id: caseId, item_id: itemId, expected_revision: expectedRevision }),
  confirmItem: (caseId: string, itemId: string, expectedRevision: number) =>
    api.post<EvidenceCatalog>(`/api/v1/cases/${caseId}/evidence-catalog/items/${itemId}/confirm`,
      { case_id: caseId, item_id: itemId, expected_revision: expectedRevision }),
  rejectItem: (caseId: string, itemId: string, expectedRevision: number) =>
    api.post<EvidenceCatalog>(`/api/v1/cases/${caseId}/evidence-catalog/items/${itemId}/reject`,
      { case_id: caseId, item_id: itemId, expected_revision: expectedRevision }),

  addBinding: (caseId: string,
               body: Omit<Schemas['AddEvidenceBindingInput'], 'case_id'>) =>
    api.post<EvidenceCatalog>(`/api/v1/cases/${caseId}/evidence-catalog/bindings`,
      { case_id: caseId, ...body }),
  removeBinding: (caseId: string, bindingId: string) =>
    api.post<EvidenceCatalog>(
      `/api/v1/cases/${caseId}/evidence-catalog/bindings/${bindingId}/remove`,
      { case_id: caseId, binding_id: bindingId }),
  confirmBinding: (caseId: string, bindingId: string, expectedRevision: number) =>
    api.post<EvidenceCatalog>(
      `/api/v1/cases/${caseId}/evidence-catalog/bindings/${bindingId}/confirm`,
      { case_id: caseId, binding_id: bindingId, expected_revision: expectedRevision }),
  rejectBinding: (caseId: string, bindingId: string, expectedRevision: number) =>
    api.post<EvidenceCatalog>(
      `/api/v1/cases/${caseId}/evidence-catalog/bindings/${bindingId}/reject`,
      { case_id: caseId, binding_id: bindingId, expected_revision: expectedRevision }),

  acceptChange: (caseId: string, changeId: string) =>
    api.post<EvidenceCatalog>(
      `/api/v1/cases/${caseId}/evidence-catalog/changes/${changeId}/accept`,
      { case_id: caseId, change_id: changeId }),
  acceptAll: (caseId: string, changeIds?: string[], expectedSeq?: number | null) =>
    api.post<EvidenceCatalog>(`/api/v1/cases/${caseId}/evidence-catalog/changes/accept-all`,
      { case_id: caseId, change_ids: changeIds ?? [], expected_seq: expectedSeq ?? null }),
  revertChange: (caseId: string, changeId: string) =>
    api.post<EvidenceCatalog>(
      `/api/v1/cases/${caseId}/evidence-catalog/changes/${changeId}/revert`,
      { case_id: caseId, change_id: changeId }),
};

/** 诉请金额（P3 契约）：读快照 + 金额候选；写命令返回整份快照。 */
export type ClaimSnapshotOut = Schemas['ClaimSnapshot-Output'];
export type ClaimAmountCandidatesOut = Schemas['ClaimAmountCandidates'];

export const claimsApi = {
  snapshot: (caseId: string, signal?: AbortSignal) =>
    api.get<ClaimSnapshotOut>(`/api/v1/cases/${caseId}/claims`, signal),
  /** 本案 exact_values(kind='amount') 候选；金额无法精确到分时 amount_cents 为 null。 */
  amountCandidates: (caseId: string, term?: string, signal?: AbortSignal) =>
    api.get<ClaimAmountCandidatesOut>(
      `/api/v1/cases/${caseId}/claims/amount-candidates${query({ query: term })}`, signal),
  seq: (caseId: string, signal?: AbortSignal) =>
    api.get<ChangeSeqsOutput>(`/api/v1/cases/${caseId}/changes/seq`, signal),

  addGroup: (caseId: string, body: Omit<Schemas['AddClaimGroupInput'], 'case_id'>) =>
    api.post<ClaimSnapshotOut>(`/api/v1/cases/${caseId}/claims/groups`,
      { case_id: caseId, ...body }),
  updateGroup: (caseId: string, groupId: string,
                body: Omit<Schemas['UpdateClaimGroupInput'], 'case_id' | 'group_id'>) =>
    api.patch<ClaimSnapshotOut>(`/api/v1/cases/${caseId}/claims/groups/${groupId}`,
      { case_id: caseId, group_id: groupId, ...body }),
  moveGroup: (caseId: string, groupId: string, expectedRevision: number,
              direction: 'up' | 'down') =>
    api.post<ClaimSnapshotOut>(`/api/v1/cases/${caseId}/claims/groups/${groupId}/move`,
      { case_id: caseId, group_id: groupId, expected_revision: expectedRevision, direction }),
  removeGroup: (caseId: string, groupId: string, expectedRevision: number) =>
    api.post<ClaimSnapshotOut>(`/api/v1/cases/${caseId}/claims/groups/${groupId}/remove`,
      { case_id: caseId, group_id: groupId, expected_revision: expectedRevision }),

  addItem: (caseId: string, body: Omit<Schemas['AddClaimItemInput'], 'case_id'>) =>
    api.post<ClaimSnapshotOut>(`/api/v1/cases/${caseId}/claims/items`,
      { case_id: caseId, ...body }),
  updateItem: (caseId: string, itemId: string,
               body: Omit<Schemas['UpdateClaimItemInput'], 'case_id' | 'item_id'>) =>
    api.patch<ClaimSnapshotOut>(`/api/v1/cases/${caseId}/claims/items/${itemId}`,
      { case_id: caseId, item_id: itemId, ...body }),
  moveItem: (caseId: string, itemId: string, expectedRevision: number,
             direction: 'up' | 'down') =>
    api.post<ClaimSnapshotOut>(`/api/v1/cases/${caseId}/claims/items/${itemId}/move`,
      { case_id: caseId, item_id: itemId, expected_revision: expectedRevision, direction }),
  removeItem: (caseId: string, itemId: string, expectedRevision: number) =>
    api.post<ClaimSnapshotOut>(`/api/v1/cases/${caseId}/claims/items/${itemId}/remove`,
      { case_id: caseId, item_id: itemId, expected_revision: expectedRevision }),
  restoreItem: (caseId: string, itemId: string, expectedRevision: number) =>
    api.post<ClaimSnapshotOut>(`/api/v1/cases/${caseId}/claims/items/${itemId}/restore`,
      { case_id: caseId, item_id: itemId, expected_revision: expectedRevision }),

  acceptChange: (caseId: string, changeId: string) =>
    api.post<ClaimSnapshotOut>(`/api/v1/cases/${caseId}/claims/changes/${changeId}/accept`,
      { case_id: caseId, change_id: changeId }),
  acceptAll: (caseId: string, changeIds?: string[], expectedSeq?: number | null) =>
    api.post<ClaimSnapshotOut>(`/api/v1/cases/${caseId}/claims/changes/accept-all`,
      { case_id: caseId, change_ids: changeIds ?? [], expected_seq: expectedSeq ?? null }),
  revertChange: (caseId: string, changeId: string) =>
    api.post<ClaimSnapshotOut>(`/api/v1/cases/${caseId}/claims/changes/${changeId}/revert`,
      { case_id: caseId, change_id: changeId }),
};

export const progressApi = {
  rules: (signal?: AbortSignal) =>
    api.get<ProgressRulesOutput>('/api/v1/progress/rules', signal),
  snapshot: (caseId: string, signal?: AbortSignal) =>
    api.get<ProgressSnapshot>(`/api/v1/cases/${caseId}/progress`, signal),
  seqs: (caseId: string, signal?: AbortSignal) =>
    api.get<ChangeSeqsOutput>(`/api/v1/cases/${caseId}/changes/seq`, signal),
  addNode: (caseId: string, body: Omit<Schemas['AddNodeInput'], 'case_id'>) =>
    api.post<ProgressSnapshot>(`/api/v1/cases/${caseId}/progress/nodes`, { case_id: caseId, ...body }),
  updateNode: (caseId: string, nodeId: string, expectedRevision: number,
               changes: Schemas['NodeChanges']) =>
    api.patch<ProgressSnapshot>(`/api/v1/cases/${caseId}/progress/nodes/${nodeId}`,
      { case_id: caseId, node_id: nodeId, expected_revision: expectedRevision, changes }),
  moveNode: (caseId: string, nodeId: string, expectedRevision: number,
             newParentId: string | null, beforeNodeId?: string | null) =>
    api.post<ProgressSnapshot>(`/api/v1/cases/${caseId}/progress/nodes/${nodeId}/move`,
      { case_id: caseId, node_id: nodeId, expected_revision: expectedRevision,
        new_parent_id: newParentId, before_node_id: beforeNodeId ?? null }),
  setActiveStage: (caseId: string, stageId: string, expectedRevision: number) =>
    api.post<ProgressSnapshot>(`/api/v1/cases/${caseId}/progress/active-stage`,
      { case_id: caseId, stage_id: stageId, expected_revision: expectedRevision }),
  insertFragment: (caseId: string, fragmentKey: string, parentId?: string | null,
                   beforeNodeId?: string | null) =>
    api.post<ProgressSnapshot>(`/api/v1/cases/${caseId}/progress/fragments`,
      { case_id: caseId, fragment_key: fragmentKey, parent_id: parentId ?? null,
        before_node_id: beforeNodeId ?? null }),
  removeNode: (caseId: string, nodeId: string, expectedRevision: number) =>
    api.post<ProgressSnapshot>(`/api/v1/cases/${caseId}/progress/nodes/${nodeId}/remove`,
      { case_id: caseId, node_id: nodeId, expected_revision: expectedRevision }),
  restoreNode: (caseId: string, nodeId: string, expectedRevision: number) =>
    api.post<ProgressSnapshot>(`/api/v1/cases/${caseId}/progress/nodes/${nodeId}/restore`,
      { case_id: caseId, node_id: nodeId, expected_revision: expectedRevision }),
  addRelation: (caseId: string, fromNodeId: string, toNodeId: string,
                relationType: string, note?: string | null) =>
    api.post<ProgressSnapshot>(`/api/v1/cases/${caseId}/progress/relations`,
      { case_id: caseId, from_node_id: fromNodeId, to_node_id: toNodeId,
        relation_type: relationType, note: note ?? null }),
  removeRelation: (caseId: string, relationId: string) =>
    api.post<ProgressSnapshot>(`/api/v1/cases/${caseId}/progress/relations/${relationId}/remove`,
      { case_id: caseId, relation_id: relationId }),
  link: (caseId: string, nodeId: string, target: Schemas['LinkTarget']) =>
    api.post<ProgressSnapshot>(`/api/v1/cases/${caseId}/progress/links`,
      { case_id: caseId, node_id: nodeId, target }),
  unlink: (caseId: string, linkId: string) =>
    api.post<ProgressSnapshot>(`/api/v1/cases/${caseId}/progress/links/${linkId}/remove`,
      { case_id: caseId, link_id: linkId }),
  acceptChange: (caseId: string, changeId: string) =>
    api.post<ProgressSnapshot>(`/api/v1/cases/${caseId}/progress/changes/${changeId}/accept`,
      { case_id: caseId, change_id: changeId }),
  acceptAll: (caseId: string, changeIds?: string[], expectedSeq?: number | null) =>
    api.post<ProgressSnapshot>(`/api/v1/cases/${caseId}/progress/changes/accept-all`,
      { case_id: caseId, change_ids: changeIds ?? [], expected_seq: expectedSeq ?? null }),
  revertChange: (caseId: string, changeId: string) =>
    api.post<ProgressSnapshot>(`/api/v1/cases/${caseId}/progress/changes/${changeId}/revert`,
      { case_id: caseId, change_id: changeId }),
};

/** 图表一源三态导出、程序硬门禁、共现辅助、防漏审计与表格映射（全部类型来自生成契约）。 */
export type ChartPreflightOut = Schemas['ChartPreflight'];
export type ChartCitationTableOut = Schemas['ChartCitationTable-Output'];
export type ChartAuditBundleOut = Schemas['ChartAuditBundle-Output'];
export type ChartRequestIn = Schemas['ChartRequest'];
export type CitationRequestIn = Schemas['CitationRequest'];
export type CooccurrenceOut = Schemas['CooccurrenceOutput-Output'];
export type SegmentHintsOut = Schemas['SegmentHints-Output'];
export type CorpusCoverageMatrixOut = Schemas['CorpusCoverageMatrix-Output'];
export type GraphAuditOut = Schemas['GraphAuditOutput-Output'];
export type EntityCandidateOut = Schemas['EntityCandidateOutput-Output'];
export type TablePreviewOut = Schemas['TablePreviewOutput'];
export type TableCompileOut = Schemas['TableCompileOutput'];
export type TableCommitOut = Schemas['TableCommitOutput-Output'];

export const chartsApi = {
  preflight: (caseId: string, body: ChartRequestIn, signal?: AbortSignal) =>
    api.post<ChartPreflightOut>(`/api/v1/cases/${caseId}/charts/preflight`, body, signal),
  citationTableJson: (caseId: string, body: Omit<CitationRequestIn, 'format'>, signal?: AbortSignal) =>
    api.post<ChartCitationTableOut>(
      `/api/v1/cases/${caseId}/charts/citation-table`, { ...body, format: 'json' }, signal),
  citationTableText: (caseId: string, body: CitationRequestIn & { format: 'md' | 'csv' },
                      signal?: AbortSignal) =>
    api.text('POST', `/api/v1/cases/${caseId}/charts/citation-table`, body, signal),
  auditBundle: (caseId: string, params: {
    chart_type: 'graph' | 'claims';
    target_id: string;
    profile: 'client' | 'court';
    snapshot_id?: string | null;
  }, signal?: AbortSignal) =>
    api.get<ChartAuditBundleOut>(
      `/api/v1/cases/${caseId}/charts/audit-bundle${query(params)}`, signal),
};

export const graphAssistApi = {
  cooccurrence: (caseId: string, params: {
    entity_a_id: string;
    entity_b_id: string;
    scope?: 'originals' | 'working';
    limit?: number;
  }, signal?: AbortSignal) =>
    api.get<CooccurrenceOut>(
      `/api/v1/cases/${caseId}/graphs/assist/cooccurrence${query(params)}`, signal),
  segmentHints: (caseId: string, segmentId: string, signal?: AbortSignal) =>
    api.get<SegmentHintsOut>(
      `/api/v1/cases/${caseId}/graphs/assist/segment-hints${query({ segment_id: segmentId })}`,
      signal),
  audit: (caseId: string, params?: { graph_id?: string; min_mentions?: number },
          signal?: AbortSignal) =>
    api.get<GraphAuditOut>(
      `/api/v1/cases/${caseId}/graphs/assist/audit${query(params ?? {})}`, signal),
  entityCandidates: (caseId: string, materialIds?: string[], signal?: AbortSignal) => {
    const search = new URLSearchParams();
    for (const id of materialIds ?? []) {
      if (id) search.append('material_ids', id);
    }
    const qs = search.toString();
    return api.get<EntityCandidateOut>(
      `/api/v1/cases/${caseId}/graphs/assist/entity-candidates${qs ? `?${qs}` : ''}`, signal);
  },
};

export const tableImportApi = {
  previewMaterial: (caseId: string, body: Schemas['TablePreviewInput'], signal?: AbortSignal) =>
    api.post<TablePreviewOut>(
      `/api/v1/cases/${caseId}/graphs/import-table/preview`, body, signal),
  previewUpload: (caseId: string, form: FormData, signal?: AbortSignal) =>
    api.upload<TablePreviewOut>(
      `/api/v1/cases/${caseId}/graphs/import-table/preview-upload`, form, signal),
  compile: (caseId: string, body: Schemas['TableCompileInput'], signal?: AbortSignal) =>
    api.post<TableCompileOut>(
      `/api/v1/cases/${caseId}/graphs/import-table/compile`, body, signal),
  commit: (caseId: string, body: Schemas['TableCommitInput'], signal?: AbortSignal) =>
    api.post<TableCommitOut>(
      `/api/v1/cases/${caseId}/graphs/import-table/commit`, body, signal),
};
