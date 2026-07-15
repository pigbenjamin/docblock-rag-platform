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
            self._write_db(user)

        return user

    def _write_db(self, user: dict) -> None:
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

    async def full_sync(self) -> dict:
        """Reconcile every user in the realm against user_principal.

        The event listener only fires on REGISTER/UPDATE_PROFILE/admin CRUD
        on a user - anything provisioned another way (bulk AD import, a
        realm restore, or simply an event lost while this service was down)
        never reaches sync_user otherwise. This walks the full user list via
        the admin API and re-syncs each one directly against our DB, without
        going through the Keycloak event listener or the other webhook
        fan-out target - it's a reconciliation pass, not a Keycloak event.

        One failure doesn't abort the run; per-user errors are collected and
        returned so the caller (a CronJob) can alert without losing the rest
        of the batch.
        """
        if self.user_repository is None:
            raise RuntimeError("UserRepository is not configured")

        user_ids = await self.keycloak_client.list_all_user_ids()
        token = await self.keycloak_client.get_token()

        synced = 0
        failures: list[dict] = []
        for user_id in user_ids:
            try:
                user = await self.keycloak_client.fetch_user(user_id, token=token)
                self._write_db(user)
                synced += 1
            except Exception as e:
                failures.append({"user_id": user_id, "error": str(e)})

        return {
            "total": len(user_ids),
            "synced": synced,
            "failed_count": len(failures),
            # capped so a bad run doesn't blow up the response body
            "failures": failures[:20],
        }

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