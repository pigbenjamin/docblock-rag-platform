from pydantic import BaseModel
from typing import Literal


# All principal types recognised by the system (used for user_principal lookups etc.)
PrincipalType = Literal["user", "department", "role"]

# Document ACL only supports user and department; role is a user-attribute only.
DocAclPrincipalType = Literal["user", "department"]

AccessEffect = Literal["detail", "summary", "deny"]


class AccessRule(BaseModel):
    principal_type: DocAclPrincipalType
    principal_id: str
    effect: AccessEffect


class WriteAccessMapRequest(BaseModel):
    document_id: str
    access_rules: list[AccessRule]


class DeleteAccessMapRequest(BaseModel):
    document_id: str
    principals: list[str]