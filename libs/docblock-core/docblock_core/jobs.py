# core/jobs.py
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, Literal, Optional
import logging

from docblock_core.config import settings  # unified settings source
from docblock_core.sql_utils import fetch_document_id_by_docid
import psycopg2


Stage = Literal["init", "marker", "human_fix", "build_blocks", "ingest", "ingest_sum", "done", "failed"]

logger = logging.getLogger(__name__)

def _now_iso() -> str:
    # simple ISO-ish timestamp, good enough for logs/job state
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def new_document_id() -> str:
    # stable UUID per document ingest session (stored in job.meta)
    return str(uuid.uuid4())


def file_sha256(path: str, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    p = Path(path)
    with p.open("rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    h = hashlib.sha256()
    h.update(text.encode("utf-8", errors="ignore"))
    return h.hexdigest()


@dataclass
class Job:
    """
    A job represents a single document processing run.

    This is a domain object (core), not a CLI object.
    - CLI/API/Worker can all load/save job.json and run stages.
    """

    job_id: str 
    doc_id: str
    tenant_id: str
    document_id: str
    source_pdf: str
    out_dir: str

    raw_md: str
    fixed_md: str
    chunk_block_json: str
    
    content_sha256: Optional[str] = None

    stage: Stage = "init"
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    # If failed, store last error (string) + optional debug info
    error: Optional[str] = None

    # freeform metadata: pipeline versions, content hash, user tags, etc.
    meta: Dict[str, Any] = field(default_factory=dict)

    def path(self) -> Path:
        return Path(self.out_dir).resolve()

    def job_file(self) -> Path:
        return self.path() / "job.json"

    def logs_dir(self) -> Path:
        logs_dir_val = settings.logs.logs_dir if hasattr(settings.logs, "logs_dir") else self.path() / "logs"
        logs_dir = Path(str(logs_dir_val))
        logs_dir.mkdir(parents=True, exist_ok=True)
        return logs_dir

    def update_stage(self, stage: Stage) -> None:
        self.stage = stage
        self.updated_at = _now_iso()

    def mark_failed(self, err: Exception | str) -> None:
        self.stage = "failed"
        self.updated_at = _now_iso()
        self.error = str(err)

    def ensure_docblock_meta(self) -> None:
        """
        Ensure docblock-rag metadata exists in job.meta.

        Required by the new multi-tenant + version schema:
          - tenant_id: from settings.db.tenant_id
          - document_id: generated once per job and kept stable
          - content_sha256: sha256 of the source PDF bytes
        """
        doc_meta = self.meta.setdefault("docblock_rag", {})

        # tenant_id: stable per deployment / per workspace
        doc_meta.setdefault("tenant_id", getattr(settings.db, "tenant_id", "firdi"))

        # document_id: stable per job (you may reuse across re-ingests if you want)
        if not doc_meta.get("document_id"):
            doc_meta["document_id"] = new_document_id()

        # content hash: used to bump documents.active_version on change
        #if not doc_meta.get("content_sha256"):
        #    doc_meta["content_sha256"] = file_sha256(self.source_pdf)

    def save(self) -> None:
        self.updated_at = _now_iso()
        p = self.job_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def load(out_dir: str) -> "Job":
        p = Path(out_dir).resolve() / "job.json"
        if not p.exists():
            raise FileNotFoundError(f"job.json not found: {p}")
        data = json.loads(p.read_text(encoding="utf-8"))
        return Job(**data)


def init_job(out_dir: str, doc_id: str, source_pdf: str) -> Job:
    """
    Create a new job in out_dir (id defaults to folder name).
    Does not run anything yet.
    """
    out_path = Path(out_dir).resolve()
    out_path.mkdir(parents=True, exist_ok=True)

    job_id = out_path.name

    pdf_stem = Path(source_pdf).stem

    tenant_id = getattr(settings.db, "tenant_id", "firdi")
    
    # check is doc_id exist
    with psycopg2.connect(settings.db.pg_dsn) as conn:
        with conn.cursor() as cur:
            existing_document_id = fetch_document_id_by_docid(cur, tenant_id, doc_id)
    if existing_document_id:
        document_id = existing_document_id
    else:
        document_id = new_document_id()
    md_dir = out_path / doc_id
    job = Job(
        job_id=job_id,
        doc_id=doc_id,
        tenant_id=tenant_id,
        document_id=document_id,
        source_pdf=str(Path(source_pdf).resolve()),
        out_dir=str(out_path),

        # artifacts under <out_dir>/<pdf_stem>/*
        raw_md=str(md_dir / "raw.md"),
        fixed_md=str(md_dir / "fixed.md"),
        chunk_block_json=str(out_path / "chunk_block.json"),

        stage="init",
        meta={
            "docblock_rag": {
                "pipeline_version": "v0.1",
                "tenant_id": tenant_id,
                "document_id": document_id,
            }
        },
    )

    # add tenant/document/version hash meta for downstream steps
    job.ensure_docblock_meta()
    job.save()
    
    logger.info(f"Initialized job {job.job_id} for doc_id {doc_id}, document_id {document_id}, source_pdf {source_pdf}, out_dir {out_dir}")
    
    return job
