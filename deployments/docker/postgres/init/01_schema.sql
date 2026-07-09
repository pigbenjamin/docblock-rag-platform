-- This SQL script defines a new PostgreSQL schema for a multi-tenant and summary-chunks

-- =========================================================
-- 00_extensions.sql
-- =========================================================
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
-- 若你未來想在DB端 gen_random_uuid() 再打開 pgcrypto
-- CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- =========================================================
-- 01_documents.sql  (document_id 為唯一識別碼 + version + content_sha256)
-- =========================================================
CREATE TABLE IF NOT EXISTS documents (
  tenant_id      TEXT NOT NULL,
  document_id    UUID NOT NULL,                  -- 唯一識別碼，上傳時由應用端生成（不在DB default）

  source_path    TEXT NOT NULL,
  md_path        TEXT,
  title          TEXT,
  original_filename TEXT,                        -- 使用者上傳時的原始檔名
  file_size      BIGINT,                          -- bytes
  mime_type      TEXT,
  external_ref   TEXT,                            -- 選填：外部系統代碼（如 Outline），僅供參考，不參與唯一性/版本判斷

  created_by     UUID,                            -- 上傳者 user_id（Keycloak sub）
  status         TEXT NOT NULL DEFAULT 'ready'    -- processing | ready | failed
                   CHECK (status IN ('processing', 'ready', 'failed')),

  active_version INT  NOT NULL DEFAULT 1,        -- 目前啟用版本
  content_sha256 TEXT NOT NULL,                  -- 原始檔 bytes 的 sha256 (hex)
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT pk_documents PRIMARY KEY (document_id),
  CONSTRAINT uq_documents_tenant_document UNIQUE (tenant_id, document_id)

  -- 可選：若你要同一路徑視為同一份文件（通常建議打開）
  -- ,CONSTRAINT uq_documents_tenant_source UNIQUE (tenant_id, source_path)
);

CREATE INDEX IF NOT EXISTS idx_documents_tenant
  ON documents(tenant_id);

CREATE INDEX IF NOT EXISTS idx_documents_tenant_source
  ON documents(tenant_id, source_path);

CREATE INDEX IF NOT EXISTS idx_documents_tenant_sha
  ON documents(tenant_id, content_sha256);

CREATE INDEX IF NOT EXISTS idx_documents_tenant_external_ref
  ON documents(tenant_id, external_ref) WHERE external_ref IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_documents_created_by
  ON documents(tenant_id, created_by);

-- =========================================================
-- =========================================================
CREATE TABLE IF NOT EXISTS text_chunks (
  id           BIGSERIAL PRIMARY KEY,
  tenant_id    TEXT NOT NULL,
  document_id  UUID NOT NULL,
  version      INT  NOT NULL,                    -- 新增：版本

  chunk_index  INT NOT NULL,
  page_start   INT,
  page_end     INT,
  char_start   INT,
  char_end     INT,
  heading_path JSONB,
  chunk_title  TEXT,
  content      TEXT NOT NULL,
  metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
  embed_text   TEXT NOT NULL,
  embedding    vector(768),
  created_at   TIMESTAMPTZ DEFAULT now(),

  CONSTRAINT fk_text_chunks_document
    FOREIGN KEY (tenant_id, document_id)
    REFERENCES documents(tenant_id, document_id)
    ON DELETE CASCADE,

  CONSTRAINT uq_text_chunks_doc_ver_chunk
    UNIQUE (tenant_id, document_id, version, chunk_index)
);

-- 查詢/回表用 btree（強烈建議）
CREATE INDEX IF NOT EXISTS idx_text_chunks_tenant_doc_ver
  ON text_chunks(tenant_id, document_id, version);

-- HNSW for text embedding（保留你原本）
CREATE INDEX IF NOT EXISTS idx_text_chunks_embedding_hnsw
  ON text_chunks USING hnsw (embedding vector_cosine_ops);

