from __future__ import annotations

import asyncio
import traceback
from pathlib import Path
from typing import Any, Dict, Optional
from enum import Enum

import uvicorn
from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel

from worker.tasks.build_chunks import run_build_chunks
from worker.tasks.ingest_chunks import run_ingest_chunks
from worker.tasks.marker_to_md import run_marker_to_md

app = FastAPI(title="Docblock Ingest Worker")

# In-memory job status store (replace with Redis for production HA)
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


def _set_status(job_id: str, status: JobStatus, detail: str = ""):
    _jobs[job_id] = {"status": status, "detail": detail}


async def _run_in_thread(fn, *args):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fn, *args)


async def _bg_marker(job_id: str, pdf_path: str, output_dir: str, document_id: str = ""):
    _set_status(job_id, JobStatus.running)
    try:
        resolved_document_id = document_id or Path(pdf_path).stem
        await _run_in_thread(run_marker_to_md, job_id, resolved_document_id, pdf_path, output_dir)
        _set_status(job_id, JobStatus.done)
    except Exception as e:
        _set_status(job_id, JobStatus.failed, traceback.format_exc())


async def _bg_build_chunks(
    job_id: str, fixed_md: str, out_json: str, source_path, document_id,
    title=None, original_filename=None, file_size=None, mime_type=None,
):
    from docblock_core.config import settings

    _set_status(job_id, JobStatus.running)
    try:
        tenant_id = settings.db.tenant_id
        await _run_in_thread(
            run_build_chunks, fixed_md, out_json, source_path, tenant_id, document_id,
            title, original_filename, file_size, mime_type,
        )
        _set_status(job_id, JobStatus.done)
    except Exception as e:
        _set_status(job_id, JobStatus.failed, traceback.format_exc())


async def _bg_ingest(job_id: str, chunk_block_json: str):
    _set_status(job_id, JobStatus.running)
    try:
        await _run_in_thread(run_ingest_chunks, chunk_block_json)
        _set_status(job_id, JobStatus.done)
    except Exception as e:
        _set_status(job_id, JobStatus.failed, traceback.format_exc())


async def _bg_full_pipeline(
    job_id: str, pdf_path: str, work_dir: str, document_id: str, source_path,
    title=None, original_filename=None, file_size=None, mime_type=None,
):
    from pathlib import Path
    from docblock_core.config import settings

    _set_status(job_id, JobStatus.running, "stage: marker")
    try:
        md_path = await _run_in_thread(run_marker_to_md, job_id, document_id, pdf_path, work_dir)

        pdf_stem = Path(pdf_path).stem
        out_json = str(Path(work_dir) / f"{pdf_stem}.chunk_block.json")

        tenant_id = settings.db.tenant_id

        _set_status(job_id, JobStatus.running, "stage: build_chunks")
        await _run_in_thread(
            run_build_chunks, md_path, out_json, source_path, tenant_id, document_id,
            title, original_filename, file_size, mime_type,
        )

        _set_status(job_id, JobStatus.running, "stage: ingest")
        await _run_in_thread(run_ingest_chunks, out_json)

        _set_status(job_id, JobStatus.done, "all stages complete")
    except Exception:
        _set_status(job_id, JobStatus.failed, traceback.format_exc())


@app.post("/jobs/marker")
def submit_marker_job(req: MarkerJobRequest, bg: BackgroundTasks):
    _set_status(req.job_id, JobStatus.pending)
    bg.add_task(_bg_marker, req.job_id, req.pdf_path, req.output_dir, req.document_id or "")
    return {"job_id": req.job_id, "status": JobStatus.pending}


@app.post("/jobs/build-chunks")
def submit_build_chunks_job(req: BuildChunksJobRequest, bg: BackgroundTasks):
    _set_status(req.job_id, JobStatus.pending)
    bg.add_task(
        _bg_build_chunks, req.job_id, req.fixed_md, req.out_json, req.source_path, req.document_id,
        req.title, req.original_filename, req.file_size, req.mime_type,
    )
    return {"job_id": req.job_id, "status": JobStatus.pending}


@app.post("/jobs/ingest")
def submit_ingest_job(req: IngestJobRequest, bg: BackgroundTasks):
    _set_status(req.job_id, JobStatus.pending)
    bg.add_task(_bg_ingest, req.job_id, req.chunk_block_json)
    return {"job_id": req.job_id, "status": JobStatus.pending}


@app.post("/jobs/pipeline")
def submit_full_pipeline(req: FullPipelineJobRequest, bg: BackgroundTasks):
    """Submit a full PDF → chunks → ingest pipeline job."""
    _set_status(req.job_id, JobStatus.pending)
    bg.add_task(
        _bg_full_pipeline, req.job_id, req.pdf_path, req.work_dir, req.document_id, req.source_path,
        req.title, req.original_filename, req.file_size, req.mime_type,
    )
    return {"job_id": req.job_id, "status": JobStatus.pending}


@app.get("/jobs/{job_id}")
def get_job_status(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job_id": job_id, **_jobs[job_id]}


@app.get("/healthz")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8762, reload=True)
