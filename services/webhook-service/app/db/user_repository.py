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