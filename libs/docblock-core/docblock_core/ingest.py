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
from docblock_core.gen_sum import build_document_sum_from_fix_md
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


def litellm_generate(
    prompt: str,
    *,
    model: str,
    ollama_gen_url: str,
    timeout: int,
    system: Optional[str] = None,
) -> str:
    """OpenAI-compatible text generation endpoint: /v1/chat/completions"""
    base = (ollama_gen_url or "").strip()
    if not base:
        raise ValueError("ollama_gen_url is required for generation")
    url = f"{base.rstrip('/')}/v1/chat/completions"

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "temperature": 0.2,
    }

    r = requests.post(url, json=payload, headers=litellm_headers(), timeout=timeout)
    r.raise_for_status()
    data = r.json()
    out = (data.get("choices") or [{}])[0].get("message", {}).get("content")
    if not isinstance(out, str):
        raise ValueError(f"Unexpected generate response: {data}")
    return out.strip()


def ensure_document_version(
    cur,
    *,
    tenant_id: str,
    document_id: str,
    source_path: str,
    md_path: str,
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
          tenant_id, document_id, source_path, md_path, title,
          original_filename, file_size, mime_type, external_ref, created_by,
          status, active_version, content_sha256
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'ready', 1, %s)
        ON CONFLICT (tenant_id, document_id) DO UPDATE
        SET
          source_path = EXCLUDED.source_path,
          md_path = EXCLUDED.md_path,
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
            tenant_id, document_id, source_path, md_path, title,
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


def _build_heuristic_summary(blocks: List[Dict[str, Any]], max_chars: int = 1800) -> str:
    """
    Simple deterministic summary to bootstrap `summary_chunks`.

    Later you can replace this with an LLM-generated summary step, but this already enables:
      - summary-level RAG search
      - ACL mode: summary vs detail
    """
    parts: List[str] = []
    for b in blocks:
        btype = b.get("block_type")
        if btype == "text":
            txt = ((b.get("payload") or {}).get("text") or "").strip()
            if txt:
                parts.append(txt)
        elif btype == "table":
            # use embed_text / lexical_text as a proxy
            et = (b.get("embed_text") or "").strip()
            if et:
                parts.append(et)
        elif btype == "image":
            et = (b.get("embed_text") or "").strip()
            if et:
                parts.append(et)

        if sum(len(p) for p in parts) >= max_chars * 2:
            break

    blob = "\n\n".join(parts).strip()
    if not blob:
        return ""
    if len(blob) <= max_chars:
        return blob
    return blob[: max_chars - 1] + "…"


def _build_summary_source(blocks: List[Dict[str, Any]], max_chars: int = 12000) -> str:
    """Build a compact-but-information-dense source text for summarization."""
    parts: List[str] = []
    for b in blocks:
        btype = b.get("block_type")
        if btype == "text":
            txt = ((b.get("payload") or {}).get("text") or "").strip()
            if txt:
                parts.append(txt)
        elif btype == "table":
            et = (b.get("embed_text") or "").strip()
            if et:
                parts.append(et)
        elif btype == "image":
            et = (b.get("embed_text") or "").strip()
            if et:
                parts.append(et)
        if sum(len(p) for p in parts) >= max_chars:
            break
    return "\n\n".join(parts).strip()[:max_chars]


def _llm_summary_prompt(source_text: str, *, language: str = "zh-TW") -> Tuple[str, str]:
    """Return (system, user_prompt)."""
    # Keep prompt short and retrieval-friendly.
    system = (
        "你是一位文件摘要助手。只能根據使用者提供的內容撰寫摘要，"
        "不得捏造、臆測或加入外部知識。"
    )
    user = (
        "請根據下方【內容】產生可用於 RAG 檢索的摘要（繁體中文）。\n"
        "要求：\n"
        "- 以條列方式輸出 6–12 點，每點 1 句話。\n"
        "- 同時補上 10–25 個關鍵字（以逗號分隔）。\n"
        "- 不要出現『我認為』『可能』『推測』等字眼；不確定就略過。\n"
        "\n"
        "【內容】\n"
        f"{source_text}\n"
    )
    return system, user


