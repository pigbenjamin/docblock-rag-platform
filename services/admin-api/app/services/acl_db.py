# app/acl/acl.py

from typing import Literal, Tuple, Dict, Any, List
from uuid import UUID
import psycopg2

from docblock_core import sql_utils

PrincipalType = Literal["user", "department", "role"]
AccessEffect = Literal["detail", "summary", "deny"]

def write_access(
    *,
    pg_dsn: str,
    tenant_id: str,
    document_id: str,
    principal_type: PrincipalType,
    principal_id: str,
    effect: AccessEffect,
) -> None:
    """
    Upsert 一筆文件 ACL 規則。

    唯一條件建議是：
      tenant_id + document_id + principal_type + principal_id
    """
    conn = psycopg2.connect(pg_dsn)
    try:
        with conn, conn.cursor() as cur:
            sql_utils.upsert_document_acl(
                cur=cur,
                tenant_id=tenant_id,
                document_id=document_id,
                principal_type=principal_type,
                principal_id=principal_id,
                effect=effect,
            )
    finally:
        conn.close()


def delete_access(
    *,
    pg_dsn: str,
    tenant_id: str,
    document_id: str,
    principal_type: PrincipalType,
    principal_id: str,
) -> None:
    """
    刪除一筆文件 ACL 規則。
    """
    conn = psycopg2.connect(pg_dsn)
    try:
        with conn, conn.cursor() as cur:
            sql_utils.delete_document_acl(
                cur=cur,
                tenant_id=tenant_id,
                document_id=document_id,
                principal_type=principal_type,
                principal_id=principal_id,
            )
    finally:
        conn.close()
                    