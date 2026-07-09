"""
重新計算所有 chunks 的 embedding（更換 embedding model / 維度後使用，
例如遷移到 embeddinggemma-300m 的 768-dim）
只處理 embedding IS NULL 的列；換模型時請先跑 scripts/migrate_embedding_768.sql
（會清空舊向量並調整欄位維度）。
Usage:
  LITELLM_BASE_URL=http://10.90.20.55:31800 \
  LITELLM_API_KEY=sk-litellm-internal \
  PG_DSN="postgresql://ai-x:changeme@localhost:5437/acl_FIRDI" \
  python3 scripts/reembed.py
"""

import os
import psycopg2
import psycopg2.extras
import requests
import time

PG_DSN        = os.environ["PG_DSN"]
LITELLM_URL   = os.environ.get("LITELLM_BASE_URL", "http://10.90.20.55:31800")
LITELLM_KEY   = os.environ.get("LITELLM_API_KEY",  "sk-litellm-internal")
EMBED_MODEL   = os.environ.get("EMBED_MODEL",       "embeddinggemma-300m")
DOC_PREFIX    = os.environ.get("EMBED_DOC_PREFIX",  "title: none | text: ")
BATCH_SIZE    = int(os.environ.get("BATCH_SIZE",    "10"))


def embed(texts: list[str]) -> list[list[float]]:
    r = requests.post(
        f"{LITELLM_URL}/v1/embeddings",
        headers={"Authorization": f"Bearer {LITELLM_KEY}"},
        json={"model": EMBED_MODEL, "input": [DOC_PREFIX + t for t in texts]},
        timeout=120,
    )
    r.raise_for_status()
    return [item["embedding"] for item in r.json()["data"]]


def vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in v) + "]"


def reembed_table(conn, table: str, text_col: str, embed_col: str, id_col: str = "id"):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"SELECT {id_col}, {text_col} FROM {table} WHERE {embed_col} IS NULL AND {text_col} IS NOT NULL")
    rows = cur.fetchall()
    total = len(rows)
    if total == 0:
        print(f"  {table}: 0 rows to embed, skip")
        return

    print(f"  {table}: {total} rows to embed ...")
    done = 0
    for i in range(0, total, BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        texts = [r[text_col] or "" for r in batch]
        ids   = [r[id_col] for r in batch]
        try:
            vecs = embed(texts)
        except Exception as e:
            print(f"    ❌ embed failed for batch {i}: {e}")
            continue

        for row_id, vec in zip(ids, vecs):
            cur.execute(
                f"UPDATE {table} SET {embed_col} = %s::{table}_{embed_col}_type WHERE {id_col} = %s",
                (vec_literal(vec), row_id),
            )
        conn.commit()
        done += len(batch)
        print(f"    {done}/{total} done")

    print(f"  {table}: ✅ complete")


def reembed_table_v2(conn, table: str, text_col: str, embed_col: str, id_col: str = "id"):
    """版本2：直接使用 vector literal cast"""
    cur = conn.cursor()
    cur.execute(f"SELECT {id_col}, {text_col} FROM {table} WHERE {embed_col} IS NULL AND {text_col} IS NOT NULL")
    rows = cur.fetchall()
    total = len(rows)
    if total == 0:
        print(f"  {table}: 0 rows, skip")
        return

    print(f"  {table}: {total} rows to embed ...")
    done = 0
    for i in range(0, total, BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        texts = [r[1] or "" for r in batch]
        ids   = [r[0] for r in batch]
        try:
            vecs = embed(texts)
        except Exception as e:
            print(f"    ❌ embed failed: {e}")
            continue

        for row_id, vec in zip(ids, vecs):
            cur.execute(
                f"UPDATE {table} SET {embed_col} = %s::vector WHERE {id_col} = %s",
                (vec_literal(vec), row_id),
            )
        conn.commit()
        done += len(batch)
        print(f"    {done}/{total}")

    print(f"  {table}: ✅")


if __name__ == "__main__":
    print(f"LITELLM_URL : {LITELLM_URL}")
    print(f"EMBED_MODEL : {EMBED_MODEL}")
    print(f"PG_DSN      : {PG_DSN[:40]}...")
    print()

    # 先測試 embed endpoint
    print("Testing embed endpoint ...")
    try:
        vecs = embed(["test"])
        print(f"✅ Embedding works, dim={len(vecs[0])}")
    except Exception as e:
        print(f"❌ Embedding failed: {e}")
        exit(1)

    conn = psycopg2.connect(PG_DSN)

    t0 = time.time()
    reembed_table_v2(conn, "text_chunks",    "content",      "embedding")
    reembed_table_v2(conn, "table_chunks",   "raw_table_md", "embedding")
    reembed_table_v2(conn, "summary_chunks", "summary_text", "embedding", id_col="document_id")
    # image_chunks.text_embedding: embed image_caption or embed_text
    reembed_table_v2(conn, "image_chunks",   "image_caption","text_embedding")

    conn.close()
    elapsed = time.time() - t0
    print(f"\nAll done in {elapsed:.1f}s")
