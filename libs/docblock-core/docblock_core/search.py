# core/search.py
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple, Literal

import psycopg2
import psycopg2.extras
import requests

import urllib3
from urllib3.exceptions import InsecureRequestWarning

from docblock_core.config import settings
from docblock_core.logging_utils import get_module_logger

search_log = settings.logs.search_log if hasattr(settings.logs, "search_log") else "logs/search.log"
logs_dir = settings.logs.logs_dir if hasattr(settings.logs, "logs_dir") else "logs"
logger = get_module_logger("core.search", logs_dir, search_log)

# load config from environment variables


# FlagReranker is imported lazily (inside _rerank_hits) to avoid forcing
# GPU/torch dependency at import time and allow lightweight startup.

# ---------------------------
# Types
# ---------------------------

SourceType = Literal[
    "text",
    "table_dense",
    "table_lex",
    "image_text",
    "summary",
    "summary_lex",
]

AccessLevel = Literal["detail", "summary", "deny"]

@dataclass
class SearchHit:
    source: SourceType
    doc_id: str
    document_id: str  # UUID string
    chunk_index: int  # for summary hits, we use 0
    score: float  # higher is better
    content: str
    metadata: Dict[str, Any]
    doc_url: Optional[str] = None  # external URL for the document, if available


# ---------------------------
# Utils
# ---------------------------

def _to_pg_vector_literal(vec: Sequence[float], fmt: str = ".8f") -> str:
    return "[" + ",".join(format(float(x), fmt) for x in vec) + "]"


def _safe_json(obj: Any) -> Dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    try:
        return json.loads(obj)
    except Exception:
        return {"_raw": str(obj)}


def _rank_norm(hits: List[SearchHit], method: str = "rrf", k: int = 60) -> List[Tuple[SearchHit, float]]:
    """
    Normalize per-list scores into comparable scale.
    Default: RRF (Reciprocal Rank Fusion) style: 1/(k + rank).
    """
    if not hits:
        return []
    if method == "rrf":
        return [(h, 1.0 / (k + i)) for i, h in enumerate(hits, start=1)]

    scores = [h.score for h in hits]
    lo, hi = min(scores), max(scores)
    if hi - lo < 1e-9:
        return [(h, 1.0) for h in hits]
    return [(h, (h.score - lo) / (hi - lo)) for h in hits]


def _dedupe_by_key(hits: List[Tuple[SearchHit, float]]) -> List[Tuple[SearchHit, float]]:
    """
    Dedupe by (document_id, chunk_index, source family).
    Keep max fused score.
    """
    best: Dict[Tuple[str, int, str], Tuple[SearchHit, float]] = {}
    for h, s in hits:
        fam = "table" if h.source.startswith("table") else ("image" if h.source.startswith("image") else ("summary" if h.source.startswith("summary") else "text"))
        key = (h.document_id, h.chunk_index, fam)
        cur = best.get(key)
        if cur is None or s > cur[1]:
            best[key] = (h, s)
    return list(best.values())

def _normalize_content(text: str) -> str:
    text = text.strip()
    text = text.replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text)
    return text

def _content_hash(text: str) -> str:
    norm = _normalize_content(text)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()

def _dedupe_by_content(hits: List[SearchHit]) -> List[SearchHit]:
    best = {}

    for h in hits:
        fam = (
            "table" if h.source.startswith("table")
            else "image" if h.source.startswith("image")
            else "summary" if h.source.startswith("summary")
            else "text"
        )

        key = (_content_hash(h.content or ""), fam)

        cur = best.get(key)
        if cur is None or h.score > cur.score:
            best[key] = h

    return list(best.values())



# ---------------------------
# Routing
# ---------------------------

ROUTING_PROFILES: Dict[str, Dict[str, float]] = {
    "balanced": {"text": 1.30, "table_dense": 1.00, "table_lex": 1.00, "image_text": 0.80, "summary": 1.00, "summary_lex": 1.10},
    #"balanced": {"text": 1.00, "table_dense": 1.20, "table_lex": 1.30, "image_text": 0.80, "summary": 1.00, "summary_lex": 1.10},
    "table_focus": {"text": 0.80, "table_dense": 1.50, "table_lex": 1.70, "image_text": 0.60, "summary": 0.90, "summary_lex": 1.00},
    "image_focus": {"text": 0.80, "table_dense": 0.90, "table_lex": 0.90, "image_text": 1.80, "summary": 0.90, "summary_lex": 1.00},
    "text_focus": {"text": 1.60, "table_dense": 0.90, "table_lex": 0.90, "image_text": 0.70, "summary": 1.10, "summary_lex": 1.10},
    "lexical_focus": {"text": 0.70, "table_dense": 1.00, "table_lex": 2.20, "image_text": 0.60, "summary": 0.80, "summary_lex": 1.60},
    # when user is in "summary" permission tier, this is the only profile that matters
    "summary_only": {"summary": 1.00, "summary_lex": 1.20},
}

ROUTER_SYSTEM = """\
You are a query router for a RAG system.
Choose ONE profile for retrieval weighting based on the user's question.

Profiles:
- balanced: general questions
- text_focus: explanations, mechanisms, definitions, summaries
- table_focus: numeric comparison, values, limits, GI, kcal, ranges, "which is lowest/highest", "compare"
- lexical_focus: codes, abbreviations, field names, exact terms, identifiers, numbers
- image_focus: figures, diagrams, chemical structures, flowcharts, "in the figure", "image shows"

Return ONLY valid JSON: {"profile": "<one of: balanced, text_focus, table_focus, lexical_focus, image_focus>"}.
"""


def _parse_router_json(s: str) -> str:
    try:
        obj = json.loads(s)
        p = obj.get("profile")
        if isinstance(p, str) and p in ROUTING_PROFILES:
            return p
    except Exception:
        pass
    return "balanced"


# ---------------------------
# Search client
# ---------------------------

