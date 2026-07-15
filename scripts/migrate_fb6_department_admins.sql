-- =========================================================
-- FB-6 遷移：建立 department_admins / global_admins 並回填
-- =========================================================
-- 背景（D8/D9，2026-07-15 定案）：Keycloak 將與 HR 系統連動、群組樹不再
-- 手動維護，部門下不會有 KM 子群組。「部門管理員」改由平台自建表管理，
-- authz 的 owner-KM 捷徑語意不變，只是來源從 user_principal 的
-- ('role','dept:X:role:KM') principal 換成查 department_admins 表。
--
-- 用法（k8s dev，先演練再正式跑）：
--   kubectl -n <ns> exec -i <postgres-pod> -- \
--     psql -U <user> -d <db> -v ON_ERROR_STOP=1 < scripts/migrate_fb6_department_admins.sql
--
-- 冪等性：DDL 全部 IF NOT EXISTS；回填為 INSERT ... ON CONFLICT DO NOTHING，
-- 重跑不會產生重複資料。已被手動移除的管理員「不會」被本腳本加回去——
-- 回填只針對 user_principal 現存的 KM role rows（dev Keycloak 目前還是
-- 舊結構有 KM 子群組；HR 連動是未來狀態，屆時 webhook 不再產生 role rows，
-- 本腳本自然無新資料可回填）。
--
-- 全域管理員（global_admins）不回填：第一位需手動 seed（D9），例如
--   INSERT INTO global_admins (tenant_id, user_id) VALUES ('firdi', '<user uuid>');

BEGIN;

-- ---------- DDL（與 deployments/docker/postgres/init/01_schema.sql 同步） ----------

CREATE TABLE IF NOT EXISTS department_admins (
  tenant_id   TEXT NOT NULL,
  department  TEXT NOT NULL,               -- 與 nodes.owner_department_id 同一套值
  user_id     UUID NOT NULL,
  created_by  UUID,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT pk_department_admins PRIMARY KEY (tenant_id, department, user_id)
);

CREATE INDEX IF NOT EXISTS idx_department_admins_user
  ON department_admins(tenant_id, user_id);

CREATE TABLE IF NOT EXISTS global_admins (
  tenant_id   TEXT NOT NULL,
  user_id     UUID NOT NULL,
  created_by  UUID,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT pk_global_admins PRIMARY KEY (tenant_id, user_id)
);

-- ---------- 回填 ----------

DO $$
DECLARE
  inserted_count integer;
  global_count   integer;
BEGIN
  -- user_principal 的 ('role','dept:X:role:KM') rows -> department_admins。
  -- 部門名直接取 role principal 中段（dev 現況是帶前綴的 'dept-A' 這種值，
  -- 與 nodes.owner_department_id 同一套，回填後 owner-KM 捷徑無縫接軌）。
  WITH backfill AS (
    INSERT INTO department_admins (tenant_id, department, user_id)
    SELECT DISTINCT
      up.tenant_id,
      substring(up.principal_id FROM '^dept:(.+):role:KM$'),
      up.user_id
    FROM user_principal up
    WHERE up.principal_type = 'role'
      AND up.principal_id ~ '^dept:(.+):role:KM$'
    ON CONFLICT DO NOTHING
    RETURNING 1
  )
  SELECT count(*) INTO inserted_count FROM backfill;

  SELECT count(*) INTO global_count FROM global_admins;

  RAISE NOTICE 'FB-6 回填完成：本次新增 % 位部門管理員（department_admins 共 % 列）',
    inserted_count, (SELECT count(*) FROM department_admins);

  IF global_count = 0 THEN
    RAISE NOTICE '提醒：global_admins 目前是空的，請手動 seed 第一位全域管理員（見腳本開頭註解）';
  END IF;
END $$;

COMMIT;

-- 驗證查詢（遷移後手動執行）：
--   對照舊 role rows：
--     SELECT tenant_id, department, user_id FROM department_admins
--     ORDER BY tenant_id, department, user_id;
--     SELECT tenant_id, principal_id, user_id FROM user_principal
--     WHERE principal_type = 'role' AND principal_id LIKE 'dept:%:role:KM'
--     ORDER BY tenant_id, principal_id, user_id;