-- =========================================================
-- 03_table_chunks.sql  (保留你原本欄位 + 改 UUID/tenant + 加 version)
-- =========================================================
CREATE TABLE IF NOT EXISTS table_chunks (
  id               BIGSERIAL PRIMARY KEY,
  tenant_id        TEXT NOT NULL,
  document_id      UUID NOT NULL,
  version          INT  NOT NULL,                -- 新增：版本

  chunk_index      INT NOT NULL,
  page_start       INT,
  page_end         INT,

  table_key        TEXT,
  table_title      TEXT,
  table_profile    JSONB,
  key_terms        JSONB,
  fields           JSONB,
  table_capabilities JSONB,

  raw_table_md     TEXT,
  raw_table_json   JSONB,

  searchable_text  TEXT NOT NULL,                -- embed_text
  lexical_text     TEXT NOT NULL,                -- for tsvector/trgm
  tsv              tsvector GENERATED ALWAYS AS (to_tsvector('simple', lexical_text)) STORED,

  metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
  embedding        vector(768),
  created_at       TIMESTAMPTZ DEFAULT now(),

  CONSTRAINT fk_table_chunks_document
    FOREIGN KEY (tenant_id, document_id)
    REFERENCES documents(tenant_id, document_id)
    ON DELETE CASCADE,

  CONSTRAINT uq_table_chunks_doc_ver_chunk
    UNIQUE (tenant_id, document_id, version, chunk_index)
);

-- 先過濾用 btree（強烈建議）
CREATE INDEX IF NOT EXISTS idx_table_chunks_tenant_doc_ver
  ON table_chunks(tenant_id, document_id, version);

-- 若常以 chunk_index 拉回顯示排序（可選但實用）
CREATE INDEX IF NOT EXISTS idx_table_chunks_tenant_doc_ver_chunk
  ON table_chunks(tenant_id, document_id, version, chunk_index);

-- HNSW for table embedding（保留你原本）
CREATE INDEX IF NOT EXISTS idx_table_chunks_embedding_hnsw
  ON table_chunks USING hnsw (embedding vector_cosine_ops);

-- Lexical indexes（保留你原本）
CREATE INDEX IF NOT EXISTS idx_table_chunks_tsv_gin
  ON table_chunks USING gin (tsv);

CREATE INDEX IF NOT EXISTS idx_table_chunks_lexical_trgm
  ON table_chunks USING gin (lexical_text gin_trgm_ops);

-- =========================================================
-- 04_image_chunks.sql  (保留你原本欄位 + 改 UUID/tenant + 加 version)
-- =========================================================
CREATE TABLE IF NOT EXISTS image_chunks (
  id             BIGSERIAL PRIMARY KEY,
  tenant_id      TEXT NOT NULL,
  document_id    UUID NOT NULL,
  version        INT  NOT NULL,                  -- 新增：版本

  chunk_index    INT NOT NULL,
  page_start     INT,
  page_end       INT,
  heading_path   JSONB,

  image_path     TEXT NOT NULL,
  image_alt      TEXT,
  image_caption  TEXT,
  image_struct   JSONB,

  embed_text     TEXT NOT NULL,                  -- text embedding source (caption + surrounding)
  metadata       JSONB NOT NULL DEFAULT '{}'::jsonb,

  -- CLIP image embedding for cross-modal retrieval
  clip_embedding vector(768),

  -- optional: text embedding for normal text retrieval
  text_embedding vector(768),

  created_at     TIMESTAMPTZ DEFAULT now(),

  CONSTRAINT fk_image_chunks_document
    FOREIGN KEY (tenant_id, document_id)
    REFERENCES documents(tenant_id, document_id)
    ON DELETE CASCADE,

  CONSTRAINT uq_image_chunks_doc_ver_chunk
    UNIQUE (tenant_id, document_id, version, chunk_index)
);

-- 先過濾用 btree（強烈建議）
CREATE INDEX IF NOT EXISTS idx_image_chunks_tenant_doc_ver
  ON image_chunks(tenant_id, document_id, version);

