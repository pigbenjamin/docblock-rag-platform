"""Small PostgreSQL helper utilities used across the project.

The module purpose is to centralize common DB tasks so callers don't
repeat connection/transaction/sequence logic and to provide small,
well-documented helpers for operations like syncing serial sequences
and safely inserting into `document_acl`.

This file is intentionally small and dependency-light so it's easy to
mock in unit tests.
"""
from __future__ import annotations

from contextlib import contextmanager
import logging
from typing import Any, Iterable, Optional, Sequence

import psycopg2
import psycopg2.extras
from psycopg2.extras import Json


logger = logging.getLogger(__name__)


__all__ = [
    "get_conn",
    "transaction",
    "execute_values",
    "reset_serial_sequence",
    "safe_insert_document_acl",
    "upsert_document_acl",
    "document_exists",
    "insert_text_chunks",
    "insert_table_chunks",
    "insert_image_chunks",
    "insert_summary_chunk",
    "delete_document_acl",
    "upsert_document_sum",
]


def get_conn(pg_dsn: str) -> psycopg2.extensions.connection:
    """Create a new psycopg2 connection using the provided DSN.

    Caller is responsible for closing the connection (or using a context).
    """
    return psycopg2.connect(pg_dsn)


@contextmanager
def transaction(conn: psycopg2.extensions.connection):
    """Context manager that yields a cursor and commits/rolls back.

    Usage:
        with transaction(conn) as cur:
            cur.execute(...)
    """
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            cur.close()
        except Exception:
            pass


def execute_values(cur, sql: str, rows: Sequence[Sequence[Any]], template: Optional[str] = None) -> None:
    """Wrapper around psycopg2.extras.execute_values for consistency.

    Keeps callers from importing extras directly and centralizes error
    handling.
    """
    if not rows:
        return
    try:
        psycopg2.extras.execute_values(cur, sql, rows, template=template)
    except Exception:
        logger.exception("execute_values failed")
        raise


