import json
import time


class UserSyncService:
    def __init__(self, keycloak_client, user_repository=None):
        self.keycloak_client = keycloak_client
        self.user_repository = user_repository

    async def sync_user(
        self,
        user_id: str,
        write_file: bool = False,
        write_db: bool = False,
    ):
        user = await self.keycloak_client.fetch_user(user_id)
        
        if write_file:
            with open(f"user_{user_id}.json", "w", encoding="utf-8") as f:
                json.dump(user, f, ensure_ascii=False, indent=4)

        if write_db:
            if self.user_repository is None:
                raise RuntimeError("UserRepository is not configured")

            principals = self._build_principals(user)

            self.user_repository.sync_user_principals(
                user_id=user["id"],
                principals=principals,
            )

        return user
    
    def _build_principals(self, user: dict) -> list[dict]:
        principals = []

        user_id = user["id"]

        principals.append({
            "principal_type": "user",
            "principal_id": f"user:{user_id}",
        })

        for group in user.get("raw_groups", []):
            path = group.get("path", "").strip("/")
            parts = path.split("/")

            if len(parts) >= 1 and parts[0]:
                dept = parts[0]

                principals.append({
                    "principal_type": "department",
                    # NOTE: no "dept:" prefix here - document_acl stores the raw
                    # department value (see ACLService.write_access._parse_principal),
                    # and fetch_doc_access_map joins on principal_id equality.
                    "principal_id": dept,
                })

            if len(parts) >= 2 and parts[0] and parts[1]:
                dept = parts[0]
                role = parts[1]

                principals.append({
                    "principal_type": "role",
                    "principal_id": f"dept:{dept}:role:{role}",
                })

        # 去重
        seen = set()
        unique = []

        for p in principals:
            key = (p["principal_type"], p["principal_id"])
            if key not in seen:
                seen.add(key)
                unique.append(p)

        return unique