# core/chunk_builder.py
from __future__ import annotations

import os
import re
import json
import hashlib
import textwrap
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path

from docblock_core.logging_utils import get_file_logger
from docblock_core.md_semantic_chunk_plus import md_semantic_chunker
from docblock_core.jobs import sha256_text


# -----------------------------
# Helpers
# -----------------------------
def sha1(s: str) -> str:
    import hashlib
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def ensure_parent(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def write_json(path: str, obj: Any) -> None:
    ensure_parent(path)
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

def load_md_lines(md_path: str) -> List[str]:
    return Path(md_path).read_text(encoding="utf-8").splitlines()


# -----------------------------
# image extraction from md
# -----------------------------
def extract_heading_path(lines: List[str], line_idx: int) -> List[str]:
    """
    超輕量 heading 路徑：往上掃 # / ## / ###...
    """
    path: List[str] = []
    current_levels: Dict[int, str] = {}
    for i in range(0, line_idx + 1):
        m = re.match(r"^(#{1,6})\s+(.*)\s*$", lines[i])
        if not m:
            continue
        lvl = len(m.group(1))
        title = m.group(2).strip()
        current_levels[lvl] = title
        # 清掉更深層
        for k in list(current_levels.keys()):
            if k > lvl:
                del current_levels[k]
    for lvl in sorted(current_levels.keys()):
        path.append(current_levels[lvl])
    return path


def extract_images_from_md(md_path: str) -> List[Dict[str, Any]]:
    """
    Parse markdown and extract image blocks: ![alt](src "title")
    Return a list of dicts: {src, alt, title, line_index, heading_path, surrounding_text}
    """
    text = read_text(md_path)
    lines = text.splitlines()

    img_pat = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<src>[^)\s]+)(?:\s+\"(?P<title>[^\"]*)\")?\)")

    # Build a simple heading stack for each line
    heading_path: List[str] = []
    line_heading_path: List[List[str]] = []

    for ln in lines:
        m = re.match(r"^(#{1,6})\s+(.*)$", ln.strip())
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            heading_path = heading_path[: level - 1] + [title]
        line_heading_path.append(list(heading_path))

    images: List[Dict[str, Any]] = []
    for i, ln in enumerate(lines):
        m = img_pat.search(ln)
        if not m:
            continue

        alt = m.group("alt") or ""
        src = m.group("src") or ""
        title = m.group("title") or ""

        # Surrounding context (a few lines)
        start = max(0, i - 2)
        end = min(len(lines), i + 3)
        surrounding = "\n".join(lines[start:end]).strip()

        images.append(
            {
                "src": src,
                "alt": alt,
                "title": title,
                "line_index": i,
                "heading_path": line_heading_path[i],
                "surrounding_text": surrounding,
            }
        )

    return images


def resolve_image_path(md_path: str, src: str) -> Optional[str]:
    """
    Resolve image src relative to md file directory if not absolute.
    """
    if not src:
        return None
    p = Path(src)
    if p.is_absolute():
        return str(p) if p.exists() else None
    base = Path(md_path).resolve().parent
    cand = (base / src).resolve()
    return str(cand) if cand.exists() else None


# -----------------------------
# table embed/lexical builders
# -----------------------------
def build_table_embed_text_from_chunk(c: Dict[str, Any]) -> str:
    """
    Use your existing rule: table embed_text is the raw md table text
    plus some header hints if present in meta.
    """
    meta = c.get("meta", {}) or {}
    raw = (c.get("text") or "").strip()

    # Optional: include inferred title, profile, capabilities etc.
    parts = []
    if meta.get("table_title"):
        parts.append(f"Table title: {meta.get('table_title')}")
    if meta.get("table_profile"):
        parts.append(f"Profile: {json.dumps(meta.get('table_profile'), ensure_ascii=False)}")
    parts.append(raw)
    return "\n".join([p for p in parts if p]).strip()


def build_table_lexical_text_from_chunk(c: Dict[str, Any]) -> str:
    """
    A lexical field for tsvector/trgm. Keep it broad and include key terms/fields.
    """
    meta = c.get("meta", {}) or {}
    raw = (c.get("text") or "").strip()

    parts = []
    if meta.get("table_title"):
        parts.append(str(meta.get("table_title")))
    if meta.get("key_terms"):
        parts.append(json.dumps(meta.get("key_terms"), ensure_ascii=False))
    if meta.get("fields"):
        parts.append(json.dumps(meta.get("fields"), ensure_ascii=False))
    parts.append(raw)
    return "\n".join([p for p in parts if p]).strip()