-- HNSW indexes（保留你原本）
CREATE INDEX IF NOT EXISTS idx_image_chunks_clip_hnsw
  ON image_chunks USING hnsw (clip_embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_image_chunks_text_hnsw
  ON image_chunks USING hnsw (text_embedding vector_cosine_ops);

-- =========================================================
-- 05_user_principal.sql  (tenant-aware)
-- =========================================================
CREATE TABLE user_principal (
    tenant_id TEXT NOT NULL,
    user_id UUID NOT NULL,
    principal_type TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (
        tenant_id,
        user_id,
        principal_type,
        principal_id
    )
);

-- 查某個使用者有哪些 principal
CREATE INDEX idx_user_principal_lookup
ON user_principal (tenant_id, user_id);

-- 反查某個 principal 底下有哪些 user
CREATE INDEX idx_user_principal_principal
ON user_principal (tenant_id, principal_type, principal_id);

--CREATE TABLE IF NOT EXISTS user_principal (
--  tenant_id      TEXT NOT NULL,
--  user_id        TEXT NOT NULL,
--  principal_type TEXT NOT NULL CHECK (principal_type IN ('department','role','user')),
--  principal_id   TEXT NOT NULL,
--  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
--
--  PRIMARY KEY (tenant_id, user_id, principal_type, principal_id)
--);
--
--CREATE INDEX IF NOT EXISTS idx_user_principal_user
--  ON user_principal(tenant_id, user_id);
--
--CREATE INDEX IF NOT EXISTS idx_user_principal_principal
--  ON user_principal(tenant_id, principal_type, principal_id);

-- =========================================================
-- 06_document_acl.sql  (tenant-aware + UUID FK)
-- =========================================================
CREATE TABLE document_acl (
    tenant_id TEXT NOT NULL,
    document_id UUID NOT NULL,

    principal_type TEXT NOT NULL
        CHECK (principal_type IN ('user', 'department', 'role')),

    principal_id TEXT NOT NULL,

    effect TEXT NOT NULL
        CHECK (effect IN ('detail', 'summary', 'deny')),

    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),

    PRIMARY KEY (
        tenant_id,
        document_id,
        principal_type,
        principal_id
    ),

    FOREIGN KEY (tenant_id, document_id)
    REFERENCES documents (tenant_id, document_id)
    ON DELETE CASCADE
);

CREATE INDEX idx_document_acl_principal
ON document_acl (
    tenant_id,
    principal_type,
    principal_id
);

CREATE INDEX idx_document_acl_document
ON document_acl (
    tenant_id,
    document_id
);

--CREATE TABLE IF NOT EXISTS document_acl (
--  id             BIGSERIAL PRIMARY KEY,
--  tenant_id      TEXT NOT NULL,
--  document_id    UUID NOT NULL,
--
--  principal_type TEXT NOT NULL CHECK (principal_type IN ('department','role','user')),
--  principal_id   TEXT NOT NULL,
--
--  effect         TEXT NOT NULL CHECK (effect IN ('allow','deny')),
--  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
--
--  CONSTRAINT fk_document_acl_document
--    FOREIGN KEY (tenant_id, document_id)
--    REFERENCES documents(tenant_id, document_id)
--    ON DELETE CASCADE,
--
--  CONSTRAINT uq_document_acl
--    UNIQUE (tenant_id, document_id, principal_type, principal_id, effect)
--);
--
--CREATE INDEX IF NOT EXISTS idx_document_acl_lookup
--  ON document_acl(tenant_id, principal_type, principal_id, effect, document_id);
--
--CREATE INDEX IF NOT EXISTS idx_document_acl_doc
--  ON document_acl(tenant_id, document_id);


-- =========================================
-- 07_summary_chunks: one summary per document
-- =========================================

CREATE TABLE IF NOT EXISTS summary_chunks (
  tenant_id     text NOT NULL,
  document_id   uuid NOT NULL,
  version       INT  NOT NULL,  -- version of the summary

  -- the actual summary content used for RAG (summary-level)
  summary_text      text NOT NULL,

  -- text used for lexical search (can be same as summary_text for now)
  searchable_text   text NOT NULL,

  -- optional metadata: model name, prompt version, language, generated_at, etc.
  metadata      jsonb NOT NULL DEFAULT '{}'::jsonb,

  -- for vector search
  embedding     vector(768),

  updated_at    timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT pk_summary_chunks PRIMARY KEY (tenant_id, document_id),

  CONSTRAINT fk_summary_chunks_documents
    FOREIGN KEY (tenant_id, document_id)
    REFERENCES documents(tenant_id, document_id)
    ON DELETE CASCADE
);

