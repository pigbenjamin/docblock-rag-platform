from __future__ import annotations

import re
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from docblock_core.jobs import sha256_text

# -----------------------------                                     
# Config
# -----------------------------

DEFAULT_MAX_CHARS_PER_SECTION = 9000   # 依你模型上下文調整（粗略用字元）
DEFAULT_OVERLAP_CHARS = 800

# 你要禁止的「細節洩漏」規則：數字 + 常見單位 + 表圖頁碼等
FORBIDDEN_PATTERNS = [
    r"[0-9０-９]+",  # any digit
    r"\b(°C|℃|%|mm|cm|m|kg|g|mg|V|A|W|Hz|kHz|MHz|GHz|pH)\b",
    r"\b(Table|Figure|Fig\.|Page)\b\s*\d+",
]

# -----------------------------
# Data structures
# -----------------------------

@dataclass
class MdSection:
    heading_path: str     # e.g. "## 3 Method / ### 3.1 Panel"
    level: int            # 1..6
    text: str             # section body (no other headings inside after split)


# -----------------------------
# Utilities
# -----------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def lint_semantic_summary(text: str) -> Tuple[bool, List[str]]:
    flags: List[str] = []
    for p in FORBIDDEN_PATTERNS:
        if re.search(p, text, re.IGNORECASE):
            flags.append(f"matched:{p}")
    return (len(flags) == 0), flags

def chunk_text_by_chars(text: str, max_chars: int, overlap: int) -> List[str]:
    """Simple char-based chunking with overlap (fallback when a section is too long)."""
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    out = []
    start = 0
    n = len(text)
    while start < n:
        end = min(n, start + max_chars)
        out.append(text[start:end])
        if end == n:
            break
        start = max(0, end - overlap)
    return out

# -----------------------------
# Markdown parsing (heading-based)
# -----------------------------

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)

def split_md_into_sections(md: str) -> List[MdSection]:
    """
    Split markdown by headings. Each section = heading + content until next heading of same or higher level.
    Keeps a heading_path by stacking headings.
    """
    md = md.replace("\r\n", "\n")
    matches = list(HEADING_RE.finditer(md))

    if not matches:
        # no headings: treat whole doc as a single section
        return [MdSection(heading_path="(no headings)", level=0, text=md.strip())]

    # Determine spans for each heading
    sections: List[MdSection] = []
    stack: List[Tuple[int, str]] = []  # (level, title)

    for i, m in enumerate(matches):
        level = len(m.group(1))
        title = m.group(2).strip()

        # Update stack to current heading level
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))

        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        body = md[start:end].strip()

        heading_path = " / ".join([("#" * lv) + " " + t for lv, t in stack])
        sections.append(MdSection(heading_path=heading_path, level=level, text=body))

    # Optionally drop empty bodies if you want
    return [s for s in sections if s.text]

# -----------------------------
# LLM calls (you will replace)
# -----------------------------

