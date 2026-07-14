from __future__ import annotations

import asyncio
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
    Top-level Keycloak groups that have a 'KM' subgroup, for a frontend
    dropdown (D2). A group only counts as a department if it structurally
    looks like one (has a KM child group) - this deliberately doesn't
    hardcode department names, so it also filters out top-level groups like
    'Public' (a shared root folder open to every department, not itself a
    department - see the node-tree migration). Read-only and purely
    informational: no authorization decision ever consults this list, only
    `user_principal` / `acl_entries`. Reuses the same `user-sync-service`
    Keycloak client webhook-service already has credentials for.
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
        groups = [g for g in resp.json() if g.get("id")]

        async def _has_km_subgroup(group_id: str) -> bool:
            children_resp = await client.get(
                f"{settings.keycloak.url}/admin/realms/{settings.keycloak.realm}/groups/{group_id}/children",
                headers={"Authorization": f"Bearer {token}"},
                params={"briefRepresentation": "true"},
            )
            children_resp.raise_for_status()
            return any(c.get("name") == "KM" for c in children_resp.json())

        has_km = await asyncio.gather(*[_has_km_subgroup(g["id"]) for g in groups])

    return [
        {"id": g["id"], "name": g.get("name")}
        for g, km in zip(groups, has_km)
        if km
    ]
