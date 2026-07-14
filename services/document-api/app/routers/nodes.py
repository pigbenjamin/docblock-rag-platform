from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.errors
from fastapi import APIRouter, Depends, Header, HTTPException

from app.auth import get_current_user_id, node_authz, require_node_action
from app.config import settings
from app.schemas.nodes import (
    FolderCreateRequest,
    NodeAclPutRequest,
    NodeMoveRequest,
    NodeRenameRequest,
)
from app.services import node_ops
from app.services.audit import audit
from docblock_core.storage import LocalFileStorage

router = APIRouter(tags=["nodes"])

storage = LocalFileStorage(Path("/data/uploads"))

# Actions echoed back per item in listings so a file-browser UI can decide
# which buttons to show. The server re-checks on every call regardless.
_LISTING_ACTIONS = ("browse", "query", "read", "upload", "update", "delete", "move", "manage_acl")


def _db_conn():
    return psycopg2.connect(settings.db.pg_dsn)


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def _permissions_for(user_id: str, node_ids: List[str]) -> Dict[str, Dict[str, bool]]:
    """{node_id: {action: bool}} for every listing action (one batch query per action)."""
    authz = node_authz()
    ctx = authz.fetch_user_context(user_id)
    perms: Dict[str, Dict[str, bool]] = {n: {} for n in node_ids}
    for action in _LISTING_ACTIONS:
        allowed = authz.evaluate(user_id=user_id, action=action, node_ids=node_ids, ctx=ctx)
        for n in node_ids:
            perms[n][action] = bool(allowed.get(n))
    return perms


# ---------------------------------------------------------------- folders --

@router.post("/folders")
def create_folder(
    req: FolderCreateRequest,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """Create a folder under an existing folder (root folders are managed by
    the migration / department sync, not this API). Requires `upload` on the
    parent - which the owning department's KM always has."""
    name = req.name.strip()
    if not name or len(name) > 255:
        raise HTTPException(status_code=400, detail="name must be 1-255 characters")
    if not _is_uuid(req.parent_id):
        raise HTTPException(status_code=400, detail="parent_id must be a UUID")
    node_ops.validate_acl_entries(req.acl)

    parent = node_ops.fetch_node(req.parent_id)
    if parent is None:
        raise HTTPException(status_code=404, detail=f"parent folder '{req.parent_id}' not found")
    if parent["node_type"] != "folder":
        raise HTTPException(status_code=400, detail="parent_id must be a folder")

    require_node_action(user_id, req.parent_id, "upload")

    node_id = str(uuid.uuid4())
    owner = (req.owner_department_id or parent["owner_department_id"]).strip()
    path_cache = f"{parent['path_cache'] or ''}/{name}"

    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO nodes
                      (id, tenant_id, parent_id, node_type, name, owner_department_id,
                       inherit_acl, path_cache, created_by, updated_by)
                    VALUES (%s, %s, %s, 'folder', %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        node_id, settings.db.tenant_id, req.parent_id, name, owner,
                        req.inherit_acl, path_cache, user_id, user_id,
                    ),
                )
                node_ops.insert_acl_entries(cur, node_id, req.acl, user_id)
            conn.commit()
    except psycopg2.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail=f"a node named '{name}' already exists in this folder")

    audit("node.create", actor_id=user_id, resource_type="node", resource_id=node_id,
          after={"name": name, "parent_id": req.parent_id, "owner_department_id": owner})
    return {"node_id": node_id, "name": name, "parent_id": req.parent_id,
            "owner_department_id": owner, "inherit_acl": req.inherit_acl}


# ------------------------------------------------------------------ nodes --

