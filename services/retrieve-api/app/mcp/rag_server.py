from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

import requests
from fastmcp import FastMCP

from docblock_core.rag import RagClient
from docblock_core.search import DocblockSearchClient, SearchHit
from docblock_core.acl import fetch_doc_access_for_user
from docblock_core.config import settings

mcp = FastMCP("docblock-rag")

_rag: Optional[RagClient] = None
_search_client: Optional[DocblockSearchClient] = None


def _get_rag() -> RagClient:
    global _rag
    if _rag is None:
        _rag = RagClient()
    return _rag


def _get_search_client() -> DocblockSearchClient:
    global _search_client
    if _search_client is None:
        _search_client = DocblockSearchClient()
    return _search_client


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


def _ollama_chat(messages: List[Dict[str, str]], *, model: str, timeout: int = 120) -> str:
    url = f"{settings.models.litellm_base_url.rstrip('/')}/v1/chat/completions"
    payload = {"model": model, "messages": messages, "stream": False}
    r = requests.post(url, json=payload, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()


# ---------------------------
# RAG answer and search tools
# ---------------------------
@mcp.tool(
    name="rag_answer",
    description="Answer a question using document-specific RAG with citations (ACL enforced: detail/summary/deny).",
)
def rag_answer(
    document_id: str,
    question: str,
    user_id: str,
    document_ids: Optional[List[str]] = None,  # optional candidate scope
    top_k: int = 10,
    routing: bool = True,
) -> Dict[str, Any]:
    """
    - If user has DETAIL for doc: use full RagClient (text/table/image).
    - If user has SUMMARY for doc: use only summary_chunks as context.
    - If DENY: return ACL_DENY.
    """
    tenant_id = settings.db.tenant_id

    access_map, principals = fetch_doc_access_for_user(
        pg_dsn=settings.db.pg_dsn,
        tenant_id=tenant_id,
        user_id=user_id,
        candidate_document_ids=document_ids,
        limit=5000,
    )

    access = access_map.get(document_id, "deny")
    if access == "deny":
        return {
            "answer": "",
            "citations": [],
            "model": getattr(settings.models, "chat_model", None),
            "error": "ACL_DENY",
            "message": f"user '{user_id}' is not allowed to access document_id='{document_id}'",
            "user": {"user_id": user_id, "principals": principals},
            "document_id_requested": document_id,
            "document_id_access": access,
        }

    if access == "summary":
        # summary-only retrieval
        hits = _get_search_client().search(
            document_id=document_id,
            query=question,
            access="summary",
            tenant_id=tenant_id,
            top_k=max(3, min(top_k, 8)),
            routing=False,  # summary only
        )
        context = _get_search_client().format_context(hits, max_chars_per_hit=2200)

        # Chat model name: prefer settings.models.chat_model, else fall back to embed_model (not ideal but works)
        chat_model = getattr(settings.models, "chat_model", None) or getattr(settings.models, "gen_model", None) or settings.models.embed_model

        system = (
            "You are a helpful assistant.\n"
            "Answer the question using ONLY the provided context.\n"
            "If the context is insufficient, say you don't know based on the provided context."
        )
        user = f"Question:\n{question}\n\nContext:\n{context}\n"

        answer = _ollama_chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            model=str(chat_model),
            timeout=getattr(settings.models, "chat_timeout", 180),
        )

        citations: List[Dict[str, Any]] = []
        for i, h in enumerate(hits, start=1):
            citations.append(
                {
                    "index": i,
                    "document_id": h.document_id,
                    "source": h.source,
                    "chunk_index": h.chunk_index,
                    "page_start": (h.metadata or {}).get("page_start"),
                    "page_end": (h.metadata or {}).get("page_end"),
                }
            )

        return {
            "answer": answer,
            "citations": citations,
            "model": str(chat_model),
            "user": {"user_id": user_id, "principals": principals},
            "document_id_requested": document_id,
            "document_id_access": access,
            "routing": {"enabled": False, "note": "summary-only"},
        }

    # access == detail
    result = _get_rag().generate(
        document_id=document_id,
        question=question,
        top_k=top_k,
        enable_table_lex=True,
        routing=routing,
    )

    citations: List[Dict[str, Any]] = []
    for i, h in enumerate(result.hits, start=1):
        meta = h.metadata or {}
        citations.append(
            {
                "index": i,
                "document_id": getattr(h, "document_id", document_id),
                "source": h.source,
                "chunk_index": h.chunk_index,
                "page_start": meta.get("page_start"),
                "page_end": meta.get("page_end"),
            }
        )

    return {
        "answer": result.answer,
        "citations": citations,
        "model": result.model,
        "user": {"user_id": user_id, "principals": principals},
        "document_id_requested": document_id,
        "document_id_access": access,
        "routing": {"enabled": routing},
    }


