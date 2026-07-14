-- =========================================================
-- FB-1 遷移：建立 nodes / acl_entries / audit_logs 並回填既有資料
-- =========================================================
-- File Browser 整合第一階段。設計決策：
--   D2 Public = 全部門 allow browse/query/read 的根資料夾
--   D3 document 節點 id = documents.document_id（單一 UUID）
--   D6 硬刪（nodes 無 deleted_at）
--
-- 用法（k8s dev，先演練再正式跑）：
--   kubectl -n <ns> exec -i <postgres-pod> -- \
--     psql -U <user> -d <db> -v ON_ERROR_STOP=1 < scripts/migrate_fb1_nodes_acl.sql
--
-- 冪等性：DDL 全部 IF NOT EXISTS；回填以 tenant 為單位——已有任何 nodes
-- 的 tenant 整個跳過，重跑不會產生重複資料，之後有新 tenant 也可安全重跑。
-- 全程單一 transaction，任何錯誤即整體回滾。
--
-- ACL 轉譯規則（document_acl -> acl_entries）：
--   dept detail（owner）      -> 不搬（資料夾繼承 + owner_department_id 規則涵蓋）
--   dept detail（非 owner）   -> allow browse/query/read/manage_acl（共同管理部門）
--   dept summary              -> allow browse/query/read（分級制廢除，allow = 看全文）
--   user detail/summary       -> allow browse/query/read
--   任何 deny                 -> deny browse/query/read
--   principal_type = 'role'   -> 不轉譯（現行系統不會產生，出現時印 NOTICE）

BEGIN;

-- ---------- DDL（與 deployments/docker/postgres/init/01_schema.sql 同步） ----------

