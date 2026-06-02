#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
md_semantic_chunk_plus.py (enhanced)
- Table-aware semantic chunking for Marker-produced Markdown using a local Ollama LLM.
- Each table becomes an isolated chunk (placeholder-driven).
- Adds metadata: doc_id, source_path, is_table, table_name/table_index, columns, key_terms, model_hint.
- NEW:
  - Always build deterministic (local) table_profile + improved key_terms for huge tables (no info loss).
  - Optional LLM-based table_capabilities + fields (navigation/usage hints, NOT summarization).
  - Flags:
      --summarize-tables (kept) : classic short summary (not recommended for huge regulatory tables)
      --infer-table-capabilities : LLM generates fields/capabilities based on profile + samples
      --capabilities-model : model for capabilities inference (default: --summary-model or --model)
"""

import os, re, json, argparse, hashlib
from typing import List, Dict, Tuple, Optional
import requests


# ============================================================
# Ollama helpers
# ============================================================

def ollama_generate(model: str, prompt: str,
                    url="http://localhost:11434/api/generate",
                    temperature=0.0, timeout=600) -> str:
    r = requests.post(url, json={
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature},
    }, timeout=timeout)
    r.raise_for_status()
    return r.json().get("response", "")

def safe_json_load(s: str):
    """
    Parse JSON strictly; if model wraps extra text, try to extract the first {...} block.
    """
    try:
        return json.loads(s)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", s)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    raise ValueError("Failed to parse JSON from model output.")


# ============================================================
# Table stitching (existing logic)
# ============================================================

def _split_table_rows(table_md: str):
    lines = [ln for ln in table_md.splitlines() if ln.strip()]
    # 第一列當表頭
    if lines and "|" in lines[0]:
        hdr = [c.strip() for c in lines[0].strip("|").split("|") if c.strip()]
    else:
        hdr = []
    return hdr, lines

def _header_similarity(h1, h2):
    if not h1 or not h2:
        return 0.0
    s1, s2 = set(h1), set(h2)
    inter = len(s1 & s2)
    union = len(s1 | s2)
    return inter / union if union else 0.0

def stitch_adjacent_tables(out_lines, tables, tables_meta,
                           max_gap_lines=6, min_header_sim=0.7, small_tail=6):
    i = 0
    while i < len(out_lines) - 1:
        cur = out_lines[i].strip()

        if cur.startswith("[[TABLE_"):
            # 向前找下一個 TABLE_xxx 與兩者之間的行距
            j = i + 1
            gap_lines = []
            next_tbl_idx = -1
            while j < len(out_lines):
                tok = out_lines[j].strip()
                if tok.startswith("[[TABLE_"):
                    next_tbl_idx = j
                    break
                gap_lines.append(out_lines[j])
                if len(gap_lines) > max_gap_lines:
                    break
                j += 1

            if next_tbl_idx != -1:
                cur_tbl = cur
                nxt_tbl = out_lines[next_tbl_idx].strip()

                # 比表頭
                h1, rows1 = _split_table_rows(tables[cur_tbl])
                h2, rows2 = _split_table_rows(tables[nxt_tbl])
                sim = _header_similarity(h1, h2)

                # 後表是否為「小尾巴」
                tail_rows = max(0, len(rows2) - 1)  # 扣掉表頭
                tailish = tail_rows <= small_tail

                # 允許夾雜的說明/表名行（不含 '|'）
                only_captions = all(("|" not in ln) for ln in gap_lines)

                if only_captions and (sim >= min_header_sim or tailish):
                    # 去掉第二表的表頭（若與第一表相似）
                    start_k = 1 if sim >= 0.5 and rows2 else 0
                    merged = "\n".join([*rows1, *rows2[start_k:]])
                    tables[cur_tbl] = merged

                    # 合併 meta（保留前表 index，表名以較長者為準）
                    m1 = tables_meta.get(cur_tbl, {})
                    m2 = tables_meta.get(nxt_tbl, {})
                    name = max((m1.get("name") or ""), (m2.get("name") or ""), key=len)
                    if name:
                        m1["name"] = name
                    tables_meta[cur_tbl] = m1

                    # 刪除 next 表：從 out_lines 拿掉 placeholder 與中間 captions 行
                    del out_lines[i+1: next_tbl_idx+1]
                    # 同時移除表 map 與 meta
                    tables.pop(nxt_tbl, None)
                    tables_meta.pop(nxt_tbl, None)
                    # 不遞增 i，繼續嘗試與下一張表合併（連續跨多頁）
                    continue
        i += 1


# ============================================================
# Table row/cell helpers (existing + reused)
# ============================================================

def _split_row_cells(row: str) -> List[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row.strip("|")
    return [c.strip() for c in row.split("|")]

def _is_numeric_like(s: str) -> bool:
    s = re.sub(r"<br\s*/?>", " ", s)
    s = s.strip()
    if not s:
        return False
    return bool(re.search(r"\d", s))

def _infer_numeric_columns(rows: List[str]) -> List[bool]:
    """
    From whole table rows (including header rows[0]), infer numeric-like columns.
    Rule: if >=60% of body cells in a column contain digits => numeric column.
    """
    if len(rows) <= 1:
        return []
    data_rows = rows[1:]
    cells_rows = [_split_row_cells(r) for r in data_rows]
    if not cells_rows:
        return []
    n_cols = max(len(c) for c in cells_rows)
    numeric_cols = [False] * n_cols
    for j in range(n_cols):
        col_vals = [cells[j] for cells in cells_rows if j < len(cells)]
        if not col_vals:
            continue
        numeric_count = sum(1 for v in col_vals if _is_numeric_like(v))
        if numeric_count / len(col_vals) >= 0.6:
            numeric_cols[j] = True
    return numeric_cols

def _remove_empty_table_rows(rows: List[str]) -> List[str]:
    cleaned = []
    for r in rows:
        if not r.strip():
            continue
        stripped = re.sub(r"[|\s]+", "", r)
        if not stripped:
            continue
        cleaned.append(r)
    return cleaned


# ============================================================
# NEW: deterministic table_profile + key_terms (Route 1)
# ============================================================

_UNIT_PAT = re.compile(
    r"(?i)\b("
    r"mg/kg|g/kg|ug/kg|μg/kg|ppm|ppb|mg/L|g/L|ug/L|μg/L|%"
    r")\b"
)

def _normalize_token(s: str) -> str:
    s = s.strip()
    s = re.sub(r"<br\s*/?>", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s

def _dedup_keep_order(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for x in items:
        if x not in seen:
            out.append(x)
            seen.add(x)
    return out

def build_table_profile(table_md: str,
                        max_samples_per_col: int = 30,
                        max_total_samples: int = 120) -> Dict[str, object]:
    """
    Build a deterministic, non-lossy "table_profile" for huge tables:
    - n_rows, n_cols
    - numeric_cols
    - value_samples: per column distinct samples (bounded)
    - units_candidates: from cells via regex
    - notes_candidates: common marks like '*', '註', '不適用', etc.
    """
    hdr, rows = _split_table_rows(table_md)
    rows = _remove_empty_table_rows(rows)

    # Determine columns (prefer parsed header cells; fallback by max columns in body)
    header_row = rows[0] if rows else ""
    header_cells = _split_row_cells(header_row) if header_row else hdr
    header_cells = [c for c in (header_cells or []) if c.strip()]

    # If header_cells is empty, infer n_cols from body
    body_cells = [_split_row_cells(r) for r in rows[1:]] if len(rows) > 1 else []
    n_cols = len(header_cells) if header_cells else (max((len(c) for c in body_cells), default=0))
    if n_cols <= 0:
        n_cols = 0

    # Numeric columns
    numeric_cols = _infer_numeric_columns(rows) if rows else []
    if numeric_cols and n_cols and len(numeric_cols) < n_cols:
        numeric_cols = numeric_cols + [False] * (n_cols - len(numeric_cols))
    if n_cols and not numeric_cols:
        numeric_cols = [False] * n_cols

    # Collect value samples
    value_samples: Dict[str, List[str]] = {}
    units_candidates: List[str] = []
    notes_candidates: List[str] = []

    # Column names for sampling key: use header_cells or fallback names col_1..n
    col_names = header_cells if header_cells else [f"col_{i+1}" for i in range(n_cols)]

    # Scan body rows; sample distinct values per column
    total_samples = 0
    for r in rows[1:]:
        cells = _split_row_cells(r)
        # normalize length
        if len(cells) < n_cols:
            cells = cells + [""] * (n_cols - len(cells))
        for j in range(n_cols):
            if total_samples >= max_total_samples:
                break
            v = _normalize_token(cells[j])
            if not v:
                continue

            # units
            for um in _UNIT_PAT.findall(v):
                units_candidates.append(um)

            # notes / marks
            if "*" in v:
                notes_candidates.append("*")
            if "註" in v or "备注" in v or "備註" in v:
                notes_candidates.append("註記")
            if "不適用" in v or "不适用" in v:
                notes_candidates.append("不適用")
            if "不得檢出" in v or "不得检出" in v:
                notes_candidates.append("不得檢出")

            cname = col_names[j]
            value_samples.setdefault(cname, [])
            if len(value_samples[cname]) < max_samples_per_col:
                # For numeric columns, keep fewer textual samples (still keep representative strings)
                # But do not drop entirely; keep up to max_samples_per_col anyway.
                if v not in value_samples[cname]:
                    value_samples[cname].append(v)
                    total_samples += 1

        if total_samples >= max_total_samples:
            break

    units_candidates = _dedup_keep_order([u.lower() for u in units_candidates])[:10]
    notes_candidates = _dedup_keep_order(notes_candidates)[:10]

    profile = {
        "n_rows": max(0, len(rows) - 1) if rows else 0,  # body rows count
        "n_cols": n_cols,
        "numeric_cols": numeric_cols,
        "value_samples": value_samples,
        "units_candidates": units_candidates,
        "notes_candidates": notes_candidates,
    }
    return profile

def build_key_terms(columns: List[str],
                    table_profile: Dict[str, object],
                    per_col_take: int = 8,
                    max_terms: int = 80) -> List[str]:
    """
    Build robust key_terms for retrieval:
    - include columns
    - include samples from non-numeric columns (entities/categories) preferentially
    - include unit/notes candidates
    """
    terms: List[str] = []
    cols = [c.strip() for c in (columns or []) if c.strip()]
    terms.extend(cols)

    value_samples = (table_profile or {}).get("value_samples") or {}
    numeric_cols = (table_profile or {}).get("numeric_cols") or []

    # Map column -> index if possible
    col_to_idx = {c: i for i, c in enumerate(cols)}
    # Prefer non-numeric columns: take samples
    for c in cols:
        samples = value_samples.get(c) or []
        idx = col_to_idx.get(c, None)
        is_num = False
        if idx is not None and idx < len(numeric_cols):
            is_num = bool(numeric_cols[idx])

        # For numeric columns, sample fewer (still might contain starred limits like "0.01*")
        take_n = max(2, per_col_take // 2) if is_num else per_col_take
        for s in samples[:take_n]:
            # avoid extremely long cells
            if len(s) > 80:
                continue
            terms.append(s)

    # Units / notes
    for u in (table_profile or {}).get("units_candidates") or []:
        terms.append(u)
    for n in (table_profile or {}).get("notes_candidates") or []:
        terms.append(n)

    # Cleanup + dedup
    terms = [_normalize_token(t) for t in terms]
    terms = [t for t in terms if t and len(t) <= 120]
    terms = _dedup_keep_order(terms)
    return terms[:max_terms]


# ============================================================
# NEW: LLM "capabilities" + "fields" (Route 2) - no summarization
# ============================================================

_CAP_CACHE: Dict[str, Dict[str, object]] = {}

def _hash_table_for_cache(table_md: str) -> str:
    return hashlib.sha1(table_md.encode("utf-8", errors="ignore")).hexdigest()

def infer_table_capabilities_with_ollama(
    table_name: str,
    columns: List[str],
    table_profile: Dict[str, object],
    model: str,
    url: str,
    timeout: int = 180,
    cache_key: Optional[str] = None,
) -> Dict[str, object]:
    """
    Use LLM to infer:
      - fields: per-column role/type hints
      - table_capabilities: what kinds of questions this table can answer
    IMPORTANT: This is NOT a content summary. We only provide profile + small samples.
    """
    key = cache_key or (table_name + "|" + "|".join(columns))
    if key in _CAP_CACHE:
        return _CAP_CACHE[key]

    # Provide only small samples for safety/cost
    value_samples = (table_profile or {}).get("value_samples") or {}
    small_samples = {}
    for c in (columns or [])[:20]:
        ss = value_samples.get(c) or []
        small_samples[c] = ss[:8]

    prompt = f"""
