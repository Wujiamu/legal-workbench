/** Test-only wrapper: `SourcePanel` needs only `caseId` and a source ref. */
import { SourcePanel } from '../SearchPanel';
import type { SearchHit } from '../../api/endpoints';

export function SourcePanelHarness({ hit }: { hit: SearchHit }) {
  return <SourcePanel caseId="case-1" sourceRef={hit.source?.source_ref ?? null}
                      method={hit.source?.method}
                      fileVersionId={hit.source?.file_version_id} />;
}
