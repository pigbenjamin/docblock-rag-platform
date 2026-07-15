from typing import Literal, Optional

from pydantic import BaseModel

# Node ACL entries only accept user/department subjects; admin status is not
# an ACL subject (owner-KM checks read the department_admins table, and authz
# synthesizes 'dept:{d}:role:KM' role principals from it for legacy entries).
NodeAclSubjectType = Literal["user", "department"]
NodeAclEffect = Literal["allow", "deny"]


class NodeAclEntryIn(BaseModel):
    subject_type: NodeAclSubjectType
    subject_id: str
    actions: list[str]
    effect: NodeAclEffect = "allow"
    inherit_to_children: bool = True


class FolderCreateRequest(BaseModel):
    parent_id: str
    name: str
    owner_department_id: Optional[str] = None  # default: parent folder's owner
    inherit_acl: bool = True
    acl: list[NodeAclEntryIn] = []


class NodeRenameRequest(BaseModel):
    name: str


class NodeMoveRequest(BaseModel):
    new_parent_id: str


class NodeAclPutRequest(BaseModel):
    # Full replacement of the node's own entries (inherited rules are not
    # affected - detach from them with inherit_acl=false instead).
    inherit_acl: Optional[bool] = None  # None = leave unchanged
    entries: list[NodeAclEntryIn]