@router.get("/nodes")
def list_nodes(
    parent_id: Optional[str] = None,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """List the children of a folder (omit parent_id for the root level),
    filtered to nodes the caller may `browse`. Each item carries a
    `permissions` map for UI gating."""
    if parent_id is not None:
        if not _is_uuid(parent_id):
            raise HTTPException(status_code=400, detail="parent_id must be a UUID")
        require_node_action(user_id, parent_id, "browse")

    children = node_ops.list_children(parent_id)
    if not children:
        return {"parent_id": parent_id, "items": []}

    perms = _permissions_for(user_id, [c["id"] for c in children])
    items = []
    for c in children:
        p = perms[c["id"]]
        if not p["browse"]:
            continue
        item = {
            "node_id": c["id"],
            "node_type": c["node_type"],
            "name": c["name"],
            "owner_department_id": c["owner_department_id"],
            "permissions": p,
            "updated_at": c["updated_at"],
        }
        if c["node_type"] == "document":
            item["document_id"] = c["id"]  # same UUID by design (D3)
            item["status"] = c["status"]
            item["active_version"] = c["active_version"]
            item["file_size"] = c["file_size"]
        items.append(item)
    return {"parent_id": parent_id, "items": items}


@router.get("/nodes/{node_id}")
def get_node(
    node_id: str,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    if not _is_uuid(node_id):
        raise HTTPException(status_code=400, detail="node_id must be a UUID")
    require_node_action(user_id, node_id, "browse")

    node = node_ops.fetch_node(node_id)
    if node is None:  # deleted between the check and the fetch
        raise HTTPException(status_code=404, detail=f"node '{node_id}' not found")
    node["permissions"] = _permissions_for(user_id, [node_id])[node_id]
    return node


@router.patch("/nodes/{node_id}")
def rename_node(
    node_id: str,
    req: NodeRenameRequest,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """Rename. Only nodes.name (and the display path cache) change - the
    node's UUID, storage files and chunks are untouched."""
    name = req.name.strip()
    if not name or len(name) > 255:
        raise HTTPException(status_code=400, detail="name must be 1-255 characters")
    if not _is_uuid(node_id):
        raise HTTPException(status_code=400, detail="node_id must be a UUID")
    require_node_action(user_id, node_id, "update")

    node = node_ops.fetch_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"node '{node_id}' not found")
    if node["parent_id"] is None:
        raise HTTPException(status_code=400, detail="root folders cannot be renamed")

    parent_path = (node["path_cache"] or "").rsplit("/", 1)[0]
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE nodes
                    SET name = %s, path_cache = %s, updated_by = %s, updated_at = now()
                    WHERE tenant_id = %s AND id = %s
                    """,
                    (name, f"{parent_path}/{name}", user_id, settings.db.tenant_id, node_id),
                )
            conn.commit()
    except psycopg2.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail=f"a node named '{name}' already exists in this folder")

    audit("node.rename", actor_id=user_id, resource_type="node", resource_id=node_id,
          before={"name": node["name"]}, after={"name": name})
    return {"node_id": node_id, "name": name}


@router.post("/nodes/{node_id}/move")
def move_node(
    node_id: str,
    req: NodeMoveRequest,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """Move a node into another folder. Needs `move` on the node and `upload`
    on the target folder. Storage files and chunks never move (paths carry no
    authority); the subtree's effective permissions follow the new parent."""
    if not _is_uuid(node_id) or not _is_uuid(req.new_parent_id):
        raise HTTPException(status_code=400, detail="node_id and new_parent_id must be UUIDs")

    node = node_ops.fetch_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"node '{node_id}' not found")
    if node["parent_id"] is None:
        raise HTTPException(status_code=400, detail="root folders cannot be moved")

    target = node_ops.fetch_node(req.new_parent_id)
    if target is None:
        raise HTTPException(status_code=404, detail=f"target folder '{req.new_parent_id}' not found")
    if target["node_type"] != "folder":
        raise HTTPException(status_code=400, detail="new_parent_id must be a folder")
    if node_ops.is_descendant_or_self(node_id, req.new_parent_id):
        raise HTTPException(status_code=400, detail="cannot move a node into its own subtree")

    require_node_action(user_id, node_id, "move")
    require_node_action(user_id, req.new_parent_id, "upload")

    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE nodes
                    SET parent_id = %s,
                        path_cache = %s,
                        permission_revision = permission_revision + 1,
                        updated_by = %s, updated_at = now()
                    WHERE tenant_id = %s AND id = %s
                    RETURNING permission_revision
                    """,
                    (
                        req.new_parent_id,
                        f"{target['path_cache'] or ''}/{node['name']}",
                        user_id, settings.db.tenant_id, node_id,
                    ),
                )
                new_revision = cur.fetchone()[0]
            conn.commit()
    except psycopg2.errors.UniqueViolation:
        raise HTTPException(
            status_code=409,
            detail=f"a node named '{node['name']}' already exists in the target folder",
        )

    audit("node.move", actor_id=user_id, resource_type="node", resource_id=node_id,
          before={"parent_id": node["parent_id"]}, after={"parent_id": req.new_parent_id})
    return {"node_id": node_id, "parent_id": req.new_parent_id, "permission_revision": new_revision}


@router.delete("/nodes/{node_id}")
def delete_node(
    node_id: str,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """Hard delete (D6): the node, its whole subtree, every contained
    document's chunks/ACL rows (FK cascade) and stored files. No trash can."""
    if not _is_uuid(node_id):
        raise HTTPException(status_code=400, detail="node_id must be a UUID")

    node = node_ops.fetch_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"node '{node_id}' not found")
    if node["parent_id"] is None:
        raise HTTPException(status_code=400, detail="root folders cannot be deleted")

    require_node_action(user_id, node_id, "delete")

    doc_ids, node_count = node_ops.delete_node_subtree(node_id=node_id, storage=storage)
    audit("node.delete", actor_id=user_id, resource_type="node", resource_id=node_id,
          before={"name": node["name"], "node_type": node["node_type"]},
          after={"deleted_nodes": node_count, "deleted_documents": doc_ids})
    return {"ok": True, "node_id": node_id, "deleted_nodes": node_count,
            "deleted_documents": doc_ids}


