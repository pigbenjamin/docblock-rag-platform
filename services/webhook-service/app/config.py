import os
from dataclasses import dataclass, field


@dataclass
class DBSettings:
    pg_dsn: str = os.getenv("PG_DSN", "dbname=acl_FIRDI user=ai-x password=86891972 host=localhost port=5435")
    tenant_id: str = os.getenv("TENANT_ID", "firdi")


@dataclass
class AclSettings:
    ADMIN_SECRET: str = os.getenv("ACL_ADMIN_SECRET", "acl-admin-secret-46804311")


@dataclass
class KeycloakSettings:
    KEYCLOAK_URL: str = os.getenv("KEYCLOAK_URL", "https://125.228.83.116:49314/")
    KEYCLOAK_REALM: str = os.getenv("KEYCLOAK_REALM", "FIRDI-AI-Platform")
    CLIENT_ID = os.getenv("KEYCLOAK_CLIENT_ID", "user-sync-service")
    CLIENT_SECRET = os.getenv("KEYCLOAK_CLIENT_SECRET", "cTlbvOSd7xVYkCshvxY58LUiH7qgySrM")
    WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "firdi-webhook-secret-46804311")


@dataclass
class AppSettings:
    acl: AclSettings = field(default_factory=AclSettings)
    keycloak: KeycloakSettings = field(default_factory=KeycloakSettings)
    db: DBSettings = field(default_factory=DBSettings)


settings = AppSettings()