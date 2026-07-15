import httpx


class KeycloakClient:
    def __init__(self, keycloak_url, realm, client_id, client_secret, verify=True):
        self.keycloak_url = keycloak_url.rstrip("/")
        self.realm = realm
        self.client_id = client_id
        self.client_secret = client_secret
        self.verify = verify

    async def get_token(self) -> str:
        async with httpx.AsyncClient(verify=self.verify) as client:
            resp = await client.post(
                f"{self.keycloak_url}/realms/{self.realm}/protocol/openid-connect/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
            )
            resp.raise_for_status()
            return resp.json()["access_token"]

    async def list_all_user_ids(self, page_size: int = 100) -> list[str]:
        """Page through the realm's users via the admin API, returning every
        user id. Used by full-sync to reconcile users that were imported (or
        edited) before the event listener existed / while it was down -
        the event listener only fires on REGISTER/UPDATE_PROFILE/admin CRUD,
        so anything provisioned outside those paths (e.g. an AD import that
        doesn't touch each user individually) never reaches us otherwise."""
        token = await self.get_token()
        headers = {"Authorization": f"Bearer {token}"}
        ids: list[str] = []
        first = 0

        async with httpx.AsyncClient(verify=self.verify, timeout=30) as client:
            while True:
                resp = await client.get(
                    f"{self.keycloak_url}/admin/realms/{self.realm}/users",
                    headers=headers,
                    params={"briefRepresentation": "true", "first": first, "max": page_size},
                )
                resp.raise_for_status()
                page = resp.json()
                ids.extend(u["id"] for u in page if u.get("id"))
                if len(page) < page_size:
                    break
                first += page_size

        return ids

    async def fetch_user(self, user_id: str, token: str | None = None) -> dict:
        if token is None:
            token = await self.get_token()
        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient(verify=self.verify) as client:
            user_resp = await client.get(
                f"{self.keycloak_url}/admin/realms/{self.realm}/users/{user_id}",
                headers=headers,
            )
            user_resp.raise_for_status()

            group_resp = await client.get(
                f"{self.keycloak_url}/admin/realms/{self.realm}/users/{user_id}/groups",
                headers=headers,
            )
            group_resp.raise_for_status()

        user = user_resp.json()
        groups = group_resp.json()

        departments = []
        roles = []

        for g in groups:
            path = g.get("path", "").strip("/")
            parts = path.split("/")

            if len(parts) >= 1:
                dept = parts[0]
                if dept and dept not in departments:
                    departments.append(dept)

            if len(parts) >= 2:
                role = parts[1]
                if role and role not in roles:
                    roles.append(role)

        return {
            "id": user["id"],
            "username": user.get("username"),
            "email": user.get("email"),
            "first_name": user.get("firstName"),
            "last_name": user.get("lastName"),
            "enabled": user.get("enabled"),
            "departments": departments,
            "roles": roles,
            "raw": user,
            "raw_groups": groups,
        }