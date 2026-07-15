from __future__ import annotations

import uuid
from typing import Any, Dict, List

import psycopg2
from fastapi import APIRouter, Depends, HTTPException

from app.auth import get_current_user_id, node_authz
from app.config import settings
from app.schemas.admins import AdminListPutRequest
from app.services.audit import audit

router = APIRouter(tags=["admins"])

# FB-6 (D8/D9): admin rosters live in our own tables, not Keycloak. The
# department_admins table is what gives a user the owner-KM shortcut in
# docblock_core.authz; global_admins short-circuits every check. Reads are
# open to any authenticated user (it's a "who do I ask for access" directory);
# writes are restricted to a global admin or, for a department's own roster,
# its current admins.


def _db_conn():
    return psycopg2.connect(settings.db.pg_dsn)


def _validate_user_ids(user_ids: List[str]) -> List[str]:
    """Deduplicate preserving order; reject non-UUID entries."""
    seen = set()
    out: List[str] = []
    for u in user_ids:
        u = u.strip()
        try:
            uuid.UUID(u)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"user_id '{u}' is not a UUID")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _department_known(cur, department: str) -> bool:
    # A department is "known" if anything in the system already references it:
    # a root folder, a member principal, or an existing admin row. This is a
    # typo guard, not an authorization rule - Keycloak is deliberately not
    # consulted (naming there may differ, see the dept-A vs A duality).
    cur.execute(
        """
        SELECT EXISTS (SELECT 1 FROM nodes
                       WHERE tenant_id = %(t)s AND parent_id IS NULL
                         AND node_type = 'folder' AND name = %(d)s)
            OR EXISTS (SELECT 1 FROM user_principal
                       WHERE tenant_id = %(t)s AND principal_type = 'department'
                         AND principal_id = %(d)s)
            OR EXISTS (SELECT 1 FROM department_admins
                       WHERE tenant_id = %(t)s AND department = %(d)s)
        """,
        {"t": settings.db.tenant_id, "d": department},
    )
    return bool(cur.fetchone()[0])


# ----------------------------------------------------- department admins --

@router.get("/departments/{department}/admins")
def list_department_admins(
    department: str,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    with _db_conn() as conn:
        with conn.cursor() as cur:
            if not _department_known(cur, department):
                raise HTTPException(status_code=404, detail=f"unknown department '{department}'")
            cur.execute(
                """
                SELECT user_id::text, created_by::text, created_at
                FROM department_admins
                WHERE tenant_id = %s AND department = %s
                ORDER BY created_at, user_id
                """,
                (settings.db.tenant_id, department),
            )
            rows = cur.fetchall()
    return {
        "department": department,
        "admins": [
            {"user_id": r[0], "created_by": r[1], "created_at": r[2]} for r in rows
        ],
    }


@router.put("/departments/{department}/admins")
def put_department_admins(
    department: str,
    req: AdminListPutRequest,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """Replace the department's admin roster wholesale. Allowed for a global
    admin or a current admin of this department. A department admin may hand
    the roster over (even drop themselves) but not empty it - leaving a
    department with no admins is a global-admin-only decision."""
    new_ids = _validate_user_ids(req.user_ids)

    ctx = node_authz().fetch_user_context(user_id)
    if not (ctx.is_global_admin or department in ctx.km_departments):
        raise HTTPException(
            status_code=403,
            detail=f"requires global admin or an admin of department '{department}'",
        )
    if not new_ids and not ctx.is_global_admin:
        raise HTTPException(
            status_code=400,
            detail="only a global admin may leave a department without admins",
        )

    with _db_conn() as conn:
        with conn.cursor() as cur:
            if not _department_known(cur, department):
                raise HTTPException(status_code=404, detail=f"unknown department '{department}'")
            cur.execute(
                """
                SELECT user_id::text FROM department_admins
                WHERE tenant_id = %s AND department = %s
                ORDER BY user_id
                """,
                (settings.db.tenant_id, department),
            )
            before_ids = [r[0] for r in cur.fetchall()]
            cur.execute(
                "DELETE FROM department_admins WHERE tenant_id = %s AND department = %s",
                (settings.db.tenant_id, department),
            )
            for uid in new_ids:
                cur.execute(
                    """
                    INSERT INTO department_admins (tenant_id, department, user_id, created_by)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (settings.db.tenant_id, department, uid, user_id),
                )
        conn.commit()

    audit("department_admins.update", actor_id=user_id,
          resource_type="department", resource_id=department,
          before={"user_ids": before_ids}, after={"user_ids": new_ids})
    return {"department": department, "user_ids": new_ids}


# --------------------------------------------------------- global admins --

@router.get("/global-admins")
def list_global_admins(
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_id::text, created_by::text, created_at
                FROM global_admins
                WHERE tenant_id = %s
                ORDER BY created_at, user_id
                """,
                (settings.db.tenant_id,),
            )
            rows = cur.fetchall()
    return {
        "admins": [
            {"user_id": r[0], "created_by": r[1], "created_at": r[2]} for r in rows
        ],
    }


@router.put("/global-admins")
def put_global_admins(
    req: AdminListPutRequest,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """Replace the global-admin roster. Global admins only, and the result
    must keep at least one admin (anti-lockout - the very first admin is
    seeded straight into the DB, see 01_schema.sql)."""
    new_ids = _validate_user_ids(req.user_ids)

    ctx = node_authz().fetch_user_context(user_id)
    if not ctx.is_global_admin:
        raise HTTPException(status_code=403, detail="requires global admin")
    if not new_ids:
        raise HTTPException(status_code=400, detail="global_admins cannot be emptied")

    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id::text FROM global_admins WHERE tenant_id = %s ORDER BY user_id",
                (settings.db.tenant_id,),
            )
            before_ids = [r[0] for r in cur.fetchall()]
            cur.execute(
                "DELETE FROM global_admins WHERE tenant_id = %s",
                (settings.db.tenant_id,),
            )
            for uid in new_ids:
                cur.execute(
                    """
                    INSERT INTO global_admins (tenant_id, user_id, created_by)
                    VALUES (%s, %s, %s)
                    """,
                    (settings.db.tenant_id, uid, user_id),
                )
        conn.commit()

    audit("global_admins.update", actor_id=user_id,
          resource_type="global_admins", resource_id=settings.db.tenant_id,
          before={"user_ids": before_ids}, after={"user_ids": new_ids})
    return {"user_ids": new_ids}
