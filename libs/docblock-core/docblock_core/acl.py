# core/acl.py
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple, Literal

import psycopg2
import psycopg2.errors
from psycopg2 import sql
from uuid import UUID

from docblock_core import sql_utils



Principal = Tuple[str, str]  # (principal_type, principal_id)
AccessLevel = Literal["detail", "summary", "deny"]


def _values_table_for_principals(principals: Sequence[Principal]) -> sql.Composed:
    parts = [sql.SQL("(%s,%s,%s)") for _ in principals]
    return sql.SQL(",").join(parts)


def _principal_priority(ptype: str) -> int:
    if ptype == "user":
        return 30
    if ptype == "role":
        return 20
    if ptype == "department":
        return 10
    return 0


def _normalize_effect(effect: str) -> AccessLevel:
    e = (effect or "").lower()
    if e == "deny":
        return "deny"
    if e in ("detail", "allow"):
        return "detail"
    if e == "summary":
        return "summary"
    return "deny"


def split_document_ids_by_access(access_map: Dict[str, AccessLevel]) -> Tuple[List[str], List[str]]:
    detail = [d for d, a in access_map.items() if a == "detail"]
    summary = [d for d, a in access_map.items() if a == "summary"]
    return detail, summary


def is_uuid(value: str) -> bool:
    try:
        UUID(value)
        return True
    except ValueError:
        return False