CREATE TABLE IF NOT EXISTS nodes (
  id                   UUID NOT NULL,
  tenant_id            TEXT NOT NULL,
  parent_id            UUID REFERENCES nodes(id) ON DELETE CASCADE,
  node_type            TEXT NOT NULL CHECK (node_type IN ('folder', 'document')),
  name                 TEXT NOT NULL,
  owner_department_id  TEXT NOT NULL,
  inherit_acl          BOOLEAN NOT NULL DEFAULT true,
  permission_revision  BIGINT NOT NULL DEFAULT 1,
  path_cache           TEXT,
  created_by           UUID,
  updated_by           UUID,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT pk_nodes PRIMARY KEY (id),
  CONSTRAINT chk_nodes_document_has_parent
    CHECK (node_type <> 'document' OR parent_id IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_nodes_parent_name
  ON nodes(tenant_id, parent_id, name) WHERE parent_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_nodes_root_name
  ON nodes(tenant_id, name) WHERE parent_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_nodes_parent
  ON nodes(tenant_id, parent_id);

CREATE INDEX IF NOT EXISTS idx_nodes_owner_department
  ON nodes(tenant_id, owner_department_id);

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

CREATE TABLE IF NOT EXISTS audit_logs (
  id             BIGSERIAL PRIMARY KEY,
  tenant_id      TEXT NOT NULL,
  event_type     TEXT NOT NULL,
  actor_id       UUID,
  resource_type  TEXT NOT NULL,
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

-- ---------- 回填 ----------

DO $$
DECLARE
  orphan_count integer;
BEGIN
  -- 回填對象 = 出現在 documents / user_principal、且還沒有任何 nodes 的
  -- tenant。已回填過的 tenant 自動排除（冪等），其他 tenant 的既有資料不受影響。
  CREATE TEMP TABLE _fb1_tenants ON COMMIT DROP AS
  SELECT t.tenant_id
  FROM (
    SELECT DISTINCT tenant_id FROM documents
    UNION
    SELECT DISTINCT tenant_id FROM user_principal
  ) t
  WHERE NOT EXISTS (SELECT 1 FROM nodes n WHERE n.tenant_id = t.tenant_id);

  IF NOT EXISTS (SELECT 1 FROM _fb1_tenants) THEN
    RAISE NOTICE '所有 tenant 都已有 nodes 資料，跳過回填';
    RETURN;
  END IF;

  -- 前置檢查：每份文件必須至少有一個 detail 部門（= 管理部門），
  -- 否則無法決定它落在哪個部門資料夾。有孤兒就整體中止，先人工處理。
  SELECT count(*) INTO orphan_count
  FROM documents d
  WHERE d.tenant_id IN (SELECT tenant_id FROM _fb1_tenants)
    AND NOT EXISTS (
      SELECT 1 FROM document_acl a
      WHERE a.tenant_id = d.tenant_id AND a.document_id = d.document_id
        AND a.principal_type = 'department' AND a.effect = 'detail'
    );
  IF orphan_count > 0 THEN
    RAISE EXCEPTION 'FB-1 回填中止：% 份文件沒有 detail 部門 ACL。先查明：SELECT document_id, title FROM documents d WHERE NOT EXISTS (SELECT 1 FROM document_acl a WHERE a.tenant_id=d.tenant_id AND a.document_id=d.document_id AND a.principal_type=''department'' AND a.effect=''detail'')', orphan_count;
  END IF;

  -- 1) 部門根資料夾。
  --    部門集合 = user_principal 與 document_acl 中出現過的 department，
  --    排除 'Public'（Public 不是部門，見步驟 2）。
  INSERT INTO nodes (id, tenant_id, parent_id, node_type, name,
                     owner_department_id, inherit_acl, path_cache)
  SELECT gen_random_uuid(), t.tenant_id, NULL, 'folder', t.dept,
         t.dept, false, '/' || t.dept
  FROM (
    SELECT DISTINCT tenant_id, principal_id AS dept
    FROM user_principal
    WHERE principal_type = 'department'
    UNION
    SELECT DISTINCT tenant_id, principal_id
    FROM document_acl
    WHERE principal_type = 'department'
  ) t
  WHERE t.dept <> 'Public'
    AND t.tenant_id IN (SELECT tenant_id FROM _fb1_tenants);

  -- 部門成員對自己部門的資料夾（含底下所有節點）可 browse/query/read。
  -- KM 的管理權不寫 ACL：由 owner_department_id + '/{部門}/KM' 群組判定。
  INSERT INTO acl_entries (tenant_id, node_id, subject_type, subject_id,
                           action, effect, inherit_to_children)
  SELECT n.tenant_id, n.id, 'department', n.name, a.action, 'allow', true
  FROM nodes n
  CROSS JOIN (VALUES ('browse'), ('query'), ('read')) AS a(action)
  WHERE n.parent_id IS NULL AND n.node_type = 'folder' AND n.name <> 'Public'
    AND n.tenant_id IN (SELECT tenant_id FROM _fb1_tenants);

  -- 2) Public 根資料夾：所有部門 allow browse/query/read（D2）。
  --    owner 掛 'Public'：在 Keycloak 的 Public 群組沒有 KM 子群組的現況下，
  --    等同「無人可上傳/改權限」，之後由 Global Admin 或指定部門接管。
  INSERT INTO nodes (id, tenant_id, parent_id, node_type, name,
                     owner_department_id, inherit_acl, path_cache)
  SELECT gen_random_uuid(), t.tenant_id, NULL, 'folder', 'Public',
         'Public', false, '/Public'
  FROM _fb1_tenants t;

  INSERT INTO acl_entries (tenant_id, node_id, subject_type, subject_id,
                           action, effect, inherit_to_children)
  SELECT p.tenant_id, p.id, 'department', d.name, a.action, 'allow', true
  FROM nodes p
  JOIN nodes d ON d.tenant_id = p.tenant_id
              AND d.parent_id IS NULL AND d.node_type = 'folder'
              AND d.name <> 'Public'
  CROSS JOIN (VALUES ('browse'), ('query'), ('read')) AS a(action)
  WHERE p.parent_id IS NULL AND p.name = 'Public'
    AND p.tenant_id IN (SELECT tenant_id FROM _fb1_tenants);

  -- 3) 文件節點：id = document_id，掛在 owner 部門資料夾下。
  --    owner = detail 部門中字典序最小者（多部門共管時取其一，其餘見 4a）。
  --    同資料夾撞名時，第 2 份起在名稱後加 document_id 前 8 碼。
  WITH owner AS (
    SELECT a.tenant_id, a.document_id, min(a.principal_id) AS owner_dept
    FROM document_acl a
    WHERE a.principal_type = 'department' AND a.effect = 'detail'
      AND a.tenant_id IN (SELECT tenant_id FROM _fb1_tenants)
    GROUP BY a.tenant_id, a.document_id
  ),
  named AS (
    SELECT d.tenant_id, d.document_id, d.created_by, o.owner_dept,
           coalesce(nullif(d.title, ''), nullif(d.original_filename, ''),
                    d.document_id::text) AS base_name,
           row_number() OVER (
             PARTITION BY d.tenant_id, o.owner_dept,
                          coalesce(nullif(d.title, ''), nullif(d.original_filename, ''),
                                   d.document_id::text)
             ORDER BY d.created_at, d.document_id
           ) AS rn
    FROM documents d
    JOIN owner o ON o.tenant_id = d.tenant_id AND o.document_id = d.document_id
  )
  INSERT INTO nodes (id, tenant_id, parent_id, node_type, name,
                     owner_department_id, inherit_acl, created_by, path_cache)
  SELECT nm.document_id, nm.tenant_id, f.id, 'document',
         CASE WHEN nm.rn = 1 THEN nm.base_name
              ELSE nm.base_name || ' (' || left(nm.document_id::text, 8) || ')' END,
         nm.owner_dept, true, nm.created_by,
         '/' || nm.owner_dept || '/' ||
         CASE WHEN nm.rn = 1 THEN nm.base_name
              ELSE nm.base_name || ' (' || left(nm.document_id::text, 8) || ')' END
  FROM named nm
  JOIN nodes f ON f.tenant_id = nm.tenant_id AND f.parent_id IS NULL
              AND f.node_type = 'folder' AND f.name = nm.owner_dept;

  -- 4a) 非 owner 的 detail 部門 = 共同管理部門
  INSERT INTO acl_entries (tenant_id, node_id, subject_type, subject_id,
                           action, effect, inherit_to_children)
  SELECT a.tenant_id, a.document_id, 'department', a.principal_id,
         act.action, 'allow', true
  FROM document_acl a
  JOIN nodes n ON n.id = a.document_id AND n.tenant_id = a.tenant_id
  CROSS JOIN (VALUES ('browse'), ('query'), ('read'), ('manage_acl')) AS act(action)
  WHERE a.principal_type = 'department' AND a.effect = 'detail'
    AND a.principal_id <> n.owner_department_id
    AND a.tenant_id IN (SELECT tenant_id FROM _fb1_tenants)
  ON CONFLICT DO NOTHING;

  -- 4b) summary 部門 + detail/summary 使用者 -> allow browse/query/read
  INSERT INTO acl_entries (tenant_id, node_id, subject_type, subject_id,
                           action, effect, inherit_to_children)
  SELECT DISTINCT a.tenant_id, a.document_id, a.principal_type, a.principal_id,
         act.action, 'allow', true
  FROM document_acl a
  JOIN nodes n ON n.id = a.document_id AND n.tenant_id = a.tenant_id
  CROSS JOIN (VALUES ('browse'), ('query'), ('read')) AS act(action)
  WHERE ((a.principal_type = 'department' AND a.effect = 'summary')
     OR (a.principal_type = 'user' AND a.effect IN ('detail', 'summary')))
    AND a.tenant_id IN (SELECT tenant_id FROM _fb1_tenants)
  ON CONFLICT DO NOTHING;

  -- 4c) deny -> deny browse/query/read
  INSERT INTO acl_entries (tenant_id, node_id, subject_type, subject_id,
                           action, effect, inherit_to_children)
  SELECT a.tenant_id, a.document_id, a.principal_type, a.principal_id,
         act.action, 'deny', true
  FROM document_acl a
  JOIN nodes n ON n.id = a.document_id AND n.tenant_id = a.tenant_id
  CROSS JOIN (VALUES ('browse'), ('query'), ('read')) AS act(action)
  WHERE a.effect = 'deny' AND a.principal_type IN ('department', 'user')
    AND a.tenant_id IN (SELECT tenant_id FROM _fb1_tenants)
  ON CONFLICT DO NOTHING;

  -- 5) 現行系統不會產生 role 型 ACL；若出現，提醒人工確認
  PERFORM 1 FROM document_acl WHERE principal_type = 'role' LIMIT 1;
  IF FOUND THEN
    RAISE NOTICE 'document_acl 有 principal_type=role 的列，本次未轉譯，請人工確認';
  END IF;

  RAISE NOTICE 'FB-1 回填完成（tenant: %）：% 個資料夾、% 個文件節點、% 條 ACL',
    (SELECT string_agg(tenant_id, ', ') FROM _fb1_tenants),
    (SELECT count(*) FROM nodes WHERE node_type = 'folder'
       AND tenant_id IN (SELECT tenant_id FROM _fb1_tenants)),
    (SELECT count(*) FROM nodes WHERE node_type = 'document'
       AND tenant_id IN (SELECT tenant_id FROM _fb1_tenants)),
    (SELECT count(*) FROM acl_entries
       WHERE tenant_id IN (SELECT tenant_id FROM _fb1_tenants));
END $$;

COMMIT;

-- 驗證查詢（遷移後手動執行）：
--   目錄樹總覽：
--     SELECT n.path_cache, n.node_type, n.owner_department_id
--     FROM nodes n ORDER BY n.path_cache;
--   每份文件的 ACL 對照舊表：
--     SELECT n.name, e.subject_type, e.subject_id, e.action, e.effect
--     FROM acl_entries e JOIN nodes n ON n.id = e.node_id
--     WHERE n.node_type = 'document' ORDER BY n.name, e.subject_type, e.subject_id, e.action;
