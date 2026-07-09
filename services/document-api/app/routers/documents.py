from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import psycopg2
from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.config import settings

router = APIRouter(prefix="/documents", tags=["documents"])

UPLOAD_DIR = Path("/data/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _db_conn():
    return psycopg2.connect(settings.db.pg_dsn)


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


@router.post("/upload")
async def upload_document(
    file: UploadFile = File(...),
    document_id: Optional[str] = Form(None),
    title: str = Form(""),
) -> Dict[str, Any]:
    """
    Upload a PDF, save it to shared storage, then trigger the full ingest pipeline
    on ingest-worker (marker → build_chunks → ingest).

    - Omit `document_id` to create a new document (a fresh UUID is generated).
    - Pass an existing `document_id` to upload a new version of that document:
      unchanged content keeps the current version; changed content bumps it.
    """
    if document_id is not None and not _is_uuid(document_id):
        raise HTTPException(status_code=400, detail="document_id must be a UUID")

    resolved_document_id = document_id or str(uuid.uuid4())

    job_id = str(uuid.uuid4())
    dest_dir = UPLOAD_DIR / job_id
    dest_dir.mkdir(parents=True)

    safe_filename = Path(file.filename or "upload.pdf").name
    pdf_path = dest_dir / safe_filename

    content = await file.read()
    pdf_path.write_bytes(content)

    work_dir = str(dest_dir)
    payload = {
        "job_id": job_id,
        "pdf_path": str(pdf_path),
        "work_dir": work_dir,
        "document_id": resolved_document_id,
        "source_path": str(pdf_path),
        "title": title or None,
        "original_filename": safe_filename,
        "file_size": len(content),
        "mime_type": file.content_type,
    }

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{settings.ingest_worker.url}/jobs/pipeline",
            json=payload,
        )
        resp.raise_for_status()

    return {
        "job_id": job_id,
        "document_id": resolved_document_id,
        "filename": safe_filename,
        "status": "submitted",
        "ingest_worker_response": resp.json(),
    }


@router.get("/job/{job_id}")
async def get_job_status(job_id: str) -> Dict[str, Any]:
    """Poll ingest-worker for pipeline job status."""
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get(f"{settings.ingest_worker.url}/jobs/{job_id}")
        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail="Job not found")
        resp.raise_for_status()
    return resp.json()


@router.get("/")
def list_documents(limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """List all documents for the configured tenant."""
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT document_id, title, source_path, original_filename,
                       file_size, status, active_version, created_at, updated_at
                FROM documents
                WHERE tenant_id = %s
                ORDER BY updated_at DESC
                LIMIT %s OFFSET %s
                """,
                (settings.db.tenant_id, limit, offset),
            )
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in rows]


@router.get("/{document_id}")
def get_document(document_id: str) -> Dict[str, Any]:
    """Get metadata for a single document by document_id."""
    if not _is_uuid(document_id):
        raise HTTPException(status_code=400, detail="document_id must be a UUID")

    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT document_id, title, source_path, original_filename,
                       file_size, status, active_version,
                       content_sha256, created_at, updated_at
                FROM documents
                WHERE tenant_id = %s AND document_id = %s
                LIMIT 1
                """,
                (settings.db.tenant_id, document_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"Document '{document_id}' not found")
            cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


@router.delete("/{document_id}")
def delete_document(document_id: str) -> Dict[str, Any]:
    """
    Delete a document and all its chunks/ACL from PostgreSQL.
    Cascades via FK constraints.
    """
    if not _is_uuid(document_id):
        raise HTTPException(status_code=400, detail="document_id must be a UUID")

    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM documents WHERE tenant_id = %s AND document_id = %s RETURNING document_id",
                (settings.db.tenant_id, document_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"Document '{document_id}' not found")
        conn.commit()

    return {"ok": True, "document_id": str(row[0])}