def _load_markdown_for_summary(md_path: str, *, max_chars: int = 20000) -> str:
    """Load original markdown *before chunking* as summarization source.

    - Reads the full markdown file from md_path.
    - If too long, keeps head+tail to better represent the whole document.
    """
    p = (md_path or "").strip()
    if not p:
        return ""
    try:
        fp = Path(p)
        if not fp.exists():
            return ""
        text = fp.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:
        return ""

    if not text:
        return ""
    if len(text) <= max_chars:
        return text

    half = max_chars // 2
    return text[:half] + "\n...\n" + text[-half:]


def old_llm_summary_prompt_from_markdown(md_text: str, *, language: str = "zh-TW") -> Tuple[str, str]:
    """Return (system, user_prompt) to summarize a full document (markdown source)."""
    system = (
        "你是一位文件摘要助手。只能根據使用者提供的原文撰寫摘要，"
        "不得捏造、臆測或加入外部知識。"
    )
    if language.lower().startswith("zh"):
        user = (
            "請根據下方【原文】產生『整份文件』的摘要（繁體中文）。\n"
            "要求：\n"
            "- 先給 1 句總結（<= 40 字）。\n"
            "- 再以條列方式輸出 6–10 點重點（每點 1 句話）。\n"
            "- 最後附上 10–25 個關鍵字（以逗號分隔）。\n"
            "- 不要只摘要開頭，請涵蓋全文主題；若原文被截斷也請以可見內容為準。\n"
            "- 不確定就略過，不要猜。\n"
            "\n"
            "【原文】\n"
            f"{md_text}\n"
        )
    else:
        user = (
            "Summarize the WHOLE document based on the following ORIGINAL text.\n"
            "Requirements:\n"
            "- 1-sentence overall summary.\n"
            "- 6-10 bullet points (one sentence each).\n"
            "- 10-25 keywords (comma-separated).\n"
            "- Do not hallucinate; omit uncertain details.\n\n"
            "ORIGINAL:\n"
            f"{md_text}\n"
        )
    return system, user


def _llm_summary_prompt_from_markdown(md_text: str, *, language: str = "zh-TW") -> Tuple[str, str]:
    system = (
        "你是一位文件資訊描述助手。"
        "你的任務是產生『文件資訊描述摘要』，"
        "僅描述這份文件提供哪些類型的資訊。"
        "不得加入摘要以外的任何說明文字。"
        "不得使用前言、結語、標題、解釋、評論或提示語。"
        "只能根據提供的原文內容，不得臆測或補充外部知識。"
    )

    user = (
        "請根據下方【原文】，輸出「文件資訊描述摘要」（繁體中文）。\n"
        "\n"
        "嚴格輸出規則（非常重要）：\n"
        "- ❌ 不要有任何摘要以外的說明文字\n"
        "- ❌ 不要出現例如「以下是摘要」、「本文件說明」、「總結如下」等語句\n"
        "- ❌ 不要加入前言、結語、標題或解釋\n"
        "- ✅ 輸出內容只能包含下列三個區塊，且必須依序出現\n"
        "\n"
        "輸出格式（只能照此格式）：\n"
        "1. 整體描述：2–3 句，高層次描述文件提供的資訊範圍\n"
        "2. 資訊類型條列：5–8 點，每點 1 句，描述文件可用來回答的問題類型\n"
        "3. 關鍵字：8–15 個，以逗號分隔（偏主題，不是細節）\n"
        "\n"
        "【原文】\n"
        f"{md_text}\n"
    )

    return system, user

# Generate summary from longer markdown (pre-chunk)
def gen_sum():
    pass

def generate_llm_summary_from_markdown(
    md_path: str,
    *,
    model: str,
    ollama_gen_url: str,
    timeout: int,
    max_md_chars: int = 20000,
    language: str = "zh-TW",
) -> Tuple[str, Dict[str, Any]]:
    """Generate a 'true' summary from original markdown (pre-chunk)."""
    md_text = _load_markdown_for_summary(md_path, max_chars=max_md_chars)
    if not md_text.strip():
        return "", {"generated_by": "llm_markdown_v0", "empty_source": True, "md_path": md_path}

    system, prompt = _llm_summary_prompt_from_markdown(md_text, language=language)
    out = litellm_generate(prompt, model=model, ollama_gen_url=ollama_gen_url, timeout=timeout, system=system)
    out = (out or "").strip()
    return out, {
        "generated_by": "llm_markdown_v0",
        "summary_model": model,
        "md_path": md_path,
        "max_md_chars": max_md_chars,
    }