-- Fast filtering by tenant/doc
CREATE INDEX IF NOT EXISTS idx_summary_chunks_tenant
  ON summary_chunks(tenant_id);

CREATE INDEX IF NOT EXISTS idx_summary_chunks_tenant_doc
  ON summary_chunks(tenant_id, document_id);

-- Optional: full-text search on summaries
CREATE INDEX IF NOT EXISTS idx_summary_chunks_tsv
  ON summary_chunks
  USING GIN (to_tsvector('simple', searchable_text));

-- Optional: pgvector index if you later add embedding
CREATE INDEX IF NOT EXISTS idx_summary_chunks_embedding_hnsw
  ON summary_chunks USING hnsw (embedding vector_cosine_ops);


-- =========================================
-- 08_document_sum: one summary per document
-- =========================================

  -- 需要 pgcrypto 與 pgvector（若你要用 UUID 產生與向量欄位）
-- CREATE EXTENSION IF NOT EXISTS pgcrypto;
-- CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS document_sum (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  tenant_id   text NOT NULL,
  document_id uuid NOT NULL,

  -- 給人看的語義摘要（不含數據/細節）
  semantic_summary   text NOT NULL,

  -- 檢索用摘要：建議 JSONB（topics/intents/keywords/偏好chunk類型）
  retrieval_summary  jsonb NOT NULL DEFAULT '{}'::jsonb,

  -- 模型/提示詞版本/語言/生成時間/lint flags/內容hash等
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

  -- 向量
  retrieval_embedding vector(768),
  summary_embedding   vector(768),

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT uq_document_sum UNIQUE (tenant_id, document_id),

  CONSTRAINT fk_document_sum_documents
    FOREIGN KEY (tenant_id, document_id)
    REFERENCES documents(tenant_id, document_id)
    ON DELETE CASCADE
);

-- 常用查詢
CREATE INDEX IF NOT EXISTS idx_document_sum_tenant
  ON document_sum(tenant_id);

CREATE INDEX IF NOT EXISTS idx_document_sum_tenant_doc
  ON document_sum(tenant_id, document_id);

-- JSONB 過濾（例如 topics/keywords）
CREATE INDEX IF NOT EXISTS idx_document_sum_retrieval_gin
  ON document_sum USING GIN (retrieval_summary);

-- Optional: FTS（若你想做 lexical search，可把 retrieval_summary.keywords/topics 串成 text 存進 metadata 或另建欄位）
CREATE INDEX IF NOT EXISTS idx_document_sum_tsv
  ON document_sum USING GIN (to_tsvector('simple', coalesce(metadata->>'searchable_text','')));

-- Optional: 向量索引（若你之後真的會用 summary 向量召回）
CREATE INDEX IF NOT EXISTS idx_document_sum_retrieval_emb_hnsw
  ON document_sum USING hnsw (retrieval_embedding vector_cosine_ops);


-- =========================================
-- 09_ingest_jobs: 持久化的上傳/ingest pipeline 任務狀態
-- =========================================
-- 取代 ingest-worker 原本的記憶體內 _jobs dict，重啟不遺失狀態。
-- document_id 不設 FK：job 建立時（admin-api 產生 document_id 當下）
-- documents row 通常還沒寫入，要等 ingest 階段才 INSERT。

CREATE TABLE IF NOT EXISTS ingest_jobs (
  job_id       UUID NOT NULL,
  tenant_id    TEXT NOT NULL,
  document_id  UUID NOT NULL,

  source_type  TEXT NOT NULL DEFAULT 'pdf',       -- pdf | md | docx | xlsx | pptx（供未來格式路由使用）
  stage        TEXT NOT NULL DEFAULT 'pending',   -- pending | marker | build_chunks | ingest | done | failed
  status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'running', 'done', 'failed')),
  detail       TEXT,                              -- 人類可讀訊息 / 錯誤內容

  created_by   UUID,                               -- 上傳者 user_id

  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT pk_ingest_jobs PRIMARY KEY (job_id)
);

CREATE INDEX IF NOT EXISTS idx_ingest_jobs_tenant_document
  ON ingest_jobs(tenant_id, document_id);

CREATE INDEX IF NOT EXISTS idx_ingest_jobs_status
  ON ingest_jobs(tenant_id, status);
