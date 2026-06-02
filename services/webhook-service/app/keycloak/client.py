import httpx


class KeycloakClient:
    def __init__(self, keycloak_url, realm, client_id, client_secret, verify=False):
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

    async def fetch_user(self, user_id: str) -> dict:
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