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

            # 部門會隨 HR 連動動態出現：確保每個部門有根資料夾（含部門成員
            # browse/query/read entries），且 Public 根對它 allow（D2）。
            # 只增不減——部門消失時的清理是破壞性操作，留給管理員手動處理。
            departments = sorted({
                p["principal_id"] for p in principals
                if p["principal_type"] == "department" and p["principal_id"] != "Public"
            })
            self.user_repository.ensure_department_infrastructure(departments)

        return user

    def _build_principals(self, user: dict) -> list[dict]:
        principals = []

        user_id = user["id"]

        principals.append({
            "principal_type": "user",
            "principal_id": f"user:{user_id}",
        })

        # FB-6 (D8/D10/D11): department = top-level group only. No role
        # principals anymore - the Keycloak tree is HR-synced and multi-level,
        # so path segment [1] is an org sub-unit (处/课), not a KM role;
        # admin rosters live in the department_admins table instead.
        for group in user.get("raw_groups", []):
            path = group.get("path", "").strip("/")
            parts = path.split("/")

            if len(parts) >= 1 and parts[0]:
                dept = parts[0]

                principals.append({
                    "principal_type": "department",
                    # NOTE: no "dept:" prefix - acl_entries stores the raw
                    # department value and NodeAuthz compares on equality.
                    "principal_id": dept,
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