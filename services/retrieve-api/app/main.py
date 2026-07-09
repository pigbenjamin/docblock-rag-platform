from __future__ import annotations

import os
from contextlib import asynccontextmanager

import psycopg2
import requests
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from docblock_core.config import settings
from docblock_core.search import DocblockSearchClient
from docblock_core.rag import RagClient

from app.routers.search import router as search_router
from app.mcp.rag_server import mcp


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.search_client = DocblockSearchClient()
    app.state.rag_client = RagClient()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Docblock Retrieve API", lifespan=lifespan)

    # 前端網域未定案前留空，等同禁止所有瀏覽器跨網域存取；不影響 server-to-server 呼叫
    allowed_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(search_router, prefix="/v1")

    @app.get("/healthz", tags=["ops"])
    def health():
        """Liveness probe — process is alive."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["ops"])
    def ready():
        """Readiness probe — DB and Ollama are reachable."""
        errors: dict = {}

        try:
            with psycopg2.connect(settings.db.pg_dsn, connect_timeout=3) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
        except Exception as e:
            errors["db"] = str(e)

        try:
            r = requests.get(
                f"{settings.models.litellm_base_url.rstrip('/')}/health",
                timeout=3,
            )
            r.raise_for_status()
        except Exception as e:
            errors["ollama"] = str(e)

        if errors:
            return JSONResponse(status_code=503, content={"status": "not_ready", **errors})
        return {"status": "ready"}

    app.mount("/mcp", mcp.http_app())

    return app


app = create_app()

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8761, reload=True)
