"""
title: Docblock RAG Pipeline
author: kuanmingchen
version: 0.3.0
description: RAG pipeline for Docblock — resolves OpenWebUI user to Keycloak sub (in-memory cache), fetches relevant chunks from retrieve-api, injects context for LLM streaming.
requirements: requests
"""

from typing import List, Union, Generator, Iterator
import os
import requests
from pydantic import BaseModel


# Module-level cache: openwebui_id → keycloak_sub
# Static identity mapping — never changes, persists for lifetime of process
_USER_CACHE: dict[str, str] = {}

# Messages short enough or generic enough to skip RAG entirely
_SKIP_PHRASES = {
    "hi", "hello", "hey", "thanks", "thank you",
    "謝謝", "好的", "ok", "okay", "good", "nice",
}


class Pipeline:

    class Valves(BaseModel):
        RETRIEVE_API_URL: str = "http://10.90.20.55:31761"
        OPENWEBUI_URL: str = "http://10.90.20.55:5000"
        OPENWEBUI_ADMIN_KEY: str = ""
        TOP_K: int = 10
        CONTEXT_TURNS: int = 2
        DEBUG: bool = False

    def __init__(self):
        self.name = "Docblock RAG Pipeline"
        self.valves = self.Valves(
            **{
                "RETRIEVE_API_URL": os.getenv("RETRIEVE_API_URL", "http://10.90.20.55:31761"),
                "OPENWEBUI_URL": os.getenv("OPENWEBUI_URL", "http://10.90.20.55:5000"),
                "OPENWEBUI_ADMIN_KEY": os.getenv("OPENWEBUI_ADMIN_KEY", ""),
                "TOP_K": int(os.getenv("RAG_TOP_K", 10)),
                "CONTEXT_TURNS": int(os.getenv("RAG_CONTEXT_TURNS", 2)),
                "DEBUG": os.getenv("RAG_DEBUG", "false").lower() == "true",
            }
        )

    async def on_startup(self):
        print(f"[Docblock RAG] started — retrieve-api={self.valves.RETRIEVE_API_URL}")

    async def on_shutdown(self):
        print("[Docblock RAG] stopped")

    # ------------------------------------------------------------------
    # User identity resolution
    # ------------------------------------------------------------------

    def _get_keycloak_sub(self, openwebui_id: str) -> str:
        """
        Resolve OpenWebUI UUID → Keycloak sub.
        In-memory cache (module-level dict); on miss calls OpenWebUI admin API once per user.
        """
        if openwebui_id in _USER_CACHE:
            if self.valves.DEBUG:
                print(f"[Docblock RAG] cache hit {openwebui_id} → {_USER_CACHE[openwebui_id]}")
            return _USER_CACHE[openwebui_id]

        resp = requests.get(
            f"{self.valves.OPENWEBUI_URL}/api/v1/users/{openwebui_id}",
            headers={"Authorization": f"Bearer {self.valves.OPENWEBUI_ADMIN_KEY}"},
            timeout=5,
        )
        resp.raise_for_status()

        # Response structure: {"oauth": {"oidc": {"sub": "<keycloak-uuid>"}}}
        user_data = resp.json()
        keycloak_sub = (
            (user_data.get("oauth") or {})
            .get("oidc", {})
            .get("sub")
        ) or openwebui_id

        _USER_CACHE[openwebui_id] = keycloak_sub

        if self.valves.DEBUG:
            print(f"[Docblock RAG] resolved {openwebui_id} → {keycloak_sub}")

        return keycloak_sub

    # ------------------------------------------------------------------
    # RAG helpers
    # ------------------------------------------------------------------

    def _should_search(self, message: str) -> bool:
        """Return False for greetings and very short messages that don't need RAG."""
        stripped = message.strip().lower()
        return stripped not in _SKIP_PHRASES and len(stripped.split()) > 2

    def _build_search_query(self, user_message: str, messages: List[dict]) -> str:
        """
        Build search query using recent user turns for multi-turn context awareness.
        e.g. follow-up "那第 5 頁呢?" is meaningless without the previous question.
        """
        recent_user = [
            m["content"] for m in messages[-(self.valves.CONTEXT_TURNS * 2):]
            if m.get("role") == "user"
        ]
        parts = recent_user[-(self.valves.CONTEXT_TURNS):]
        return " ".join(parts) if parts else user_message

    def _format_context(self, hits: list) -> str:
        """Format search hits as a context block with inline source citations."""
        sections = []
        for i, h in enumerate(hits, start=1):
            source = h.get("source", "unknown")
            page_start = h.get("page_start")
            page_end = h.get("page_end")
            content = (h.get("content") or "").strip()

            if page_start is not None:
                page = f"第 {page_start}"
                if page_end and page_end != page_start:
                    page += f"–{page_end}"
                page += " 頁"
                label = f"[來源 {i}: {source}, {page}]"
            else:
                label = f"[來源 {i}: {source}]"

            sections.append(f"{label}\n{content}")

        return "\n\n---\n\n".join(sections)

    # ------------------------------------------------------------------
    # Pipeline entry point
    # ------------------------------------------------------------------

    def pipe(
        self,
        user_message: str,
        model_id: str,
        messages: List[dict],
        body: dict,
    ) -> Union[str, dict, Generator, Iterator]:
        del model_id  # required by OpenWebUI pipeline interface

        user_info = body.get("user", {})
        openwebui_id = user_info.get("id", "")

        # 1. Resolve Keycloak sub (in-memory cache, one API call per new user)
        try:
            keycloak_sub = self._get_keycloak_sub(openwebui_id)
        except Exception as e:
            print(f"[Docblock RAG] user resolve error: {e}")
            keycloak_sub = openwebui_id  # fallback — ACL will likely deny

        if self.valves.DEBUG:
            print(f"[Docblock RAG] openwebui={openwebui_id} keycloak={keycloak_sub}")

        # --- TEST MODE: 確認 keycloak_sub 正確後再接 RAG ---
        return (
            f"**[Pipeline Test — RAG not yet connected]**\n\n"
            f"### User 解析結果\n"
            f"- `openwebui_id`: `{openwebui_id}`\n"
            f"- `keycloak_sub`: `{keycloak_sub}`\n"
            f"- `email`: `{user_info.get('email', '')}`\n\n"
            f"### Valve 設定\n"
            f"- `retrieve_api`: `{self.valves.RETRIEVE_API_URL}`\n"
            f"- `top_k`: `{self.valves.TOP_K}`\n"
            f"- `context_turns`: `{self.valves.CONTEXT_TURNS}`\n"
            f"- `conversation_turns`: `{len(messages)}`\n\n"
            f"**Your message:**\n> {user_message}\n\n"
            f"---\n"
            f"確認 `keycloak_sub` 正確後，移除 TEST MODE 區塊即可接入 RAG。"
        )

        # --- RAG（TEST MODE 通過後取消以下註解）---
        # # 2. Skip RAG for greetings / trivial messages
        # if not self._should_search(user_message):
        #     return body
        #
        # # 3. Build multi-turn-aware search query
        # search_query = self._build_search_query(user_message, messages)
        #
        # # 4. Call retrieve-api /v1/search
        # try:
        #     resp = requests.post(
        #         f"{self.valves.RETRIEVE_API_URL}/v1/search",
        #         json={"query": search_query, "user_id": keycloak_sub, "top_k": self.valves.TOP_K},
        #         timeout=30,
        #     )
        #     resp.raise_for_status()
        #     hits = resp.json().get("hits", [])
        # except Exception as e:
        #     print(f"[Docblock RAG] search error: {e}")
        #     hits = []
        #
        # # 5. No hits — let LLM answer normally
        # if not hits:
        #     return body
        #
        # # 6. Inject RAG context, OpenWebUI handles LLM + streaming
        # context = self._format_context(hits)
        # rag_system = (
        #     "你是一個知識庫問答助理。請根據以下文件內容回答問題。\n"
        #     "若文件內容不足以回答，請明確說明無法從提供的文件中找到答案，不要猜測。\n"
        #     "回答結尾請條列使用的來源（來源編號及文件名稱）。\n\n"
        #     f"文件內容：\n\n{context}"
        # )
        # body["messages"] = [{"role": "system", "content": rag_system}] + [
        #     m for m in messages if m.get("role") != "system"
        # ]
        # return body