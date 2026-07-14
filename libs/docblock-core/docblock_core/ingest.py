# core/ingest.py
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras
import requests
import logging

from docblock_core.clip_embed import clip_image_embed, ClipEmbedder
from docblock_core import sql_utils
from docblock_core.config import settings
from docblock_core.llm_http import litellm_headers

def to_pg_vector_literal(vec: List[float], fmt: str = ".8f") -> str:
    return "[" + ",".join(format(float(x), fmt) for x in vec) + "]"


def litellm_embed(text: str, *, model: str, ollama_base_url: str, timeout: int) -> List[float]:
    """OpenAI-compatible embeddings endpoint: /v1/embeddings"""
    url = f"{ollama_base_url.rstrip('/')}/v1/embeddings"
    try:
        r = requests.post(
            url,
            json={"model": model, "input": settings.models.embed_doc_prefix + text},
            headers=litellm_headers(),
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        raise RuntimeError(f"Embedding request failed: {e}")
    try:
        emb = data["data"][0]["embedding"]
    except (KeyError, IndexError, TypeError):
        raise ValueError(f"Unexpected embedding response: {data}")
    if not isinstance(emb, list):
        raise ValueError(f"Unexpected embedding response: {data}")
    return emb


def ensure_document_version(
    cur,
    *,
    tenant_id: str,
    document_id: str,
    source_path: str,
    title: Optional[str],
    content_sha256: str,
    original_filename: Optional[str] = None,
    file_size: Optional[int] = None,
    mime_type: Optional[str] = None,
    external_ref: Optional[str] = None,
    created_by: Optional[str] = None,
) -> Tuple[str, int, bool]:
    """
    Upsert documents by (tenant_id, document_id) and return (document_id, active_version, content_unchanged).

    - If this is a new document_id, insert with active_version=1; content_unchanged=False
    - If already exists:
        - if content_sha256 changed -> active_version += 1; content_unchanged=False
        - else keep active_version; content_unchanged=True
      Always update paths/title/content_sha256/updated_at.

    Note:
      The caller (admin-api at upload time) is responsible for generating a fresh
      document_id for new uploads and passing the existing document_id when
      re-uploading a new version of the same logical document.
    """
    # CTE captures old version before upsert so we can detect content changes
    cur.execute(
        """
        WITH current AS (
          SELECT active_version AS old_version
          FROM documents
          WHERE tenant_id = %s AND document_id = %s
        )
        INSERT INTO documents (
          tenant_id, document_id, source_path, title,
          original_filename, file_size, mime_type, external_ref, created_by,
          status, active_version, content_sha256
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'ready', 1, %s)
        ON CONFLICT (tenant_id, document_id) DO UPDATE
        SET
          source_path = EXCLUDED.source_path,
          title = COALESCE(EXCLUDED.title, documents.title),
          original_filename = COALESCE(EXCLUDED.original_filename, documents.original_filename),
          file_size = COALESCE(EXCLUDED.file_size, documents.file_size),
          mime_type = COALESCE(EXCLUDED.mime_type, documents.mime_type),
          external_ref = COALESCE(EXCLUDED.external_ref, documents.external_ref),
          status = 'ready',
          updated_at = now(),
          active_version = CASE
            WHEN documents.content_sha256 IS DISTINCT FROM EXCLUDED.content_sha256
              THEN documents.active_version + 1
            ELSE documents.active_version
          END,
          content_sha256 = EXCLUDED.content_sha256
        RETURNING document_id, active_version, (SELECT old_version FROM current)
        """,
        (
            tenant_id, document_id,
            tenant_id, document_id, source_path, title,
            original_filename, file_size, mime_type, external_ref, created_by,
            content_sha256,
        ),
    )
    row = cur.fetchone()
    db_document_id = str(row[0])
    new_version = int(row[1])
    old_version = row[2]  # None if brand-new document
    # content_unchanged = document existed before AND version didn't increment
    content_unchanged = (old_version is not None) and (new_version == int(old_version))
    return db_document_id, new_version, content_unchanged


def ingest_to_db(
    *,
    chunk_block_json: str,
    pg_dsn: str,
    embed_model: str,
    ollama_base_url: str = "http://localhost:11434",
    vision_device: str = "cuda",
    embed_timeout: int = 120,
    skip_embedding_errors: bool = True,
    logger: Optional[logging.Logger] = None,
) -> Tuple[str, int]:
    """
    Core ingest entrypoint (API-ready).

    Expects chunk_block.json to contain (in bundle["doc"]):
      - tenant_id
      - document_id
      - content_sha256
      - source_path, title(optional)
      - original_filename, file_size, mime_type, external_ref, created_by (all optional)

    Returns (document_id, active_version).
    """
    bundle = json.loads(Path(chunk_block_json).read_text(encoding="utf-8"))
    doc = bundle["doc"]
    blocks = bundle["blocks"]

    tenant_id = doc.get("tenant_id")
    document_id = doc.get("document_id")
    content_sha256 = doc.get("content_sha256")

    if not tenant_id or not document_id or not content_sha256:
        raise ValueError("chunk_block.json missing required doc fields: tenant_id/document_id/content_sha256")

    conn = psycopg2.connect(pg_dsn)
    conn.autocommit = False
    
    # logger: only use injected logger 
    logger = logger or logging.getLogger("core.ingest")

    # allow env override to skip embedding failures
    if not skip_embedding_errors:
        env_skip = os.getenv("SKIP_EMBED_ERRORS", "").strip().lower()
        if env_skip in ("1", "true", "yes", "y"):
            skip_embedding_errors = True

    try:
        with conn, conn.cursor() as cur:
            db_document_id, version, content_unchanged = ensure_document_version(
                cur,
                tenant_id=str(tenant_id),
                document_id=str(document_id),
                source_path=str(doc["source_path"]),
                title=doc.get("title"),
                content_sha256=str(content_sha256),
                original_filename=doc.get("original_filename"),
                file_size=doc.get("file_size"),
                mime_type=doc.get("mime_type"),
                external_ref=doc.get("external_ref"),
                created_by=doc.get("created_by"),
            )

        if content_unchanged:
            logger.info(
                "[ingest] content unchanged (sha256 identical), skipping re-embedding: "
                "document_id=%s version=%s",
                db_document_id, version,
            )
            conn.close()
            return db_document_id, version

        text_rows = []
        table_rows = []
        image_rows = []

        for b in blocks:
            btype = b["block_type"]
            loc = b.get("loc") or {}
            meta = b.get("meta") or {}
            heading_path = b.get("heading_path") or []
            chunk_index = int(b["chunk_index"])

            if btype == "text":
                content = (b.get("payload") or {}).get("text") or ""
                embed_text = b.get("embed_text") or content
                try:
                    embed_text = embed_text.strip()
                    emb = (
                        litellm_embed(
                            embed_text, model=embed_model, ollama_base_url=ollama_base_url, timeout=embed_timeout
                        )
                        if embed_text.strip()
                        else None
                    )
                    emb_lit = to_pg_vector_literal(emb) if emb else None

                    text_rows.append(
                        (
                            tenant_id,
                            db_document_id,
                            version,
                            chunk_index,
                            loc.get("page_start"),
                            loc.get("page_end"),
                            loc.get("char_start"),
                            loc.get("char_end"),
                            json.dumps(heading_path, ensure_ascii=False),
                            meta.get("title"),
                            content,
                            json.dumps(meta, ensure_ascii=False),
                            embed_text,
                            emb_lit,
                        )
                    )
                except Exception as e:
                    snippet = (embed_text or "").strip().replace("\n", " ")[:160]
                    if snippet:
                        snippet = f" text_snippet='{snippet}'"
                    if skip_embedding_errors:
                        msg = f"[ingest] skip text chunk index={chunk_index} embed error: {e}{snippet}"
                        logger.warning(msg)
                        continue
                    raise ValueError(f"Error embedding text chunk index={chunk_index}: {e}{snippet}") from e

            elif btype == "table":
                embed_text = b.get("embed_text") or ""
                lexical_text = b.get("lexical_text") or embed_text
                payload = b.get("payload") or {}
                raw_md = payload.get("raw_table_md")
                raw_json = payload.get("raw_table_json")
                
                try:
                    emb = (
                        litellm_embed(
                            embed_text, model=embed_model, ollama_base_url=ollama_base_url, timeout=embed_timeout
                        )
                        if embed_text.strip()
                        else None
                    )
                    emb_lit = to_pg_vector_literal(emb) if emb else None

                    table_rows.append(
                        (
                            tenant_id,
                            db_document_id,
                            version,
                            chunk_index,
                            loc.get("page_start"),
                            loc.get("page_end"),
                            meta.get("table_key"),
                            meta.get("table_name") or None,
                            json.dumps(meta.get("table_profile"), ensure_ascii=False)
                            if meta.get("table_profile") is not None
                            else None,
                            json.dumps(meta.get("key_terms"), ensure_ascii=False) if meta.get("key_terms") is not None else None,
                            json.dumps(meta.get("fields"), ensure_ascii=False) if meta.get("fields") is not None else None,
                            json.dumps(meta.get("table_capabilities"), ensure_ascii=False)
                            if meta.get("table_capabilities") is not None
                            else None,
                            raw_md,
                            json.dumps(raw_json, ensure_ascii=False) if raw_json is not None else None,
                            embed_text,  # searchable_text
                            lexical_text,
                            json.dumps(meta, ensure_ascii=False),
                            emb_lit,
                        )
                    )
                except Exception as e:
                    snippet = (embed_text or "").strip().replace("\n", " ")[:160]
                    if snippet:
                        snippet = f" text_snippet='{snippet}'"
                    if skip_embedding_errors:
                        msg = f"[ingest] skip table chunk index={chunk_index} embed error: {e}{snippet}"
                        logger.warning(msg)
                        continue
                    raise ValueError(f"Error embedding table chunk index={chunk_index}: {e}{snippet}") from e

            elif btype == "image":
                payload = b.get("payload") or {}
                image_path = payload.get("image_path")
                if not image_path:
                    continue
                try:
                    #clip_emb = clip_image_embed(image_path, device=vision_device)
                    embedder = ClipEmbedder(device=vision_device)
                    clip_emb = embedder.embed_image(image_path)
                    clip_lit = to_pg_vector_literal(clip_emb)

                    embed_text = b.get("embed_text") or ""
                    bge_emb = (
                        litellm_embed(
                            embed_text, model=embed_model, ollama_base_url=ollama_base_url, timeout=embed_timeout
                        )
                        if embed_text.strip()
                        else None
                    )
                    bge_lit = to_pg_vector_literal(bge_emb) if bge_emb else None

                    image_rows.append(
                        (
                            tenant_id,
                            db_document_id,
                            version,
                            chunk_index,
                            loc.get("page_start"),
                            loc.get("page_end"),
                            json.dumps(heading_path, ensure_ascii=False),
                            image_path,
                            payload.get("image_alt"),
                            payload.get("image_caption"),
                            json.dumps(payload.get("image_struct"), ensure_ascii=False)
                            if payload.get("image_struct") is not None
                            else None,
                            embed_text,
                            json.dumps(meta, ensure_ascii=False),
                            clip_lit,
                            bge_lit,
                        )
                    )
                except Exception as e:
                    snippet = f" image_path='{image_path}'" if image_path else ""
                    if skip_embedding_errors:
                        msg = f"[ingest] skip image chunk index={chunk_index} embed error: {e}{snippet}"
                        logger.warning(msg)
                        continue
                    raise ValueError(f"Error embedding image chunk index={chunk_index}: {e}{snippet}") from e

        with conn, conn.cursor() as cur:
            if text_rows:
                sql_utils.insert_text_chunks(cur, text_rows)

            if table_rows:
                sql_utils.insert_table_chunks(cur, table_rows)

            if image_rows:
                sql_utils.insert_image_chunks(cur, image_rows)

        conn.commit()

        if version > 1:
            with conn.cursor() as cur:
                for table in ("text_chunks", "table_chunks", "image_chunks"):
                    cur.execute(
                        f"DELETE FROM {table} WHERE tenant_id = %s AND document_id = %s AND version < %s",  # noqa: S608
                        (str(tenant_id), db_document_id, version),
                    )
            conn.commit()
            logger.info(
                "[ingest] cleaned up old version chunks for document_id=%s versions < %s",
                db_document_id, version,
            )

        return db_document_id, version

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Backwards-compatible CLI wrapper (optional)
def ingest(bundle_path: str) -> Tuple[str, int]:
    """Ingest a chunk_block.json bundle to PostgreSQL using env vars.

    Required env:
      - PG_DSN
      - LITELLM_BASE_URL
      - EMBED_MODEL

    Optional env:
      - EMBED_TIMEOUT
      - VISION_DEVICE

    Returns (document_id, active_version).
    """
    pg_dsn = settings.db.pg_dsn
    ollama_base_url = settings.models.ollama_base_url
    embed_model = settings.models.embed_model

    embed_timeout = settings.models.embed_timeout
    vision_device = settings.models.vision_device

    return ingest_to_db(
        chunk_block_json=bundle_path,
        pg_dsn=pg_dsn,
        embed_model=embed_model,
        ollama_base_url=ollama_base_url,
        vision_device=vision_device,
        embed_timeout=embed_timeout,
    )