def generate_llm_summary(
    blocks: List[Dict[str, Any]],
    *,
    model: str,
    ollama_gen_url: str,
    timeout: int,
    max_source_chars: int = 12000,
) -> Tuple[str, Dict[str, Any]]:
    """Generate an LLM summary; return (summary_text, metadata)."""
    source_text = _build_summary_source(blocks, max_chars=max_source_chars)
    if not source_text.strip():
        return "", {"generated_by": "llm_v1", "empty_source": True}

    system, prompt = _llm_summary_prompt(source_text)
    out = litellm_generate(prompt, model=model, ollama_gen_url=ollama_gen_url, timeout=timeout, system=system)
    out = (out or "").strip()
    return out, {"generated_by": "llm_v1", "summary_model": model, "max_source_chars": max_source_chars}

def write_access(document_id, tenant_id, access_map, pg_dsn: str) -> None:
    """
    Write user access levels to document_acl table.
    """
    conn = psycopg2.connect(pg_dsn)
    conn.autocommit = False

    def _parse_principal(key):
        # Accept (ptype, pid) tuples or strings like "type:id" or plain user id
        if isinstance(key, (list, tuple)) and len(key) == 2: 
            return str(key[0]), str(key[1])
        if isinstance(key, str):
            s = key
            for sep in (":", "/", "|", ",", "-", "."):  # separators
                if sep in s:
                    a, b = s.split(sep, 1)
                    return a.strip(), b.strip()
            return "user", s
        raise ValueError("Unsupported principal key: %r" % (key,))

    try:
        with conn, conn.cursor() as cur:
            # We'll normalize effect names to lower-case strings as provided.
            for principal_key, effect in access_map.items():
                ptype, pid = _parse_principal(principal_key)
                eff = (effect or "").strip()
                if not eff:
                    # skip empty effects
                    continue

                # Remove any existing ACL rows for this (tenant, document, principal)
                cur.execute(
                    """
                    DELETE FROM document_acl
                    WHERE tenant_id = %s AND document_id = %s
                      AND principal_type = %s AND principal_id = %s
                    """,
                    (tenant_id, document_id, ptype, pid),
                )

                # Insert the new effect row
                cur.execute(
                    """
                    INSERT INTO document_acl (tenant_id, document_id, principal_type, principal_id, effect)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (tenant_id, document_id, ptype, pid, eff),
                )
    finally:
        conn.close()


def ingest_to_db(
    *,
    chunk_block_json: str,
    pg_dsn: str,
    embed_model: str,
    ollama_base_url: str = "http://localhost:11434",
    summary_model: Optional[str] = settings.models.summary_model,
    ollama_gen_url: Optional[str] = None,
    summary_timeout: int = 180,
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
      - source_path, md_path, title(optional)
      - original_filename, file_size, mime_type, external_ref, created_by (all optional)

    Returns (document_id, active_version).
    """
    bundle = json.loads(Path(chunk_block_json).read_text(encoding="utf-8"))
    doc = bundle["doc"]
    blocks = bundle["blocks"]

    md_path_str = str(doc.get("md_path") or "")

    tenant_id = doc.get("tenant_id")
    document_id = doc.get("document_id")
    content_sha256 = doc.get("content_sha256")

    if not tenant_id or not document_id or not content_sha256:
        raise ValueError("chunk_block.json missing required doc fields: tenant_id/document_id/content_sha256")

    eff_summary_model = (summary_model or "").strip()
    eff_ollama_gen_url = (ollama_gen_url or "").strip()

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
                md_path=str(doc.get("md_path") or ""),
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

            ## 1 summary per document
            #summary_meta: Dict[str, Any]
            #summary_text = ""
#
            #if eff_summary_model and eff_ollama_gen_url:
            #    try:
            #        # IMPORTANT: summary is generated from original markdown BEFORE chunking
            #        summary_text, summary_meta = generate_llm_summary_from_markdown(
            #            md_path_str,
            #            model=str(eff_summary_model),
            #            ollama_gen_url=str(eff_ollama_gen_url),
            #            timeout=int(summary_timeout),
            #        )
            #    except Exception as e:
            #        # fall back to heuristic summary (never fail ingest because of summary)
            #        summary_text = _build_heuristic_summary(blocks)
            #        summary_meta = {
            #            "generated_by": "heuristic_v0",
            #            "fallback_from": "llm_markdown_v0",
            #            "error": str(e),
            #        }
            #else:
            #    summary_text = _build_heuristic_summary(blocks)
            #    summary_meta = {
            #        "generated_by": "heuristic_v0",
            #        "note": "SUMMARY_MODEL / OLLAMA_GEN_URL not set; using heuristic",
            #    }
#
            #if summary_text.strip():
            #    summary_emb = ollama_embed(
            #        summary_text, model=embed_model, ollama_base_url=ollama_base_url, #timeout=embed_timeout
            #    )
            #    summary_lit = to_pg_vector_literal(summary_emb)
#
            #    sql_utils.insert_summary_chunk(
            #        cur,
            #        tenant_id,
            #        db_document_id,
            #        version,
            #        summary_text,
            #        json.dumps(summary_meta, ensure_ascii=False),
            #        summary_lit,
            #    )

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


def ingest_sum(
    *,
    fixed_md_path: str,
    pg_dsn: str,
    tenant_id: str,
    document_id: str,
    content_sha256: str,
    title: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate and upsert document_sum for a fixed markdown file.

    Returns the generated payload from build_document_sum_from_fix_md.
    """
    
    fix_md_text = Path(fixed_md_path).read_text(encoding="utf-8", errors="ignore")
    payload = build_document_sum_from_fix_md(
        fix_md_text,
        title=title or "(untitled)",
    )
    
    # ensure content_sha256 is present and consistent
    payload.setdefault("metadata", {})
    payload["metadata"].setdefault("content_sha256", content_sha256)

    summary_embedding = payload.get("summary_embedding")
    summary_embedding_lit = (
        to_pg_vector_literal(summary_embedding)
        if isinstance(summary_embedding, list)
        else None
    )

    retrieval_embedding = payload.get("retrieval_embedding")
    retrieval_embedding_lit = (
        to_pg_vector_literal(retrieval_embedding)
        if isinstance(retrieval_embedding, list)
        else None
    )

    params = {
        "tenant_id": tenant_id,
        "document_id": document_id,
        "semantic_summary": payload.get("semantic_summary", ""),
        "retrieval_summary": json.dumps(payload.get("retrieval_summary", {}), ensure_ascii=False),
        "metadata": json.dumps(payload.get("metadata", {}), ensure_ascii=False),
        "summary_embedding": summary_embedding_lit,
        "retrieval_embedding": retrieval_embedding_lit,
    }

    with psycopg2.connect(pg_dsn) as conn:
        with conn.cursor() as cur:
            sql_utils.upsert_document_sum(
                cur,
                tenant_id=tenant_id,
                document_id=document_id,
                semantic_summary=params["semantic_summary"],
                retrieval_summary_json=params["retrieval_summary"],
                metadata_json=params["metadata"],
                summary_embedding=params["summary_embedding"],
                retrieval_embedding=params["retrieval_embedding"],
            )

    return payload


# Backwards-compatible CLI wrapper (optional)
def ingest(bundle_path: str) -> Tuple[str, int]:
    """Ingest a chunk_block.json bundle to PostgreSQL using env vars.

    Required env:
      - PG_DSN
      - LITELLM_BASE_URL
      - EMBED_MODEL

    Optional env:
      - SUMMARY_MODEL          (for true summary generation)
      - SUMMARY_TIMEOUT
      - EMBED_TIMEOUT
      - VISION_DEVICE

    Returns (document_id, active_version).
    """
    pg_dsn = settings.db.pg_dsn
    ollama_base_url = settings.models.ollama_base_url
    embed_model = settings.models.embed_model

    summary_model = settings.models.summary_model
    ollama_gen_url = settings.models.ollama_gen_url
    summary_timeout = settings.models.summary_timeout
    
    embed_timeout = settings.models.embed_timeout
    vision_device = settings.models.vision_device

    return ingest_to_db(
        chunk_block_json=bundle_path,
        pg_dsn=pg_dsn,
        embed_model=embed_model,
        ollama_base_url=ollama_base_url,
        summary_model=summary_model,
        ollama_gen_url=ollama_gen_url,
        summary_timeout=summary_timeout,
        vision_device=vision_device,
        embed_timeout=embed_timeout,
    )