你是一個「表格檢索導覽」助理。不要摘要表格內容，也不要濃縮/覆寫數據。
你的任務是：根據表格的欄位與少量示例，推斷「欄位語意」與「此表可回答的問題類型」。

表名:
{table_name}

欄位(columns):
{json.dumps(columns, ensure_ascii=False)}

表格輪廓(table_profile):
{json.dumps({
  "n_rows": table_profile.get("n_rows"),
  "n_cols": table_profile.get("n_cols"),
  "numeric_cols": table_profile.get("numeric_cols"),
  "units_candidates": table_profile.get("units_candidates"),
  "notes_candidates": table_profile.get("notes_candidates"),
}, ensure_ascii=False)}

欄位示例(value_samples，僅少量):
{json.dumps(small_samples, ensure_ascii=False)}

請輸出 STRICT JSON，格式如下（不要多餘文字）:
{{
  "fields": {{
    "欄位名": {{"role": "entity|category|measure|constraint|note|time|location|id|text|other", "type": "string|number|mixed"}}
  }},
  "table_capabilities": [
    "可回答的問題類型1",
    "可回答的問題類型2"
  ]
}}

規則:
- fields 要涵蓋所有 columns（若不確定，用 role=other, type=mixed）
- table_capabilities 著重「怎麼查」：例如用哪個欄位查到哪個欄位
- 不要生成任何具體數值結論（避免把巨表壓扁造成資訊消失）
"""

    raw = ollama_generate(model=model, prompt=prompt, url=url, temperature=0.0, timeout=timeout)
    data = safe_json_load(raw)
    if not isinstance(data, dict):
        data = {}

    fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}
    caps = data.get("table_capabilities") if isinstance(data.get("table_capabilities"), list) else []

    # Ensure every column exists in fields
    fixed_fields = {}
    for c in columns:
        v = fields.get(c)
        if isinstance(v, dict):
            role = v.get("role") or "other"
            typ = v.get("type") or "mixed"
            fixed_fields[c] = {"role": role, "type": typ}
        else:
            fixed_fields[c] = {"role": "other", "type": "mixed"}

    res = {"fields": fixed_fields, "table_capabilities": caps[:8]}
    _CAP_CACHE[key] = res
    return res


# ============================================================
# Markdown stripping to plain + placeholders
# ============================================================

def strip_md_to_plain(md: str):
    code_blocks, tables = {}, {}
    tables_meta: Dict[str, Dict] = {}

    def repl_code(m):
        i = len(code_blocks) + 1
        key = f"[[CODEBLOCK_{i:03d}]]"
        code_blocks[key] = m.group(0)
        return key

    # 1) code blocks
    md = re.sub(r"```[\s\S]*?```", repl_code, md)

    # 2) tables -> placeholders
    lines = md.splitlines()
    out_lines = []
    i = 0
    tcount = 0
    while i < len(lines):
        if re.match(r"^\s*\|.*\|\s*$", lines[i]):
            buf = [lines[i]]
            i += 1
            while i < len(lines) and re.match(r"^\s*\|.*\|\s*$", lines[i]):
                buf.append(lines[i])
                i += 1
            tcount += 1
            key = f"[[TABLE_{tcount:03d}]]"
            table_md = "\n".join(buf)
            tables[key] = table_md

            # infer table name: nearest previous non-empty non-table line
            j = len(out_lines) - 1
            name = ""
            while j >= 0:
                prev = out_lines[j].strip()
                if not prev:
                    j -= 1
                    continue
                if re.match(r"^\s*\|.*\|\s*$", prev):
                    j -= 1
                    continue
                name = prev
                break

            # columns from first row
            cols = []
            try:
                first = buf[0].strip()
                if first.startswith("|"):
                    parts = [c.strip() for c in first.strip("|").split("|")]
                    cols = [c for c in parts if c]
            except Exception:
                pass

            tables_meta[key] = {"index": tcount, "raw": table_md, "name": name, "columns": cols}
            out_lines.append(key)
        else:
            out_lines.append(lines[i])
            i += 1

    # 3) stitch adjacent tables (cross-page)
    stitch_adjacent_tables(out_lines, tables, tables_meta,
                           max_gap_lines=6, min_header_sim=0.7, small_tail=3)

    md = "\n".join(out_lines)

    # 4) cleanup markdown artifacts
    md = re.sub(r"^#{1,6}\s+", "", md, flags=re.MULTILINE)
    md = re.sub(r"^\s*[-*+]\s+", "", md, flags=re.MULTILINE)
    md = re.sub(r"^\s*\d+[\.\)]\s+", "", md, flags=re.MULTILINE)
    md = re.sub(r"\n{3,}", "\n\n", md)

    return md, code_blocks, tables, tables_meta


# ============================================================
# Segmentation helpers (existing)
# ============================================================

def approx_chars_for_tokens(target_tokens: int) -> int:
    return int(target_tokens * 1.4)

def build_segment_prompt(text: str, target_tokens: int, hard_min_chars: int) -> str:
    target_chars = approx_chars_for_tokens(target_tokens)
    return f"""You are a Chinese text segmenter. Return STRICT JSON only.

