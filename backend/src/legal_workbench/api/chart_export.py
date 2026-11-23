"""Local chart previews, guarded export and read-only review bundles."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from .. import chart_export
from .deps import Guarded, get_db
from .ids import uuid_of

router = APIRouter(prefix="/api/v1/cases/{case_id}/charts",
                   tags=["chart-export"], dependencies=[Guarded])


@router.post("/preflight", response_model=chart_export.ChartPreflight)
def preflight(case_id: str, request: chart_export.ChartRequest,
              session: Session = Depends(get_db)) -> chart_export.ChartPreflight:
    return chart_export.preflight_chart_export(session, uuid_of(case_id, "case_id"), request)


@router.post("/citation-table", response_model=chart_export.ChartCitationTable,
             responses={200: {"content": {"text/markdown": {}, "text/csv": {}}}})
def citation_table(case_id: str, request: chart_export.CitationRequest,
                   session: Session = Depends(get_db)):
    result = chart_export.chart_citation_table(session, uuid_of(case_id, "case_id"), request, human=True)
    if request.format == "json":
        return result
    return PlainTextResponse(
        chart_export.render_citation_table(result, request.format),
        media_type="text/markdown" if request.format == "md" else "text/csv",
        headers={"Content-Disposition": f'attachment; filename="chart-citations.{request.format}"'},
    )


@router.get("/audit-bundle", response_model=chart_export.ChartAuditBundle)
def audit_bundle(case_id: str, chart_type: chart_export.ChartType, target_id: str,
                 profile: chart_export.ExternalProfile, snapshot_id: str | None = None,
                 session: Session = Depends(get_db)) -> chart_export.ChartAuditBundle:
    request = chart_export.ChartRequest(
        chart_type=chart_type, target_id=uuid_of(target_id, "target_id"), profile=profile,
        snapshot_id=uuid_of(snapshot_id, "snapshot_id") if snapshot_id is not None else None,
    )
    return chart_export.build_chart_audit_bundle(session, uuid_of(case_id, "case_id"), request)