# ---------------------------
# RAG search tools
# ---------------------------
@mcp.tool(
    name="rag_search",
    description="Cross-doc retrieval for RAG. Applies ACL automatically (detail/summary/deny).",
)
def rag_search(
    query: str,
    user_id: str,
    document_ids: Optional[List[str]] = None,  # optional dataset filter
    top_k: int = 10,
    top_k_per_doc: int = 20,
    routing: bool = True,
    router_model: str = settings.models.chat_model,
    enable_table_lex: bool = True,
    preview_chars: int = 400,
    max_docs: int = 5000,
) -> Dict[str, Any]:
    tenant_id = settings.db.tenant_id

    access_map, principals = fetch_doc_access_for_user(
        pg_dsn=settings.db.pg_dsn,
        tenant_id=tenant_id,
        user_id=user_id,
        candidate_document_ids=document_ids,
        limit=max_docs,
    )

    # Keep only detail/summary
    allowed = [d for d, a in access_map.items() if a in ("detail", "summary")]
    if not allowed:
        return {
            "query": query,
            "user": {"user_id": user_id, "principals": principals},
            "document_ids_input": document_ids or [],
            "document_ids_used": [],
            "hits": [],
            "note": "No documents available after ACL filtering.",
        }

    res = _get_search_client().multi_search(
        document_ids=allowed,
        access_map=access_map,
        query=query,
        top_k=top_k,
        top_k_per_doc=top_k_per_doc,
        routing=routing,
        router_model=router_model,
        enable_table_lex=enable_table_lex,
        tenant_id=tenant_id,
    )

    hits = res.get("hits", []) or []
    hits_out = [_hit_to_dict(h, i + 1, preview_chars=preview_chars) for i, h in enumerate(hits)]

    return {
        "query": query,
        "user": {"user_id": user_id, "principals": principals},
        "document_ids_input": document_ids or [],
        "document_ids_used": res.get("document_ids_used", allowed),
        "access": res.get("access", {}),
        "routing": res.get("routing", {"enabled": routing, "router_model": router_model}),
        "hits": hits_out,
    }

# 檢查模型答案生成的正確性
@mcp.tool(
    name="rag_gen_check",
    description="Check the correctness of RAG generated answer based on retrieved hits.",
)
def rag_gen_check(
    question: str,
    answer: str,
    #hits: List[Dict[str, Any]],
    hits: str,
    model: str = settings.models.chat_model,
    timeout: int = 120,
) -> str:
    system = (
        "You are a helpful assistant. Check the correctness of the provided answer based on the retrieved hits.\n"
        "Answer with 'CORRECT' if the answer is fully supported by the hits, otherwise answer 'INCORRECT'.\n"
        "Do not hallucinate or make assumptions beyond the provided hits."
    )
    user = f"Question:\n{question}\n\nAnswer:\n{answer}\n\nRetrieved Hits:\n{hits}\n"

    check_result = _ollama_chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        model=model,
        timeout=timeout,
    )

    #return check_result.strip().upper()
    return check_result.strip()

if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8761)