class ACLService:
    def __init__(self, *, pg_dsn: str, tenant_id: str):
        if not pg_dsn:
            raise ValueError("pg_dsn is required")
        if not tenant_id:
            raise ValueError("tenant_id is required")

        self.pg_dsn = pg_dsn
        self.tenant_id = tenant_id

    def fetch_user_principals(self, user_id: str) -> List[Principal]:
        q = """
        SELECT principal_type, principal_id
        FROM user_principal
        WHERE tenant_id = %s AND user_id = %s
        ORDER BY principal_type, principal_id
        """

        with psycopg2.connect(self.pg_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(q, (self.tenant_id, user_id))
                rows = cur.fetchall()

        principals = [(str(r[0]), str(r[1])) for r in rows]

        if ("user", user_id) not in principals:
            principals.insert(0, ("user", user_id))

        seen = set()
        out: List[Principal] = []
        for p in principals:
            if p not in seen:
                seen.add(p)
                out.append(p)

        return out

    def fetch_doc_access_map(
        self,
        *,
        principals: Sequence[Principal],
        candidate_document_ids: Optional[Sequence[str]] = None,
        limit: int = 10000,
    ) -> Dict[str, AccessLevel]:
        if not principals:
            return {}

        principals_values = _values_table_for_principals(principals)

        where_candidates = sql.SQL("")
        params: List[object] = []

        for ptype, pid in principals:
            params.extend([ptype, pid, _principal_priority(ptype)])

        # tenant_id must come before cand to match SQL: WHERE d.tenant_id = %s {where_candidates}
        params.append(self.tenant_id)

        if candidate_document_ids is not None:
            cand = [d for d in candidate_document_ids if d]
            if not cand:
                return {}
            where_candidates = sql.SQL("AND d.document_id = ANY(%s::uuid[])")
            params.append(cand)

        params.extend([limit, self.tenant_id])

        q = sql.SQL(
            """
WITH principals(principal_type, principal_id, priority) AS (
  VALUES {principals_values}
),
candidates AS (
  SELECT d.document_id
  FROM documents d
  WHERE d.tenant_id = %s
  {where_candidates}
  ORDER BY d.document_id
  LIMIT %s
),
matches AS (
  SELECT
    c.document_id,
    a.effect,
    p.priority,
    CASE
      WHEN lower(a.effect) = 'deny' THEN 30
      WHEN lower(a.effect) IN ('detail','allow') THEN 20
      WHEN lower(a.effect) = 'summary' THEN 10
      ELSE 0
    END AS effect_rank
  FROM candidates c
  JOIN document_acl a
    ON a.tenant_id = %s
   AND a.document_id = c.document_id
  JOIN principals p
    ON p.principal_type = a.principal_type
   AND p.principal_id = a.principal_id
),
best AS (
  SELECT DISTINCT ON (m.document_id)
    m.document_id,
    m.effect
  FROM matches m
  ORDER BY m.document_id, m.priority DESC, m.effect_rank DESC
)
SELECT c.document_id, COALESCE(b.effect, 'deny') AS effect
FROM candidates c
LEFT JOIN best b ON b.document_id = c.document_id
ORDER BY c.document_id
"""
        ).format(
            principals_values=principals_values,
            where_candidates=where_candidates,
        )

        with psycopg2.connect(self.pg_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(q, params)
                rows = cur.fetchall()

        return {
            str(document_id): _normalize_effect(str(effect))
            for document_id, effect in rows
        }

    def fetch_doc_access_for_user(
        self,
        *,
        user_id: str,
        candidate_document_ids: Optional[Sequence[str]] = None,
        limit: int = 10000,
    ) -> Tuple[Dict[str, AccessLevel], List[Principal]]:
        principals = self.fetch_user_principals(user_id)

        access_map = self.fetch_doc_access_map(
            principals=principals,
            candidate_document_ids=candidate_document_ids,
            limit=limit,
        )

        return access_map, principals

    def write_access(
        self,
        *,
        document_id: str,
        access_map: Dict[str, str],
    ) -> Dict[str, Any]:
        """Write ACL rows for a document using `sql_utils` helpers and return result.

        Returns a dict: {
        'success': bool,
        'results': [ { 'principal': (ptype, pid), 'effect': eff, 'deleted': int, 'inserted': bool, ... } ],
        'errors': [ { 'principal': (ptype, pid), 'error': str } ]
        }
        """
        def _parse_principal(key) -> Principal:
            if isinstance(key, (list, tuple)) and len(key) == 2:
                return str(key[0]), str(key[1])

            if isinstance(key, str):
                # Only split on explicit type separators; exclude '-' and '.' to
                # avoid mangling UUID-format user_ids.
                for sep in (":", "/", "|", ","):
                    if sep in key:
                        a, b = key.split(sep, 1)
                        return a.strip(), b.strip()

                return "user", key

            raise ValueError(f"Unsupported principal key: {key!r}")

        conn = psycopg2.connect(self.pg_dsn)
        results: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []
        overall_success = True

        try:
            db_document_id = str(document_id)

            if not is_uuid(db_document_id):
                return {
                    "success": False,
                    "results": [],
                    "errors": [
                        {
                            "principal": None,
                            "error": f"document_id must be a UUID: {document_id!r}",
                        }
                    ],
                }

            with conn, conn.cursor() as cur:
                if not sql_utils.document_exists(cur, self.tenant_id, db_document_id):
                    return {
                        "success": False,
                        "results": [],
                        "errors": [
                            {
                                "principal": None,
                                "error": f"document not found (document_id): {document_id}",
                            }
                        ],
                    }

            # Process each principal
            _VALID_EFFECTS = {"detail", "summary", "deny"}

            for principal_key, effect in access_map.items():
                ptype, pid = _parse_principal(principal_key)
                eff = (effect or "").strip().lower()
                if eff == "allow":
                    eff = "detail"  # backward-compat alias

                if not eff:
                    overall_success = False
                    errors.append({
                        "principal": (ptype, pid),
                        "error": "effect is empty",
                    })
                    continue

                if eff not in _VALID_EFFECTS:
                    overall_success = False
                    errors.append({
                        "principal": (ptype, pid),
                        "error": f"invalid effect '{eff}': must be one of {sorted(_VALID_EFFECTS)}",
                    })
                    continue

                entry: Dict[str, Any] = {
                    "principal": (ptype, pid),
                    "effect": eff,
                }
                try:
                    with conn, conn.cursor() as cur:
                        upserted = sql_utils.upsert_document_acl(
                            cur,
                            self.tenant_id,
                            db_document_id,
                            ptype,
                            pid,
                            eff,
                        )
                        entry["upserted"] = bool(upserted)
                        results.append(entry)
                except psycopg2.errors.ForeignKeyViolation:
                    # document_id exists in our check but FK failed (race or tenant mismatch)
                    overall_success = False
                    errors.append({
                        "principal": (ptype, pid),
                        "error": f"document_id '{db_document_id}' does not satisfy FK constraint",
                    })
                except Exception as e:
                    overall_success = False
                    errors.append({
                        "principal": (ptype, pid),
                        "error": str(e),
                    })

            return {
                "success": overall_success,
                "results": results,
                "errors": errors,
            }

        finally:
            conn.close()
    
    def delete_access(
        self,
        *,
        document_id: str,
        principal: Principal,
    ) -> Dict[str, Any]:
        """Delete an ACL row for a document and principal.

        Returns a dict: {
            'success': bool,
            'found_document': bool,   # False when doc itself was not found
            'deleted': int,           # number of rows deleted (0 or 1)
            'reason': str,
        }
        """
        ptype, pid = principal
        db_document_id = str(document_id)

        if not is_uuid(db_document_id):
            return {
                "success": False,
                "found_document": False,
                "deleted": 0,
                "reason": f"document_id must be a UUID: {document_id!r}",
            }

        conn = psycopg2.connect(self.pg_dsn)
        try:
            with conn, conn.cursor() as cur:
                if not sql_utils.document_exists(cur, self.tenant_id, db_document_id):
                    return {
                        "success": False,
                        "found_document": False,
                        "deleted": 0,
                        "reason": f"document not found (document_id): {document_id}",
                    }

                deleted = sql_utils.delete_document_acl(
                    cur,
                    self.tenant_id,
                    db_document_id,
                    ptype,
                    pid,
                )

                return {
                    "success": True,
                    "found_document": True,
                    "deleted": deleted,
                    "reason": "ok" if deleted > 0 else "no matching ACL row found",
                }

        except Exception as e:
            return {
                "success": False,
                "found_document": None,
                "deleted": 0,
                "reason": str(e),
            }

        finally:
            conn.close()


def fetch_doc_access_for_user(
    *,
    pg_dsn: str,
    tenant_id: str,
    user_id: str,
    candidate_document_ids: Optional[Sequence[str]] = None,
    limit: int = 10000,
) -> Tuple[Dict[str, AccessLevel], List[Principal]]:
    """Module-level convenience wrapper around ACLService.fetch_doc_access_for_user.

    Returns (access_map, principals). access_map is keyed by document_id (UUID string).
    """
    svc = ACLService(pg_dsn=pg_dsn, tenant_id=tenant_id)
    return svc.fetch_doc_access_for_user(
        user_id=user_id,
        candidate_document_ids=candidate_document_ids,
        limit=limit,
    )


# 使用方式
"""
acl = ACLService(
    pg_dsn=settings.db.pg_dsn,
    tenant_id=settings.db.tenant_id,
)

access_map, principals = acl.fetch_doc_access_for_user(
    user_id="001",
    candidate_document_ids=["fdf8c0ed-0f19-42ae-8d58-c04969610365"],
)

detail_ids, summary_ids = split_document_ids_by_access(access_map)
"""

# 寫入文件權限
# 目前只有對principal_type為"department"及"user"限制, "role"不限制
"""
acl = ACLService(
    pg_dsn=settings.db.pg_dsn,
    tenant_id=settings.db.tenant_id,
)
result = acl.write_access(
    document_id="fdf8c0ed-0f19-42ae-8d58-c04969610365",
    access_map={
        ("department", "A"): "detail",
        ("department", "B"): "summary",
        ("user", "47f097e7-9bea-4cbe-8f87-e5c3ecd887ae"): "detail",
    },
)
"""

# 刪除文件權限
"""
acl = ACLService(
    pg_dsn=settings.db.pg_dsn,
    tenant_id=settings.db.tenant_id,
)
success = acl.delete_access(
    document_id="fdf8c0ed-0f19-42ae-8d58-c04969610365",
    principal=("user", "47f097e7-9bea-4cbe-8f87-e5c3ecd887ae"),
)
"""

# access_map 格式規則
# key 是 tuple，包含 principal_type 和 principal_id
# value 是 AccessLevel，表示訪問級別，類型為 str，例如 "detail", "summary"或"deny"

# sql查詢方式
## 查 Robin 的 principals：
"""
SELECT principal_type, principal_id
FROM user_principal
WHERE tenant_id = 'tenant_001'
  AND user_id = '001';
"""

## 查可見文件
"""
SELECT DISTINCT document_id, effect
FROM document_acl
WHERE tenant_id = 'tenant_001'
  AND (principal_type, principal_id) IN (
      SELECT principal_type, principal_id
      FROM user_principal
      WHERE tenant_id = 'tenant_001'
        AND user_id = '001'
  );
"""