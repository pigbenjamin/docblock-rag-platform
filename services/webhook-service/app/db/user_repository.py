from docblock_core import sql_utils

#class UserRepository:
#    def __init__(self, pg_dsn: str):
#        self.pg_dsn = pg_dsn
#
#    def update_user(self, user: dict):
#        conn = sql_util_for_keycloak.get_conn(self.pg_dsn)
#
#        try:
#            cur = conn.cursor()
#            sql_util_for_keycloak.update_user(cur, user)
#            conn.commit()
#        except Exception:
#            conn.rollback()
#            raise
#        finally:
#            conn.close()
            
            
class UserRepository:
    def __init__(self, pg_dsn: str, tenant_id: str):
        self.pg_dsn = pg_dsn
        self.tenant_id = tenant_id

    def sync_user_principals(self, user_id: str, principals: list[dict]):
        conn = sql_utils.get_conn(self.pg_dsn)

        try:
            cur = conn.cursor()

            sql_utils.delete_user_principals(cur, self.tenant_id, user_id)

            for p in principals:
                sql_utils.write_user_principal(
                    cur,
                    tenant_id=self.tenant_id,
                    user_id=user_id,
                    principal_type=p["principal_type"],
                    principal_id=p["principal_id"],
                )
            conn.commit()

        except Exception:
            conn.rollback()
            raise

        finally:
            conn.close()

    # 部門成員預設能看見自己部門與 Public 的內容；這兩件事都掛在根資料夾的
    # acl_entries 上（見 FB-1 遷移的步驟 1/2）。部門隨 HR 同步動態出現時，
    # 由這裡補齊同樣的基礎設施。全部 ON CONFLICT DO NOTHING：已存在的
    # 資料夾/entries（含管理員後來手動改過的）原封不動，只增不減。
    _INSERT_ROOT_FOLDER = """
        INSERT INTO nodes (id, tenant_id, parent_id, node_type, name,
                           owner_department_id, inherit_acl, path_cache)
        VALUES (gen_random_uuid(), %(tenant)s, NULL, 'folder', %(name)s,
                %(owner)s, false, '/' || %(name)s)
        ON CONFLICT (tenant_id, name) WHERE parent_id IS NULL DO NOTHING
    """

    _SELECT_ROOT_FOLDER = """
        SELECT id FROM nodes
        WHERE tenant_id = %(tenant)s AND parent_id IS NULL
          AND node_type = 'folder' AND name = %(name)s
    """

    _INSERT_ALLOW_ENTRIES = """
        INSERT INTO acl_entries (tenant_id, node_id, subject_type, subject_id,
                                 action, effect, inherit_to_children)
        SELECT %(tenant)s, %(node_id)s, 'department', %(dept)s,
               a.action, 'allow', true
        FROM (VALUES ('browse'), ('query'), ('read')) AS a(action)
        ON CONFLICT DO NOTHING
    """

    def ensure_department_infrastructure(self, departments: list[str]):
        conn = sql_utils.get_conn(self.pg_dsn)

        try:
            cur = conn.cursor()

            # Public 根資料夾：無條件確保存在（owner='Public'，由全域管理員管）
            public_params = {"tenant": self.tenant_id, "name": "Public", "owner": "Public"}
            cur.execute(self._INSERT_ROOT_FOLDER, public_params)
            cur.execute(self._SELECT_ROOT_FOLDER, public_params)
            public_id = cur.fetchone()[0]

            for dept in departments:
                params = {"tenant": self.tenant_id, "name": dept, "owner": dept}
                cur.execute(self._INSERT_ROOT_FOLDER, params)
                cur.execute(self._SELECT_ROOT_FOLDER, params)
                dept_root_id = cur.fetchone()[0]

                cur.execute(self._INSERT_ALLOW_ENTRIES, {
                    "tenant": self.tenant_id, "node_id": dept_root_id, "dept": dept,
                })
                cur.execute(self._INSERT_ALLOW_ENTRIES, {
                    "tenant": self.tenant_id, "node_id": public_id, "dept": dept,
                })

            conn.commit()

        except Exception:
            conn.rollback()
            raise

        finally:
            conn.close()