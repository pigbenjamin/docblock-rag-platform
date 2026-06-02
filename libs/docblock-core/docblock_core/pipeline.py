# core/pipeline.py
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Literal, Optional

from docblock_core.jobs import Job, file_sha256
from docblock_core.logging_utils import get_file_logger, get_module_logger, setup_root_logger

from docblock_core.marker_runner import run_marker
from docblock_core.chunk_builder import build_blocks
from docblock_core.ingest import ingest_to_db, ingest_sum

from docblock_core.config import settings  # unified settings source


StageToRun = Literal["marker", "build_blocks", "ingest", "ingest_sum"]


def _require_file(path: str, what: str) -> None:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"{what} not found: {p}")


def _record_run_config(job: Job) -> None:
    """Record effective runtime configuration into job.meta."""
    job.meta.setdefault("run_config", {})

    job.meta["run_config"] = {
        "pipeline_version": getattr(settings, "pipeline_version", "unknown"),
        "schema_version": getattr(settings, "schema_version", "unknown"),

        "models": {
            "seg_model": settings.models.seg_model,
            "embed_model": settings.models.embed_model,
            "ollama_base_url": settings.models.ollama_base_url,
            "ollama_gen_url": settings.models.ollama_gen_url,
            "vision_device": settings.models.vision_device,
        },

        "chunking": {
            "infer_table_capabilities": settings.chunking.infer_table_capabilities,
            "summarize_tables": settings.chunking.summarize_tables,
            "capabilities_model": settings.chunking.capabilities_model,
        },

        "tools": {
            "marker_cmd": settings.tools.marker_cmd,
            "marker_timeout": settings.tools.marker_timeout,
        },

        "db": {
            "pg_dsn_set": bool(settings.db.pg_dsn),
            "tenant_id": getattr(settings.db, "tenant_id", "demo"),
        },
    }

    job.save()


def _inject_doc_meta_into_chunk_json(job: Job) -> None:
    """
    Make chunk_block.json self-contained for ingest:

    - doc.tenant_id
    - doc.document_id
    - doc.content_sha256
    - doc.doc_id / source_path / md_path / title(optional)
    """
    job.ensure_docblock_meta()
    doc_meta = job.meta.get("docblock_rag", {}) or {}

    # record fix.md hash if available
    #if job.fixed_md and Path(job.fixed_md).exists():
    #    doc_meta["fix_md_sha256"] = file_sha256(job.fixed_md)
    #    job.meta["docblock_rag"] = doc_meta

    p = Path(job.chunk_block_json)
    bundle = json.loads(p.read_text(encoding="utf-8"))

    doc = bundle.get("doc") or {}
    doc["tenant_id"] = doc_meta.get("tenant_id")
    doc["document_id"] = doc_meta.get("document_id")

    #doc.setdefault("doc_id", job.doc_id)
    doc.setdefault("source_path", job.source_pdf)
    # Treat the human-fixed markdown as the effective md_path
    doc.setdefault("md_path", job.fixed_md)
    if "title" not in doc:
        doc["title"] = job["document_id"]

    bundle["doc"] = doc
    p.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    
    # write content_sha256 into job.meta
    content_sha256 = bundle.get("doc", {}).get("content_sha256")
    if content_sha256:
        doc_meta["content_sha256"] = content_sha256
        job.meta["docblock_rag"] = doc_meta
        job.save()


def _run_marker(job: Job, logs_dir: Path) -> Job:
    job.update_stage("marker")
    job.save()
    marker_log = settings.logs.marker_log if hasattr(settings.logs, "marker_log") else "marker.log"
    log_path = str(logs_dir / marker_log)
    #logger = get_file_logger("core.marker", log_path)
    logger = get_module_logger("core.marker", logs_dir, marker_log)
    logger.info(
        "[marker:start] job_id=%s doc_id=%s pdf=%s out_dir=%s",
        job.job_id, job.doc_id, job.source_pdf, job.out_dir
    )
    _t0 = time.perf_counter()

    run_marker(
        job_id=job.job_id,
        doc_id=job.doc_id,
        pdf_path=job.source_pdf,
        out_dir=job.out_dir,
        marker_cmd=settings.tools.marker_cmd,
        timeout=settings.tools.marker_timeout,
        logger=logger,
    )
    
    logger.info(
        "[marker:end] job_id=%s doc_id=%s pdf=%s out_dir=%s elapsed=%.2fs",
        job.job_id, job.doc_id, job.source_pdf, job.out_dir, time.perf_counter() - _t0
    )

    job.update_stage("human_fix")
    job.save()
    return job


def _run_build_blocks(job: Job, logs_dir: Path, handled_md: Optional[str] = None) -> Job:
    _require_file(job.fixed_md, "fixed.md")

    job.update_stage("build_blocks")
    job.save()

    build_blocks_log = settings.logs.build_blocks_log if hasattr(settings.logs, "build_blocks_log") else "build_blocks.log"
    log_path = str(logs_dir / build_blocks_log)
    #logger = get_file_logger("core.build_blocks", log_path)
    logger = get_module_logger("core.build_blocks", logs_dir, build_blocks_log)
    
    # handled_md for following modified md input
    # if handled_md, let job.fixed_md=handled_md
    if handled_md:
        job.fixed_md = handled_md
            
    logger.info(
        "[build_blocks:start] job_id=%s doc_id=%s fixed_md=%s out_json=%s",
        job.job_id, job.doc_id, job.fixed_md, job.chunk_block_json
    )
    _t0 = time.perf_counter()

    build_blocks(
        fixed_md=job.fixed_md,
        out_json=job.chunk_block_json,
        doc_id=job.doc_id,
        source_path=job.source_pdf,
        tenant_id=job.tenant_id,
        document_id=job.document_id,        
        seg_model=settings.models.seg_model,
        ollama_gen_url=settings.models.ollama_gen_url,
        infer_table_capabilities=settings.chunking.infer_table_capabilities,
        summarize_tables=settings.chunking.summarize_tables,
        capabilities_model=settings.chunking.capabilities_model,
        log_path=log_path,
    )

    # Ensure chunk_block.json has tenant/document/hash information for ingest
    _inject_doc_meta_into_chunk_json(job)
    
    logger.info(
        "[build_blocks:end] job_id=%s doc_id=%s fixed_md=%s out_json=%s elapsed=%.2fs",
        job.job_id, job.doc_id, job.fixed_md, job.chunk_block_json, time.perf_counter() - _t0
    )

    job.update_stage("ingest")
    job.save()
    return job


