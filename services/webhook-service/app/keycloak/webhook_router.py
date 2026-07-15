from fastapi import APIRouter, Header, HTTPException


class WebhookRouter:
    def __init__(self, user_sync_service, webhook_secret: str):
        self.user_sync_service = user_sync_service
        self.webhook_secret = webhook_secret
        self.router = APIRouter()

        self.router.add_api_route(
            #"/keycloak/user-sync",
            "/user-sync",
            self.sync_user,
            methods=["POST"],
        )
        self.router.add_api_route(
            "/full-sync",
            self.full_sync,
            methods=["POST"],
        )

    async def sync_user(self, payload: dict, x_webhook_secret: str = Header(None)):
        if x_webhook_secret != self.webhook_secret:
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

        user_id = payload.get("user_id")
        if not user_id:
            raise HTTPException(status_code=400, detail="Missing user_id")

        try:
            await self.user_sync_service.sync_user(
                user_id=user_id,
                write_file=False,
                write_db=True,
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

        return {
            "status": "synced",
            "user_id": user_id,
        }

    async def full_sync(self, x_webhook_secret: str = Header(None)):
        if x_webhook_secret != self.webhook_secret:
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

        try:
            summary = await self.user_sync_service.full_sync()
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

        return {"status": "completed", **summary}