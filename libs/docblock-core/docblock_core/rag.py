# core/rag.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Any

import requests

from docblock_core.config import settings
from docblock_core.search import DocblockSearchClient, SearchHit


@dataclass
class RagAnswer:
    answer: str
    hits: List[SearchHit]
    context: str
    model: str
    usage: Optional[Dict[str, Any]] = None
    routing: Optional[Dict[str, Any]] = None


DEFAULT_SYSTEM_PROMPT = """\
You are a factual assistant. Use ONLY the provided context to answer.
- If the context does not contain enough information, say you don't know.
- Do NOT invent citations. Do NOT use outside knowledge.
- Keep the answer concise and structured.
"""
#- Always cite sources using bracket numbers like [1], [2] that correspond to the context items.

def build_user_prompt(question: str, context: str) -> str:
    return f"""\
Question:
{question}

Context:
{context}

Instructions:
- Answer the question using ONLY the context.
- Include citations like [1], [2] after the relevant sentences.
- If not enough evidence is present in the context, answer: "I don't know based on the provided context."
"""


class RagClient:
    """
    RAG generation client:
      - retrieval via DocblockSearchClient
      - generation via Ollama /api/chat
    """

    def __init__(
        self,
        *,
        pg_dsn: Optional[str] = None,
        ollama_base_url: Optional[str] = None,
        litellm_base_url: Optional[str] = None,
        embed_model: Optional[str] = None,
        chat_model: Optional[str] = None,
        chat_timeout: int = 300,
    ) -> None:
        self.search_client = DocblockSearchClient(
            pg_dsn=pg_dsn or settings.db.pg_dsn,
            litellm_base_url=litellm_base_url or settings.models.litellm_base_url,
            embed_model=embed_model or settings.models.embed_model,
        )
        self.litellm_base_url = (litellm_base_url or settings.models.litellm_base_url).rstrip("/")
        self.chat_model = chat_model or settings.models.chat_model
        self.chat_timeout = chat_timeout

    def generate(
        self,
        *,
        doc_id: str,
        question: str,
        top_k: int = 10,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        weights: Optional[Dict[str, float]] = None,
        enable_table_lex: bool = True,
        max_chars_per_hit: int = 1800,
        routing: bool = True,
        router_model: str = "qwen3.5-9b",
    ) -> RagAnswer:
        # 1) Retrieve
        hits = self.search_client.search(
            doc_id=doc_id,
            query=question,
            top_k=top_k,
            enable_table_lex=enable_table_lex,
            weights=weights,
            routing=routing,
            router_model=router_model,
        )

        # 2) Build context (with [1],[2]...)
        context = self.search_client.format_context(hits, max_chars_per_hit=max_chars_per_hit)
        user_prompt = build_user_prompt(question, context)

        # 3) Generate
        url = f"{self.litellm_base_url}/v1/chat/completions"
        payload = {
            "model": self.chat_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
        }
        r = requests.post(url, json=payload, timeout=self.chat_timeout)
        r.raise_for_status()
        data = r.json()

        msg = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        usage = data.get("usage")

        return RagAnswer(
            answer=msg.strip(),
            hits=hits,
            context=context,
            model=self.chat_model,
            routing={
                "enabled": routing,
                "router_model": router_model,
            },
            usage=usage,
        )