class DocblockSearchClient:
    """
    Multi-tenant, ACL-aware search client (detail/summary/deny).

    Key points:
    - Uses documents.(tenant_id, doc_id) to resolve document_id(UUID) and active_version
    - For "detail" docs: searches text_chunks / table_chunks / image_chunks (version=active_version)
    - For "summary" docs: searches summary_chunks only
    """

    def __init__(
        self,
        pg_dsn: Optional[str] = None,
        tenant_id: Optional[str] = None,
        ollama_base_url: Optional[str] = None,
        litellm_base_url: Optional[str] = None,
        embed_model: Optional[str] = None,
        embed_timeout: Optional[int] = None,
        rerank_model: Optional[str] = None,
    ) -> None:
        self.pg_dsn = pg_dsn if pg_dsn is not None else settings.db.pg_dsn
        self.tenant_id = tenant_id if tenant_id is not None else getattr(settings.db, "tenant_id", None)
        self.ollama_base_url = ollama_base_url if ollama_base_url is not None else settings.models.ollama_base_url
        self.litellm_base_url = litellm_base_url if litellm_base_url is not None else settings.models.litellm_base_url
        self.embed_model = embed_model if embed_model is not None else settings.models.embed_model
        self.embed_timeout = int(embed_timeout if embed_timeout is not None else settings.models.embed_timeout)
        #self.reranker = FlagReranker(rerank_model if rerank_model is not None else settings.models.rerank_model, use_fp16=True)
        self.reranker = None  # 暫時不在這裡初始化，改成在需要的時候才初始化，避免不必要的依賴和啟動成本
        self.rerank_model = rerank_model if rerank_model is not None else settings.models.rerank_model

        if not self.pg_dsn:
            raise ValueError("PG_DSN is empty. Set settings.db.pg_dsn or env PG_DSN.")
        if not self.litellm_base_url:
            raise ValueError("LITELLM_BASE_URL is empty. Set settings.models.litellm_base_url or env LITELLM_BASE_URL.")
        if not self.tenant_id:
            # we *can* still work if caller passes tenant_id per-call, but fail fast helps
            raise ValueError("TENANT_ID is empty. Set settings.db.tenant_id or pass tenant_id=...")

    # ---------------------------
    # Embedding
    # ---------------------------
    def embed_query(self, query: str) -> List[float]:
        url = f"{self.litellm_base_url.rstrip('/')}/v1/embeddings"
        r = requests.post(
            url,
            json={"model": self.embed_model, "input": query},
            timeout=self.embed_timeout,
        )
        r.raise_for_status()
        data = r.json()
        try:
            emb = data["data"][0]["embedding"]
        except (KeyError, IndexError):
            raise ValueError(f"Unexpected embeddings response: {data}")
        if not isinstance(emb, list):
            raise ValueError(f"Unexpected embeddings response: {data}")
        return emb

    def _build_passage(self, hit: "SearchHit", max_passage_chars: int = 1800) -> str:
        """
        Build a passage text for reranking, including metadata and content.
        """
        meta = hit.metadata or {}

        title = (
            meta.get("document_name")
            or meta.get("file_name")
            or meta.get("title")
            or ""
        )
        section = (
            meta.get("section_title")
            or meta.get("heading")
            or meta.get("section")
            or ""
        )
        source = hit.source or ""
        doc_id = hit.document_id or hit.doc_id or ""
        chunk_index = hit.chunk_index

        parts = []
        if title:
            parts.append(f"Document: {title}")
        if section:
            parts.append(f"Section: {section}")
        if source:
            parts.append(f"Source: {source}")
        if doc_id:
            parts.append(f"Document ID: {doc_id}")
        if chunk_index is not None:
            parts.append(f"Chunk Index: {chunk_index}")

        content = (hit.content or "").strip()
        if len(content) > max_passage_chars:
            content = content[:max_passage_chars] + "\n...[truncated]"

        parts.append("Passage:")
        parts.append(content)
        return "\n".join(parts)

    # ---------------------------
    # Reranking
    # ---------------------------
    def rerank_hits(
        self,
        *,
        query: str,
        hits: List["SearchHit"],
        top_n: Optional[int] = None,
        reranker_model: str = "qwen3:8b",
        litellm_base_url: Optional[str] = None,
        timeout: int = 120,
        max_passage_chars: int = 1800,
    ) -> List["SearchHit"]:
        """
        LLM-based rerank via LiteLLM /v1/chat/completions.

        Strategy:
        - Score each (query, hit) pair with a LiteLLM model
        - Use JSON output for stability
        - Keep fused_score in metadata and overwrite hit.score with rerank_score
        """

        if not hits:
            return []

        _base_url = (litellm_base_url or self.litellm_base_url).rstrip("/")

        def _call_litellm_score(query_text: str, passage_text: str) -> tuple[float, str]:
            """
            Return: (score, short_reason)
            score range: 0.0 ~ 1.0
            """
            system_prompt = (
                "You are a retrieval reranker for a RAG system.\n"
                "Your task is to score how useful the passage is for answering the query.\n"
                "Score from 0 to 1.\n"
                "Higher score means the passage is more directly useful, more specific, and more answer-bearing.\n"
                "Do not reward mere keyword overlap.\n"
                "Prefer passages that can directly answer the query.\n"
                'Output only valid JSON: {"score": <0.0-1.0>, "reason": "<short reason>"}'
            )

            user_prompt = (
                f"Query:\n{query_text}\n\n"
                f"{passage_text}\n\n"
                "Scoring guidance:\n"
                "- 0.9~1.0: directly answers the query\n"
                "- 0.7~0.89: strongly relevant, likely useful\n"
                "- 0.4~0.69: somewhat relevant but incomplete\n"
                "- 0.1~0.39: weakly related\n"
                "- 0.0~0.09: irrelevant\n"
            )

            payload = {
                "model": reranker_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0,
                "stream": False,
            }

            url = f"{_base_url}/v1/chat/completions"
            resp = requests.post(url, json=payload, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()

            raw_text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
            if not raw_text:
                return 0.0, "empty response"

            try:
                obj = json.loads(raw_text)
                score = float(obj.get("score", 0.0))
                reason = str(obj.get("reason", ""))
                score = max(0.0, min(1.0, score))
                return score, reason
            except Exception:
                return 0.0, f"invalid json: {raw_text[:200]}"

        reranked: List["SearchHit"] = []

        for hit in hits:
            passage = self._build_passage(hit, max_passage_chars=max_passage_chars)

            try:
                rerank_score, reason = _call_litellm_score(query, passage)
            except Exception as e:
                rerank_score = 0.0
                reason = f"rerank_failed: {e}"

            meta = dict(hit.metadata or {})
            meta["rerank_model"] = reranker_model
            meta["rerank_score"] = float(rerank_score)
            meta["rerank_reason"] = reason
            meta["rank_stage"] = "reranked"

            # 保留原本 fused score
            if "fused_score" not in meta:
                meta["fused_score"] = float(hit.score)

            reranked.append(
                SearchHit(
                    source=hit.source,
                    doc_id=hit.doc_id,
                    document_id=hit.document_id,
                    chunk_index=hit.chunk_index,
                    score=float(rerank_score),   # 最終排序用 rerank score
                    content=hit.content,
                    metadata=meta,
                )
            )

        reranked.sort(key=lambda h: h.score, reverse=True)

        if top_n is not None:
            reranked = reranked[:top_n]

        return reranked

    # ---------------------------
    # BGE rerank
    # ---------------------------
    def rerank_hits_bge(
        self,
        *,
        query: str,
        hits: List["SearchHit"],
        top_n: Optional[int] = None,
        max_passage_chars: int = 1800,
        rerank_model: str = settings.models.rerank_model,
    ) -> List["SearchHit"]:
        """
        BAAI BGE-based rerank using Cross-Encoder.
        """
        if not hits:
            return []
        
        # 1. 準備批次資料對 (Query, Passage)
        # Cross-Encoder 的輸入格式通常是 [[q, p1], [q, p2], ...]
        pairs = []
        for hit in hits:
            # 複用你原本優雅的 _build_passage 邏輯
            passage = self._build_passage(hit, max_passage_chars)
            pairs.append([query, passage])

        # 2. 執行批次推理 (這比 Ollama 一個一個跑快非常多)
        # scores 會返回一個 float 列表，數值通常在實數區間（越大的負數或正數代表相關性）
        raw_scores: List[float] = []
        try:
            # 延遲初始化 FlagReranker，避免不必要的啟動成本和依賴問題
            if self.reranker is None:
                from FlagEmbedding import FlagReranker  # noqa: PLC0415
                self.reranker = FlagReranker(rerank_model, use_fp16=True)
            
            # compute_score 支持一次傳入整個 pairs 列表
            computed_scores = self.reranker.compute_score(pairs)
            
            # 如果只有一個 hit，FlagEmbedding 有時會返回 float 而不是 list，做個處理
            if isinstance(computed_scores, float):
                raw_scores = [computed_scores]
            elif isinstance(computed_scores, list):
                raw_scores = [float(s) for s in computed_scores]
            else:
                raw_scores = []

            if len(raw_scores) != len(hits):
                raise ValueError(f"Unexpected rerank score length: got {len(raw_scores)}, expected {len(hits)}")
                
        except Exception as e:
            print(f"BGE Rerank error: {e}")
            # 發生錯誤時回傳原始結果（或自定義降級邏輯）
            return hits[:top_n] if top_n else hits

        # 3. 組裝結果
        reranked: List["SearchHit"] = []
        for i, hit in enumerate(hits):
            score = float(raw_scores[i])
            
            meta = dict(hit.metadata or {})
            meta["rerank_model"] = rerank_model
            meta["rerank_score"] = score
            meta["rank_stage"] = "reranked"

            if "fused_score" not in meta:
                meta["fused_score"] = float(hit.score)

            # 這裡要注意：BGE 的分數不是 0-1 概率，而是相似度分值（可能大於 1 或小於 0）
            # 排序邏輯維持不變：分數越高越相關
            reranked.append(
                SearchHit(
                    source=hit.source,
                    doc_id=hit.doc_id,
                    document_id=hit.document_id,
                    chunk_index=hit.chunk_index,
                    score=score, 
                    content=hit.content,
                    metadata=meta,
                )
            )

        # 4. 排序與過濾
        reranked.sort(key=lambda h: h.score, reverse=True)
        if top_n is not None:
            reranked = reranked[:top_n]

        return reranked
    
    
    # ---------------------------
    # HTTP Reranking (via proxy → Nostr → LiteLLM)
    # ---------------------------
    def rerank_hits_http(
        self,
        *,
        query: str,
        hits: List["SearchHit"],
        top_n: Optional[int] = None,
        reranker_model: Optional[str] = None,
        timeout: int = 120,
        max_passage_chars: int = 1800,
    ) -> List["SearchHit"]:
        """
        Rerank via POST {base_url}/v1/rerank (OpenAI format).
        base_url = OLLAMA_BASE_URL, which points to nostr-proxy.
        nostr-proxy routes the request as Kind 2001 → LiteLLM → vLLM reranker.
        Falls back to original ordering on any error.
        """
        if not hits:
            return []

        reranker_model = reranker_model or self.rerank_model
        documents = [self._build_passage(h, max_passage_chars) for h in hits]

        url = f"{self.litellm_base_url.rstrip('/')}/v1/rerank"
        payload = {"model": reranker_model, "query": query, "documents": documents}

        try:
            resp = requests.post(url, json=payload, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("[rerank_hits_http] request failed, returning original order: %s", e)
            return hits[:top_n] if top_n else hits

        results = data.get("results", [])
        score_map = {r["index"]: float(r.get("relevance_score", 0.0)) for r in results}

        reranked: List["SearchHit"] = []
        for i, hit in enumerate(hits):
            score = score_map.get(i, 0.0)
            meta = dict(hit.metadata or {})
            meta["rerank_model"] = reranker_model
            meta["rerank_score"] = score
            meta["rank_stage"] = "reranked"
            meta.setdefault("fused_score", float(hit.score))
            reranked.append(
                SearchHit(
                    source=hit.source,
                    doc_id=hit.doc_id,
                    document_id=hit.document_id,
                    chunk_index=hit.chunk_index,
                    score=score,
                    content=hit.content,
                    metadata=meta,
                )
            )

        reranked.sort(key=lambda h: h.score, reverse=True)
        if top_n is not None:
            reranked = reranked[:top_n]
        return reranked

    # ---------------------------
    # Routing
    # ---------------------------
    def route_profile(self, query: str, *, router_model: str = "qwen2:7b", timeout: int = 30) -> str:
        url = f"{self.litellm_base_url.rstrip('/')}/v1/chat/completions"
        payload = {
            "model": router_model,
            "messages": [
                {"role": "system", "content": ROUTER_SYSTEM},
                {"role": "user", "content": query},
            ],
            "stream": False,
        }
        try:
            r = requests.post(url, json=payload, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            return _parse_router_json(content.strip())
        except Exception:
            return "balanced"
        
    # ---------------------------
    # Routing: lexical filtering
    # ---------------------------    
    def route_docs_text_lexical(
        self,
        *,
        tenant_id: str,
        doc_ids: List[str],
        query: str,
        n: int = 100,
    ) -> List[str]:
        """
        Routing: use FTS over text_chunks.content to pick Top-N candidate docs.
        Returns: list of doc_id (external id, i.e., documents.doc_id).
        """
        if not doc_ids:
            return []

        sql = """
        SELECT
          d.doc_id,
          MAX(
            ts_rank(
              to_tsvector('simple', COALESCE(tc.content,'')),
              plainto_tsquery('simple', %(q)s)
            )
          ) AS score
        FROM documents d
        JOIN text_chunks tc
          ON tc.tenant_id = d.tenant_id
         AND tc.document_id = d.document_id
         AND tc.version = d.active_version
        WHERE d.tenant_id = %(tenant_id)s
          AND d.doc_id = ANY(%(doc_ids)s::text[])
          AND plainto_tsquery('simple', %(q)s) @@ to_tsvector('simple', COALESCE(tc.content,''))
        GROUP BY d.doc_id
        ORDER BY score DESC
        LIMIT %(n)s
        """

        with self._conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, {"tenant_id": tenant_id, "doc_ids": doc_ids, "q": query, "n": n})
            rows = cur.fetchall()

        # 只回 doc_id；如果你想 debug，也可以把 score 一起回傳
        return [r["doc_id"] for r in rows if r.get("doc_id")]

    # ---------------------------
    # DB helpers
    # ---------------------------
    def _conn(self):
        return psycopg2.connect(self.pg_dsn)

    def _resolve_doc(self, *, tenant_id: str, doc_id: str) -> Tuple[str, int]:
        """
        Resolve (document_id, active_version) by (tenant_id, doc_id).
        """
        sql = """
        SELECT document_id::text AS document_id, active_version
        FROM documents
        WHERE tenant_id = %(tenant_id)s AND doc_id = %(doc_id)s
        """
        with self._conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, {"tenant_id": tenant_id, "doc_id": doc_id})
            row = cur.fetchone()
        if not row:
            raise ValueError(f"Unknown doc_id '{doc_id}' for tenant_id='{tenant_id}'")
        return str(row["document_id"]), int(row["active_version"])
    
    # get hit url
    def _get_hit_url(self, doc_id: str) -> str:
        outline_api_token = settings.outline.api_token
        outline_url = settings.outline.outline_url

        if not doc_id:
            return ""
        if not outline_api_token or not outline_url:
            #logger.warning("[_get_hit_url] missing outline settings")
            return ""
        
        outline_session = requests.Session()
        outline_session.verify = False
        urllib3.disable_warnings(InsecureRequestWarning)
        outline_session.headers.update({"Authorization": f"Bearer {outline_api_token}"})
        #logger.info("[_get_hit_url] fetching doc info from outline for doc_id=%s", doc_id)
        try:
            r = outline_session.post(
                f"{outline_url}/api/documents.info",
                json={"id": doc_id},
                timeout=10,
            )

            if r.status_code == 404:
                #logger.warning("[_get_hit_url] doc_id not found in outline: %s", doc_id)
                return ""
            
            r.raise_for_status()
            doc_info = r.json().get("data") or {}
            url_path = doc_info.get("url")
            #logger.info("[_get_hit_url] got doc info from outline for doc_id=%s url_path=%s", doc_id, url_path)
            if not url_path:
                return ""
            if isinstance(url_path, str) and (url_path.startswith("http://") or url_path.startswith("https://")):
                return url_path
            return f"{outline_url}{url_path}"

        except requests.RequestException as e:
            #logger.warning("[_get_hit_url] outline request failed for doc_id=%s error=%s", doc_id, str(e))
            return ""
        except (ValueError, TypeError) as e:
            #logger.warning("[_get_hit_url] invalid outline response for doc_id=%s error=%s", doc_id, str(e))
            return ""

    # ---------------------------
    # Low-level searches (MULTI-DOC)
    # ---------------------------
    def search_text_dense_multi(
        self,
        *,
        tenant_id: str,
        doc_ids: List[str],
        qvec: List[float],
        k: int = 200,
    ) -> List[SearchHit]:
        """
        Dense search across multiple docs in a single query (text_chunks).
        Returns chunk-level SearchHit (source='text') with RAW dense score.
        """
        if not doc_ids:
            return []

        qv = _to_pg_vector_literal(qvec)

        sql = """
        SELECT
          d.doc_id,
          tc.document_id::text AS document_id,
          tc.chunk_index,
          tc.page_start,
          tc.page_end,
          tc.heading_path,
          tc.content,
          tc.metadata,
          (1 - (tc.embedding <=> %(qvec)s::vector)) AS score
        FROM documents d
        JOIN text_chunks tc
          ON tc.tenant_id = d.tenant_id
         AND tc.document_id = d.document_id
         AND tc.version = d.active_version
        WHERE d.tenant_id = %(tenant_id)s
          AND d.doc_id = ANY(%(doc_ids)s::text[])
          AND tc.embedding IS NOT NULL
        ORDER BY tc.embedding <=> %(qvec)s::vector
        LIMIT %(k)s
        """

        with self._conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, {"tenant_id": tenant_id, "doc_ids": doc_ids, "qvec": qv, "k": k})
            rows = cur.fetchall()

        hits: List[SearchHit] = []
        for r in rows:
            meta = _safe_json(r["metadata"])
            meta.setdefault("page_start", r.get("page_start"))
            meta.setdefault("page_end", r.get("page_end"))
            meta.setdefault("heading_path", r.get("heading_path"))

            hits.append(
                SearchHit(
                    source="text",
                    doc_id=r["doc_id"],
                    document_id=r["document_id"],
                    #doc_url=self._get_hit_url(doc_id=r["doc_id"]),
                    chunk_index=int(r["chunk_index"]),
                    score=float(r["score"]) if r["score"] is not None else 0.0,
                    content=r["content"] or "",
                    metadata=meta,
                )
            )
        return hits
    
    def search_table_dense_multi(
        self,
        *,
        tenant_id: str,
        doc_ids: List[str],
        qvec: List[float],
        k: int = 200,
    ) -> List[SearchHit]:
        if not doc_ids:
            return []

        qv = _to_pg_vector_literal(qvec)

        sql = """
        SELECT
        d.doc_id,
        tb.document_id::text AS document_id,
        tb.chunk_index,
        tb.page_start,
        tb.page_end,
        tb.table_key,
        tb.table_title,
        tb.raw_table_md,
        tb.metadata,
        (1 - (tb.embedding <=> %(qvec)s::vector)) AS score
        FROM documents d
        JOIN table_chunks tb
        ON tb.document_id = d.document_id
        AND tb.version = d.active_version
        WHERE d.tenant_id = %(tenant_id)s
        AND d.doc_id = ANY(%(doc_ids)s::text[])
        AND tb.embedding IS NOT NULL
        ORDER BY tb.embedding <=> %(qvec)s::vector
        LIMIT %(k)s
        """

        with self._conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, {"tenant_id": tenant_id, "doc_ids": doc_ids, "qvec": qv, "k": k})
            rows = cur.fetchall()

        hits: List[SearchHit] = []
        for r in rows:
            meta = _safe_json(r.get("metadata"))
            meta.setdefault("page_start", r.get("page_start"))
            meta.setdefault("page_end", r.get("page_end"))
            meta.setdefault("table_key", r.get("table_key"))
            meta.setdefault("table_title", r.get("table_title"))

            hits.append(
                SearchHit(
                    source="table_dense",
                    doc_id=r["doc_id"],
                    document_id=r["document_id"],
                    #doc_url=self._get_hit_url(doc_id=r["doc_id"]),
                    chunk_index=int(r["chunk_index"]),
                    score=float(r["score"]) if r["score"] is not None else 0.0,
                    content=r.get("raw_table_md") or "",
                    metadata=meta,
                )
            )
        return hits
    
    def search_table_lexical_multi(
        self,
        *,
        tenant_id: str,
        doc_ids: List[str],
        query: str,
        k: int = 200,
    ) -> List[SearchHit]:
        if not doc_ids:
            return []

        sql = """
        WITH q AS (
        SELECT plainto_tsquery('simple', %(q)s) AS tsq
        )
        SELECT
        d.doc_id,
        tb.document_id::text AS document_id,
        tb.chunk_index,
        tb.page_start,
        tb.page_end,
        tb.table_key,
        tb.table_title,
        tb.raw_table_md,
        tb.metadata,
        ts_rank(tb.tsv, q.tsq) AS score
        FROM documents d
        JOIN table_chunks tb
        ON tb.document_id = d.document_id
        AND tb.version = d.active_version
        JOIN q ON TRUE
        WHERE d.tenant_id = %(tenant_id)s
        AND d.doc_id = ANY(%(doc_ids)s::text[])
        AND tb.tsv @@ q.tsq
        ORDER BY score DESC
        LIMIT %(k)s
        """

        with self._conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, {"tenant_id": tenant_id, "doc_ids": doc_ids, "q": query, "k": k})
            rows = cur.fetchall()

        hits: List[SearchHit] = []
        for r in rows:
            meta = _safe_json(r.get("metadata"))
            meta.setdefault("page_start", r.get("page_start"))
            meta.setdefault("page_end", r.get("page_end"))
            meta.setdefault("table_key", r.get("table_key"))
            meta.setdefault("table_title", r.get("table_title"))

            hits.append(
                SearchHit(
                    source="table_lex",
                    doc_id=r["doc_id"],
                    document_id=r["document_id"],
                    #doc_url=self._get_hit_url(doc_id=r["doc_id"]),
                    chunk_index=int(r["chunk_index"]),
                    score=float(r["score"]) if r["score"] is not None else 0.0,
                    content=r.get("raw_table_md") or "",
                    metadata=meta,
                )
            )
        return hits
    
    def search_image_text_dense_multi(
        self,
        *,
        tenant_id: str,
        doc_ids: List[str],
        qvec: List[float],
        k: int = 200,
    ) -> List[SearchHit]:
        if not doc_ids:
            return []

        qv = _to_pg_vector_literal(qvec)

        sql = """
        SELECT
        d.doc_id,
        im.document_id::text AS document_id,
        im.chunk_index,
        im.page_start,
        im.page_end,
        im.heading_path,
        im.image_path,
        im.image_alt,
        im.image_caption,
        im.metadata,
        (1 - (im.text_embedding <=> %(qvec)s::vector)) AS score
        FROM documents d
        JOIN image_chunks im
        ON im.document_id = d.document_id
        AND im.version = d.active_version
        WHERE d.tenant_id = %(tenant_id)s
        AND d.doc_id = ANY(%(doc_ids)s::text[])
        AND im.text_embedding IS NOT NULL
        ORDER BY im.text_embedding <=> %(qvec)s::vector
        LIMIT %(k)s
        """

        with self._conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                sql,
                {
                    "tenant_id": tenant_id,
                    "doc_ids": doc_ids,
                    "qvec": qv,
                    "k": k,
                },
            )
            rows = cur.fetchall()

        hits: List[SearchHit] = []
        for r in rows:
            meta = _safe_json(r.get("metadata"))
            meta.setdefault("page_start", r.get("page_start"))
            meta.setdefault("page_end", r.get("page_end"))
            meta.setdefault("heading_path", r.get("heading_path"))
            meta.setdefault("image_path", r.get("image_path"))
            meta.setdefault("image_alt", r.get("image_alt"))

            hits.append(
                SearchHit(
                    source="image_text",
                    doc_id=r["doc_id"],
                    document_id=r["document_id"],
                    #doc_url=self._get_hit_url(doc_id=r["doc_id"]),
                    chunk_index=int(r["chunk_index"]),
                    score=float(r["score"]) if r["score"] is not None else 0.0,
                    content=r.get("image_caption") or r.get("image_alt") or "",
                    metadata=meta,
                )
            )
        return hits
    
    # ---------------------------
    # Low-level searches (DETAIL)
    # ---------------------------
    def search_text_dense(self, *, tenant_id: str, doc_id: str, qvec: List[float], k: int = 30) -> List[SearchHit]:
        qv = _to_pg_vector_literal(qvec)
        sql = """
        SELECT
          d.doc_id,
          tc.document_id::text AS document_id,
          tc.chunk_index,
          tc.page_start,
          tc.page_end,
          tc.heading_path,
          tc.content,
          tc.metadata,
          (1 - (tc.embedding <=> %(qvec)s::vector)) AS score
        FROM documents d
        JOIN text_chunks tc
          ON tc.tenant_id = d.tenant_id
         AND tc.document_id = d.document_id
         AND tc.version = d.active_version
        WHERE d.tenant_id = %(tenant_id)s
          AND d.doc_id = %(doc_id)s
          AND tc.embedding IS NOT NULL
        ORDER BY tc.embedding <=> %(qvec)s::vector
        LIMIT %(k)s
        """
        with self._conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, {"tenant_id": tenant_id, "doc_id": doc_id, "qvec": qv, "k": k})
            rows = cur.fetchall()

        hits: List[SearchHit] = []
        for r in rows:
            meta = _safe_json(r["metadata"])
            # helpful standard fields for UI/citation
            meta.setdefault("page_start", r.get("page_start"))
            meta.setdefault("page_end", r.get("page_end"))
            meta.setdefault("heading_path", r.get("heading_path"))
            hits.append(
                SearchHit(
                    source="text",
                    doc_id=r["doc_id"],
                    document_id=r["document_id"],
                    #doc_url=self._get_hit_url(doc_id=r["doc_id"]),
                    chunk_index=int(r["chunk_index"]),
                    score=float(r["score"]) if r["score"] is not None else 0.0,
                    content=r["content"] or "",
                    metadata=meta,
                )
            )
        return hits

    def search_table_dense(self, *, tenant_id: str, doc_id: str, qvec: List[float], k: int = 30) -> List[SearchHit]:
        qv = _to_pg_vector_literal(qvec)
        sql = """
        SELECT
          d.doc_id,
          t.document_id::text AS document_id,
          t.chunk_index,
          t.page_start,
          t.page_end,
          t.raw_table_md AS content,
          t.metadata,
          (1 - (t.embedding <=> %(qvec)s::vector)) AS score
        FROM documents d
        JOIN table_chunks t
          ON t.tenant_id = d.tenant_id
         AND t.document_id = d.document_id
         AND t.version = d.active_version
        WHERE d.tenant_id = %(tenant_id)s
          AND d.doc_id = %(doc_id)s
          AND t.embedding IS NOT NULL
        ORDER BY t.embedding <=> %(qvec)s::vector
        LIMIT %(k)s
        """
        with self._conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, {"tenant_id": tenant_id, "doc_id": doc_id, "qvec": qv, "k": k})
            rows = cur.fetchall()

        hits: List[SearchHit] = []
        for r in rows:
            meta = _safe_json(r["metadata"])
            meta.setdefault("page_start", r.get("page_start"))
            meta.setdefault("page_end", r.get("page_end"))
            hits.append(
                SearchHit(
                    source="table_dense",
                    doc_id=r["doc_id"],
                    document_id=r["document_id"],
                    #doc_url=self._get_hit_url(doc_id=r["doc_id"]),
                    chunk_index=int(r["chunk_index"]),
                    score=float(r["score"]) if r["score"] is not None else 0.0,
                    content=r["content"] or "",
                    metadata=meta,
                )
            )
        return hits

    def search_table_lexical(self, *, tenant_id: str, doc_id: str, query: str, k: int = 30) -> List[SearchHit]:
        """
        Uses pg_trgm similarity on table_chunks.lexical_text.
        """
        sql = """
        SELECT
          d.doc_id,
          t.document_id::text AS document_id,
          t.chunk_index,
          t.page_start,
          t.page_end,
          t.raw_table_md AS content,
          t.metadata,
          similarity(t.lexical_text, %(q)s) AS score
        FROM documents d
        JOIN table_chunks t
          ON t.tenant_id = d.tenant_id
         AND t.document_id = d.document_id
         AND t.version = d.active_version
        WHERE d.tenant_id = %(tenant_id)s
          AND d.doc_id = %(doc_id)s
          AND t.lexical_text IS NOT NULL
        ORDER BY similarity(t.lexical_text, %(q)s) DESC
        LIMIT %(k)s
        """
        with self._conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, {"tenant_id": tenant_id, "doc_id": doc_id, "q": query, "k": k})
            rows = cur.fetchall()

        hits: List[SearchHit] = []
        for r in rows:
            s = float(r["score"]) if r["score"] is not None else 0.0
            if s <= 0:
                continue
            meta = _safe_json(r["metadata"])
            meta.setdefault("page_start", r.get("page_start"))
            meta.setdefault("page_end", r.get("page_end"))
            hits.append(
                SearchHit(
                    source="table_lex",
                    doc_id=r["doc_id"],
                    document_id=r["document_id"],
                    #doc_url=self._get_hit_url(doc_id=r["doc_id"]),
                    chunk_index=int(r["chunk_index"]),
                    score=s,
                    content=r["content"] or "",
                    metadata=meta,
                )
            )
        return hits

    def search_image_text_dense(self, *, tenant_id: str, doc_id: str, qvec: List[float], k: int = 10) -> List[SearchHit]:
        qv = _to_pg_vector_literal(qvec)
        sql = """
        SELECT
          d.doc_id,
          ic.document_id::text AS document_id,
          ic.chunk_index,
          ic.page_start,
          ic.page_end,
          COALESCE(ic.image_caption,'') || '\n' || COALESCE(ic.embed_text,'') AS content,
          ic.metadata,
          (1 - (ic.text_embedding <=> %(qvec)s::vector)) AS score
        FROM documents d
        JOIN image_chunks ic
          ON ic.tenant_id = d.tenant_id
         AND ic.document_id = d.document_id
         AND ic.version = d.active_version
        WHERE d.tenant_id = %(tenant_id)s
          AND d.doc_id = %(doc_id)s
          AND ic.text_embedding IS NOT NULL
        ORDER BY ic.text_embedding <=> %(qvec)s::vector
        LIMIT %(k)s
        """
        with self._conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, {"tenant_id": tenant_id, "doc_id": doc_id, "qvec": qv, "k": k})
            rows = cur.fetchall()

        hits: List[SearchHit] = []
        for r in rows:
            meta = _safe_json(r["metadata"])
            meta.setdefault("page_start", r.get("page_start"))
            meta.setdefault("page_end", r.get("page_end"))
            hits.append(
                SearchHit(
                    source="image_text",
                    doc_id=r["doc_id"],
                    document_id=r["document_id"],
                    #doc_url=self._get_hit_url(doc_id=r["doc_id"]),
                    chunk_index=int(r["chunk_index"]),
                    score=float(r["score"]) if r["score"] is not None else 0.0,
                    content=r["content"] or "",
                    metadata=meta,
                )
            )
        return hits

    # ---------------------------
    # Low-level searches (SUMMARY)
    # ---------------------------
    def search_summary_dense(self, *, tenant_id: str, doc_id: str, qvec: List[float], k: int = 5) -> List[SearchHit]:
        """
        One summary row per doc. We still return a "hit" for fusion/formatting.
        """
        qv = _to_pg_vector_literal(qvec)
        sql = """
        SELECT
          d.doc_id,
          s.document_id::text AS document_id,
          s.summary_text AS content,
          s.metadata,
          (1 - (s.embedding <=> %(qvec)s::vector)) AS score
        FROM documents d
        JOIN summary_chunks s
          ON s.tenant_id = d.tenant_id
         AND s.document_id = d.document_id
        WHERE d.tenant_id = %(tenant_id)s
          AND d.doc_id = %(doc_id)s
          AND s.embedding IS NOT NULL
        ORDER BY s.embedding <=> %(qvec)s::vector
        LIMIT %(k)s
        """
        with self._conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, {"tenant_id": tenant_id, "doc_id": doc_id, "qvec": qv, "k": k})
            rows = cur.fetchall()

        hits: List[SearchHit] = []
        for r in rows:
            hits.append(
                SearchHit(
                    source="summary",
                    doc_id=r["doc_id"],
                    document_id=r["document_id"],
                    #doc_url=self._get_hit_url(doc_id=r["doc_id"]),
                    chunk_index=0,
                    score=float(r["score"]) if r["score"] is not None else 0.0,
                    content=r["content"] or "",
                    metadata=_safe_json(r["metadata"]),
                )
            )
        return hits

    def search_summary_lexical(self, *, tenant_id: str, doc_id: str, query: str, k: int = 5) -> List[SearchHit]:
        """
        Summary lexical via ts_rank on to_tsvector('simple', searchable_text).
        """
        sql = """
        SELECT
          d.doc_id,
          s.document_id::text AS document_id,
          s.summary_text AS content,
          s.metadata,
          ts_rank(to_tsvector('simple', s.searchable_text), plainto_tsquery('simple', %(q)s)) AS score
        FROM documents d
        JOIN summary_chunks s
          ON s.tenant_id = d.tenant_id
         AND s.document_id = d.document_id
        WHERE d.tenant_id = %(tenant_id)s
          AND d.doc_id = %(doc_id)s
          AND plainto_tsquery('simple', %(q)s) @@ to_tsvector('simple', s.searchable_text)
        ORDER BY score DESC
        LIMIT %(k)s
        """
        with self._conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, {"tenant_id": tenant_id, "doc_id": doc_id, "q": query, "k": k})
            rows = cur.fetchall()

        hits: List[SearchHit] = []
        for r in rows:
            s = float(r["score"]) if r["score"] is not None else 0.0
            if s <= 0:
                continue
            hits.append(
                SearchHit(
                    source="summary_lex",
                    doc_id=r["doc_id"],
                    document_id=r["document_id"],
                    #doc_url=self._get_hit_url(doc_id=r["doc_id"]),
                    chunk_index=0,
                    score=s,
                    content=r["content"] or "",
                    metadata=_safe_json(r["metadata"]),
                )
            )
        return hits

    # ---------------------------
    # Fusion
    # ---------------------------
    def fuse(
        self,
        *,
        text_hits: List[SearchHit],
        table_dense_hits: List[SearchHit],
        table_lex_hits: List[SearchHit],
        image_hits: List[SearchHit],
        summary_hits: List[SearchHit],
        summary_lex_hits: List[SearchHit],
        weights: Optional[Dict[str, float]] = None,
        norm: str = "rrf",
    ) -> List[SearchHit]:
        w = {
            "text": 1.30,
            "table_dense": 1.00,
            "table_lex": 1.00,
            "image_text": 0.80,
            "summary": 1.00,
            "summary_lex": 1.10,
        }
        if weights:
            w.update(weights)

        # ---- helper: make sure raw score is preserved once ----
        def _ensure_raw(hits: List[SearchHit], raw_source: str) -> None:
            for h in hits:
                if h.metadata is None:
                    h.metadata = {}
                # 存一次就好：避免你多次 fuse 時覆蓋
                h.metadata.setdefault("raw_score", float(h.score) if h.score is not None else 0.0)
                h.metadata.setdefault("raw_source", raw_source)

        _ensure_raw(text_hits, "text")
        _ensure_raw(table_dense_hits, "table_dense")
        _ensure_raw(table_lex_hits, "table_lex")
        _ensure_raw(image_hits, "image_text")
        _ensure_raw(summary_hits, "summary")
        _ensure_raw(summary_lex_hits, "summary_lex")

        fused_pairs: List[Tuple[SearchHit, float, float, str]] = []
        # store: (hit, fused_score, weight_used, source_key)
        for h, s in _rank_norm(text_hits, method=norm):
            fused_pairs.append((h, w["text"] * s, w["text"], "text"))
        for h, s in _rank_norm(table_dense_hits, method=norm):
            fused_pairs.append((h, w["table_dense"] * s, w["table_dense"], "table_dense"))
        for h, s in _rank_norm(table_lex_hits, method=norm):
            fused_pairs.append((h, w["table_lex"] * s, w["table_lex"], "table_lex"))
        for h, s in _rank_norm(image_hits, method=norm):
            fused_pairs.append((h, w["image_text"] * s, w["image_text"], "image_text"))
        for h, s in _rank_norm(summary_hits, method=norm):
            fused_pairs.append((h, w["summary"] * s, w["summary"], "summary"))
        for h, s in _rank_norm(summary_lex_hits, method=norm):
            fused_pairs.append((h, w["summary_lex"] * s, w["summary_lex"], "summary_lex"))

        # adapt your dedupe util: it expects List[Tuple[SearchHit, float]]
        # so we temporarily drop extra fields, then restore them
        tmp_pairs: List[Tuple[SearchHit, float]] = [(h, fs) for (h, fs, _wt, _sk) in fused_pairs]
        tmp_pairs = _dedupe_by_key(tmp_pairs)
        tmp_pairs.sort(key=lambda x: x[1], reverse=True)

        # build lookup from hit identity -> (weight_used, source_key)
        # note: after dedupe, we still have the original SearchHit objects, so we can map by id(h)
        extra_by_id = {id(h): (wt, sk) for (h, _fs, wt, sk) in fused_pairs}

        out: List[SearchHit] = []
        for h, fused_s in tmp_pairs:
            wt, sk = extra_by_id.get(id(h), (None, None))

            meta = dict(h.metadata or {})
            meta["fused_score"] = float(fused_s)
            meta["fused_norm"] = norm
            if wt is not None:
                meta["fused_weight"] = float(wt)
            if sk is not None:
                meta["fused_from"] = sk

            out.append(
                SearchHit(
                    source=h.source,
                    doc_id=h.doc_id,
                    document_id=h.document_id,
                    chunk_index=h.chunk_index,
                    score=float(fused_s),
                    content=h.content,
                    metadata=meta,
                )
            )

        out = _dedupe_by_content(out)
        out.sort(key=lambda x: x.score, reverse=True)
        return out

    # ---------------------------
    # Public search APIs
    # ---------------------------
    def search(
        self,
        *,
        doc_id: str,
        query: str,
        access: AccessLevel = "detail",
        tenant_id: Optional[str] = None,
        top_k: int = 20,
        top_k_per_doc: int = 20,
        routing: bool = True,
        router_model: str = "qwen2:7b",
        enable_table_lex: bool = True,
        weights: Optional[Dict[str, float]] = None,
    ) -> List[SearchHit]:
        """
        Single-doc search respecting access level.
        """
        _t0 = time.perf_counter()
        t = tenant_id if tenant_id is not None else self.tenant_id
        if t is None:
            raise ValueError("tenant_id is required for search")
        logger.info(
            "[search:start] tenant_id=%s doc_id=%s access=%s top_k=%s top_k_per_doc=%s routing=%s enable_table_lex=%s query_len=%s",
            t,
            doc_id,
            access,
            top_k,
            top_k_per_doc,
            routing,
            enable_table_lex,
            len(query or ""),
        )

        try:
            qvec = self.embed_query(query)

            if access == "deny":
                logger.info(
                    "[search:end] tenant_id=%s doc_id=%s access=deny result_count=0 elapsed=%.2fs",
                    t,
                    doc_id,
                    time.perf_counter() - _t0,
                )
                return []

            if access == "summary":
                summary_hits = self.search_summary_dense(tenant_id=t, doc_id=doc_id, qvec=qvec, k=5)
                summary_lex_hits: List[SearchHit] = []
                try:
                    summary_lex_hits = self.search_summary_lexical(tenant_id=t, doc_id=doc_id, query=query, k=5)
                except Exception:
                    summary_lex_hits = []

                # routing profile for summary-only is optional; if enabled, use summary_only weights
                routed_weights = weights
                if routing and routed_weights is None:
                    routed_weights = ROUTING_PROFILES["summary_only"]

                fused = self.fuse(
                    text_hits=[],
                    table_dense_hits=[],
                    table_lex_hits=[],
                    image_hits=[],
                    summary_hits=summary_hits,
                    summary_lex_hits=summary_lex_hits,
                    weights=routed_weights,
                    norm="rrf",
                )
                out = fused[:top_k]
                logger.info(
                    "[search:summary] tenant_id=%s doc_id=%s summary_dense=%s summary_lex=%s result_count=%s elapsed=%.2fs",
                    t,
                    doc_id,
                    len(summary_hits),
                    len(summary_lex_hits),
                    len(out),
                    time.perf_counter() - _t0,
                )
                return out

            # access == "detail"
            text_hits = self.search_text_dense(tenant_id=t, doc_id=doc_id, qvec=qvec, k=top_k_per_doc)
            table_dense_hits = self.search_table_dense(tenant_id=t, doc_id=doc_id, qvec=qvec, k=top_k_per_doc)
            image_hits = self.search_image_text_dense(tenant_id=t, doc_id=doc_id, qvec=qvec, k=max(5, top_k_per_doc // 3))

            table_lex_hits: List[SearchHit] = []
            if enable_table_lex:
                try:
                    table_lex_hits = self.search_table_lexical(tenant_id=t, doc_id=doc_id, query=query, k=top_k_per_doc)
                except Exception:
                    table_lex_hits = []

            profile = "balanced"
            routed_weights = weights
            if routing and routed_weights is None:
                try:
                    profile = self.route_profile(query, router_model=router_model)
                    routed_weights = ROUTING_PROFILES.get(profile, ROUTING_PROFILES["balanced"])
                except Exception:
                    routed_weights = None

            fused = self.fuse(
                text_hits=text_hits,
                table_dense_hits=table_dense_hits,
                table_lex_hits=table_lex_hits,
                image_hits=image_hits,
                summary_hits=[],
                summary_lex_hits=[],
                weights=routed_weights,
                norm="rrf",
            )
            out = fused[:top_k]
            logger.info(
                "[search:end] tenant_id=%s doc_id=%s profile=%s text=%s table_dense=%s table_lex=%s image=%s result_count=%s elapsed=%.2fs",
                t,
                doc_id,
                profile,
                len(text_hits),
                len(table_dense_hits),
                len(table_lex_hits),
                len(image_hits),
                len(out),
                time.perf_counter() - _t0,
            )
            return out
        except Exception:
            logger.exception(
                "[search:error] tenant_id=%s doc_id=%s access=%s routing=%s",
                t,
                doc_id,
                access,
                routing,
            )
            raise
    
    def multi_search(
        self,
        *,
        doc_ids: List[str],
        query: str,
        access_map: Optional[Dict[str, AccessLevel]] = None,
        top_k_per_doc: int = 20,
        top_k: int = 20,
        routing: bool = True,
        router_model: str = "qwen3:8b",
        enable_table_lex: bool = True,
        weights: Optional[Dict[str, float]] = None,
        tenant_id: Optional[str] = None,
        rerank: bool = False,
        rerank_model: str = "qwen3:8b",
    ) -> Dict[str, Any]:
        """
        Cross-doc retrieval, ACL-aware via access_map.

        - access_map[doc_id] decides whether this doc is searched in detail or summary tier.
        - deny docs are ignored.
        """
        _t0 = time.perf_counter()
        logger.info(
            "[multi_search:start] doc_ids=%s top_k=%s top_k_per_doc=%s routing=%s rerank=%s enable_table_lex=%s query_len=%s",
            len(doc_ids or []),
            top_k,
            top_k_per_doc,
            routing,
            rerank,
            enable_table_lex,
            len(query or ""),
        )
        if not doc_ids:
            logger.error("[multi_search:error] doc_ids is empty")
            raise ValueError("doc_ids is empty")

        t = tenant_id if tenant_id is not None else self.tenant_id
        if t is None:
            raise ValueError("tenant_id is required for multi_search")
        access_map = access_map or {d: "detail" for d in doc_ids}

        # Deduplicate doc_ids while keeping order
        used = list(dict.fromkeys([d for d in doc_ids if d]))

        # Filter out deny early
        used = [d for d in used if access_map.get(d, "deny") != "deny"]
        logger.info(
            "[multi_search:acl] tenant_id=%s input_doc_ids=%s usable_doc_ids=%s",
            t,
            len(doc_ids),
            len(used),
        )
        if not used:
            logger.info(
                "[multi_search:end] tenant_id=%s usable_doc_ids=0 result_count=0 elapsed=%.2fs",
                t,
                time.perf_counter() - _t0,
            )
            return {
                "query": query,
                "doc_ids_input": doc_ids,
                "doc_ids_used": [],
                "hits": [],
                "note": "No doc_ids left after ACL filtering (deny).",
            }

        qvec = self.embed_query(query)

        # Route once (shared across docs) — but only meaningful for detail docs.
        profile = "balanced"
        routed_weights = weights
        if routing and routed_weights is None:
            try:
                profile = self.route_profile(query, router_model=router_model)
                routed_weights = ROUTING_PROFILES.get(profile, ROUTING_PROFILES["balanced"])
            except Exception:
                profile = "balanced"
                routed_weights = None  # fuse() will use defaults
        logger.info(
            "[multi_search:routing] tenant_id=%s profile=%s routing=%s router_model=%s routing_weights=%s",
            t,
            profile,
            routing,
            router_model,
            routed_weights,
        )

        all_hits: List[SearchHit] = []

        # --- summary tier: per-doc summary_chunks search ---
        summary_ids = [d for d in used if access_map.get(d, "deny") == "summary"]
        for did in summary_ids:
            s_dense = self.search_summary_dense(tenant_id=t, doc_id=did, qvec=qvec, k=5)
            s_lex: List[SearchHit] = []
            try:
                s_lex = self.search_summary_lexical(tenant_id=t, doc_id=did, query=query, k=5)
            except Exception:
                s_lex = []

            fused = self.fuse(
                text_hits=[],
                table_dense_hits=[],
                table_lex_hits=[],
                image_hits=[],
                summary_hits=s_dense,
                summary_lex_hits=s_lex,
                weights=ROUTING_PROFILES["summary_only"],
                norm="rrf",
            )
            all_hits.extend(fused[: max(3, top_k_per_doc // 4)])
        if summary_ids:
            logger.info(
                "[multi_search:summary] tenant_id=%s summary_ids=%s hits=%s",
                t, len(summary_ids), len(all_hits),
            )

        # --- detail tier: Stage1 routing (text_lex FTS) -> Stage2 dense multi-doc ---
        detail_ids = [d for d in used if access_map.get(d, "deny") == "detail"]
        if detail_ids:
            # Stage 1: routing (pick candidate docs)
            # 你可以先用一個保守值：TopN=100；資料大了再調小
            routed_detail_ids = self.route_docs_text_lexical(
                tenant_id=t,
                doc_ids=detail_ids,
                query=query,
                n=min(100, len(detail_ids)),
            )

            # fallback：如果 FTS 命中太少，避免 routed_detail_ids 變空
            if not routed_detail_ids:
                routed_detail_ids = detail_ids
            logger.info(
                "[multi_search:detail] tenant_id=%s detail_ids=%s routed_detail_ids=%s",
                t,
                len(detail_ids),
                len(routed_detail_ids),
            )

            # Stage 2: dense multi-doc (RAW scores)
            # 建議多取一些候選，再由 fuse + top_k 截斷
            k_text  = max(200, top_k * 20)
            k_table = max(200, top_k * 20)
            k_img   = max(200, top_k * 20)
            
            text_hits = self.search_text_dense_multi(
                tenant_id=t,
                doc_ids=routed_detail_ids,
                qvec=qvec,
                k=k_text,
            )

            table_dense_hits = self.search_table_dense_multi(
                tenant_id=t,
                doc_ids=routed_detail_ids,
                qvec=qvec,
                k=k_table,
            )

            table_lex_hits: List[SearchHit] = []
            if enable_table_lex:
                table_lex_hits = self.search_table_lexical_multi(
                    tenant_id=t,
                    doc_ids=routed_detail_ids,
                    query=query,
                    k=k_table,
                )

            image_hits = self.search_image_text_dense_multi(
                tenant_id=t,
                doc_ids=routed_detail_ids,
                qvec=qvec,
                k=k_img,
            )
            logger.info(
                "[multi_search:hits] tenant_id=%s text=%s table_dense=%s table_lex=%s image=%s",
                t,
                len(text_hits),
                len(table_dense_hits),
                len(table_lex_hits),
                len(image_hits),
            )
            
            # Mark RAW scores and sources before fusion
            def _mark_raw(hits: list[SearchHit], source: str):
                for h in hits:
                    h.metadata.setdefault("raw_score", h.score)
                    h.metadata.setdefault("raw_source", source)
                    
            _mark_raw(text_hits, "text_dense")
            _mark_raw(table_dense_hits, "table_dense")
            _mark_raw(table_lex_hits, "table_lex")
            _mark_raw(image_hits, "image_text")

            fused = self.fuse(
                text_hits=text_hits,
                table_dense_hits=table_dense_hits,
                table_lex_hits=table_lex_hits,
                image_hits=image_hits,
                summary_hits=[],
                summary_lex_hits=[],
                weights=routed_weights,
                norm="rrf",
            )
            all_hits.extend(fused[:top_k_per_doc])
            logger.info(
                "[multi_search:fused] tenant_id=%s fused_candidates=%s kept_per_doc=%s",
                t,
                len(fused),
                min(len(fused), top_k_per_doc),
            )

        all_hits.sort(key=lambda h: h.score, reverse=True)
        #print(all_hits[:top_k])  # debug
        if rerank:
            candidate_k = min(len(all_hits), max(top_k * 3, 30))
            candidates = all_hits[:candidate_k]
            reranked = self.rerank_hits_http(
                query=query,
                hits=candidates,
                reranker_model=rerank_model,
            )
        else:
            reranked = all_hits

        logger.info(
            "[multi_search:end] tenant_id=%s used_doc_ids=%s all_hits=%s rerank_hits=%s returned_top_k=%s elapsed=%.2fs",
            t,
            len(used),
            len(all_hits),
            len(reranked),
            min(len(all_hits), top_k),
            time.perf_counter() - _t0,
        )
        
        return {
            "query": query,
            "doc_ids_input": doc_ids,
            "doc_ids_used": used,
            "routing": {
                "enabled": routing,
                "router_model": router_model,
                "profile": profile,
                "weights": routed_weights,
            },
            "hits": all_hits[:top_k],
            "rerank_hits": reranked[:top_k],
            "access": {d: access_map.get(d, "deny") for d in used},
        }
        
    # ---------------------------
    # Context formatting helper
    # ---------------------------
    @staticmethod
    def format_context(hits: List[SearchHit], max_chars_per_hit: int = 1800) -> str:
        """
        Format a list of SearchHit objects into a string with doc_id and chunk_index information.
        [1] doc_id=..., source=..., chunk_index=..., content=...
        [2] doc_id=..., source=..., chunk_index=..., content=...
        ... etc.
        """
        parts: List[str] = []
        print(hits)
        for i, h in enumerate(hits, start=1):
            meta = h.metadata or {}
            #page_start = meta.get("page_start")
            #page_end = meta.get("page_end")
            #pages = ""
            #if page_start is not None or page_end is not None:
            #    pages = f" pages={page_start}-{page_end}"
            source = meta.get("source_path", "unknown").split("/")[-1]
            hit_url = h.doc_url or ""
            header = f"[{i}] doc_id={h.doc_id}, 文件名稱={source}, chunk_index={h.chunk_index}, url={hit_url}"
            content = (h.content or "").strip()
            if len(content) > max_chars_per_hit:
                content = content[: max_chars_per_hit].rstrip() + "\n…(truncated)…"
            parts.append(header + "\ncontent:" + content)
        return "\n\n".join(parts)


