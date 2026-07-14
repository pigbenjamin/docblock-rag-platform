from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import psycopg2
import psycopg2.errors
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.auth import (
    get_current_user_id,
    get_current_user_id_or_admin_secret,
    node_authz,
    require_node_action,
)
from app.config import settings
from app.schemas.nodes import NodeAclEntryIn
from app.services import node_ops
from app.services.audit import audit
from docblock_core.storage import LocalFileStorage

router = APIRouter(prefix="/documents", tags=["documents"])

UPLOAD_DIR = Path("/data/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
storage = LocalFileStorage(UPLOAD_DIR)

# 目前 pipeline 只走 marker 處理 PDF；docx/xlsx/pptx 轉檔路由尚未實作（見 future-office-format-routing 備忘）
ALLOWED_UPLOAD_EXTENSIONS = {".pdf"}
ALLOWED_UPLOAD_CONTENT_TYPES = {"application/pdf"}
MAX_UPLOAD_SIZE_BYTES = 100 * 1024 * 1024  # 100MB
_READ_CHUNK_SIZE = 1024 * 1024  # 1MB


async def _read_within_limit(file: UploadFile) -> bytes:
    """Read an upload in chunks, aborting as soon as MAX_UPLOAD_SIZE_BYTES is
    exceeded instead of buffering an arbitrarily large file before checking."""
    chunks: List[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_READ_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_SIZE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"file exceeds max upload size of {MAX_UPLOAD_SIZE_BYTES // (1024 * 1024)}MB",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _db_conn():
    return psycopg2.connect(settings.db.pg_dsn)


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def _job_document_id(job_id: str) -> Optional[str]:
    if not _is_uuid(job_id):
        return None
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT document_id FROM ingest_jobs WHERE tenant_id = %s AND job_id = %s",
                (settings.db.tenant_id, job_id),
            )
            row = cur.fetchone()
    return str(row[0]) if row else None


def _cleanup_failed_node(document_id: str) -> None:
    """Remove the placeholder document node created before handing the job
    to ingest-worker, once we know that job failed (or never started) -
    otherwise a failed upload leaves an empty node stuck at 'processing'
    forever. No-ops if the node is already gone, or if ingest actually got
    far enough to write a `documents` row before failing later (that row
    represents real content and must not be silently deleted)."""
    node = node_ops.fetch_node(document_id)
    if node is None or node["node_type"] != "document" or node.get("status") != "processing":
        return
    node_ops.delete_node_subtree(node_id=document_id, storage=storage)


@router.post("/upload")
async def upload_document(
    file: UploadFile = File(...),
    document_id: Optional[str] = Form(None),
    parent_folder_id: Optional[str] = Form(None),
    title: str = Form(""),
    owner_department_id: Optional[str] = Form(None),
    acl: Optional[str] = Form(None),
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """
    Upload a PDF, save it to shared storage, then trigger the full ingest pipeline
    on ingest-worker (marker → build_chunks → ingest).

    - Omit `document_id` to create a new document: a fresh UUID is generated
      and used directly as both the document's id and its node id (D3) under
      `parent_folder_id` (required for new documents). The caller needs
      `upload` on that folder.
    - Pass an existing `document_id` to upload a new version of that
      document: unchanged content keeps the current version; changed content
      bumps it. The document stays where it is (`parent_folder_id` is
      ignored); the caller needs `update` on it.
    - `owner_department_id` (new documents only): which department's KM can
      manage the document going forward, in addition to whatever the parent
      folder already grants by inheritance. Defaults to the parent folder's
      owner.
    - `acl` (new documents only, optional): JSON array of extra ACL entries
      to grant beyond folder inheritance - same shape as `POST /v1/folders`'
      `acl` field, e.g. `[{"subject_type":"department","subject_id":"B","actions":["browse","query","read"]}]`.
    - Caller identity: `Authorization: Bearer <keycloak access token>`
      (preferred, verified via JWKS) or the legacy `X-User-Id` header while
      the frontend/tests migrate.
    """
    if document_id is not None and not _is_uuid(document_id):
        raise HTTPException(status_code=400, detail="document_id must be a UUID")

    safe_filename = Path(file.filename or "upload.pdf").name
    ext = Path(safe_filename).suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"unsupported file extension '{ext}'; only PDF is currently supported",
        )
    if file.content_type and file.content_type not in ALLOWED_UPLOAD_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"unsupported content-type '{file.content_type}'; only application/pdf is currently supported",
        )

    extra_acl: List[NodeAclEntryIn] = []
    if acl:
        try:
            extra_acl = [NodeAclEntryIn(**e) for e in json.loads(acl)]
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"invalid acl JSON: {e}")
        node_ops.validate_acl_entries(extra_acl)

    if document_id is None:
        if not parent_folder_id or not _is_uuid(parent_folder_id):
            raise HTTPException(status_code=400, detail="parent_folder_id is required for new documents")

        parent = node_ops.fetch_node(parent_folder_id)
        if parent is None:
            raise HTTPException(status_code=404, detail=f"parent folder '{parent_folder_id}' not found")
        if parent["node_type"] != "folder":
            raise HTTPException(status_code=400, detail="parent_folder_id must be a folder")

        require_node_action(user_id, parent_folder_id, "upload")

        resolved_document_id = str(uuid.uuid4())
        resolved_owner = (owner_department_id or parent["owner_department_id"]).strip()
        node_name = title.strip() or safe_filename
        path_cache = f"{parent['path_cache'] or ''}/{node_name}"

        try:
            with _db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO nodes
                          (id, tenant_id, parent_id, node_type, name, owner_department_id,
                           inherit_acl, path_cache, created_by, updated_by)
                        VALUES (%s, %s, %s, 'document', %s, %s, true, %s, %s, %s)
                        """,
                        (
                            resolved_document_id, settings.db.tenant_id, parent_folder_id,
                            node_name, resolved_owner, path_cache, user_id, user_id,
                        ),
                    )
                    node_ops.insert_acl_entries(cur, resolved_document_id, extra_acl, user_id)
                conn.commit()
        except psycopg2.errors.UniqueViolation:
            raise HTTPException(
                status_code=409,
                detail=f"a node named '{node_name}' already exists in this folder",
            )
    else:
        resolved_document_id = document_id
        require_node_action(user_id, resolved_document_id, "update")

    job_id = str(uuid.uuid4())
    try:
        content = await _read_within_limit(file)
        pdf_path = storage.save_temp(job_id, safe_filename, content)

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
        }

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{settings.ingest_worker.url}/jobs/pipeline",
                json=payload,
            )
            resp.raise_for_status()
    except Exception:
        if document_id is None:
            _cleanup_failed_node(resolved_document_id)
        raise

    audit(
        "document.upload", actor_id=user_id, resource_type="document", resource_id=resolved_document_id,
        after={"job_id": job_id, "filename": safe_filename, "new_document": document_id is None},
    )

    return {
        "job_id": job_id,
        "document_id": resolved_document_id,
        "filename": safe_filename,
        "status": "submitted",
        "ingest_worker_response": resp.json(),
    }


@router.get("/job/{job_id}")
async def get_job_status(
    job_id: str,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """Poll ingest-worker for pipeline job status. Requires `browse` on the
    job's document once its node exists (created at upload time, before the
    job is even submitted - see upload_document)."""
    document_id = _job_document_id(job_id)
    if document_id:
        require_node_action(user_id, document_id, "browse")

    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get(f"{settings.ingest_worker.url}/jobs/{job_id}")
        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail="Job not found")
        resp.raise_for_status()
    result = resp.json()

    if document_id and result.get("status") == "failed":
        _cleanup_failed_node(document_id)

    return result


@router.get("/")
def list_documents(
    limit: int = 50,
    offset: int = 0,
    user_id: str = Depends(get_current_user_id),
) -> List[Dict[str, Any]]:
    """List documents the caller may `browse` (D1), flat and tenant-wide.
    For a folder-scoped view use `GET /v1/nodes?parent_id=`.

    Permission filtering happens after pagination, so a page can come back
    with fewer than `limit` items even when more exist beyond it - the same
    limitation the design doc defers to the node_effective_permissions cache
    (second-stage work); acceptable for the current data volume.
    """
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
    items = [dict(zip(cols, row)) for row in rows]
    if not items:
        return items

    allowed = set(node_authz().filter_allowed(
        user_id=user_id, action="browse",
        node_ids=[str(item["document_id"]) for item in items],
    ))
    return [item for item in items if str(item["document_id"]) in allowed]


@router.get("/{document_id}")
def get_document(
    document_id: str,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """Get metadata for a single document by document_id. Requires `browse`."""
    if not _is_uuid(document_id):
        raise HTTPException(status_code=400, detail="document_id must be a UUID")
    require_node_action(user_id, document_id, "browse")

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


@router.get("/{document_id}/content")
def download_document(
    document_id: str,
    user_id: str = Depends(get_current_user_id),
) -> FileResponse:
    """Download/preview the active version's original file. Requires `read`,
    independent of `browse`/`query` (§10.4: seeing a name in a listing or
    letting RAG use a document's content doesn't imply the original file may
    be downloaded)."""
    if not _is_uuid(document_id):
        raise HTTPException(status_code=400, detail="document_id must be a UUID")
    require_node_action(user_id, document_id, "read")

    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source_path, original_filename, mime_type, status
                FROM documents
                WHERE tenant_id = %s AND document_id = %s
                """,
                (settings.db.tenant_id, document_id),
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"document '{document_id}' not found")

    source_path, original_filename, mime_type, status = row
    if status != "ready":
        raise HTTPException(status_code=409, detail=f"document is not ready for download (status={status})")

    path = Path(source_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="stored file is missing")

    audit("document.download", actor_id=user_id, resource_type="document", resource_id=document_id)
    return FileResponse(
        path=str(path),
        media_type=mime_type or "application/octet-stream",
        filename=original_filename or path.name,
    )


