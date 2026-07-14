-- =========================================================
-- FB-5 遷移：drop 舊 ACL/摘要表 + 清懸空欄位
-- =========================================================
-- 拿掉 document_acl（已被 nodes/acl_entries 取代，見
-- scripts/migrate_fb1_nodes_acl.sql）與 summary_chunks/document_sum
-- （D5：摘要分級授權整條拿掉，這兩張表的寫入邏輯其實從未真正執行過）；
-- 順手清掉 documents.md_path——它永遠指向 job 完成後就被
-- shutil.rmtree 清掉的暫存路徑，從來沒有程式碼讀回這個值。
--
-- 前置條件（腳本會自行檢查，不符合就中止不動任何東西）：
--   1. 每個 tenant 若 documents 有資料，nodes 也必須有資料——代表
--      migrate_fb1_nodes_acl.sql 已經對該 tenant 跑過。
--   2. document_acl 目前的規則數，回填到 acl_entries 後數量不能變少
--      （允許增加，因為 FB-1 遷移一條 detail/summary/deny 規則會展開成
--      多條 action-level 規則）。這是抓「migrate_fb1 忘了跑」或「跑到一半」
--      的保護網，不是精確的資料對帳。
--
-- 用法（k8s dev，正式執行前務必先在複本資料庫演練）：
--   kubectl -n <ns> exec -i <postgres-pod> -- \
--     psql -U <user> -d <db> -v ON_ERROR_STOP=1 < scripts/migrate_fb5_drop_legacy.sql
--
-- 冪等性：三張表都用 DROP TABLE IF EXISTS，重跑第二次時前置檢查會因為
-- document_acl 已經不存在而直接跳過（不會誤判成「忘了跑 FB-1」）。

BEGIN;

DO $$
DECLARE
  missing_tenant text;
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'document_acl'
  ) THEN
    RAISE NOTICE 'document_acl 已不存在，視為此腳本已執行過，跳過前置檢查與 DROP';
    RETURN;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name IN ('nodes', 'acl_entries')
    GROUP BY 1 HAVING count(*) = 2
  ) THEN
    RAISE EXCEPTION 'FB-5 清理中止：nodes/acl_entries 兩張表還不存在——'
      '請先執行 scripts/migrate_fb1_nodes_acl.sql（它會順便建好這兩張表），再重跑本腳本。';
  END IF;

  -- 檢查 1：有 documents 資料的 tenant 必須也有 nodes 資料
  SELECT d.tenant_id INTO missing_tenant
  FROM documents d
  WHERE NOT EXISTS (SELECT 1 FROM nodes n WHERE n.tenant_id = d.tenant_id)
  LIMIT 1;

  IF missing_tenant IS NOT NULL THEN
    RAISE EXCEPTION 'FB-5 清理中止：tenant ''%'' 有 documents 資料但 nodes 是空的——'
      '看起來 scripts/migrate_fb1_nodes_acl.sql 還沒對這個 tenant 執行過。'
      '請先確認 FB-1 遷移已完成，再重跑本腳本。', missing_tenant;
  END IF;

  -- 檢查 2：acl_entries 的規則數不能少於 document_acl（粗略防呆，見上方註解）
  IF (SELECT count(*) FROM acl_entries) < (SELECT count(*) FROM document_acl) THEN
    RAISE EXCEPTION 'FB-5 清理中止：acl_entries 規則數（%）少於 document_acl（%）,'
      '看起來 FB-1 遷移可能沒有完整跑完。請先查明再重跑本腳本。',
      (SELECT count(*) FROM acl_entries), (SELECT count(*) FROM document_acl);
  END IF;

  RAISE NOTICE '前置檢查通過，開始 drop document_acl / summary_chunks / document_sum';
END $$;

DROP TABLE IF EXISTS document_acl CASCADE;
DROP TABLE IF EXISTS summary_chunks CASCADE;
DROP TABLE IF EXISTS document_sum CASCADE;
ALTER TABLE documents DROP COLUMN IF EXISTS md_path;

COMMIT;

-- 驗證查詢（清理後手動執行）：
--   SELECT table_name FROM information_schema.tables
--   WHERE table_schema = 'public'
--     AND table_name IN ('document_acl', 'summary_chunks', 'document_sum');
--   -- 應該回傳 0 rows
--   SELECT column_name FROM information_schema.columns
--   WHERE table_schema = 'public' AND table_name = 'documents' AND column_name = 'md_path';
--   -- 應該回傳 0 rows
