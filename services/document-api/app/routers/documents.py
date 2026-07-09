from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import psycopg2
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.auth import (
    get_current_user_id,
    get_current_user_id_or_admin_secret,
    require_department_km,
    require_document_km,
)
from app.config import settings
from docblock_core.storage import LocalFileStorage

router = APIRouter(prefix="/documents", tags=["documents"])

UPLOAD_DIR = Path("/data/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
storage = LocalFileStorage(UPLOAD_DIR)


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
    departments: List[str] = Form(...),
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """
    Upload a PDF, save it to shared storage, then trigger the full ingest pipeline
    on ingest-worker (marker → build_chunks → ingest).

    - Omit `document_id` to create a new document (a fresh UUID is generated).
    - Pass an existing `document_id` to upload a new version of that document:
      unchanged content keeps the current version; changed content bumps it.
    - `departments`: at least one department this document belongs to. Each
      listed department, plus the uploader, is automatically granted `detail`
      (management) access; no other access_rules can be set at upload time -
      use the ACL endpoints afterwards for anything more specific. The caller
      must hold the KM role in at least one listed department.
    - Caller identity: `Authorization: Bearer <keycloak access token>`
      (preferred, verified via JWKS) or the legacy `X-User-Id` header while
      the frontend/tests migrate.
    """
    if document_id is not None and not _is_uuid(document_id):
        raise HTTPException(status_code=400, detail="document_id must be a UUID")

    departments = [d.strip() for d in departments if d.strip()]
    if not departments:
        raise HTTPException(status_code=400, detail="at least one department is required")

    require_department_km(user_id, departments, mode="any")

    resolved_document_id = document_id or str(uuid.uuid4())

    job_id = str(uuid.uuid4())
    safe_filename = Path(file.filename or "upload.pdf").name

    content = await file.read()
    pdf_path = storage.save_temp(job_id, safe_filename, content)

    access_rules: Dict[str, str] = {f"department:{dept}": "detail" for dept in departments}
    access_rules[f"user:{user_id}"] = "detail"

    payload = {
        "job_id": job_id,
        "pdf_path": str(pdf_path),
        "work_dir": str(pdf_path.parent),
        "document_id": resolved_document_id,
        "source_path": str(pdf_path),
        "title": title or None,
        "original_filename": safe_filename,
        "file_size": len(content),
        "mime_type": file.content_type,
        "created_by": user_id,
        "access_rules": access_rules,
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
        "departments": departments,
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
def delete_document(
    document_id: str,
    user_id: Optional[str] = Depends(get_current_user_id_or_admin_secret),
) -> Dict[str, Any]:
    """
    Delete a document and all its chunks/ACL from PostgreSQL.
    Cascades via FK constraints.

    Requires KM role in one of the document's owning departments (or the
    legacy `X-Acl-Secret` admin bypass).
    """
    if not _is_uuid(document_id):
        raise HTTPException(status_code=400, detail="document_id must be a UUID")

    if user_id is not None:
        require_document_km(user_id, document_id)

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
