from typing import Literal
from app.services.acl_db import write_access, delete_access
from app.schemas.acl import WriteAccessMapRequest, DeleteAccessMapRequest


_DOC_ACL_PRINCIPAL_TYPES = ("user", "department")


def parse_principal_key(key: str) -> tuple[Literal["user", "department"], str]:
    """
    Parse a principal key for document ACL operations.
    Only 'user' and 'department' are accepted; 'role' is a user-attribute
    and does not control document-level access.

    Example:
      user:c31f90f3-b99f-4c2e-91e4-4e7776e2b995
      department:A
    """
    if ":" not in key:
        raise ValueError(f"Invalid principal key: {key}")

    principal_type, principal_id = key.split(":", 1)

    if principal_type not in _DOC_ACL_PRINCIPAL_TYPES:
        raise ValueError(
            f"principal_type '{principal_type}' is not allowed in document ACL. "
            f"Allowed types: {_DOC_ACL_PRINCIPAL_TYPES}"
        )

    if not principal_id:
        raise ValueError(f"principal_id is empty: {key}")

    return principal_type, principal_id


class AclService:
    def __init__(self, pg_dsn: str, tenant_id: str):
        self.pg_dsn = pg_dsn
        self.tenant_id = tenant_id

    def write_access_map(self, req: WriteAccessMapRequest):

        access_map = {
            (r.principal_type, r.principal_id): r.effect
            for r in req.access_rules
        }

        for (principal_type, principal_id), effect in access_map.items():

            write_access(
                pg_dsn=self.pg_dsn,
                tenant_id=self.tenant_id,
                document_id=req.document_id,
                principal_type=principal_type,  # type: ignore
                principal_id=principal_id,
                effect=effect,  # type: ignore
            )
        

    def delete_access_map(self, req: DeleteAccessMapRequest) -> None:
        for principal_key in req.principals:
            principal_type, principal_id = parse_principal_key(principal_key)

            delete_access(
                pg_dsn=self.pg_dsn,
                tenant_id=self.tenant_id,
                document_id=req.document_id,
                principal_type=principal_type,
                principal_id=principal_id,
            )
            
# example of access_map:
#{
#    ("department", "A"): "detail",
#    ("department", "B"): "summary",
#    ("user", "47f097e7-9bea-4cbe-8f87-e5c3ecd887ae"): "detail",
#}