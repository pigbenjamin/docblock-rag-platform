from __future__ import annotations

from contextlib import asynccontextmanager

import psycopg2
import requests
import uvicorn
from fastapi import FastAPI
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
                f"{settings.models.ollama_base_url.rstrip('/')}/api/tags",
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
