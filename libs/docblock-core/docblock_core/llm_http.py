# llm_http.py
"""Shared helpers for OpenAI-compatible (LiteLLM) HTTP calls."""
from __future__ import annotations

from typing import Dict

from docblock_core.config import settings


def litellm_headers() -> Dict[str, str]:
    """Authorization header for direct LiteLLM access; empty dict when no key is configured."""
    key = (settings.tools.litellm_api_key or "").strip()
    if key:
        return {"Authorization": f"Bearer {key}"}
    return {}
