from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

import requests
from fastmcp import FastMCP

from docblock_core.rag import RagClient
from docblock_core.search import DocblockSearchClient, SearchHit
from docblock_core.authz import NodeAuthz, list_document_ids
from docblock_core.config import settings

mcp = FastMCP("docblock-rag")

_rag: Optional[RagClient] = None
_search_client: Optional[DocblockSearchClient] = None
_node_authz = NodeAuthz(pg_dsn=settings.db.pg_dsn, tenant_id=settings.db.tenant_id)


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
    description="Answer a question using document-specific RAG with citations (ACL enforced: query permission).",
)
def rag_answer(
    document_id: str,
    question: str,
    user_id: str,
    top_k: int = 10,
    routing: bool = True,
) -> Dict[str, Any]:
    allowed = _node_authz.evaluate_one(user_id=user_id, action="query", node_id=document_id)
    if not allowed:
        return {
            "answer": "",
            "citations": [],
            "model": getattr(settings.models, "chat_model", None),
            "error": "ACL_NOT_FOUND" if allowed is None else "ACL_DENY",
            "message": f"user '{user_id}' is not allowed to access document_id='{document_id}'",
            "user_id": user_id,
            "document_id_requested": document_id,
        }

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
        "user_id": user_id,
        "document_id_requested": document_id,
        "routing": {"enabled": routing},
    }


# ---------------------------
# RAG search tools
# ---------------------------
@mcp.tool(
    name="rag_search",
    description="Cross-doc retrieval for RAG. Applies ACL automatically (query permission, allow/deny).",
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

    candidates = list_document_ids(
        pg_dsn=settings.db.pg_dsn,
        tenant_id=tenant_id,
        candidate_document_ids=document_ids,
        limit=max_docs,
    )
    allowed = _node_authz.filter_allowed(user_id=user_id, action="query", node_ids=candidates)
    if not allowed:
        return {
            "query": query,
            "user_id": user_id,
            "document_ids_input": document_ids or [],
            "document_ids_used": [],
            "hits": [],
            "note": "No documents available after ACL filtering.",
        }

    res = _get_search_client().multi_search(
        document_ids=allowed,
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
        "user_id": user_id,
        "document_ids_input": document_ids or [],
        "document_ids_used": res.get("document_ids_used", allowed),
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
