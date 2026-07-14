import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from app.routers.departments import router as departments_router
from app.routers.documents import router as documents_router
from app.routers.nodes import router as nodes_router


def create_app() -> FastAPI:
    app = FastAPI(title="Docblock Document API")

    # 前端網域未定案前留空，等同禁止所有瀏覽器跨網域存取；不影響 server-to-server 呼叫
    allowed_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(documents_router, prefix="/v1")
    app.include_router(departments_router, prefix="/v1")
    app.include_router(nodes_router, prefix="/v1")

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    return app


app = create_app()

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8765, reload=True)
