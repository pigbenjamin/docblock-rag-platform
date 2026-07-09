-- ============================================================
-- 遷移既有資料庫的 embedding 欄位到 768-dim（embeddinggemma-300m）
-- 舊向量與新模型不相容，一律清空；跑完後執行 scripts/reembed.py 重嵌入。
-- clip_embedding vector(768) 是 CLIP 圖像向量，與文字 embedding model 無關，不動。
--
-- Usage:
--   psql "$PG_DSN" -f scripts/migrate_embedding_768.sql
-- ============================================================

BEGIN;

-- 1) 先移除向量索引（欄位型別變更前必須先移除）
DROP INDEX IF EXISTS idx_text_chunks_embedding_hnsw;
DROP INDEX IF EXISTS idx_table_chunks_embedding_hnsw;
DROP INDEX IF EXISTS idx_image_chunks_text_hnsw;
DROP INDEX IF EXISTS idx_summary_chunks_embedding_hnsw;
DROP INDEX IF EXISTS idx_document_sum_retrieval_emb_hnsw;

-- 2) 變更維度並丟棄舊向量（USING NULL 使任何舊維度都能轉換）
ALTER TABLE text_chunks    ALTER COLUMN embedding           TYPE vector(768) USING NULL;
ALTER TABLE table_chunks   ALTER COLUMN embedding           TYPE vector(768) USING NULL;
ALTER TABLE image_chunks   ALTER COLUMN text_embedding      TYPE vector(768) USING NULL;
ALTER TABLE summary_chunks ALTER COLUMN embedding           TYPE vector(768) USING NULL;
ALTER TABLE document_sum   ALTER COLUMN retrieval_embedding TYPE vector(768) USING NULL;
ALTER TABLE document_sum   ALTER COLUMN summary_embedding   TYPE vector(768) USING NULL;

-- 3) 重建向量索引
CREATE INDEX IF NOT EXISTS idx_text_chunks_embedding_hnsw
  ON text_chunks USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_table_chunks_embedding_hnsw
  ON table_chunks USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_image_chunks_text_hnsw
  ON image_chunks USING hnsw (text_embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_summary_chunks_embedding_hnsw
  ON summary_chunks USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_document_sum_retrieval_emb_hnsw
  ON document_sum USING hnsw (retrieval_embedding vector_cosine_ops);

COMMIT;
