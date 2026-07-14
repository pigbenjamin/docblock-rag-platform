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
-- 09_ingest_jobs: 持久化的上傳/ingest pipeline 任務狀態
-- =========================================================
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

-- =========================================================
-- 10_nodes: 邏輯目錄樹（File Browser 整合 FB-1）
-- =========================================================
-- 前端目錄樹的權威來源：folder 與 document 都是一個 node。
-- document 節點的 id 直接沿用 documents.document_id（同一個 UUID，
-- 不另發 node_id），chunks/檢索/引用不需要多一層 join。
-- documents -> nodes 的 FK 於 FB-5 cutover 時再補：過渡期舊上傳流程
-- 尚未建 node，先加 FK 會擋掉現行寫入。
CREATE TABLE IF NOT EXISTS nodes (
  id                   UUID NOT NULL,
  tenant_id            TEXT NOT NULL,
  parent_id            UUID REFERENCES nodes(id) ON DELETE CASCADE,
  node_type            TEXT NOT NULL CHECK (node_type IN ('folder', 'document')),
  name                 TEXT NOT NULL,
  owner_department_id  TEXT NOT NULL,                  -- 預設管理責任部門；該部門 KM 可管理此節點
  inherit_acl          BOOLEAN NOT NULL DEFAULT true,  -- 本節點無可決定規則時是否繼續往 parent 找
  permission_revision  BIGINT NOT NULL DEFAULT 1,      -- ACL/搬移變更版號（If-Match optimistic locking 用）
  path_cache           TEXT,                           -- 顯示/除錯用快取；不是授權依據
  created_by           UUID,
  updated_by           UUID,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT pk_nodes PRIMARY KEY (id),
  CONSTRAINT chk_nodes_document_has_parent
    CHECK (node_type <> 'document' OR parent_id IS NOT NULL)
);

-- 同一資料夾下名稱唯一；root 層（parent NULL）另用 partial index
CREATE UNIQUE INDEX IF NOT EXISTS uq_nodes_parent_name
  ON nodes(tenant_id, parent_id, name) WHERE parent_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_nodes_root_name
  ON nodes(tenant_id, name) WHERE parent_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_nodes_parent
  ON nodes(tenant_id, parent_id);

CREATE INDEX IF NOT EXISTS idx_nodes_owner_department
  ON nodes(tenant_id, owner_department_id);

-- =========================================================
-- 11_acl_entries: 節點 ACL（action × allow/deny + 繼承）
-- =========================================================
-- 取代 document_acl 的 detail/summary/deny 三級制（document_acl 於
-- FB-5 cutover 後移除）。subject_id 慣例：department -> Keycloak 群組名
-- （如 'A'）；user -> Keycloak sub 的裸 UUID 字串。
CREATE TABLE IF NOT EXISTS acl_entries (
  tenant_id            TEXT NOT NULL,
  node_id              UUID NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
  subject_type         TEXT NOT NULL CHECK (subject_type IN ('department', 'user', 'role')),
  subject_id           TEXT NOT NULL,
  action               TEXT NOT NULL CHECK (action IN
                         ('browse', 'query', 'read', 'upload',
                          'update', 'delete', 'move', 'manage_acl')),
  effect               TEXT NOT NULL CHECK (effect IN ('allow', 'deny')),
  inherit_to_children  BOOLEAN NOT NULL DEFAULT true,
  created_by           UUID,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT pk_acl_entries PRIMARY KEY (tenant_id, node_id, subject_type, subject_id, action)
);

CREATE INDEX IF NOT EXISTS idx_acl_entries_node
  ON acl_entries(tenant_id, node_id);

CREATE INDEX IF NOT EXISTS idx_acl_entries_subject
  ON acl_entries(tenant_id, subject_type, subject_id);

-- =========================================================
-- 12_audit_logs: 稽核（v1 最小版：寫入型事件 + 下載）
-- =========================================================
-- event_type 慣例：document.upload / document.download / document.delete /
-- node.create / node.rename / node.move / node.delete / acl.update
CREATE TABLE IF NOT EXISTS audit_logs (
  id             BIGSERIAL PRIMARY KEY,
  tenant_id      TEXT NOT NULL,
  event_type     TEXT NOT NULL,
  actor_id       UUID,                        -- NULL = 系統或 admin-secret bypass
  resource_type  TEXT NOT NULL,               -- node | document | job
  resource_id    TEXT NOT NULL,
  before_data    JSONB,
  after_data     JSONB,
  result         TEXT NOT NULL DEFAULT 'ok'
                   CHECK (result IN ('ok', 'denied', 'failed')),
  reason         TEXT,
  request_id     TEXT,
  client_ip      INET,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_logs_resource
  ON audit_logs(tenant_id, resource_type, resource_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_logs_actor
  ON audit_logs(tenant_id, actor_id, created_at DESC);
