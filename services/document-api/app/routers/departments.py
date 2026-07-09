from __future__ import annotations

from typing import Any, Dict, List

import httpx
from fastapi import APIRouter, HTTPException

from app.config import settings

router = APIRouter(prefix="/departments", tags=["departments"])


async def _get_admin_token() -> str:
    async with httpx.AsyncClient(verify=settings.keycloak.verify) as client:
        resp = await client.post(
            f"{settings.keycloak.url}/realms/{settings.keycloak.realm}/protocol/openid-connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": settings.keycloak.client_id,
                "client_secret": settings.keycloak.client_secret,
            },
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


@router.get("")
async def list_departments() -> List[Dict[str, Any]]:
    """
    Top-level Keycloak groups (A/B/C/...) = departments, for a frontend
    dropdown. Read-only and purely informational - it does not participate
    in any authorization decision, which always queries `user_principal`
    instead. Reuses the same `user-sync-service` Keycloak client
    webhook-service already has credentials for.
    """
    token = await _get_admin_token()
    async with httpx.AsyncClient(verify=settings.keycloak.verify) as client:
        resp = await client.get(
            f"{settings.keycloak.url}/admin/realms/{settings.keycloak.realm}/groups",
            headers={"Authorization": f"Bearer {token}"},
            params={"briefRepresentation": "true"},
        )
        if resp.status_code == 403:
            raise HTTPException(
                status_code=502,
                detail=(
                    "Keycloak client 'user-sync-service' lacks permission to list "
                    "groups; grant it a role covering group listing (e.g. view-users) "
                    "in the Keycloak admin console"
                ),
            )
        resp.raise_for_status()

    groups = resp.json()
    return [{"id": g.get("id"), "name": g.get("name")} for g in groups]
