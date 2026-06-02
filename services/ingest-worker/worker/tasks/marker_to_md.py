from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

from docblock_core.config import settings


def run_marker_to_md(job_id: str, doc_id: str, pdf_path: str, out_dir: str) -> str:
    """Call marker-service via LiteLLM proxy to convert PDF → Markdown.

    Returns the output .md file path produced by marker-service.
    """
    proxy_url = settings.tools.litellm_proxy_url
    payload = {
        "model": "marker/pdf-to-md",
        "messages": [{
            "role": "user",
            "content": json.dumps({
                "pdf_path": pdf_path,
                "doc_id": doc_id,
                "out_dir": out_dir,
                "job_id": job_id,
            }),
        }],
    }

    # marker can take up to 30 min; give the HTTP client a matching timeout
    timeout = settings.tools.marker_timeout + 60

    with httpx.Client(timeout=timeout) as client:
        resp = client.post(
            f"{proxy_url}/v1/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {settings.tools.litellm_api_key}"},
        )
        resp.raise_for_status()

    md_path: str = resp.json()["choices"][0]["message"]["content"]
    return md_path
