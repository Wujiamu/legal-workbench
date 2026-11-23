"""Intake, material, file-version and job endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy.orm import Session

from .. import graph_assist, imports, jobs, materials, parsing
from ..errors import ErrorCode, WorkbenchError
from ..schemas import (CreateWorkingCopyInput, CreateWorkingCopyOutput, GetJobStatusOutput,
                       ImportMaterialsInput, ImportMaterialsOutput, IntakeStatus, JobStatus,
                       ListIntakeItemsOutput, ListMaterialsOutput, RegisterFileVersionInput,
                       RegisterFileVersionOutput, ReparseInput, ReparseOutput, RetryJobOutput,
                       RevisionOutput, UpdateAcquisitionInput, UpdateClassificationInput, UpdateDisplayNameInput)
from ..storage import Storage
from .deps import Guarded, get_db, get_storage
from .ids import uuid_of

router = APIRouter(prefix="/api/v1/cases/{case_id}", tags=["materials"], dependencies=[Guarded])


def _match(path_value: str, body_value, field: str) -> None:
    """A duplicated id in body and path must agree before any service call."""
    if str(body_value) != str(path_value):
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "路径与请求体中的标识不一致",
                             {"field": field, "path": str(path_value),
                              "body": str(body_value)})


@router.post("/intakes", response_model=ImportMaterialsOutput)
def import_materials(case_id: str, payload: ImportMaterialsInput,
                     session: Session = Depends(get_db),
                     storage: Storage = Depends(get_storage)) -> ImportMaterialsOutput:
    _match(case_id, payload.case_id, "case_id")
    created = imports.create_intake(
        session, storage, case_id=payload.case_id, source_path=payload.source_path,
        source_label=payload.source_label, request_key=payload.request_key,
    )
    return ImportMaterialsOutput(intake_id=created.intake_id, job_id=created.job_id)


@router.post("/intakes/upload", response_model=ImportMaterialsOutput)
def upload_materials(case_id: str, source_label: str = Form(...), request_key: str = Form(...),
                     relative_paths: list[str] = Form(...),
                     files: list[UploadFile] = File(...),
                     session: Session = Depends(get_db),
                     storage: Storage = Depends(get_storage)) -> ImportMaterialsOutput:
    if len(files) != len(relative_paths):
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "文件数量与相对路径数量不一致",
                             {"files": len(files), "relative_paths": len(relative_paths)})
    uploads = [(relative, upload.file) for relative, upload in zip(relative_paths, files)]
    created = imports.create_upload_intake(
        session, storage, case_id=uuid_of(case_id, "case_id"), source_label=source_label,
        request_key=request_key, uploads=uploads,
    )
    return ImportMaterialsOutput(intake_id=created.intake_id, job_id=created.job_id)


@router.get("/intakes/{intake_id}/items", response_model=ListIntakeItemsOutput)
def list_intake_items(case_id: str, intake_id: str, status: IntakeStatus | None = Query(None),
                      offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                      session: Session = Depends(get_db)) -> ListIntakeItemsOutput:
    return imports.list_intake_items(session, case_id=uuid_of(case_id, "case_id"),
                                     intake_id=uuid_of(intake_id, "intake_id"),
                                     status=status, offset=offset, limit=limit)


@router.get("/materials", response_model=ListMaterialsOutput)
def list_materials(case_id: str, category: str | None = Query(None),
                   status: IntakeStatus | None = Query(None), offset: int = Query(0, ge=0),
                   limit: int = Query(50, ge=1, le=200),
                   session: Session = Depends(get_db)) -> ListMaterialsOutput:
    items, total, case_total, categories = materials.list_materials(
        session, case_id=uuid_of(case_id, "case_id"),
        category=category, status=status, offset=offset, limit=limit)
    return ListMaterialsOutput(items=items, offset=offset, limit=limit, total=total,
                               case_total=case_total, categories=categories)


@router.get("/materials/coverage-matrix", response_model=graph_assist.CorpusCoverageMatrix)
def coverage_matrix(case_id: str, graph_id: str | None = None,
                    session: Session = Depends(get_db)) -> graph_assist.CorpusCoverageMatrix:
    return graph_assist.get_corpus_coverage_matrix(
        session, uuid_of(case_id, "case_id"),
        graph_id=uuid_of(graph_id, "graph_id") if graph_id is not None else None,
    )


@router.patch("/materials/{material_id}/acquisition", response_model=RevisionOutput)
def update_acquisition(case_id: str, material_id: str, payload: UpdateAcquisitionInput,
                       session: Session = Depends(get_db)) -> RevisionOutput:
    _match(case_id, payload.case_id, "case_id")
    _match(material_id, payload.material_id, "material_id")
    return RevisionOutput(revision=materials.update_acquisition(session, payload))


@router.patch("/materials/{material_id}/classification", response_model=RevisionOutput)
def update_classification(case_id: str, material_id: str, payload: UpdateClassificationInput,
                          session: Session = Depends(get_db)) -> RevisionOutput:
    _match(case_id, payload.case_id, "case_id")
    _match(material_id, payload.material_id, "material_id")
    revision = materials.update_classification(
        session, case_id=payload.case_id, material_id=payload.material_id,
        category=payload.category, expected_revision=payload.expected_revision,
    )
    return RevisionOutput(revision=revision)


@router.patch("/materials/{material_id}/display-name", response_model=RevisionOutput)
def update_display_name(case_id: str, material_id: str, payload: UpdateDisplayNameInput,
                        session: Session = Depends(get_db)) -> RevisionOutput:
    _match(case_id, payload.case_id, "case_id")
    _match(material_id, payload.material_id, "material_id")
    revision = materials.update_display_name(
        session, case_id=payload.case_id, material_id=payload.material_id,
        display_name=payload.display_name, expected_revision=payload.expected_revision,
    )
    return RevisionOutput(revision=revision)


@router.post("/working-copies", response_model=CreateWorkingCopyOutput)
def create_working_copy(case_id: str, payload: CreateWorkingCopyInput,
                        session: Session = Depends(get_db),
                        storage: Storage = Depends(get_storage)) -> CreateWorkingCopyOutput:
    _match(case_id, payload.case_id, "case_id")
    working_id, relative = materials.create_working_copy(
        session, storage, case_id=payload.case_id,
        file_version_id=payload.file_version_id, display_name=payload.display_name,
    )
    return CreateWorkingCopyOutput(working_copy_id=working_id, relative_path=relative)


@router.post("/working-copies/{working_copy_id}/file-versions",
             response_model=RegisterFileVersionOutput)
def register_file_version(case_id: str, working_copy_id: str,
                          payload: RegisterFileVersionInput,
                          session: Session = Depends(get_db),
                          storage: Storage = Depends(get_storage)
                          ) -> RegisterFileVersionOutput:
    _match(case_id, payload.case_id, "case_id")
    _match(working_copy_id, payload.working_copy_id, "working_copy_id")
    version_id, job_id = materials.register_file_version(
        session, storage, case_id=payload.case_id, working_copy_id=payload.working_copy_id,
        expected_revision=payload.expected_revision,
    )
    return RegisterFileVersionOutput(file_version_id=version_id, job_id=job_id)


@router.post("/file-versions/{file_version_id}/parse-runs", response_model=ReparseOutput)
def reparse(case_id: str, file_version_id: str, payload: ReparseInput,
            session: Session = Depends(get_db)) -> ReparseOutput:
    _match(case_id, payload.case_id, "case_id")
    _match(file_version_id, payload.file_version_id, "file_version_id")
    run_id, job_id = parsing.reparse(session, case_id=payload.case_id,
                                     file_version_id=payload.file_version_id,
                                     force_ocr_pages=payload.force_ocr_pages)
    return ReparseOutput(parse_run_id=run_id, job_id=job_id)


@router.get("/jobs/{job_id}", response_model=GetJobStatusOutput)
def get_job_status(case_id: str, job_id: str,
                   session: Session = Depends(get_db)) -> GetJobStatusOutput:
    return jobs.get_job_status(session, case_id=uuid_of(case_id, "case_id"),
                               job_id=uuid_of(job_id, "job_id"))


@router.post("/jobs/{job_id}/retry", response_model=RetryJobOutput)
def retry_job(case_id: str, job_id: str,
              session: Session = Depends(get_db)) -> RetryJobOutput:
    job = jobs.retry_job(session, case_id=uuid_of(case_id, "case_id"),
                         job_id=uuid_of(job_id, "job_id"))
    return RetryJobOutput(job_id=job.id, status=JobStatus(job.status))
