from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from docblock_core.config import settings
from docblock_core.acl import fetch_doc_access_for_user
from docblock_core.search import DocblockSearchClient, SearchHit
from docblock_core.rag import RagClient

router = APIRouter(tags=["search"])


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
    document_ids: Optional[List[str]] = None
    top_k: int = 10
    routing: bool = True


@router.post("/search")
def search(req: SearchRequest, request: Request) -> Dict[str, Any]:
    """ACL-enforced cross-document semantic search."""
    tenant_id = settings.db.tenant_id

    access_map, principals = fetch_doc_access_for_user(
        pg_dsn=settings.db.pg_dsn,
        tenant_id=tenant_id,
        user_id=req.user_id,
        candidate_document_ids=req.document_ids,
        limit=req.max_docs,
    )

    allowed = [d for d, a in access_map.items() if a in ("detail", "summary")]
    if not allowed:
        return {
            "query": req.query,
            "user": {"user_id": req.user_id, "principals": principals},
            "document_ids_used": [],
            "access": dict(access_map),
            "hits": [],
            "note": "No documents available after ACL filtering.",
        }

    res = _sc(request).multi_search(
        document_ids=allowed,
        access_map=access_map,
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
        "user": {"user_id": req.user_id, "principals": principals},
        "document_ids_used": res.get("document_ids_used", allowed),
        "access": dict(access_map),
        "routing": res.get("routing", {}),
        "hits": [_hit_to_dict(h, i + 1, req.preview_chars) for i, h in enumerate(hits)],
    }


@router.post("/answer")
def answer(req: AnswerRequest, request: Request) -> Dict[str, Any]:
    """ACL-enforced single-document RAG answer with citations."""
    tenant_id = settings.db.tenant_id

    access_map, principals = fetch_doc_access_for_user(
        pg_dsn=settings.db.pg_dsn,
        tenant_id=tenant_id,
        user_id=req.user_id,
        candidate_document_ids=req.document_ids,
        limit=5000,
    )

    access = access_map.get(req.document_id, "deny")
    if access == "deny":
        raise HTTPException(
            status_code=403,
            detail=f"ACL_DENY: user '{req.user_id}' has no access to document_id='{req.document_id}'",
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
        "user": {"user_id": req.user_id, "principals": principals},
        "document_id": req.document_id,
        "document_id_access": access,
    }