@router.delete("/{document_id}")
def delete_document(
    document_id: str,
    user_id: Optional[str] = Depends(get_current_user_id_or_admin_secret),
) -> Dict[str, Any]:
    """
    Hard delete (D6): the document node, its chunks and its ACL entries all
    disappear (FK cascade). No trash can.

    Requires `delete` on the document's node (or the legacy `X-Acl-Secret`
    admin bypass, which skips per-user checks entirely). Only accepts an id
    that is actually a document - a folder id (including a department root)
    is rejected rather than silently deleting that whole subtree; use
    `DELETE /v1/nodes/{node_id}` for folders.
    """
    if not _is_uuid(document_id):
        raise HTTPException(status_code=400, detail="document_id must be a UUID")

    node = node_ops.fetch_node(document_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"Document '{document_id}' not found")
    if node["node_type"] != "document":
        raise HTTPException(
            status_code=400,
            detail=f"'{document_id}' is a folder, not a document; use DELETE /v1/nodes/{{node_id}} instead",
        )

    if user_id is not None:
        require_node_action(user_id, document_id, "delete")

    _doc_ids, node_count = node_ops.delete_node_subtree(node_id=document_id, storage=storage)
    if node_count == 0:
        raise HTTPException(status_code=404, detail=f"Document '{document_id}' not found")

    audit("document.delete", actor_id=user_id, resource_type="document", resource_id=document_id)
    return {"ok": True, "document_id": document_id}