Task:
- Given a plain Chinese text, segment it into semantic chunks.
- Each chunk should be around {target_tokens} tokens (~{target_chars} chars).
- Boundaries must fall on paragraph ends; do NOT cut inside placeholders [[CODEBLOCK_xxx]] or [[TABLE_xxx]].
- Prefer splitting at topic transitions or section-like transitions.
- Cover the whole text; chunks must be contiguous and non-overlapping. If the trailing tail is short (>{hard_min_chars} chars), keep it.
- Provide short Chinese titles (<= 16 chars). If unclear, use a phrase from the first sentence.

Output STRICT JSON only:
{{"spans":[{{"start":0,"end":1234,"title":"..."}}]}}

TEXT BEGIN
{text}
TEXT END
"""

def validate_spans(spans: List[Dict], n: int) -> List[Dict]:
    cleaned = []
    last_end = 0
    for sp in spans:
        try:
            s = int(sp.get("start", 0)); e = int(sp.get("end", 0))
            title = sp.get("title") or ""
        except Exception:
            continue
        s = max(0, min(n, s)); e = max(0, min(n, e))
        if e < s:
            s, e = e, s
        if s < last_end:
            s = last_end
        if e <= s:
            continue
        cleaned.append({"start": s, "end": e, "title": title})
        last_end = e
    if cleaned and cleaned[-1]["end"] < n:
        cleaned.append({"start": cleaned[-1]["end"], "end": n, "title": ""})
    if not cleaned:
        cleaned = [{"start": 0, "end": n, "title": ""}]
    return cleaned

def fallback_segment(text: str, target_tokens: int, hard_min_chars: int) -> List[Dict]:
    target_chars = approx_chars_for_tokens(target_tokens)
    paras = [p.strip() for p in re.split(r"\n\n+", text) if p.strip()]
    spans = []
    cur = ""
    start_idx = 0
    cursor = 0
    for p in paras:
        pos = text.find(p, cursor)
        if pos == -1:
            continue
        if not cur:
            start_idx = pos
        inter = text[cursor:pos]
        cur = (cur + inter + p) if cur else (inter + p)
        cursor = pos + len(p)
        if len(cur) >= target_chars:
            end_idx = start_idx + len(cur)
            spans.append({"start": start_idx, "end": end_idx, "title": ""})
            cur = ""
    if cur:
        spans.append({"start": start_idx, "end": start_idx + len(cur), "title": ""})
    if not spans:
        spans = [{"start": 0, "end": len(text), "title": ""}]
    fixed = []
    for sp in spans:
        if fixed and (sp["end"] - sp["start"] < hard_min_chars):
            fixed[-1]["end"] = sp["end"]
        else:
            fixed.append(sp)
    return fixed

def segment_with_ollama(text: str, model: str, url: str, target_tokens: int, hard_min_chars: int, timeout: int = 300) -> List[Dict]:
    sys_prompt = "You return STRICT JSON only. No code fences, no extra text."
    prompt = sys_prompt + "\n\n" + build_segment_prompt(text, target_tokens, hard_min_chars)
    resp = requests.post(url, json={
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.0}
    }, timeout=timeout)
    resp.raise_for_status()
    raw = resp.json().get("response", "")
    data = safe_json_load(raw)
    spans = data.get("spans", [])
    return spans

def segment_text(text: str, window_chars: int, window_overlap: int):
    n = len(text)
    if n <= window_chars:
        return [(0, text)]
    out = []
    i = 0
    while i < n:
        end = min(n, i + window_chars)
        out.append((i, text[i:end]))
        if end == n:
            break
        i = end - window_overlap
        if i < 0:
            i = 0
    return out


# ============================================================
# Placeholder restore + table split inside a segment
# ============================================================

def restore_placeholders(text: str, code_map: Dict[str,str], table_map: Dict[str,str]) -> str:
    merged = {**code_map, **table_map}
    for k in sorted(merged.keys(), key=len, reverse=True):
        text = text.replace(k, merged[k])
    return text

def split_tables_in_segment(segment_plain: str):
    """
    Split a span's text into pieces, isolating each [[TABLE_xxx]] placeholder.
    Returns list of (piece_text_or_key, table_key_or_None)
    """
    parts = []
    pos = 0
    for m in re.finditer(r"\[\[TABLE_(\d{3})\]\]", segment_plain):
        start, end = m.span()
        key = m.group(0)
        if start > pos:
            parts.append((segment_plain[pos:start], None))
        parts.append((key, key))
        pos = end
    if pos < len(segment_plain):
        parts.append((segment_plain[pos:], None))
    return parts


# ============================================================
# Optional (kept): short table summary (NOT recommended for huge tables)
# ============================================================

def summarize_table_with_ollama(table_md: str, model: str, url: str, timeout: int = 120) -> Dict[str, object]:
    prompt = (
        "請閱讀下面的 Markdown 表格，"
        "用 1~3 句的中文，總結這個表格的主要內容（包含主題、重要欄位代表的意義）。"
        "並我挑出最多 12 個適合作為搜尋用的關鍵詞（中文即可）\n"
        "請只回傳 JSON，格式如下：\n"
        "{\"summary\":\"...\",\"key_terms\":[\"...\",\"...\"]}\n\n"
        "表格如下：\n" + table_md
    )
    try:
        resp = requests.post(url, json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.0}
        }, timeout=timeout)
        resp.raise_for_status()
        raw = resp.json().get("response","")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("non-dict")
        summary = data.get("summary","")
        key_terms = data.get("key_terms",[])
        if not isinstance(key_terms, list):
            key_terms = []
        return {"summary": summary, "key_terms": key_terms}
    except Exception:
        # fallback: column names
        terms = []
        for line in table_md.splitlines()[:2]:
            if "|" in line:
                parts = [c.strip() for c in line.strip("|").split("|") if c.strip()]
                terms.extend(parts)
        terms = list(dict.fromkeys(terms))[:12]
        return {"summary": "", "key_terms": terms}


# ============================================================
# Metadata builder (enhanced)
# ============================================================

def make_metadata(doc_id: str, source_path: str, is_table: bool, model_hint: str,
                  table_key: Optional[str], tables_meta: Dict[str,Dict],
                  summarize: bool, summary_model: str, url: str,
                  infer_table_capabilities: bool = False,
                  capabilities_model: Optional[str] = None) -> Dict[str, object]:
    meta = {
        "doc_id": doc_id,
        "source_path": source_path,
        "is_table": bool(is_table),
        "model_hint": model_hint
    }

    if is_table and table_key and table_key in tables_meta:
        tmeta = tables_meta[table_key]
        meta["table_index"] = tmeta.get("index")
        meta["table_name"] = tmeta.get("name") or ""
        cols = tmeta.get("columns") or []
        meta["columns"] = cols

        # NEW: deterministic profile + robust key_terms (always)
        table_raw = tmeta.get("raw", "")
        table_profile = build_table_profile(table_raw)
        meta["table_profile"] = table_profile
        meta["key_terms"] = build_key_terms(cols, table_profile)

        # Kept: optional short summary (can cause info loss for huge tables)
        if summarize:
            sres = summarize_table_with_ollama(table_raw, model=summary_model, url=url)
            meta["summary"] = sres.get("summary", "")
            # merge key_terms, but keep deterministic first
            extra = sres.get("key_terms") or []
            if isinstance(extra, list):
                meta["key_terms"] = _dedup_keep_order(meta["key_terms"] + extra)

        # NEW: optional LLM "capabilities" + "fields" (navigation, no summarization)
        if infer_table_capabilities:
            cap_model = capabilities_model or summary_model
            cache_key = _hash_table_for_cache(table_raw)
            try:
                capres = infer_table_capabilities_with_ollama(
                    table_name=meta.get("table_name",""),
                    columns=cols,
                    table_profile=table_profile,
                    model=cap_model,
                    url=url,
                    cache_key=cache_key
                )
                if isinstance(capres, dict):
                    meta["fields"] = capres.get("fields", {})
                    meta["table_capabilities"] = capres.get("table_capabilities", [])
            except Exception:
                # do not break pipeline
                meta["fields"] = {c: {"role": "other", "type": "mixed"} for c in cols}
                meta["table_capabilities"] = []

    return meta


# ============================================================
# Chunk builder (enhanced pass-through)
# ============================================================

def build_chunks(plain_text: str, spans: List[Dict], code_map: Dict[str,str], table_map: Dict[str,str],
                 tables_meta: Dict[str,Dict], inter_chunk_overlap_chars: int,
                 doc_id: str, source_path: str,
                 summarize_tables: bool, summary_model: str, ollama_url: str,
                 infer_table_capabilities: bool = False,
                 capabilities_model: Optional[str] = None) -> List[Dict]:
    chunks = []
    for i, sp in enumerate(spans):
        s = sp["start"]; e = sp["end"]; title = sp.get("title") or ""
        s_eff = s if i == 0 else max(0, s - inter_chunk_overlap_chars)
        segment_plain = plain_text[s_eff:e]

        pieces = split_tables_in_segment(segment_plain)

        for piece, table_key in pieces:
            if table_key and table_key in table_map:
                text_restored = restore_placeholders(piece, code_map, table_map)
                meta = make_metadata(
                    doc_id=doc_id,
                    source_path=source_path,
                    is_table=True,
                    model_hint="table",
                    table_key=table_key,
                    tables_meta=tables_meta,
                    summarize=summarize_tables,
                    summary_model=summary_model,
                    url=ollama_url,
                    infer_table_capabilities=infer_table_capabilities,
                    capabilities_model=capabilities_model
                )
                chunks.append({
                    "title": tables_meta.get(table_key,{}).get("name","") or title,
                    "start": s, "end": e,
                    "text": text_restored,
                    "meta": meta
                })
            else:
                if piece.strip():
                    text_restored = restore_placeholders(piece, code_map, table_map)
                    meta = make_metadata(
                        doc_id=doc_id,
                        source_path=source_path,
                        is_table=False,
                        model_hint="text",
                        table_key=None,
                        tables_meta=tables_meta,
                        summarize=False,
                        summary_model=summary_model,
                        url=ollama_url,
                        infer_table_capabilities=False,
                        capabilities_model=capabilities_model
                    )
                    chunks.append({
                        "title": title,
                        "start": s, "end": e,
                        "text": text_restored,
                        "meta": meta
                    })
    return chunks


# ============================================================
# CLI + main pipeline
# ============================================================

def parser():
    ap = argparse.ArgumentParser(description="Table-aware semantic chunking for Marker-produced Markdown using Ollama.")
    ap.add_argument("md", help="Path to Marker-produced Markdown")
    ap.add_argument("--out", default="chunks.json", help="Output JSON path")
    ap.add_argument("--model", default="gpt-oss:20b", help="Ollama model for segmentation")
    ap.add_argument("--ollama-url", default="http://localhost:11434/api/generate", help="Ollama /api/generate endpoint")
    ap.add_argument("--target-tokens", type=int, default=480, help="Target tokens per chunk")
    ap.add_argument("--hard-min-chars", type=int, default=300, help="Minimum chars for the tail chunk before merging")
    ap.add_argument("--window-chars", type=int, default=15000, help="Sliding window size in characters")
    ap.add_argument("--window-overlap", type=int, default=500, help="Overlap between windows")
    ap.add_argument("--inter-chunk-overlap", type=int, default=220, help="Text overlap added at chunk joins")
    ap.add_argument("--doc-id", default=None, help="doc_id for metadata (default: basename of md)")
    ap.add_argument("--source-path", default=None, help="source_path for metadata (default: md path)")

    # Existing (kept)
    ap.add_argument("--summarize-tables", action="store_true",
                    help="(Optional) Use Ollama to short-summarize tables (may lose detail for huge tables)")
    ap.add_argument("--summary-model", default=None,
                    help="Ollama model for table summary (default: same as --model)")

    # NEW
    ap.add_argument("--infer-table-capabilities", action="store_true",
                    help="Use Ollama to infer fields & table_capabilities based on table_profile (no summarization)")
    ap.add_argument("--capabilities-model", default=None,
                    help="Ollama model for capabilities inference (default: --summary-model or --model)")

    return ap.parse_args()


def md_semantic_chunker(md_path: str, output_path: str, **kwargs):
    params = {
        "md": md_path,
        "out": output_path,

        "model": "gpt-oss:20b",
        "ollama_url": "http://localhost:11434/api/generate",
        "target_tokens": 480,
        "hard_min_chars": 300,
        "window_chars": 15000,
        "window_overlap": 500,
        "doc_id": None,
        "source_path": None,
        "inter_chunk_overlap": 220,

        "summarize_tables": False,
        "summary_model": None,

        # NEW
        "infer_table_capabilities": False,
        "capabilities_model": None,
    }
    params.update(kwargs)

    class Args:
        def __init__(self, **entries):
            self.__dict__.update(entries)

    args = Args(**params)

    with open(args.md, "r", encoding="utf-8") as f:
        md = f.read()

    plain, code_map, table_map, tables_meta = strip_md_to_plain(md)

    windows = segment_text(plain, args.window_chars, args.window_overlap)

    win_spans_list = []
    for off, wtxt in windows:
        try:
            spans = segment_with_ollama(
                wtxt,
                model=args.model,
                url=args.ollama_url,
                target_tokens=args.target_tokens,
                hard_min_chars=args.hard_min_chars
            )
            spans = validate_spans(spans, len(wtxt))
        except Exception:
            print("Warning: Ollama segmentation failed for a window. Falling back to simple segmentation.")
            spans = fallback_segment(wtxt, target_tokens=args.target_tokens, hard_min_chars=args.hard_min_chars)
        win_spans_list.append(spans)

    merged = []
    for (offset,_), spans in zip(windows, win_spans_list):
        for sp in spans:
            merged.append({"start": offset + sp["start"], "end": offset + sp["end"], "title": sp.get("title","")})

    merged.sort(key=lambda x: (x["start"], x["end"]))
    merged = validate_spans(merged, len(plain))

    doc_id = args.doc_id or os.path.basename(args.md)
    source_path = args.source_path or os.path.abspath(args.md)
    summary_model = args.summary_model or args.model
    capabilities_model = args.capabilities_model or summary_model or args.model

    chunks = build_chunks(
        plain_text=plain,
        spans=merged,
        code_map=code_map,
        table_map=table_map,
        tables_meta=tables_meta,
        inter_chunk_overlap_chars=args.inter_chunk_overlap,
        doc_id=doc_id,
        source_path=source_path,
        summarize_tables=bool(args.summarize_tables),
        summary_model=summary_model,
        ollama_url=args.ollama_url,
        infer_table_capabilities=bool(args.infer_table_capabilities),
        capabilities_model=capabilities_model
    )

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(chunks, f, ensure_ascii=False, indent=2)

    print(f"Wrote {args.out} (chunks: {len(chunks)})")
    return chunks


def main():
    args = parser()
    md_path = args.md
    output_path = args.out

    kwargs = vars(args).copy()
    kwargs.pop("md", None)
    kwargs.pop("out", None)

    # Align naming to md_semantic_chunker() params
    # argparse uses hyphenated names -> underscore already in vars(args)
    # but we also unify to our internal keys if needed:
    kwargs["summarize_tables"] = kwargs.pop("summarize_tables", False)
    kwargs["summary_model"] = kwargs.pop("summary_model", None)
    kwargs["infer_table_capabilities"] = kwargs.pop("infer_table_capabilities", False)
    kwargs["capabilities_model"] = kwargs.pop("capabilities_model", None)
    kwargs["inter_chunk_overlap"] = kwargs.pop("inter_chunk_overlap", kwargs.get("inter_chunk_overlap", 220))

    md_semantic_chunker(md_path=md_path, output_path=output_path, **kwargs)


if __name__ == "__main__":
    main()
