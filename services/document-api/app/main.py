from fastapi import FastAPI
import uvicorn

from app.routers.acl import router as acl_router
from app.routers.departments import router as departments_router
from app.routers.documents import router as documents_router


def create_app() -> FastAPI:
    app = FastAPI(title="Docblock Document API")

    app.include_router(documents_router, prefix="/v1")
    app.include_router(acl_router, prefix="/v1")
    app.include_router(departments_router, prefix="/v1")

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    return app


app = create_app()

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8765, reload=True)
