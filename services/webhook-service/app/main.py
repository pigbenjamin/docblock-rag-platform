from fastapi import FastAPI
import uvicorn

from app.config import settings
from app.keycloak.client import KeycloakClient
from app.keycloak.user_sync_service import UserSyncService
from app.keycloak.webhook_router import WebhookRouter
from app.db.user_repository import UserRepository


def create_app() -> FastAPI:
    app = FastAPI(title="Docblock Webhook Service - Keycloak User Sync")

    if not settings.keycloak.CLIENT_SECRET:
        raise RuntimeError("KEYCLOAK_CLIENT_SECRET is not set")
    if not settings.keycloak.WEBHOOK_SECRET:
        raise RuntimeError("WEBHOOK_SECRET is not set")

    keycloak_client = KeycloakClient(
        keycloak_url=settings.keycloak.KEYCLOAK_URL,
        realm=settings.keycloak.KEYCLOAK_REALM,
        client_id=settings.keycloak.CLIENT_ID,
        client_secret=settings.keycloak.CLIENT_SECRET,
        verify=settings.keycloak.VERIFY,
    )

    user_repository = UserRepository(
        pg_dsn=settings.db.pg_dsn,
        tenant_id=settings.db.tenant_id,
    )

    user_sync_service = UserSyncService(
        keycloak_client=keycloak_client,
        user_repository=user_repository,
    )

    keycloak_router = WebhookRouter(
        user_sync_service=user_sync_service,
        webhook_secret=settings.keycloak.WEBHOOK_SECRET,
    )

    app.include_router(keycloak_router.router, prefix="/keycloak")

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    return app


app = create_app()

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8763, reload=True)
