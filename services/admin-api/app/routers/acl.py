from fastapi import APIRouter, Depends, Header, HTTPException

from app.config import settings
from app.schemas.acl import WriteAccessMapRequest, DeleteAccessMapRequest
from app.services.acl_service import AclService


def verify_acl_secret(x_acl_secret: str = Header(default="")):
    if x_acl_secret != settings.acl.admin_secret:
        raise HTTPException(status_code=401, detail="Invalid ACL secret")


router = APIRouter(prefix="/acl", tags=["acl"])
_svc = AclService(pg_dsn=settings.db.pg_dsn, tenant_id=settings.db.tenant_id)


@router.post("/write-map", dependencies=[Depends(verify_acl_secret)])
def write_access_map(req: WriteAccessMapRequest):
    try:
        _svc.write_access_map(req)
        return {"ok": True, "document_id": req.document_id, "count": len(req.access_rules)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/delete-map", dependencies=[Depends(verify_acl_secret)])
def delete_access_map(req: DeleteAccessMapRequest):
    try:
        _svc.delete_access_map(req)
        return {"ok": True, "document_id": req.document_id, "count": len(req.principals)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