# -----------------------------
# Image understanding (optional)
# -----------------------------
def blip_caption(image_path: str, device: str = "cuda") -> str:
    """
    需要：
      pip install transformers pillow torch
    """
    from PIL import Image
    from transformers import BlipProcessor, BlipForConditionalGeneration

    model_name = os.getenv("BLIP_MODEL", "Salesforce/blip-image-captioning-base")
    processor = BlipProcessor.from_pretrained(model_name)
    model = BlipForConditionalGeneration.from_pretrained(model_name)
    model.to(device)

    img = Image.open(image_path).convert("RGB")
    inputs = processor(img, return_tensors="pt").to(device)
    out = model.generate(**inputs, max_new_tokens=60)
    caption = processor.decode(out[0], skip_special_tokens=True).strip()
    return caption


def ollama_struct_image(
    caption: str,
    surrounding_text: str,
    model: str,
    url: str,
    timeout: int = 180,
) -> Dict[str, Any]:
    import requests

    prompt = f"""
你是一個技術文件的圖像理解助手。
請根據「圖像caption」與「圖像周邊文字」推斷該圖像的資訊，並輸出 JSON（只輸出 JSON）。

請輸出欄位：
- figure_type: one of ["photo","diagram","flowchart","architecture","chart","table_screenshot","unknown"]
- entities: 圖中重要元件/物件/概念（list[str]）
- relations: 若是架構/流程，列出關係（list[{{"from":...,"to":...,"type":...}}]）
- summary: 1~3 句中文描述
- keywords: 適合檢索的關鍵詞（list[str]）

圖像caption:
{caption}

圖像周邊文字:
{surrounding_text}
""".strip()

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.0},
    }
    r = requests.post(url, json=payload, timeout=timeout)
    r.raise_for_status()
    raw = r.json().get("response", "").strip()

    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass

    return {
        "figure_type": "unknown",
        "entities": [],
        "relations": [],
        "summary": caption,
        "keywords": [],
    }


def build_image_embed_text(alt: str, caption: str, struct: Dict[str, Any], surrounding_text: str) -> str:
    parts = []
    if alt:
        parts.append(f"alt: {alt}")
    if caption:
        parts.append(f"caption: {caption}")
    if struct:
        parts.append("struct_summary: " + (struct.get("summary") or ""))
        kw = struct.get("keywords") or []
        if kw:
            parts.append("keywords: " + ", ".join([str(x) for x in kw]))
        ents = struct.get("entities") or []
        if ents:
            parts.append("entities: " + ", ".join([str(x) for x in ents]))
    if surrounding_text:
        parts.append("surrounding:\n" + surrounding_text)
    return "\n".join(parts).strip()