def reset_serial_sequence(conn: psycopg2.extensions.connection, table: str, pk_column: str) -> None:
    """Sync the serial/sequence for `table.pk_column` to the current MAX(pk).

    This uses `pg_get_serial_sequence` and `setval`. It is safe to call
    multiple times and useful to recover from manual inserts that left
    the sequence behind.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_get_serial_sequence(%s, %s)", (table, pk_column))
        seq_row = cur.fetchone()
        if not seq_row or not seq_row[0]:
            # no sequence found (maybe pk isn't serial) — nothing to do
            logger.debug("no serial sequence for %s.%s", table, pk_column)
            return
        seq_name = seq_row[0]

        # set sequence to max(pk) + 1 (or 1 if table empty)
        cur.execute(f"SELECT COALESCE(MAX({pk_column}), 0) FROM {table}")
        max_row = cur.fetchone()
        max_id = int(max_row[0] if max_row else 0)
        new_val = max_id + 1
        cur.execute("SELECT setval(%s, %s, false)", (seq_name, new_val))
        conn.commit()
        logger.info("reset sequence %s to %s", seq_name, new_val)


def document_exists(cur, tenant_id: str, document_id: str) -> bool:
    """Check if a UUID document_id exists in the documents table.

    Returns True if found, False otherwise.
    """
    cur.execute(
        "SELECT 1 FROM documents WHERE tenant_id = %s AND document_id = %s LIMIT 1",
        (tenant_id, document_id),
    )
    return cur.fetchone() is not None


# for text/table/image chunk upsert in docblock-rag app
def insert_text_chunks(cur, rows: Sequence[Sequence[Any]]) -> None:
        """Insert or update `text_chunks` rows in bulk using execute_values.

        Expects `rows` to match the template used by the project.
        """
        sql = """
        INSERT INTO text_chunks (
            tenant_id, document_id, version,
            chunk_index, page_start, page_end, char_start, char_end,
            heading_path, chunk_title, content, metadata,
            embed_text, embedding
        )
        VALUES %s
        ON CONFLICT (tenant_id, document_id, version, chunk_index) DO UPDATE
        SET
            page_start = EXCLUDED.page_start,
            page_end = EXCLUDED.page_end,
            char_start = EXCLUDED.char_start,
            char_end = EXCLUDED.char_end,
            heading_path = EXCLUDED.heading_path,
            chunk_title = EXCLUDED.chunk_title,
            content = EXCLUDED.content,
            metadata = EXCLUDED.metadata,
            embed_text = EXCLUDED.embed_text,
            embedding = EXCLUDED.embedding
        """
        template = "(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s::jsonb,%s,%s::vector)"
        execute_values(cur, sql, rows, template=template)


def insert_table_chunks(cur, rows: Sequence[Sequence[Any]]) -> None:
        """Insert or update `table_chunks` rows in bulk using execute_values."""
        sql = """
        INSERT INTO table_chunks (
            tenant_id, document_id, version,
            chunk_index, page_start, page_end,
            table_key, table_title, table_profile, key_terms, fields, table_capabilities,
            raw_table_md, raw_table_json,
            searchable_text, lexical_text, metadata,
            embedding
        )
        VALUES %s
        ON CONFLICT (tenant_id, document_id, version, chunk_index) DO UPDATE
        SET
            page_start = EXCLUDED.page_start,
            page_end = EXCLUDED.page_end,
            table_key = EXCLUDED.table_key,
            table_title = EXCLUDED.table_title,
            table_profile = EXCLUDED.table_profile,
            key_terms = EXCLUDED.key_terms,
            fields = EXCLUDED.fields,
            table_capabilities = EXCLUDED.table_capabilities,
            raw_table_md = EXCLUDED.raw_table_md,
            raw_table_json = EXCLUDED.raw_table_json,
            searchable_text = EXCLUDED.searchable_text,
            lexical_text = EXCLUDED.lexical_text,
            metadata = EXCLUDED.metadata,
            embedding = EXCLUDED.embedding
        """
        template = "(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s::jsonb,%s,%s,%s::jsonb,%s::vector)"
        execute_values(cur, sql, rows, template=template)


def insert_image_chunks(cur, rows: Sequence[Sequence[Any]]) -> None:
        """Insert or update `image_chunks` rows in bulk using execute_values."""
        sql = """
        INSERT INTO image_chunks (
            tenant_id, document_id, version,
            chunk_index, page_start, page_end,
            heading_path, image_path, image_alt, image_caption, image_struct,
            embed_text, metadata,
            clip_embedding, text_embedding
        )
        VALUES %s
        ON CONFLICT (tenant_id, document_id, version, chunk_index) DO UPDATE
        SET
            page_start = EXCLUDED.page_start,
            page_end = EXCLUDED.page_end,
            heading_path = EXCLUDED.heading_path,
            image_path = EXCLUDED.image_path,
            image_alt = EXCLUDED.image_alt,
            image_caption = EXCLUDED.image_caption,
            image_struct = EXCLUDED.image_struct,
            embed_text = EXCLUDED.embed_text,
            metadata = EXCLUDED.metadata,
            clip_embedding = EXCLUDED.clip_embedding,
            text_embedding = EXCLUDED.text_embedding
        """
        template = "(%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s::vector,%s::vector)"
        execute_values(cur, sql, rows, template=template)


def insert_summary_chunk(cur, tenant_id: str, document_id: str, version: int, summary_text: str, metadata_json: str, embedding: str) -> None:
        """Insert or update a single row into `summary_chunks`.

        - `metadata_json` should be a JSON string (already dumped).
        - `embedding` should be the PostgreSQL vector literal string (e.g. "[0.1,0.2,...]").
        """
        cur.execute(
                """
                INSERT INTO summary_chunks (
                    tenant_id, document_id, version,
                    summary_text, searchable_text, metadata, embedding
                )
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::vector)
                ON CONFLICT (tenant_id, document_id) DO UPDATE
                SET
                    version = EXCLUDED.version,
                    summary_text = EXCLUDED.summary_text,
                    searchable_text = EXCLUDED.searchable_text,
                    metadata = EXCLUDED.metadata,
                    embedding = EXCLUDED.embedding,
                    updated_at = now()
                """,
                (tenant_id, document_id, version, summary_text, summary_text, metadata_json, embedding),
        )


def upsert_document_sum(
        cur,
        *,
        tenant_id: str,
        document_id: str,
        semantic_summary: str,
        retrieval_summary_json: str,
        metadata_json: str,
        summary_embedding: Optional[str],
        retrieval_embedding: Optional[str],
) -> None:
        """Upsert a row into document_sum.

        - `retrieval_summary_json` and `metadata_json` must be JSON strings.
        - `retrieval_embedding` should be a vector literal string or None.
        """
        cur.execute(
                """
                INSERT INTO document_sum (
                    tenant_id,
                    document_id,
                    semantic_summary,
                    retrieval_summary,
                    metadata,
                    summary_embedding,
                    retrieval_embedding,
                    updated_at
                ) VALUES (
                    %s,
                    %s,
                    %s,
                    %s::jsonb,
                    %s::jsonb,
                    %s,
                    %s,
                    now()
                )
                ON CONFLICT (tenant_id, document_id)
                DO UPDATE SET
                    semantic_summary = EXCLUDED.semantic_summary,
                    retrieval_summary = EXCLUDED.retrieval_summary,
                    metadata = EXCLUDED.metadata,
                    retrieval_embedding = EXCLUDED.retrieval_embedding,
                    updated_at = now();
                """,
                (
                        tenant_id,
                        document_id,
                        semantic_summary,
                        retrieval_summary_json,
                        metadata_json,
                        summary_embedding,
                        retrieval_embedding,
                ),
        )


# for document acl in webhook
def safe_insert_document_acl(
    cur,
    tenant_id: str,
    document_id: str,
    principal_type: str,
    principal_id: str,
    effect: str,
) -> bool:
    """Insert a row into `document_acl` using ON CONFLICT DO NOTHING.

    Returns True if the row was inserted, False if it already existed.
    """
    cur.execute(
        """
        INSERT INTO document_acl (tenant_id, document_id, principal_type, principal_id, effect)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (tenant_id, document_id, principal_type, principal_id) DO NOTHING
        RETURNING 1
        """,
        (tenant_id, document_id, principal_type, principal_id, effect),
    )
    row = cur.fetchone()
    return bool(row)


def upsert_document_acl(
    cur,
    tenant_id: str,
    document_id: str,
    principal_type: str,
    principal_id: str,
    effect: str,
) -> bool:
    """
    Insert or update a row in `document_acl` using ON CONFLICT DO UPDATE.
    If a row with the same tenant_id, document_id, principal_type, and principal_id already exists, it will update the effect and updated_at timestamp.
    """
    cur.execute(
        """
        INSERT INTO document_acl (
            tenant_id,
            document_id,
            principal_type,
            principal_id,
            effect,
            created_at,
            updated_at
        )
        VALUES (
            %s, %s, %s, %s, %s, now(), now()
        )
        ON CONFLICT (tenant_id, document_id, principal_type, principal_id)
        DO UPDATE SET
            effect = EXCLUDED.effect,
            updated_at = now()
        RETURNING 1;
        """,
        (tenant_id, document_id, principal_type, principal_id, effect)
    )
    row = cur.fetchone()
    return bool(row)


def delete_document_acl(cur, tenant_id: str, document_id: str, principal_type: str, principal_id: str) -> int:
        """Delete document_acl rows for the given tenant/document/principal.

        Returns the number of rows deleted (cur.rowcount).
        """
        cur.execute(
                """
                DELETE FROM document_acl
                WHERE tenant_id = %s AND document_id = %s
                    AND principal_type = %s AND principal_id = %s
                """,
                (tenant_id, document_id, principal_type, principal_id),
        )
        return cur.rowcount
    
    
# for keycloak user sync in webhook
def update_user(cur, user: dict):
    """將從 Keycloak 獲取的使用者資料更新到 PostgreSQL 資料庫中"""
    
    cur.execute(
        """
        INSERT INTO users (
            id, username, email, first_name, last_name,
            enabled, department, roles, raw, updated_at
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
        ON CONFLICT (id)
        DO UPDATE SET
            username = EXCLUDED.username,
            email = EXCLUDED.email,
            first_name = EXCLUDED.first_name,
            last_name = EXCLUDED.last_name,
            enabled = EXCLUDED.enabled,
            department = EXCLUDED.department,
            roles = EXCLUDED.roles,
            raw = EXCLUDED.raw,
            updated_at = now()  
        """,
        (
            user["id"],
            user["username"],
            user["email"],
            user["first_name"],
            user["last_name"],
            user["enabled"],
            user["department"],
            user["roles"],
            Json(user["raw"]),
        )
    )

def delete_user_principals(cur, tenant_id: str, user_id: str):
    """從 PostgreSQL 資料庫中刪除使用者的所有權限資料"""
    
    cur.execute(
        """
        DELETE FROM user_principal
        WHERE tenant_id = %s
            AND user_id = %s
        """,
        (tenant_id, user_id),
    )
    
def write_user_principal(cur, tenant_id: str, user_id: str, principal_type: str, principal_id: str):
    """將使用者的權限資料寫入 PostgreSQL 資料庫中"""
    
    cur.execute(
        """
        INSERT INTO user_principal (
            tenant_id,
            user_id,
            principal_type,
            principal_id
        )
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (tenant_id, user_id, principal_type, principal_id)
        DO UPDATE SET
            updated_at = now()
        """,
        (tenant_id, user_id, principal_type, principal_id),
    )
    
# for keycloak update
