from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from docblock_core.config import settings
from docblock_core.authz import NodeAuthz, list_document_ids
from docblock_core.search import DocblockSearchClient, SearchHit
from docblock_core.rag import RagClient

router = APIRouter(tags=["search"])

_node_authz = NodeAuthz(pg_dsn=settings.db.pg_dsn, tenant_id=settings.db.tenant_id)


def _sc(request: Request) -> DocblockSearchClient:
    return request.app.state.search_client


def _rag(request: Request) -> RagClient:
    return request.app.state.rag_client


def _hit_to_dict(h: SearchHit, rank: int, preview_chars: int = 400) -> Dict[str, Any]:
    meta = h.metadata or {}
    content = (h.content or "").strip()
    preview = content[:preview_chars].rstrip() + ("…" if len(content) > preview_chars else "")
    return {
        "rank": rank,
        "document_id": h.document_id,
        "doc_url": getattr(h, "doc_url", None),
        "source": h.source,
        "score": float(h.score),
        "chunk_index": int(h.chunk_index),
        "page_start": meta.get("page_start"),
        "page_end": meta.get("page_end"),
        "heading_path": meta.get("heading_path") or meta.get("heading") or meta.get("section_path"),
        "preview": preview,
        "content": content,
        "metadata": meta,
    }


class SearchRequest(BaseModel):
    query: str
    user_id: str
    document_ids: Optional[List[str]] = None
    top_k: int = 10
    top_k_per_doc: int = 20
    routing: bool = True
    router_model: str = settings.models.chat_model
    enable_table_lex: bool = True
    preview_chars: int = 400
    max_docs: int = 5000


class AnswerRequest(BaseModel):
    document_id: str
    question: str
    user_id: str
    top_k: int = 10
    routing: bool = True


@router.post("/search")
def search(req: SearchRequest, request: Request) -> Dict[str, Any]:
    """ACL-enforced cross-document semantic search (query permission, allow/deny)."""
    tenant_id = settings.db.tenant_id

    candidates = list_document_ids(
        pg_dsn=settings.db.pg_dsn,
        tenant_id=tenant_id,
        candidate_document_ids=req.document_ids,
        limit=req.max_docs,
    )
    allowed = _node_authz.filter_allowed(user_id=req.user_id, action="query", node_ids=candidates)
    if not allowed:
        return {
            "query": req.query,
            "user_id": req.user_id,
            "document_ids_used": [],
            "hits": [],
            "note": "No documents available after ACL filtering.",
        }

    res = _sc(request).multi_search(
        document_ids=allowed,
        query=req.query,
        top_k=req.top_k,
        top_k_per_doc=req.top_k_per_doc,
        routing=req.routing,
        router_model=req.router_model,
        enable_table_lex=req.enable_table_lex,
        tenant_id=tenant_id,
    )

    hits = res.get("hits", []) or []
    return {
        "query": req.query,
        "user_id": req.user_id,
        "document_ids_used": res.get("document_ids_used", allowed),
        "routing": res.get("routing", {}),
        "hits": [_hit_to_dict(h, i + 1, req.preview_chars) for i, h in enumerate(hits)],
    }


@router.post("/answer")
def answer(req: AnswerRequest, request: Request) -> Dict[str, Any]:
    """ACL-enforced single-document RAG answer with citations (query permission)."""
    allowed = _node_authz.evaluate_one(user_id=req.user_id, action="query", node_id=req.document_id)
    if allowed is None:
        raise HTTPException(status_code=404, detail=f"document_id='{req.document_id}' not found")
    if not allowed:
        raise HTTPException(
            status_code=403,
            detail=f"ACL_DENY: user '{req.user_id}' has no query access to document_id='{req.document_id}'",
        )

    result = _rag(request).generate(
        document_id=req.document_id,
        question=req.question,
        top_k=req.top_k,
        enable_table_lex=True,
        routing=req.routing,
    )

    citations = [
        {
            "index": i + 1,
            "document_id": getattr(h, "document_id", req.document_id),
            "source": h.source,
            "chunk_index": h.chunk_index,
            "page_start": (h.metadata or {}).get("page_start"),
            "page_end": (h.metadata or {}).get("page_end"),
        }
        for i, h in enumerate(result.hits)
    ]

    return {
        "answer": result.answer,
        "hits": [_hit_to_dict(h, i + 1) for i, h in enumerate(result.hits)],
        "context": result.context,
        "citations": citations,
        "model": result.model,
        "user_id": req.user_id,
        "document_id": req.document_id,
    }