def _run_ingest(job: Job, logs_dir: Path, chunk_block_json: Optional[str] = None) -> Job:
    if chunk_block_json:
        job.chunk_block_json = chunk_block_json
    _require_file(job.chunk_block_json, "chunk_block.json")

    job.update_stage("ingest")
    job.save()

    ingest_log = settings.logs.ingest_log if hasattr(settings.logs, "ingest_log") else "ingest.log"
    log_path = str(logs_dir / ingest_log)
    #logger = get_file_logger("core.ingest", log_path)
    logger = get_module_logger("core.ingest", logs_dir, ingest_log)
    logger.info(
        "[ingest:start] job_id=%s doc_id=%s chunk_json=%s",
        job.job_id, job.doc_id, job.chunk_block_json[:100]
    )
    _t0 = time.perf_counter()

    ingest_to_db(
        chunk_block_json=job.chunk_block_json,
        pg_dsn=settings.db.pg_dsn,
        embed_model=settings.models.embed_model,
        ollama_base_url=settings.models.ollama_base_url,
        ollama_gen_url=settings.models.ollama_gen_url,
        summary_model=settings.chunking.capabilities_model,
        vision_device=settings.models.vision_device,
        logger=logger,
    )

    logger.info(
        "[ingest:end] job_id=%s doc_id=%s chunk_json=%s elapsed=%.2fs",
        job.job_id, job.doc_id, job.chunk_block_json[:100], time.perf_counter() - _t0
    )
    
    job.update_stage("done")
    job.save()
    return job


def _run_ingest_sum(job: Job, logs_dir: Path, handled_md: Optional[str] = None) -> Job:
    if handled_md:
        job.fixed_md = handled_md
    _require_file(job.fixed_md, "fixed.md")

    job.update_stage("ingest_sum")
    job.save()

    ingest_sum_log = settings.logs.ingest_sum_log if hasattr(settings.logs, "ingest_sum_log") else "ingest_sum.log"
    log_path = str(logs_dir / ingest_sum_log)
    #logger = get_file_logger("core.gen_sum", log_path)
    logger = get_module_logger("core.gen_sum", logs_dir, ingest_sum_log)
    logger.info(
        "[ingest_sum:start] job_id=%s doc_id=%s fixed_md=%s",
        job.job_id, job.doc_id, job.fixed_md
    )
    _t0 = time.perf_counter()

    job.ensure_docblock_meta()
    doc_meta = job.meta.get("docblock_rag", {}) or {}

    tenant_id = doc_meta.get("tenant_id")
    document_id = doc_meta.get("document_id")
    content_sha256 = doc_meta.get("content_sha256")
    if not tenant_id or not document_id:
        logger.error(
            "[ingest_sum] job_id=%s doc_id=%s Missing tenant_id or document_id in job meta. doc_meta=%s",
            job.job_id, job.doc_id, doc_meta
        )
        raise ValueError("missing tenant_id/document_id in job meta for ingest_sum")

    ingest_sum(
        fixed_md_path=job.fixed_md,
        pg_dsn=settings.db.pg_dsn,
        tenant_id=tenant_id,
        document_id=document_id,
        title=job.document_id,
        content_sha256=content_sha256,
    )
    
    logger.info(
        "[ingest_sum:end] job_id=%s doc_id=%s fixed_md=%s elapsed=%.2fs",
        job.job_id, job.doc_id, job.fixed_md, time.perf_counter() - _t0
    )

    job.save()
    return job


def run_stage(job: Job, stage: StageToRun) -> Job:
    """
    Run exactly one pipeline stage.

    Stages:
      marker       PDF -> raw.md (marker output directory)
      build_blocks fixed.md -> chunk_block.json
      ingest       chunk_block.json -> PostgreSQL
    """
    logs_dir = job.logs_dir()

    try:
        _record_run_config(job)

        if stage == "marker":
            return _run_marker(job, logs_dir)

        if stage == "build_blocks":
            return _run_build_blocks(job, logs_dir)

        if stage == "ingest":
            return _run_ingest(job, logs_dir)

        if stage == "ingest_sum":
            return _run_ingest_sum(job, logs_dir)

        raise ValueError(f"Unknown stage: {stage}")

    except Exception as e:
        pipeline_error_log = settings.logs.pipeline_error_log if hasattr(settings.logs, "pipeline_error_log") else "pipeline_error.log"
        fail_log = str(logs_dir / pipeline_error_log)
        #logger = get_file_logger("core.pipeline", fail_log)
        logger = get_module_logger("core.pipeline", logs_dir, pipeline_error_log)
        logger.exception(
            "pipeline failed stage=%s job_id=%s doc_id=%s",
            stage, job.job_id, job.doc_id
        )

        job.mark_failed(e)
        job.save()
        raise