def call_llm(system: str, user: str) -> str:
    """
    Replace with your local LLM call (Ollama, etc.)
    Must return plain text.
    """
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip()
    gen_url = os.getenv("OLLAMA_GEN_URL", "").strip()
    model = os.getenv("LLM_MODEL", "").strip() or os.getenv("SUMMARY_MODEL", "").strip() or "qwen3:8b"
    timeout = int(os.getenv("OLLAMA_TIMEOUT", "180"))

    if not gen_url:
        gen_url = base_url.rstrip("/") + "/api/generate"

    payload: Dict[str, Any] = {
        "model": model,
        "prompt": user,
        "stream": False,
        "options": {"temperature": 0.2},
    }
    if system:
        payload["system"] = system

    try:
        r = requests.post(gen_url, json=payload, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        raise RuntimeError(f"LLM request failed: {e}")

    out = data.get("response")
    if not isinstance(out, str):
        raise ValueError(f"Unexpected LLM response: {data}")
    return out.strip()

def call_embed(text: str) -> List[float]:
    """
    Replace with your embedding endpoint.
    Return embedding vector list[float].
    """
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip()
    embed_url = os.getenv("OLLAMA_EMBED_URL", "").strip()
    model = os.getenv("EMBED_MODEL", "").strip() or os.getenv("EMBEDDING_MODEL", "").strip() or "bge-m3"
    timeout = int(os.getenv("EMBED_TIMEOUT", os.getenv("OLLAMA_TIMEOUT", "120")))

    if not embed_url:
        embed_url = base_url.rstrip("/") + "/api/embeddings"

    payload: Dict[str, Any] = {
        "model": model,
        "prompt": text,
    }

    try:
        r = requests.post(embed_url, json=payload, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        raise RuntimeError(f"Embedding request failed: {e}")

    emb = data.get("embedding")
    if not isinstance(emb, list):
        raise ValueError(f"Unexpected embedding response: {data}")
    return emb

# -----------------------------
# Prompts
# -----------------------------

SEMANTIC_MAP_SYSTEM = """You write a local, section-level semantic abstraction.
Rules:
- DO NOT include any numbers, ranges, units, model names, command lines, parameters, steps, or table contents.
- DO NOT quote from the section.
- Output 1-2 sentences in Traditional Chinese.
- Describe only what knowledge this section conveys at a high level (qualitative).
"""

SEMANTIC_MAP_USER_TEMPLATE = """Section heading:
{heading_path}

Section text:
{section_text}

Task:
Write a qualitative, human-readable semantic abstraction for this section only.
"""

RETRIEVAL_MAP_SYSTEM = """You extract retrieval-oriented labels from a section.
Rules:
- No numbers/units/specs/model names/commands/steps.
- Output MUST be valid JSON with keys: topics, question_intents, keywords.
- topics: 3-8 short noun phrases (Traditional Chinese preferred; English allowed if technical term).
- question_intents: 3-6 questions starting with 如何/何時/為何/差異/用途.
- keywords: 6-15 keywords.
"""

RETRIEVAL_MAP_USER_TEMPLATE = """Section heading:
{heading_path}

Section text:
{section_text}

Return JSON only.
"""

SEMANTIC_REDUCE_SYSTEM = """You merge multiple section-level semantic abstractions into a document-level semantic summary.
Rules:
- Output 1-2 sentences in Traditional Chinese.
- Do NOT include any numbers, units, specs, model names, commands, parameters, steps.
- Focus on: what this document is about + key qualitative properties + typical application context.
"""

SEMANTIC_REDUCE_USER_TEMPLATE = """Document title:
{title}

Section semantic notes:
{notes}

Task:
Write a concise document-level semantic summary.
"""

RETRIEVAL_REDUCE_SYSTEM = """You consolidate multiple retrieval JSON pieces into one document-level retrieval summary.
Rules:
- Output MUST be valid JSON with keys: topics, question_intents, keywords, preferred_chunk_types.
- De-duplicate and normalize wording.
- No numbers/units/specs/model names/commands/steps.
- preferred_chunk_types should be an array among: ["text","table","image"] (choose what likely applies).
"""

RETRIEVAL_REDUCE_USER_TEMPLATE = """Document title:
{title}

Retrieval pieces (JSON array):
{pieces_json}

Return JSON only.
"""

SEMANTIC_REWRITE_SYSTEM = """You rewrite a semantic summary to remove any forbidden details.
Rules:
- Remove all numbers, units, specs, model names, commands, parameters, steps.
- Keep meaning, keep 1-2 sentences Traditional Chinese.
Return only rewritten summary text.
"""

SEMANTIC_REWRITE_USER_TEMPLATE = """Original summary:
{summary}

Lint flags:
{flags}

Rewrite to comply.
"""

# -----------------------------
# Map stage
# -----------------------------

def map_section_semantic(section: MdSection, max_chars: int, overlap: int) -> List[str]:
    """
    Map a section to one or more semantic snippets.
    If the section is huge, we chunk it and map each chunk, then return snippets.
    """
    pieces = chunk_text_by_chars(section.text, max_chars=max_chars, overlap=overlap)
    out: List[str] = []
    for p in pieces:
        user = SEMANTIC_MAP_USER_TEMPLATE.format(heading_path=section.heading_path, section_text=p)
        s = call_llm(SEMANTIC_MAP_SYSTEM, user).strip()
        if s:
            out.append(s)
    return out

def map_section_retrieval(section: MdSection, max_chars: int, overlap: int) -> List[Dict[str, Any]]:
    pieces = chunk_text_by_chars(section.text, max_chars=max_chars, overlap=overlap)
    out: List[Dict[str, Any]] = []
    for p in pieces:
        user = RETRIEVAL_MAP_USER_TEMPLATE.format(heading_path=section.heading_path, section_text=p)
        raw = call_llm(RETRIEVAL_MAP_SYSTEM, user).strip()
        try:
            obj = json.loads(raw)
        except Exception as e:
            # If your model sometimes wraps text, you can add a JSON-extract fallback here.
            raise ValueError(f"Retrieval map JSON parse failed: {e}\nRaw:\n{raw}")
        out.append(obj)
    return out

# -----------------------------
# Reduce stage
# -----------------------------

def reduce_semantic(title: str, semantic_notes: List[str]) -> str:
    notes = "\n- " + "\n- ".join([n.strip() for n in semantic_notes if n.strip()])
    user = SEMANTIC_REDUCE_USER_TEMPLATE.format(title=title, notes=notes)
    return call_llm(SEMANTIC_REDUCE_SYSTEM, user).strip()

def reduce_retrieval(title: str, retrieval_pieces: List[Dict[str, Any]]) -> Dict[str, Any]:
    user = RETRIEVAL_REDUCE_USER_TEMPLATE.format(title=title, pieces_json=json.dumps(retrieval_pieces, ensure_ascii=False))
    raw = call_llm(RETRIEVAL_REDUCE_SYSTEM, user).strip()
    try:
        return json.loads(raw)
    except Exception as e:
        raise ValueError(f"Retrieval reduce JSON parse failed: {e}\nRaw:\n{raw}")

def rewrite_semantic_if_needed(summary: str, flags: List[str]) -> str:
    user = SEMANTIC_REWRITE_USER_TEMPLATE.format(summary=summary, flags=json.dumps(flags, ensure_ascii=False))
    return call_llm(SEMANTIC_REWRITE_SYSTEM, user).strip()

# -----------------------------
# Orchestration: fix.md -> document_sum payload
# -----------------------------

def build_document_sum_from_fix_md(
    fix_md_text: str,
    title: str,
    *,
    max_chars_per_section: int = DEFAULT_MAX_CHARS_PER_SECTION,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> Dict[str, Any]:
    """
    Returns:
      {
        "semantic_summary": str,
        "retrieval_summary": dict,
        "metadata": dict,
        "summary_embedding": list[float] | None,
        "retrieval_embedding": list[float] | None
        
      }
    """
    sections = split_md_into_sections(fix_md_text)

    semantic_notes: List[str] = []
    retrieval_pieces: List[Dict[str, Any]] = []

    for sec in sections:
        semantic_notes.extend(map_section_semantic(sec, max_chars=max_chars_per_section, overlap=overlap_chars))
        retrieval_pieces.extend(map_section_retrieval(sec, max_chars=max_chars_per_section, overlap=overlap_chars))

    semantic_summary = reduce_semantic(title, semantic_notes)

    ok, flags = lint_semantic_summary(semantic_summary)
    if not ok:
        semantic_summary = rewrite_semantic_if_needed(semantic_summary, flags)
        ok2, flags2 = lint_semantic_summary(semantic_summary)
        if not ok2:
            # 如果你希望「絕不入庫不合規 summary」，就 raise
            raise ValueError(f"semantic_summary still violates lint: {flags2}")

    summary_embedding = call_embed(semantic_summary)

    retrieval_summary = reduce_retrieval(title, retrieval_pieces)

    # 建議把 retrieval_summary 轉成可 embedding 的文字（topics + intents + keywords）
    topics = retrieval_summary.get("topics", [])
    intents = retrieval_summary.get("question_intents", [])
    keywords = retrieval_summary.get("keywords", [])
    embed_text = "\n".join([
        f"TITLE: {title}",
        "TOPICS: " + " / ".join(topics),
        "INTENTS: " + " / ".join(intents),
        "KEYWORDS: " + " / ".join(keywords),
    ]).strip()

    retrieval_embedding = call_embed(embed_text)

    metadata = {
        "generated_at": now_iso(),
        "title": title,
        #"fix_md_sha256": sha256_text(fix_md_text),
        "pipeline": {
            "split": "markdown_heading_then_char_chunk",
            "max_chars_per_section": max_chars_per_section,
            "overlap_chars": overlap_chars,
        },
        "lint": {
            "semantic_ok": True,
        },
        # 你可以加 model_name / prompt_version
        # "model": "...",
        # "prompt_version": "v1",
    }

    return {
        "semantic_summary": semantic_summary,
        "retrieval_summary": retrieval_summary,
        "metadata": metadata,
        "summary_embedding": summary_embedding,
        "retrieval_embedding": retrieval_embedding,
    }

# -----------------------------
# SQL upsert reference
# -----------------------------
# The SQL upsert for document_sum now lives in core/sql_utils.py
# as upsert_document_sum(...). Keep any SQL reference there.