# =========================================================
# ✅ Unified public API: build_blocks()
# =========================================================
def build_blocks(
    *,
    fixed_md: str,
    out_json: str,
    doc_id: str,
    source_path: str,
    tenant_id: str | None = None,
    document_id: str | None = None,
    #content_sha256: str | None = None,
    seg_model: str = "qwen3.5-9b",
    ollama_gen_url: str = "http://localhost:4000",
    infer_table_capabilities: bool = True,
    summarize_tables: bool = False,
    capabilities_model: Optional[str] = None,
    log_path: Optional[str] = None,
) -> str:
    """
    Build chunk_block.json from fixed_md.

    Output schema (v2.0):
      {
        "version": "2.0",
        "doc": {
          "tenant_id": "...",
          "document_id": "...",
          "content_sha256": "...",
          "doc_id": "...",
          "source_path": "...",
          "md_path": "...",
          "title": null
        },
        "blocks": [...]
      }
    """
    md_path = str(Path(fixed_md).resolve())
    ensure_parent(out_json)

    # Calculate content_sha256 for doc metadata
    content_sha256 = sha256_text(read_text(md_path))
    
    logger = get_file_logger("core.chunk_builder", log_path) if log_path else None
    if logger:
        logger.info("build_blocks md=%s out_json=%s document_id=%s", md_path, out_json, document_id)

    # 1) semantic chunks (text + table) from md
    # md_semantic_chunker requires an output path; write chunks next to the final out_json
    chunks_out = str(Path(out_json).with_suffix(".chunks.json"))
    chunks = md_semantic_chunker(
        md_path=md_path,
        output_path=chunks_out,
        seg_model=seg_model,
        ollama_gen_url=ollama_gen_url,
        doc_id=doc_id or Path(md_path).name,
        source_path=source_path or md_path,
        summarize_tables=bool(summarize_tables),
        summary_model=capabilities_model or seg_model,
        infer_table_capabilities=bool(infer_table_capabilities),
        capabilities_model=capabilities_model,
    )

    # 2) image blocks (from md)
    images = extract_images_from_md(md_path)

    # 3) unify to chunk_block bundle
    bundle: Dict[str, Any] = {
        "version": "2.0",
        "doc": {
            "doc_id": doc_id,
            "tenant_id": tenant_id,
            "document_id": document_id,
            "content_sha256": content_sha256,
            "source_path": source_path or md_path,
            "md_path": md_path,
            "title": None,
        },
        "blocks": [],
    }

    # text/table → blocks
    for idx, c in enumerate(chunks):
        meta = c.get("meta", {}) or {}
        is_table = bool(meta.get("is_table"))
        block_type = "table" if is_table else "text"

        if block_type == "table":
            embed_text = build_table_embed_text_from_chunk(c)
            lexical_text = build_table_lexical_text_from_chunk(c)
            payload = {
                "raw_table_md": c.get("text") or "",
                "raw_table_json": meta.get("raw_table_json") or None,
                "text": "",
            }
        else:
            embed_text = c.get("text") or ""
            lexical_text = None
            payload = {"text": c.get("text") or ""}

        block_id = sha1(f"{bundle['doc']['doc_id']}|{block_type}|{idx}|{c.get('start')}|{c.get('end')}")

        bundle["blocks"].append(
            {
                "block_id": block_id,
                "block_type": block_type,
                "chunk_index": idx,
                "loc": {
                    "page_start": meta.get("page_start"),
                    "page_end": meta.get("page_end"),
                    "char_start": c.get("start"),
                    "char_end": c.get("end"),
                    "line_start": None,
                    "line_end": None,
                },
                "heading_path": meta.get("heading_path") or [],
                "embed_text": embed_text,
                "lexical_text": lexical_text,
                "payload": payload,
                "meta": meta,
            }
        )

    # image → blocks（chunk_index 接在 text/table 後面）
    base = len(bundle["blocks"])
    for i, im in enumerate(images):
        img_abs = resolve_image_path(md_path, im["src"])
        if not img_abs:
            continue

        caption = im.get("title") or ""
        struct = None  # your pipeline may fill this later

        embed_text = textwrap.dedent(
            f"""
            alt: {im.get('alt','')}
            caption: {caption}
            surrounding:
            {im.get('surrounding_text','')}
            """
        ).strip()

        chunk_index = base + i
        block_id = sha1(f"{bundle['doc']['doc_id']}|image|{chunk_index}|{img_abs}")

        bundle["blocks"].append(
            {
                "block_id": block_id,
                "block_type": "image",
                "chunk_index": chunk_index,
                "loc": {
                    "page_start": None,
                    "page_end": None,
                    "char_start": None,
                    "char_end": None,
                    "line_start": im.get("line_index"),
                    "line_end": im.get("line_index"),
                },
                "heading_path": im.get("heading_path") or [],
                "embed_text": embed_text,
                "lexical_text": None,
                "payload": {
                    "image_path": img_abs,
                    "image_alt": im.get("alt", ""),
                    "image_caption": caption,
                    "image_struct": struct,
                },
                "meta": {
                    "is_image": True,
                },
            }
        )

    write_json(out_json, bundle)
    if logger:
        logger.info("done build_blocks blocks=%d out=%s", len(bundle["blocks"]), out_json)
    return out_json


# Backward-compatible alias (optional)
def build_chunk_blocks(
    fixed_md: str,
    out_json: str,
    doc_id: str,
    source_path: str,
    seg_model: str = "qwen3.5-9b",
    ollama_gen_url: str = "http://localhost:4000",
    infer_table_capabilities: bool = True,
    capabilities_model: Optional[str] = None,
    summarize_tables: bool = False,
) -> str:
    # legacy signature adapter
    return build_blocks(
        fixed_md=fixed_md,
        out_json=out_json,
        doc_id=doc_id,
        source_path=source_path,
        seg_model=seg_model,
        ollama_gen_url=ollama_gen_url,
        infer_table_capabilities=infer_table_capabilities,
        capabilities_model=capabilities_model,
        summarize_tables=summarize_tables,
        log_path=None,
    )
