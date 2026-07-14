from __future__ import annotations

import json
import logging
from typing import Any, Optional

import psycopg2

from app.config import settings

logger = logging.getLogger(__name__)


def audit(
    event_type: str,
    *,
    actor_id: Optional[str],
    resource_type: str,
    resource_id: str,
    before: Any = None,
    after: Any = None,
    result: str = "ok",
    reason: Optional[str] = None,
) -> None:
    """Append an audit row. Best-effort: an audit failure must never break
    the operation being audited, so errors are logged and swallowed.

    actor_id=None means the legacy admin-secret bypass / a system actor.
    """
    try:
        with psycopg2.connect(settings.db.pg_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO audit_logs
                      (tenant_id, event_type, actor_id, resource_type, resource_id,
                       before_data, after_data, result, reason)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        settings.db.tenant_id,
                        event_type,
                        actor_id,
                        resource_type,
                        resource_id,
                        json.dumps(before, ensure_ascii=False, default=str) if before is not None else None,
                        json.dumps(after, ensure_ascii=False, default=str) if after is not None else None,
                        result,
                        reason,
                    ),
                )
    except Exception:
        logger.warning("audit write failed for %s on %s/%s", event_type, resource_type, resource_id, exc_info=True)
