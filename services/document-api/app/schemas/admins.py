from pydantic import BaseModel


class AdminListPutRequest(BaseModel):
    # Full replacement of the admin list (mirrors the node-ACL PUT style).
    user_ids: list[str]
