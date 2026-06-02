from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from docblock_core.marker_runner import run_marker
from docblock_core.config import settings

app = FastAPI(title="Marker Service")


# ── Simple REST schema ────────────────────────────────────────────

class ConvertRequest(BaseModel):
    pdf_path: str
    doc_id: str
    out_dir: str
    job_id: Optional[str] = None


class ConvertResponse(BaseModel):
    md_path: str
    doc_id: str
    elapsed: float


# ── Simple REST endpoint ──────────────────────────────────────────

@app.post("/v1/convert", response_model=ConvertResponse)
def convert(req: ConvertRequest):
    job_id = req.job_id or f"direct-{int(time.time())}"
    t0 = time.perf_counter()
    try:
        md_path = run_marker(
            job_id=job_id,
            doc_id=req.doc_id,
            pdf_path=req.pdf_path,
            out_dir=req.out_dir,
            marker_cmd=settings.tools.marker_cmd,
            timeout=settings.tools.marker_timeout,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return ConvertResponse(md_path=md_path, doc_id=req.doc_id, elapsed=time.perf_counter() - t0)


# ── OpenAI-compatible endpoint (for LiteLLM routing) ─────────────
#
# message content format (JSON string):
#   { "pdf_path": "...", "doc_id": "...", "out_dir": "...", "job_id": "..." }
#
# response: choices[0].message.content = md_path

@app.post("/v1/chat/completions")
def chat_completions(body: Dict[str, Any]):
    messages: List[Dict] = body.get("messages", [])
    if not messages:
        raise HTTPException(status_code=400, detail="messages is required")

    raw = messages[-1].get("content", "")
    try:
        params = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(status_code=400, detail="message content must be a JSON string")

    pdf_path: str = params.get("pdf_path", "")
    out_dir: str = params.get("out_dir", "")
    doc_id: str = params.get("doc_id") or Path(pdf_path).stem
    job_id: str = params.get("job_id") or f"litellm-{int(time.time())}"

    if not pdf_path or not out_dir:
        raise HTTPException(status_code=400, detail="pdf_path and out_dir are required")

    t0 = time.perf_counter()
    try:
        md_path = run_marker(
            job_id=job_id,
            doc_id=doc_id,
            pdf_path=pdf_path,
            out_dir=out_dir,
            marker_cmd=settings.tools.marker_cmd,
            timeout=settings.tools.marker_timeout,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    created = int(t0)
    return {
        "id": f"marker-{created}",
        "object": "chat.completion",
        "created": created,
        "model": body.get("model", "marker-pdf-to-md"),
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": md_path},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@app.get("/healthz")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8766, reload=False)
