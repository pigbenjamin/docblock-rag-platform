from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from fastapi import HTTPException

from app.config import settings
from docblock_core.authz import ACTIONS
from docblock_core.storage import LocalFileStorage


def _db_conn():
    return psycopg2.connect(settings.db.pg_dsn)


def validate_acl_entries(entries) -> None:
    for e in entries:
        if not e.actions:
            raise HTTPException(status_code=400, detail="acl entry needs at least one action")
        for a in e.actions:
            if a not in ACTIONS:
                raise HTTPException(status_code=400, detail=f"unknown action '{a}'; must be one of {ACTIONS}")
        if not e.subject_id.strip():
            raise HTTPException(status_code=400, detail="acl entry subject_id is empty")


def insert_acl_entries(cur, node_id: str, entries, created_by: Optional[str]) -> None:
    for e in entries:
        for action in dict.fromkeys(e.actions):  # de-dupe, keep order
            cur.execute(
                """
                INSERT INTO acl_entries
                  (tenant_id, node_id, subject_type, subject_id, action, effect,
                   inherit_to_children, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, node_id, subject_type, subject_id, action)
                DO UPDATE SET effect = EXCLUDED.effect,
                              inherit_to_children = EXCLUDED.inherit_to_children,
                              updated_at = now()
                """,
                (
                    settings.db.tenant_id, node_id, e.subject_type, e.subject_id.strip(),
                    action, e.effect, e.inherit_to_children, created_by,
                ),
            )


def fetch_node(node_id: str) -> Optional[Dict[str, Any]]:
    """Load one node row (plus document status when it's a document node)."""
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT n.id::text, n.parent_id::text, n.node_type, n.name,
                       n.owner_department_id, n.inherit_acl, n.permission_revision,
                       n.path_cache, n.created_by::text, n.created_at, n.updated_at,
                       d.status, d.active_version, d.title, d.original_filename,
                       d.file_size, d.mime_type
                FROM nodes n
                LEFT JOIN documents d
                  ON d.tenant_id = n.tenant_id AND d.document_id = n.id
                WHERE n.tenant_id = %s AND n.id = %s
                """,
                (settings.db.tenant_id, node_id),
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [c[0] for c in cur.description]
    node = dict(zip(cols, row))
    if node["node_type"] == "document" and node["status"] is None:
        # node exists but ingest hasn't written the documents row yet
        node["status"] = "processing"
    return node


def list_children(parent_id: Optional[str]) -> List[Dict[str, Any]]:
    """Direct children of a folder (parent_id=None -> root nodes), folders first.

    NOT permission-filtered - callers must apply the browse filter.
    """
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT n.id::text, n.node_type, n.name, n.owner_department_id,
                       n.permission_revision, n.created_at, n.updated_at,
                       d.status, d.active_version, d.file_size
                FROM nodes n
                LEFT JOIN documents d
                  ON d.tenant_id = n.tenant_id AND d.document_id = n.id
                WHERE n.tenant_id = %s
                  AND ((%s::uuid IS NULL AND n.parent_id IS NULL) OR n.parent_id = %s::uuid)
                ORDER BY (n.node_type <> 'folder'), n.name
                """,
                (settings.db.tenant_id, parent_id, parent_id),
            )
            rows = cur.fetchall()
            cols = [c[0] for c in cur.description]
    out = []
    for row in rows:
        item = dict(zip(cols, row))
        if item["node_type"] == "document" and item["status"] is None:
            item["status"] = "processing"
        out.append(item)
    return out


def is_descendant_or_self(node_id: str, candidate_id: str) -> bool:
    """True if candidate_id is node_id itself or anywhere in its subtree
    (used to block cyclic moves)."""
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH RECURSIVE sub AS (
                  SELECT id FROM nodes WHERE tenant_id = %s AND id = %s
                  UNION ALL
                  SELECT n.id FROM nodes n JOIN sub s ON n.parent_id = s.id
                  WHERE n.tenant_id = %s
                )
                SELECT 1 FROM sub WHERE id = %s LIMIT 1
                """,
                (settings.db.tenant_id, node_id, settings.db.tenant_id, candidate_id),
            )
            return cur.fetchone() is not None


def delete_node_subtree(
    *, node_id: str, storage: LocalFileStorage
) -> Tuple[List[str], int]:
    """Hard-delete a node and its whole subtree (D6).

    Order matters: documents rows first (chunks + document_acl cascade via
    their FKs), then the top node (descendant nodes and acl_entries cascade
    via parent_id / node_id FKs), then the stored files - so a crash can only
    leave orphan files, never DB rows pointing at deleted storage.

    Returns (deleted_document_ids, deleted_node_count).
    """
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH RECURSIVE sub AS (
                  SELECT id, node_type FROM nodes WHERE tenant_id = %s AND id = %s
                  UNION ALL
                  SELECT n.id, n.node_type FROM nodes n JOIN sub s ON n.parent_id = s.id
                  WHERE n.tenant_id = %s
                )
                SELECT id::text, node_type FROM sub
                """,
                (settings.db.tenant_id, node_id, settings.db.tenant_id),
            )
            sub_rows = cur.fetchall()
            if not sub_rows:
                return [], 0

            doc_ids = [r[0] for r in sub_rows if r[1] == "document"]
            if doc_ids:
                cur.execute(
                    "DELETE FROM documents WHERE tenant_id = %s AND document_id = ANY(%s::uuid[])",
                    (settings.db.tenant_id, doc_ids),
                )
            cur.execute(
                "DELETE FROM nodes WHERE tenant_id = %s AND id = %s",
                (settings.db.tenant_id, node_id),
            )
        conn.commit()

    for doc_id in doc_ids:
        storage.delete_document(tenant_id=settings.db.tenant_id, document_id=doc_id)

    return doc_ids, len(sub_rows)