# -------------------------------------------------------------------- acl --

@router.get("/nodes/{node_id}/acl")
def get_node_acl(
    node_id: str,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """The node's own entries (inherited rules live on ancestors). Requires
    `manage_acl` - held implicitly by the owning department's KM."""
    if not _is_uuid(node_id):
        raise HTTPException(status_code=400, detail="node_id must be a UUID")
    require_node_action(user_id, node_id, "manage_acl")

    node = node_ops.fetch_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"node '{node_id}' not found")

    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT subject_type, subject_id, action, effect, inherit_to_children
                FROM acl_entries
                WHERE tenant_id = %s AND node_id = %s
                ORDER BY subject_type, subject_id, action
                """,
                (settings.db.tenant_id, node_id),
            )
            rows = cur.fetchall()

    # group per subject for a UI-friendly shape (mirrors the PUT request body)
    grouped: Dict[tuple, Dict[str, Any]] = {}
    for stype, sid, action, effect, inherit in rows:
        key = (stype, sid, effect, inherit)
        grouped.setdefault(key, {
            "subject_type": stype, "subject_id": sid, "effect": effect,
            "inherit_to_children": inherit, "actions": [],
        })["actions"].append(action)

    return {
        "node_id": node_id,
        "owner_department_id": node["owner_department_id"],
        "inherit_acl": node["inherit_acl"],
        "permission_revision": node["permission_revision"],
        "entries": list(grouped.values()),
    }


@router.put("/nodes/{node_id}/acl")
def put_node_acl(
    node_id: str,
    req: NodeAclPutRequest,
    user_id: str = Depends(get_current_user_id),
    if_match: Optional[str] = Header(default=None, alias="If-Match"),
) -> Dict[str, Any]:
    """Replace the node's own entries wholesale. Requires `manage_acl`.

    Sharing vs management: granting browse/query/read is plain sharing;
    `manage_acl` in an entry hands co-management to that subject, and
    owner_department_id never changes here. Pass If-Match with the
    permission_revision you last read to detect concurrent edits (409).
    """
    if not _is_uuid(node_id):
        raise HTTPException(status_code=400, detail="node_id must be a UUID")
    node_ops.validate_acl_entries(req.entries)
    require_node_action(user_id, node_id, "manage_acl")

    expected_revision: Optional[int] = None
    if if_match is not None:
        token = if_match.strip().lstrip("W/").strip('"')
        if not token.isdigit():
            raise HTTPException(status_code=400, detail='If-Match must look like "12"')
        expected_revision = int(token)

    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT permission_revision, inherit_acl FROM nodes
                WHERE tenant_id = %s AND id = %s FOR UPDATE
                """,
                (settings.db.tenant_id, node_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"node '{node_id}' not found")
            current_revision, current_inherit = row

            if expected_revision is not None and expected_revision != current_revision:
                raise HTTPException(
                    status_code=409,
                    detail=f"permission_revision is {current_revision}, you sent {expected_revision}; reload and retry",
                )

            cur.execute(
                "SELECT subject_type, subject_id, action, effect FROM acl_entries WHERE tenant_id = %s AND node_id = %s",
                (settings.db.tenant_id, node_id),
            )
            before_rows = [
                {"subject_type": r[0], "subject_id": r[1], "action": r[2], "effect": r[3]}
                for r in cur.fetchall()
            ]

            cur.execute(
                "DELETE FROM acl_entries WHERE tenant_id = %s AND node_id = %s",
                (settings.db.tenant_id, node_id),
            )
            node_ops.insert_acl_entries(cur, node_id, req.entries, user_id)

            new_inherit = current_inherit if req.inherit_acl is None else req.inherit_acl
            cur.execute(
                """
                UPDATE nodes
                SET permission_revision = permission_revision + 1,
                    inherit_acl = %s, updated_by = %s, updated_at = now()
                WHERE tenant_id = %s AND id = %s
                RETURNING permission_revision
                """,
                (new_inherit, user_id, settings.db.tenant_id, node_id),
            )
            new_revision = cur.fetchone()[0]
        conn.commit()

    audit("acl.update", actor_id=user_id, resource_type="node", resource_id=node_id,
          before={"entries": before_rows},
          after={"entries": [e.model_dump() if hasattr(e, "model_dump") else e.dict() for e in req.entries],
                 "inherit_acl": new_inherit})
    return {"node_id": node_id, "permission_revision": new_revision, "inherit_acl": new_inherit}
