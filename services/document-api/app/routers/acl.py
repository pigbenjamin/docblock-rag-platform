from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException

from app.config import settings
from app.auth import get_current_user_id_or_admin_secret, require_document_km
from app.schemas.acl import WriteAccessMapRequest, DeleteAccessMapRequest
from docblock_core.acl import ACLService, parse_principal_key

router = APIRouter(prefix="/acl", tags=["acl"])
_svc = ACLService(pg_dsn=settings.db.pg_dsn, tenant_id=settings.db.tenant_id)


def _authorize(user_id: Optional[str], document_id: str) -> None:
    """user_id is None means the caller used the legacy X-Acl-Secret admin
    bypass, which (like before JWT existed) skips the per-user KM check."""
    if user_id is not None:
        require_document_km(user_id, document_id)


@router.get("/{document_id}")
def get_access_rules(
    document_id: str,
    user_id: Optional[str] = Depends(get_current_user_id_or_admin_secret),
) -> Dict[str, Any]:
    _authorize(user_id, document_id)
    try:
        rows = _svc.list_document_access(document_id=document_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"document_id": document_id, "access_rules": rows}


@router.post("/write-map")
def write_access_map(
    req: WriteAccessMapRequest,
    user_id: Optional[str] = Depends(get_current_user_id_or_admin_secret),
) -> Dict[str, Any]:
    _authorize(user_id, req.document_id)

    if user_id is not None:
        # Sharing must never hand a *new* department 'detail' (= management
        # rights) - only the ingest pipeline's upload-time write can do that.
        # A department that already manages the doc may re-assert its own
        # 'detail' row; anything else can only be shared as 'summary'.
        existing = {
            (r["principal_type"], r["principal_id"]): r["effect"]
            for r in _svc.list_document_access(document_id=req.document_id)
        }
        for rule in req.access_rules:
            if (
                rule.principal_type == "department"
                and rule.effect == "detail"
                and existing.get(("department", rule.principal_id)) != "detail"
            ):
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"cannot grant department '{rule.principal_id}' detail "
                        "(management) access via sharing; only upload can do that"
                    ),
                )

    access_map = {f"{r.principal_type}:{r.principal_id}": r.effect for r in req.access_rules}
    result = _svc.write_access(document_id=req.document_id, access_map=access_map)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["errors"])
    return {"ok": True, "document_id": req.document_id, "count": len(result["results"])}


@router.post("/delete-map")
def delete_access_map(
    req: DeleteAccessMapRequest,
    user_id: Optional[str] = Depends(get_current_user_id_or_admin_secret),
) -> Dict[str, Any]:
    _authorize(user_id, req.document_id)

    deleted = 0
    errors: List[Dict[str, Any]] = []
    for key in req.principals:
        try:
            ptype, pid = parse_principal_key(key)
        except ValueError as e:
            errors.append({"principal": key, "error": str(e)})
            continue

        result = _svc.delete_access(document_id=req.document_id, principal=(ptype, pid))
        if result["success"]:
            deleted += result["deleted"]
        else:
            errors.append({"principal": key, "error": result["reason"]})

    if errors:
        raise HTTPException(status_code=400, detail=errors)
    return {"ok": True, "document_id": req.document_id, "count": deleted}
