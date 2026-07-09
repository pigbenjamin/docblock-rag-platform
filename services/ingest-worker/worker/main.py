from __future__ import annotations

import asyncio
import json
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, Optional
from enum import Enum

import psycopg2
import uvicorn
from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel

from docblock_core.acl import ACLService
from docblock_core.config import settings
from docblock_core.storage import LocalFileStorage
from worker.tasks.build_chunks import run_build_chunks
from worker.tasks.ingest_chunks import run_ingest_chunks
from worker.tasks.marker_to_md import run_marker_to_md

app = FastAPI(title="Docblock Ingest Worker")

UPLOAD_DIR = Path("/data/uploads")
storage = LocalFileStorage(UPLOAD_DIR)

# In-memory fallback, used only for jobs that don't carry a resolvable
# (tenant_id, document_id) at submission time (e.g. a bare /jobs/marker call
# with no document_id, or job_ids from ad-hoc stage testing that aren't
# UUIDs). Jobs from the real upload flow persist to `ingest_jobs` instead,
# so status survives a worker restart.
_jobs: Dict[str, Dict[str, Any]] = {}


class JobStatus(str, Enum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"


class MarkerJobRequest(BaseModel):
    job_id: str
    pdf_path: str
    output_dir: str
    document_id: Optional[str] = None  # defaults to pdf stem if omitted (used as working directory name)


class BuildChunksJobRequest(BaseModel):
    job_id: str
    fixed_md: str
    out_json: str
    document_id: str  # DB UUID; caller (document-api) generates this at upload time
    source_path: Optional[str] = None
    title: Optional[str] = None
    original_filename: Optional[str] = None
    file_size: Optional[int] = None
    mime_type: Optional[str] = None
    created_by: Optional[str] = None


class IngestJobRequest(BaseModel):
    job_id: str
    chunk_block_json: str


class FullPipelineJobRequest(BaseModel):
    """Convenience endpoint: PDF → build chunks → ingest in sequence."""
    job_id: str
    pdf_path: str
    work_dir: str
    document_id: str  # DB UUID; caller (document-api) generates this at upload time
    source_path: Optional[str] = None
    title: Optional[str] = None
    original_filename: Optional[str] = None
    file_size: Optional[int] = None
    mime_type: Optional[str] = None
    created_by: Optional[str] = None
    access_rules: Optional[Dict[str, str]] = None  # e.g. {"department:A": "detail", "user:<uuid>": "detail"}


def _is_uuid(value: Optional[str]) -> bool:
    if not value:
        return False
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def _set_status(
    job_id: str,
    status: JobStatus,
    detail: str = "",
    *,
    stage: Optional[str] = None,
    tenant_id: Optional[str] = None,
    document_id: Optional[str] = None,
    created_by: Optional[str] = None,
    source_type: str = "pdf",
) -> None:
    _jobs[job_id] = {"status": status, "detail": detail, "stage": stage}

    if not (_is_uuid(job_id) and tenant_id and _is_uuid(document_id)):
        return  # no persistable identity yet - memory-only (e.g. ad-hoc /jobs/marker call)

    conn = psycopg2.connect(settings.db.pg_dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ingest_jobs (job_id, tenant_id, document_id, source_type, stage, status, detail, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (job_id) DO UPDATE SET
                  stage = EXCLUDED.stage,
                  status = EXCLUDED.status,
                  detail = EXCLUDED.detail,
                  updated_at = now()
                """,
                (job_id, tenant_id, document_id, source_type, stage or status.value, status.value, detail, created_by),
            )
    finally:
        conn.close()


def _update_document_source_path(tenant_id: str, document_id: str, source_path: str) -> None:
    conn = psycopg2.connect(settings.db.pg_dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE documents SET source_path = %s, updated_at = now() WHERE tenant_id = %s AND document_id = %s",
                (source_path, tenant_id, document_id),
            )
    finally:
        conn.close()


async def _run_in_thread(fn, *args):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fn, *args)


async def _bg_marker(job_id: str, pdf_path: str, output_dir: str, document_id: str = ""):
    _set_status(job_id, JobStatus.running, stage="marker")
    try:
        resolved_document_id = document_id or Path(pdf_path).stem
        await _run_in_thread(run_marker_to_md, job_id, resolved_document_id, pdf_path, output_dir)
        _set_status(job_id, JobStatus.done, stage="marker")
    except Exception:
        _set_status(job_id, JobStatus.failed, traceback.format_exc(), stage="marker")


async def _bg_build_chunks(
    job_id: str, fixed_md: str, out_json: str, source_path, document_id,
    title=None, original_filename=None, file_size=None, mime_type=None, created_by=None,
):
    tenant_id = settings.db.tenant_id

    _set_status(job_id, JobStatus.running, stage="build_chunks", tenant_id=tenant_id, document_id=document_id, created_by=created_by)
    try:
        await _run_in_thread(
            run_build_chunks, fixed_md, out_json, source_path, tenant_id, document_id,
            title, original_filename, file_size, mime_type, created_by,
        )
        _set_status(job_id, JobStatus.done, stage="build_chunks", tenant_id=tenant_id, document_id=document_id, created_by=created_by)
    except Exception:
        _set_status(job_id, JobStatus.failed, traceback.format_exc(), stage="build_chunks", tenant_id=tenant_id, document_id=document_id, created_by=created_by)


async def _bg_ingest(job_id: str, chunk_block_json: str, tenant_id=None, document_id=None, created_by=None):
    _set_status(job_id, JobStatus.running, stage="ingest", tenant_id=tenant_id, document_id=document_id, created_by=created_by)
    try:
        await _run_in_thread(run_ingest_chunks, chunk_block_json)
        _set_status(job_id, JobStatus.done, stage="ingest", tenant_id=tenant_id, document_id=document_id, created_by=created_by)
    except Exception:
        _set_status(job_id, JobStatus.failed, traceback.format_exc(), stage="ingest", tenant_id=tenant_id, document_id=document_id, created_by=created_by)


async def _bg_full_pipeline(
    job_id: str, pdf_path: str, work_dir: str, document_id: str, source_path,
    title=None, original_filename=None, file_size=None, mime_type=None,
    created_by=None, access_rules=None,
):
    tenant_id = settings.db.tenant_id

    _set_status(job_id, JobStatus.running, "stage: marker", stage="marker", tenant_id=tenant_id, document_id=document_id, created_by=created_by)
    try:
        md_path = await _run_in_thread(run_marker_to_md, job_id, document_id, pdf_path, work_dir)

        pdf_stem = Path(pdf_path).stem
        out_json = str(Path(work_dir) / f"{pdf_stem}.chunk_block.json")

        _set_status(job_id, JobStatus.running, "stage: build_chunks", stage="build_chunks", tenant_id=tenant_id, document_id=document_id, created_by=created_by)
        await _run_in_thread(
            run_build_chunks, md_path, out_json, source_path, tenant_id, document_id,
            title, original_filename, file_size, mime_type, created_by,
        )

        _set_status(job_id, JobStatus.running, "stage: ingest", stage="ingest", tenant_id=tenant_id, document_id=document_id, created_by=created_by)
        db_document_id, version = await _run_in_thread(run_ingest_chunks, out_json)

        if access_rules:
            _set_status(job_id, JobStatus.running, "stage: acl", stage="acl", tenant_id=tenant_id, document_id=document_id, created_by=created_by)
            acl_service = ACLService(pg_dsn=settings.db.pg_dsn, tenant_id=tenant_id)
            result = acl_service.write_access(document_id=db_document_id, access_map=access_rules)
            if not result["success"]:
                raise RuntimeError(f"ACL write failed: {result['errors']}")

        _set_status(job_id, JobStatus.running, "stage: finalize_storage", stage="finalize_storage", tenant_id=tenant_id, document_id=document_id, created_by=created_by)
        final_path = storage.finalize(tenant_id=tenant_id, document_id=db_document_id, version=version, temp_path=pdf_path)
        _update_document_source_path(tenant_id, db_document_id, str(final_path))

        _set_status(job_id, JobStatus.done, "all stages complete", stage="done", tenant_id=tenant_id, document_id=document_id, created_by=created_by)
    except Exception:
        _set_status(job_id, JobStatus.failed, traceback.format_exc(), stage="failed", tenant_id=tenant_id, document_id=document_id, created_by=created_by)


@app.post("/jobs/marker")
def submit_marker_job(req: MarkerJobRequest, bg: BackgroundTasks):
    _set_status(req.job_id, JobStatus.pending, stage="marker")
    bg.add_task(_bg_marker, req.job_id, req.pdf_path, req.output_dir, req.document_id or "")
    return {"job_id": req.job_id, "status": JobStatus.pending}


@app.post("/jobs/build-chunks")
def submit_build_chunks_job(req: BuildChunksJobRequest, bg: BackgroundTasks):
    tenant_id = settings.db.tenant_id
    _set_status(req.job_id, JobStatus.pending, stage="build_chunks", tenant_id=tenant_id, document_id=req.document_id, created_by=req.created_by)
    bg.add_task(
        _bg_build_chunks, req.job_id, req.fixed_md, req.out_json, req.source_path, req.document_id,
        req.title, req.original_filename, req.file_size, req.mime_type, req.created_by,
    )
    return {"job_id": req.job_id, "status": JobStatus.pending}


@app.post("/jobs/ingest")
def submit_ingest_job(req: IngestJobRequest, bg: BackgroundTasks):
    # chunk_block.json already exists (written by a prior build-chunks stage) -
    # peek its doc metadata so this standalone job can also be persisted.
    tenant_id = document_id = created_by = None
    try:
        bundle = json.loads(Path(req.chunk_block_json).read_text(encoding="utf-8"))
        doc = bundle.get("doc") or {}
        tenant_id = doc.get("tenant_id")
        document_id = doc.get("document_id")
        created_by = doc.get("created_by")
    except Exception:
        pass  # fall back to memory-only tracking

    _set_status(req.job_id, JobStatus.pending, stage="ingest", tenant_id=tenant_id, document_id=document_id, created_by=created_by)
    bg.add_task(_bg_ingest, req.job_id, req.chunk_block_json, tenant_id, document_id, created_by)
    return {"job_id": req.job_id, "status": JobStatus.pending}


@app.post("/jobs/pipeline")
def submit_full_pipeline(req: FullPipelineJobRequest, bg: BackgroundTasks):
    """Submit a full PDF → chunks → ingest pipeline job."""
    tenant_id = settings.db.tenant_id
    _set_status(req.job_id, JobStatus.pending, stage="pending", tenant_id=tenant_id, document_id=req.document_id, created_by=req.created_by)
    bg.add_task(
        _bg_full_pipeline, req.job_id, req.pdf_path, req.work_dir, req.document_id, req.source_path,
        req.title, req.original_filename, req.file_size, req.mime_type, req.created_by, req.access_rules,
    )
    return {"job_id": req.job_id, "status": JobStatus.pending}


@app.get("/jobs/{job_id}")
def get_job_status(job_id: str):
    if _is_uuid(job_id):
        conn = psycopg2.connect(settings.db.pg_dsn)
        try:
            with conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT status, stage, detail FROM ingest_jobs WHERE job_id = %s",
                    (job_id,),
                )
                row = cur.fetchone()
                if row:
                    return {"job_id": job_id, "status": row[0], "stage": row[1], "detail": row[2]}
        finally:
            conn.close()

    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job_id": job_id, **_jobs[job_id]}


@app.get("/healthz")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8762, reload=True)
