from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm
from fastapi import Header, HTTPException

from app.config import settings
from docblock_core.authz import NodeAuthz, UserAuthzContext

_node_authz = NodeAuthz(pg_dsn=settings.db.pg_dsn, tenant_id=settings.db.tenant_id)


def node_authz() -> NodeAuthz:
    return _node_authz


def require_node_action(
    user_id: str,
    node_id: str,
    action: str,
    *,
    ctx: Optional[UserAuthzContext] = None,
) -> None:
    """Raise 404 when the node doesn't exist, 403 when `action` is denied.

    Node-tree replacement for require_document_km: evaluates acl_entries up
    the folder tree (owner-department KM always passes, see docblock_core.authz).
    """
    allowed = _node_authz.evaluate_one(user_id=user_id, action=action, node_id=node_id, ctx=ctx)
    if allowed is None:
        raise HTTPException(status_code=404, detail=f"node '{node_id}' not found")
    if not allowed:
        raise HTTPException(status_code=403, detail=f"requires '{action}' permission on node {node_id}")

_JWKS_TTL_SECONDS = 600
_jwks_cache: Dict[str, Any] = {"keys": [], "fetched_at": 0.0}

_DISCOVERY_TTL_SECONDS = 3600
_discovery_cache: Dict[str, Any] = {"issuer": None, "fetched_at": 0.0}


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def _fetch_issuer() -> str:
    # Keycloak's advertised `issuer` is built from its configured frontend
    # hostname, which can differ from KEYCLOAK_URL (the address document-api
    # uses to *reach* Keycloak, e.g. a cluster-internal IP) - so we read it
    # from the discovery document instead of assuming they match.
    #
    # We deliberately do NOT use the discovery document's `jwks_uri` the same
    # way: it's built from that same external hostname, which may not be
    # network-reachable from document-api (confirmed in dev: the frontend
    # hostname resolves but times out, while KEYCLOAK_URL works fine). The
    # certs path is a fixed, well-known path under the realm regardless of
    # which reachable hostname you use, so we build it from KEYCLOAK_URL.
    url = f"{settings.keycloak.url}/realms/{settings.keycloak.realm}/.well-known/openid-configuration"
    resp = httpx.get(url, verify=settings.keycloak.verify, timeout=5)
    resp.raise_for_status()
    return resp.json()["issuer"]


def _issuer() -> str:
    now = time.time()
    if not _discovery_cache["issuer"] or now - _discovery_cache["fetched_at"] > _DISCOVERY_TTL_SECONDS:
        _discovery_cache["issuer"] = _fetch_issuer()
        _discovery_cache["fetched_at"] = now
    return _discovery_cache["issuer"]


def _jwks_url() -> str:
    return f"{settings.keycloak.url}/realms/{settings.keycloak.realm}/protocol/openid-connect/certs"


def _fetch_jwks() -> List[Dict[str, Any]]:
    resp = httpx.get(_jwks_url(), verify=settings.keycloak.verify, timeout=5)
    resp.raise_for_status()
    return resp.json().get("keys", [])


def _get_signing_key(kid: str) -> Optional[Dict[str, Any]]:
    now = time.time()
    if not _jwks_cache["keys"] or now - _jwks_cache["fetched_at"] > _JWKS_TTL_SECONDS:
        _jwks_cache["keys"] = _fetch_jwks()
        _jwks_cache["fetched_at"] = now

    for key in _jwks_cache["keys"]:
        if key.get("kid") == kid:
            return key

    # kid miss: Keycloak may have rotated its signing key, force one refetch.
    _jwks_cache["keys"] = _fetch_jwks()
    _jwks_cache["fetched_at"] = time.time()
    for key in _jwks_cache["keys"]:
        if key.get("kid") == kid:
            return key

    return None


def _verify_jwt(token: str) -> str:
    """Verify a Keycloak-issued access token locally against the realm's JWKS.

    Returns the token's `sub` (Keycloak user_id) on success.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"invalid token header: {e}")

    kid = header.get("kid")
    jwk = _get_signing_key(kid) if kid else None
    if jwk is None:
        raise HTTPException(status_code=401, detail="unknown signing key")

    try:
        public_key = RSAAlgorithm.from_jwk(json.dumps(jwk))
        claims = jwt.decode(
            token,
            key=public_key,
            algorithms=["RS256"],
            issuer=_issuer(),
            options={"verify_aud": False},
        )
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"invalid token: {e}")

    sub = claims.get("sub")
    if not sub or not _is_uuid(sub):
        raise HTTPException(status_code=401, detail="token missing a valid 'sub' claim")
    return sub


def get_current_user_id(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
) -> str:
    """
    Resolve the caller's user_id.

    Preferred: `Authorization: Bearer <keycloak access token>`, verified locally
    against the realm's JWKS. Falls back to the legacy `X-User-Id` header
    while the frontend/tests migrate to sending a real token - remove the
    fallback once nothing depends on it.
    """
    if authorization:
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Authorization header must be 'Bearer <token>'")
        return _verify_jwt(authorization[len("Bearer "):])

    if x_user_id:
        if not _is_uuid(x_user_id):
            raise HTTPException(status_code=400, detail="X-User-Id must be a UUID")
        return x_user_id

    raise HTTPException(status_code=401, detail="Missing Authorization bearer token")


def get_current_user_id_or_admin_secret(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_acl_secret: Optional[str] = Header(default=None, alias="X-Acl-Secret"),
) -> Optional[str]:
    """
    Resolve the caller for ACL-management endpoints.

    Returns the caller's user_id (JWT or X-User-Id, subject to the normal KM
    check), or None if authenticated via the legacy `X-Acl-Secret` admin
    bypass - which, like before JWT existed, skips per-user KM checks
    entirely. Remove the secret bypass once nothing depends on it.
    """
    if x_acl_secret:
        if not settings.acl.admin_secret or x_acl_secret != settings.acl.admin_secret:
            raise HTTPException(status_code=401, detail="Invalid ACL secret")
        return None

    return get_current_user_id(authorization=authorization, x_user_id=x_user_id)
